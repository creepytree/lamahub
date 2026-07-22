"""Environment parsing and application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _get_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _get_bool(name: str, default: bool = False) -> bool:
    raw_value = _get_str(name).lower()
    if not raw_value:
        return default

    return raw_value in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    raw_value = _get_str(name)
    if not raw_value:
        return default

    try:
        return int(raw_value)
    except ValueError:
        return default


def _get_list(name: str, default: list[str] | None = None) -> list[str]:
    raw_value = _get_str(name)
    if not raw_value:
        return list(default or [])

    return [value.strip() for value in raw_value.split(",") if value.strip()]


def _get_fixed_models(name: str) -> tuple[list[str], dict[str, int]]:
    """Parse FIXED_MODELS entries, each optionally suffixed with @<num_ctx>.

    The @ suffix is used (not :) because : already separates the ollama tag,
    e.g. "glm4:9b@96000". Returns the bare model names plus a name->num_ctx map
    for the entries that pinned a context length.
    """
    models: list[str] = []
    ctx_by_model: dict[str, int] = {}
    for entry in _get_list(name):
        model_name, _, ctx_raw = entry.partition("@")
        model_name = model_name.strip()
        if not model_name:
            continue
        models.append(model_name)
        ctx_raw = ctx_raw.strip()
        if ctx_raw:
            try:
                ctx_by_model[model_name] = int(ctx_raw)
            except ValueError:
                # malformed ctx: still manage the model, just skip the pin
                pass
    return models, ctx_by_model


def _get_base_path() -> str:
    base_path = _get_str("BASE_PATH")

    if base_path:
        if not base_path.startswith("/"):
            base_path = "/" + base_path
        base_path = base_path.rstrip("/")

    return base_path


@dataclass(frozen=True)
class EnvConfig:
    base_path: str
    fixed_models: list[str]
    fixed_model_ctx: dict[str, int]  # name -> pinned num_ctx for warm-load
    instance_dir: str
    log_level: str
    ollama_url: str
    login_enabled: bool
    login_user: str
    login_password: str
    login_timeout: int  # session lifetime in minutes
    hf_token: str  # optional HuggingFace token: raises API limits, unlocks gated repos
    hf_staging_max_gb: int  # hard cap on the HF shard staging cache (0 = unlimited)


def load_env() -> EnvConfig:
    fixed_models, fixed_model_ctx = _get_fixed_models("FIXED_MODELS")
    return EnvConfig(
        base_path=_get_base_path(),
        fixed_models=fixed_models,
        fixed_model_ctx=fixed_model_ctx,
        instance_dir=_get_str("INSTANCE_DIR"),
        # Default to INFO: DEBUG is opt-in for troubleshooting. At DEBUG the
        # per-request debug lines (model info fetches, reconcile detail, etc.)
        # make the Log tab hard to read; set LOG_LEVEL=DEBUG to get them back.
        log_level=_get_str("LOG_LEVEL", "INFO").upper(),
        ollama_url=_get_str("OLLAMA_URL", "http://localhost:11434"),
        login_enabled=_get_bool("LOGIN"),
        login_user=_get_str("LOGIN_USER"),
        login_password=_get_str("LOGIN_PW"),
        login_timeout=_get_int("LOGIN_TIMEOUT", 60),
        hf_token=_get_str("HF_TOKEN"),
        hf_staging_max_gb=_get_int("HF_STAGING_MAX_GB", 100),
    )


env = load_env()
