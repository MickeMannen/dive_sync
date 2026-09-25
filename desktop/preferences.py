"""Desktop-only UI preferences (visible/sorted columns per dive editor
section) - kept as a small dedicated JSON file in the app's private data
directory, separate from src.core.config's settings.json (that file is
shared with Docker/CLI and holds sync configuration, not UI state)."""
import os
import json
import logging
from typing import List, Tuple

from desktop.paths import data_dir

logger = logging.getLogger("dive_sync.desktop.preferences")

PREFS_FILE = os.path.join(data_dir(), "desktop_prefs.json")


def _load() -> dict:
    if not os.path.exists(PREFS_FILE):
        return {}
    try:
        with open(PREFS_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to load desktop preferences, using defaults: %s", e)
        return {}


def _save(data: dict) -> None:
    try:
        with open(PREFS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning("Failed to save desktop preferences: %s", e)


def get_visible_columns(service: str, default: List[str]) -> List[str]:
    prefs = _load()
    columns = prefs.get(service, {}).get("visible_columns")
    return columns if columns else list(default)


def set_visible_columns(service: str, columns: List[str]) -> None:
    prefs = _load()
    prefs.setdefault(service, {})["visible_columns"] = list(columns)
    _save(prefs)


def introduce_columns(service: str, keys: List[str]) -> List[str]:
    """The ``keys`` this service's table has never been offered before,
    marked as offered now. A column added in a new version is switched on
    once, even for a table whose saved column choice predates it, and stays
    off after the user hides it."""
    prefs = _load()
    section = prefs.setdefault(service, {})
    seen = section.setdefault("introduced_columns", [])
    new = [k for k in keys if k not in seen]
    if new:
        seen.extend(new)
        _save(prefs)
    return new


def get_sort(service: str, default_column: str) -> Tuple[str, bool]:
    """Returns (column_key, ascending)."""
    prefs = _load()
    sort = prefs.get(service, {}).get("sort", {})
    return sort.get("column", default_column), sort.get("ascending", True)


def set_sort(service: str, column: str, ascending: bool) -> None:
    prefs = _load()
    prefs.setdefault(service, {})["sort"] = {"column": column, "ascending": ascending}
    _save(prefs)


# -- non-secret credential fields (rework.md D9 finding, 2026-09-22) --------
#
# A Garmin account's token_dir, Subsurface's base_url and most of Submersion's
# store config aren't secrets - they were previously stored in the keychain
# alongside real passwords anyway, which multiplied how many separate
# per-item macOS Keychain access prompts a single app launch could trigger
# (each keychain item needs its own one-time OS approval). Keeping them here
# instead, next to the UI prefs above, cuts that down to one keychain item
# per service that actually holds a secret.

def get_garmin_token_dir(username: str, default: str) -> str:
    prefs = _load()
    value = prefs.get("garmin", {}).get("token_dirs", {}).get(username)
    return value or default


def set_garmin_token_dir(username: str, token_dir: str) -> None:
    prefs = _load()
    service = prefs.setdefault("garmin", {})
    dirs = service.setdefault("token_dirs", {})
    if token_dir:
        dirs[username] = token_dir
    else:
        dirs.pop(username, None)
    _save(prefs)


def get_subsurface_base_url(default: str) -> str:
    prefs = _load()
    return prefs.get("subsurface", {}).get("base_url") or default


def set_subsurface_base_url(base_url: str) -> None:
    prefs = _load()
    prefs.setdefault("subsurface", {})["base_url"] = base_url
    _save(prefs)


SUBMERSION_NON_SECRET_FIELDS = (
    "store_type", "endpoint_url", "region", "bucket", "prefix", "path_style", "folder_path",
)


def get_submersion_config() -> dict:
    prefs = _load()
    return dict(prefs.get("submersion", {}))


def set_submersion_config(**fields) -> None:
    prefs = _load()
    config = prefs.setdefault("submersion", {})
    for key, value in fields.items():
        config[key] = value
    _save(prefs)
