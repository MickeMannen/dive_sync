import os
import logging
import threading
import time
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

from src.core.config import ConfigManager
from src.core.fields import FieldLink
from src.core.sync_engine import SyncEngine

logger = logging.getLogger("dive_sync.scheduler")
logger.setLevel(logging.INFO)

is_sync_running = False
last_sync_results: Dict[str, Any] = {}

is_download_running = False
last_download_results: Dict[str, Any] = {}


def run_download_thread(
    overwrite: bool = False,
    base_dir: Optional[str] = None,
    include_garmin: bool = True,
    include_divelogs: bool = True,
):
    """Fetches and caches raw dive JSON from Garmin/Divelogs to local disk -
    this is the only thing that populates the per-dive cache files
    dive_cache.py's list/read/update/delete functions operate on. A regular
    sync run does not do this (it fetches into memory for matching/pushing
    only), so this needs to run at least once before there's anything for a
    dive editor UI to show. include_garmin/include_divelogs let a caller
    (e.g. a per-tab "Refresh") scope this to just one service."""
    global is_download_running, last_download_results
    is_download_running = True
    logger.info(
        "Raw dive data download started (overwrite=%s, garmin=%s, divelogs=%s)...",
        overwrite, include_garmin, include_divelogs,
    )
    try:
        engine = SyncEngine()
        resolved_base_dir = base_dir or os.environ.get("DATA_DIR", "./data")
        success = engine.download_and_save_raw_data(
            mock_data_dir=resolved_base_dir,
            overwrite=overwrite,
            include_garmin=include_garmin,
            include_divelogs=include_divelogs,
        )
        last_download_results = {"success": success}
        if success:
            logger.info("Raw dive data download completed successfully.")
        else:
            logger.error("Raw dive data download completed with errors - see log above.")
    except Exception as e:
        logger.error("Raw dive data download encountered an error: %s", e)
        last_download_results = {"error": str(e)}
    finally:
        is_download_running = False


def run_sync_thread(dry_run: bool, custom_settings: Optional[Dict[str, Any]] = None):
    global is_sync_running, last_sync_results
    is_sync_running = True
    job_id = custom_settings.get("id") if custom_settings else "Manual"
    logger.info("Synchronization started for job '%s' (Dry Run: %s)", job_id, dry_run)
    try:
        if custom_settings and custom_settings.get("pair"):
            from src.core.config import ConfigManager
            from src.core.pairs import engine_for_pair, find_pair
            pair = find_pair(ConfigManager.load_settings(), custom_settings["pair"])
            engine = engine_for_pair(pair)
        else:
            engine = SyncEngine()
            engine.run_overrides = {}
        if custom_settings:
            # run_sync re-reads settings.json before every run, so per-job
            # values must go in as explicit overrides rather than by editing
            # engine.settings here (which the reload would discard).
            overrides: Dict[str, Any] = dict(engine.run_overrides)
            if custom_settings.get("directionality") and not custom_settings.get("pair"):
                overrides["direction_override"] = custom_settings["directionality"]
            if "only_new" in custom_settings and custom_settings["only_new"] is not None:
                overrides["only_new_override"] = bool(custom_settings["only_new"])
            if "sync_gases" in custom_settings and custom_settings["sync_gases"] is not None:
                overrides["sync_gases_override"] = bool(custom_settings["sync_gases"])
            if "sync_fit" in custom_settings and custom_settings["sync_fit"] is not None:
                overrides["sync_fit_override"] = bool(custom_settings["sync_fit"])
            if "date_from" in custom_settings:
                overrides["date_from_override"] = custom_settings["date_from"]
            if "date_to" in custom_settings:
                overrides["date_to_override"] = custom_settings["date_to"]
            if custom_settings.get("field_links"):
                overrides["field_links_override"] = [
                    link if isinstance(link, FieldLink) else FieldLink.model_validate(link)
                    for link in custom_settings["field_links"]
                ]
            results = engine.run_sync(dry_run=dry_run, **overrides)
        else:
            results = engine.run_sync(dry_run=dry_run)
        last_sync_results = results
        logger.info("Synchronization completed successfully.")
    except Exception as e:
        logger.error("Sync run encountered an error: %s", e)
        last_sync_results = {"error": str(e)}
    finally:
        is_sync_running = False


