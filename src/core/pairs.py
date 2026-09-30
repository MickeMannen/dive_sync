"""Sync pairs: turn service specs into adapters and a ``SyncEngine`` (rework.md F3).

A *service spec* is ``<service id>[:<argument>]``:

    garmin                 Garmin Connect (credentials.json)
    divelogs               Divelogs.org (credentials.json)
    uddf:<file>            a UDDF 3.2 file
    subsurface:<directory> a Subsurface git-storage checkout
    shearwater             the Shearwater app's database of a configured account (credentials
                           ``shearwater``; several accounts pick one per run, none saved = the app's active one)
    shearwater:<file>      one such database named by its path
    subsurface-cloud       Subsurface Cloud (credentials.json ``subsurface``; clone under subsurface/<account>/cloud)
    submersion             Submersion sync store (credentials.json ``submersion``: S3 bucket or folder)

Relative file arguments resolve against ``DATA_DIR``. With ``mock_data_dir``
the Garmin and Divelogs specs give the offline mock adapters, so a pair such
as ``garmin -> uddf:out.uddf`` can be exercised without credentials.
"""
from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple, Dict

from src.core.adapter import BaseDiveAdapter
from src.core import config, layout
from src.core.config import SERVICE_ID_ALIASES, ConfigManager, SettingsModel, SyncPairModel
from src.core.fields import FieldLink, common_default_links, default_field_links, submersion_default_links, FieldSpec

KNOWN_SERVICES = ("garmin", "divelogs", "uddf", "subsurface", "subsurface-cloud", "submersion", "shearwater")
FILE_SERVICES = ("uddf", "subsurface")
# takes a path or not: without one the configured account's file is used
OPTIONAL_ARG_SERVICES = ("shearwater",)
# spec name -> service id (the cloud adapter shares Subsurface's catalogue and ids)
SERVICE_IDS = SERVICE_ID_ALIASES


def service_id_of(spec: str) -> str:
    name, _ = parse_service_spec(spec)
    return SERVICE_IDS.get(name, name)


def parse_service_spec(spec: str) -> Tuple[str, Optional[str]]:
    service, _, arg = spec.strip().partition(":")
    service = service.strip().lower()
    if service not in KNOWN_SERVICES:
        raise ValueError(f"Unknown service {service!r}; expected one of {', '.join(KNOWN_SERVICES)}")
    if service == "submersion" and not config.SUBMERSION_ENABLED:
        raise ValueError("Submersion sync is disabled for now (it does not work yet)")
    arg = arg.strip() or None
    if service in FILE_SERVICES and not arg:
        raise ValueError(f"Service {service!r} needs a path: {service}:<path>")
    if service not in FILE_SERVICES and service not in OPTIONAL_ARG_SERVICES and arg:
        raise ValueError(f"Service {service!r} takes no argument")
    return service, arg


def field_catalog_of(service_id: str) -> list:
    """A service's field catalogue, by service id."""
    if service_id == "garmin":
        from src.core.services.garmin import GarminAdapter as adapter
    elif service_id == "divelogs":
        from src.core.services.divelogs import DivelogsAdapter as adapter
    elif service_id in ("subsurface", "subsurface-cloud"):
        from src.core.services.subsurface import SubsurfaceAdapter as adapter
    elif service_id == "uddf":
        from src.core.services.uddf import UddfAdapter as adapter
    elif service_id == "submersion":
        from src.core.services.submersion.adapter import SubmersionAdapter as adapter
    elif service_id == "shearwater":
        from src.core.services.shearwater import ShearwaterAdapter as adapter
    else:
        raise ValueError(f"Unknown service {service_id!r}")
    return adapter.field_catalog()


def display_name_of(spec: str) -> str:
    """The name a service spec is shown under ("subsurface-cloud" -> "Subsurface Cloud")."""
    service, _ = parse_service_spec(spec)
    if service == "subsurface-cloud":
        from src.core.services.subsurface_cloud import SubsurfaceCloudAdapter
        return SubsurfaceCloudAdapter.display_name
    return {"garmin": "Garmin Connect", "divelogs": "Divelogs.org", "subsurface": "Subsurface",
            "uddf": "UDDF file", "submersion": "Submersion", "shearwater": "Shearwater app"}.get(service, service)


