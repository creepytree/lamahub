"""HuggingFace GGUF browse + shards-direct deploy into Ollama.

Search and repo inspection use HF's official public API (no scraping); deploy
follows the proven shards-direct path: download each shard to the staging
cache (resumable, sha256 hashed in the same pass), upload every shard as its
own blob (HEAD dedupe first), then POST /api/create with all shards in the
``files`` map — Ollama's own GGUF parser assembles the split. No merge binary,
no 2x disk. See HF_DEPLOY_DESIGN.md.
"""

import asyncio
import hashlib
import json
import os
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from lamahub.env import env
from lamahub.extensions import logger
from lamahub.services import staging_store

HF_BASE = "https://huggingface.co"

# Only one deploy at a time: parallel multi-GB downloads thrash disk and net.
# Deploys run as a detached background task (run_deploy) so a browser reload or
# tab close does NOT abort them — the client only observes live progress through
# the /hf/deploy/status poll. `deploy_active` is the synchronous start guard;
# `current_deploy` holds the latest progress of the in-flight (or last) deploy.
deploy_active = False
current_deploy: dict[str, Any] | None = None

# family bases like ".../model-Q4_K_M" carry the quant as their suffix
_SHARD_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.I)
_QUANT_RE = re.compile(r"((?:UD-)?(?:I?Q|BF16|F16|F32|TQ|MXFP)[A-Z0-9_]*)$", re.I)

# small TTL caches so repeated searches/expands don't re-hit HF
_search_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_quants_cache: dict[str, tuple[float, list[dict]]] = {}
_SEARCH_TTL = 90.0
_QUANTS_TTL = 600.0


def _hf_headers() -> dict[str, str]:
    headers = {"User-Agent": "lamahub"}
    if env.hf_token:
        headers["Authorization"] = f"Bearer {env.hf_token}"
    return headers


def _group_families(siblings: list[dict]) -> dict[str, list[dict]]:
    """Group .gguf files into quant families {family_base: [file, ...]}.

    A family is either one single .gguf or all shards of a -NNNNN-of-NNNNN
    split. mmproj companion files (vision projectors) are skipped — they are
    not standalone quants (v1 deploys the text weights only).
    """
    families: dict[str, list[dict]] = {}
    for sibling in siblings:
        path = sibling.get("rfilename", "")
        if not path.lower().endswith(".gguf"):
            continue
        if os.path.basename(path).lower().startswith("mmproj"):
            continue
        match = _SHARD_RE.search(path)
        base = path[: match.start()] if match else path[:-5]
        families.setdefault(base, []).append(sibling)
    return families


def _quant_label(family_base: str) -> str:
    """Human quant label for a family base, e.g. Q4_K_M or UD-IQ2_M."""
    name = os.path.basename(family_base)
    match = _QUANT_RE.search(name)
    return match.group(1) if match else name


async def search_models(query: str, cursor: str = "") -> dict[str, Any]:
    """Search GGUF repos on HF; returns {items, next_cursor}.

    filter=gguf keeps it to Ollama-ingestible repos; HF's search param is a
    native token-AND over the repo id (so "qwen 122" works). Pagination is
    cursor-based via the response Link header. Cached briefly per (query,
    cursor) — searches only fire on an explicit button/Enter, no debounce.
    """
    cache_key = (query, cursor)
    cached = _search_cache.get(cache_key)
    if cached and cached[0] > time.time():
        return cached[1]

    params: dict[str, str] = {
        "filter": "gguf",
        "sort": "downloads",
        "direction": "-1",
        "limit": "30",
        "full": "true",
    }
    if query:
        params["search"] = query
    if cursor:
        params["cursor"] = cursor

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(f"{HF_BASE}/api/models", params=params, headers=_hf_headers())
            response.raise_for_status()
    except Exception as e:
        logger.error(f"HF search failed: {e}")
        return {"items": [], "next_cursor": "", "error": str(e)}

    next_cursor = ""
    next_link = response.links.get("next", {}).get("url", "")
    if next_link:
        next_cursor = parse_qs(urlparse(next_link).query).get("cursor", [""])[0]

    items = []
    for model in response.json():
        families = _group_families(model.get("siblings") or [])
        if not families:
            continue
        items.append(
            {
                "id": model.get("id") or model.get("modelId"),
                "author": model.get("author") or "",
                "downloads": model.get("downloads") or 0,
                "likes": model.get("likes") or 0,
                "updated": model.get("lastModified") or "",
                "pipeline": model.get("pipeline_tag") or "",
                "gated": bool(model.get("gated")),
                "quant_count": len(families),
            }
        )

    result = {"items": items, "next_cursor": next_cursor}
    _search_cache[cache_key] = (time.time() + _SEARCH_TTL, result)
    logger.info(f"HF search '{query}' -> {len(items)} repos (cursor={'yes' if cursor else 'no'})")
    return result


