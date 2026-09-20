import queue
import logging
import asyncio
import threading
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.core.config import ConfigManager, SettingsModel, SyncFilters, SyncScheduleSlot, GarminCredentials, DivelogsCredentials, CredentialsModel, CronJobModel
from src.core.fields import FieldLink, build_catalog, validate_field_links
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

class SettingsSchema(BaseModel):
    directionality: str
    sync_filters: SyncFiltersSchema
    grace_window_minutes: int
    api_cooldown_seconds: float
    schedule: List[Dict[str, int]]
    cron_jobs: List[CronJobSchema] = []
    # Omitted (None) keeps the board currently on disk, so a settings form
    # that does not know about links cannot wipe them.
    field_links: Optional[List[FieldLink]] = None

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
            )
            for job in data.cron_jobs
        ]
        current = ConfigManager.load_settings()
        field_links = data.field_links if data.field_links is not None else current.field_links

        catalog = _pair_catalog()
        problems = validate_field_links(field_links, catalog)
        for job in cron_jobs:
            if job.field_links:
                problems.extend(f"Job '{job.id}': {p}" for p in validate_field_links(job.field_links, catalog))
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
        )
        ConfigManager.save_settings(settings)
        logger.info("Schedule configuration updated successfully.")
        return {"status": "success", "message": "Settings updated."}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to update settings: %s", e)
        raise HTTPException(status_code=400, detail=str(e))


# The only pair this build syncs. Track F turns this into a list of pairs.
SYNC_PAIRS = [(GarminAdapter, DivelogsAdapter)]


def _pair_catalog(source=GarminAdapter, target=DivelogsAdapter):
    return build_catalog(source.field_catalog(), target.field_catalog())


@app.get("/api/fields")
def get_fields():
    """Field catalogues per sync pair, for the mapping board. Needs no login."""
    pairs = []
    for source, target in SYNC_PAIRS:
        pairs.append({
            "source": source.service_id,
            "target": target.service_id,
            "source_name": source.display_name,
            "target_name": target.display_name,
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
        "divelogs_accounts": divelogs_users
    }


@app.post("/api/credentials")
def save_credentials(data: CredentialsSchema):
    try:
        creds = CredentialsModel(
            garmin=GarminCredentials(
                username=data.garmin_username,
                password=data.garmin_password,
                token_dir=data.garmin_token_dir
            ),
            divelogs=DivelogsCredentials(
                username=data.divelogs_username,
                password=data.divelogs_password
            )
        )
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

    return results

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