def default_links_for(source_id: str, target_id: str) -> List[FieldLink]:
    """The shipped board for a pair: the Garmin/Divelogs one when that is the
    pair, the metadata-only one when Submersion is a side (rework.md F17),
    otherwise the common links (dive number as a match key where both
    services let the user set it). A common link naming a field one of the
    two services does not have (Subsurface keeps no visibility, UDDF no
    weight) is left out: the board could not be saved with it."""
    if (source_id, target_id) == ("garmin", "divelogs"):
        return default_field_links()
    numbered = "divelogs" not in (source_id, target_id)
    if "submersion" in (source_id, target_id):
        links = submersion_default_links(source_id, target_id, match_on_dive_number=numbered)
    else:
        links = common_default_links(source_id, target_id, match_on_dive_number=numbered)
    if "shearwater" in (source_id, target_id):
        links += _shearwater_site_links(source_id, target_id)
    try:
        catalog = {f.key: f for sid in (source_id, target_id) for f in field_catalog_of(sid)}
    except ValueError:
        return links
    return _valid_for_pair(links, catalog)


def _valid_for_pair(links: List[FieldLink], catalog: Dict[str, FieldSpec]) -> List[FieldLink]:
    """The shipped links a pair can actually save: a link naming a field the
    pair lacks, joining fields of different types (Shearwater's visibility
    is text, Garmin's a number) or writing a field its service cannot
    write (Shearwater's tanks) is dropped - a two-way link whose one side
    is unwritable becomes one-way towards the writable side. Without this
    the board opened with a rule the validator refused, so Save never
    succeeded and the page stayed "unsaved" (found 2026-09-30)."""
    from src.core.fields import validate_field_links
    out: List[FieldLink] = []
    for link in links:
        if link.target not in catalog or any(k not in catalog for k in link.source):
            continue
        candidates = [link]
        if link.direction == "bidirectional":
            candidates += [link.model_copy(update={"direction": "to_target"}),
                           link.model_copy(update={"direction": "to_source"})]
        for candidate in candidates:
            if not validate_field_links([candidate], catalog):
                out.append(candidate)
                break
    return out


def _shearwater_site_links(source_id: str, target_id: str) -> List[FieldLink]:
    """The site fields of a Shearwater pair, which the generic board cannot
    guess (rework.md Track H): the app's Site is the dive site (unified
    ``location``) and its Location the area. Garmin's site is its location
    name; Divelogs has the same two-level split (location + dive site);
    Subsurface and UDDF have one site name."""
    other = target_id if source_id == "shearwater" else source_id
    site_of = {"garmin": "garmin.locationName", "divelogs": "divelogs.divesite", "subsurface": "subsurface.location",
               "uddf": "uddf.location"}.get(other)
    if not site_of:
        return []
    pairs = [("site", site_of, "shearwater.site")]
    if other == "divelogs":
        pairs.append(("area", "divelogs.location", "shearwater.location"))
    links = []
    for link_id, other_key, shearwater_key in pairs:
        source, target = (shearwater_key, other_key) if source_id == "shearwater" else (other_key, shearwater_key)
        links.append(FieldLink(id=link_id, source=[source], target=target, conflict="manual"))
    return links


def _resolve_path(arg: str) -> str:
    if os.path.isabs(arg):
        return arg
    return os.path.join(os.environ.get("DATA_DIR", "."), arg)