def _next_daily_occurrence(now: datetime, hour: int, minute: int) -> datetime:
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _next_weekly_occurrence(now: datetime, day_of_week: int, hour: int, minute: int) -> datetime:
    # day_of_week: 0=Sunday..6=Saturday (matches scheduler_loop's mapping below)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    current_mapped = (now.weekday() + 1) % 7
    days_ahead = (day_of_week - current_mapped) % 7
    candidate += timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def get_next_scheduled_run(settings=None) -> Optional[str]:
    """Best-effort estimate of the next scheduled sync time, for status display."""
    if settings is None:
        settings = ConfigManager.load_settings()

    now = datetime.now()
    candidates = []

    for slot in getattr(settings, "schedule", []):
        candidates.append(_next_daily_occurrence(now, slot.hour, slot.minute))

    for job in getattr(settings, "cron_jobs", []):
        if not job.enabled:
            continue
        if job.frequency == "hourly":
            candidate = now.replace(minute=job.minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(hours=1)
            candidates.append(candidate)
        elif job.frequency == "daily":
            candidates.append(_next_daily_occurrence(now, job.hour, job.minute))
        elif job.frequency == "weekly":
            candidates.append(_next_weekly_occurrence(now, job.day_of_week, job.hour, job.minute))
        elif job.frequency == "custom_minutes":
            candidates.append(now + timedelta(minutes=job.interval_minutes))

    if not candidates:
        return None
    return min(candidates).isoformat()


async def scheduler_loop():
    global is_sync_running
    logger.info("Background schedule watcher started.")
    last_checked_minute = None

    # Track last run timestamp for custom minutes jobs
    job_last_run: Dict[str, float] = {}

    while True:
        try:
            now = datetime.now()
            current_minute = (now.hour, now.minute)
            current_time = time.time()

            if current_minute != last_checked_minute:
                settings = ConfigManager.load_settings()

                # Support old schedule slots (daily)
                for slot in getattr(settings, "schedule", []):
                    if slot.hour == now.hour and slot.minute == now.minute:
                        logger.info("Legacy scheduled slot triggered for %02d:%02d", slot.hour, slot.minute)
                        if not is_sync_running:
                            threading.Thread(target=run_sync_thread, args=(False,), daemon=True).start()
                        else:
                            logger.warning("Scheduled sync skipped: another synchronization is currently running.")

                # Support new custom cron jobs
                for job in getattr(settings, "cron_jobs", []):
                    if not job.enabled:
                        continue

                    should_trigger = False

                    if job.frequency == "hourly":
                        if now.minute == job.minute:
                            should_trigger = True
                    elif job.frequency == "daily":
                        if now.hour == job.hour and now.minute == job.minute:
                            should_trigger = True
                    elif job.frequency == "weekly":
                        mapped_weekday = (now.weekday() + 1) % 7
                        if mapped_weekday == job.day_of_week and now.hour == job.hour and now.minute == job.minute:
                            should_trigger = True
                    elif job.frequency == "custom_minutes":
                        if job.id not in job_last_run:
                            job_last_run[job.id] = current_time
                        elif current_time - job_last_run[job.id] >= job.interval_minutes * 60:
                            should_trigger = True

                    if should_trigger:
                        logger.info("Cron job '%s' triggered (%s)", job.id, job.frequency)
                        if job.frequency == "custom_minutes":
                            job_last_run[job.id] = current_time

                        if not is_sync_running:
                            custom_set = job.model_dump()
                            threading.Thread(target=run_sync_thread, args=(False, custom_set), daemon=True).start()
                        else:
                            logger.warning("Cron job '%s' skipped: another synchronization is currently running.", job.id)

                last_checked_minute = current_minute

            await asyncio.sleep(10)
        except asyncio.CancelledError:
            logger.info("Background schedule watcher stopped.")
            break
        except Exception as e:
            logger.error("Error in scheduler loop: %s", e)
            await asyncio.sleep(30)
