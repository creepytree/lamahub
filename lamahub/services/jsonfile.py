"""Small JSON-on-disk helpers shared by the instance stores."""

import json
import os

from lamahub.extensions import logger


def read_json(path: str, label: str) -> dict | None:
    """Read a JSON object; None when missing, unreadable or not an object."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Error reading {label}: {e}")
        return None
    return data if isinstance(data, dict) else None


def write_json(path: str, data: dict) -> None:
    """Write-temp-then-rename so a crash mid-write can't corrupt the file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_file = f"{path}.tmp"
    with open(tmp_file, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp_file, path)
