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


def get_sort(service: str, default_column: str) -> Tuple[str, bool]:
    """Returns (column_key, ascending)."""
    prefs = _load()
    sort = prefs.get(service, {}).get("sort", {})
    return sort.get("column", default_column), sort.get("ascending", True)


def set_sort(service: str, column: str, ascending: bool) -> None:
    prefs = _load()
    prefs.setdefault(service, {})["sort"] = {"column": column, "ascending": ascending}
    _save(prefs)
