import json

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


def test_trigger_sync_passes_account_overrides(monkeypatch):
    client = TestClient(app)

    called = []
    monkeypatch.setattr(scheduler, "run_sync_thread", lambda dry_run, custom_settings=None: called.append((dry_run, custom_settings)))

    res = client.post("/api/sync/trigger", json={"dry_run": True, "garmin_username": "alice", "divelogs_username": "bob"})
    assert res.status_code == 200
    assert called[0][1]["garmin_username"] == "alice"
    assert called[0][1]["divelogs_username"] == "bob"


def test_trigger_sync_rejects_when_already_running(monkeypatch):
    client = TestClient(app)
    monkeypatch.setattr(scheduler, "is_sync_running", True)

    res = client.post("/api/sync/trigger")
    assert res.status_code == 409


def test_run_job_now_runs_the_saved_job_as_the_scheduler_would(tmp_path, monkeypatch):
    import time
    from src.core import scheduler
    c = _web_client(tmp_path, monkeypatch, {"garmin": {"username": "g", "password": "p"},
                                            "divelogs": {"username": "d", "password": "p"}})
    from src.core.config import ConfigManager, CronJobModel
    settings = ConfigManager.load_settings()
    settings.cron_jobs = [CronJobModel(id="nightly", frequency="daily", hour=6, only_new=False, enabled=False,
                                       source="divelogs", target="garmin", directionality="to_garmin")]
    ConfigManager.save_settings(settings)
    seen = {}
    monkeypatch.setattr(scheduler, "run_sync_thread", lambda dry_run, custom=None: seen.update(dry_run=dry_run, custom=custom))
    monkeypatch.setattr(scheduler, "is_sync_running", False)
    assert c.post("/api/jobs/nightly/run").status_code == 200
    time.sleep(0.2)
    # the whole saved job, as the scheduler loop passes it; a disabled job runs too
    assert seen["dry_run"] is False and seen["custom"]["id"] == "nightly"
    assert (seen["custom"]["source"], seen["custom"]["target"]) == ("divelogs", "garmin")
    assert seen["custom"]["only_new"] is False and seen["custom"]["directionality"] == "to_garmin"
    assert c.post("/api/jobs/no-such-job/run").status_code == 404
    monkeypatch.setattr(scheduler, "is_sync_running", True)
    assert c.post("/api/jobs/nightly/run").status_code == 409


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
        "garmin_accounts": [{"username": "test@garmin.com", "password": "garminpassword", "token_dir": "tokens/garmin"}],
        "divelogs_accounts": [{"username": "test_divelogs", "password": "divelogspassword"}],
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

    # A second account can be added without retyping the first one's password
    save_payload = {
        "garmin_accounts": [
            {"username": "test@garmin.com", "password": "", "token_dir": "tokens/garmin"},
            {"username": "second@garmin.com", "password": "secondpw", "token_dir": "tokens/garmin"},
        ],
        "divelogs_accounts": [{"username": "test_divelogs", "password": "divelogspassword"}],
    }
    res = client.post("/api/credentials", json=save_payload)
    assert res.status_code == 200
    stored = original_load(creds_file)
    stored_by_username = {a.username: a for a in stored.get_garmin_accounts()}
    assert stored_by_username["test@garmin.com"].password == "garminpassword"
    assert stored_by_username["second@garmin.com"].password == "secondpw"


