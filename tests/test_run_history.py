import logging
import threading
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from src.core import run_history, scheduler


def _record(job="Manual", error=None, results=None, **kw):
    start = datetime(2026, 9, 1, 6, 0, 0)
    return run_history.record_run(job, "manual", start, start + timedelta(seconds=12.34), False,
                                  results=results, error=error, **kw)


def test_count_results_keeps_only_what_happened():
    counts = run_history.count_results({
        "matched_count": 4,
        "uploaded_to_divelogs": [{}, {}],
        "uploaded_to_garmin": [],
        "updated_on_divelogs": [{}],
        "deleted_on_divelogs": [],
        "skipped": [{}],
        "conflicts": [],
        "source": "garmin",
    })
    assert counts == {"matched": 4, "uploaded_to_divelogs": 2, "updated_on_divelogs": 1, "skipped": 1}


def test_record_and_list_newest_first():
    first = _record(job="a", results={"uploaded_to_divelogs": [{"time": "t"}], "source": "garmin", "target": "divelogs"})
    second = _record(job="b", error="kaboom")

    runs = run_history.list_runs()
    assert [r["id"] for r in runs] == [second["id"], first["id"]]
    assert runs[1]["counts"] == {"uploaded_to_divelogs": 1}
    assert runs[1]["source"] == "garmin" and runs[1]["duration_s"] == 12.3
    assert runs[0]["status"] == "error" and runs[0]["error"] == "kaboom"
    # listings leave out the full result and the log
    assert "results" not in runs[0] and "log" not in runs[0]
    assert [r["job"] for r in run_history.list_runs(job="a")] == ["a"]
    assert [r["job"] for r in run_history.list_runs(status="error")] == ["b"]


def test_get_run_returns_everything():
    entry = _record(results={"skipped": [{"reason": "x"}]}, log=["line 1"])
    run = run_history.get_run(entry["id"])
    assert run["results"] == {"skipped": [{"reason": "x"}]}
    assert run["log"] == ["line 1"]
    assert run_history.get_run("nope") is None


def test_history_is_trimmed_to_max_runs(monkeypatch):
    monkeypatch.setattr(run_history, "MAX_RUNS", 3)
    ids = [_record(job=str(i))["id"] for i in range(5)]
    assert [r["id"] for r in run_history.list_runs()] == ids[:1:-1]


def test_damaged_line_is_skipped():
    _record(job="ok")
    with open(run_history.history_path(), "a") as f:
        f.write("{not json\n")
    assert [r["job"] for r in run_history.list_runs()] == ["ok"]


def test_unwritable_history_does_not_raise(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setattr(run_history, "HISTORY_FILE", str(blocker / "sync_history.jsonl"))
    entry = _record()
    assert entry["status"] == "ok"


def test_latest_per_job():
    _record(job="a")
    newer = _record(job="a", error="x")
    other = _record(job="b")
    latest = run_history.latest_per_job()
    assert latest["a"]["id"] == newer["id"] and latest["b"]["id"] == other["id"]


def test_log_capture_keeps_only_its_own_thread():
    log = logging.getLogger("dive_sync.test")
    capture = run_history.LogCapture().attach()
    try:
        log.info("mine")
        t = threading.Thread(target=lambda: log.info("someone else's"))
        t.start()
        t.join()
    finally:
        capture.detach()
    log.info("after")
    assert len(capture.lines) == 1 and capture.lines[0].endswith("mine")


def test_run_sync_thread_records_history(monkeypatch):
    from src.core.sync_engine import SyncEngine

    def fake_run(self, dry_run=False):
        logging.getLogger("dive_sync.sync_engine").info("Sync action: Upload")
        return {"dry_run": dry_run, "source": "garmin", "target": "divelogs", "uploaded_to_divelogs": [{"time": "t"}]}

    monkeypatch.setattr(SyncEngine, "run_sync", fake_run)
    scheduler.run_sync_thread(dry_run=True, trigger="scheduled")

    [run] = run_history.list_runs()
    assert run["job"] == "Manual" and run["trigger"] == "scheduled" and run["dry_run"] is True
    assert run["counts"] == {"uploaded_to_divelogs": 1}
    full = run_history.get_run(run["id"])
    assert any("Sync action: Upload" in line for line in full["log"])


def test_run_sync_thread_records_failure(monkeypatch):
    from src.core.sync_engine import SyncEngine

    def boom(self, dry_run=False):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(SyncEngine, "run_sync", boom)
    scheduler.run_sync_thread(dry_run=False)

    [run] = run_history.list_runs()
    assert run["status"] == "error" and run["error"] == "kaboom" and run["trigger"] == "manual"


def test_history_endpoints():
    from src.web.app import app
    entry = _record(job="nightly", results={"updated_on_divelogs": [{"id": "1"}]}, log=["hello"])
    client = TestClient(app)

    runs = client.get("/api/history").json()["runs"]
    assert [r["id"] for r in runs] == [entry["id"]]
    assert client.get("/api/history", params={"job": "other"}).json()["runs"] == []

    full = client.get(f"/api/history/{entry['id']}").json()
    assert full["log"] == ["hello"] and full["results"]["updated_on_divelogs"] == [{"id": "1"}]
    assert client.get("/api/history/missing").status_code == 404

    status = client.get("/api/status").json()
    assert status["last_runs"]["nightly"]["id"] == entry["id"]
