from fastapi.testclient import TestClient

from src.web.app import app
import src.core.scheduler as scheduler


def test_trigger_sync(monkeypatch):
    client = TestClient(app)

    called = []
    monkeypatch.setattr(scheduler, "run_sync_thread", lambda dry_run, custom_settings=None: called.append((dry_run, custom_settings)))

    res = client.post("/api/sync/trigger", json={"dry_run": True})
    assert res.status_code == 200
    assert res.json()["status"] == "success"


def test_trigger_sync_rejects_when_already_running(monkeypatch):
    client = TestClient(app)
    monkeypatch.setattr(scheduler, "is_sync_running", True)

    res = client.post("/api/sync/trigger")
    assert res.status_code == 409


def test_get_status():
    client = TestClient(app)
    res = client.get("/api/status")
    assert res.status_code == 200
    data = res.json()
    assert "is_running" in data
    assert "last_results" in data
    assert "next_scheduled_run" in data


def test_credentials_api(tmp_path, monkeypatch):
    client = TestClient(app)
    creds_file = str(tmp_path / "credentials.json")

    import src.core.config
    monkeypatch.setattr(src.core.config, "CREDENTIALS_FILE", creds_file)

    # Save empty credentials
    empty_creds = src.core.config.CredentialsModel()
    src.core.config.ConfigManager.save_credentials(empty_creds, creds_file)

    # Patch load_credentials default path or method
    original_load = src.core.config.ConfigManager.load_credentials
    original_save = src.core.config.ConfigManager.save_credentials
    monkeypatch.setattr(src.core.config.ConfigManager, "load_credentials", lambda path=creds_file: original_load(creds_file))
    monkeypatch.setattr(src.core.config.ConfigManager, "save_credentials", lambda creds, path=creds_file: original_save(creds, creds_file))

    # Get initial status
    res = client.get("/api/credentials/status")
    assert res.status_code == 200
    assert res.json()["garmin_configured"] is False
    assert res.json()["divelogs_configured"] is False

    # Save credentials
    save_payload = {
        "garmin_username": "test@garmin.com",
        "garmin_password": "garminpassword",
        "garmin_token_dir": "tokens/garmin",
        "divelogs_username": "test_divelogs",
        "divelogs_password": "divelogspassword"
    }
    res = client.post("/api/credentials", json=save_payload)
    assert res.status_code == 200
    assert res.json()["status"] == "success"

    # Check status after saving
    res = client.get("/api/credentials/status")
    assert res.status_code == 200
    assert res.json()["garmin_configured"] is True
    assert res.json()["divelogs_configured"] is True
    assert res.json()["garmin_username"] == "test@garmin.com"
    assert res.json()["divelogs_username"] == "test_divelogs"
    assert res.json()["garmin_accounts"] == ["test@garmin.com"]
    assert res.json()["divelogs_accounts"] == ["test_divelogs"]


def test_credentials_test_api(monkeypatch):
    client = TestClient(app)

    from src.core.services.garmin import GarminAdapter
    from src.core.services.divelogs import DivelogsAdapter

    monkeypatch.setattr(GarminAdapter, "login", lambda self: True)
    monkeypatch.setattr(DivelogsAdapter, "login", lambda self: True)

    payload = {
        "garmin_username": "user",
        "garmin_password": "pass",
        "divelogs_username": "user",
        "divelogs_password": "pass"
    }
    res = client.post("/api/credentials/test", json=payload)
    assert res.status_code == 200
    assert res.json()["garmin"] is True
    assert res.json()["divelogs"] is True


def test_cron_jobs_api(tmp_path, monkeypatch):
    import src.core.config
    client = TestClient(app)

    settings_file = str(tmp_path / "settings.json")
    original_load = src.core.config.ConfigManager.load_settings
    original_save = src.core.config.ConfigManager.save_settings
    monkeypatch.setattr(src.core.config.ConfigManager, "load_settings", lambda path=settings_file: original_load(settings_file))
    monkeypatch.setattr(src.core.config.ConfigManager, "save_settings", lambda settings, path=settings_file: original_save(settings, settings_file))

    # 1. Get initial settings (defaults)
    res = client.get("/api/settings")
    assert res.status_code == 200
    assert "cron_jobs" in res.json()
    assert res.json()["cron_jobs"] == []

    # 2. Save settings with a cron job
    save_payload = {
        "directionality": "bidirectional",
        "sync_filters": {
            "date_from": "2026-01-01",
            "date_to": "2026-12-31",
            "only_new": True,
            "sync_gases": True,
            "sync_fit": False
        },
        "grace_window_minutes": 15,
        "api_cooldown_seconds": 1.0,
        "schedule": [],
        "cron_jobs": [
            {
                "id": "my-custom-job",
                "directionality": "to_divelogs",
                "frequency": "daily",
                "hour": 12,
                "minute": 30,
                "day_of_week": 0,
                "interval_minutes": 60,
                "only_new": True,
                "sync_gases": False,
                "sync_fit": True,
                "enabled": True
            }
        ]
    }

    res = client.post("/api/settings", json=save_payload)
    assert res.status_code == 200
    assert res.json()["status"] == "success"

    # 3. Verify it was saved
    res = client.get("/api/settings")
    assert res.status_code == 200
    cron_jobs = res.json()["cron_jobs"]
    assert len(cron_jobs) == 1
    assert cron_jobs[0]["id"] == "my-custom-job"
    assert cron_jobs[0]["directionality"] == "to_divelogs"
    assert cron_jobs[0]["frequency"] == "daily"
    assert cron_jobs[0]["hour"] == 12
    assert cron_jobs[0]["minute"] == 30
    assert cron_jobs[0]["sync_gases"] is False
    assert cron_jobs[0]["sync_fit"] is True
    assert cron_jobs[0]["enabled"] is True

    # 4. Status reflects the next scheduled run once a job is configured
    res = client.get("/api/status")
    assert res.status_code == 200
    assert res.json()["next_scheduled_run"] is not None


