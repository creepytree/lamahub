"""Ollama API service for model management."""

import asyncio
from typing import Any

import httpx

from lamahub.env import env
from lamahub.extensions import logger
from lamahub.services import fixed_store


def normalize_model_name(model_name: str) -> str:
    """Append the implicit :latest tag ollama uses when none is given."""
    model_name = model_name.strip()
    if model_name and ":" not in model_name:
        return f"{model_name}:latest"
    return model_name


class OllamaService:
    """Service for interacting with the Ollama API.

    Provides methods for listing, pulling, deleting, and inspecting models.
    """

    def __init__(self):
        """Initialize the stateless service.

        The target Ollama URL is supplied per call so a single instance can
        serve requests against any configured endpoint.
        """
        self.timeout = 300.0
        self.fixed_models = env.fixed_models
        self.fixed_model_ctx = env.fixed_model_ctx

    async def list_models(self, base_url: str) -> dict[str, Any]:
        """List all available models.

        Args:
            base_url: Base URL of the target Ollama endpoint.

        Returns:
            Dictionary containing models list or error.
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{base_url}/api/tags")
                response.raise_for_status()
                return response.json()
        except Exception as e:
            logger.error(f"Error listing models: {e}")
            return {"models": [], "error": str(e)}

    async def ensure_fixed_models(self, base_url: str) -> None:
        """Pull configured fixed models when they are not installed.

        Args:
            base_url: Base URL of the endpoint to manage fixed models on.
        """
        if not self.fixed_models:
            return

        installed_response = await self.list_models(base_url)
        if installed_response.get("error"):
            logger.error(f"Skipping fixed model checks: {installed_response['error']}")
            return

        installed_models = {
            self._normalize_model_name(model.get("name", ""))
            for model in installed_response.get("models", [])
            if model.get("name")
        }

        for model_name in self.fixed_models:
            normalized_model_name = self._normalize_model_name(model_name)
            if normalized_model_name in installed_models:
                logger.debug(f"Fixed model already installed: {model_name}")
                continue

            logger.info(f"Pulling missing fixed model: {model_name}")
            async for line in self.pull_model_stream(base_url, model_name):
                logger.debug(f"Fixed model pull progress for {model_name}: {line}")
            installed_models.add(normalized_model_name)

    async def get_model_max_ctx(self, base_url: str, model_name: str) -> int | None:
        """Return a model's native maximum context length, or None if unknown.

        Read from /api/show model_info (key "<arch>.context_length"). Ollama
        clamps any requested/baked num_ctx to this ceiling, so a pin above it is
        silently reduced.
        """
        info = await self.show_model_info(base_url, model_name)
        model_info = info.get("model_info") or {}
        for key, value in model_info.items():
            if key.endswith(".context_length"):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
        return None

    async def get_baked_ctx(self, base_url: str, model_name: str) -> int | None:
        """Return the num_ctx currently baked into the model's Modelfile, if any.

        Parses the /api/show "parameters" block (a text listing of PARAMETER
        lines). None means the model carries no explicit num_ctx.
        """
        info = await self.show_model_info(base_url, model_name)
        for line in (info.get("parameters") or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "num_ctx":
                try:
                    return int(parts[1])
                except ValueError:
                    return None
        return None

    async def probe_effective_ctx(self, base_url: str, model_name: str) -> int | None:
        """Load the model with no options and read back the context it loaded at.

        Captures the effective default (min of the server's OLLAMA_CONTEXT_LENGTH
        and the model's native max) that a plain, ctx-omitting request resolves
        to. Must be called before baking, while the model is still pristine.
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                await client.post(
                    f"{base_url}/api/generate",
                    json={"model": model_name, "prompt": "", "stream": False, "keep_alive": "30s"},
                )
            running = await self.get_running_models(base_url)
            normalized = self._normalize_model_name(model_name)
            for model in running.get("models", []):
                if self._normalize_model_name(model.get("name") or model.get("model", "")) == normalized:
                    return model.get("context_length")
        except Exception as e:
            logger.error(f"Error probing effective context for {model_name}: {e}")
        return None

    async def bake_ctx(self, base_url: str, model_name: str, num_ctx: int) -> bool:
        """Bake num_ctx into the model's Modelfile via a self-referential create.

        Rewrites the same tag's manifest (FROM itself + PARAMETER num_ctx),
        reusing the content-addressed weight blobs, so it is cheap and keeps the
        model name unchanged. This makes num_ctx the model's default, honored by
        every client that does not send an explicit num_ctx, overriding the
        server's OLLAMA_CONTEXT_LENGTH. Purely local — no registry access.
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{base_url}/api/create",
                    json={
                        "model": model_name,
                        "from": model_name,
                        "parameters": {"num_ctx": num_ctx},
                        "stream": False,
                    },
                )
                response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error baking num_ctx={num_ctx} into {model_name}: {e}")
            return False

    async def ensure_baked_ctx(self, base_url: str, model_name: str, num_ctx: int) -> int | None:
        """Idempotently bake a pin's context, capturing the baseline first.

        Clamps num_ctx to the model's native max, and only (re)bakes when the
        currently baked value differs — so repeated reconciles are no-ops. On the
        first bake it records the pre-pin baseline (existing param, or the probed
        effective default when there was none) so unpinning can revert offline.

        Returns the effective (clamped) target context, or None on failure.
        """
        max_ctx = await self.get_model_max_ctx(base_url, model_name)
        target = min(num_ctx, max_ctx) if max_ctx else num_ctx
        if max_ctx and num_ctx > max_ctx:
            logger.warning(
                f"Pin num_ctx={num_ctx} exceeds {model_name} max {max_ctx}; using {max_ctx}"
            )

        current = await self.get_baked_ctx(base_url, model_name)
        if current == target:
            return target

        if fixed_store.get_baseline(model_name) is None:
            default_ctx = await self.probe_effective_ctx(base_url, model_name) if current is None else None
            fixed_store.set_baseline(model_name, num_ctx=current, default_ctx=default_ctx)

        if await self.bake_ctx(base_url, model_name, target):
            logger.info(f"Baked num_ctx={target} into pinned model {model_name}")
            return target
        return None

    async def restore_ctx(self, base_url: str, model_name: str) -> None:
        """Revert a baked pin to its recorded baseline, then forget the baseline.

        Offline-safe: re-bakes the baseline num_ctx (the model's pre-pin explicit
        value, else the probed default, else its native max) via a local create.
        Ollama cannot truly unset a parameter, so this restores the value rather
        than removing the line; behavior matches the pristine model.
        """
        baseline = fixed_store.get_baseline(model_name)
        if baseline is None:
            return
        target = baseline.get("num_ctx") or baseline.get("default_ctx")
        if target is None:
            target = await self.get_model_max_ctx(base_url, model_name)
        if target and await self.bake_ctx(base_url, model_name, target):
            logger.info(f"Restored {model_name} context baseline to num_ctx={target}")
        fixed_store.remove_baseline(model_name)

    async def maintain_fixed_models(self, base_url: str, interval: float = 30.0) -> None:
        """Pull fixed models and keep the @ctx pins baked and resident.

        Pulls anything missing once, then reconciles on each interval so UI pin
        edits and any external drift (a re-pull that wiped the baked param)
        self-heal without a restart.

        Args:
            base_url: Base URL of the endpoint to manage fixed models on.
            interval: Seconds between reconcile passes.
        """
        await self.ensure_fixed_models(base_url)

        while True:
            try:
                await self.reconcile_fixed_ctx(base_url)
            except Exception as e:
                logger.error(f"Error reconciling fixed models: {e}")
            await asyncio.sleep(interval)

    async def reconcile_fixed_ctx(self, base_url: str) -> None:
        """Make baked contexts and residency match the current set of @ctx pins.

        Reverts models we baked that are no longer pinned (GC), bakes the pinned
        ones to their (clamped) context, and keeps each pinned model resident so
        a chat that displaced it gets warm-loaded back — now at the baked default.
        """
        ctx_map = self.effective_fixed_ctx()
        pinned = {self._normalize_model_name(name) for name in ctx_map}

        # GC: anything we previously baked but is no longer pinned reverts.
        for name in fixed_store.baseline_names():
            if self._normalize_model_name(name) not in pinned:
                await self.restore_ctx(base_url, name)

        if not ctx_map:
            return

        running = await self.get_running_models(base_url)
        loaded = {
            self._normalize_model_name(model.get("name") or model.get("model", ""))
            for model in running.get("models", [])
        }
        for model_name, num_ctx in ctx_map.items():
            await self.ensure_baked_ctx(base_url, model_name, num_ctx)
            if self._normalize_model_name(model_name) not in loaded:
                await self.load_model(base_url, model_name)

    def _env_fixed_names(self) -> set[str]:
        return {self._normalize_model_name(name) for name in self.fixed_models}

    def is_env_fixed_model(self, model_name: str) -> bool:
        """True if the model is pinned via FIXED_MODELS (protected, UI-read-only)."""
        return self._normalize_model_name(model_name) in self._env_fixed_names()

    def effective_fixed_models(self) -> list[dict[str, Any]]:
        """Merge the env baseline and the UI pins into one list.

        Each entry is {name, num_ctx, source}. env entries win on a name
        collision so an env pin can never be shadowed or removed from the UI.
        """
        env_names = self._env_fixed_names()
        merged = [
            {"name": name, "num_ctx": self.fixed_model_ctx.get(name), "source": "env"}
            for name in self.fixed_models
        ]
        for name, meta in fixed_store.load_pins().items():
            if self._normalize_model_name(name) in env_names:
                continue
            merged.append({"name": name, "num_ctx": (meta or {}).get("num_ctx"), "source": "user"})
        return merged

    def effective_fixed_ctx(self) -> dict[str, int]:
        """name -> num_ctx for every pin (env or UI) that set a context length."""
        return {entry["name"]: entry["num_ctx"] for entry in self.effective_fixed_models() if entry["num_ctx"]}

    def is_fixed_model(self, model_name: str) -> bool:
        """True if the model is pinned via env or the UI (protected from deletion)."""
        normalized_name = self._normalize_model_name(model_name)
        return any(self._normalize_model_name(entry["name"]) == normalized_name for entry in self.effective_fixed_models())

    @staticmethod
    def _normalize_model_name(model_name: str) -> str:
        return normalize_model_name(model_name)

    async def get_running_models(self, base_url: str) -> dict[str, Any]:
        """Get currently running models.

        Args:
            base_url: Base URL of the target Ollama endpoint.

        Returns:
            Dictionary containing running models or error.
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{base_url}/api/ps")
                response.raise_for_status()
                return response.json()
        except Exception as e:
            logger.error(f"Error getting running models: {e}")
            return {"models": [], "error": str(e)}

    async def pull_model_stream(self, base_url: str, model_name: str):
        """Pull a model from Ollama library with streaming progress.

        Args:
            base_url: Base URL of the target Ollama endpoint.
            model_name: Name of the model to pull.

        Yields:
            JSON strings with progress updates.
        """
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST",
                    f"{base_url}/api/pull",
                    json={"name": model_name},
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line:
                            yield line
        except Exception as e:
            logger.error(f"Error pulling model {model_name}: {e}")
            yield f'{{"status": "error", "error": "{str(e)}"}}'

    async def delete_model(self, base_url: str, model_name: str) -> dict[str, Any]:
        """Delete a model.

        Args:
            base_url: Base URL of the target Ollama endpoint.
            model_name: Name of the model to delete.

        Returns:
            Dictionary with status and message.
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(
                    "DELETE",
                    f"{base_url}/api/delete",
                    json={"name": model_name},
                )
                response.raise_for_status()
                return {"status": "success", "message": f"Model {model_name} deleted"}
        except Exception as e:
            logger.error(f"Error deleting model {model_name}: {e}")
            return {"status": "error", "message": str(e)}

    async def unload_model(self, base_url: str, model_name: str) -> dict[str, Any]:
        """Unload a running model from memory.

        Args:
            base_url: Base URL of the target Ollama endpoint.
            model_name: Name of the model to unload.

        Returns:
            Dictionary with status and message.
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{base_url}/api/generate",
                    json={
                        "model": model_name,
                        "stream": False,
                        "keep_alive": 0,
                    },
                )
                response.raise_for_status()
                return {"status": "success", "message": f"Model {model_name} unloaded"}
        except Exception as e:
            logger.error(f"Error unloading model {model_name}: {e}")
            return {"status": "error", "message": str(e)}

    async def load_model(self, base_url: str, model_name: str) -> dict[str, Any]:
        """Load a model into memory.

        Args:
            base_url: Base URL of the target Ollama endpoint.
            model_name: Name of the model to load.

        Returns:
            Dictionary with status and message.
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{base_url}/api/generate",
                    json={
                        "model": model_name,
                        "stream": False,
                        "keep_alive": -1,
                    },
                )
                response.raise_for_status()
                return {"status": "success", "message": f"Model {model_name} loaded"}
        except Exception as e:
            logger.error(f"Error loading model {model_name}: {e}")
            return {"status": "error", "message": str(e)}

    async def show_model_info(self, base_url: str, model_name: str) -> dict[str, Any]:
        """Get detailed information about a model.

        Args:
            base_url: Base URL of the target Ollama endpoint.
            model_name: Name of the model to inspect.

        Returns:
            Dictionary with model details or error.
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{base_url}/api/show",
                    json={"name": model_name},
                )
                response.raise_for_status()
                return response.json()
        except Exception as e:
            logger.error(f"Error getting model info for {model_name}: {e}")
            return {"error": str(e)}

    async def chat_stream(
        self,
        base_url: str,
        model_name: str,
        messages: list[dict[str, str]],
        options: dict[str, Any] | None = None,
        think: bool = False,
        tools: list[dict[str, Any]] | None = None,
    ):
        """Send a chat message to a model with streaming response.

        Args:
            base_url: Base URL of the target Ollama endpoint.
            model_name: Name of the model to chat with.
            messages: List of message objects with 'role' and 'content'.
            options: Optional model parameters (temperature, top_k, top_p, etc.)
            think: Enable thinking/reasoning mode for supported models.
            tools: Optional tool definitions for tool-capable models.

        Yields:
            JSON strings with response chunks.
        """
        try:
            payload = {
                "model": model_name,
                "messages": messages,
                "stream": True,
            }
            if think:
                payload["think"] = True
            if tools:
                payload["tools"] = tools
            if options:
                payload["options"] = options
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST",
                    f"{base_url}/api/chat",
                    json=payload,
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line:
                            yield line
        except Exception as e:
            logger.error(f"Error chatting with model {model_name}: {e}")
            yield f'{{"error": "{str(e)}"}}'

    async def generate_stream(
        self,
        base_url: str,
        model_name: str,
        prompt: str,
        options: dict[str, Any] | None = None,
    ):
        """Generate text from a model with streaming response.

        Args:
            base_url: Base URL of the target Ollama endpoint.
            model_name: Name of the model to use.
            prompt: The prompt to generate from.
            options: Optional model parameters (temperature, top_k, top_p, etc.)

        Yields:
            JSON strings with response chunks.
        """
        try:
            payload = {
                "model": model_name,
                "prompt": prompt,
                "stream": True,
            }
            if options:
                payload["options"] = options
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST",
                    f"{base_url}/api/generate",
                    json=payload,
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line:
                            yield line
        except Exception as e:
            logger.error(f"Error generating with model {model_name}: {e}")
            yield f'{{"error": "{str(e)}"}}'


ollama_service = OllamaService()