def build_adapter(spec: str, settings: SettingsModel, credentials_path: Optional[str] = None,
                  mock_data_dir: Optional[str] = None, garmin_username: Optional[str] = None,
                  divelogs_username: Optional[str] = None,
                  subsurface_username: Optional[str] = None,
                  shearwater_account: Optional[str] = None) -> BaseDiveAdapter:
    service, arg = parse_service_spec(spec)
    if service == "uddf":
        from src.core.services.uddf import UddfAdapter
        return UddfAdapter(_resolve_path(arg))
    if service == "subsurface":
        from src.core.services.subsurface import SubsurfaceAdapter
        return SubsurfaceAdapter(_resolve_path(arg))
    if service == "shearwater":
        from src.core.config import CREDENTIALS_FILE
        from src.core.services.shearwater import ShearwaterAdapter
        if arg:
            return ShearwaterAdapter(_resolve_path(arg))
        accounts = ConfigManager.load_credentials(credentials_path or CREDENTIALS_FILE).get_shearwater_accounts()
        if not accounts:
            raise ValueError("No Shearwater app database configured or found on this computer; "
                             "add one in Settings or name the file as shearwater:<path>")
        picked = _pick(accounts, shearwater_account, "Shearwater app", "--shearwater-account", key=lambda a: a.name)
        return ShearwaterAdapter(picked.resolved_path())
    if service == "subsurface-cloud":
        from src.core.config import CREDENTIALS_FILE
        from src.core.services.subsurface_cloud import SubsurfaceCloudAdapter
        accounts = ConfigManager.load_credentials(credentials_path or CREDENTIALS_FILE).get_subsurface_accounts()
        if not accounts:
            raise ValueError("Subsurface Cloud is not configured; run setup_credentials.py --services subsurface")
        creds = _pick(accounts, subsurface_username, "Subsurface Cloud", "--subsurface", key=lambda a: a.email)
        if not creds.configured:
            raise ValueError(f"No Subsurface Cloud password stored for {creds.email}.")
        return SubsurfaceCloudAdapter(creds.email, creds.password, creds.base_url)
    if service == "submersion":
        from src.core.config import CREDENTIALS_FILE
        from src.core.services.submersion.adapter import SubmersionAdapter
        creds = ConfigManager.load_credentials(credentials_path or CREDENTIALS_FILE).submersion
        if not creds.configured:
            raise ValueError("Submersion is not configured; run setup_credentials.py --services submersion")
        return SubmersionAdapter(creds)
    if mock_data_dir:
        from src.core.services.mock_adapters import LocalMockDivelogsAdapter, LocalMockGarminAdapter
        if service == "garmin":
            return LocalMockGarminAdapter(mock_data_dir=mock_data_dir, username=garmin_username)
        return LocalMockDivelogsAdapter(mock_data_dir=mock_data_dir, username=divelogs_username)
    from src.core.config import CREDENTIALS_FILE
    creds = ConfigManager.load_credentials(credentials_path or CREDENTIALS_FILE)
    if service == "garmin":
        from src.core.services.garmin import GarminAdapter
        accounts = creds.get_garmin_accounts()
        account = _pick(accounts, garmin_username, "Garmin", "--garmin")
        token_dir = layout.garmin_token_dir(account.username, account.token_dir)
        return GarminAdapter(username=account.username, password=account.password, token_dir=token_dir,
                             cooldown_seconds=settings.api_cooldown_seconds)
    from src.core.services.divelogs import DivelogsAdapter
    accounts = creds.get_divelogs_accounts()
    account = _pick(accounts, divelogs_username, "Divelogs", "--divelogs")
    return DivelogsAdapter(username=account.username, password=account.password,
                           cooldown_seconds=settings.api_cooldown_seconds)


def _pick(accounts, username: Optional[str], label: str, flag: str, key=lambda a: a.username):
    if not accounts:
        raise ValueError(f"No {label} account configured.")
    if len(accounts) == 1 and not username:
        return accounts[0]
    if not username:
        raise ValueError(f"Multiple {label} accounts configured. Please specify {flag} username.")
    for account in accounts:
        if key(account) == username:
            return account
    raise ValueError(f"No {label} account configured matching username: {username}")


# The spec a configured service is synced through (Subsurface: its cloud).
CONFIGURED_SPECS = {"garmin": "garmin", "divelogs": "divelogs", "subsurface": "subsurface-cloud",
                    "submersion": "submersion", "shearwater": "shearwater"}


CACHE_SERVICE_ORDER = ("garmin", "divelogs", "submersion", "subsurface")


def cache_services(configured: List[str]) -> List[dict]:
    """The services a "Download dives" control offers, as {id, label, spec}:
    every configured service (credentials.configured_services()), in the
    order the pages show them. Both the desktop Sync page and the web
    dashboard build their pick lists from this."""
    return [{"id": s, "label": display_name_of(CONFIGURED_SPECS[s]), "spec": CONFIGURED_SPECS[s]}
            for s in CACHE_SERVICE_ORDER if s in configured]


