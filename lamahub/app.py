"""FastAPI application for lamahub.

Initializes the app with Socket.IO support, static file serving and route
registration. Imported only after the environment is final (see
``lamahub.start``), because configuration is read at import time.
"""

import os
import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import socketio

from lamahub.api import api
from lamahub.routes import pages
from lamahub.shell import druids
from lamahub.env import env
from lamahub.extensions import sio, logger
from lamahub.config import BASE_PATH, INSTANCE_PATH
from lamahub.services.endpoints import registry
from lamahub.services.ollama import ollama_service

instance_path = INSTANCE_PATH
os.makedirs(instance_path, exist_ok=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Fixed models are managed only on the default endpoint.
    fixed_models_task = asyncio.create_task(ollama_service.maintain_fixed_models(registry.default.url))
    try:
        yield
    finally:
        if not fixed_models_task.done():
            fixed_models_task.cancel()
            with suppress(asyncio.CancelledError):
                await fixed_models_task
        else:
            fixed_models_task.result()


class BasePathMiddleware:
    """Allow BASE_PATH routing with or without proxy prefix stripping."""

    def __init__(self, app, base_path: str):
        self.app = app
        self.base_path = base_path
        path_parts = [part for part in base_path.split("/") if part]
        self.base_path_candidates = ["/" + "/".join(path_parts[index:]) for index in range(len(path_parts))]

    async def __call__(self, scope, receive, send):
        if self.base_path and scope["type"] in {"http", "websocket"}:
            path = scope.get("path", "")
            for base_path in self.base_path_candidates:
                if path == base_path:
                    scope = {**scope, "path": "/"}
                    break
                if path.startswith(f"{base_path}/"):
                    scope = {**scope, "path": path[len(base_path) :]}
                    break

        await self.app(scope, receive, send)


# BasePathMiddleware handles BASE_PATH; setting FastAPI.root_path too breaks StaticFiles behind stripping proxies.
lamahub = FastAPI(title="Lamahub", lifespan=lifespan)
# druids.install mounts /druids assets, login routes and the session
# middleware. Middleware runs outermost-last-added first: BasePathMiddleware
# is added after so the auth middleware only sees app-relative paths.
druids.install(lamahub)
lamahub.add_middleware(BasePathMiddleware, base_path=BASE_PATH)

if druids.login_enabled:
    logger.info(f"Login enabled (user '{env.login_user}', timeout {env.login_timeout} min)")

static_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
lamahub.mount("/static", StaticFiles(directory=static_path), name="static")

lamahub.include_router(api)
lamahub.include_router(pages)

logger.init_lamahub(instance_path)

# Socket.IO wrapper is the ASGI entrypoint; keep it mounted at its app-relative
# path, the outer middleware accepts BASE_PATH-prefixed requests too.
app = BasePathMiddleware(socketio.ASGIApp(sio, lamahub, socketio_path="socket.io"), BASE_PATH)

from lamahub import events  # noqa: E402, F401
