"""Page routes for the Lamahub."""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from lamahub.extensions import logger
from lamahub.config import BASE_PATH
from lamahub.shell import druids

pages = APIRouter()


@pages.get("/")
async def root(request: Request):
    """Render the main dashboard page."""
    logger.debug("Rendering dashboard")
    return druids.templates.TemplateResponse(request, "main.jinja2", {})


# Keep /home for backward compatibility
@pages.get("/home")
async def home():
    """Redirect to root for backward compatibility."""
    return RedirectResponse(url=f"{BASE_PATH}/")


# Keep /models for backward compatibility
@pages.get("/models")
async def models():
    """Redirect to root for backward compatibility."""
    return RedirectResponse(url=f"{BASE_PATH}/")
