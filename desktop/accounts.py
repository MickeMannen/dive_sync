"""Several accounts per service in the desktop app (rework.md E19).

Garmin, Divelogs and Subsurface Cloud can each hold several accounts - a
live one and a test one on the same machine. Every dives page, and the Sync
page, picks which account it works with and remembers that pick
(preferences.py). Cached dives live in one folder per account, and a pair's
sync state (links, last sync time, conflicts) is kept per account
combination (pairs.account_state_file), so a test account never sees a live
account's dives or history. Mapping boards stay per service pair: they
describe fields, not accounts."""
from __future__ import annotations

from typing import Dict, List

from desktop import credentials, preferences


ACCOUNT_SERVICES = ("garmin", "divelogs", "subsurface")


def names(service: str, model=None) -> List[str]:
    """The configured accounts of ``service`` (usernames; Subsurface: emails)."""
    if service not in ACCOUNT_SERVICES:
        return []
    model = model or credentials.load_credentials_model()
    accounts = {"garmin": model.get_garmin_accounts, "divelogs": model.get_divelogs_accounts,
                "subsurface": model.get_subsurface_accounts}[service]()
    return [credentials.account_name(a) for a in accounts if credentials.account_name(a)]


def selected(page: str, service: str, model=None) -> str:
    """The account ``page`` works with: its remembered pick while that
    account still exists, otherwise the first one ("" with none)."""
    available = names(service, model)
    saved = preferences.get_selected_account(page, service)
    if saved in available:
        return saved
    return available[0] if available else ""


def select(page: str, service: str, account: str) -> None:
    preferences.set_selected_account(page, service, account)


def sync_selection(model=None) -> Dict[str, str]:
    """{service id: account} the Sync page has picked, for every service
    with at least one account."""
    model = model or credentials.load_credentials_model()
    return {s: a for s in ACCOUNT_SERVICES if (a := selected("sync", s, model))}


def engine_kwargs(selection: Dict[str, str] = None) -> dict:
    """pairs.engine_for / engine_for_pair keyword arguments for the given
    accounts (default: the Sync page's), with per-account sync state."""
    selection = sync_selection() if selection is None else selection
    return {"garmin_username": selection.get("garmin") or None,
            "divelogs_username": selection.get("divelogs") or None,
            "subsurface_username": selection.get("subsurface") or None,
            "account_scoped_state": True}


def accounts_for_spec(spec: str, model=None) -> List[str]:
    """The accounts a pair side can run as; [""] for a side without accounts
    (a UDDF file, a local Subsurface checkout, the Submersion store)."""
    from src.core.pairs import parse_service_spec
    try:
        service, _ = parse_service_spec(spec)
    except ValueError:
        return [""]
    # "subsurface:<dir>" is a local checkout; only the cloud has accounts
    service = {"subsurface-cloud": "subsurface", "subsurface": ""}.get(service, service)
    return names(service, model) or [""]
