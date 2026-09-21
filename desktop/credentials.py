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

That narrow window is deliberate: the original Toga shell could not run any
cleanup on Cmd+Q, and a crash mid-operation still cannot, so "materialize
once at startup, clean up on exit" would leave the file behind. The Qt app
additionally clears everything on aboutToQuit (desktop/app.py).
"""
import os
import json
import logging
import threading
from typing import List

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


# --- multi-account storage (rework.md E7) ---------------------------------
#
# A service's account usernames live as a JSON array under one keychain
# entry ("garmin_usernames" / "divelogs_usernames"); each account's own
# fields live under a key scoped by safe_account_id(username) so usernames
# with odd characters (dots, @, ...) stay valid keyring key material - the
# same sanitizer the Garmin token cache filename uses, so both stay in sync
# by construction rather than by convention.

def _account_usernames(service: str) -> List[str]:
    raw = _get(service, "usernames")
    if not raw:
        return []
    try:
        return [u for u in json.loads(raw) if u]
    except (json.JSONDecodeError, TypeError):
        return []


def _set_account_usernames(service: str, usernames: List[str]) -> None:
    _set(service, "usernames", json.dumps(usernames) if usernames else "")


def _account_field(service: str, username: str, field: str) -> str:
    return f"account_{safe_account_id(username)}_{field}"


def _migrate_legacy_single_account(service: str) -> List[str]:
    """One-time upgrade path: before E7, each service kept exactly one
    account under a bare "<service>_username"/"<service>_password" key. If
    that's all that's there, adopt it into the new list-of-accounts scheme
    (and clear the old keys) instead of the account silently disappearing
    the first time this runs post-upgrade."""
    legacy_username = _get(service, "username")
    if not legacy_username:
        return []
    _set(service, _account_field(service, legacy_username, "password"), _get(service, "password"))
    if service == "garmin":
        _set(service, _account_field(service, legacy_username, "token_dir"), _get(service, "token_dir") or DEFAULT_GARMIN_TOKEN_DIR)
    _set_account_usernames(service, [legacy_username])
    _set(service, "username", "")
    _set(service, "password", "")
    if service == "garmin":
        _set(service, "token_dir", "")
    return [legacy_username]


def _load_accounts(service: str, cls):
    usernames = _account_usernames(service) or _migrate_legacy_single_account(service)
    accounts = []
    for username in usernames:
        kwargs = {"username": username, "password": _get(service, _account_field(service, username, "password"))}
        if service == "garmin":
            kwargs["token_dir"] = _get(service, _account_field(service, username, "token_dir")) or DEFAULT_GARMIN_TOKEN_DIR
        accounts.append(cls(**kwargs))
    return accounts


def _save_accounts(service: str, accounts) -> None:
    """Full replace: accounts not present in ``accounts`` are dropped from
    the keychain, same semantics as the status page's credentials form. An
    account with a blank password whose username already had one stored
    keeps that stored password (same rationale as save_credentials in
    src/web/app.py: adding/removing one account shouldn't force retyping
    every other account's password)."""
    previous = set(_account_usernames(service))
    new_usernames = [a.username for a in accounts if a.username]
    for username in previous - set(new_usernames):
        _set(service, _account_field(service, username, "password"), "")
        if service == "garmin":
            _set(service, _account_field(service, username, "token_dir"), "")
    for account in accounts:
        if not account.username:
            continue
        password = account.password or (_get(service, _account_field(service, account.username, "password")) if account.username in previous else "")
        _set(service, _account_field(service, account.username, "password"), password)
        if service == "garmin":
            _set(service, _account_field(service, account.username, "token_dir"), account.token_dir or DEFAULT_GARMIN_TOKEN_DIR)
    _set_account_usernames(service, new_usernames)


def load_credentials_model():
    from src.core.config import CredentialsModel, GarminCredentials, DivelogsCredentials, SubsurfaceCredentials, SubmersionCredentials

    return CredentialsModel(
        garmin=_load_accounts("garmin", GarminCredentials),
        divelogs=_load_accounts("divelogs", DivelogsCredentials),
        subsurface=SubsurfaceCredentials(
            email=_get("subsurface", "email"),
            password=_get("subsurface", "password"),
            base_url=_get("subsurface", "base_url") or SubsurfaceCredentials().base_url,
        ),
        submersion=SubmersionCredentials(
            store_type=_get("submersion", "store_type") or SubmersionCredentials().store_type,
            endpoint_url=_get("submersion", "endpoint_url"),
            region=_get("submersion", "region"),
            bucket=_get("submersion", "bucket"),
            prefix=_get("submersion", "prefix") or SubmersionCredentials().prefix,
            access_key_id=_get("submersion", "access_key_id"),
            secret_access_key=_get("submersion", "secret_access_key"),
            path_style=bool(_get("submersion", "path_style")),
            folder_path=_get("submersion", "folder_path"),
        ),
    )


def save_credentials_model(model) -> None:
    _save_accounts("garmin", model.get_garmin_accounts())
    _save_accounts("divelogs", model.get_divelogs_accounts())
    subsurface = getattr(model, "subsurface", None)
    _set("subsurface", "email", subsurface.email if subsurface else "")
    _set("subsurface", "password", subsurface.password if subsurface else "")
    _set("subsurface", "base_url", subsurface.base_url if subsurface and subsurface.email else "")

    submersion = getattr(model, "submersion", None)
    _set("submersion", "store_type", submersion.store_type if submersion else "")
    _set("submersion", "endpoint_url", submersion.endpoint_url if submersion else "")
    _set("submersion", "region", submersion.region if submersion else "")
    _set("submersion", "bucket", submersion.bucket if submersion else "")
    _set("submersion", "prefix", submersion.prefix if submersion else "")
    _set("submersion", "access_key_id", submersion.access_key_id if submersion else "")
    _set("submersion", "secret_access_key", submersion.secret_access_key if submersion else "")
    _set("submersion", "path_style", "1" if (submersion and submersion.path_style) else "")
    _set("submersion", "folder_path", submersion.folder_path if submersion else "")

    logger.info("Credentials saved to OS keychain.")


def has_any_credentials() -> bool:
    return bool(
        _account_usernames("garmin")
        or _account_usernames("divelogs")
        or _get("garmin", "username")    # pre-E7 single account, not yet migrated
        or _get("divelogs", "username")
        or _get("subsurface", "email")
        or _get("submersion", "bucket")
        or _get("submersion", "folder_path")
    )


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
    to authenticate - materializes the credentials file and every configured
    Garmin account's token file (rework.md E7: an operation may target any
    one of several accounts, and materializing only the first would force a
    fresh username/password login - risking Garmin's 429 rate limit - for
    every other account) if this is the first concurrent operation that
    needs them. Always pair with a matching end_operation() in a `finally`
    block."""
    global _active_operations
    with _operation_lock:
        _active_operations += 1
        if _active_operations == 1:
            materialize_local_cache()
            model = load_credentials_model()
            for account in model.get_garmin_accounts():
                materialize_garmin_token(account.username, account.token_dir)


def end_operation() -> None:
    """Pairs with begin_operation() - syncs the (possibly refreshed) Garmin
    token back to the keychain and removes both materialized files once the
    last concurrent operation needing them has finished."""
    global _active_operations
    with _operation_lock:
        _active_operations = max(0, _active_operations - 1)
        if _active_operations == 0:
            model = load_credentials_model()
            for account in model.get_garmin_accounts():
                sync_garmin_token_from_file(account.username, account.token_dir)
                clear_garmin_token_file(account.username, account.token_dir)
            clear_local_cache()
