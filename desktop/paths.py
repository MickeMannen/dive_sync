import os

import platformdirs

APP_NAME = "DiveSync"
APP_AUTHOR = "Mikael Christersson"


def data_dir() -> str:
    path = platformdirs.user_data_dir(APP_NAME, APP_AUTHOR)
    os.makedirs(path, exist_ok=True)
    return path


def configure_environment() -> None:
    """Point src.core.config's file-based settings/credentials store at this
    app's private per-user data directory, instead of the current working
    directory. Must run before any src.core module is first imported -
    DATA_DIR-derived paths (SETTINGS_FILE, CREDENTIALS_FILE) are computed
    once at import time."""
    os.environ.setdefault("DATA_DIR", data_dir())
