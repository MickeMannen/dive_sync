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

def get_cached_dives() -> Dict[str, List[Dict[str, Any]]]:
    garmin_dives = []
    divelogs_dives = []
    
    # Check paths
    base_dirs = ["./tests/real", "./tests"]
    
    # 1. Garmin
    garmin_dir = None
    for d in base_dirs:
        path = os.path.join(d, "garmin")
        if os.path.exists(path) and os.listdir(path):
            garmin_dir = path
            break
            
    if garmin_dir:
        import json
        for filename in os.listdir(garmin_dir):
            if filename.endswith(".json") and filename != "sync_state.json":
                filepath = os.path.join(garmin_dir, filename)
                try:
                    with open(filepath, "r") as f:
                        data = json.load(f)
                    
                    summary = data.get("summary", {})
                    details = data.get("details") or {}
                    if not isinstance(details, dict):
                        details = {}
                    
                    # Parse essential information
                    sum_dto = details.get("summaryDTO", {}) or summary.get("summaryDTO", {}) or {}
                    metadata = details.get("metadataDTO", {}) or summary.get("metadataDTO", {}) or {}
                    
                    dive_num = metadata.get("diveNumber") or filename.replace(".json", "")
                    date_time = sum_dto.get("startTimeLocal") or summary.get("startTimeLocal") or ""
                    duration = sum_dto.get("duration") or summary.get("duration") or 0
                    max_depth = sum_dto.get("maxDepth") or summary.get("maxDepth") or 0.0
                    location = details.get("activityName") or summary.get("activityName") or ""
                    notes = details.get("description") or summary.get("description") or ""
                    
                    info = details.get("diveInfo") or summary.get("diveInfo") or {}
                    weight = info.get("weight")
                    weight_unit = info.get("weightUnit", {}).get("unitKey") if isinstance(info.get("weightUnit"), dict) else ""
                    visibility = info.get("visibility")
                    visibility_unit = info.get("visibilityUnit", {}).get("unitKey") if isinstance(info.get("visibilityUnit"), dict) else ""
                    
                    weight_str = ""
                    if weight is not None:
                        w_unit = "kg" if "kilogram" in str(weight_unit).lower() else "lbs"
                        weight_str = f"{weight:g} {w_unit}"
                        
                    visibility_str = ""
                    if visibility is not None:
                        v_unit = "m" if "meter" in str(visibility_unit).lower() else "ft"
                        visibility_str = f"{visibility:g} {v_unit}"

                    garmin_dives.append({
                        "id": str(summary.get("activityId") or ""),
                        "dive_number": dive_num,
                        "date_time": date_time,
                        "duration": duration,
                        "max_depth": max_depth,
                        "location": location,
                        "notes": notes,
                        "weight": weight_str,
                        "visibility": visibility_str,
                        "filename": filename
                    })
                except Exception as e:
                    logger.warning("Failed to parse cached Garmin dive file %s: %s", filename, e)
                    
    # 2. Divelogs
    divelogs_dir = None
    for d in base_dirs:
        path = os.path.join(d, "divelogs")
        if os.path.exists(path) and os.listdir(path):
            divelogs_dir = path
            break
            
    if divelogs_dir:
        import json
        for filename in os.listdir(divelogs_dir):
            if filename.endswith(".json") and filename != "sync_state.json":
                filepath = os.path.join(divelogs_dir, filename)
                try:
                    with open(filepath, "r") as f:
                        data = json.load(f)
                    
                    dive_num = data.get("divenumber") or filename.replace(".json", "")
                    date = data.get("date") or ""
                    time = data.get("time") or "00:00:00"
                    
                    garmin_id = data.get("garmin_id") or ""
                    
                    # Essential information
                    location_parts = []
                    if data.get("location"):
                        location_parts.append(str(data["location"]))
                    if data.get("divesite"):
                        location_parts.append(str(data["divesite"]))
                    location = ", ".join(location_parts) if location_parts else ""
                    
                    weights_val = data.get("weights")
                    weight_str = ""
                    if weights_val not in [None, "", 0]:
                        try:
                            w_num = float(weights_val)
                            weight_str = f"{w_num:g}"
                        except (ValueError, TypeError):
                            weight_str = str(weights_val)
                            
                    visibility_str = str(data.get("visibility") or "")

                    divelogs_dives.append({
                        "id": str(data.get("id") or ""),
                        "dive_number": dive_num,
                        "date_time": f"{date} {time}",
                        "duration": data.get("duration") or 0,
                        "max_depth": data.get("maxdepth") or 0.0,
                        "location": location,
                        "notes": data.get("notes") or "",
                        "garmin_id": garmin_id,
                        "weight": weight_str,
                        "visibility": visibility_str,
                        "filename": filename
                    })
                except Exception as e:
                    logger.warning("Failed to parse cached Divelogs dive file %s: %s", filename, e)
                    
    # Sort dives by date descending
    garmin_dives.sort(key=lambda x: x["date_time"], reverse=True)
    divelogs_dives.sort(key=lambda x: x["date_time"], reverse=True)
    
    return {
        "garmin": garmin_dives,
        "divelogs": divelogs_dives
    }

@app.get("/api/dives")
def get_dives():
    return get_cached_dives()

# Serve index.html statically
@app.get("/")
def read_root():
    return FileResponse("src/web/static/index.html")

# Mount other static assets
app.mount("/static", StaticFiles(directory="src/web/static"), name="static")
