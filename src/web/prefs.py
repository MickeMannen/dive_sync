"""The web dashboard's own, app-local preferences (plans/units.md J6).

One small JSON file, ``web_prefs.json``, beside ``settings.json`` in the
data folder (``src/core/layout.py``): what this dashboard shows, not what
the sync does. Today it holds only the unit system (``{"units":
"metric"}``). By the owner's decision (2026-10-18) the choice never enters
``SettingsModel`` or ``settings.json``, so a sync profile export carries no
units and an import never changes them; the desktop app keeps its own in
``desktop_prefs.json`` and the CLI takes ``--units`` per run.
"""
from __future__ import annotations

import json
import logging
import os

from src.core import config, units

logger = logging.getLogger("dive_sync.web.prefs")

PREFS_NAME = "web_prefs.json"


def prefs_file() -> str:
    """``web_prefs.json`` beside settings.json (the data folder)."""
    return os.path.join(os.path.dirname(config.SETTINGS_FILE) or ".", PREFS_NAME)


def _load() -> dict:
    path = prefs_file()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("Failed to load web preferences, using defaults: %s", e)
        return {}


def _save(data: dict) -> None:
    path = prefs_file()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning("Failed to save web preferences: %s", e)


def get_units() -> str:
    """The unit system the dashboard shows: ``"metric"`` (the default, and
    what an unknown value and a missing file fall back to) or ``"imperial"``."""
    return units.normalize(_load().get("units"))


def set_units(system: str) -> None:
    """Remember the unit system (normalised to ``metric`` / ``imperial``)."""
    data = _load()
    data["units"] = units.normalize(system)
    _save(data)
