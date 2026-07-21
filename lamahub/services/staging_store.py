"""Disk cache of downloaded HF shard families awaiting/after deploy.

Each staged family (one quant of one repo) lives in
instance/hf_staging/<repo>__<quant>/ holding its .gguf shards plus a meta.json
describing them. The store is a cache, not scratch: shards downloaded once can
be deployed to another endpoint without re-downloading, and interrupted
downloads resume from the partial files.

A hard total-size cap (HF_STAGING_MAX_GB) is enforced by evicting the
least-recently-used complete families; the family currently being staged or
deployed is never evicted. See HF_DEPLOY_DESIGN.md.
"""

import json
import os
import re
import shutil
import threading
import time

from lamahub.config import INSTANCE_PATH
from lamahub.env import env
from lamahub.extensions import logger

STAGING_PATH = os.path.join(INSTANCE_PATH, "hf_staging")
_lock = threading.Lock()

# meta.json shape:
# {
#   "repo": "unsloth/Qwen3-...-GGUF", "quant": "Q4_K_M", "subfolder": "",
#   "model_name": "qwen3-...:q4_k_m",
#   "shards": [{"name": "...-00001-of-00002.gguf", "size": int,
#               "sha256": str|None, "complete": bool}],
#   "status": "downloading" | "staged" | "deployed",
#   "endpoints": ["http://..."],          # endpoints this family was deployed to
#   "created": epoch, "last_used": epoch
# }


def family_id(repo: str, quant: str) -> str:
    """Filesystem-safe id for a (repo, quant) family."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", f"{repo}__{quant}")


def family_dir(fam_id: str) -> str:
    return os.path.join(STAGING_PATH, fam_id)


def _meta_path(fam_id: str) -> str:
    return os.path.join(family_dir(fam_id), "meta.json")


def _read_meta(fam_id: str) -> dict | None:
    try:
        with open(_meta_path(fam_id), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Error reading staging meta {fam_id}: {e}")
        return None
    return data if isinstance(data, dict) else None


def _write_meta(fam_id: str, meta: dict) -> None:
    # write-temp-then-rename so a crash mid-write can't corrupt the meta
    os.makedirs(family_dir(fam_id), exist_ok=True)
    tmp_file = f"{_meta_path(fam_id)}.tmp"
    with open(tmp_file, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    os.replace(tmp_file, _meta_path(fam_id))


def get_meta(fam_id: str) -> dict | None:
    """Return a family's meta, or None if not staged."""
    with _lock:
        return _read_meta(fam_id)


def save_meta(fam_id: str, meta: dict) -> None:
    """Persist a family's meta."""
    with _lock:
        _write_meta(fam_id, meta)


def touch(fam_id: str) -> None:
    """Mark a family as recently used (LRU bookkeeping)."""
    with _lock:
        meta = _read_meta(fam_id)
        if meta:
            meta["last_used"] = time.time()
            _write_meta(fam_id, meta)


def disk_size(fam_id: str) -> int:
    """Bytes currently on disk for a family (shards + partials)."""
    total = 0
    directory = family_dir(fam_id)
    try:
        for name in os.listdir(directory):
            total += os.path.getsize(os.path.join(directory, name))
    except OSError:
        pass
    return total


def list_staged() -> list[dict]:
    """All staged families with their meta and current disk usage."""
    families = []
    try:
        fam_ids = sorted(os.listdir(STAGING_PATH))
    except FileNotFoundError:
        return []
    for fam_id in fam_ids:
        meta = get_meta(fam_id)
        if meta is None:
            continue
        families.append({**meta, "id": fam_id, "disk_size": disk_size(fam_id)})
    return families


def prune(fam_id: str) -> bool:
    """Delete a staged family's files. Returns True if it existed.

    Pruning only clears the local download cache — models already created on an
    Ollama endpoint keep their blobs in Ollama's own store.
    """
    directory = family_dir(fam_id)
    if not os.path.isdir(directory):
        return False
    shutil.rmtree(directory, ignore_errors=True)
    logger.info(f"Pruned staged family {fam_id}")
    return True


def auto_prune(protect: str | None = None, incoming_bytes: int = 0) -> None:
    """Evict LRU complete families until the staging cap fits incoming_bytes.

    Args:
        protect: Family id never to evict (the one being staged/deployed).
        incoming_bytes: Additional bytes about to be written.
    """
    cap = env.hf_staging_max_gb * 1024**3
    if cap <= 0:
        return
    families = list_staged()
    used = sum(fam["disk_size"] for fam in families)
    if used + incoming_bytes <= cap:
        return

    evictable = sorted(
        (fam for fam in families if fam["id"] != protect and fam.get("status") != "downloading"),
        key=lambda fam: fam.get("last_used") or fam.get("created") or 0,
    )
    for fam in evictable:
        if used + incoming_bytes <= cap:
            break
        prune(fam["id"])
        used -= fam["disk_size"]
        logger.info(f"Auto-pruned {fam['id']} ({fam['disk_size'] / 1e9:.1f} GB) to fit staging cap")