def configured_specs(configured: List[str]) -> List[str]:
    """The specs of ``configured`` (credentials.configured_specs(): service
    ids, or a full spec such as ``shearwater:<path>`` for a service whose
    spec carries a path; plain ids from the older configured_services() are
    still accepted)."""
    out = []
    for entry in configured:
        if entry in CONFIGURED_SPECS:
            out.append(CONFIGURED_SPECS[entry])
        elif ":" in entry:
            try:
                parse_service_spec(entry)
            except ValueError:
                continue
            out.append(entry)
    return out


def board_pairs(settings: SettingsModel, configured: List[str]) -> List[dict]:
    """The mapping boards to offer: every saved pair, then each combination of
    the ``configured`` services (credentials.configured_specs()) that no
    saved pair joins yet - with the shipped default board until it is saved,
    which adds it to ``sync_pairs``. What the Sync page runs for such a
    combination (engine_for) uses that saved pair, so both pages agree.
    Entries: {id, source, target (specs), saved}."""
    out, joined = [], []
    for p in settings.sync_pairs:
        try:
            joined.append({service_id_of(p.source), service_id_of(p.target)})
        except ValueError:
            continue            # a disabled (Submersion) or unknown service: no board to offer
        out.append({"id": p.id, "source": p.source, "target": p.target, "saved": True})
    ids = {p.id for p in settings.sync_pairs}
    specs = configured_specs(configured)
    for i, source in enumerate(specs):
        for target in specs[i + 1:]:
            if {service_id_of(source), service_id_of(target)} in joined:
                continue
            pair_id = base = f"{service_id_of(source)}_{service_id_of(target)}"
            n = 2
            while pair_id in ids:
                pair_id, n = f"{base}_{n}", n + 1
            ids.add(pair_id)
            out.append({"id": pair_id, "source": source, "target": target, "saved": False})
    return out


def sync_endpoints(settings: SettingsModel, configured: List[str]) -> List[dict]:
    """What a Source or Target picker offers: every configured service, then
    the other ends of saved pairs (e.g. a UDDF file), as {spec, id, label}."""
    out, seen = [], set()
    specs = configured_specs(configured)
    specs += [spec for p in settings.sync_pairs for spec in (p.source, p.target)]
    for spec in specs:
        if spec in seen:
            continue
        seen.add(spec)
        try:
            out.append({"spec": spec, "id": service_id_of(spec), "label": display_name_of(spec)})
        except ValueError:
            continue
    return out


def board_between(boards: List[dict], a_spec: str, b_spec: str) -> Optional[dict]:
    """The board (board_pairs entry) joining two specs, either way round; a
    saved pair before an unsaved combination."""
    matches = [b for b in boards if {b["source"], b["target"]} == {a_spec, b_spec}]
    return next((b for b in matches if b["saved"]), matches[0] if matches else None)


def engine_for_board(settings: SettingsModel, board: dict, **kwargs):
    """The engine of a board_pairs entry: a saved pair by its settings, a
    combination by its two specs."""
    if board["saved"]:
        return engine_for_pair(find_pair(settings, board["id"]), **kwargs)
    return engine_for(board["source"], board["target"], **kwargs)


def conflicts_file_for(board: dict, state_dir: str) -> str:
    """Where a board's engine keeps its conflicts (beside its state file,
    SyncEngine), so they can be listed without building an engine."""
    from src.core.conflicts import conflicts_path_for
    return conflicts_path_for(legacy_state_file(state_dir, service_id_of(board["source"]), service_id_of(board["target"])))


# -- per-account sync state (rework.md E19, desktop app) ---------------------

def adapter_account(adapter) -> str:
    """The account an adapter logs in as ("" for a file, a store or a local
    checkout): Garmin/Divelogs' username, Subsurface Cloud's email, the
    Shearwater database's account folder."""
    return str(getattr(adapter, "email", None) or getattr(adapter, "username", None)
               or getattr(adapter, "account", None) or "")


def _safe_state_part(account: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.@-]", "_", account)


