import logging
import os
import sys
from typing import Optional

import platformdirs

APP_NAME = "DiveSync"
# The app's identity (rework.md E21): the macOS bundle id, the macOS data
# folder and the keychain service name. Each platform names its data folder
# its own way - reverse-DNS is a macOS convention only.
APP_ID = "org.christersson.dive_sync"
WINDOWS_COMPANY = "Christersson"
LINUX_APP_DIR = "dive-sync"

logger = logging.getLogger("dive_sync.desktop.paths")


def _platform_data_dir(platform: str) -> str:
    """macOS ``~/Library/Application Support/org.christersson.dive_sync``,
    Windows ``%LOCALAPPDATA%\\Christersson\\DiveSync`` (Local, not Roaming:
    the dive caches are large), Linux ``~/.local/share/dive-sync`` (XDG)."""
    if platform == "darwin":
        return platformdirs.user_data_dir(APP_ID, appauthor=False)
    if platform == "win32":
        return platformdirs.user_data_dir(APP_NAME, WINDOWS_COMPANY, roaming=False)
    return platformdirs.user_data_dir(LINUX_APP_DIR, appauthor=False)


def data_dir() -> str:
    path = _platform_data_dir(sys.platform)
    os.makedirs(path, exist_ok=True)
    return path


def old_data_dir() -> Optional[str]:
    """The data folder of versions before 0.3.0, when it still exists. The
    new layout starts fresh and never reads or touches it (rework.md E21)."""
    path = platformdirs.user_data_dir("DiveSync", "Mikael Christersson")
    return path if os.path.isdir(path) and os.path.abspath(path) != os.path.abspath(data_dir()) else None


def configure_environment() -> None:
    """Point src.core.config's file-based settings/credentials store at this
    app's private per-user data directory, instead of the current working
    directory. Must run before any src.core module is first imported -
    DATA_DIR-derived paths (SETTINGS_FILE, CREDENTIALS_FILE) are computed
    once at import time."""
    os.environ.setdefault("DATA_DIR", data_dir())
