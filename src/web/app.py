import os
import queue
import logging
import asyncio
import threading
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.core.config import ConfigManager, SettingsModel, SyncFilters, SyncScheduleSlot
from src.core.sync_engine import SyncEngine

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
    title="Dive Sync Dashboard", 
    description="Dive Sync Web Service Synchronization Dashboard.",
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

def get_cached_dives(garmin_username: Optional[str] = None, divelogs_username: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    garmin_dives = []
    divelogs_dives = []
    
    # Check paths
    base_dirs = ["./data", "./tests"]
    
    from src.core.config import ConfigManager
    try:
        creds = ConfigManager.load_credentials()
        garmin_accounts = creds.get_garmin_accounts()
        divelogs_accounts = creds.get_divelogs_accounts()
    except Exception:
        garmin_accounts = []
        divelogs_accounts = []

    if not garmin_username:
        if len(garmin_accounts) == 1:
            garmin_username = garmin_accounts[0].username
        elif len(garmin_accounts) > 1:
            garmin_username = garmin_accounts[0].username

    if not divelogs_username:
        if len(divelogs_accounts) == 1:
            divelogs_username = divelogs_accounts[0].username
        elif len(divelogs_accounts) > 1:
            divelogs_username = divelogs_accounts[0].username
    
    # 1. Garmin
    garmin_dir = None
    for d in base_dirs:
        if garmin_username:
            path_user = os.path.join(d, "garmin", garmin_username)
            if os.path.exists(path_user) and os.listdir(path_user):
                garmin_dir = path_user
                break
        path_direct = os.path.join(d, "garmin")
        if os.path.exists(path_direct) and os.listdir(path_direct):
            garmin_dir = path_direct
            break
            
    if garmin_dir:
        import json
        for filename in os.listdir(garmin_dir):
            if filename.endswith(".json") and filename != "sync_state.json" and not os.path.isdir(os.path.join(garmin_dir, filename)):
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
                    buddy = info.get("buddy") or ""
                    
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
                        "buddy": buddy,
                        "filename": filename
                    })
                except Exception as e:
                    logger.warning("Failed to parse cached Garmin dive file %s: %s", filename, e)
                    
    # 2. Divelogs
    divelogs_dir = None
    for d in base_dirs:
        if divelogs_username:
            path_user = os.path.join(d, "divelogs", divelogs_username)
            if os.path.exists(path_user) and os.listdir(path_user):
                divelogs_dir = path_user
                break
        path_direct = os.path.join(d, "divelogs")
        if os.path.exists(path_direct) and os.listdir(path_direct):
            divelogs_dir = path_direct
            break
            
    if divelogs_dir:
        import json
        for filename in os.listdir(divelogs_dir):
            if filename.endswith(".json") and filename != "sync_state.json" and not os.path.isdir(os.path.join(divelogs_dir, filename)):
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
                    buddy = data.get("buddy") or ""
 
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
                        "buddy": buddy,
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
def get_dives(garmin_user: Optional[str] = None, divelogs_user: Optional[str] = None):
    return get_cached_dives(garmin_username=garmin_user, divelogs_username=divelogs_user)
 
is_download_running = False
last_download_error = None
 
def run_download_thread(overwrite: bool, base_dir: str, garmin_user: Optional[str] = None, divelogs_user: Optional[str] = None):
    global is_download_running, last_download_error
    is_download_running = True
    last_download_error = None
    try:
        engine = SyncEngine(garmin_username=garmin_user, divelogs_username=divelogs_user)
        success = engine.download_and_save_raw_data(mock_data_dir=base_dir, overwrite=overwrite)
        if not success:
            last_download_error = "Download completed with warnings/failures."
    except Exception as e:
        last_download_error = str(e)
        logger.error("Raw data download failed: %s", e)
    finally:
        is_download_running = False
 
@app.post("/api/dives/download")
def download_raw_dives(overwrite: bool = Query(True), garmin_user: Optional[str] = None, divelogs_user: Optional[str] = None):
    global is_sync_running, is_download_running
    if is_sync_running or is_download_running:
        raise HTTPException(status_code=409, detail="A synchronization or download job is already in progress.")
        
    base_dir = "./data" if os.path.exists("./data") else "./tests"
    threading.Thread(target=run_download_thread, args=(overwrite, base_dir, garmin_user, divelogs_user), daemon=True).start()
    return {"status": "success", "message": "Raw data download started in background."}
 