async def repo_quants(repo: str) -> list[dict]:
    """Quant families of a repo with real byte sizes (?blobs=true), cached.

    Each entry: {family, label, shards: [{name, size}], total_size}.
    """
    cached = _quants_cache.get(repo)
    if cached and cached[0] > time.time():
        return cached[1]

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(
                f"{HF_BASE}/api/models/{repo}", params={"blobs": "true"}, headers=_hf_headers()
            )
            response.raise_for_status()
    except Exception as e:
        logger.error(f"HF repo quants failed for {repo}: {e}")
        return []

    quants = []
    for base, files in _group_families(response.json().get("siblings") or []).items():
        shards = sorted(
            ({"name": f["rfilename"], "size": f.get("size") or 0} for f in files),
            key=lambda s: s["name"],
        )
        quants.append(
            {
                "family": base,
                "label": _quant_label(base),
                "shards": shards,
                "total_size": sum(s["size"] for s in shards),
            }
        )
    quants.sort(key=lambda q: q["total_size"])
    _quants_cache[repo] = (time.time() + _QUANTS_TTL, quants)
    logger.info(f"HF repo {repo}: {len(quants)} quant families")
    return quants


def _prehash(path: str) -> "hashlib._Hash":
    """Hash an existing partial file so a resumed download continues the digest."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest


async def _download_shard(client: httpx.AsyncClient, repo: str, shard: dict, dest: str):
    """Stream one shard to disk (Range-resumable), yielding byte progress.

    Yields (completed, total) tuples; returns via a final ("sha256", digest)
    marker tuple. The sha256 is computed in the same pass as the write.
    """
    url = f"{HF_BASE}/{repo}/resolve/main/{shard['name']}"
    total = shard["size"]
    have = os.path.getsize(dest) if os.path.exists(dest) else 0

    if have and have == total:
        digest = await asyncio.to_thread(_prehash, dest)
        yield ("sha256", digest.hexdigest())
        return

    if have:
        digest = await asyncio.to_thread(_prehash, dest)
        headers = {**_hf_headers(), "Range": f"bytes={have}-"}
        mode = "ab"
    else:
        digest = hashlib.sha256()
        headers = _hf_headers()
        mode = "wb"

    async with client.stream("GET", url, headers=headers) as response:
        if response.status_code == 200 and mode == "ab":
            # server ignored the Range request: restart from scratch
            digest, mode, have = hashlib.sha256(), "wb", 0
        response.raise_for_status()
        last_report = 0.0
        with open(dest, mode) as fh:
            async for chunk in response.aiter_bytes(1 << 20):
                fh.write(chunk)
                digest.update(chunk)
                have += len(chunk)
                now = time.monotonic()
                if now - last_report > 0.5:
                    last_report = now
                    yield (have, total)
    yield (have, total)
    yield ("sha256", digest.hexdigest())


async def _upload_blob(client: httpx.AsyncClient, base_url: str, path: str, sha256: str):
    """Upload a shard file as an Ollama blob, yielding (sent, total) progress.

    HEAD-checks first so shards already on the endpoint skip instantly.
    """
    blob = f"sha256:{sha256}"
    head = await client.head(f"{base_url}/api/blobs/{blob}")
    total = os.path.getsize(path)
    if head.status_code == 200:
        logger.info(f"Blob already on endpoint (dedupe): {os.path.basename(path)}")
        yield (total, total)
        return
    logger.info(f"Uploading blob {os.path.basename(path)} ({total / 1e9:.2f} GB)")

    sent = 0

    async def chunks():
        nonlocal sent
        with open(path, "rb") as fh:
            while True:
                block = fh.read(1 << 20)
                if not block:
                    return
                sent += len(block)
                yield block

    task = asyncio.create_task(
        client.post(
            f"{base_url}/api/blobs/{blob}",
            content=chunks(),
            headers={"Content-Length": str(total)},
        )
    )
    while not task.done():
        yield (sent, total)
        await asyncio.sleep(0.5)
    response = await task
    if response.status_code not in (200, 201):
        raise RuntimeError(f"blob upload failed ({response.status_code}): {response.text[:200]}")
    yield (total, total)


async def deploy_family(base_url: str, repo: str, family: str, model_name: str):
    """Full shards-direct deploy of one quant family, yielding progress JSON.

    Stages: download (resumable, hashes inline) -> upload (blob per shard,
    HEAD dedupe) -> create (Ollama assembles the split; its stream is relayed).
    Progress events: {"stage", "status", "file", "shard", "total_shards",
    "completed", "total"} — mirrored after Ollama's own pull progress shape so
    the frontend strip logic carries over.
    """
    quants = await repo_quants(repo)
    match = next((q for q in quants if q["family"] == family), None)
    if match is None:
        yield json.dumps({"error": f"quant family not found: {family}"})
        return
    shards = match["shards"]

    fam_id = staging_store.family_id(repo, match["label"])
    staging_store.auto_prune(protect=fam_id, incoming_bytes=match["total_size"])
    meta = staging_store.get_meta(fam_id) or {
        "repo": repo,
        "quant": match["label"],
        "family": family,
        "model_name": model_name,
        "shards": [{"name": s["name"], "size": s["size"], "sha256": None} for s in shards],
        "status": "downloading",
        "endpoints": [],
        "created": time.time(),
    }
    meta["last_used"] = time.time()
    meta["model_name"] = model_name
    staging_store.save_meta(fam_id, meta)
    known_sha = {s["name"]: s.get("sha256") for s in meta.get("shards", [])}

    total_shards = len(shards)
    digests: dict[str, str] = {}
    try:
        # 1) download all shards into the staging family dir
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as hf_client:
            for index, shard in enumerate(shards, start=1):
                dest = os.path.join(staging_store.family_dir(fam_id), os.path.basename(shard["name"]))
                complete = os.path.exists(dest) and os.path.getsize(dest) == shard["size"]
                if complete and known_sha.get(shard["name"]):
                    digests[shard["name"]] = known_sha[shard["name"]]
                    logger.info(f"Shard cached, skipping download: {os.path.basename(shard['name'])}")
                    continue
                logger.info(
                    f"Downloading shard {index}/{total_shards}: "
                    f"{os.path.basename(shard['name'])} ({shard['size'] / 1e9:.2f} GB)"
                )
                async for event in _download_shard(hf_client, repo, shard, dest):
                    if event[0] == "sha256":
                        digests[shard["name"]] = event[1]
                    else:
                        yield json.dumps(
                            {
                                "stage": "download",
                                "status": f"downloading {os.path.basename(shard['name'])} ({index}/{total_shards})",
                                "shard": index,
                                "total_shards": total_shards,
                                "completed": event[0],
                                "total": event[1],
                            }
                        )
                for entry in meta["shards"]:
                    if entry["name"] == shard["name"]:
                        entry["sha256"] = digests[shard["name"]]
                meta["status"] = "downloading"
                staging_store.save_meta(fam_id, meta)

        meta["status"] = "staged"
        staging_store.save_meta(fam_id, meta)

        # 2) upload every shard as its own blob on the target endpoint
        async with httpx.AsyncClient(timeout=None) as ollama_client:
            for index, shard in enumerate(shards, start=1):
                path = os.path.join(staging_store.family_dir(fam_id), os.path.basename(shard["name"]))
                async for sent, total in _upload_blob(ollama_client, base_url, path, digests[shard["name"]]):
                    yield json.dumps(
                        {
                            "stage": "upload",
                            "status": f"uploading {os.path.basename(shard['name'])} ({index}/{total_shards})",
                            "shard": index,
                            "total_shards": total_shards,
                            "completed": sent,
                            "total": total,
                        }
                    )

            # 3) create: all shards in the files map, Ollama assembles the split
            files = {os.path.basename(s["name"]): f"sha256:{digests[s['name']]}" for s in shards}
            logger.info(f"Creating {model_name} from {len(files)} shard blob(s) on {base_url}")
            async with ollama_client.stream(
                "POST", f"{base_url}/api/create", json={"model": model_name, "files": files}
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if message.get("error"):
                        yield json.dumps({"error": message["error"]})
                        return
                    yield json.dumps({"stage": "create", "status": message.get("status", "")})

        meta["status"] = "deployed"
        if base_url not in meta["endpoints"]:
            meta["endpoints"].append(base_url)
        meta["last_used"] = time.time()
        staging_store.save_meta(fam_id, meta)
        logger.info(f"Deployed {repo} [{match['label']}] as {model_name} on {base_url}")
        yield json.dumps({"stage": "done", "status": "success", "model": model_name})
    except Exception as e:
        logger.error(f"Deploy failed for {repo} [{family}]: {e}")
        staging_store.save_meta(fam_id, meta)
        yield json.dumps({"error": str(e)})


async def run_deploy(base_url: str, repo: str, family: str, model_name: str) -> None:
    """Run a deploy detached from any request, recording progress to memory.

    Survives browser reloads and tab closes (a 50 GB pull can take an hour): the
    client kicks this off with a POST, then watches ``current_deploy`` via the
    status poll. Holds ``deploy_active`` for the whole run so a second deploy is
    refused up front rather than racing on disk and network.
    """
    global deploy_active, current_deploy
    current_deploy = {
        "active": True,
        "repo": repo,
        "family": family,
        "model_name": model_name,
        "endpoint": base_url,
        "stage": "starting",
        "status": "starting",
        "shard": 0,
        "total_shards": 0,
        "completed": 0,
        "total": 0,
        "error": None,
        "model": None,
    }
    try:
        async for line in deploy_family(base_url, repo, family, model_name):
            message = json.loads(line)
            if message.get("error"):
                current_deploy.update(active=False, error=message["error"])
                return
            current_deploy.update(
                stage=message.get("stage", current_deploy["stage"]),
                status=message.get("status", current_deploy["status"]),
                shard=message.get("shard", 0),
                total_shards=message.get("total_shards", 0),
                completed=message.get("completed", 0),
                total=message.get("total", 0),
            )
            if message.get("stage") == "done":
                current_deploy.update(active=False, model=message.get("model"))
    except Exception as e:
        logger.error(f"Deploy task crashed: {e}")
        current_deploy.update(active=False, error=str(e))
    finally:
        current_deploy["active"] = False
        deploy_active = False