def test_fields_api():
    client = TestClient(app)
    res = client.get("/api/fields")
    assert res.status_code == 200
    pairs = res.json()["pairs"]
    assert len(pairs) == 1
    pair = pairs[0]
    assert (pair["source"], pair["target"]) == ("garmin", "divelogs")
    garmin_keys = {f["key"] for f in pair["fields"]["garmin"]}
    divelogs_keys = {f["key"] for f in pair["fields"]["divelogs"]}
    assert {"garmin.buddy", "garmin.activityName", "garmin.tanks"} <= garmin_keys
    assert {"divelogs.buddy", "divelogs.divesite", "divelogs.tanks"} <= divelogs_keys
    tanks = next(f for f in pair["fields"]["garmin"] if f["key"] == "garmin.tanks")
    assert tanks["writable"] is False and tanks["type"] == "tanks"


def _isolated_settings(tmp_path, monkeypatch):
    import src.core.config
    settings_file = str(tmp_path / "settings.json")
    original_load = src.core.config.ConfigManager.load_settings
    original_save = src.core.config.ConfigManager.save_settings
    monkeypatch.setattr(src.core.config.ConfigManager, "load_settings", lambda path=settings_file: original_load(settings_file))
    monkeypatch.setattr(src.core.config.ConfigManager, "save_settings", lambda settings, path=settings_file: original_save(settings, settings_file))


def _base_settings_payload():
    return {
        "directionality": "bidirectional",
        "sync_filters": {"date_from": None, "date_to": None, "only_new": True, "sync_gases": True, "sync_fit": False},
        "grace_window_minutes": 15,
        "api_cooldown_seconds": 1.0,
        "schedule": [],
        "cron_jobs": [],
    }


def test_settings_api_carries_field_links(tmp_path, monkeypatch):
    _isolated_settings(tmp_path, monkeypatch)
    client = TestClient(app)

    # Defaults come back with the shipped board
    res = client.get("/api/settings")
    assert res.status_code == 200
    default_ids = [l["id"] for l in res.json()["field_links"]]
    assert "buddy" in default_ids and "dive_number" in default_ids

    # Saving an edited board keeps it
    payload = _base_settings_payload()
    payload["field_links"] = [
        {"id": "buddy", "source": ["garmin.buddy"], "target": "divelogs.buddy", "direction": "to_target", "conflict": "prefer_non_empty"},
    ]
    res = client.post("/api/settings", json=payload)
    assert res.status_code == 200, res.text
    links = client.get("/api/settings").json()["field_links"]
    assert len(links) == 1 and links[0]["conflict"] == "prefer_non_empty"

    # A settings form that omits field_links does not wipe the board
    res = client.post("/api/settings", json=_base_settings_payload())
    assert res.status_code == 200
    links = client.get("/api/settings").json()["field_links"]
    assert len(links) == 1 and links[0]["id"] == "buddy"

    # An invalid board is refused with the reasons
    payload["field_links"] = [{"id": "bad", "source": ["garmin.buddy"], "target": "divelogs.max_depth"}]
    res = client.post("/api/settings", json=payload)
    assert res.status_code == 400
    assert "cannot link garmin.buddy (text) to divelogs.max_depth (number)" in res.json()["detail"]["errors"][0]
    # ... and the previous board is untouched
    assert client.get("/api/settings").json()["field_links"][0]["id"] == "buddy"

    # Cron jobs may carry their own board, which is validated too
    payload = _base_settings_payload()
    payload["cron_jobs"] = [{
        "id": "job", "directionality": "to_divelogs", "frequency": "daily", "hour": 1, "minute": 0,
        "day_of_week": 0, "interval_minutes": 60, "only_new": True, "sync_gases": True, "sync_fit": False,
        "enabled": True, "field_links": [{"id": "x", "source": ["garmin.nope"], "target": "divelogs.buddy"}],
    }]
    res = client.post("/api/settings", json=payload)
    assert res.status_code == 400
    assert res.json()["detail"]["errors"][0].startswith("Job 'job':")
