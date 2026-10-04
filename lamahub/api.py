"""REST API endpoints for interacting with Ollama models."""

from typing import Any

from fastapi import APIRouter, Depends, Header
from fastapi.responses import StreamingResponse

from lamahub.env import env
from lamahub.extensions import logger
from lamahub.services.endpoints import Endpoint, registry
from lamahub.services.ollama import MODEL_KINDS, normalize_model_name, ollama_service
from lamahub.services import fixed_store, hf_deploy, hf_hub, staging_store

api = APIRouter(prefix="/api")


async def resolve_endpoint(x_ollama_url: str | None = Header(default=None)) -> Endpoint:
    """Resolve the target endpoint from the X-Ollama-Url header.

    The header is validated against the configured allowlist; unknown or
    missing values fall back to the default endpoint.
    """
    return registry.resolve(x_ollama_url)


def _sse(lines) -> StreamingResponse:
    """Relay an async iterator of JSON lines as a server-sent event stream."""

    async def events():
        async for line in lines:
            yield f"data: {line}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


# Fixed models are managed only on the default endpoint (see endpoints.py).
_NOT_DEFAULT = {"status": "error", "message": "Fixed models are managed on the default endpoint"}


@api.get("/logs")
async def get_logs(limit: int = 500):
    """Return recent application log entries for the Log tab."""
    limit = max(1, min(limit, 5000))
    return {"entries": logger.read_entries(limit)}


@api.get("/endpoints")
async def get_endpoints():
    """List configured Ollama endpoints and flag the default."""
    default_url = registry.default.url
    return {
        "endpoints": [
            {"name": endpoint.name, "url": endpoint.url, "default": endpoint.url == default_url}
            for endpoint in registry.endpoints
        ],
        "default": default_url,
    }


@api.get("/models")
async def get_models(endpoint: Endpoint = Depends(resolve_endpoint)):
    """Get list of all available models"""
    # Deliberately not logged: the dashboard polls this on a timer (~30s), so a
    # log line here just floods the log with noise that carries no diagnostic
    # value. Mutations (pull/delete/pin/deploy) below are logged instead.
    return await ollama_service.list_models(endpoint.url)


@api.get("/models/running")
async def get_running_models(endpoint: Endpoint = Depends(resolve_endpoint)):
    """Get currently running models"""
    # Deliberately not logged: polled every ~4s by the running-models widget.
    # See the note on get_models above.
    return await ollama_service.get_running_models(endpoint.url)


@api.get("/models/fixed")
async def get_fixed_models(endpoint: Endpoint = Depends(resolve_endpoint)):
    """Get fixed models (env baseline + UI pins) with their pinned context length.

    Fixed models are managed only on the default endpoint, so other endpoints
    report none. Each entry is {name, num_ctx, kind, source}.
    """
    if not registry.is_default(endpoint.url):
        return {"models": []}
    return {"models": ollama_service.effective_fixed_models()}


# Declared before the generic "/models/{model_name:path}" delete so a
# "/models/fixed/..." path is not swallowed by it.
@api.put("/models/fixed/{model_name:path}")
async def pin_fixed_model(model_name: str, data: dict[str, Any], endpoint: Endpoint = Depends(resolve_endpoint)):
    """Pin a model in the UI-managed layer, optionally at a fixed context length."""
    if not registry.is_default(endpoint.url):
        return _NOT_DEFAULT
    if ollama_service.is_env_fixed_model(model_name):
        return {"status": "error", "message": f"Model {model_name} is set via FIXED_MODELS and cannot be edited"}

    num_ctx = data.get("num_ctx")
    if num_ctx is not None:
        try:
            num_ctx = int(num_ctx)
        except (TypeError, ValueError):
            return {"status": "error", "message": "num_ctx must be an integer"}
        if num_ctx <= 0:
            return {"status": "error", "message": "num_ctx must be positive"}

    # kind picks the endpoint the residency keeper warm-loads through; null means
    # detect it from the model's capabilities on every pass.
    kind = data.get("kind") or None
    if kind is not None and kind not in MODEL_KINDS:
        return {"status": "error", "message": f"kind must be one of {', '.join(MODEL_KINDS)}"}

    normalized_name = normalize_model_name(model_name)
    fixed_store.set_pin(normalized_name, num_ctx, kind)
    logger.info(f"Pinned model {normalized_name} (num_ctx={num_ctx}, kind={kind})")

    # Bake the context immediately so clients get it without waiting for the
    # reconcile loop; surface any clamp to the model's native maximum.
    if num_ctx:
        effective = await ollama_service.ensure_baked_ctx(endpoint.url, normalized_name, num_ctx, kind)
        if effective is not None and effective != num_ctx:
            return {
                "status": "success",
                "message": f"num_ctx clamped to model maximum {effective}",
                "num_ctx": effective,
            }
    return {"status": "success"}


