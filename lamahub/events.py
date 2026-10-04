"""Socket.IO event handlers for real-time communication."""

from lamahub.extensions import sio, logger
from lamahub.shell import druids


@sio.event
async def connect(sid, environ):
    """Handle client connection.

    Socket.IO is mounted outside the FastAPI stack, so the druids session
    middleware does not cover it; reject unauthenticated handshakes here when
    login is enabled.
    """
    if druids.auth.enabled and not druids.auth.is_cookie_header_authenticated(environ.get("HTTP_COOKIE")):
        logger.warning(f"Rejected unauthenticated Socket.IO connection: {sid}")
        return False
    logger.debug(f"Client connected: {sid}")


@sio.event
async def disconnect(sid):
    """Handle client disconnection."""
    logger.debug(f"Client disconnected: {sid}")


@sio.event
async def refresh_models(sid):
    """Handle client request to refresh model data."""
    logger.debug(f"Client {sid} requested model refresh")
    await sio.emit("model_update", {"refresh": True}, room=sid)
