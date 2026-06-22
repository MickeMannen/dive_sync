import os
import queue
import logging
import asyncio
import threading
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Dict, Any, List

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.core.config import ConfigManager, SettingsModel, SyncFilters, SyncScheduleSlot
from src.core.sync_engine import SyncEngine

# Configure logger
logger = logging.getLogger("anti_gravity.web")
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
logging.getLogger("anti_gravity").addHandler(sse_handler)

is_sync_running = False
last_sync_results: Dict[str, Any] = {}
scheduler_task: Optional[asyncio.Task] = None

def run_sync_thread(dry_run: bool):
    global is_sync_running, last_sync_results
    is_sync_running = True
    logger.info("Manual synchronization started (Dry Run: %s)", dry_run)
    try:
        engine = SyncEngine()
        results = engine.run_sync(dry_run=dry_run)
        last_sync_results = results
        logger.info("Manual synchronization completed successfully.")
    except Exception as e:
        logger.error("Sync run encountered an error: %s", e)
        last_sync_results = {"error": str(e)}
    finally:
        is_sync_running = False

async def scheduler_loop():
    global is_sync_running
    logger.info("Background schedule watcher started.")
    last_checked_minute = None
    
    while True:
        try:
            now = datetime.now()
            current_minute = (now.hour, now.minute)
            
            if current_minute != last_checked_minute:
                settings = ConfigManager.load_settings()
                for slot in settings.schedule:
                    if slot.hour == now.hour and slot.minute == now.minute:
                        logger.info("Scheduled synchronization slot triggered for %02d:%02d", slot.hour, slot.minute)
                        if not is_sync_running:
                            threading.Thread(target=run_sync_thread, args=(False,), daemon=True).start()
                        else:
                            logger.warning("Scheduled sync skipped: another synchronization is currently running.")
                last_checked_minute = current_minute
                
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            logger.info("Background schedule watcher stopped.")
            break
        except Exception as e:
            logger.error("Error in scheduler loop: %s", e)
            await asyncio.sleep(30)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Start background scheduler
    global scheduler_task
    scheduler_task = asyncio.create_task(scheduler_loop())
    yield
    # Shutdown: Stop background scheduler
    if scheduler_task:
        scheduler_task.cancel()
        await asyncio.gather(scheduler_task, return_exceptions=True)

app = FastAPI(
    title="Anti-Gravity Sync Dashboard", 
    description="Anti-Gravity Web Service Synchronization Dashboard.",
    lifespan=lifespan
)

# API Schemas for Web Request
class SyncFiltersSchema(BaseModel):
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    only_new: bool = True
    sync_gases: bool = True
    sync_fit: bool = False

class SettingsSchema(BaseModel):
    directionality: str
    sync_filters: SyncFiltersSchema
    grace_window_minutes: int
    api_cooldown_seconds: float
    schedule: List[Dict[str, int]]

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
            schedule=schedule_slots
        )
        ConfigManager.save_settings(settings)
        logger.info("Configuration updated successfully.")
        return {"status": "success", "message": "Settings updated."}
    except Exception as e:
        logger.error("Failed to update settings: %s", e)
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/credentials/status")
def get_credentials_status():
    creds = ConfigManager.load_credentials()
    return {
        "garmin_configured": bool(creds.garmin.username and creds.garmin.password),
        "divelogs_configured": bool(creds.divelogs.username and creds.divelogs.password)
    }

@app.post("/api/sync/trigger")
def trigger_sync(dry_run: bool = Query(False)):
    global is_sync_running
    if is_sync_running:
        raise HTTPException(status_code=409, detail="A synchronization run is already in progress.")
        
    threading.Thread(target=run_sync_thread, args=(dry_run,), daemon=True).start()
    return {"status": "success", "message": "Sync job triggered in background."}

@app.get("/api/sync/status")
def get_sync_status():
    global is_sync_running, last_sync_results
    return {
        "is_running": is_sync_running,
        "last_results": last_sync_results
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
        logger.info("Web dashboard client connected to log stream.")
        while True:
            try:
                # Poll queue
                if not sse_log_queue.empty():
                    line = sse_log_queue.get()
                    yield f"data: {line}\n\n"
                else:
                    await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                logger.info("Web dashboard client disconnected from log stream.")
                break
            except Exception as e:
                yield f"data: Error: {str(e)}\n\n"
                break
                
    return StreamingResponse(log_generator(), media_type="text/event-stream")

# Serve index.html statically
@app.get("/")
def read_root():
    return FileResponse("src/web/static/index.html")

# Mount other static assets
app.mount("/static", StaticFiles(directory="src/web/static"), name="static")
