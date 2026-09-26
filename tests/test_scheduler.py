from datetime import datetime

from src.core import scheduler
from src.core.config import SettingsModel, SyncScheduleSlot, CronJobModel


def test_run_sync_thread_success(monkeypatch):
    from src.core.sync_engine import SyncEngine

    monkeypatch.setattr(SyncEngine, "run_sync", lambda self, dry_run=False: {"status": "ok"})

    scheduler.run_sync_thread(dry_run=True)

    assert scheduler.is_sync_running is False
    assert scheduler.last_sync_results["Manual"] == {"status": "ok"}


def test_run_sync_thread_records_error(monkeypatch):
    from src.core.sync_engine import SyncEngine

    def boom(self, dry_run=False):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(SyncEngine, "run_sync", boom)

    scheduler.run_sync_thread(dry_run=False)

    assert scheduler.is_sync_running is False
    assert scheduler.last_sync_results["Manual"] == {"error": "kaboom"}


def test_run_sync_thread_sends_failure_alert_when_notify_url_set(monkeypatch, tmp_path):
    from src.core.sync_engine import SyncEngine
    from src.core.config import ConfigManager
    from src.core import notify

    def boom(self, dry_run=False):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(SyncEngine, "run_sync", boom)
    monkeypatch.setattr(ConfigManager, "load_settings", lambda path=None: SettingsModel(notify_url="https://ntfy.sh/mytopic"))
    calls = []
    monkeypatch.setattr(notify, "notify_run_failure", lambda url, job_id, error: calls.append((url, job_id, error)))

    scheduler.run_sync_thread(dry_run=False)

    assert calls == [("https://ntfy.sh/mytopic", "Manual", "kaboom")]


def test_run_sync_thread_skips_alert_when_notify_url_unset(monkeypatch):
    from src.core.sync_engine import SyncEngine
    from src.core.config import ConfigManager
    from src.core import notify

    def boom(self, dry_run=False):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(SyncEngine, "run_sync", boom)
    monkeypatch.setattr(ConfigManager, "load_settings", lambda path=None: SettingsModel())
    calls = []
    monkeypatch.setattr(notify, "notify_run_failure", lambda *a: calls.append(a))

    scheduler.run_sync_thread(dry_run=False)

    assert calls == []


def test_run_sync_thread_success_sends_no_alert(monkeypatch):
    from src.core.sync_engine import SyncEngine
    from src.core import notify

    monkeypatch.setattr(SyncEngine, "run_sync", lambda self, dry_run=False: {"status": "ok"})
    calls = []
    monkeypatch.setattr(notify, "notify_run_failure", lambda *a: calls.append(a))

    scheduler.run_sync_thread(dry_run=True)

    assert calls == []


def test_run_sync_thread_keys_results_by_job_id(monkeypatch):
    from src.core.sync_engine import SyncEngine

    monkeypatch.setattr(SyncEngine, "run_sync", lambda self, dry_run=False, **overrides: {"status": "ok"})

    scheduler.run_sync_thread(dry_run=True, custom_settings={"id": "job-a"})
    scheduler.run_sync_thread(dry_run=True)

    assert scheduler.last_sync_results["job-a"] == {"status": "ok"}
    assert scheduler.last_sync_results["Manual"] == {"status": "ok"}


def test_run_sync_thread_passes_account_overrides(monkeypatch):
    from src.core.sync_engine import SyncEngine

    captured = {}

    def fake_init(self, *args, garmin_username=None, divelogs_username=None, **kwargs):
        captured["garmin_username"] = garmin_username
        captured["divelogs_username"] = divelogs_username
        self.run_overrides = {}

    monkeypatch.setattr(SyncEngine, "__init__", fake_init)
    monkeypatch.setattr(SyncEngine, "run_sync", lambda self, dry_run=False, **overrides: {"status": "ok"})

    scheduler.run_sync_thread(dry_run=True, custom_settings={"id": "job-a", "garmin_username": "alice", "divelogs_username": "bob"})

    assert captured == {"garmin_username": "alice", "divelogs_username": "bob"}


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