@api.delete("/models/fixed/{model_name:path}")
async def unpin_fixed_model(model_name: str, endpoint: Endpoint = Depends(resolve_endpoint)):
    """Remove a UI pin. env FIXED_MODELS entries cannot be removed here."""
    if not registry.is_default(endpoint.url):
        return _NOT_DEFAULT
    if ollama_service.is_env_fixed_model(model_name):
        return {"status": "error", "message": f"Model {model_name} is set via FIXED_MODELS and cannot be removed"}

    normalized_name = normalize_model_name(model_name)
    if fixed_store.remove_pin(normalized_name):
        # Revert the baked context to the pre-pin baseline (offline, no pull).
        await ollama_service.restore_ctx(endpoint.url, normalized_name)
        logger.info(f"Unpinned model {normalized_name}")
        return {"status": "success"}
    return {"status": "error", "message": f"Model {model_name} is not pinned"}


def _pull(verb: str, data: dict[str, Any], endpoint: Endpoint):
    """Stream an Ollama pull; shared by pull (new model) and update (re-pull)."""
    model_name = data.get("name")
    if not model_name:
        return {"status": "error", "message": "Model name is required"}
    logger.info(f"{verb} model: {model_name} on {endpoint.url}")
    return _sse(ollama_service.pull_model_stream(endpoint.url, model_name))


@api.post("/models/pull")
async def pull_model(data: dict[str, Any], endpoint: Endpoint = Depends(resolve_endpoint)):
    """Pull a new model with streaming progress"""
    return _pull("Pulling", data, endpoint)


@api.post("/models/update")
async def update_model(data: dict[str, Any], endpoint: Endpoint = Depends(resolve_endpoint)):
    """Update a model with streaming progress"""
    return _pull("Updating", data, endpoint)


@api.delete("/models/{model_name:path}")
async def delete_model(model_name: str, endpoint: Endpoint = Depends(resolve_endpoint)):
    """Delete a model"""
    # FIXED_MODELS are only protected on the default endpoint, where they are
    # managed; elsewhere the same name is just a regular, deletable model.
    if registry.is_default(endpoint.url) and ollama_service.is_fixed_model(model_name):
        if ollama_service.is_env_fixed_model(model_name):
            return {"status": "error", "message": f"Model {model_name} is configured in FIXED_MODELS"}
        return {"status": "error", "message": f"Model {model_name} is pinned; unpin it before deleting"}

    logger.info(f"Deleting model: {model_name} on {endpoint.url}")
    return await ollama_service.delete_model(endpoint.url, model_name)


@api.post("/models/{model_name:path}/unload")
async def unload_model(model_name: str, endpoint: Endpoint = Depends(resolve_endpoint)):
    """Unload a running model from memory"""
    logger.info(f"Unloading model: {model_name} on {endpoint.url}")
    return await ollama_service.unload_model(endpoint.url, model_name)


@api.post("/models/{model_name:path}/load")
async def load_model(model_name: str, endpoint: Endpoint = Depends(resolve_endpoint)):
    """Load a model into memory"""
    logger.info(f"Loading model: {model_name} on {endpoint.url}")
    return await ollama_service.load_model(endpoint.url, model_name)


