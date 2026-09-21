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
from pydantic import BaseModel

from src.core.config import ConfigManager, SettingsModel, SyncFilters, SyncScheduleSlot, GarminCredentials, DivelogsCredentials, SubsurfaceCredentials, SubmersionCredentials, CredentialsModel, CronJobModel, SyncPairModel
from src.core.fields import FieldLink, build_catalog
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
    sync_fit: bool = False

class CronJobSchema(BaseModel):
    id: str
    directionality: str
    frequency: str
    hour: int
    minute: int
    day_of_week: int
    interval_minutes: int
    only_new: bool
    sync_gases: bool
    sync_fit: bool
    enabled: bool
    field_links: Optional[List[FieldLink]] = None
    pair: Optional[str] = None

class SettingsSchema(BaseModel):
    directionality: str
    sync_filters: SyncFiltersSchema
    grace_window_minutes: int
    api_cooldown_seconds: float
    schedule: List[Dict[str, int]]
    cron_jobs: List[CronJobSchema] = []
    # Omitted (None) keeps the board / pairs currently on disk, so a settings
    # form that does not know about them cannot wipe them.
    field_links: Optional[List[FieldLink]] = None
    sync_pairs: Optional[List[SyncPairModel]] = None

class SyncTriggerRequest(BaseModel):
    dry_run: bool = False
    directionality: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    only_new: Optional[bool] = None
    sync_gases: Optional[bool] = None
    sync_fit: Optional[bool] = None

class CredentialsSchema(BaseModel):
    garmin_username: str = ""
    garmin_password: str = ""
    garmin_token_dir: str = "tokens/garmin"
    divelogs_username: str = ""
    divelogs_password: str = ""
    # Optional sections; omitted (None) means "leave what is stored".
    subsurface: Optional[SubsurfaceCredentials] = None
    submersion: Optional[SubmersionCredentials] = None