def test_run_download_thread_services_picks_the_right_path_per_service(monkeypatch, tmp_path):
    """``services`` supersedes the two booleans and reaches the UnifiedDive
    services, whose download goes through dive_cache rather than the engine."""
    from src.core import dive_cache
    from src.core.sync_engine import SyncEngine

    engine_calls, cache_calls = [], []
    monkeypatch.setattr(
        SyncEngine, "download_and_save_raw_data",
        lambda self, mock_data_dir, overwrite, include_garmin, include_divelogs:
            engine_calls.append((include_garmin, include_divelogs)) or True,
    )
    monkeypatch.setattr(dive_cache, "download_service_dives",
                        lambda service, overwrite=False, base_dir=None, username=None: cache_calls.append(service) or 1)

    scheduler.run_download_thread(base_dir=str(tmp_path), services=["submersion"])
    assert engine_calls == [] and cache_calls == ["submersion"]   # no Garmin/Divelogs API hit
    assert scheduler.last_download_results == {"success": True}

    engine_calls.clear(); cache_calls.clear()
    scheduler.run_download_thread(base_dir=str(tmp_path), services=["garmin", "subsurface"])
    assert engine_calls == [(True, False)] and cache_calls == ["subsurface"]

    # One service failing is reported but must not cost the others their run.
    engine_calls.clear(); cache_calls.clear()

    def boom(service, overwrite=False, base_dir=None, username=None):
        cache_calls.append(service)
        raise RuntimeError("store unreachable")

    monkeypatch.setattr(dive_cache, "download_service_dives", boom)
    scheduler.run_download_thread(base_dir=str(tmp_path), services=["garmin", "submersion", "subsurface"])
    assert engine_calls == [(True, False)] and cache_calls == ["submersion", "subsurface"]
    assert scheduler.last_download_results == {"success": False}


def test_run_sync_thread_builds_an_engine_from_two_specs(monkeypatch, tmp_path):
    """The desktop Sync page's built-in combinations have no saved pair, so
    they travel as source/target specs."""
    import src.core.pairs as pairs

    captured = {}

    class FakeEngine:
        run_overrides = {}

        def run_sync(self, **kwargs):
            captured["overrides"] = kwargs
            return {"ok": True}

    def fake_engine_for(source, target, **kwargs):
        captured["specs"] = (source, target)
        return FakeEngine()

    monkeypatch.setattr(pairs, "engine_for", fake_engine_for)
    scheduler.run_sync_thread(False, {"source": "garmin", "target": "submersion",
                                      "directionality": "to_submersion"})

    assert captured["specs"] == ("garmin", "submersion")
    assert captured["overrides"].get("direction_override") == "to_submersion"


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


def test_progress_reporting_round_trip():
    """rework.md E14: adapters report how far a long job has got, the UIs read
    it. A Garmin refresh is three API calls per dive with a cool-down between
    each, so an indeterminate spinner was all either UI could show before."""
    from src.core import progress
    progress.clear()
    assert progress.current() is None and progress.text() == ""

    progress.report(0, 0, "Starting download", "garmin")
    state = progress.current()
    assert state["fraction"] is None and state["service"] == "garmin"
    assert progress.text() == "Starting download"        # size unknown -> no counter

    progress.report(3, 12, "Fetching Garmin dive 2026-08-29 12:52:59", "garmin")
    state = progress.current()
    assert state["done"] == 3 and state["total"] == 12 and state["fraction"] == 0.25
    assert progress.text().endswith("(3/12)")

    progress.clear()
    assert progress.current() is None


def test_garmin_detail_fetch_reports_progress(monkeypatch):
    """The reporting really is wired into the slow loop, not just available."""
    from datetime import datetime
    from src.core import progress
    from src.core.services.garmin import GarminAdapter
    progress.clear()
    seen = []
    monkeypatch.setattr(progress, "report",
                        lambda done, total, message="", service="": seen.append((done, total, service)))

    g = GarminAdapter("dummy", "dummy")
    g.logged_in = True
    g.cooldown_seconds = 0

    class FakeClient:
        def connectapi(self, url, params=None):
            return {"activityId": url.rsplit("/", 1)[-1], "summaryDTO": {}}

        def get_activity_details(self, activity_id):
            return {}
    g.client = FakeClient()

    targets = [({"activityId": str(i)}, datetime(2026, 6, i + 1, 10, 0)) for i in range(1, 4)]
    g._fetch_activity_details(targets)
    assert seen == [(1, 3, "garmin"), (2, 3, "garmin"), (3, 3, "garmin")], seen


