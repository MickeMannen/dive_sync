import os
import json
import queue
import logging
import asyncio
import threading
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.core.config import DEFAULT_PAIR_ID, ConfigManager, SettingsModel, SyncFilters, SyncScheduleSlot, GarminCredentials, DivelogsCredentials, SubsurfaceCredentials, SubmersionCredentials, CredentialsModel, CronJobModel, SyncPairModel
from src.core.fields import FieldLink, SyncRule, build_catalog, links_to_rules
from src.core.templates import preview as preview_link, validate_links
from src.core.config import ProfileError, export_profile, import_profile
from src.core.services.garmin import GarminAdapter
from src.core.services.divelogs import DivelogsAdapter
import src.core.scheduler as scheduler

# Configure logger
logger = logging.getLogger("dive_sync.web")
logger.setLevel(logging.INFO)

# SSE Queue for log streaming
sse_log_queue = queue.Queue()

class SSELogHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        try:
            log_line = self.format(record)
            self.log_queue.put_nowait(log_line)
        except Exception:
            pass

# Add SSE log handler to parent logger so it intercepts core engine logs
sse_handler = SSELogHandler(sse_log_queue)
sse_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.getLogger("dive_sync").addHandler(sse_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Start background scheduler
    scheduler_task = asyncio.create_task(scheduler.scheduler_loop())
    yield
    # Shutdown: Stop background scheduler
    scheduler_task.cancel()
    await asyncio.gather(scheduler_task, return_exceptions=True)

app = FastAPI(
    title="Dive Sync Status",
    description="Read-only status and schedule configuration for the Dive Sync scheduled-sync engine.",
    lifespan=lifespan
)

# API Schemas for Web Request
class SyncFiltersSchema(BaseModel):
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    only_new: bool = True
    sync_gases: bool = True
    use_garmin_cache: bool = True

class CronJobSchema(BaseModel):
    id: str
    # None / "" = the pair's saved direction (rework.md G0: one receiver per job)
    directionality: Optional[str] = None
    frequency: str
    hour: int
    minute: int
    day_of_week: int
    interval_minutes: int
    only_new: bool
    sync_gases: bool
    enabled: bool
    field_links: Optional[List[FieldLink]] = None
    pair: Optional[str] = None
    # a job may name its two sides instead of a pair (CronJobModel)
    source: Optional[str] = None
    target: Optional[str] = None
    use_garmin_cache: Optional[bool] = None
    garmin_username: Optional[str] = None
    divelogs_username: Optional[str] = None

class SettingsSchema(BaseModel):
    # Direction of the garmin_divelogs pair (rework.md G1: kept as the form's
    # field; omitted keeps what is stored)
    directionality: Optional[str] = None
    sync_filters: SyncFiltersSchema
    grace_window_minutes: int
    api_cooldown_seconds: float
    propagate_deletes: bool = False
    create_on_garmin: bool = False
    create_device_dives_on_submersion: bool = False
    schedule: List[Dict[str, int]]
    cron_jobs: List[CronJobSchema] = []
    # Omitted (None) keeps the board / pairs currently on disk, so a settings
    # form that does not know about them cannot wipe them. field_links is the
    # garmin_divelogs pair's board in its link form (rework.md G1/G4); a pair
    # in sync_pairs may likewise carry field_links instead of rules.
    field_links: Optional[List[FieldLink]] = None
    sync_pairs: Optional[List[SyncPairModel]] = None
    # Same omitted-keeps-current rule; an explicit "" (an emptied form field,
    # as opposed to a missing key) disables alerts.
    notify_url: Optional[str] = None

class SyncTriggerRequest(BaseModel):
    dry_run: bool = False
    # The two sides, as on the desktop Sync page; None = the default pair
    source: Optional[str] = None
    target: Optional[str] = None
    directionality: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    only_new: Optional[bool] = None
    sync_gases: Optional[bool] = None
    # None = the saved default (sync_filters.use_garmin_cache)
    use_garmin_cache: Optional[bool] = None
    garmin_username: Optional[str] = None
    divelogs_username: Optional[str] = None

class CredentialsSchema(BaseModel):
    # Repeatable rows (rework.md A8): the Garmin/Divelogs sections of the
    # form are always fully submitted, so these lists fully replace what was
    # stored (an empty list means "no accounts"), same as the old single
    # username/password fields did. A row's password left blank keeps the
    # stored password for an existing account with the same username (see
    # save_credentials) rather than wiping it.
    garmin_accounts: List[GarminCredentials] = Field(default_factory=list)
    divelogs_accounts: List[DivelogsCredentials] = Field(default_factory=list)
    # Optional sections; omitted (None) means "leave what is stored".
    subsurface: Optional[SubsurfaceCredentials] = None
    submersion: Optional[SubmersionCredentials] = None

@app.get("/api/settings")
def get_settings():
    """Settings as stored (version 2: rules on pairs). The garmin_divelogs
    pair's direction is also given at top level as ``directionality`` for the
    default-settings form (rework.md G7: the link views are gone; a
    version-1 ``field_links`` payload is still accepted on POST)."""
    settings = ConfigManager.load_settings()
    data = settings.model_dump()
    data["directionality"] = settings.directionality
    return data

@app.post("/api/settings")
def save_settings(data: SettingsSchema):
    try:
        schedule_slots = [
            SyncScheduleSlot(hour=slot["hour"], minute=slot["minute"])
            for slot in data.schedule
        ]
        cron_jobs = [
            CronJobModel(
                id=job.id,
                directionality=job.directionality or None,
                frequency=job.frequency,
                hour=job.hour,
                minute=job.minute,
                day_of_week=job.day_of_week,
                interval_minutes=job.interval_minutes,
                only_new=job.only_new,
                sync_gases=job.sync_gases,
                enabled=job.enabled,
                field_links=job.field_links,
                pair=job.pair,
                source=job.source,
                target=job.target,
                use_garmin_cache=job.use_garmin_cache,
                garmin_username=job.garmin_username,
                divelogs_username=job.divelogs_username,
            )
            for job in data.cron_jobs
        ]
        current = ConfigManager.load_settings()
        sync_pairs = list(data.sync_pairs) if data.sync_pairs is not None else list(current.sync_pairs)
        if all(p.id != DEFAULT_PAIR_ID for p in sync_pairs):
            # The pairs table on the status page leaves the garmin_divelogs
            # pair out (its direction is posted at top level, its board as
            # part of sync_pairs when the mapping board saves), so carry the
            # stored entry over rather than resetting it.
            sync_pairs.insert(0, current.default_pair())
        board_kwargs = {}
        if data.field_links is not None:
            # a version-1 style client posting the garmin_divelogs board as links
            board_kwargs["field_links"] = data.field_links

        catalog = _pair_catalog()
        problems = []
        for job in cron_jobs:
            if job.field_links:
                problems.extend(f"Job '{job.id}': {p}" for p in validate_links(job.field_links, catalog))
        if problems:
            raise HTTPException(status_code=400, detail={"message": "Field links are invalid.", "errors": problems})

        settings = SettingsModel(
            directionality=data.directionality or current.directionality,
            **board_kwargs,
            sync_filters=SyncFilters(
                date_from=data.sync_filters.date_from,
                date_to=data.sync_filters.date_to,
                only_new=data.sync_filters.only_new,
                sync_gases=data.sync_filters.sync_gases,
                use_garmin_cache=data.sync_filters.use_garmin_cache,
            ),
            grace_window_minutes=data.grace_window_minutes,
            api_cooldown_seconds=data.api_cooldown_seconds,
            propagate_deletes=data.propagate_deletes,
            create_on_garmin=data.create_on_garmin,
            create_device_dives_on_submersion=data.create_device_dives_on_submersion,
            schedule=schedule_slots,
            cron_jobs=cron_jobs,
            sync_pairs=sync_pairs,
            garmin_timezone=current.garmin_timezone,
            notify_url=data.notify_url if data.notify_url is not None else current.notify_url,
        )
        # Every pair's board is checked against its own catalogue (rework.md G5)
        for pair in settings.sync_pairs:
            if pair.rules is None:
                continue
            pair_catalog = _catalog_for_pair(pair)
            if pair_catalog is None:
                continue
            problems.extend(f"Pair '{pair.id}': {p}" for p in validate_links(pair.field_links or [], pair_catalog))
        if problems:
            raise HTTPException(status_code=400, detail={"message": "Field links are invalid.", "errors": problems})
        ConfigManager.save_settings(settings)
        logger.info("Schedule configuration updated successfully.")
        return {"status": "success", "message": "Settings updated."}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to update settings: %s", e)
        raise HTTPException(status_code=400, detail=str(e))


def _pair_catalog(source=GarminAdapter, target=DivelogsAdapter):
    return build_catalog(source.field_catalog(), target.field_catalog())


def _catalog_for_pair(pair: SyncPairModel):
    """The merged catalogue of a pair's two services, or None when a spec is unknown."""
    from src.core.pairs import parse_service_spec
    try:
        source = _adapter_class(parse_service_spec(pair.source)[0])
        target = _adapter_class(parse_service_spec(pair.target)[0])
    except (ValueError, KeyError):
        return None
    return _pair_catalog(source, target)


class PreviewRequest(BaseModel):
    link: FieldLink


@app.post("/api/fields/preview")
def preview_field_link(data: PreviewRequest):
    """Validate one (unsaved) link and render its template from an example dive."""
    return preview_link(data.link, _pair_catalog())


ADAPTER_CLASSES = {"garmin": GarminAdapter, "divelogs": DivelogsAdapter}


def _adapter_class(service_id: str):
    if service_id == "uddf":
        from src.core.services.uddf import UddfAdapter
        return UddfAdapter
    if service_id in ("subsurface", "subsurface-cloud"):
        from src.core.services.subsurface import SubsurfaceAdapter
        return SubsurfaceAdapter
    if service_id == "submersion":
        from src.core.services.submersion.adapter import SubmersionAdapter
        return SubmersionAdapter
    return ADAPTER_CLASSES[service_id]


@app.get("/api/fields")
def get_fields():
    """Field catalogues per sync pair (the garmin_divelogs pair first, then
    every saved one, then each combination of configured services with no
    saved pair yet - pairs.board_pairs), for the mapping board. Needs no
    login."""
    from src.core.pairs import board_pairs, default_links_for, parse_service_spec
    settings = ConfigManager.load_settings()
    configured = ConfigManager.load_credentials().configured_services()
    specs = [(p["id"], p["source"], p["target"], p["saved"]) for p in board_pairs(settings, configured)]
    pairs = []
    for pair_id, source_spec, target_spec, saved in specs:
        try:
            source = _adapter_class(parse_service_spec(source_spec)[0])
            target = _adapter_class(parse_service_spec(target_spec)[0])
        except (ValueError, KeyError) as e:
            logger.warning("Skipping sync pair %s: %s", pair_id, e)
            continue
        default_links = default_links_for(source.service_id, target.service_id)
        default_rules, default_keys = links_to_rules(default_links, source.service_id, target.service_id)
        pairs.append({
            "id": pair_id,
            "source": source.service_id,
            "target": target.service_id,
            # the specs a save writes into sync_pairs for a board not saved yet
            "source_spec": source_spec,
            "target_spec": target_spec,
            "saved": saved,
            "source_name": source.display_name,
            "target_name": target.display_name,
            "default_rules": {receiver: [r.model_dump() for r in items] for receiver, items in default_rules.items()},
            "default_match_keys": default_keys,
            "fields": {
                source.service_id: [f.model_dump() for f in source.field_catalog()],
                target.service_id: [f.model_dump() for f in target.field_catalog()],
            },
        })
    return {"pairs": pairs}

@app.get("/api/credentials/status")
def get_credentials_status():
    creds = ConfigManager.load_credentials()
    garmin_accounts = creds.get_garmin_accounts()
    divelogs_accounts = creds.get_divelogs_accounts()
    garmin_users = [acc.username for acc in garmin_accounts if acc.username]
    divelogs_users = [acc.username for acc in divelogs_accounts if acc.username]
    return {
        "garmin_configured": len(garmin_users) > 0,
        "divelogs_configured": len(divelogs_users) > 0,
        "garmin_username": garmin_users[0] if garmin_users else "",
        "divelogs_username": divelogs_users[0] if divelogs_users else "",
        "garmin_accounts": garmin_users,
        "divelogs_accounts": divelogs_users,
        # Structured rows (no passwords) for the repeatable-account-row
        # credentials form; garmin_accounts/divelogs_accounts above stay a
        # flat username list for dropdown-style consumers (cron job editor).
        # has_password is whether one is stored at all, never the password
        # itself: a username saved without one can't log in, and the form
        # should say so rather than leaving it to the next sync.
        "garmin_account_rows": [{"username": a.username, "token_dir": a.token_dir, "has_password": bool(a.password)}
                                for a in garmin_accounts if a.username],
        "divelogs_account_rows": [{"username": a.username, "has_password": bool(a.password)}
                                  for a in divelogs_accounts if a.username],
        "subsurface_configured": creds.subsurface.configured,
        "subsurface_email": creds.subsurface.email,
        "submersion_configured": creds.submersion.configured,
        "submersion_store": {
            "store_type": creds.submersion.store_type,
            "endpoint_url": creds.submersion.endpoint_url,
            "region": creds.submersion.region,
            # No override saved: the region comes from the endpoint, so the
            # settings page can leave its Advanced section folded away.
            "region_auto": not creds.submersion.region,
            "bucket": creds.submersion.bucket,
            "prefix": creds.submersion.prefix,
            "path_style": creds.submersion.path_style,
            "folder_path": creds.submersion.folder_path,
        },
    }


def _merge_passwords(submitted: list, existing: list):
    """A row with a blank password whose username matches an already-stored
    account keeps that account's stored password, so adding/removing one
    account doesn't force retyping every other account's password."""
    existing_by_username = {a.username: a for a in existing if a.username}
    merged = []
    for account in submitted:
        if not account.password and account.username in existing_by_username:
            account = account.model_copy(update={"password": existing_by_username[account.username].password})
        merged.append(account)
    return merged


@app.post("/api/credentials")
def save_credentials(data: CredentialsSchema):
    try:
        # Only the sections the form sends are replaced; the stored
        # Subsurface / Submersion entries survive a Garmin/Divelogs save.
        current = ConfigManager.load_credentials()
        creds = current.model_copy(update={
            "garmin": _merge_passwords(data.garmin_accounts, current.get_garmin_accounts()),
            "divelogs": _merge_passwords(data.divelogs_accounts, current.get_divelogs_accounts()),
        })
        if data.subsurface is not None:
            creds = creds.model_copy(update={"subsurface": data.subsurface})
        if data.submersion is not None:
            creds = creds.model_copy(update={"submersion": data.submersion})
        ConfigManager.save_credentials(creds)
        logger.info("Credentials updated via status page.")
        return {"status": "success", "message": "Credentials saved successfully."}
    except Exception as e:
        logger.error("Failed to save credentials: %s", e)
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/credentials/test")
def test_credentials(data: CredentialsSchema):
    """Per-account results (rework.md A8): only rows with both a username
    and a password can actually be tested - a row kept via the blank-password
    merge in save_credentials has no password here to test with."""
    results = {"garmin": None, "divelogs": None}

    garmin_rows = [a for a in data.garmin_accounts if a.username and a.password]
    if garmin_rows:
        from src.core.services.garmin import GarminAdapter
        garmin_results = []
        for account in garmin_rows:
            try:
                adapter = GarminAdapter(
                    username=account.username,
                    password=account.password,
                    token_dir=account.token_dir,
                    cooldown_seconds=1.0
                )
                ok = adapter.login()
            except Exception as e:
                logger.error("Garmin credential test failed for %s: %s", account.username, e)
                ok = False
            garmin_results.append({"username": account.username, "ok": ok})
        results["garmin"] = garmin_results

    divelogs_rows = [a for a in data.divelogs_accounts if a.username and a.password]
    if divelogs_rows:
        from src.core.services.divelogs import DivelogsAdapter
        divelogs_results = []
        for account in divelogs_rows:
            try:
                adapter = DivelogsAdapter(
                    username=account.username,
                    password=account.password,
                    cooldown_seconds=1.0
                )
                ok = adapter.login()
            except Exception as e:
                logger.error("Divelogs credential test failed for %s: %s", account.username, e)
                ok = False
            divelogs_results.append({"username": account.username, "ok": ok})
        results["divelogs"] = divelogs_results

    if data.subsurface is not None and data.subsurface.configured:
        from src.core.services.subsurface_cloud import check_cloud_login
        ok, message = check_cloud_login(data.subsurface.email, data.subsurface.password, data.subsurface.base_url)
        results["subsurface"] = ok
        results["subsurface_message"] = message

    if data.submersion is not None and data.submersion.configured:
        from src.core.services.submersion.store import check_store_access
        ok, message = check_store_access(data.submersion)
        if ok:
            # Connectivity is fine; also try to unlock an end-to-end
            # encrypted library so a wrong/missing passphrase surfaces here
            # rather than only on the next real sync.
            import tempfile
            from src.core.services.submersion.adapter import SubmersionAdapter
            with tempfile.TemporaryDirectory() as scratch:
                adapter = SubmersionAdapter(data.submersion, device_state_dir=scratch)
                try:
                    enc_ok, enc_message = adapter._resolve_encryption()
                except Exception as e:
                    enc_ok, enc_message = False, str(e)
                if not enc_ok:
                    ok, message = False, enc_message
                elif enc_message:
                    message = f"{message} {enc_message}."
        results["submersion"] = ok
        results["submersion_message"] = message

    return results

# ---------------------------------------------------------------------------
# Phase 6: mapping board support (Test mapping, conflicts, profiles, full compare)
# ---------------------------------------------------------------------------

def _boards():
    from src.core.pairs import board_pairs
    return board_pairs(ConfigManager.load_settings(), ConfigManager.load_credentials().configured_services())


def _engine_for_pair_id(pair_id: Optional[str]):
    """The engine for a board: a saved pair or a combination of configured
    services the Mapping page offers; None / "default" = garmin_divelogs."""
    from src.core.pairs import engine_for_board
    if not pair_id or pair_id == "default":
        pair_id = DEFAULT_PAIR_ID
    board = next((b for b in _boards() if b["id"] == pair_id), None)
    if board is None:
        raise ValueError(f"No sync pair named {pair_id!r}")
    return engine_for_board(ConfigManager.load_settings(), board)


class MappingTestRequest(BaseModel):
    pair: Optional[str] = None
    field_links: Optional[List[FieldLink]] = None          # a candidate board as links (version-1 clients)
    rules: Optional[Dict[str, List[SyncRule]]] = None       # ... or as receiver rules (rework.md G5)
    match_keys: Optional[List[List[str]]] = None
    limit: int = 10


@app.post("/api/mapping/test")
def test_mapping(data: MappingTestRequest):
    """Read-only rehearsal of a (possibly unsaved) board on the newest dives
    of both services. Fetches live, so it takes a while and cannot run next
    to a sync."""
    if scheduler.is_sync_running:
        raise HTTPException(status_code=409, detail="A synchronization run is in progress; try again when it has finished.")
    try:
        engine = _engine_for_pair_id(data.pair)
        return engine.test_mapping(data.field_links, limit=max(1, min(data.limit, 50)),
                                   rules=data.rules, match_keys=data.match_keys)
    except Exception as e:
        logger.error("Test mapping failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/conflicts")
def list_conflicts(pair: Optional[str] = None):
    try:
        engine = _engine_for_pair_id(pair)
        return {"pair": pair or "default", "source": engine.source_id, "target": engine.target_id,
                "conflicts": [c.model_dump(mode="json") for c in engine.list_conflicts()]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/conflicts/all")
def list_all_conflicts():
    """Every board's waiting conflicts, grouped by pair, with field labels and
    readable values (the Conflicts page)."""
    import os
    from src.core import config
    from src.core.conflicts import ConflictStore, display_value
    from src.core.pairs import conflicts_file_for, display_name_of, field_catalog_of, service_id_of

    def label(key: str) -> str:
        try:
            return next((f.label for f in field_catalog_of(key.split(".")[0]) if f.key == key), key)
        except ValueError:
            return key

    groups = []
    for board in _boards():
        items = ConflictStore(conflicts_file_for(board, os.path.dirname(config.SETTINGS_FILE))).load()
        if not items:
            continue
        names = {service_id_of(board["source"]): display_name_of(board["source"]),
                 service_id_of(board["target"]): display_name_of(board["target"])}
        groups.append({
            "pair_id": board["id"],
            "label": f"{display_name_of(board['source'])} ↔ {display_name_of(board['target'])}",
            "conflicts": [dict(c.model_dump(mode="json"), pair_id=board["id"],
                               source_name=names.get(c.source_service, c.source_service),
                               target_name=names.get(c.target_service, c.target_service),
                               field_label=label(c.target_key),
                               source_text=display_value(c.source_value),
                               target_text=display_value(c.target_value)) for c in items],
        })
    return {"groups": groups}


@app.get("/api/sync/endpoints")
def sync_endpoints():
    """What the Source and Target pickers offer (Sync now, scheduled jobs)."""
    from src.core.pairs import sync_endpoints as endpoints
    settings = ConfigManager.load_settings()
    return {"endpoints": endpoints(settings, ConfigManager.load_credentials().configured_services()),
            "boards": _boards()}


class ResolveRequest(BaseModel):
    winner: str
    pair: Optional[str] = None


@app.post("/api/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: str, data: ResolveRequest):
    if scheduler.is_sync_running:
        raise HTTPException(status_code=409, detail="A synchronization run is in progress; try again when it has finished.")
    try:
        engine = _engine_for_pair_id(data.pair)
        resolved = engine.resolve_conflict(conflict_id, data.winner)
        return {"status": "success", "resolved": resolved.model_dump(mode="json")}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Conflict resolution failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/sync/full-compare")
def request_full_compare(pair: Optional[str] = None):
    """Make the next run of this pair compare every matched dive (the
    'apply the changed board to all dives' prompt)."""
    try:
        engine = _engine_for_pair_id(pair)
        engine.request_full_compare(True)
        return {"status": "success", "message": "The next run will compare every matched dive."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/settings/export")
def export_settings_profile():
    profile = export_profile(ConfigManager.load_settings())
    return JSONResponse(profile, headers={"Content-Disposition": 'attachment; filename="dive_sync_profile.json"'})


@app.post("/api/settings/import")
async def import_settings_profile(file: UploadFile = File(...), apply: bool = False):
    """Upload a profile. Without ``apply`` only the diff summary comes back;
    with ``apply=true`` the listed sections replace the current settings."""
    try:
        data = json.loads((await file.read()).decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"The file is not valid JSON: {e}")
    current = ConfigManager.load_settings()
    try:
        new_settings, summary = import_profile(data, current, _pair_catalog())
    except ProfileError as e:
        raise HTTPException(status_code=400, detail=str(e))
    problems = validate_links(new_settings.field_links, _pair_catalog())
    if problems:
        raise HTTPException(status_code=400, detail={"message": "The profile's field links are invalid.", "errors": problems})
    if apply:
        ConfigManager.save_settings(new_settings)
        logger.info("Settings replaced from an uploaded profile (%s).", ", ".join(summary.sections))
    return {"applied": apply, "summary": summary.model_dump()}


@app.post("/api/sync/trigger")
def trigger_sync(request: Optional[SyncTriggerRequest] = None):
    if scheduler.is_sync_running:
        raise HTTPException(status_code=409, detail="A synchronization run is already in progress.")

    dry_run = False
    custom_settings = None
    if request:
        dry_run = request.dry_run
        custom_settings = request.model_dump(exclude_none=True)
        if request.source and request.target:
            # the two sides of the Sync page: the pair between them, either way
            # round, writing the target. Sync now never deletes (the desktop
            # app's Mirror is the one way to delete from a manual run).
            from src.core.pairs import board_between, service_id_of
            if request.source == request.target:
                raise HTTPException(status_code=400, detail="Pick two different services to sync.")
            board = board_between(_boards(), request.source, request.target)
            if board and board["saved"]:
                custom_settings.pop("source"), custom_settings.pop("target")
                custom_settings["pair"] = board["id"]
            custom_settings["directionality"] = f"to_{service_id_of(request.target)}"
            custom_settings["propagate_deletes"] = False

    threading.Thread(target=scheduler.run_sync_thread, args=(dry_run, custom_settings), daemon=True).start()
    return {"status": "success", "message": "Sync job triggered in background."}

# ---- Garmin dive list + FIT files --------------------------------------------

_GARMIN_ROW_KEYS = ("filename", "account", "id", "dive_number", "date", "time", "location",
                    "max_depth", "duration", "fit", "fit_file", "manual")


class FitDownloadRequest(BaseModel):
    # Cached JSON names of the dives to fetch; None = every dive without a FIT yet
    filenames: Optional[List[str]] = None


def _require_idle():
    if scheduler.is_sync_running or scheduler.is_download_running:
        raise HTTPException(status_code=409, detail="A sync or download is already running.")


@app.get("/api/dives/garmin")
def list_garmin_dives():
    """The locally cached Garmin dives (what the last refresh fetched), each
    with whether its original .fit has been downloaded."""
    from src.core import dive_cache
    rows = dive_cache.list_dives("garmin")
    return {"dives": [{k: row.get(k) for k in _GARMIN_ROW_KEYS} for row in rows]}


@app.post("/api/dives/garmin/refresh")
def refresh_garmin_dives():
    _require_idle()
    threading.Thread(target=scheduler.run_download_thread, kwargs={"services": ["garmin"]}, daemon=True).start()
    return {"status": "started"}


@app.post("/api/dives/garmin/fit")
def download_garmin_fits(request: Optional[FitDownloadRequest] = None):
    _require_idle()
    from src.core import dive_cache
    filenames = request.filenames if request else None
    if filenames is None:
        filenames = [r["filename"] for r in dive_cache.list_dives("garmin") if not r.get("fit")]
    if not filenames:
        return {"status": "nothing_to_do", "count": 0}
    threading.Thread(target=scheduler.run_fit_download_thread, args=(filenames,), daemon=True).start()
    return {"status": "started", "count": len(filenames)}


@app.get("/api/dives/garmin/fit/{activity_id}")
def get_garmin_fit(activity_id: str, account: Optional[str] = None):
    """Hands a downloaded .fit to the browser, e.g. to import in Submersion."""
    from src.core import garmin_files
    if not activity_id.isdigit() or (account and (account != os.path.basename(account) or account.startswith("."))):
        raise HTTPException(status_code=400, detail="Invalid activity id or account.")
    path = garmin_files.find_fit(activity_id, account or None)
    if not path:
        raise HTTPException(status_code=404, detail="No FIT downloaded for this dive.")
    return FileResponse(path, media_type="application/octet-stream", filename=os.path.basename(path))


@app.get("/api/status")
def get_status():
    settings = ConfigManager.load_settings()
    from src.core import progress
    return {
        "is_running": scheduler.is_sync_running,
        "is_downloading": scheduler.is_download_running,
        # How far a long job has got (rework.md E14); None when nothing runs
        "progress": progress.current(),
        "last_results": scheduler.last_sync_results,
        "next_scheduled_run": scheduler.get_next_scheduled_run(settings)
    }

class NotifyTestRequest(BaseModel):
    notify_url: str


@app.post("/api/notify/test")
def test_notify(data: NotifyTestRequest):
    """Send a real test alert to the given URL, without saving it (rework.md
    A11) - lets the status page's Test button check a URL before Save."""
    from src.core.notify import send_notification
    ok, detail = send_notification(data.notify_url, "Dive Sync", "This is a test alert from Dive Sync.")
    return {"ok": ok, "detail": detail}


@app.get("/api/logs/stream")
def stream_logs():
    async def log_generator():
        # Clear old items in queue before streaming
        while not sse_log_queue.empty():
            try:
                sse_log_queue.get_nowait()
            except Exception:
                break

        # Start streaming
        logger.info("Status page client connected to log stream.")
        while True:
            try:
                # Poll queue
                if not sse_log_queue.empty():
                    line = sse_log_queue.get()
                    yield f"data: {line}\n\n"
                else:
                    await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                logger.info("Status page client disconnected from log stream.")
                break
            except Exception as e:
                yield f"data: Error: {str(e)}\n\n"
                break

    return StreamingResponse(log_generator(), media_type="text/event-stream")

@app.get("/api/version")
def get_app_version():
    from src.core.version import get_version_info
    return get_version_info()


@app.get("/api/about")
def get_about():
    """Version, license, project links and component versions (the About page)."""
    from src.core.about import about_info
    return about_info()

# Serve index.html statically
@app.get("/")
def read_root():
    return FileResponse("src/web/static/index.html")

# Mount other static assets
app.mount("/static", StaticFiles(directory="src/web/static"), name="static")
