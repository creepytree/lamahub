"""Shards-direct deploy of HF GGUF quant families into Ollama, plus the job queue.

Follows the proven shards-direct path: download each shard to the staging
cache (resumable, sha256 hashed in the same pass), upload every shard as its
own blob (HEAD dedupe first), then POST /api/create with all shards in the
``files`` map — Ollama's own GGUF parser assembles the split. No merge binary,
no 2x disk. Browsing (search, quant families) lives in hf_hub. See
HF_DEPLOY_DESIGN.md.
"""

import asyncio
import hashlib
import json
import os
import time
from typing import Any

import httpx

from lamahub.extensions import logger
from lamahub.services import hf_hub, staging_store

# Long multi-GB pulls routinely hit a dropped connection ("peer closed
# connection without sending complete message body"). Each shard download is
# retried in place — it resumes from the bytes already on disk via a Range
# request, so a retry costs nothing already fetched.
# Retries count consecutive failures without progress: an attempt that moved
# bytes resets the counter, so a many-hour pull survives any number of
# isolated stalls while a truly dead link still gives up.
_DL_RETRIES = 6

# Never timeout=None: a connection that is accepted and then stalls (CDN,
# proxy, flaky link) would block the read forever — no exception, so no
# retry and no log line (seen in production: frozen at 3.28/15.3 GB for
# hours). A bounded read timeout turns the stall into httpx.ReadTimeout, a
# TransportError, which the resume path below handles.
_HF_TIMEOUT = httpx.Timeout(60.0, connect=20.0)
# Ollama answers a blob POST only after digesting the whole upload, and
# /api/create can sit quietly while it parses a large GGUF: longer reads.
_OLLAMA_TIMEOUT = httpx.Timeout(60.0, connect=20.0, read=900.0)
# How often a running transfer writes a progress line to the log.
_LOG_EVERY = 60.0


def _progress(
    stage: str, action: str, index: int, total_shards: int, completed: int, total: int, suffix: str = ""
) -> dict[str, Any]:
    """One per-shard progress event (see deploy_family)."""
    return {
        "stage": stage,
        "status": f"{action} ({index}/{total_shards}){suffix}",
        "shard": index,
        "total_shards": total_shards,
        "completed": completed,
        "total": total,
    }


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
    url = f"{hf_hub.HF_BASE}/{repo}/resolve/main/{shard['name']}"
    total = shard["size"]
    have = os.path.getsize(dest) if os.path.exists(dest) else 0

    if have and have == total:
        digest = await asyncio.to_thread(_prehash, dest)
        yield ("sha256", digest.hexdigest())
        return

    if have:
        digest = await asyncio.to_thread(_prehash, dest)
        headers = {**hf_hub.hf_headers(), "Range": f"bytes={have}-"}
        mode = "ab"
    else:
        digest = hashlib.sha256()
        headers = hf_hub.hf_headers()
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
    """Full shards-direct deploy of one quant family, yielding progress dicts.

    Stages: download (resumable, hashes inline) -> upload (blob per shard,
    HEAD dedupe) -> create (Ollama assembles the split; its stream is relayed).
    Progress events: {"stage", "status", "file", "shard", "total_shards",
    "completed", "total"} — mirrored after Ollama's own pull progress shape so
    the frontend strip logic carries over.
    """
    quants = await hf_hub.repo_quants(repo)
    match = next((q for q in quants if q["family"] == family), None)
    if match is None:
        yield {"error": f"quant family not found: {family}"}
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
        async with httpx.AsyncClient(timeout=_HF_TIMEOUT, follow_redirects=True) as hf_client:
            for index, shard in enumerate(shards, start=1):
                name = os.path.basename(shard["name"])
                dest = os.path.join(staging_store.family_dir(fam_id), name)
                complete = os.path.exists(dest) and os.path.getsize(dest) == shard["size"]
                if complete and known_sha.get(shard["name"]):
                    digests[shard["name"]] = known_sha[shard["name"]]
                    logger.info(f"Shard cached, skipping download: {name}")
                    continue
                logger.info(f"Downloading shard {index}/{total_shards}: {name} ({shard['size'] / 1e9:.2f} GB)")
                # a partial (or unhashed complete) file is re-read to seed the digest
                # before any byte moves; say so, or the strip sits on "starting"
                have = os.path.getsize(dest) if os.path.exists(dest) else 0
                if have:
                    yield _progress("download", f"verifying {name}", index, total_shards, have, shard["size"])
                attempt = 0
                while True:
                    start_bytes = os.path.getsize(dest) if os.path.exists(dest) else 0
                    try:
                        async for event in _download_shard(hf_client, repo, shard, dest):
                            if event[0] == "sha256":
                                digests[shard["name"]] = event[1]
                            else:
                                yield _progress("download", f"downloading {name}", index, total_shards, *event)
                        break  # shard finished cleanly
                    except httpx.TransportError as e:
                        # network/stream drop mid-download — resume from the partial
                        # file (a Range request) rather than losing what we have
                        have = os.path.getsize(dest) if os.path.exists(dest) else 0
                        attempt = 1 if have > start_bytes else attempt + 1
                        if attempt > _DL_RETRIES:
                            raise RuntimeError(f"download failed after {_DL_RETRIES} retries ({name}): {e}") from e
                        wait = min(2**attempt, 30)
                        logger.warning(
                            f"Shard download interrupted at {have / 1e9:.2f}/{shard['size'] / 1e9:.2f} GB "
                            f"({type(e).__name__}: {str(e) or 'no data within the read timeout'}); retry {attempt}/{_DL_RETRIES} "
                            f"in {wait}s (resuming)"
                        )
                        yield _progress(
                            "download",
                            f"reconnecting {name}",
                            index,
                            total_shards,
                            have,
                            shard["size"],
                            suffix=f" — retry {attempt}/{_DL_RETRIES}",
                        )
                        await asyncio.sleep(wait)
                for entry in meta["shards"]:
                    if entry["name"] == shard["name"]:
                        entry["sha256"] = digests[shard["name"]]
                meta["status"] = "downloading"
                staging_store.save_meta(fam_id, meta)

        meta["status"] = "staged"
        staging_store.save_meta(fam_id, meta)

        # 2) upload every shard as its own blob on the target endpoint
        async with httpx.AsyncClient(timeout=_OLLAMA_TIMEOUT) as ollama_client:
            for index, shard in enumerate(shards, start=1):
                name = os.path.basename(shard["name"])
                path = os.path.join(staging_store.family_dir(fam_id), name)
                async for sent, total in _upload_blob(ollama_client, base_url, path, digests[shard["name"]]):
                    yield _progress("upload", f"uploading {name}", index, total_shards, sent, total)

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
                        yield {"error": message["error"]}
                        return
                    yield {"stage": "create", "status": message.get("status", "")}

        meta["status"] = "deployed"
        if base_url not in meta["endpoints"]:
            meta["endpoints"].append(base_url)
        meta["last_used"] = time.time()
        staging_store.save_meta(fam_id, meta)
        logger.info(f"Deployed {repo} [{match['label']}] as {model_name} on {base_url}")
        yield {"stage": "done", "status": "success", "model": model_name}
    except Exception as e:
        logger.error(f"Deploy failed for {repo} [{family}]: {e}")
        staging_store.save_meta(fam_id, meta)
        yield {"error": str(e)}


