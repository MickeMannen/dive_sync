"""Persistent history of sync runs, for the web dashboard's History page.

``scheduler.last_sync_results`` only holds the latest result per job and is
lost on restart; this keeps the last ``MAX_RUNS`` runs (any job) as one JSON
object per line in ``DATA_DIR/sync_history.jsonl``, next to settings.json,
each with the run's full result and the log lines it wrote."""
import json
import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("dive_sync.run_history")

HISTORY_FILENAME = "sync_history.jsonl"
MAX_RUNS = 200
MAX_LOG_LINES = 2000

# Tests point this at a temporary file; None = DATA_DIR/sync_history.jsonl
HISTORY_FILE: Optional[str] = None

_lock = threading.Lock()

# The run record's fields a listing carries; the full result and log only
# come with get_run.
_SUMMARY_KEYS = ("id", "job", "trigger", "started_at", "finished_at", "duration_s",
                 "dry_run", "source", "target", "status", "error", "counts")


def history_path() -> str:
    return HISTORY_FILE or os.path.join(os.environ.get("DATA_DIR", "."), HISTORY_FILENAME)


def count_results(results: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """How many dives each kind of action touched: uploaded_to_<svc>,
    updated_on_<svc>, deleted_on_<svc>, skipped and conflicts. Empty lists
    are left out so a listing names only what happened; matched_count is
    kept even at 0, as it says the run compared something."""
    counts: Dict[str, int] = {}
    for key, value in (results or {}).items():
        if isinstance(value, list) and value and (
                key.startswith(("uploaded_to_", "updated_on_", "deleted_on_")) or key in ("skipped", "conflicts")):
            counts[key] = len(value)
    if isinstance((results or {}).get("matched_count"), int):
        counts["matched"] = results["matched_count"]
    return counts


class LogCapture(logging.Handler):
    """Collects the dive_sync log lines written by one thread - the run's own,
    not what the web requests serving the page log meanwhile."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.thread_id = threading.get_ident()
        self.lines: List[str] = []
        self.dropped = 0
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    def emit(self, record):
        if record.thread != self.thread_id:
            return
        if len(self.lines) >= MAX_LOG_LINES:
            self.dropped += 1
            return
        try:
            self.lines.append(self.format(record))
        except Exception:
            pass

    def attach(self) -> "LogCapture":
        logging.getLogger("dive_sync").addHandler(self)
        return self

    def detach(self) -> None:
        logging.getLogger("dive_sync").removeHandler(self)

    def all_lines(self) -> List[str]:
        if self.dropped:
            return self.lines + [f"... {self.dropped} more line(s) not kept"]
        return self.lines


def record_run(job: str, trigger: str, started_at: datetime, finished_at: datetime, dry_run: bool,
               results: Optional[Dict[str, Any]] = None, error: Optional[str] = None,
               source: Optional[str] = None, target: Optional[str] = None,
               log: Optional[List[str]] = None) -> Dict[str, Any]:
    """Append one run and trim the file to the newest MAX_RUNS. Never raises:
    a history that cannot be written must not fail the sync it describes."""
    results = results or {}
    entry = {
        "id": uuid.uuid4().hex[:12],
        "job": job,
        "trigger": trigger,
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        "duration_s": round((finished_at - started_at).total_seconds(), 1),
        "dry_run": bool(results.get("dry_run", dry_run)),
        "source": results.get("source") or source,
        "target": results.get("target") or target,
        "status": "error" if error else "ok",
        "error": error,
        "counts": count_results(results),
        "results": results,
        "log": log or [],
    }
    try:
        with _lock:
            runs = _read_all()
            runs.append(entry)
            _write_all(runs[-MAX_RUNS:])
    except Exception as e:
        logger.warning("Could not write the sync history (%s): %s", history_path(), e)
    return entry


def _read_all() -> List[Dict[str, Any]]:
    path = history_path()
    if not os.path.exists(path):
        return []
    runs = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                runs.append(json.loads(line))
            except ValueError:
                continue   # one damaged line (e.g. a crash mid-write) costs only that run
    return runs


def _write_all(runs: List[Dict[str, Any]]) -> None:
    path = history_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for run in runs:
            f.write(json.dumps(run, default=str) + "\n")
    os.replace(tmp, path)


def list_runs(job: Optional[str] = None, status: Optional[str] = None, limit: int = MAX_RUNS) -> List[Dict[str, Any]]:
    """Run summaries, newest first, optionally only one job's or only errors/ok."""
    with _lock:
        runs = _read_all()
    runs.reverse()
    if job:
        runs = [r for r in runs if r.get("job") == job]
    if status:
        runs = [r for r in runs if r.get("status") == status]
    return [{k: r.get(k) for k in _SUMMARY_KEYS} for r in runs[:max(0, limit)]]


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        runs = _read_all()
    return next((r for r in runs if r.get("id") == run_id), None)


def latest_per_job() -> Dict[str, Dict[str, Any]]:
    """The newest run summary of each job, for the Sync page's highlights."""
    latest: Dict[str, Dict[str, Any]] = {}
    for run in list_runs():
        latest.setdefault(run["job"], run)
    return latest
