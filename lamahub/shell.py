"""The single Druids app-shell instance for lamahub.

Design system, base template, accent theming, login page and session
handling all come from the installed ``druids`` package (pip name
``druidforms``); this module just configures it from the environment.
"""

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
