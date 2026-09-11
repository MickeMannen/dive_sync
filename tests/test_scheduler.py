from datetime import datetime

from src.core import scheduler
from src.core.config import SettingsModel, SyncScheduleSlot, CronJobModel


def test_run_sync_thread_success(monkeypatch):
    from src.core.sync_engine import SyncEngine

    monkeypatch.setattr(SyncEngine, "run_sync", lambda self, dry_run=False: {"status": "ok"})

    scheduler.run_sync_thread(dry_run=True)

    assert scheduler.is_sync_running is False
    assert scheduler.last_sync_results == {"status": "ok"}


def test_run_sync_thread_records_error(monkeypatch):
    from src.core.sync_engine import SyncEngine

    def boom(self, dry_run=False):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(SyncEngine, "run_sync", boom)

    scheduler.run_sync_thread(dry_run=False)

    assert scheduler.is_sync_running is False
    assert scheduler.last_sync_results == {"error": "kaboom"}


def test_run_download_thread_success(monkeypatch, tmp_path):
    from src.core.sync_engine import SyncEngine

    calls = []
    monkeypatch.setattr(
        SyncEngine, "download_and_save_raw_data",
        lambda self, mock_data_dir, overwrite, include_garmin, include_divelogs:
            calls.append((mock_data_dir, overwrite, include_garmin, include_divelogs)) or True,
    )

    scheduler.run_download_thread(overwrite=True, base_dir=str(tmp_path), include_garmin=True, include_divelogs=False)

    assert scheduler.is_download_running is False
    assert scheduler.last_download_results == {"success": True}
    assert calls == [(str(tmp_path), True, True, False)]


def test_run_download_thread_records_failure(monkeypatch, tmp_path):
    from src.core.sync_engine import SyncEngine

    monkeypatch.setattr(
        SyncEngine, "download_and_save_raw_data",
        lambda self, mock_data_dir, overwrite, include_garmin, include_divelogs: False,
    )

    scheduler.run_download_thread(base_dir=str(tmp_path))

    assert scheduler.is_download_running is False
    assert scheduler.last_download_results == {"success": False}


def test_run_download_thread_records_error(monkeypatch, tmp_path):
    from src.core.sync_engine import SyncEngine

    def boom(self, mock_data_dir, overwrite, include_garmin, include_divelogs):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(SyncEngine, "download_and_save_raw_data", boom)

    scheduler.run_download_thread(base_dir=str(tmp_path))

    assert scheduler.is_download_running is False
    assert scheduler.last_download_results == {"error": "network exploded"}


def test_get_next_scheduled_run_none_when_empty():
    settings = SettingsModel()
    assert scheduler.get_next_scheduled_run(settings) is None


def test_get_next_scheduled_run_legacy_slot():
    settings = SettingsModel(schedule=[SyncScheduleSlot(hour=23, minute=59)])
    next_run = scheduler.get_next_scheduled_run(settings)
    assert next_run is not None
    parsed = datetime.fromisoformat(next_run)
    assert parsed.hour == 23 and parsed.minute == 59


def test_get_next_scheduled_run_ignores_disabled_cron_jobs():
    settings = SettingsModel(cron_jobs=[
        CronJobModel(id="disabled-job", frequency="hourly", minute=0, enabled=False)
    ])
    assert scheduler.get_next_scheduled_run(settings) is None


def test_get_next_scheduled_run_custom_minutes():
    settings = SettingsModel(cron_jobs=[
        CronJobModel(id="frequent-job", frequency="custom_minutes", interval_minutes=15, enabled=True)
    ])
    next_run = scheduler.get_next_scheduled_run(settings)
    assert next_run is not None
    delta = datetime.fromisoformat(next_run) - datetime.now()
    assert 0 < delta.total_seconds() <= 15 * 60 + 1
