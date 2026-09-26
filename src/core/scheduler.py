import os
import logging
import threading
import time
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from src.core import dive_cache
from src.core import progress
from src.core import run_history
from src.core.config import ConfigManager
from src.core.fields import FieldLink
from src.core.sync_engine import SyncEngine

logger = logging.getLogger("dive_sync.scheduler")
logger.setLevel(logging.INFO)

is_sync_running = False
# Keyed by job id ("Manual" for an on-demand trigger with no cron job), so
# concurrent-looking jobs (different accounts/pairs) don't clobber each
# other's last-run status on the status page (rework.md A7).
last_sync_results: Dict[str, Dict[str, Any]] = {}

is_download_running = False
last_download_results: Dict[str, Any] = {}


def run_download_thread(
    overwrite: bool = False,
    base_dir: Optional[str] = None,
    include_garmin: bool = True,
    include_divelogs: bool = True,
    services: Optional[List[str]] = None,
    refresh_fits: bool = False,
):
    """Fetches and caches dive JSON to local disk - this is the only thing
    that populates the per-dive cache files dive_cache.py's
    list/read/update/delete functions operate on. A regular sync run does not
    do this (it fetches into memory for matching/pushing only), so this needs
    to run at least once before there's anything for a dive editor UI to show.

    ``services`` names exactly which services to download, e.g. a per-tab
    "Refresh" that should not also hit the other services' APIs. It supersedes
    include_garmin/include_divelogs, which remain for older callers; None
    means those two flags decide.

    ``refresh_fits`` (a Garmin full refresh): afterwards download the original
    .fit of every cached device dive again, replacing the saved copies.
    Hand-logged dives are skipped - their FIT is one Connect makes up."""
    global is_download_running, last_download_results
    if services is not None:
        include_garmin = "garmin" in services
        include_divelogs = "divelogs" in services
    unified = [s for s in (services if services is not None else []) if dive_cache.is_unified_cache(s)]

    is_download_running = True
    progress.report(0, 0, "Starting download", ",".join(services or []))
    logger.info(
        "Raw dive data download started (overwrite=%s, garmin=%s, divelogs=%s%s)...",
        overwrite, include_garmin, include_divelogs,
        ", " + ", ".join(unified) if unified else "",
    )
    try:
        resolved_base_dir = base_dir or os.environ.get("DATA_DIR", "./data")
        success = True
        if include_garmin or include_divelogs:
            engine = SyncEngine()
            success = engine.download_and_save_raw_data(
                mock_data_dir=resolved_base_dir,
                overwrite=overwrite,
                include_garmin=include_garmin,
                include_divelogs=include_divelogs,
            )
        for service in unified:
            # One service failing must not cost the others their download.
            try:
                # always a full download, pruned (overwrite only concerns Garmin's reuse of unchanged dives)
                dive_cache.download_service_dives(service, base_dir=resolved_base_dir)
            except Exception as e:
                logger.error("Failed to download %s dives: %s", service, e)
                success = False
        last_download_results = {"success": success}
        if refresh_fits and include_garmin:
            fits = _refresh_garmin_fits(resolved_base_dir)
            last_download_results["fit"] = fits
            if fits["failed"]:
                success = False
                last_download_results["success"] = False
        if success:
            logger.info("Raw dive data download completed successfully.")
        else:
            logger.error("Raw dive data download completed with errors - see log above.")
    except Exception as e:
        logger.error("Raw dive data download encountered an error: %s", e)
        last_download_results = {"error": str(e)}
    finally:
        is_download_running = False
        progress.clear()


def _refresh_garmin_fits(base_dir: str) -> Dict[str, Any]:
    """Re-download the FIT of every cached Garmin device dive, per account."""
    by_account: Dict[Optional[str], List[str]] = {}
    for row in dive_cache.list_garmin_dives(base_dir=base_dir):
        if not row.get("manual"):
            by_account.setdefault(row.get("account"), []).append(row["filename"])
    result: Dict[str, Any] = {"downloaded": [], "failed": {}}
    for account, filenames in by_account.items():
        logger.info("Refreshing %d FIT file(s)%s...", len(filenames), f" for {account}" if account else "")
        part = dive_cache.download_garmin_fits(filenames, account, base_dir)
        result["downloaded"] += part["downloaded"]
        result["failed"].update(part["failed"])
    logger.info("FIT refresh finished: %d saved, %d failed.", len(result["downloaded"]), len(result["failed"]))
    return result


def run_fit_download_thread(filenames: List[str], username: Optional[str] = None,
                            base_dir: Optional[str] = None) -> Dict[str, Any]:
    """Download the original .fit of the given cached Garmin dives. Holds the
    download flag like a refresh does: both talk to Garmin, and one long job
    at a time is what progress.py assumes."""
    global is_download_running, last_download_results
    is_download_running = True
    progress.report(0, len(filenames), "Starting FIT download", "garmin")
    try:
        result = dive_cache.download_garmin_fits(filenames, username, base_dir)
        last_download_results = {"success": not result["failed"], "fit": result}
        logger.info("FIT download finished: %d saved, %d failed.", len(result["downloaded"]), len(result["failed"]))
        return result
    except Exception as e:
        logger.error("FIT download encountered an error: %s", e)
        last_download_results = {"error": str(e)}
        return {"downloaded": [], "failed": {}, "error": str(e)}
    finally:
        is_download_running = False
        progress.clear()


