"""HuggingFace Hub browse: GGUF repo search and per-repo quant families.

Uses HF's official public API (no scraping). A repo's .gguf files are grouped
into quant families — one single file or all shards of a -NNNNN-of-NNNNN split
— which is the unit hf_deploy stages and deploys. See HF_DEPLOY_DESIGN.md.
"""

import os
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from lamahub.env import env
from lamahub.extensions import logger

HF_BASE = "https://huggingface.co"

# family bases like ".../model-Q4_K_M" carry the quant as their suffix
_SHARD_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.I)
_QUANT_RE = re.compile(r"((?:UD-)?(?:I?Q|BF16|F16|F32|TQ|MXFP)[A-Z0-9_]*)$", re.I)

# small TTL caches so repeated searches/expands don't re-hit HF
_search_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_quants_cache: dict[str, tuple[float, list[dict]]] = {}
_SEARCH_TTL = 90.0
_QUANTS_TTL = 600.0


def hf_headers() -> dict[str, str]:
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
            response = await client.get(f"{HF_BASE}/api/models", params=params, headers=hf_headers())
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
            response = await client.get(f"{HF_BASE}/api/models/{repo}", params={"blobs": "true"}, headers=hf_headers())
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
