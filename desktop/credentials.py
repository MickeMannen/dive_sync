"""OS keychain-backed credential storage for the desktop app (via `keyring`
- Keychain on macOS, Credential Manager on Windows, Secret Service on
Linux). This is the desktop app's only durable credential store; Docker
keeps its existing plain credentials.json unchanged (it's headless, so
there's no OS keychain to use).

Two things get keychain-backed this way: the Garmin/Divelogs
username+password, and (see materialize_garmin_token()/
sync_garmin_token_from_file()) the Garmin session token cache -
`garminconnect`/garth's token file grants live API access without the
password, so it's just as sensitive.

src.core (SyncEngine, dive_cache, GarminAdapter, ...) only knows how to read
plain files, so keychain-sourced secrets are materialized into the
locations src.core expects - under this app's private per-user data
directory (see desktop/paths.py), not a repo/cwd-relative path - only for
the duration of an actual sync/edit operation that needs them (see
begin_operation()/end_operation()), not for the app's whole lifetime.

That narrow window is deliberate, not cosmetic: toga-cocoa 0.5.6 does not
wire the native Quit menu item or Cmd+Q to App.on_exit/request_exit at all
(NSApplication's default "terminate:" handles those directly, ending the
process before any Python shutdown code - including atexit callbacks -
gets a chance to run), so "materialize once at startup, clean up on exit"
would leave the file behind indefinitely on a normal quit.
"""
import os
import logging
import threading

import keyring

from desktop.paths import data_dir
from src.core.services.garmin import safe_account_id, safe_token_filename

logger = logging.getLogger("dive_sync.desktop.credentials")

SERVICE_NAME = "DiveSync"

# Anchored to the app's private data directory rather than a bare relative
# "tokens/garmin" - a relative path resolves against whatever the process's
# current working directory happens to be at launch (project root when run
# via PyCharm/`python -m desktop`, something else entirely once packaged),
# which is exactly the inconsistency this whole module exists to avoid for
# credentials.json/settings.json.
DEFAULT_GARMIN_TOKEN_DIR = os.path.join(data_dir(), "tokens", "garmin")


def _key(service: str, field: str) -> str:
    return f"{service}_{field}"


def _get(service: str, field: str) -> str:
    return keyring.get_password(SERVICE_NAME, _key(service, field)) or ""


def _set(service: str, field: str, value: str) -> None:
    if value:
        keyring.set_password(SERVICE_NAME, _key(service, field), value)
        return
    try:
        keyring.delete_password(SERVICE_NAME, _key(service, field))
    except keyring.errors.PasswordDeleteError:
        pass


def load_credentials_model():
    from src.core.config import CredentialsModel, GarminCredentials, DivelogsCredentials

    return CredentialsModel(
        garmin=GarminCredentials(
            username=_get("garmin", "username"),
            password=_get("garmin", "password"),
            token_dir=_get("garmin", "token_dir") or DEFAULT_GARMIN_TOKEN_DIR,
        ),
        divelogs=DivelogsCredentials(
            username=_get("divelogs", "username"),
            password=_get("divelogs", "password"),
        ),
    )


def save_credentials_model(model) -> None:
    garmin_accounts = model.get_garmin_accounts()
    divelogs_accounts = model.get_divelogs_accounts()
    garmin = garmin_accounts[0] if garmin_accounts else None
    divelogs = divelogs_accounts[0] if divelogs_accounts else None

    _set("garmin", "username", garmin.username if garmin else "")
    _set("garmin", "password", garmin.password if garmin else "")
    _set("garmin", "token_dir", (garmin.token_dir if garmin else "") or DEFAULT_GARMIN_TOKEN_DIR)
    _set("divelogs", "username", divelogs.username if divelogs else "")
    _set("divelogs", "password", divelogs.password if divelogs else "")

    logger.info("Credentials saved to OS keychain.")


def has_any_credentials() -> bool:
    return bool(_get("garmin", "username") or _get("divelogs", "username"))


def materialize_local_cache() -> None:
    """Write keychain-sourced credentials into src.core.config's file-based
    store so SyncEngine/dive_cache work unmodified. Call once at app
    startup, and again whenever credentials are saved."""
    from src.core.config import ConfigManager

    ConfigManager.save_credentials(load_credentials_model())


def clear_local_cache() -> None:
    """Best-effort removal of the materialized credentials.json, so no
    plaintext copy of the keychain contents lingers on disk once nothing
    needs it."""
    from src.core.config import CREDENTIALS_FILE

    try:
        os.remove(CREDENTIALS_FILE)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("Failed to remove materialized credentials file: %s", e)


def _garmin_token_key(username: str) -> str:
    return f"garmin_token_{safe_account_id(username)}"


def materialize_garmin_token(username: str, token_dir: str) -> None:
    """If a token blob is stored in the keychain for this Garmin username,
    write it out to <token_dir>/garmin_tokens_<safe>.json so GarminAdapter's
    login() (via garth) can find and reuse it, instead of forcing a fresh
    username/password login. No-op if there's no username or nothing has
    been cached yet (e.g. before the very first successful login)."""
    if not username:
        return
    blob = keyring.get_password(SERVICE_NAME, _garmin_token_key(username))
    if not blob:
        return
    os.makedirs(token_dir, exist_ok=True)
    path = os.path.join(token_dir, safe_token_filename(username))
    with open(path, "w") as f:
        f.write(blob)


def sync_garmin_token_from_file(username: str, token_dir: str) -> None:
    """Read back whatever GarminAdapter/garth wrote (or refreshed) to the
    token file during login and store it in the keychain. Call before
    clearing the materialized token file, so a session refreshed mid-sync
    isn't lost."""
    if not username:
        return
    path = os.path.join(token_dir, safe_token_filename(username))
    try:
        with open(path, "r") as f:
            blob = f.read()
    except FileNotFoundError:
        return
    if blob:
        keyring.set_password(SERVICE_NAME, _garmin_token_key(username), blob)


def clear_garmin_token_file(username: str, token_dir: str) -> None:
    """Best-effort removal of the materialized token file, mirroring
    clear_local_cache() for credentials.json."""
    if not username:
        return
    path = os.path.join(token_dir, safe_token_filename(username))
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("Failed to remove materialized Garmin token file: %s", e)


_operation_lock = threading.Lock()
_active_operations = 0


def begin_operation() -> None:
    """Call before any sync/edit operation that needs SyncEngine/dive_cache
    to authenticate - materializes the credentials file and Garmin token
    file if this is the first concurrent operation that needs them. Always
    pair with a matching end_operation() in a `finally` block."""
    global _active_operations
    with _operation_lock:
        _active_operations += 1
        if _active_operations == 1:
            materialize_local_cache()
            model = load_credentials_model()
            garmin_accounts = model.get_garmin_accounts()
            if garmin_accounts:
                materialize_garmin_token(garmin_accounts[0].username, garmin_accounts[0].token_dir)


def end_operation() -> None:
    """Pairs with begin_operation() - syncs the (possibly refreshed) Garmin
    token back to the keychain and removes both materialized files once the
    last concurrent operation needing them has finished."""
    global _active_operations
    with _operation_lock:
        _active_operations = max(0, _active_operations - 1)
        if _active_operations == 0:
            model = load_credentials_model()
            garmin_accounts = model.get_garmin_accounts()
            if garmin_accounts:
                username, token_dir = garmin_accounts[0].username, garmin_accounts[0].token_dir
                sync_garmin_token_from_file(username, token_dir)
                clear_garmin_token_file(username, token_dir)
            clear_local_cache()