@app.get("/api/settings")
def get_settings():
    settings = ConfigManager.load_settings()
    return settings.model_dump()

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
                directionality=job.directionality,
                frequency=job.frequency,
                hour=job.hour,
                minute=job.minute,
                day_of_week=job.day_of_week,
                interval_minutes=job.interval_minutes,
                only_new=job.only_new,
                sync_gases=job.sync_gases,
                sync_fit=job.sync_fit,
                enabled=job.enabled,
                field_links=job.field_links,
                pair=job.pair,
            )
            for job in data.cron_jobs
        ]
        current = ConfigManager.load_settings()
        field_links = data.field_links if data.field_links is not None else current.field_links

        catalog = _pair_catalog()
        problems = validate_links(field_links, catalog)
        for job in cron_jobs:
            if job.field_links:
                problems.extend(f"Job '{job.id}': {p}" for p in validate_links(job.field_links, catalog))
        if problems:
            raise HTTPException(status_code=400, detail={"message": "Field links are invalid.", "errors": problems})

        settings = SettingsModel(
            directionality=data.directionality,
            sync_filters=SyncFilters(
                date_from=data.sync_filters.date_from,
                date_to=data.sync_filters.date_to,
                only_new=data.sync_filters.only_new,
                sync_gases=data.sync_filters.sync_gases,
                sync_fit=data.sync_filters.sync_fit
            ),
            grace_window_minutes=data.grace_window_minutes,
            api_cooldown_seconds=data.api_cooldown_seconds,
            schedule=schedule_slots,
            cron_jobs=cron_jobs,
            field_links=field_links,
            sync_pairs=data.sync_pairs if data.sync_pairs is not None else current.sync_pairs,
            garmin_timezone=current.garmin_timezone,
        )
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
    """Field catalogues per sync pair (the implicit Garmin -> Divelogs pair
    plus every configured one), for the mapping board. Needs no login."""
    from src.core.pairs import default_links_for, parse_service_spec
    settings = ConfigManager.load_settings()
    specs = [("default", "garmin", "divelogs")] + [(p.id, p.source, p.target) for p in settings.sync_pairs]
    pairs = []
    for pair_id, source_spec, target_spec in specs:
        try:
            source = _adapter_class(parse_service_spec(source_spec)[0])
            target = _adapter_class(parse_service_spec(target_spec)[0])
        except (ValueError, KeyError) as e:
            logger.warning("Skipping sync pair %s: %s", pair_id, e)
            continue
        pairs.append({
            "id": pair_id,
            "source": source.service_id,
            "target": target.service_id,
            "source_name": source.display_name,
            "target_name": target.display_name,
            "default_links": [l.model_dump() for l in default_links_for(source.service_id, target.service_id)],
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
        "subsurface_configured": creds.subsurface.configured,
        "subsurface_email": creds.subsurface.email,
        "submersion_configured": creds.submersion.configured,
        "submersion_store": {
            "store_type": creds.submersion.store_type,
            "endpoint_url": creds.submersion.endpoint_url,
            "region": creds.submersion.region,
            "bucket": creds.submersion.bucket,
            "prefix": creds.submersion.prefix,
            "path_style": creds.submersion.path_style,
            "folder_path": creds.submersion.folder_path,
        },
    }


@app.post("/api/credentials")
def save_credentials(data: CredentialsSchema):
    try:
        # Only the sections the form sends are replaced; the stored
        # Subsurface / Submersion entries survive a Garmin/Divelogs save.
        current = ConfigManager.load_credentials()
        creds = current.model_copy(update={
            "garmin": GarminCredentials(
                username=data.garmin_username,
                password=data.garmin_password,
                token_dir=data.garmin_token_dir
            ),
            "divelogs": DivelogsCredentials(
                username=data.divelogs_username,
                password=data.divelogs_password
            ),
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
    results = {"garmin": None, "divelogs": None}

    if data.garmin_username and data.garmin_password:
        try:
            from src.core.services.garmin import GarminAdapter
            adapter = GarminAdapter(
                username=data.garmin_username,
                password=data.garmin_password,
                token_dir=data.garmin_token_dir,
                cooldown_seconds=1.0
            )
            results["garmin"] = adapter.login()
        except Exception as e:
            logger.error("Garmin credential test failed: %s", e)
            results["garmin"] = False

    if data.divelogs_username and data.divelogs_password:
        try:
            from src.core.services.divelogs import DivelogsAdapter
            adapter = DivelogsAdapter(
                username=data.divelogs_username,
                password=data.divelogs_password,
                cooldown_seconds=1.0
            )
            results["divelogs"] = adapter.login()
        except Exception as e:
            logger.error("Divelogs credential test failed: %s", e)
            results["divelogs"] = False

    if data.subsurface is not None and data.subsurface.configured:
        from src.core.services.subsurface_cloud import check_cloud_login
        ok, message = check_cloud_login(data.subsurface.email, data.subsurface.password, data.subsurface.base_url)
        results["subsurface"] = ok
        results["subsurface_message"] = message

    if data.submersion is not None and data.submersion.configured:
        from src.core.services.submersion.store import check_store_access
        ok, message = check_store_access(data.submersion)
        results["submersion"] = ok
        results["submersion_message"] = message

    return results

# ---------------------------------------------------------------------------
# Phase 6: mapping board support (Test mapping, conflicts, profiles, full compare)
# ---------------------------------------------------------------------------

def _engine_for_pair_id(pair_id: Optional[str]):
    """The engine for the implicit Garmin -> Divelogs pair or a configured one."""
    from src.core.pairs import engine_for_pair, find_pair
    from src.core.sync_engine import SyncEngine
    if not pair_id or pair_id == "default":
        engine = SyncEngine()
        engine.run_overrides = {}
        return engine
    return engine_for_pair(find_pair(ConfigManager.load_settings(), pair_id))


class MappingTestRequest(BaseModel):
    pair: Optional[str] = None
    field_links: Optional[List[FieldLink]] = None
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
        links = data.field_links
        if links is None and engine.run_overrides.get("field_links_override") is not None:
            links = engine.run_overrides["field_links_override"]
        return engine.test_mapping(links, limit=max(1, min(data.limit, 50)))
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

    threading.Thread(target=scheduler.run_sync_thread, args=(dry_run, custom_settings), daemon=True).start()
    return {"status": "success", "message": "Sync job triggered in background."}

@app.get("/api/status")
def get_status():
    settings = ConfigManager.load_settings()
    return {
        "is_running": scheduler.is_sync_running,
        "last_results": scheduler.last_sync_results,
        "next_scheduled_run": scheduler.get_next_scheduled_run(settings)
    }

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

# Serve index.html statically
@app.get("/")
def read_root():
    return FileResponse("src/web/static/index.html")

# Mount other static assets
app.mount("/static", StaticFiles(directory="src/web/static"), name="static")