def test_full_refresh_replaces_fit_files_of_device_dives(monkeypatch, tmp_path):
    """A Garmin full refresh (refresh_fits) downloads every device dive's FIT
    again, per account; hand-logged dives are skipped."""
    from src.core import dive_cache, scheduler
    from src.core.sync_engine import SyncEngine
    monkeypatch.setattr(SyncEngine, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(SyncEngine, "download_and_save_raw_data", lambda self, **k: True)
    monkeypatch.setattr(dive_cache, "list_garmin_dives", lambda username=None, base_dir=None: [
        {"filename": "1.json", "account": "a", "manual": False},
        {"filename": "2.json", "account": "a", "manual": True},
        {"filename": "3.json", "account": "b", "manual": False}])
    calls = []
    def fake_fits(filenames, username=None, base_dir=None):
        calls.append((username, list(filenames)))
        return {"downloaded": [f for f in filenames if f != "3.json"], "failed": {"3.json": "boom"} if "3.json" in filenames else {}}
    monkeypatch.setattr(dive_cache, "download_garmin_fits", fake_fits)

    scheduler.run_download_thread(True, str(tmp_path), services=["garmin"], refresh_fits=True)
    assert calls == [("a", ["1.json"]), ("b", ["3.json"])]
    assert scheduler.last_download_results["fit"] == {"downloaded": ["1.json"], "failed": {"3.json": "boom"}}
    assert scheduler.last_download_results["success"] is False
    calls.clear()
    scheduler.run_download_thread(False, str(tmp_path), services=["garmin"])      # a plain refresh leaves FITs alone
    assert calls == [] and scheduler.last_download_results == {"success": True}


def test_run_sync_thread_passes_mirror_and_delete_choice(monkeypatch):
    from src.core.sync_engine import SyncEngine
    seen = {}
    monkeypatch.setattr(SyncEngine, "__init__", lambda self, *a, **k: setattr(self, "run_overrides", {}))
    monkeypatch.setattr(SyncEngine, "run_sync", lambda self, dry_run=False, **kw: seen.update(kw) or {})
    scheduler.run_sync_thread(True, {"directionality": "to_divelogs", "propagate_deletes": False})
    assert seen["propagate_deletes_override"] is False and "mirror_override" not in seen
    seen.clear()
    scheduler.run_sync_thread(True, {"directionality": "to_divelogs", "mirror": True, "propagate_deletes": False})
    assert seen["mirror_override"] is True


def test_overwrite_only_refetches_garmin(tmp_path):
    """Unticking "Use cached Garmin dives" re-fetches every Garmin dive; the
    other services are downloaded in full either way, so their local copy is
    never emptied first (an interrupted download would leave nothing)."""
    import json
    from src.core.sync_engine import SyncEngine
    (tmp_path / "credentials.json").write_text(json.dumps({}))
    for name in ("garmin", "divelogs"):
        (tmp_path / name / "default" / "data").mkdir(parents=True)
        (tmp_path / name / "default" / "data" / "1.json").write_text("{}")
    engine = SyncEngine(settings_path=str(tmp_path / "settings.json"), credentials_path=str(tmp_path / "credentials.json"))
    engine.download_and_save_raw_data(mock_data_dir=str(tmp_path), overwrite=True)
    assert not (tmp_path / "garmin" / "default" / "data" / "1.json").exists()
    assert (tmp_path / "divelogs" / "default" / "data" / "1.json").exists()


def test_scheduled_job_with_source_and_target(monkeypatch):
    """A job saved from the web dashboard names its two sides; the scheduler
    runs the pair between them, writing the target, with the job's options."""
    from src.core import pairs
    seen = {}

    class Engine:
        run_overrides = {}
        def run_sync(self, dry_run=False, **kw):
            seen["run"] = kw
            return {}

    monkeypatch.setattr(pairs, "engine_for", lambda s, t, **kw: seen.update(sides=(s, t)) or Engine())
    job = CronJobModel(id="garmin-to-subsurface-daily", source="garmin", target="subsurface-cloud",
                       directionality="to_subsurface", frequency="daily", hour=6, use_garmin_cache=False)
    scheduler.run_sync_thread(False, job.model_dump())
    assert seen["sides"] == ("garmin", "subsurface-cloud")
    assert seen["run"]["direction_override"] == "to_subsurface" and seen["run"]["use_garmin_cache_override"] is False


def test_run_download_thread_one_service_with_several_accounts_on_both(monkeypatch, tmp_path):
    """A dives page refreshes one service's picked account. With two
    accounts on the *other* service too, the engine must not fail asking
    which of those to use - that side is never logged in to."""
    from src.core.config import CredentialsModel, DivelogsCredentials, GarminCredentials
    from src.core.sync_engine import SyncEngine

    creds = CredentialsModel(
        garmin=[GarminCredentials(username="live-g", password="x"), GarminCredentials(username="test-g", password="x")],
        divelogs=[DivelogsCredentials(username="live-d", password="x"), DivelogsCredentials(username="test-d", password="x")],
    )
    monkeypatch.setattr(scheduler.ConfigManager, "load_credentials", staticmethod(lambda path=None: creds))
    monkeypatch.setattr("src.core.sync_engine.ConfigManager.load_credentials", staticmethod(lambda path=None: creds))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    used = []
    monkeypatch.setattr(SyncEngine, "download_and_save_raw_data",
                        lambda self, mock_data_dir, overwrite, include_garmin, include_divelogs:
                            used.append((self.garmin_username, self.divelogs_username)) or True)

    scheduler.run_download_thread(base_dir=str(tmp_path), services=["garmin"], accounts={"garmin": "test-g"})
    assert scheduler.last_download_results == {"success": True}
    scheduler.run_download_thread(base_dir=str(tmp_path), services=["divelogs"], accounts={"divelogs": "test-d"})
    assert scheduler.last_download_results == {"success": True}
    assert used == [("test-g", "live-d"), ("live-g", "test-d")]