def _progress_line(record: dict[str, Any]) -> str:
    """'download 1/2: 3.28/15.31 GB (21%) at 11.1 MB/s, ~18 min left' for the log."""
    done, total, rate = record["completed"], record["total"], record["rate"]
    line = (
        f"{record['stage']} {record['shard']}/{record['total_shards']}: "
        f"{done / 1e9:.2f}/{total / 1e9:.2f} GB ({done * 100 // total}%)"
    )
    if rate > 0:
        minutes = (total - done) / rate / 60
        line += f" at {rate / 1e6:.1f} MB/s, " + (f"~{minutes:.0f} min left" if minutes >= 1 else "<1 min left")
    return line


class _RateMeter:
    """Transfer speed over a sliding window of (time, bytes) samples.

    Restarts whenever the counter changes meaning (new stage or shard) or runs
    backwards (a download restarted from scratch), so a rate never mixes two
    transfers.
    """

    WINDOW = 5.0  # seconds

    def __init__(self) -> None:
        self._key: tuple | None = None
        self._samples: list[tuple[float, int]] = []

    def current(self) -> float:
        """The last rate, or 0.0 once no sample arrived within the window."""
        if not self._samples or time.monotonic() - self._samples[-1][0] > self.WINDOW:
            return 0.0
        (t0, c0), (t1, c1) = self._samples[0], self._samples[-1]
        return (c1 - c0) / (t1 - t0) if t1 > t0 else 0.0

    def update(self, key: tuple, completed: int) -> float:
        """Record a sample and return bytes/s (0.0 until two samples span time)."""
        now = time.monotonic()
        if key != self._key or (self._samples and completed < self._samples[-1][1]):
            self._key, self._samples = key, []
        self._samples.append((now, completed))
        self._samples = [(t, c) for t, c in self._samples if now - t <= self.WINDOW]
        (t0, c0), (t1, c1) = self._samples[0], self._samples[-1]
        return (c1 - c0) / (t1 - t0) if t1 > t0 else 0.0


