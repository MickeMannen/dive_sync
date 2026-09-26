"""The Stop button (progress.request_stop): a long job ends at its next
progress report, keeps what it did, and the request never outlives it."""
import json
from types import SimpleNamespace

import pytest

from src.core import progress, scheduler
from src.core.sync_engine import SyncEngine


@pytest.fixture(autouse=True)
def no_leftover_stop():
    progress.clear()
    yield
    progress.clear()


def test_report_raises_once_a_stop_is_requested_and_clear_forgets_it():
    progress.report(1, 3, "x")
    progress.request_stop()
    with pytest.raises(progress.Stopped):
        progress.report(2, 3, "y")
    progress.clear()
    assert not progress.stop_requested()
    progress.report(1, 3, "next job")        # not stopped


def test_stopped_is_not_swallowed_by_a_per_dive_except_exception():
    progress.request_stop()
    with pytest.raises(progress.Stopped):
        try:
            progress.report(1, 2)
        except Exception:
            pass


@pytest.mark.parametrize("stop_before_start", [False, True])
def test_download_stops_and_releases_the_download_flag(monkeypatch, tmp_path, stop_before_start):
    reached = []

    def download(self, mock_data_dir, overwrite, include_garmin, include_divelogs):
        for n in range(1, 4):
            progress.report(n, 3, f"dive {n}", "garmin")
            reached.append(n)
            if n == 1:
                progress.request_stop()
        return True

    monkeypatch.setattr(SyncEngine, "download_and_save_raw_data", download)
    if stop_before_start:
        progress.request_stop()
    scheduler.run_download_thread(base_dir=str(tmp_path), services=["garmin"])
    assert scheduler.last_download_results == {"stopped": True}
    assert reached == ([] if stop_before_start else [1])
    assert scheduler.is_download_running is False and not progress.stop_requested()


def test_stopped_sync_keeps_its_work_and_sends_no_failure_alert(monkeypatch):
    kept, alerts = [], []

    def run_sync(self, dry_run=False, **overrides):
        progress.request_stop()
        progress.report(1, 5, "dive 1")

    monkeypatch.setattr(SyncEngine, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(SyncEngine, "run_sync", run_sync)
    monkeypatch.setattr(SyncEngine, "keep_stopped_run", lambda self: kept.append(True))
    monkeypatch.setattr(scheduler, "_notify_failure", lambda *a: alerts.append(a))
    monkeypatch.setattr(scheduler.run_history, "record_run", lambda *a, **k: None)
    scheduler.run_sync_thread(False)
    assert scheduler.last_sync_results["Manual"] == {"stopped": True}
    assert kept == [True] and alerts == [] and scheduler.is_sync_running is False


def test_keep_stopped_run_saves_links_but_not_the_last_sync_time(tmp_path):
    state_file = tmp_path / "sync_state.json"
    state_file.write_text(json.dumps({"last_sync_time": "2026-09-01T10:00:00", "links": {}}))
    finished = []
    engine = SyncEngine.__new__(SyncEngine)
    engine.state_file = str(state_file)
    engine.source = SimpleNamespace(finish=lambda: finished.append("source"))
    engine.target = SimpleNamespace()        # not a batching adapter
    engine._run_links = {"garmin:1": "divelogs:9"}
    engine.keep_stopped_run()
    state = json.loads(state_file.read_text())
    assert state == {"last_sync_time": "2026-09-01T10:00:00", "links": {"garmin:1": "divelogs:9"}}
    assert finished == ["source"]
    engine.keep_stopped_run()                # only once
    assert finished == ["source"]
