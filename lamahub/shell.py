"""The single Druids app-shell instance for lamahub.

Design system, base template, accent theming, login page and session
handling all come from the installed ``druids`` package (pip name
``druidforms``); this module just configures it from the environment.
"""

import hashlib
import os

from druids import Druids, LoginSettings

from lamahub.config import AUTHOR, BASE_PATH, GITHUB_URL, VERSION
from lamahub.env import env

_templates_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

login = (
    LoginSettings(user=env.login_user, password=env.login_password, timeout_minutes=env.login_timeout)
    if env.login_enabled
    else None
)

druids = Druids(
    "Lamahub",
    version=VERSION,
    author=AUTHOR,
    github_url=GITHUB_URL,
    base_path=BASE_PATH,
    login=login,
    templates_dir=_templates_dir,
)


def _static_version() -> str:
    """Short content hash of lamahub/static, appended as ?v= to the app's asset
    URLs so a rebuild busts browser caches (StaticFiles sends no Cache-Control,
    so browsers may otherwise reuse a stale deploy.js heuristically)."""
    digest = hashlib.sha256()
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    for root, _dirs, files in sorted(os.walk(static_dir)):
        for name in sorted(files):
            with open(os.path.join(root, name), "rb") as fh:
                digest.update(fh.read())
    return digest.hexdigest()[:10]


druids.templates.env.globals["asset_v"] = _static_version()
