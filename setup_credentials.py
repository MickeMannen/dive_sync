#!/usr/bin/env python3
"""Interactive credential setup for every service dive_sync can talk to.

Each service is configured in its own section; a section you skip keeps
whatever is already stored. Credentials are written to ``credentials.json``
in ``DATA_DIR`` (the current directory when unset), so a separate account set
for testing is simply a separate DATA_DIR:

    DATA_DIR=~/.dive_sync_test python setup_credentials.py

Every section verifies the login read-only before anything is saved.
"""
import argparse
import getpass
import logging
import os
import sys
from typing import Callable, Dict, List, Optional, Tuple

from src.core import layout
from src.core.config import (
    CREDENTIALS_FILE,
    ConfigManager,
    CredentialsModel,
    DivelogsCredentials,
    GarminCredentials,
    SUBMERSION_ENABLED,
    SubmersionCredentials,
    SubsurfaceCredentials,
    normalize_endpoint_url,
    region_from_endpoint,
)

SERVICES = ("garmin", "divelogs", "subsurface") + (("submersion",) if SUBMERSION_ENABLED else ())


def append_to_gitignore(entry: str):
    gitignore_path = ".gitignore"
    content = ""
    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r") as f:
            content = f.read()

    if entry not in content.splitlines():
        with open(gitignore_path, "a") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write(f"{entry}\n")
        print(f"Added '{entry}' to {gitignore_path}")


def _ask(prompt: str, default: str = "", secret: bool = False) -> str:
    shown = f"{prompt} [{default}]: " if default and not secret else f"{prompt}: "
    if secret:
        value = getpass.getpass(shown + ("(keep current) " if default else ""))
    else:
        value = input(shown)
    value = value.strip()
    return value or default


def _yes(prompt: str, default: bool = True) -> bool:
    answer = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


# ---------------------------------------------------------------- sections

def setup_garmin(current: CredentialsModel) -> Tuple[Optional[GarminCredentials], Optional[bool]]:
    accounts = current.get_garmin_accounts()
    existing = accounts[0] if accounts else GarminCredentials()
    if len(accounts) > 1:
        print(f"ℹ {len(accounts)} Garmin accounts are stored; this tool edits the first one ({existing.username}).")
    print("\n--- Garmin Connect ---")
    user = _ask("Username/Email", existing.username)
    password = _ask("Password", existing.password, secret=True)
    if not (user and password):
        print("ℹ Garmin skipped.")
        return None, None
    creds = GarminCredentials(username=user, password=password, token_dir=existing.token_dir)
    print("Authenticating with Garmin Connect...")
    from src.core.services.garmin import GarminAdapter
    token_dir = layout.garmin_token_dir(user, creds.token_dir)
    ok = GarminAdapter(username=user, password=password, token_dir=token_dir, cooldown_seconds=1.0).login()
    print("✓ Garmin Connect authentication succeeded!" if ok else "✗ Garmin Connect authentication failed.")
    return creds, ok


def setup_divelogs(current: CredentialsModel) -> Tuple[Optional[DivelogsCredentials], Optional[bool]]:
    accounts = current.get_divelogs_accounts()
    existing = accounts[0] if accounts else DivelogsCredentials()
    if len(accounts) > 1:
        print(f"ℹ {len(accounts)} Divelogs accounts are stored; this tool edits the first one ({existing.username}).")
    print("\n--- Divelogs.org ---")
    user = _ask("Username", existing.username)
    password = _ask("Password", existing.password, secret=True)
    if not (user and password):
        print("ℹ Divelogs skipped.")
        return None, None
    creds = DivelogsCredentials(username=user, password=password)
    print("Authenticating with Divelogs.org...")
    from src.core.services.divelogs import DivelogsAdapter
    ok = DivelogsAdapter(username=user, password=password, cooldown_seconds=1.0).login()
    print("✓ Divelogs.org authentication succeeded!" if ok else "✗ Divelogs.org authentication failed.")
    return creds, ok


def setup_subsurface(current: CredentialsModel) -> Tuple[Optional[SubsurfaceCredentials], Optional[bool]]:
    existing = current.first_subsurface_account()
    print("\n--- Subsurface Cloud ---")
    print("The email and password you use for cloud storage in Subsurface (Preferences > Cloud).")
    email = _ask("Email", existing.email)
    password = _ask("Password", existing.password, secret=True)
    base_url = _ask("Server", existing.base_url or SubsurfaceCredentials().base_url)
    if not (email and password):
        print("ℹ Subsurface skipped.")
        return None, None
    creds = SubsurfaceCredentials(email=email, password=password, base_url=base_url)
    print("Checking Subsurface Cloud login...")
    from src.core.services.subsurface_cloud import check_cloud_login
    ok, message = check_cloud_login(email, password, base_url)
    print(("✓ " if ok else "✗ ") + message)
    return creds, ok