def legacy_state_file(state_dir: str, source_id: str, target_id: str) -> str:
    """The state file a pair had before it was kept per account, in the
    ``sync/`` folder beside settings.json (``state_dir``, layout.py)."""
    name = "sync_state.json" if (source_id, target_id) == ("garmin", "divelogs") \
        else f"sync_state_{source_id}_{target_id}.json"
    return os.path.join(layout.sync_dir(state_dir), name)


def account_state_file(state_dir: str, source_id: str, source_account: str,
                       target_id: str, target_account: str) -> str:
    """The state file of one account combination of a pair. Garmin ->
    Divelogs keeps the name it already had with several accounts
    (``sync_state_<garmin user>_<divelogs user>.json``); every other pair
    names each side as ``<service>-<account>`` (just ``<service>`` for a side
    without accounts)."""
    if (source_id, target_id) == ("garmin", "divelogs") and source_account and target_account:
        return os.path.join(layout.sync_dir(state_dir), f"sync_state_{source_account}_{target_account}.json")

    def part(service_id: str, account: str) -> str:
        return f"{service_id}-{_safe_state_part(account)}" if account else service_id
    return os.path.join(layout.sync_dir(state_dir), f"sync_state_{part(source_id, source_account)}_{part(target_id, target_account)}.json")


def find_pair(settings: SettingsModel, pair_id: str) -> SyncPairModel:
    for pair in settings.sync_pairs:
        if pair.id == pair_id:
            return pair
    raise ValueError(f"No sync pair named {pair_id!r} in settings (configured: "
                     f"{', '.join(p.id for p in settings.sync_pairs) or 'none'})")


def engine_for(source_spec: str, target_spec: str, settings_path: Optional[str] = None,
               credentials_path: Optional[str] = None, mock_data_dir: Optional[str] = None,
               garmin_username: Optional[str] = None, divelogs_username: Optional[str] = None,
               pair: Optional[SyncPairModel] = None, subsurface_username: Optional[str] = None,
               account_scoped_state: bool = False, shearwater_account: Optional[str] = None):
    """A ``SyncEngine`` for two service specs. When the specs are exactly
    ``garmin`` and ``divelogs`` the classic constructor is used so account
    selection, state files and cache directories behave as before. With
    ``pair`` the engine runs that saved pair (its direction, board and
    options come from settings on every run, rework.md G1); without one it
    runs the saved pair with these service ids if there is one, else a
    transient pair with the shipped defaults that writes its target.
    ``account_scoped_state`` (the desktop app) keeps the pair's state and
    conflicts per account combination (account_state_file)."""
    from src.core.config import CREDENTIALS_FILE, SETTINGS_FILE
    from src.core.sync_engine import SyncEngine
    settings_path = settings_path or SETTINGS_FILE
    credentials_path = credentials_path or CREDENTIALS_FILE
    settings = ConfigManager.load_settings(settings_path)
    source_id = service_id_of(source_spec)
    target_id = service_id_of(target_spec)
    if source_id == target_id:
        raise ValueError(f"Cannot sync {source_id} with itself.")

    if (source_id, target_id) == ("garmin", "divelogs"):
        engine = SyncEngine(settings_path=settings_path, credentials_path=credentials_path, mock_data_dir=mock_data_dir,
                            garmin_username=garmin_username, divelogs_username=divelogs_username,
                            account_scoped_state=account_scoped_state)
    else:
        source = build_adapter(source_spec, settings, credentials_path, mock_data_dir, garmin_username, divelogs_username,
                               subsurface_username, shearwater_account)
        target = build_adapter(target_spec, settings, credentials_path, mock_data_dir, garmin_username, divelogs_username,
                               subsurface_username, shearwater_account)
        engine = SyncEngine(settings_path=settings_path, credentials_path=credentials_path,
                            source_adapter=source, target_adapter=target, account_scoped_state=account_scoped_state)
    if pair is not None:
        engine.pair_id = pair.id
        engine.refresh_pair()
    engine.run_overrides = {}
    return engine


def engine_for_pair(pair: SyncPairModel, **kwargs):
    return engine_for(pair.source, pair.target, pair=pair, **kwargs)