def test_credentials_test_api(monkeypatch):
    client = TestClient(app)

    from src.core.services.garmin import GarminAdapter
    from src.core.services.divelogs import DivelogsAdapter

    monkeypatch.setattr(GarminAdapter, "login", lambda self: True)
    monkeypatch.setattr(DivelogsAdapter, "login", lambda self: True)

    payload = {
        "garmin_accounts": [{"username": "user", "password": "pass"}],
        "divelogs_accounts": [{"username": "user", "password": "pass"}],
    }
    res = client.post("/api/credentials/test", json=payload)
    assert res.status_code == 200
    assert res.json()["garmin"] == [{"username": "user", "ok": True}]
    assert res.json()["divelogs"] == [{"username": "user", "ok": True}]


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
        "directionality": "to_divelogs",
        "sync_filters": {
            "date_from": "2026-01-01",
            "date_to": "2026-12-31",
            "only_new": True,
            "sync_gases": True,
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
                "enabled": True,
                "garmin_username": "alice",
                "divelogs_username": "bob"
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
    assert cron_jobs[0]["enabled"] is True
    assert cron_jobs[0]["garmin_username"] == "alice"
    assert cron_jobs[0]["divelogs_username"] == "bob"

    # 4. Status reflects the next scheduled run once a job is configured
    res = client.get("/api/status")
    assert res.status_code == 200
    assert res.json()["next_scheduled_run"] is not None


def test_fields_api(tmp_path, monkeypatch):
    _isolated_settings(tmp_path, monkeypatch)
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


def test_create_on_garmin_round_trips(tmp_path, monkeypatch):
    _isolated_settings(tmp_path, monkeypatch)
    client = TestClient(app)

    assert client.get("/api/settings").json()["create_on_garmin"] is False  # off by default

    payload = _base_settings_payload()
    payload["create_on_garmin"] = True
    res = client.post("/api/settings", json=payload)
    assert res.status_code == 200
    assert client.get("/api/settings").json()["create_on_garmin"] is True


def test_notify_url_round_trips_and_is_kept_when_omitted(tmp_path, monkeypatch):
    _isolated_settings(tmp_path, monkeypatch)
    client = TestClient(app)

    payload = _base_settings_payload()
    payload["notify_url"] = "https://ntfy.sh/mytopic"
    res = client.post("/api/settings", json=payload)
    assert res.status_code == 200
    assert client.get("/api/settings").json()["notify_url"] == "https://ntfy.sh/mytopic"

    # Omitting it keeps what's stored (same rule as field_links/sync_pairs)
    res = client.post("/api/settings", json=_base_settings_payload())
    assert res.status_code == 200
    assert client.get("/api/settings").json()["notify_url"] == "https://ntfy.sh/mytopic"

    # An explicit "" clears it
    payload["notify_url"] = ""
    res = client.post("/api/settings", json=payload)
    assert res.status_code == 200
    assert client.get("/api/settings").json()["notify_url"] == ""


def test_notify_on_round_trips_is_kept_when_omitted_and_checked(tmp_path, monkeypatch):
    """R24: notify_on defaults to 'failure', is saved by the alerts form,
    kept by a form that omits it, and an unknown value is refused."""
    _isolated_settings(tmp_path, monkeypatch)
    client = TestClient(app)
    assert client.get("/api/settings").json()["notify_on"] == "failure"

    payload = _base_settings_payload()
    payload["notify_url"] = "https://ntfy.sh/mytopic"
    payload["notify_on"] = "changes"
    assert client.post("/api/settings", json=payload).status_code == 200
    assert client.get("/api/settings").json()["notify_on"] == "changes"

    assert client.post("/api/settings", json=_base_settings_payload()).status_code == 200
    assert client.get("/api/settings").json()["notify_on"] == "changes"

    payload["notify_on"] = "sometimes"
    assert client.post("/api/settings", json=payload).status_code == 422
    assert client.get("/api/settings").json()["notify_on"] == "changes"


def test_dashboard_offers_when_to_send_alerts():
    client = TestClient(app)
    html = client.get("/").text
    assert 'id="notify-on"' in html
    for value in ("failure", "changes", "always"):
        assert f'<option value="{value}">' in html
    with open("src/web/static/script.js") as f:
        js = f.read()
    assert 'notify_on: $("notify-on").value' in js


def test_notify_test_endpoint(monkeypatch):
    client = TestClient(app)
    from src.core import notify
    monkeypatch.setattr(notify, "send_notification", lambda url, title, message: (True, "Sent (HTTP 200)."))

    res = client.post("/api/notify/test", json={"notify_url": "https://ntfy.sh/mytopic"})
    assert res.status_code == 200
    assert res.json() == {"ok": True, "detail": "Sent (HTTP 200)."}


def _isolated_settings(tmp_path, monkeypatch):
    import src.core.config
    settings_file = str(tmp_path / "settings.json")
    original_load = src.core.config.ConfigManager.load_settings
    original_save = src.core.config.ConfigManager.save_settings
    monkeypatch.setattr(src.core.config.ConfigManager, "load_settings", lambda path=settings_file: original_load(settings_file))
    monkeypatch.setattr(src.core.config.ConfigManager, "save_settings", lambda settings, path=settings_file: original_save(settings, settings_file))


def _base_settings_payload():
    return {
        "directionality": "to_divelogs",
        "sync_filters": {"date_from": None, "date_to": None, "only_new": True, "sync_gases": True},
        "grace_window_minutes": 15,
        "api_cooldown_seconds": 1.0,
        "schedule": [],
        "cron_jobs": [],
    }


def _default_rules(client):
    return client.get("/api/settings").json()["sync_pairs"][0]["rules"]


def test_settings_api_accepts_a_version_1_board(tmp_path, monkeypatch):
    """A client may still post the garmin_divelogs board as ``field_links``
    (version 1); it is stored as rules and read back as such (rework.md G7)."""
    _isolated_settings(tmp_path, monkeypatch)
    client = TestClient(app)

    # Defaults come back with the shipped board, as rules
    res = client.get("/api/settings")
    assert res.status_code == 200 and "field_links" not in res.json()
    rules = _default_rules(client)
    assert {r["id"] for r in rules["divelogs"]} >= {"buddy", "site"}

    # Saving an edited board keeps it
    payload = _base_settings_payload()
    payload["field_links"] = [
        {"id": "buddy", "source": ["garmin.buddy"], "target": "divelogs.buddy", "direction": "to_target", "conflict": "prefer_non_empty"},
    ]
    res = client.post("/api/settings", json=payload)
    assert res.status_code == 200, res.text
    rules = _default_rules(client)
    assert list(rules) == ["divelogs"] and len(rules["divelogs"]) == 1 and rules["divelogs"][0]["conflict"] == "prefer_non_empty"

    # A settings form that omits field_links does not wipe the board
    res = client.post("/api/settings", json=_base_settings_payload())
    assert res.status_code == 200
    rules = _default_rules(client)
    assert len(rules["divelogs"]) == 1 and rules["divelogs"][0]["id"] == "buddy"

    # An invalid board is refused with the reasons
    payload["field_links"] = [{"id": "bad", "source": ["garmin.buddy"], "target": "divelogs.max_depth"}]
    res = client.post("/api/settings", json=payload)
    assert res.status_code == 400
    assert "cannot link garmin.buddy (text) to divelogs.max_depth (number)" in res.json()["detail"]["errors"][0]
    # ... and the previous board is untouched
    assert _default_rules(client)["divelogs"][0]["id"] == "buddy"

    # Cron jobs may carry their own board, which is validated too
    payload = _base_settings_payload()
    payload["cron_jobs"] = [{
        "id": "job", "directionality": "to_divelogs", "frequency": "daily", "hour": 1, "minute": 0,
        "day_of_week": 0, "interval_minutes": 60, "only_new": True, "sync_gases": True,
        "enabled": True, "field_links": [{"id": "x", "source": ["garmin.nope"], "target": "divelogs.buddy"}],
    }]
    res = client.post("/api/settings", json=payload)
    assert res.status_code == 400
    assert res.json()["detail"]["errors"][0].startswith("Job 'job':")


def test_fields_preview_api_and_template_validation_on_save(tmp_path, monkeypatch):
    client = TestClient(app)
    link = {"id": "site", "source": ["divelogs.location", "divelogs.divesite"], "target": "garmin.activityName",
            "direction": "to_target", "template": "{divelogs.divesite} ({divelogs.location})"}
    res = client.post("/api/fields/preview", json={"link": link})
    assert res.status_code == 200 and res.json()["ok"] and res.json()["text"] == "Zenobia (Larnaca)"
    bad = dict(link, template="{nope}")
    res = client.post("/api/fields/preview", json={"link": bad})
    assert res.status_code == 200 and not res.json()["ok"] and "unknown field {nope}" in res.json()["problems"][0]

    _isolated_settings(tmp_path, monkeypatch)
    payload = _base_settings_payload()
    payload["field_links"] = [bad]
    res = client.post("/api/settings", json=payload)
    assert res.status_code == 400 and "unknown field {nope}" in res.json()["detail"]["errors"][0]
    payload["field_links"] = [link]
    assert client.post("/api/settings", json=payload).status_code == 200


def test_credentials_api_preserves_subsurface(tmp_path, monkeypatch):
    import src.core.config
    client = TestClient(app)
    creds_file = str(tmp_path / "credentials.json")
    original_load = src.core.config.ConfigManager.load_credentials
    original_save = src.core.config.ConfigManager.save_credentials
    monkeypatch.setattr(src.core.config.ConfigManager, "load_credentials", lambda path=creds_file: original_load(creds_file))
    monkeypatch.setattr(src.core.config.ConfigManager, "save_credentials", lambda creds, path=creds_file: original_save(creds, creds_file))

    subsurface = {"email": "me@x.org", "password": "pw"}
    res = client.post("/api/credentials", json={"garmin_accounts": [{"username": "g", "password": "p"}],
                                                 "subsurface": subsurface})
    assert res.status_code == 200
    status = client.get("/api/credentials/status").json()
    assert status["subsurface_configured"] and status["subsurface_email"] == "me@x.org"
    assert "password" not in status

    # a Garmin/Divelogs-only save (the current form) keeps them
    res = client.post("/api/credentials", json={"garmin_accounts": [{"username": "g2", "password": "p"}]})
    assert res.status_code == 200
    status = client.get("/api/credentials/status").json()
    assert status["garmin_username"] == "g2" and status["subsurface_configured"]

    from src.core.services import subsurface_cloud
    monkeypatch.setattr(subsurface_cloud, "check_cloud_login", lambda e, p, b: (True, "cloud ok"))
    res = client.post("/api/credentials/test", json={"subsurface": subsurface})
    assert res.status_code == 200
    body = res.json()
    assert body["subsurface"] is True and body["subsurface_message"] == "cloud ok"
    assert body["garmin"] is None


def test_old_submersion_entries_are_ignored_by_the_api(tmp_path, monkeypatch):
    """plans/subsurface-submersion.md F18: Submersion support is gone. A
    credentials file or a page from an older build that still carries a
    Submersion store, a Submersion pair or create_device_dives_on_submersion
    is accepted; the Submersion parts are dropped, the rest is kept."""
    import src.core.config
    _isolated_settings(tmp_path, monkeypatch)
    creds_file = str(tmp_path / "credentials.json")
    original_load = src.core.config.ConfigManager.load_credentials
    original_save = src.core.config.ConfigManager.save_credentials
    monkeypatch.setattr(src.core.config.ConfigManager, "load_credentials", lambda path=creds_file: original_load(creds_file))
    monkeypatch.setattr(src.core.config.ConfigManager, "save_credentials", lambda creds, path=creds_file: original_save(creds, creds_file))
    store = {"endpoint_url": "https://s3.eu-central-003.backblazeb2.com", "bucket": "dives",
             "access_key_id": "id", "secret_access_key": "key"}
    with open(creds_file, "w") as f:
        json.dump({"garmin": {"username": "g", "password": "p"}, "submersion": store}, f)
    client = TestClient(app)

    status = client.get("/api/credentials/status").json()
    assert status["garmin_username"] == "g" and not any("submersion" in key for key in status)
    res = client.post("/api/credentials", json={"garmin_accounts": [{"username": "g", "password": ""}],
                                                 "submersion": store})
    assert res.status_code == 200
    saved = json.load(open(creds_file))
    assert "submersion" not in saved and saved["garmin"][0]["password"] == "p"
    body = client.post("/api/credentials/test", json={"submersion": store}).json()
    assert "submersion" not in body

    payload = _base_settings_payload()
    payload["create_device_dives_on_submersion"] = True
    payload["sync_pairs"] = [{"id": "g2s", "source": "garmin", "target": "submersion", "directionality": "to_target"},
                             {"id": "g2ss", "source": "garmin", "target": "subsurface-cloud", "directionality": "to_target"}]
    payload["cron_jobs"] = [{"id": "to-submersion", "pair": "g2s", "frequency": "daily", "hour": 3, "minute": 0,
                             "day_of_week": 0, "interval_minutes": 60, "only_new": True, "sync_gases": True,
                             "enabled": True}]
    res = client.post("/api/settings", json=payload)
    assert res.status_code == 200, res.text
    data = client.get("/api/settings").json()
    assert [p["id"] for p in data["sync_pairs"]] == ["garmin_divelogs", "g2ss"]
    assert data["cron_jobs"] == [] and "create_device_dives_on_submersion" not in data


# ---------------------------------------------------------------- phase 6 endpoints

class _FakeEngine:
    """Stands in for SyncEngine in the board endpoints: no logins, no fetches."""
    source_id, target_id = "garmin", "divelogs"
    run_overrides = {}

    def __init__(self):
        self.full_compare = None
        self.resolved = []

    def test_mapping(self, links=None, limit=10, rules=None, match_keys=None):
        self.seen = {"links": links, "rules": rules, "match_keys": match_keys}
        first = (links or [None])[0].id if links else (next(iter(rules.values()))[0].id if rules else "buddy")
        return {"ok": True, "problems": [], "matched": 1, "fetched": {"garmin": limit, "divelogs": limit},
                "rows": [{"link": first, "result": "equal"}]}

    def list_conflicts(self):
        from src.core.conflicts import Conflict
        return [Conflict(id="abc123", link_id="notes", source_service="garmin", target_service="divelogs",
                         source_key="garmin.notes", target_key="divelogs.notes", field_type="text",
                         source_value="A", target_value="B", dive_ids={"garmin": "1", "divelogs": "2"})]

    def resolve_conflict(self, conflict_id, winner):
        if conflict_id != "abc123":
            raise ValueError("No conflict with that id")
        self.resolved.append((conflict_id, winner))
        return self.list_conflicts()[0]

    def request_full_compare(self, on=True):
        self.full_compare = on


def test_mapping_test_conflicts_and_full_compare_endpoints(monkeypatch):
    import src.web.app as web
    client = TestClient(app)
    fake = _FakeEngine()
    monkeypatch.setattr(web, "_engine_for_pair_id", lambda pair_id: fake)

    res = client.post("/api/mapping/test", json={"field_links": [
        {"id": "buddy", "source": ["garmin.buddy"], "target": "divelogs.buddy"}], "limit": 5})
    assert res.status_code == 200 and res.json()["rows"][0]["link"] == "buddy" and res.json()["fetched"]["garmin"] == 5

    monkeypatch.setattr(scheduler, "is_sync_running", True)
    assert client.post("/api/mapping/test", json={}).status_code == 409
    assert client.post("/api/conflicts/abc123/resolve", json={"winner": "source"}).status_code == 409
    monkeypatch.setattr(scheduler, "is_sync_running", False)

    res = client.get("/api/conflicts")
    assert res.status_code == 200 and res.json()["conflicts"][0]["id"] == "abc123" and res.json()["source"] == "garmin"
    res = client.post("/api/conflicts/abc123/resolve", json={"winner": "target"})
    assert res.status_code == 200 and fake.resolved == [("abc123", "target")]
    assert client.post("/api/conflicts/nope/resolve", json={"winner": "target"}).status_code == 400

    res = client.post("/api/sync/full-compare")
    assert res.status_code == 200 and fake.full_compare is True


def test_resolve_conflicts_batch_endpoint(monkeypatch):
    """Save changes on the Conflicts page: one engine per pair, every pick
    attempted, per-pick outcome so failed picks can stay staged."""
    import src.web.app as web
    client = TestClient(app)
    engines = {}

    def factory(pair_id):
        if pair_id == "broken":
            raise ValueError("No sync pair named 'broken'")
        return engines.setdefault(pair_id or "default", _FakeEngine())
    monkeypatch.setattr(web, "_engine_for_pair_id", factory)

    monkeypatch.setattr(scheduler, "is_sync_running", True)
    assert client.post("/api/conflicts/resolve", json={"decisions": [{"id": "abc123", "winner": "source"}]}).status_code == 409
    monkeypatch.setattr(scheduler, "is_sync_running", False)

    res = client.post("/api/conflicts/resolve", json={"decisions": [
        {"id": "abc123", "winner": "source"},
        {"id": "nope", "winner": "target"},                       # unknown id: fails, the rest still run
        {"id": "abc123", "winner": "target", "pair": "other"},
        {"id": "abc123", "winner": "source", "pair": "other"},    # same pair reuses its engine
        {"id": "abc123", "winner": "source", "pair": "broken"},
    ]})
    assert res.status_code == 200
    body = res.json()
    assert (body["resolved"], body["failed"]) == (3, 2)
    assert [r["status"] for r in body["results"]] == ["resolved", "failed", "resolved", "resolved", "failed"]
    assert "No conflict" in body["results"][1]["detail"] and "broken" in body["results"][4]["detail"]
    assert engines["default"].resolved == [("abc123", "source")]
    assert engines["other"].resolved == [("abc123", "target"), ("abc123", "source")]
    assert set(engines) == {"default", "other"}

    res = client.post("/api/conflicts/resolve", json={"decisions": []})
    assert res.status_code == 200 and res.json() == {"resolved": 0, "failed": 0, "results": []}


def test_resolve_conflicts_batch_reports_progress(monkeypatch):
    """While Save runs, the Conflicts page polls /api/status for how many
    picks are done; the endpoint reports one step per pick, names the service
    being written, clears the slot when finished and refuses a second
    concurrent save."""
    import src.web.app as web
    from src.core import progress
    client = TestClient(app)
    seen = []
    fake = _FakeEngine()

    class Watching(_FakeEngine):
        def resolve_conflict(self, cid, winner):
            seen.append(progress.current())
            return fake.resolve_conflict(cid, winner)
    engine = Watching()
    monkeypatch.setattr(web, "_engine_for_pair_id", lambda pair_id: engine)
    monkeypatch.setattr(scheduler, "is_sync_running", False)
    progress.clear()

    res = client.post("/api/conflicts/resolve", json={
        "service": "Divelogs.org",
        "decisions": [{"id": "abc123", "winner": "source"}, {"id": "nope", "winner": "target"}, {"id": "abc123", "winner": "target"}]})
    assert res.status_code == 200 and res.json()["resolved"] == 2
    assert [(p["done"], p["total"]) for p in seen] == [(0, 3), (1, 3), (2, 3)]
    assert seen[0]["message"] == "Saving picks to Divelogs.org" and seen[0]["service"] == "Divelogs.org"
    assert progress.current() is None                           # slot freed for the next job

    # a stop request takes effect before the next pick and never leaks out
    seen.clear()
    def stopping(cid, winner):
        seen.append(cid)
        progress.request_stop()
    monkeypatch.setattr(engine, "resolve_conflict", stopping)
    res = client.post("/api/conflicts/resolve", json={"decisions": [
        {"id": "a", "winner": "source"}, {"id": "b", "winner": "source"}, {"id": "c", "winner": "source"}]})
    body = res.json()
    assert res.status_code == 200 and seen == ["a"] and (body["resolved"], body["failed"]) == (1, 2)
    assert "Stopped" in body["results"][1]["detail"] and not progress.stop_requested()

    # second concurrent save is refused rather than interleaved
    assert web._resolve_lock.acquire(blocking=False)
    try:
        res = client.post("/api/conflicts/resolve", json={"decisions": [{"id": "abc123", "winner": "source"}]})
        assert res.status_code == 409 and "already being saved" in res.json()["detail"]
    finally:
        web._resolve_lock.release()


def test_profile_export_and_import_endpoints(tmp_path, monkeypatch):
    import io
    import json as _json
    _isolated_settings(tmp_path, monkeypatch)
    client = TestClient(app)

    res = client.get("/api/settings/export")
    assert res.status_code == 200 and res.headers["content-disposition"].startswith("attachment")
    profile = res.json()
    assert profile["dive_sync_profile"] == 2 and "sync_pairs" in profile and "field_links" not in profile

    profile["grace_window_minutes"] = 45
    rules = profile["sync_pairs"][0]["rules"]
    for receiver in rules:
        rules[receiver] = [r for r in rules[receiver] if r["id"] in ("buddy", "notes")]
    upload = {"file": ("profile.json", io.BytesIO(_json.dumps(profile).encode()), "application/json")}
    res = client.post("/api/settings/import", files=upload)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["applied"] is False and "grace_window_minutes: 15 -> 45" in body["summary"]["changes"]
    assert client.get("/api/settings").json()["grace_window_minutes"] == 15   # nothing applied yet

    upload = {"file": ("profile.json", io.BytesIO(_json.dumps(profile).encode()), "application/json")}
    res = client.post("/api/settings/import?apply=true", files=upload)
    assert res.status_code == 200 and res.json()["applied"] is True
    saved = client.get("/api/settings").json()
    assert saved["grace_window_minutes"] == 45
    assert [r["id"] for r in saved["sync_pairs"][0]["rules"]["divelogs"]] == ["buddy", "notes"]

    bad = {"file": ("x.json", io.BytesIO(b"{not json"), "application/json")}
    assert client.post("/api/settings/import", files=bad).status_code == 400
    newer = {"file": ("x.json", io.BytesIO(_json.dumps({"dive_sync_profile": 99}).encode()), "application/json")}
    assert "newer" in client.post("/api/settings/import", files=newer).json()["detail"]


def test_about_and_version_endpoints(monkeypatch):
    from fastapi.testclient import TestClient
    from src.core import version as version_mod
    from src.web.app import app
    monkeypatch.setattr(version_mod.requests, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    version_mod.VERSION_CHECK_CACHE["last_checked"] = 0.0
    c = TestClient(app)
    about = c.get("/api/about").json()
    assert about["license_name"] == "MIT" and "Permission is hereby granted" in about["license_text"]
    assert about["version"] and about["version"] != "local-dev" and about["project_url"].startswith("https://")
    assert [r["name"] for r in about["references"]] == ["libdivecomputer", "Garmin FIT SDK"] and all(r["url"].startswith("https://") for r in about["references"])
    assert about["references"][1]["url"] == "https://developer.garmin.com/fit/download/" and "(FIT) Protocol License" in about["references"][1]["license"]
    assert any(c["name"] == "Garmin FIT SDK" and c["version"] for c in about["components"])
    assert 'id="about-references"' in c.get("/").text
    v = c.get("/api/version").json()                        # the keys the sidebar label reads
    assert v["current_version"] == about["version"] and "update_available" in v


def _web_client(tmp_path, monkeypatch, credentials=None):
    import json
    from fastapi.testclient import TestClient
    from src.core import config
    from src.web import app as webapp
    creds_path, settings_path = str(tmp_path / "credentials.json"), str(tmp_path / "settings.json")
    (tmp_path / "credentials.json").write_text(json.dumps(credentials or {}))
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)
    monkeypatch.setattr(config, "CREDENTIALS_FILE", creds_path)
    # the loaders bind their default path at import; point them here
    load_s, load_c, save_s = config.ConfigManager.load_settings, config.ConfigManager.load_credentials, config.ConfigManager.save_settings
    monkeypatch.setattr(config.ConfigManager, "load_settings", staticmethod(lambda path=None: load_s(path or settings_path)))
    monkeypatch.setattr(config.ConfigManager, "load_credentials", staticmethod(lambda path=None: load_c(path or creds_path)))
    monkeypatch.setattr(config.ConfigManager, "save_settings", staticmethod(lambda s, path=None: save_s(s, path or settings_path)))
    return TestClient(webapp.app)


def test_web_sync_now_takes_a_source_and_target_and_never_deletes(tmp_path, monkeypatch):
    from src.core import scheduler
    c = _web_client(tmp_path, monkeypatch, {"garmin": {"username": "g", "password": "p"},
                                            "divelogs": {"username": "d", "password": "p"},
                                            "subsurface": {"email": "me@x.org", "password": "p"}})
    ends = c.get("/api/sync/endpoints").json()
    assert [e["spec"] for e in ends["endpoints"]] == ["garmin", "divelogs", "subsurface-cloud"]
    seen = {}
    monkeypatch.setattr(scheduler, "run_sync_thread", lambda dry_run, custom=None: seen.update(custom=custom))
    monkeypatch.setattr(scheduler, "is_sync_running", False)
    assert c.post("/api/sync/trigger", json={"dry_run": True, "source": "divelogs", "target": "garmin"}).status_code == 200
    import time; time.sleep(0.2)
    assert seen["custom"]["pair"] == "garmin_divelogs" and seen["custom"]["directionality"] == "to_garmin"
    assert seen["custom"]["propagate_deletes"] is False
    c.post("/api/sync/trigger", json={"dry_run": True, "source": "subsurface-cloud", "target": "garmin"})
    time.sleep(0.2)
    assert (seen["custom"]["source"], seen["custom"]["target"]) == ("subsurface-cloud", "garmin")
    assert "pair" not in seen["custom"] and seen["custom"]["directionality"] == "to_garmin"
    assert c.post("/api/sync/trigger", json={"source": "garmin", "target": "garmin"}).status_code == 400


def test_web_jobs_with_source_and_target_and_all_conflicts(tmp_path, monkeypatch):
    from src.core.config import ConfigManager
    from src.core.conflicts import Conflict, ConflictStore
    c = _web_client(tmp_path, monkeypatch, {"garmin": {"username": "g", "password": "p"},
                                            "subsurface": {"email": "me@x.org", "password": "p"}})
    settings = c.get("/api/settings").json()
    settings["cron_jobs"] = [{"id": "garmin-to-subsurface-daily", "source": "garmin", "target": "subsurface-cloud",
                              "directionality": "to_subsurface", "frequency": "daily", "hour": 6, "minute": 0,
                              "day_of_week": 1, "interval_minutes": 60, "only_new": True, "sync_gases": True,
                              "enabled": True, "use_garmin_cache": False}]
    assert c.post("/api/settings", json=settings).status_code == 200
    job = ConfigManager.load_settings(str(tmp_path / "settings.json")).cron_jobs[0]
    assert (job.source, job.target, job.use_garmin_cache) == ("garmin", "subsurface-cloud", False)

    ConflictStore(str(tmp_path / "sync" / "conflicts_garmin_subsurface.json")).save([Conflict(
        id="c1", link_id="buddy", source_service="garmin", target_service="subsurface", source_key="garmin.buddy",
        target_key="subsurface.buddy", field_type="text", dive_ids={"garmin": "1", "subsurface": "x"},
        source_value="Anna", target_value="Bob", dive_time="2026-06-27 09:20:00")])
    groups = c.get("/api/conflicts/all").json()["groups"]
    assert [g["pair_id"] for g in groups] == ["garmin_subsurface"]
    item = groups[0]["conflicts"][0]
    assert (item["field_label"], item["source_text"], item["target_text"]) == ("Buddy", "Anna", "Bob")
    assert (item["source_name"], item["target_name"]) == ("Garmin Connect", "Subsurface Cloud")
    # Clear all (owner request 2026-09-30): the queue is emptied, nothing else touched
    assert c.delete("/api/conflicts").json() == {"cleared": 1}
    assert c.get("/api/conflicts/all").json()["groups"] == []
    assert c.delete("/api/conflicts").json() == {"cleared": 0}
    assert 'id="conflicts-clear"' in c.get("/").text


def test_stop_asks_the_running_job_to_stop(monkeypatch):
    from src.core import progress, scheduler
    client = TestClient(app)
    progress.clear()
    monkeypatch.setattr(scheduler, "is_sync_running", False)
    monkeypatch.setattr(scheduler, "is_download_running", False)
    assert client.post("/api/stop").status_code == 409       # nothing to stop
    assert not progress.stop_requested()
    monkeypatch.setattr(scheduler, "is_sync_running", True)
    try:
        assert client.post("/api/stop").json() == {"status": "stopping"}
        assert progress.stop_requested()
    finally:
        progress.clear()


def test_dashboard_has_the_stop_button():
    html = TestClient(app).get("/").text
    assert 'id="status-stop"' in html


def test_saving_settings_drops_conflicts_the_boards_no_longer_raise(tmp_path, monkeypatch):
    """Owner request 2026-09-27: a rule change clears its queued conflicts."""
    import os
    import src.core.config as config
    from src.core import layout
    from src.core.conflicts import Conflict, ConflictStore
    _isolated_settings(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    client = TestClient(app)

    def conflict(link, source_key, target_key):
        return Conflict(id=Conflict.make_id(link, "garmin", "divelogs", "1", "2"), link_id=link, source_service="garmin",
                        target_service="divelogs", source_external_id="1", target_external_id="2", source_key=source_key,
                        target_key=target_key, field_type="text", dive_ids={"garmin": "1", "divelogs": "2"},
                        source_value="A", target_value="B")
    store = ConflictStore(os.path.join(layout.sync_dir(str(tmp_path)), "conflicts_u_v.json"))
    store.save([conflict("buddy", "garmin.buddy", "divelogs.buddy"), conflict("bogus", "garmin.notes", "divelogs.notes")])

    # the default board keeps buddy manual: only the entry of a rule that does not exist goes
    assert client.post("/api/settings", json=_base_settings_payload()).status_code == 200
    assert [c.link_id for c in store.load()] == ["buddy"]

    payload = dict(_base_settings_payload(),
                   field_links=[{"id": "buddy", "source": ["garmin.buddy"], "target": "divelogs.buddy", "conflict": "source_wins"}])
    assert client.post("/api/settings", json=payload).status_code == 200
    assert store.load() == [] and os.path.exists(store.path)


def test_download_dives_endpoints(tmp_path, monkeypatch):
    """The Sync page's "Download dives": the pick list is every configured
    service, and the POST downloads exactly the ticked ones (owner request
    2026-09-29: one or several, not only all-or-one)."""
    import threading
    import src.core.config as config
    from src.core.config import CredentialsModel, GarminCredentials, SubsurfaceCredentials
    creds = CredentialsModel(garmin=[GarminCredentials(username="g@x", password="p")],
                             subsurface=[SubsurfaceCredentials(email="s@x", password="p")])
    monkeypatch.setattr(config.ConfigManager, "load_credentials", staticmethod(lambda path=None: creds))
    monkeypatch.setattr(scheduler, "is_sync_running", False)
    monkeypatch.setattr(scheduler, "is_download_running", False)
    started = threading.Event()
    calls = []
    monkeypatch.setattr(scheduler, "run_download_thread",
                        lambda **kw: (calls.append(kw), started.set()))
    client = TestClient(app)

    assert [s["id"] for s in client.get("/api/dives/services").json()["services"]] == ["garmin", "subsurface"]
    assert 'id="download-dives"' in client.get("/").text

    res = client.post("/api/dives/refresh", json={"services": ["subsurface", "garmin"], "overwrite": True,
                                                  "accounts": {"garmin": "g@x", "divelogs": ""}})
    assert res.json() == {"status": "started", "services": ["garmin", "subsurface"]}
    assert started.wait(5)
    assert calls == [{"overwrite": True, "services": ["garmin", "subsurface"], "accounts": {"garmin": "g@x"}}]

    assert client.post("/api/dives/refresh", json={"services": []}).status_code == 400
    assert "divelogs" in client.post("/api/dives/refresh", json={"services": ["divelogs"]}).json()["detail"]
    monkeypatch.setattr(scheduler, "is_download_running", True)
    assert client.post("/api/dives/refresh", json={"services": ["garmin"]}).status_code == 409
    assert client.get("/api/status").json()["last_download"] == scheduler.last_download_results


def test_credentials_api_has_no_shearwater_section(tmp_path, monkeypatch):
    """The Shearwater app's database is a desktop-app setting: the web form
    and its status do not show it, and saving the form leaves an entry
    stored in credentials.json alone (it still works as a sync side)."""
    import json
    import shutil
    import src.core.config as config
    from tests.test_shearwater import FIXTURE
    creds_file = str(tmp_path / "credentials.json")
    load_c, save_c = config.ConfigManager.load_credentials, config.ConfigManager.save_credentials
    monkeypatch.setattr(config.ConfigManager, "load_credentials", staticmethod(lambda path=None: load_c(creds_file)))
    monkeypatch.setattr(config.ConfigManager, "save_credentials", staticmethod(lambda creds, path=None: save_c(creds, creds_file)))
    client = TestClient(app)
    db = tmp_path / "dive_data.db"
    shutil.copy(FIXTURE, db)
    with open(creds_file, "w") as f:
        json.dump({"shearwater": [{"database": str(db)}]}, f)
    assert 'id="shearwater' not in client.get("/").text
    assert not any(key.startswith("shearwater") for key in client.get("/api/credentials/status").json())
    # a form save (even one from an old page still sending the section) keeps the stored entry
    res = client.post("/api/credentials", json={"garmin_accounts": [], "divelogs_accounts": [],
                                                "shearwater": {"database": ""}})
    assert res.status_code == 200
    assert [a.database for a in load_c(creds_file).saved_shearwater_accounts()] == [str(db)]
    assert any(e["id"] == "shearwater" for e in client.get("/api/sync/endpoints").json()["endpoints"])


def test_fields_preview_shows_the_take_sample():
    client = TestClient(app)
    link = {"id": "area", "source": ["garmin.activityName"], "target": "divelogs.location", "direction": "to_target",
            "conflict": "manual", "take": r"^(\w+)"}
    data = client.post("/api/fields/preview", json={"link": link}).json()
    assert data["ok"] and data["take_sample"] == data["text"].split(" ")[0]          # the example's first word
    link["take"] = "^(.+?),"                                                          # the example has no comma
    assert client.post("/api/fields/preview", json={"link": link}).json()["take_sample"] is None
    assert 'id="editor-take"' in client.get("/").text


# --- units (plans/units.md J6): app-local, never in the profile ----------------

def _depth_conflict(**kw):
    from src.core.conflicts import Conflict
    base = dict(id="d1", link_id="depth", source_service="garmin", target_service="divelogs",
                source_external_id="1", target_external_id="2", source_key="garmin.max_depth",
                target_key="divelogs.max_depth", source_type="number", field_type="number",
                dive_ids={"garmin": "1", "divelogs": "2"}, source_value=30.2, target_value=30.5,
                dive_time="2026-06-27 09:20:00")
    base.update(kw)
    return Conflict(**base)


def test_units_setting_is_app_local_and_never_in_the_profile(tmp_path, monkeypatch):
    import json
    from src.web import prefs
    c = _web_client(tmp_path, monkeypatch, {"garmin": {"username": "g", "password": "p"},
                                            "divelogs": {"username": "d", "password": "p"}})
    assert prefs.prefs_file() == str(tmp_path / "web_prefs.json")
    settings = c.get("/api/settings").json()
    assert settings["units"] == "metric"                       # the default, no file needed
    assert not (tmp_path / "web_prefs.json").exists()

    payload = dict(settings, units="imperial")
    assert c.post("/api/settings", json=payload).status_code == 200
    assert json.loads((tmp_path / "web_prefs.json").read_text()) == {"units": "imperial"}
    assert "units" not in (tmp_path / "settings.json").read_text()
    assert "units" not in json.loads((tmp_path / "settings.json").read_text())
    assert c.get("/api/settings").json()["units"] == "imperial"
    # the sync profile carries no units, so an export / import moves none
    profile = c.get("/api/settings/export").json()
    assert "units" not in profile and "units" not in json.dumps(profile)
    # omitted keeps what is stored; an unknown value falls back to metric
    payload.pop("units")
    assert c.post("/api/settings", json=payload).status_code == 200
    assert c.get("/api/settings").json()["units"] == "imperial"
    assert c.post("/api/settings", json=dict(payload, units="bogus")).status_code == 200
    assert c.get("/api/settings").json()["units"] == "metric"
    assert json.loads((tmp_path / "web_prefs.json").read_text()) == {"units": "metric"}
    # a broken file is the default, not an error
    (tmp_path / "web_prefs.json").write_text("{not json")
    assert c.get("/api/settings").json()["units"] == "metric"
    assert c.get("/api/settings").status_code == 200


def test_dashboard_units_select_matches_the_core_labels(tmp_path, monkeypatch):
    from src.core import units
    c = _web_client(tmp_path, monkeypatch)
    page = c.get("/").text
    assert 'id="units"' in page and 'id="api-cooldown"' in page
    for system, label in units.UNIT_LABELS.items():
        assert f'<option value="{system}">{label}</option>' in page


def test_conflicts_all_follow_the_units_setting_and_the_pick_stays_a_side(tmp_path, monkeypatch):
    from src.core.conflicts import ConflictStore
    from src.web import prefs
    c = _web_client(tmp_path, monkeypatch, {"garmin": {"username": "g", "password": "p"},
                                            "divelogs": {"username": "d", "password": "p"}})
    ConflictStore(str(tmp_path / "sync" / "conflicts.json")).save([
        _depth_conflict(),
        _depth_conflict(id="t1", link_id="temp", source_key="garmin.temp_min", target_key="divelogs.temp_min",
                        source_value=24.5, target_value=26.0),
        _depth_conflict(id="w1", link_id="weight", source_key="garmin.weight", target_key="divelogs.weight",
                        source_value=[4.0, "kilogram"], target_value=[10.0, "pound"]),
        _depth_conflict(id="n1", link_id="notes", source_key="garmin.notes", target_key="divelogs.notes",
                        source_type="text", field_type="text", source_value="Anna", target_value="Bob"),
        _depth_conflict(id="u1", link_id="time", source_key="garmin.duration", target_key="divelogs.duration",
                        source_value=2700, target_value=2760)])

    def texts():
        groups = c.get("/api/conflicts/all").json()["groups"]
        return {x["id"]: (x["source_text"], x["target_text"]) for g in groups for x in g["conflicts"]}
    # metric: exactly today's texts
    assert texts() == {"d1": ("30.2", "30.5"), "t1": ("24.5", "26"), "w1": ("4.0, kilogram", "10.0, pound"),
                       "n1": ("Anna", "Bob"), "u1": ("2700", "2760")}
    prefs.set_units("imperial")
    assert texts() == {"d1": ("99 ft", "100 ft"), "t1": ("76 °F", "79 °F"), "w1": ("8.8 lb", "10.0 lb"),
                       "n1": ("Anna", "Bob"), "u1": ("2700", "2760")}
    # the raw values travel unconverted, and the pick is a side
    item = next(x for g in c.get("/api/conflicts/all").json()["groups"] for x in g["conflicts"] if x["id"] == "d1")
    assert (item["source_value"], item["target_value"]) == (30.2, 30.5) and item["field_label"] == "Max depth"
    import src.web.app as web
    fake = _FakeEngine()
    monkeypatch.setattr(web, "_engine_for_pair_id", lambda pair_id: fake)
    monkeypatch.setattr(scheduler, "is_sync_running", False)
    assert c.post("/api/conflicts/abc123/resolve", json={"winner": "source"}).status_code == 200
    assert fake.resolved == [("abc123", "source")]
    prefs.set_units("metric")
    assert texts()["d1"] == ("30.2", "30.5")


def test_mapping_test_and_history_follow_the_units_setting(tmp_path, monkeypatch):
    from datetime import datetime
    from src.core import run_history
    from src.core.pairs import field_catalog_of
    from src.web import prefs
    import src.web.app as web
    c = _web_client(tmp_path, monkeypatch)
    monkeypatch.setattr(scheduler, "is_sync_running", False)

    class Engine(_FakeEngine):
        catalog = {f.key: f for f in field_catalog_of("garmin") + field_catalog_of("divelogs")}

        def test_mapping(self, links=None, limit=10, rules=None, match_keys=None):
            return {"ok": True, "problems": [], "matched": 1, "fetched": {}, "unmatched": {},
                    "rows": [{"dive_time": "t", "link": "depth", "receiver": "divelogs", "source_key": "garmin.max_depth",
                              "target_key": "divelogs.max_depth", "source_value": 30.2, "target_value": 30.5,
                              "result": "conflict", "conflict": True, "warnings": []},
                             {"dive_time": "t", "link": "notes", "receiver": "divelogs", "source_key": "garmin.notes",
                              "target_key": "divelogs.notes", "source_value": "A", "target_value": "B",
                              "result": "kept", "conflict": False, "warnings": []}]}
    monkeypatch.setattr(web, "_engine_for_pair_id", lambda pair_id: Engine())
    monkeypatch.setattr(run_history, "HISTORY_FILE", str(tmp_path / "sync" / "sync_history.jsonl"))
    now = datetime(2026, 6, 27, 9, 20)
    run = run_history.record_run("Manual", "manual", now, now, False,
                                 results={"conflicts": [_depth_conflict().model_dump(mode="json")], "uploaded_to_divelogs": []},
                                 source="garmin", target="divelogs")

    rows = c.post("/api/mapping/test", json={}).json()["rows"]
    assert "source_text" not in rows[0] and "target_text" not in rows[0] and rows[0]["source_value"] == 30.2
    conflict = c.get(f"/api/history/{run['id']}").json()["results"]["conflicts"][0]
    assert "source_text" not in conflict and conflict["source_value"] == 30.2

    prefs.set_units("imperial")
    rows = c.post("/api/mapping/test", json={}).json()["rows"]
    assert (rows[0]["source_text"], rows[0]["target_text"]) == ("99 ft", "100 ft")
    assert (rows[0]["source_value"], rows[0]["target_value"]) == (30.2, 30.5)      # raw values kept
    assert "source_text" not in rows[1] and "target_text" not in rows[1]           # text: the JS shows it as before
    conflict = c.get(f"/api/history/{run['id']}").json()["results"]["conflicts"][0]
    assert (conflict["source_text"], conflict["target_text"]) == ("99 ft", "100 ft")
    assert (conflict["source_value"], conflict["target_value"]) == (30.2, 30.5)
    # the stored history is never rewritten
    assert "source_text" not in (tmp_path / "sync" / "sync_history.jsonl").read_text()
    # the scheduler reads the same setting for its log lines
    assert scheduler.units_for_log is prefs.get_units and scheduler.units_for_log() == "imperial"


# ---- MySSI on the dashboard (plans/myssi-sync.md K5) ------------------------

def test_credentials_api_keeps_and_edits_myssi_accounts(tmp_path, monkeypatch):
    """The MySSI section is an account list like Garmin's: rows by email, a
    blank password keeps the stored one, a kept account keeps its token
    (never shown), a row left out is removed with its token, and a save
    without the section (an older page) leaves the accounts alone."""
    import json
    from src.core.config import ConfigManager
    c = _web_client(tmp_path, monkeypatch, {"garmin": {"username": "g", "password": "p"},
                                            "ssi": [{"email": "one@example.com", "password": "pw1", "token": "tok-1"}]})
    status = c.get("/api/credentials/status").json()
    assert status["ssi_configured"] and status["ssi_accounts"] == ["one@example.com"]
    assert status["ssi_account_rows"] == [{"email": "one@example.com", "has_password": True}]
    assert "in beta" in status["ssi_note"] and "SSI can change or close" in status["ssi_unofficial_note"]
    assert "tok-1" not in str(status) and "pw1" not in str(status)

    # add a second account; the first row's blank password keeps pw1 and the token
    res = c.post("/api/credentials", json={"garmin_accounts": [{"username": "g", "password": ""}],
                                           "ssi_accounts": [{"email": "one@example.com", "password": ""},
                                                            {"email": " two@example.com ", "password": "pw2"}]})
    assert res.status_code == 200
    stored = {a.email: a for a in ConfigManager.load_credentials().get_ssi_accounts()}
    assert stored["one@example.com"].password == "pw1" and stored["one@example.com"].token == "tok-1"
    assert stored["two@example.com"].password == "pw2" and stored["two@example.com"].token == ""
    assert ConfigManager.load_credentials().get_garmin_accounts()[0].password == "p"
    assert c.get("/api/credentials/status").json()["ssi_accounts"] == ["one@example.com", "two@example.com"]

    # a save without the section keeps them; an empty section removes them
    assert c.post("/api/credentials", json={"garmin_accounts": [{"username": "g", "password": ""}]}).status_code == 200
    assert c.get("/api/credentials/status").json()["ssi_accounts"] == ["one@example.com", "two@example.com"]
    assert c.post("/api/credentials", json={"garmin_accounts": [{"username": "g", "password": ""}],
                                            "ssi_accounts": [{"email": "two@example.com", "password": ""}]}).status_code == 200
    assert [a.email for a in ConfigManager.load_credentials().get_ssi_accounts()] == ["two@example.com"]
    assert c.post("/api/credentials", json={"garmin_accounts": [], "ssi_accounts": []}).status_code == 200
    assert c.get("/api/credentials/status").json()["ssi_configured"] is False
    assert json.load(open(tmp_path / "credentials.json"))["ssi"] == []

    # Test: one login per row with both an email and a password, nothing kept
    from src.core.services.ssi import SsiAdapter
    built = []
    monkeypatch.setattr(SsiAdapter, "login", lambda self: built.append(self.email) or self.email == "one@example.com")
    res = c.post("/api/credentials/test", json={"ssi_accounts": [{"email": "one@example.com", "password": "pw"},
                                                                 {"email": "two@example.com", "password": "pw"},
                                                                 {"email": "three@example.com", "password": ""}]})
    assert res.status_code == 200
    assert res.json()["ssi"] == [{"email": "one@example.com", "ok": True}, {"email": "two@example.com", "ok": False}]
    assert built == ["one@example.com", "two@example.com"] and res.json()["garmin"] is None
    assert c.post("/api/credentials/test", json={"garmin_accounts": []}).json()["ssi"] is None


def test_myssi_is_a_side_for_sync_now_and_scheduled_jobs(tmp_path, monkeypatch):
    """With an account stored, MySSI is offered as an endpoint and in
    Download dives; Sync now and a saved job carry the account pick
    (``ssi_account``) to the scheduler, which forwards it to the engine."""
    import time
    from src.core import scheduler
    from src.core.config import ConfigManager
    c = _web_client(tmp_path, monkeypatch, {"garmin": {"username": "g", "password": "p"},
                                            "ssi": [{"email": "one@example.com", "password": "pw"},
                                                    {"email": "two@example.com", "password": "pw"}]})
    ends = c.get("/api/sync/endpoints").json()["endpoints"]
    assert {"spec": "ssi", "id": "ssi", "label": "MySSI"} in ends
    assert any(s["id"] == "ssi" and s["label"] == "MySSI" for s in c.get("/api/dives/services").json()["services"])

    seen = {}
    real_run_sync_thread = scheduler.run_sync_thread
    monkeypatch.setattr(scheduler, "run_sync_thread", lambda dry_run, custom=None: seen.update(custom=custom))
    monkeypatch.setattr(scheduler, "is_sync_running", False)
    assert c.post("/api/sync/trigger", json={"source": "garmin", "target": "ssi", "ssi_account": "two@example.com"}).status_code == 200
    time.sleep(0.2)
    assert (seen["custom"]["source"], seen["custom"]["target"]) == ("garmin", "ssi")
    assert seen["custom"]["ssi_account"] == "two@example.com" and seen["custom"]["directionality"] == "to_ssi"

    settings = c.get("/api/settings").json()
    settings["cron_jobs"] = [{"id": "garmin-to-ssi-daily", "source": "garmin", "target": "ssi", "directionality": "to_ssi",
                              "frequency": "daily", "hour": 6, "minute": 0, "day_of_week": 1, "interval_minutes": 60,
                              "only_new": True, "sync_gases": True, "enabled": True, "ssi_account": "one@example.com"}]
    assert c.post("/api/settings", json=settings).status_code == 200
    job = ConfigManager.load_settings().cron_jobs[0]
    assert (job.source, job.target, job.ssi_account) == ("garmin", "ssi", "one@example.com")
    assert c.get("/api/settings").json()["cron_jobs"][0]["ssi_account"] == "one@example.com"
    seen.clear()
    assert c.post("/api/jobs/garmin-to-ssi-daily/run").status_code == 200
    time.sleep(0.2)
    assert seen["custom"]["ssi_account"] == "one@example.com"

    # the scheduler hands the pick to the engine builder
    import src.core.pairs as pairs
    captured = {}

    class FakeEngine:
        run_overrides = {}

        def run_sync(self, **kwargs):
            return {"ok": True}

    monkeypatch.setattr(pairs, "engine_for", lambda source, target, **kw: captured.update(kw) or FakeEngine())
    real_run_sync_thread(False, job.model_dump())
    assert captured["ssi_account"] == "one@example.com"


def test_dashboard_has_the_myssi_section_and_pickers():
    """The page: a MySSI account list on Settings with the beta note, and a
    MySSI account picker next to Sync now and in the job editor."""
    client = TestClient(app)
    html = client.get("/").text
    for element in ("ssi-accounts-rows", "ssi-add-account", "ssi-configured", "ssi-note",
                    "trigger-ssi-account", "job-ssi-account", "trigger-note"):
        assert f'id="{element}"' in html, element
    with open("src/web/static/script.js") as f:
        js = f.read()
    assert "ssi_accounts" in js and "ssi_account" in js and 'ssi: "MySSI"' in js


def test_dashboard_shows_failed_writes_in_the_run_views():
    """check.md B3/B4: a run's ``failed`` writes are counted in the job
    results and the history list, and the run view lists them under their
    own heading, apart from the uploaded/updated ones."""
    with open("src/web/static/script.js") as f:
        js = f.read()
    assert "if (counts.failed) parts.push(`failed: ${counts.failed}`)" in js
    assert "if (last.failed && last.failed.length) parts.push(`failed: ${last.failed.length}`)" in js
    assert "<h3>Failed (${results.failed.length})</h3>" in js
    assert 'write${results.failed.length === 1 ? "" : "s"} failed' in js


def test_garmin_fit_upload_on_jobs_sync_now_and_kept_by_the_settings_form(tmp_path, monkeypatch):
    """R13 (beta): a job stores garmin_fit_upload (None when unticked, the
    pair's setting then applies), Sync now passes it to the run, and the
    settings form - which has no such field - keeps the stored global value."""
    from src.core.config import ConfigManager
    _isolated_settings(tmp_path, monkeypatch)
    client = TestClient(app)
    assert client.get("/api/settings").json()["garmin_fit_upload"] is False          # off by default

    payload = _base_settings_payload()
    payload["cron_jobs"] = [
        {"id": "to-garmin", "source": "divelogs", "target": "garmin", "directionality": "to_garmin",
         "frequency": "daily", "hour": 6, "minute": 0, "day_of_week": 0, "interval_minutes": 60,
         "only_new": True, "sync_gases": True, "enabled": True, "garmin_fit_upload": True},
        {"id": "plain", "frequency": "daily", "hour": 7, "minute": 0, "day_of_week": 0, "interval_minutes": 60,
         "only_new": True, "sync_gases": True, "enabled": True}]
    assert client.post("/api/settings", json=payload).status_code == 200
    jobs = {j["id"]: j for j in client.get("/api/settings").json()["cron_jobs"]}
    assert jobs["to-garmin"]["garmin_fit_upload"] is True and jobs["plain"]["garmin_fit_upload"] is None

    settings = ConfigManager.load_settings()
    settings.garmin_fit_upload = True
    ConfigManager.save_settings(settings)
    assert client.post("/api/settings", json=payload).status_code == 200
    assert client.get("/api/settings").json()["garmin_fit_upload"] is True           # not wiped by the form

    called = []
    monkeypatch.setattr(scheduler, "run_sync_thread", lambda dry_run, custom_settings=None: called.append(custom_settings))
    assert client.post("/api/sync/trigger", json={"dry_run": True, "garmin_fit_upload": True}).status_code == 200
    assert called[-1]["garmin_fit_upload"] is True
    assert client.post("/api/sync/trigger", json={"dry_run": True}).status_code == 200
    assert "garmin_fit_upload" not in called[-1]


def test_dashboard_offers_new_garmin_dives_as_fit():
    """The page: a "New Garmin dives as .fit (beta)" box next to Sync now and
    in the job editor; ticked, the run or job carries garmin_fit_upload."""
    client = TestClient(app)
    html = client.get("/").text
    for element in ("trigger-garmin-fit", "job-garmin-fit"):
        assert f'id="{element}"' in html, element
    assert html.count("New Garmin dives as .fit (beta)") == 2
    with open("src/web/static/script.js") as f:
        js = f.read()
    assert 'if ($("trigger-garmin-fit").checked) payload.garmin_fit_upload = true;' in js
    assert 'garmin_fit_upload: $("job-garmin-fit").checked ? true : null,' in js