@api.get("/models/{model_name:path}/info")
async def get_model_info(model_name: str, endpoint: Endpoint = Depends(resolve_endpoint)):
    """Get detailed information about a model"""
    logger.debug(f"Fetching info for model: {model_name} on {endpoint.url}")
    return await ollama_service.show_model_info(endpoint.url, model_name)


@api.post("/chat")
async def chat(data: dict[str, Any], endpoint: Endpoint = Depends(resolve_endpoint)):
    """Chat with a model using streaming response"""
    model_name = data.get("model")
    messages = data.get("messages", [])
    options = data.get("options")
    think = data.get("think", False)
    tools = data.get("tools")
    if not model_name:
        return {"error": "Model name is required"}
    if not messages:
        return {"error": "Messages are required"}
    logger.info(f"Chat with model: {model_name}, options: {options}, think: {think}, tools: {bool(tools)}")
    return _sse(ollama_service.chat_stream(endpoint.url, model_name, messages, options, think, tools))


@api.post("/generate")
async def generate(data: dict[str, Any], endpoint: Endpoint = Depends(resolve_endpoint)):
    """Generate text from a model using streaming response"""
    model_name = data.get("model")
    prompt = data.get("prompt", "")
    options = data.get("options")
    if not model_name:
        return {"error": "Model name is required"}
    if not prompt:
        return {"error": "Prompt is required"}
    logger.info(f"Generate with model: {model_name}, options: {options}")
    return _sse(ollama_service.generate_stream(endpoint.url, model_name, prompt, options))


# ---------------------------------------------------------------------------
# HF sharded-GGUF deploy (see HF_DEPLOY_DESIGN.md)


@api.get("/hf/search")
async def hf_search(q: str = "", cursor: str = ""):
    """Search GGUF repos on HuggingFace (cursor-paginated)."""
    return await hf_hub.search_models(q, cursor)


@api.get("/hf/repo/{repo:path}/quants")
async def hf_repo_quants(repo: str):
    """List a repo's quant families with sizes and shard counts."""
    return {"quants": await hf_hub.repo_quants(repo)}


@api.post("/hf/deploy")
async def hf_deploy_model(data: dict[str, Any], endpoint: Endpoint = Depends(resolve_endpoint)):
    """Queue a deploy; it runs as a detached background task (observe via
    /hf/deploy/status), so a browser reload or tab close doesn't abort a long
    (multi-GB) download. Deploys run one at a time, the rest wait FIFO.
    """
    repo = data.get("repo")
    family = data.get("family")
    model_name = (data.get("model_name") or "").strip()
    if not repo or not family or not model_name:
        return {"status": "error", "message": "repo, family and model_name are required"}
    result = hf_deploy.deploys.enqueue(endpoint.url, repo, family, model_name)
    if result["status"] == "queued":
        logger.info(f"Queued HF {repo} [{family}] as {model_name} on {endpoint.url} (#{result['position']})")
    return {**result, "model_name": model_name}


@api.delete("/hf/deploy/queue/{job_id}")
async def hf_cancel_queued(job_id: int):
    """Remove a waiting deploy from the queue."""
    if hf_deploy.deploys.cancel(job_id):
        return {"status": "success"}
    return {"status": "error", "message": "Not queued (already running or finished)"}


@api.get("/hf/deploy/status")
async def hf_deploy_status():
    """Live deploy, waiting queue and recent results, for the client poll."""
    return hf_deploy.deploys.status()


@api.get("/hf/staging")
async def hf_staging():
    """List staged shard families in the local download cache."""
    return {"families": staging_store.list_staged(), "max_gb": env.hf_staging_max_gb}


@api.delete("/hf/staging/{family_id}")
async def hf_prune_staging(family_id: str):
    """Prune one staged family from the local cache (models on Ollama keep their blobs)."""
    if hf_deploy.deploys.active:
        return {"status": "error", "message": "Cannot prune while a deploy is running"}
    if staging_store.prune(family_id):
        return {"status": "success"}
    return {"status": "error", "message": "Not staged"}
