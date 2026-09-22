"""OS keychain-backed credential storage for the desktop app (via `keyring`
- Keychain on macOS, Credential Manager on Windows, Secret Service on
Linux). This is the desktop app's only durable credential store; Docker
keeps its existing plain credentials.json unchanged (it's headless, so
there's no OS keychain to use).

Two things get keychain-backed this way: the Garmin/Divelogs
username+password (and Subsurface/Submersion's), and (see
materialize_garmin_token()/sync_garmin_token_from_file()) the Garmin session
token cache - `garminconnect`/garth's token file grants live API access
without the password, so it's just as sensitive. Everything else (Garmin
token_dir, Subsurface base_url, Submersion's store config) lives in
desktop/preferences.py's plain JSON file instead: each keychain item needs
its own one-time macOS access approval, and a hands-on test on 2026-09-22
(rework.md D7/D9) found that storing every individual field as its own
keychain item multiplied that into 15-20 separate prompts for one app
launch - most of them for values that were never secret in the first place.
One keychain item per service that actually holds a secret keeps that down
to a handful.

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

from desktop import preferences
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


def _get_json(key: str):
    raw = keyring.get_password(SERVICE_NAME, key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _set_json(key: str, value) -> None:
    if value:
        keyring.set_password(SERVICE_NAME, key, json.dumps(value))
        return
    try:
        keyring.delete_password(SERVICE_NAME, key)
    except keyring.errors.PasswordDeleteError:
        pass


# --- accounts: one keychain item per service, holding every account -------

def _account_usernames_pre_consolidation(service: str) -> List[str]:
    """The rework.md E7 scheme (2026-09-22, same day, superseded a few hours
    later by the one-item-per-service scheme below): one keychain item per
    *field per account* (``account_<id>_password``, ``account_<id>_token_dir``
    ...), indexed by a separate ``<service>_usernames`` list item. Only used
    here to migrate any keychain that happened to pick this scheme up during
    that window."""
    raw = _get(service, "usernames")
    if not raw:
        return []
    try:
        return [u for u in json.loads(raw) if u]
    except (json.JSONDecodeError, TypeError):
        return []


def _account_field(service: str, username: str, field: str) -> str:
    return f"account_{safe_account_id(username)}_{field}"


def _migrate_from_pre_consolidation_scheme(service: str) -> List[dict]:
    usernames = _account_usernames_pre_consolidation(service)
    if not usernames:
        return []
    accounts = []
    for username in usernames:
        account = {"username": username, "password": _get(service, _account_field(service, username, "password"))}
        accounts.append(account)
        if service == "garmin":
            token_dir = _get(service, _account_field(service, username, "token_dir"))
            if token_dir:
                preferences.set_garmin_token_dir(username, token_dir)
        _set(service, _account_field(service, username, "password"), "")
        if service == "garmin":
            _set(service, _account_field(service, username, "token_dir"), "")
    _set(service, "usernames", "")
    return accounts


def _migrate_from_legacy_single_account_scheme(service: str) -> List[dict]:
    """Before E7 (also 2026-09-22, earlier the same day): exactly one
    account under a bare "<service>_username"/"<service>_password" key."""
    username = _get(service, "username")
    if not username:
        return []
    account = {"username": username, "password": _get(service, "password")}
    if service == "garmin":
        token_dir = _get(service, "token_dir")
        if token_dir:
            preferences.set_garmin_token_dir(username, token_dir)
    _set(service, "username", "")
    _set(service, "password", "")
    if service == "garmin":
        _set(service, "token_dir", "")
    return [account]


def _load_raw_accounts(service: str) -> List[dict]:
    """A list of ``{"username", "password"}`` dicts, from whichever
    generation of storage this keychain happens to have - self-healing: a
    migration is immediately written back in the new scheme and the old
    keys are cleared, so this only runs once per keychain."""
    accounts = _get_json(f"{service}_accounts")
    if accounts:
        return accounts
    migrated = _migrate_from_pre_consolidation_scheme(service) or _migrate_from_legacy_single_account_scheme(service)
    if migrated:
        _set_json(f"{service}_accounts", migrated)
    return migrated


def _load_accounts(service: str, cls):
    accounts = []
    for entry in _load_raw_accounts(service):
        kwargs = {"username": entry.get("username", ""), "password": entry.get("password", "")}
        if service == "garmin":
            kwargs["token_dir"] = preferences.get_garmin_token_dir(kwargs["username"], DEFAULT_GARMIN_TOKEN_DIR)
        accounts.append(cls(**kwargs))
    return accounts


def _save_accounts(service: str, accounts) -> None:
    """Full replace: accounts not present in ``accounts`` are dropped, same
    semantics as the status page's credentials form. An account with a
    blank password whose username already had one stored keeps that stored
    password (same rationale as save_credentials in src/web/app.py: adding/
    removing one account shouldn't force retyping every other account's
    password). Garmin's token_dir is non-secret and lives in preferences.py,
    not here."""
    previous = {a.get("username"): a for a in _load_raw_accounts(service)}
    saved = []
    for account in accounts:
        if not account.username:
            continue
        password = account.password or previous.get(account.username, {}).get("password", "")
        saved.append({"username": account.username, "password": password})
        if service == "garmin":
            preferences.set_garmin_token_dir(account.username, account.token_dir or DEFAULT_GARMIN_TOKEN_DIR)
    kept_usernames = {a["username"] for a in saved}
    if service == "garmin":
        for username in set(previous) - kept_usernames:
            preferences.set_garmin_token_dir(username, "")
    _set_json(f"{service}_accounts", saved)


def load_credentials_model():
    from src.core.config import CredentialsModel, GarminCredentials, DivelogsCredentials, SubsurfaceCredentials, SubmersionCredentials

    subsurface = _get_json("subsurface")
    if subsurface is None:
        subsurface = {}
        email = _get("subsurface", "email")
        if email:
            subsurface = {"email": email, "password": _get("subsurface", "password")}
            base_url = _get("subsurface", "base_url")
            if base_url:
                preferences.set_subsurface_base_url(base_url)
            _set("subsurface", "email", "")
            _set("subsurface", "password", "")
            _set("subsurface", "base_url", "")
            _set_json("subsurface", subsurface)

    submersion_secret = _get_json("submersion_secret")
    submersion_config = preferences.get_submersion_config()
    if submersion_secret is None and not submersion_config:
        # Legacy flat scheme (pre-consolidation): every field its own item.
        access_key_id = _get("submersion", "access_key_id")
        secret_access_key = _get("submersion", "secret_access_key")
        non_secret = {field: _get("submersion", field) for field in preferences.SUBMERSION_NON_SECRET_FIELDS}
        if access_key_id or secret_access_key or any(non_secret.values()):
            submersion_secret = {"access_key_id": access_key_id, "secret_access_key": secret_access_key}
            preferences.set_submersion_config(**{k: v for k, v in non_secret.items() if v})
            submersion_config = preferences.get_submersion_config()
            for field in ("access_key_id", "secret_access_key", *preferences.SUBMERSION_NON_SECRET_FIELDS):
                _set("submersion", field, "")
            _set_json("submersion_secret", submersion_secret)
    submersion_secret = submersion_secret or {}

    return CredentialsModel(
        garmin=_load_accounts("garmin", GarminCredentials),
        divelogs=_load_accounts("divelogs", DivelogsCredentials),
        subsurface=SubsurfaceCredentials(
            email=subsurface.get("email", ""),
            password=subsurface.get("password", ""),
            base_url=preferences.get_subsurface_base_url(SubsurfaceCredentials().base_url),
        ),
        submersion=SubmersionCredentials(
            store_type=submersion_config.get("store_type") or SubmersionCredentials().store_type,
            endpoint_url=submersion_config.get("endpoint_url", ""),
            region=submersion_config.get("region", ""),
            bucket=submersion_config.get("bucket", ""),
            prefix=submersion_config.get("prefix") or SubmersionCredentials().prefix,
            access_key_id=submersion_secret.get("access_key_id", ""),
            secret_access_key=submersion_secret.get("secret_access_key", ""),
            path_style=bool(submersion_config.get("path_style")),
            folder_path=submersion_config.get("folder_path", ""),
            passphrase=submersion_secret.get("passphrase", ""),
        ),
    )


def save_subsurface_credentials(subsurface) -> None:
    """``subsurface`` is a ``SubsurfaceCredentials`` (or None to clear)."""
    if subsurface and subsurface.email:
        _set_json("subsurface", {"email": subsurface.email, "password": subsurface.password})
        preferences.set_subsurface_base_url(subsurface.base_url)
    else:
        _set_json("subsurface", None)


def save_submersion_credentials(submersion) -> None:
    """``submersion`` is a ``SubmersionCredentials`` (or None to clear)."""
    if submersion and submersion.configured:
        _set_json("submersion_secret", {
            "access_key_id": submersion.access_key_id,
            "secret_access_key": submersion.secret_access_key,
            "passphrase": submersion.passphrase,
        })
        preferences.set_submersion_config(
            store_type=submersion.store_type, endpoint_url=submersion.endpoint_url, region=submersion.region,
            bucket=submersion.bucket, prefix=submersion.prefix, path_style=submersion.path_style,
            folder_path=submersion.folder_path,
        )
    else:
        _set_json("submersion_secret", None)


def save_credentials_model(model) -> None:
    _save_accounts("garmin", model.get_garmin_accounts())
    _save_accounts("divelogs", model.get_divelogs_accounts())
    save_subsurface_credentials(getattr(model, "subsurface", None))
    save_submersion_credentials(getattr(model, "submersion", None))
    logger.info("Credentials saved to OS keychain.")


def has_any_credentials() -> bool:
    model = load_credentials_model()
    return bool(
        model.get_garmin_accounts()
        or model.get_divelogs_accounts()
        or model.subsurface.configured
        or model.submersion.configured
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
