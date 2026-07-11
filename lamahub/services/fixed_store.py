"""Persistent, UI-managed layer of pinned (fixed) models.

Stores user pins in instance/fixed_models.json as {name: {"num_ctx": int|None}}.
Layers on top of the env FIXED_MODELS baseline: env pins are protected and
cannot be edited here, user pins live only in this file and survive restarts.

Also persists the pre-pin context baseline in instance/ctx_baseline.json so a
pin's baked num_ctx can be reverted offline (no registry pull) when unpinned.
See ctx_pin logic in OllamaService.
"""

import json
import os
import threading

from lamahub.config import INSTANCE_PATH
from lamahub.extensions import logger

_STORE_FILE = os.path.join(INSTANCE_PATH, "fixed_models.json")
_BASELINE_FILE = os.path.join(INSTANCE_PATH, "ctx_baseline.json")
_lock = threading.Lock()


def _read(path: str, label: str) -> dict[str, dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Error reading {label}: {e}")
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: str, data: dict[str, dict]) -> None:
    # write-temp-then-rename so a crash mid-write can't corrupt the store
    os.makedirs(INSTANCE_PATH, exist_ok=True)
    tmp_file = f"{path}.tmp"
    with open(tmp_file, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp_file, path)


def load_pins() -> dict[str, dict]:
    """Return the user pin map {name: {"num_ctx": int|None}}."""
    with _lock:
        return _read(_STORE_FILE, "fixed-model store")


def set_pin(model_name: str, num_ctx: int | None) -> None:
    """Add or update a user pin."""
    with _lock:
        pins = _read(_STORE_FILE, "fixed-model store")
        pins[model_name] = {"num_ctx": num_ctx}
        _write(_STORE_FILE, pins)


def remove_pin(model_name: str) -> bool:
    """Remove a user pin. Returns True if it existed."""
    with _lock:
        pins = _read(_STORE_FILE, "fixed-model store")
        if model_name not in pins:
            return False
        del pins[model_name]
        _write(_STORE_FILE, pins)
        return True


# ---------------------------------------------------------------------------
# Context baseline: the model's pre-pin context state, captured once before the
# first bake so unpinning can revert the baked num_ctx offline.
# Entry shape: {name: {"num_ctx": int|None, "default_ctx": int|None}}
#   num_ctx      - explicit Modelfile num_ctx the model had before we touched it
#                  (None if it had none; then default_ctx is the fallback).
#   default_ctx  - probed effective load context when num_ctx was None, i.e. what
#                  a no-options request loaded at (min of server env and model max).


def get_baseline(model_name: str) -> dict | None:
    """Return the stored context baseline for a model, or None if unrecorded."""
    with _lock:
        return _read(_BASELINE_FILE, "ctx baseline store").get(model_name)


def set_baseline(model_name: str, num_ctx: int | None, default_ctx: int | None) -> None:
    """Record a model's pre-pin context baseline (only if not already recorded)."""
    with _lock:
        baselines = _read(_BASELINE_FILE, "ctx baseline store")
        baselines[model_name] = {"num_ctx": num_ctx, "default_ctx": default_ctx}
        _write(_BASELINE_FILE, baselines)


def remove_baseline(model_name: str) -> None:
    """Forget a model's context baseline (after it has been reverted)."""
    with _lock:
        baselines = _read(_BASELINE_FILE, "ctx baseline store")
        if model_name in baselines:
            del baselines[model_name]
            _write(_BASELINE_FILE, baselines)


def baseline_names() -> list[str]:
    """Names of every model with a recorded context baseline (i.e. baked by us)."""
    with _lock:
        return list(_read(_BASELINE_FILE, "ctx baseline store").keys())
