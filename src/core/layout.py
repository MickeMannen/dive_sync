"""Where dive_sync keeps its files inside the data folder (rework.md E21).

::

    <data root>/
        settings.json, credentials.json, desktop_prefs.json
        backups/<timestamp>/            pre-sync snapshots, one per run
        sync/                           sync_state_*.json, conflicts_*.json, sync_history.jsonl
        <service>/<account>/data/       that account's cached dives (JSON)
        garmin/<account>/fit/           the account's downloaded .fit files
        garmin/<account>/tokens/        the account's Garmin login tokens
        subsurface/<account>/cloud/     the account's Subsurface Cloud checkout
        submersion/                     Submersion device identity and clock

Everything that belongs to one account sits in one folder, so removing an
account's data is removing one folder. A side without accounts (a
Submersion store, a local Subsurface directory) uses the account folder
``default``.

The data root is ``DATA_DIR`` (the desktop app sets it to its per-user data
folder, Docker to the mounted volume); ``./data`` when unset, as before. The
settings/state folder stays where settings.json is, which a bare CLI run
keeps in the working directory.
"""
from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple

DEFAULT_ACCOUNT = "default"
DATA_SUBDIR = "data"
FIT_SUBDIR = "fit"
TOKENS_SUBDIR = "tokens"
CLOUD_SUBDIR = "cloud"
SYNC_SUBDIR = "sync"
# credentials.json's token_dir before E21. It named one shared folder; it now
# means "the account's own tokens folder", like a blank token_dir.
LEGACY_TOKEN_DIR = "tokens/garmin"


def data_root(base_dir: Optional[str] = None) -> str:
    return base_dir or os.environ.get("DATA_DIR", "./data")


def account_dir_name(account: Optional[str]) -> str:
    """One account's folder name: readable (an email stays an email) but
    never able to leave the service folder."""
    if not account or not account.strip():
        return DEFAULT_ACCOUNT
    return re.sub(r"[^A-Za-z0-9_.@-]", "_", account.strip()).lstrip(".") or DEFAULT_ACCOUNT


def account_dir(service: str, account: Optional[str], base_dir: Optional[str] = None) -> str:
    return os.path.join(data_root(base_dir), service, account_dir_name(account))


def dives_dir(service: str, account: Optional[str], base_dir: Optional[str] = None) -> str:
    """``<service>/<account>/data``: where that account's dives are cached."""
    return os.path.join(account_dir(service, account, base_dir), DATA_SUBDIR)


def all_dives_dirs(service: str, base_dir: Optional[str] = None) -> List[Tuple[str, str]]:
    """``(account folder name, dives folder)`` of every account of a service
    that has a dives folder - what a listing without an account reads."""
    root = os.path.join(data_root(base_dir), service)
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name, DATA_SUBDIR)
        if os.path.isdir(path):
            out.append((name, path))
    return out


def garmin_fit_dir(account: Optional[str], base_dir: Optional[str] = None) -> str:
    return os.path.join(account_dir("garmin", account, base_dir), FIT_SUBDIR)


def garmin_token_dir(account: Optional[str], token_dir: str = "", base_dir: Optional[str] = None) -> str:
    """Where a Garmin account's login tokens live. ``token_dir`` is the
    account's configured folder: blank (or the pre-E21 shared default) means
    the account's own ``tokens`` folder; a relative one is under the data
    root, an absolute one is used as is."""
    if not token_dir or token_dir.strip().rstrip("/\\") in (LEGACY_TOKEN_DIR, os.path.normpath(LEGACY_TOKEN_DIR)):
        return os.path.join(account_dir("garmin", account, base_dir), TOKENS_SUBDIR)
    if os.path.isabs(token_dir):
        return token_dir
    return os.path.join(data_root(base_dir), token_dir)


def subsurface_cloud_dir(account: Optional[str], base_dir: Optional[str] = None) -> str:
    return os.path.join(account_dir("subsurface", account, base_dir), CLOUD_SUBDIR)


def account_of_path(service: str, path: str) -> Optional[str]:
    """The account folder a cached dive file (``<service>/<account>/data/<file>``)
    belongs to; None for ``default`` or a path outside the layout."""
    parts = path.replace("\\", "/").split("/")
    if len(parts) >= 4 and parts[-4] == service and parts[-2] == DATA_SUBDIR:
        return None if parts[-3] == DEFAULT_ACCOUNT else parts[-3]
    return None


def sync_dir(settings_dir: str) -> str:
    """The sync bookkeeping folder beside settings.json."""
    return os.path.join(settings_dir or ".", SYNC_SUBDIR)
