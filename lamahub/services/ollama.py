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


# How a model is warm-loaded. Ollama routes loading by capability: an embedding
# model rejects /api/generate with 400 "does not support generate" and has to be
# loaded through /api/embed instead.
#
# Only these two of Ollama's endpoints load a model, so "kind" is deliberately
# about the ENDPOINT, not about what the model is for: vision/OCR, tool-calling
# and thinking models are all loaded exactly like chat. Reranking has no
# endpoint at all yet (ollama/ollama#7219 is unmerged) -- when it lands, adding
# it is one row in each table below. What a model can actually *do* is reported
# separately by /api/show capabilities, which the UI shows as badges.
KIND_CHAT = "chat"
KIND_EMBED = "embed"

# kind -> (endpoint, body that loads it; "empty request" per the ollama docs)
_LOAD_ENDPOINTS: dict[str, tuple[str, dict[str, Any]]] = {
    KIND_CHAT: ("/api/generate", {"prompt": "", "stream": False}),
    KIND_EMBED: ("/api/embed", {"input": ""}),
}

# /api/show capability -> kind, first match wins; anything else loads as chat
_CAPABILITY_KINDS = (("embedding", KIND_EMBED),)

MODEL_KINDS = tuple(_LOAD_ENDPOINTS)


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

    async def detect_model_kind(self, base_url: str, model_name: str) -> str:
        """Infer a model's load kind from its /api/show capabilities."""
        info = await self.show_model_info(base_url, model_name)
        capabilities = info.get("capabilities") or []
        for capability, kind in _CAPABILITY_KINDS:
            if capability in capabilities:
                return kind
        return KIND_CHAT

    async def resolve_model_kind(self, base_url: str, model_name: str, kind: str | None) -> str:
        """Use the stored kind when set, otherwise detect it from capabilities."""
        if kind in MODEL_KINDS:
            return kind
        return await self.detect_model_kind(base_url, model_name)

    @staticmethod
    def _load_request(kind: str, model_name: str, keep_alive: Any) -> tuple[str, dict[str, Any]]:
        """Endpoint path and body that load a model of this kind at keep_alive.

        Both are the documented "empty request loads the model" form; keep_alive
        -1 pins it, 0 unloads it. Embedding models only answer on /api/embed.
        """
        path, body = _LOAD_ENDPOINTS.get(kind) or _LOAD_ENDPOINTS[KIND_CHAT]
        return path, {"model": model_name, "keep_alive": keep_alive, **body}

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

    async def probe_effective_ctx(self, base_url: str, model_name: str, kind: str | None = None) -> int | None:
        """Load the model with no options and read back the context it loaded at.

        Captures the effective default (min of the server's OLLAMA_CONTEXT_LENGTH
        and the model's native max) that a plain, ctx-omitting request resolves
        to. Must be called before baking, while the model is still pristine.

        WHY THIS EXISTS (and why we can't skip it and "just bake what we want"):
        Baking sets the model's default num_ctx via `create from=self`. When the
        user later UNPINS, we want to put the model back exactly how it was —
        offline, without a registry pull. That is easy if the model already had
        an explicit `num_ctx` PARAMETER (we saved it, we re-bake it). But if the
        model had NO explicit num_ctx it relied on the server default
        (OLLAMA_CONTEXT_LENGTH, clamped to native max), and Ollama's
        `create from=self` cannot *unset* an inherited parameter — you can only
        set a value. So to make unpin faithful we must record the NUMBER that the
        server default resolved to, and the only reliable way to read it is to
        load the model once and inspect /api/ps `context_length`. Hence the
        one-shot load with keep_alive:30s. This runs exactly once per model (see
        ensure_baked_ctx: guarded by `baseline is None`), not on every reconcile.
        """
        try:
            kind = await self.resolve_model_kind(base_url, model_name, kind)
            path, payload = self._load_request(kind, model_name, "30s")
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                await client.post(f"{base_url}{path}", json=payload)
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

        WHY BAKE INSTEAD OF JUST WARM-LOADING WITH THE DESIRED num_ctx:
        A warm-load (`keep_alive:-1` + options.num_ctx) pins only the ONE runner
        instance we loaded. Ollama keys a runner by model+effective options, so
        the moment ANY other client hits the model with a different or ABSENT
        num_ctx (e.g. every /v1 OpenAI-compatible client — they can't send it —
        or any external tool that omits it), Ollama spins a separate runner at
        that size and evicts ours. So a warm-load-only pin is defeated by the
        first ctx-omitting request from anything we don't control. Baking makes
        num_ctx the model's *default*, so those ctx-omitting requests resolve to
        the pinned value instead of evicting it — the pin becomes universal
        (native + /v1) and durable across restarts. The keep_alive:-1 warm-load
        still runs, but only for RESIDENCY (see reconcile_fixed_ctx), layered on
        top of the bake — it does NOT carry num_ctx. The one case baking cannot
        cover is a client sending an EXPLICIT, different num_ctx (precedence:
        explicit > Modelfile > env); only a rewriting proxy could, which we
        deliberately did not build (keeps lamahub a control-plane, not a
        data-plane).
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

    async def ensure_baked_ctx(
        self, base_url: str, model_name: str, num_ctx: int, kind: str | None = None
    ) -> int | None:
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
            default_ctx = await self.probe_effective_ctx(base_url, model_name, kind) if current is None else None
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

        RESIDENCY KEEPER: the load_model call below is what actually keeps a
        pinned model in memory. It is NOT the pin/bake step and NOT the browser —
        it is this server-side loop (started as maintain_fixed_models in app.py,
        every ~30s). load_model uses keep_alive:-1, so once loaded the model
        never idle-evicts; if something unloads it (manual unload, or a request
        with a different num_ctx spinning a separate runner), the next pass
        reloads it at the baked ctx. That is why a pinned model "comes back" a
        few seconds after you unload it.

        CAVEAT — residency is checked by NAME, not by context. If a chat loads
        the model at a *different* num_ctx, /api/ps still lists the name, so this
        loop considers it resident and won't correct the ctx. Preventing that is
        the chat client's job: it omits num_ctx for pinned models (see
        getPromptOptions in static/js/chat.js) so it never displaces this runner.
        """
        ctx_map = self.effective_fixed_ctx()
        kind_map = self.effective_fixed_kinds()
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
            kind = kind_map.get(model_name)
            await self.ensure_baked_ctx(base_url, model_name, num_ctx, kind)
            if self._normalize_model_name(model_name) not in loaded:
                # Keeper reload: model was evicted (idle, manual, or displaced by
                # a differing-ctx request) — warm it back at the baked default.
                # Logged at INFO because it is a real state change and low
                # frequency; it also makes the otherwise-silent keeper provable.
                logger.info(f"Keeping fixed model resident: reloading {model_name} (baked num_ctx={num_ctx})")
                await self.load_model(base_url, model_name, kind)

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
            {"name": name, "num_ctx": self.fixed_model_ctx.get(name), "kind": None, "source": "env"}
            for name in self.fixed_models
        ]
        for name, meta in fixed_store.load_pins().items():
            if self._normalize_model_name(name) in env_names:
                continue
            meta = meta or {}
            merged.append(
                {
                    "name": name,
                    "num_ctx": meta.get("num_ctx"),
                    "kind": meta.get("kind"),
                    "source": "user",
                }
            )
        return merged

    def effective_fixed_ctx(self) -> dict[str, int]:
        """name -> num_ctx for every pin (env or UI) that set a context length."""
        return {entry["name"]: entry["num_ctx"] for entry in self.effective_fixed_models() if entry["num_ctx"]}

    def effective_fixed_kinds(self) -> dict[str, str | None]:
        """name -> stored load kind for every pin; None means detect it."""
        return {entry["name"]: entry.get("kind") for entry in self.effective_fixed_models()}

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

    async def unload_model(self, base_url: str, model_name: str, kind: str | None = None) -> dict[str, Any]:
        """Unload a running model from memory.

        Args:
            base_url: Base URL of the target Ollama endpoint.
            model_name: Name of the model to unload.
            kind: Load kind ("chat"/"embed"); detected from capabilities if None.

        Returns:
            Dictionary with status and message.
        """
        try:
            kind = await self.resolve_model_kind(base_url, model_name, kind)
            path, payload = self._load_request(kind, model_name, 0)
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{base_url}{path}", json=payload)
                response.raise_for_status()
                return {"status": "success", "message": f"Model {model_name} unloaded"}
        except Exception as e:
            logger.error(f"Error unloading model {model_name}: {e}")
            return {"status": "error", "message": str(e)}

    async def load_model(self, base_url: str, model_name: str, kind: str | None = None) -> dict[str, Any]:
        """Load a model into memory and pin it there (keep_alive -1).

        Args:
            base_url: Base URL of the target Ollama endpoint.
            model_name: Name of the model to load.
            kind: Load kind ("chat"/"embed"); detected from capabilities if None.

        Returns:
            Dictionary with status and message.
        """
        try:
            kind = await self.resolve_model_kind(base_url, model_name, kind)
            path, payload = self._load_request(kind, model_name, -1)
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{base_url}{path}", json=payload)
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