class DeployQueue:
    """Serialized deploy jobs — the single source of truth for deploy state.

    Only one deploy runs at a time (parallel multi-GB downloads thrash disk and
    net); further jobs wait FIFO, drained by one worker task. Everything runs
    detached from requests, so a browser reload or tab close does NOT abort a
    download — the client only observes via the /hf/deploy/status poll.

    ``active`` is True while the worker runs, ``current`` is the live progress
    record of the in-flight job, ``waiting`` the queued jobs and ``history`` the
    last finished records, so the client can report each result even when the
    next job starts between two polls. In memory only: a restart drops the queue.
    """

    HISTORY_MAX = 20

    def __init__(self) -> None:
        self.active = False
        self.current: dict[str, Any] | None = None
        self.waiting: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self._next_id = 0
        self._meter = _RateMeter()

    @staticmethod
    def _key(job: dict[str, Any]) -> tuple[str, str, str]:
        return (job["endpoint"], job["repo"], job["family"])

    def enqueue(self, base_url: str, repo: str, family: str, model_name: str) -> dict[str, Any]:
        """Queue a deploy and start the worker if idle. Synchronous (no await), so
        concurrent requests can't race on the queue.

        Returns ``{"status": "started"|"queued", "id", "position"}`` or an error when
        the same family is already queued or deploying to the same endpoint.
        """
        job = {"endpoint": base_url, "repo": repo, "family": family, "model_name": model_name}
        pending = self.waiting + ([self.current] if self.active and self.current else [])
        if any(self._key(other) == self._key(job) for other in pending):
            return {"status": "error", "message": f"{repo} is already queued for this endpoint"}
        self._next_id += 1
        job["id"] = self._next_id
        if self.active:
            self.waiting.append(job)
            return {"status": "queued", "id": job["id"], "position": len(self.waiting)}
        self.active = True
        self._begin(job)
        asyncio.create_task(self._drain())
        return {"status": "started", "id": job["id"], "position": 0}

    def cancel(self, job_id: int) -> bool:
        """Drop a job that is still waiting (the running one can't be cancelled)."""
        for job in self.waiting:
            if job["id"] == job_id:
                self.waiting.remove(job)
                return True
        return False

    def status(self) -> dict[str, Any]:
        """Snapshot for the client poll: live job, waiting jobs, recent results."""
        return {
            "active": self.active,
            # rate is re-read at poll time so a stalled transfer shows 0, not the
            # last speed it had before the events stopped
            "current": {**self.current, "rate": round(self._meter.current())} if self.active and self.current else None,
            "queue": self.waiting,
            "history": self.history,
        }

    def _begin(self, job: dict[str, Any]) -> None:
        """Make ``job`` the in-flight deploy (fresh progress record)."""
        self.current = {
            **job,
            "active": True,
            "stage": "starting",
            "status": "starting",
            "shard": 0,
            "total_shards": 0,
            "completed": 0,
            "total": 0,
            "rate": 0,
            "error": None,
            "model": None,
        }

    async def _drain(self) -> None:
        """Worker: run the begun deploy, then the queued ones, until none wait."""
        try:
            while True:
                await self._run()
                if not self.waiting:
                    break
                self._begin(self.waiting.pop(0))
        finally:
            self.active = False

    async def _run(self) -> None:
        """Run the in-flight deploy, mirroring its progress events onto ``current``."""
        record = self.current
        assert record is not None
        base_url, repo, family = record["endpoint"], record["repo"], record["family"]
        logger.info(f"Deploying HF {repo} [{family}] as {record['model_name']} on {base_url}")
        meter = self._meter = _RateMeter()
        last_log = 0.0
        try:
            async for event in deploy_family(base_url, repo, family, record["model_name"]):
                if event.get("error"):
                    record["error"] = event["error"]
                    return
                record.update(
                    stage=event.get("stage", record["stage"]),
                    status=event.get("status", record["status"]),
                    shard=event.get("shard", 0),
                    total_shards=event.get("total_shards", 0),
                    completed=event.get("completed", 0),
                    total=event.get("total", 0),
                )
                # bytes/s of the live transfer; 0 for events without byte progress
                record["rate"] = (
                    round(meter.update((record["stage"], record["shard"]), record["completed"]))
                    if record["total"]
                    else 0
                )
                now = time.monotonic()
                if record["total"] and now - last_log >= _LOG_EVERY:
                    last_log = now
                    logger.info(f"Deploy {record['model_name']}: {_progress_line(record)}")
                if event.get("stage") == "done":
                    record["model"] = event.get("model")
        except Exception as e:
            logger.error(f"Deploy task crashed: {e}")
            record["error"] = str(e)
        finally:
            record["active"] = False
            record["rate"] = 0
            self.history.append(dict(record))
            del self.history[: -self.HISTORY_MAX]


deploys = DeployQueue()
