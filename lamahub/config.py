"""Configuration settings for the Lamahub application."""

import os
from importlib.metadata import metadata

from lamahub.env import env

# INSTANCE_DIR for system installs to keep instance data outside the package
# directory (may not be writable); shared by the app and the fixed-model store.
INSTANCE_PATH = env.instance_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance")


def get_project_config() -> dict:
    meta = metadata("lamahub")

    version = meta["Version"]

    author = meta.get("Author")
    if not author:
        author_email = meta.get("Author-email", "")
        author = author_email.split("<")[0].strip()
    author = author or "Unknown"

    url = meta.get("Home-page")
    if not url:
        for project_url in meta.get_all("Project-URL", []):
            label, value = project_url.split(",", 1)
            if label.strip().lower() == "homepage":
                url = value.strip()
                break
    url = url or ""

    return {
        "version": version,
        "author": author,
        "github_url": url,
    }


cfg = get_project_config()

BASE_PATH = env.base_path
FIXED_MODELS = env.fixed_models

VERSION = cfg["version"]
AUTHOR = cfg["author"]
GITHUB_URL = cfg["github_url"]