@app.get("/api/dives/download/status")
def get_download_status():
    global is_download_running, last_download_error
    return {
        "is_running": is_download_running,
        "error": last_download_error
    }
 
@app.get("/api/dives/raw")
def get_raw_dive(service: str, filename: str, username: Optional[str] = None):
    import json
    base_dirs = ["./data", "./tests"]
    found_dir = None
    for d in base_dirs:
        if username:
            path = os.path.join(d, service, username)
            if os.path.exists(path):
                found_dir = path
                break
        path = os.path.join(d, service)
        if os.path.exists(path):
            found_dir = path
            break
            
    if not found_dir:
        raise HTTPException(status_code=404, detail="Service directory not found.")
        
    filepath = os.path.join(found_dir, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Dive file not found.")
        
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to read raw dive file %s: %s", filename, e)
        raise HTTPException(status_code=500, detail=str(e))
 
def push_garmin_update_background(filename: str, filepath: str):
    import json
    import time
    try:
        from src.core.config import ConfigManager
        creds = ConfigManager.load_credentials()
        
        # Determine username from filepath (if nested)
        username = None
        parts = filepath.replace("\\", "/").split("/")
        if len(parts) >= 3 and parts[-3] == "garmin":
            username = parts[-2]
            
        garmin_accounts = creds.get_garmin_accounts()
        if not username:
            if len(garmin_accounts) == 1:
                username = garmin_accounts[0].username
            else:
                logger.error("Multiple Garmin accounts configured but username could not be determined from path: %s", filepath)
                return
                
        matching = [a for a in garmin_accounts if a.username == username]
        if not matching:
            logger.error("No Garmin credentials found for username: %s", username)
            return
        active_creds = matching[0]
        
        if not active_creds.username or not active_creds.password:
            logger.warning("Garmin Connect credentials not found, skipping background remote update.")
            return
            
        with open(filepath, "r") as f:
            dive_data = json.load(f)
            
        summary = dive_data.get("summary", {})
        details = dive_data.get("details") or {}
        
        activity_id = summary.get("activityId")
        if not activity_id:
            logger.error("No Garmin activityId found in cache for %s; cannot update remotely.", filename)
            return
            
        from src.core.services.garmin import GarminAdapter
        adapter = GarminAdapter(active_creds.username, active_creds.password, token_dir=active_creds.token_dir)
        
        # Map raw cached dict to UnifiedDive
        unified_dive = adapter._map_to_unified(summary, details)
        
        logger.info("Background thread updating Garmin Connect for Activity ID %s...", activity_id)
        success = adapter.update_dive(str(activity_id), unified_dive)
        if success:
            logger.info("Garmin Connect successfully updated in background for Activity ID %s.", activity_id)
        else:
            logger.error("Garmin Connect background update failed for Activity ID %s.", activity_id)
    except Exception as e:
        logger.error("Error in background Garmin remote update for file %s: %s", filename, e)
 
def push_divelogs_update_background(filename: str, filepath: str):
    import json
    import time
    try:
        from src.core.config import ConfigManager
        creds = ConfigManager.load_credentials()
        
        # Determine username from filepath (if nested)
        username = None
        parts = filepath.replace("\\", "/").split("/")
        if len(parts) >= 3 and parts[-3] == "divelogs":
            username = parts[-2]
            
        divelogs_accounts = creds.get_divelogs_accounts()
        if not username:
            if len(divelogs_accounts) == 1:
                username = divelogs_accounts[0].username
            else:
                logger.error("Multiple Divelogs accounts configured but username could not be determined from path: %s", filepath)
                return
                
        matching = [a for a in divelogs_accounts if a.username == username]
        if not matching:
            logger.error("No Divelogs credentials found for username: %s", username)
            return
        active_creds = matching[0]
        
        if not active_creds.username or not active_creds.password:
            logger.warning("Divelogs.org credentials not found, skipping background remote update.")
            return
            
        with open(filepath, "r") as f:
            dive_data = json.load(f)
            
        dive_id = dive_data.get("id")
        if not dive_id:
            logger.error("No Divelogs ID found in cache for %s; cannot update remotely.", filename)
            return
            
        from src.core.services.divelogs import DivelogsAdapter
        adapter = DivelogsAdapter(active_creds.username, active_creds.password)
        
        # Map raw cached dict to UnifiedDive
        unified_dive = adapter._map_to_unified(dive_data)
        
        logger.info("Background thread updating Divelogs.org for Dive ID %s...", dive_id)
        success = adapter.update_dive(str(dive_id), unified_dive)
        if success:
            logger.info("Divelogs.org successfully updated in background for Dive ID %s.", dive_id)
        else:
            logger.error("Divelogs.org background update failed for Dive ID %s.", dive_id)
    except Exception as e:
        logger.error("Error in background Divelogs remote update for file %s: %s", filename, e)
 
class UpdateDiveSchema(BaseModel):
    service: str  # "garmin" or "divelogs"
    filename: str
    username: Optional[str] = None
    dive_number: Optional[str] = None
    date_time: Optional[str] = None
    duration: Optional[int] = None
    max_depth: Optional[float] = None
    location: Optional[str] = None
    notes: Optional[str] = None
    weight: Optional[str] = None
    visibility: Optional[str] = None
    buddy: Optional[str] = None
 
@app.post("/api/dives/update")
def update_dive_endpoint(data: UpdateDiveSchema):
    import json
    base_dirs = ["./data", "./tests"]
    found_dir = None
    for d in base_dirs:
        if data.username:
            path = os.path.join(d, data.service, data.username)
            if os.path.exists(path):
                found_dir = path
                break
        path = os.path.join(d, data.service)
        if os.path.exists(path):
            found_dir = path
            break
            
    if not found_dir:
        raise HTTPException(status_code=404, detail="Service directory not found.")
        
    filepath = os.path.join(found_dir, data.filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Dive file not found.")
        
    try:
        with open(filepath, "r") as f:
            dive_data = json.load(f)
            
        if data.service == "garmin":
            summary = dive_data.get("summary", {})
            details = dive_data.get("details") or {}
            if not isinstance(details, dict):
                details = {}
                dive_data["details"] = details
            
            # Update fields in Garmin structure
            if data.dive_number is not None:
                if "metadataDTO" not in summary:
                    summary["metadataDTO"] = {}
                if "metadataDTO" not in details:
                    details["metadataDTO"] = {}
                summary["metadataDTO"]["diveNumber"] = data.dive_number
                details["metadataDTO"]["diveNumber"] = data.dive_number
                
            if data.date_time is not None:
                if "summaryDTO" not in summary:
                    summary["summaryDTO"] = {}
                if "summaryDTO" not in details:
                    details["summaryDTO"] = {}
                summary["startTimeLocal"] = data.date_time
                details["startTimeLocal"] = data.date_time
                summary["summaryDTO"]["startTimeLocal"] = data.date_time
                details["summaryDTO"]["startTimeLocal"] = data.date_time
                
            if data.duration is not None:
                if "summaryDTO" not in summary:
                    summary["summaryDTO"] = {}
                if "summaryDTO" not in details:
                    details["summaryDTO"] = {}
                summary["duration"] = data.duration
                details["duration"] = data.duration
                summary["summaryDTO"]["duration"] = data.duration
                details["summaryDTO"]["duration"] = data.duration
                summary["summaryDTO"]["bottomTime"] = data.duration
                details["summaryDTO"]["bottomTime"] = data.duration
                
            if data.max_depth is not None:
                if "summaryDTO" not in summary:
                    summary["summaryDTO"] = {}
                if "summaryDTO" not in details:
                    details["summaryDTO"] = {}
                summary["maxDepth"] = data.max_depth
                details["maxDepth"] = data.max_depth
                summary["summaryDTO"]["maxDepth"] = data.max_depth
                details["summaryDTO"]["maxDepth"] = data.max_depth
                
            if data.location is not None:
                summary["activityName"] = data.location
                details["activityName"] = data.location
                details["locationName"] = data.location
                summary["locationName"] = data.location
                
            if data.notes is not None:
                summary["description"] = data.notes
                details["description"] = data.notes
                
            if "diveInfo" not in summary or not isinstance(summary["diveInfo"], dict):
                summary["diveInfo"] = {}
            if "diveInfo" not in details or not isinstance(details["diveInfo"], dict):
                details["diveInfo"] = {}
                
            if data.weight is not None:
                import re
                weight_val = None
                weight_unit = "kilogram"
                match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]+)?$", data.weight)
                if match:
                    weight_val = float(match.group(1))
                    unit_str = (match.group(2) or "").strip().lower()
                    if "lb" in unit_str or "pound" in unit_str:
                        weight_unit = "pound"
                else:
                    try:
                        weight_val = float(data.weight)
                    except ValueError:
                        pass
                
                summary["diveInfo"]["weight"] = weight_val
                details["diveInfo"]["weight"] = weight_val
                if weight_val is not None:
                    u_key = weight_unit
                    u_id = 8 if u_key == "kilogram" else 9
                    factor = 1000.0 if u_key == "kilogram" else 453.59237
                    u_info = {"unitId": u_id, "unitKey": u_key, "factor": factor}
                    summary["diveInfo"]["weightUnit"] = u_info
                    details["diveInfo"]["weightUnit"] = u_info
                else:
                    summary["diveInfo"]["weightUnit"] = None
                    details["diveInfo"]["weightUnit"] = None
                    
            if data.visibility is not None:
                import re
                vis_val = None
                vis_unit = "meter"
                match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]+)?$", data.visibility)
                if match:
                    vis_val = float(match.group(1))
                    unit_str = (match.group(2) or "").strip().lower()
                    if "ft" in unit_str or "foot" in unit_str or "feet" in unit_str:
                        vis_unit = "foot"
                else:
                    try:
                        vis_val = float(data.visibility)
                    except ValueError:
                        pass
                        
                summary["diveInfo"]["visibility"] = vis_val
                details["diveInfo"]["visibility"] = vis_val
                if vis_val is not None:
                    u_key = vis_unit
                    u_id = 1 if u_key == "meter" else 2
                    factor = 100.0 if u_key == "meter" else 30.48
                    u_info = {"unitId": u_id, "unitKey": u_key, "factor": factor}
                    summary["diveInfo"]["visibilityUnit"] = u_info
                    details["diveInfo"]["visibilityUnit"] = u_info
                else:
                    summary["diveInfo"]["visibilityUnit"] = None
                    details["diveInfo"]["visibilityUnit"] = None
                    
            if data.buddy is not None:
                summary["diveInfo"]["buddy"] = data.buddy
                details["diveInfo"]["buddy"] = data.buddy
                
        elif data.service == "divelogs":
            if data.date_time is not None:
                dt_parts = data.date_time.split(" ", 1)
                dive_data["date"] = dt_parts[0]
                if len(dt_parts) > 1:
                    dive_data["time"] = dt_parts[1]
                    
            if data.duration is not None:
                dive_data["duration"] = data.duration
                
            if data.max_depth is not None:
                dive_data["maxdepth"] = data.max_depth
                
            if data.location is not None:
                if "," in data.location:
                    parts = data.location.split(",", 1)
                    dive_data["location"] = parts[0].strip()
                    dive_data["divesite"] = parts[1].strip()
                else:
                    dive_data["location"] = data.location
                    dive_data["divesite"] = ""
                    
            if data.notes is not None:
                dive_data["notes"] = data.notes
                
            if data.weight is not None:
                dive_data["weights"] = data.weight
                
            if data.visibility is not None:
                dive_data["visibility"] = data.visibility
                
            if data.buddy is not None:
                dive_data["buddy"] = data.buddy

        with open(filepath, "w") as f:
            json.dump(dive_data, f, indent=2)
            
        logger.info("Successfully updated cached %s dive filename %s.", data.service, data.filename)
        
        # Trigger background update to remote site
        if data.service == "garmin":
            threading.Thread(
                target=push_garmin_update_background,
                args=(data.filename, filepath),
                daemon=True
            ).start()
        elif data.service == "divelogs":
            threading.Thread(
                target=push_divelogs_update_background,
                args=(data.filename, filepath),
                daemon=True
            ).start()
            
        return {"status": "success", "message": "Dive updated."}
        
    except Exception as e:
        logger.error("Failed to update dive: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

# Serve index.html statically
@app.get("/")
def read_root():
    return FileResponse("src/web/static/index.html")

# Mount other static assets
app.mount("/static", StaticFiles(directory="src/web/static"), name="static")