def setup_submersion(current: CredentialsModel) -> Tuple[Optional[SubmersionCredentials], Optional[bool]]:
    existing = current.submersion
    print("\n--- Submersion sync store (S3-compatible, e.g. Backblaze B2) ---")
    print("In Submersion, set the sync provider to S3 and point it at the same bucket.")
    print("For Backblaze B2: create a bucket, then an application key restricted to that bucket;")
    print("the endpoint is s3.<region>.backblazeb2.com - https:// is assumed when you leave the scheme off.")
    endpoint = normalize_endpoint_url(_ask("Endpoint URL", existing.endpoint_url))
    bucket = _ask("Bucket", existing.bucket)
    key_id = _ask("Access key id (B2: application key id)", existing.access_key_id)
    secret = _ask("Secret access key (B2: application key)", existing.secret_access_key, secret=True)
    # Advanced, the way Submersion folds these away: the region is normally read
    # straight out of the endpoint, and the prefix is the one Submersion uses.
    derived = region_from_endpoint(endpoint)
    print(f"Region read from the endpoint: {derived or '(none - a self-hosted store needs one set by hand)'}")
    region = ""
    if not derived or existing.region or _yes("Override the region?", default=False):
        region = _ask("Region (blank = read it from the endpoint)", existing.region)
    prefix = _ask("Key prefix", existing.prefix or "submersion-sync/")
    if not (endpoint and bucket and key_id and secret):
        print("ℹ Submersion store skipped.")
        return None, None
    creds = SubmersionCredentials(store_type="s3", endpoint_url=endpoint, region=region, bucket=bucket,
                                  prefix=prefix, access_key_id=key_id, secret_access_key=secret,
                                  path_style=existing.path_style)
    print("Checking the store (one read-only listing)...")
    from src.core.services.submersion.store import check_store_access
    ok, message = check_store_access(creds)
    print(("✓ " if ok else "✗ ") + message)
    return creds, ok


SECTIONS: Dict[str, Callable[[CredentialsModel], Tuple[Optional[object], Optional[bool]]]] = {
    "garmin": setup_garmin,
    "divelogs": setup_divelogs,
    "subsurface": setup_subsurface,
    "submersion": setup_submersion,
}


def _apply(current: CredentialsModel, service: str, creds) -> CredentialsModel:
    """Store ``creds`` for ``service`` without touching the other sections.
    Garmin/Divelogs keep a list shape when one is stored (first entry replaced)."""
    update = {}
    if service == "garmin":
        accounts = current.get_garmin_accounts()
        update["garmin"] = [creds] + accounts[1:] if isinstance(current.garmin, list) else creds
    elif service == "divelogs":
        accounts = current.get_divelogs_accounts()
        update["divelogs"] = [creds] + accounts[1:] if isinstance(current.divelogs, list) else creds
    else:
        update[service] = creds
    return current.model_copy(update=update)


def run(services: List[str], path: str = CREDENTIALS_FILE, assume_yes: bool = False) -> int:
    current = ConfigManager.load_credentials(path) if os.path.exists(path) else CredentialsModel()
    configured = current.configured_services()
    print("=" * 60)
    print("      DIVE SYNC INTERACTIVE PROVISIONER & CREDENTIAL SETUP")
    print("=" * 60)
    print(f"Credentials file: {path}")
    print(f"Already configured: {', '.join(configured) or 'nothing'}")

    updated = current
    changed: List[str] = []
    failed: List[str] = []
    for service in services:
        if service in configured and not assume_yes and not _yes(f"\n{service}: already configured. Change it?", default=False):
            continue
        creds, ok = SECTIONS[service](updated)
        if creds is None:
            continue
        if ok is False and not assume_yes:
            if not _yes(f"{service}: verification failed. Save anyway?", default=False):
                continue
        updated = _apply(updated, service, creds)
        changed.append(service)
        if ok is False:
            failed.append(service)

    if not changed:
        print("\nNothing changed.")
        return 0
    ConfigManager.save_credentials(updated, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    print(f"\n✓ Saved {', '.join(changed)} to {path}")
    if failed:
        print(f"⚠ Saved without a successful check: {', '.join(failed)}")
    if os.path.abspath(os.path.dirname(path) or ".") == os.path.abspath("."):
        append_to_gitignore("credentials.json")
        append_to_gitignore("data/")
        append_to_gitignore("sync/")
    return 1 if failed else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Configure and verify dive_sync service credentials.")
    parser.add_argument("--services", default=",".join(SERVICES),
                        help=f"Comma-separated subset of {', '.join(SERVICES)} (default: all).")
    parser.add_argument("--yes", action="store_true",
                        help="Do not ask before replacing configured sections or saving failed checks.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    services = [s.strip() for s in args.services.split(",") if s.strip()]
    unknown = [s for s in services if s not in SERVICES]
    if unknown:
        parser.error(f"unknown service(s): {', '.join(unknown)}")
    return run(services)


if __name__ == "__main__":
    sys.exit(main())
