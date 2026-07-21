"""REST API endpoints for interacting with Ollama models."""

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Header
from fastapi.responses import StreamingResponse

from lamahub.env import env
from lamahub.extensions import logger
from lamahub.services.endpoints import Endpoint, registry
from lamahub.services.ollama import normalize_model_name, ollama_service
from lamahub.services import fixed_store, hf_deploy, staging_store

api = APIRouter(prefix="/api")


async def resolve_endpoint(x_ollama_url: str | None = Header(default=None)) -> Endpoint:
    """Resolve the target endpoint from the X-Ollama-Url header.

    The header is validated against the configured allowlist; unknown or
    missing values fall back to the default endpoint.
    """
    return registry.resolve(x_ollama_url)


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
    logger.debug(f"Fetching all models from {endpoint.url}")
    return await ollama_service.list_models(endpoint.url)


@api.get("/models/running")
async def get_running_models(endpoint: Endpoint = Depends(resolve_endpoint)):
    """Get currently running models"""
    logger.debug(f"Fetching running models from {endpoint.url}")
    return await ollama_service.get_running_models(endpoint.url)


@api.get("/models/fixed")
async def get_fixed_models(endpoint: Endpoint = Depends(resolve_endpoint)):
    """Get fixed models (env baseline + UI pins) with their pinned context length.

    Fixed models are managed only on the default endpoint, so other endpoints
    report none. Each entry is {name, num_ctx, source}.
    """
    if not registry.is_default(endpoint.url):
        return {"models": []}
    return {"models": ollama_service.effective_fixed_models()}


# Declared before the generic "/models/{model_name:path}" delete so a
# "/models/fixed/..." path is not swallowed by it.
@api.put("/models/fixed/{model_name:path}")
async def pin_fixed_model(
    model_name: str, data: dict[str, Any], endpoint: Endpoint = Depends(resolve_endpoint)
):
    """Pin a model in the UI-managed layer, optionally at a fixed context length."""
    if not registry.is_default(endpoint.url):
        return {"status": "error", "message": "Fixed models are managed on the default endpoint"}
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

    normalized_name = normalize_model_name(model_name)
    fixed_store.set_pin(normalized_name, num_ctx)
    logger.info(f"Pinned model {normalized_name} (num_ctx={num_ctx})")

    # Bake the context immediately so clients get it without waiting for the
    # reconcile loop; surface any clamp to the model's native maximum.
    if num_ctx:
        effective = await ollama_service.ensure_baked_ctx(endpoint.url, normalized_name, num_ctx)
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
        return {"status": "error", "message": "Fixed models are managed on the default endpoint"}
    if ollama_service.is_env_fixed_model(model_name):
        return {"status": "error", "message": f"Model {model_name} is set via FIXED_MODELS and cannot be removed"}

    normalized_name = normalize_model_name(model_name)
    if fixed_store.remove_pin(normalized_name):
        # Revert the baked context to the pre-pin baseline (offline, no pull).
        await ollama_service.restore_ctx(endpoint.url, normalized_name)
        logger.info(f"Unpinned model {normalized_name}")
        return {"status": "success"}
    return {"status": "error", "message": f"Model {model_name} is not pinned"}


@api.post("/models/pull")
async def pull_model(data: dict[str, Any], endpoint: Endpoint = Depends(resolve_endpoint)):
    """Pull a new model with streaming progress"""
    model_name = data.get("name")
    if not model_name:
        return {"status": "error", "message": "Model name is required"}
    logger.info(f"Pulling model: {model_name} on {endpoint.url}")

    async def stream_progress():
        async for line in ollama_service.pull_model_stream(endpoint.url, model_name):
            yield f"data: {line}\n\n"

    return StreamingResponse(stream_progress(), media_type="text/event-stream")


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

    async def stream_response():
        async for line in ollama_service.chat_stream(endpoint.url, model_name, messages, options, think, tools):
            yield f"data: {line}\n\n"

    return StreamingResponse(stream_response(), media_type="text/event-stream")


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

    async def stream_response():
        async for line in ollama_service.generate_stream(endpoint.url, model_name, prompt, options):
            yield f"data: {line}\n\n"

    return StreamingResponse(stream_response(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# HF sharded-GGUF deploy (see HF_DEPLOY_DESIGN.md)


@api.get("/hf/search")
async def hf_search(q: str = "", cursor: str = ""):
    """Search GGUF repos on HuggingFace (cursor-paginated)."""
    return await hf_deploy.search_models(q, cursor)


@api.get("/hf/repo/{repo:path}/quants")
async def hf_repo_quants(repo: str):
    """List a repo's quant families with sizes and shard counts."""
    return {"quants": await hf_deploy.repo_quants(repo)}


@api.post("/hf/deploy")
async def hf_deploy_model(data: dict[str, Any], endpoint: Endpoint = Depends(resolve_endpoint)):
    """Start a deploy as a detached background task; observe via /hf/deploy/status.

    Decoupled from this request so a browser reload or tab close doesn't abort a
    long (multi-GB) download. Serialized: only one deploy runs at a time.
    """
    repo = data.get("repo")
    family = data.get("family")
    model_name = (data.get("model_name") or "").strip()
    if not repo or not family or not model_name:
        return {"status": "error", "message": "repo, family and model_name are required"}
    if hf_deploy.deploy_active:
        return {"status": "error", "message": "Another deploy is already running"}
    # set the guard synchronously (before any await) so a concurrent start can't race in
    hf_deploy.deploy_active = True
    logger.info(f"Deploying HF {repo} [{family}] as {model_name} on {endpoint.url}")
    asyncio.create_task(hf_deploy.run_deploy(endpoint.url, repo, family, model_name))
    return {"status": "started", "model_name": model_name}


@api.get("/hf/deploy/status")
async def hf_deploy_status():
    """Live progress of the in-flight (or last) deploy, for the client poll."""
    return hf_deploy.current_deploy or {"active": False}


@api.get("/hf/staging")
async def hf_staging():
    """List staged shard families in the local download cache."""
    return {"families": staging_store.list_staged(), "max_gb": env.hf_staging_max_gb}


@api.delete("/hf/staging/{family_id}")
async def hf_prune_staging(family_id: str):
    """Prune one staged family from the local cache (models on Ollama keep their blobs)."""
    if hf_deploy.deploy_active:
        return {"status": "error", "message": "Cannot prune while a deploy is running"}
    if staging_store.prune(family_id):
        return {"status": "success"}
    return {"status": "error", "message": "Not staged"}


@api.post("/models/update")
async def update_model(data: dict[str, Any], endpoint: Endpoint = Depends(resolve_endpoint)):
    """Update a model with streaming progress"""
    model_name = data.get("name")
    if not model_name:
        return {"status": "error", "message": "Model name is required"}
    logger.info(f"Updating model: {model_name} on {endpoint.url}")

    async def stream_progress():
        async for line in ollama_service.pull_model_stream(endpoint.url, model_name):
            yield f"data: {line}\n\n"

    return StreamingResponse(stream_progress(), media_type="text/event-stream")
