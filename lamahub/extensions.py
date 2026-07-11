"""Shared extensions and singletons for the Lamahub application."""

import socketio
from lamahub.services.system.log import Log

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
logger = Log()