def run_sync_thread(dry_run: bool, custom_settings: Optional[Dict[str, Any]] = None, trigger: str = "manual"):
    """``trigger`` is "scheduled" when the scheduler loop starts the run,
    for the History page; it does not change what the run does."""
    global is_sync_running, last_sync_results
    is_sync_running = True
    job_id = (custom_settings or {}).get("id") or "Manual"
    garmin_username = (custom_settings or {}).get("garmin_username") or None
    divelogs_username = (custom_settings or {}).get("divelogs_username") or None
    started_at = datetime.now()
    capture = run_history.LogCapture().attach()
    results: Dict[str, Any] = {}
    error: Optional[str] = None
    logger.info("Synchronization started for job '%s' (Dry Run: %s)", job_id, dry_run)
    try:
        if custom_settings and custom_settings.get("pair"):
            from src.core.config import ConfigManager
            from src.core.pairs import engine_for_pair, find_pair
            pair = find_pair(ConfigManager.load_settings(), custom_settings["pair"])
            engine = engine_for_pair(pair, garmin_username=garmin_username, divelogs_username=divelogs_username)
        elif custom_settings and custom_settings.get("source") and custom_settings.get("target"):
            # Two service specs with no saved pair behind them: what the
            # desktop Sync page offers for the combinations of configured
            # services, so syncing e.g. Garmin to Submersion needs no
            # hand-written entry in settings.json first.
            from src.core.pairs import engine_for
            engine = engine_for(custom_settings["source"], custom_settings["target"],
                                garmin_username=garmin_username, divelogs_username=divelogs_username)
        else:
            engine = SyncEngine(garmin_username=garmin_username, divelogs_username=divelogs_username)
        if custom_settings:
            # run_sync re-reads settings.json before every run, so per-job
            # values must go in as explicit overrides rather than by editing
            # engine.settings here (which the reload would discard).
            overrides: Dict[str, Any] = dict(engine.run_overrides)
            # rework.md G0: every run writes one side. A job (or the UI's
            # ad-hoc run) that names a direction runs that way - a two-way
            # schedule is two jobs on the same pair, one per direction - and
            # a job that leaves it empty follows the pair's saved direction.
            if custom_settings.get("directionality"):
                overrides["direction_override"] = custom_settings["directionality"]
            if "only_new" in custom_settings and custom_settings["only_new"] is not None:
                overrides["only_new_override"] = bool(custom_settings["only_new"])
            if "sync_gases" in custom_settings and custom_settings["sync_gases"] is not None:
                overrides["sync_gases_override"] = bool(custom_settings["sync_gases"])
            if custom_settings.get("use_garmin_cache") is not None:
                overrides["use_garmin_cache_override"] = bool(custom_settings["use_garmin_cache"])
            if custom_settings.get("propagate_deletes") is not None:
                overrides["propagate_deletes_override"] = bool(custom_settings["propagate_deletes"])
            if custom_settings.get("mirror"):
                overrides["mirror_override"] = True
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
        last_sync_results[job_id] = results
        logger.info("Synchronization completed successfully.")
    except Exception as e:
        error = str(e)
        logger.error("Sync run encountered an error: %s", e)
        last_sync_results[job_id] = {"error": error}
        _notify_failure(job_id, error)
    finally:
        capture.detach()
        run_history.record_run(
            job_id, trigger, started_at, datetime.now(), dry_run,
            results=results if isinstance(results, dict) else {}, error=error,
            source=(custom_settings or {}).get("source"), target=(custom_settings or {}).get("target"),
            log=capture.all_lines(),
        )
        is_sync_running = False
        progress.clear()


def _notify_failure(job_id: str, error: str) -> None:
    """Best-effort webhook alert (rework.md A11). A broken/unset notify_url,
    or the settings file itself being unreadable, must never raise out of
    here - this runs from the except block of the sync it's reporting on."""
    try:
        from src.core.config import ConfigManager
        from src.core.notify import notify_run_failure
        notify_url = ConfigManager.load_settings().notify_url
        if notify_url:
            notify_run_failure(notify_url, job_id, error)
    except Exception as notify_error:
        logger.warning("Failed to send failure alert for job '%s': %s", job_id, notify_error)


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
                            threading.Thread(target=run_sync_thread, args=(False,),
                                             kwargs={"trigger": "scheduled"}, daemon=True).start()
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
                            threading.Thread(target=run_sync_thread, args=(False, custom_set),
                                             kwargs={"trigger": "scheduled"}, daemon=True).start()
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
