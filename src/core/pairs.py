"""Sync pairs: turn service specs into adapters and a ``SyncEngine`` (rework.md F3).

A *service spec* is ``<service id>[:<argument>]``:

    garmin                 Garmin Connect (credentials.json)
    divelogs               Divelogs.org (credentials.json)
    uddf:<file>            a UDDF 3.2 file
    subsurface:<directory> a Subsurface git-storage checkout
    subsurface-cloud       Subsurface Cloud (credentials.json ``subsurface``; clone under DATA_DIR/subsurface_cloud)
    submersion             Submersion sync store (credentials.json ``submersion``: S3 bucket or folder)

Relative file arguments resolve against ``DATA_DIR``. With ``mock_data_dir``
the Garmin and Divelogs specs give the offline mock adapters, so a pair such
as ``garmin -> uddf:out.uddf`` can be exercised without credentials.
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

from src.core.adapter import BaseDiveAdapter
from src.core.config import ConfigManager, SettingsModel, SyncPairModel
from src.core.fields import FieldLink, common_default_links, default_field_links

KNOWN_SERVICES = ("garmin", "divelogs", "uddf", "subsurface", "subsurface-cloud", "submersion")
FILE_SERVICES = ("uddf", "subsurface")
# spec name -> service id (the cloud adapter shares Subsurface's catalogue and ids)
SERVICE_IDS = {"subsurface-cloud": "subsurface"}


def service_id_of(spec: str) -> str:
    name, _ = parse_service_spec(spec)
    return SERVICE_IDS.get(name, name)


def parse_service_spec(spec: str) -> Tuple[str, Optional[str]]:
    service, _, arg = spec.strip().partition(":")
    service = service.strip().lower()
    if service not in KNOWN_SERVICES:
        raise ValueError(f"Unknown service {service!r}; expected one of {', '.join(KNOWN_SERVICES)}")
    arg = arg.strip() or None
    if service in FILE_SERVICES and not arg:
        raise ValueError(f"Service {service!r} needs a path: {service}:<path>")
    if service not in FILE_SERVICES and arg:
        raise ValueError(f"Service {service!r} takes no argument")
    return service, arg


def default_links_for(source_id: str, target_id: str) -> List[FieldLink]:
    """The shipped board for a pair: the Garmin/Divelogs one when that is the
    pair, otherwise the common links (dive number as a match key where both
    services let the user set it)."""
    if (source_id, target_id) == ("garmin", "divelogs"):
        return default_field_links()
    numbered = "divelogs" not in (source_id, target_id)
    return common_default_links(source_id, target_id, match_on_dive_number=numbered)


def _resolve_path(arg: str) -> str:
    if os.path.isabs(arg):
        return arg
    return os.path.join(os.environ.get("DATA_DIR", "."), arg)


def build_adapter(spec: str, settings: SettingsModel, credentials_path: Optional[str] = None,
                  mock_data_dir: Optional[str] = None, garmin_username: Optional[str] = None,
                  divelogs_username: Optional[str] = None) -> BaseDiveAdapter:
    service, arg = parse_service_spec(spec)
    if service == "uddf":
        from src.core.services.uddf import UddfAdapter
        return UddfAdapter(_resolve_path(arg))
    if service == "subsurface":
        from src.core.services.subsurface import SubsurfaceAdapter
        return SubsurfaceAdapter(_resolve_path(arg))
    if service == "subsurface-cloud":
        from src.core.config import CREDENTIALS_FILE
        from src.core.services.subsurface_cloud import SubsurfaceCloudAdapter
        creds = ConfigManager.load_credentials(credentials_path or CREDENTIALS_FILE).subsurface
        if not creds.configured:
            raise ValueError("Subsurface Cloud is not configured; run setup_credentials.py --services subsurface")
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
        token_dir = account.token_dir
        if not os.path.isabs(token_dir):
            token_dir = os.path.join(os.environ.get("DATA_DIR", "."), token_dir)
        return GarminAdapter(username=account.username, password=account.password, token_dir=token_dir,
                             cooldown_seconds=settings.api_cooldown_seconds)
    from src.core.services.divelogs import DivelogsAdapter
    accounts = creds.get_divelogs_accounts()
    account = _pick(accounts, divelogs_username, "Divelogs", "--divelogs")
    return DivelogsAdapter(username=account.username, password=account.password,
                           cooldown_seconds=settings.api_cooldown_seconds)


def _pick(accounts, username: Optional[str], label: str, flag: str):
    if not accounts:
        raise ValueError(f"No {label} account configured.")
    if len(accounts) == 1 and not username:
        return accounts[0]
    if not username:
        raise ValueError(f"Multiple {label} accounts configured. Please specify {flag} username.")
    for account in accounts:
        if account.username == username:
            return account
    raise ValueError(f"No {label} account configured matching username: {username}")


def find_pair(settings: SettingsModel, pair_id: str) -> SyncPairModel:
    for pair in settings.sync_pairs:
        if pair.id == pair_id:
            return pair
    raise ValueError(f"No sync pair named {pair_id!r} in settings (configured: "
                     f"{', '.join(p.id for p in settings.sync_pairs) or 'none'})")


def engine_for(source_spec: str, target_spec: str, settings_path: Optional[str] = None,
               credentials_path: Optional[str] = None, mock_data_dir: Optional[str] = None,
               garmin_username: Optional[str] = None, divelogs_username: Optional[str] = None,
               pair: Optional[SyncPairModel] = None):
    """A ``SyncEngine`` for two service specs. When the specs are exactly
    ``garmin`` and ``divelogs`` the classic constructor is used so account
    selection, state files and cache directories behave as before. The
    returned engine carries ``run_overrides``: keyword arguments for
    ``run_sync`` that apply the pair's own direction, grace window and board."""
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
                            garmin_username=garmin_username, divelogs_username=divelogs_username)
    else:
        source = build_adapter(source_spec, settings, credentials_path, mock_data_dir, garmin_username, divelogs_username)
        target = build_adapter(target_spec, settings, credentials_path, mock_data_dir, garmin_username, divelogs_username)
        engine = SyncEngine(settings_path=settings_path, credentials_path=credentials_path,
                            source_adapter=source, target_adapter=target)

    overrides = {}
    if pair is not None:
        overrides["direction_override"] = pair.directionality
        if pair.grace_window_minutes is not None:
            overrides["grace_window_override"] = pair.grace_window_minutes
        if pair.propagate_deletes is not None:
            overrides["propagate_deletes_override"] = pair.propagate_deletes
        overrides["field_links_override"] = pair.field_links if pair.field_links is not None else default_links_for(source_id, target_id)
    elif (source_id, target_id) != ("garmin", "divelogs"):
        overrides["field_links_override"] = default_links_for(source_id, target_id)
    engine.run_overrides = overrides
    # show-mapping / test-mapping read settings.field_links; keep them in step with the run
    if "field_links_override" in overrides:
        engine.settings.field_links = list(overrides["field_links_override"])
    return engine


def engine_for_pair(pair: SyncPairModel, **kwargs):
    return engine_for(pair.source, pair.target, pair=pair, **kwargs)
