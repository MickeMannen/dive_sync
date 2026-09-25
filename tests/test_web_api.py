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


def test_credentials_api_preserves_subsurface_and_submersion(tmp_path, monkeypatch):
    import src.core.config
    client = TestClient(app)
    creds_file = str(tmp_path / "credentials.json")
    original_load = src.core.config.ConfigManager.load_credentials
    original_save = src.core.config.ConfigManager.save_credentials
    monkeypatch.setattr(src.core.config.ConfigManager, "load_credentials", lambda path=creds_file: original_load(creds_file))
    monkeypatch.setattr(src.core.config.ConfigManager, "save_credentials", lambda creds, path=creds_file: original_save(creds, creds_file))

    subsurface = {"email": "me@x.org", "password": "pw"}
    submersion = {"endpoint_url": "https://s3.eu-central-003.backblazeb2.com", "region": "eu-central-003",
                  "bucket": "dives", "access_key_id": "id", "secret_access_key": "key"}
    res = client.post("/api/credentials", json={"garmin_accounts": [{"username": "g", "password": "p"}],
                                                 "subsurface": subsurface, "submersion": submersion})
    assert res.status_code == 200
    status = client.get("/api/credentials/status").json()
    assert status["subsurface_configured"] and status["subsurface_email"] == "me@x.org"
    assert status["submersion_configured"] and status["submersion_store"]["bucket"] == "dives"
    assert "password" not in status and "secret_access_key" not in str(status)

    # a Garmin/Divelogs-only save (the current form) keeps them
    res = client.post("/api/credentials", json={"garmin_accounts": [{"username": "g2", "password": "p"}]})
    assert res.status_code == 200
    status = client.get("/api/credentials/status").json()
    assert status["garmin_username"] == "g2" and status["subsurface_configured"] and status["submersion_configured"]

    from src.core.services import subsurface_cloud
    from src.core.services.submersion import store as submersion_store
    monkeypatch.setattr(subsurface_cloud, "check_cloud_login", lambda e, p, b: (True, "cloud ok"))
    monkeypatch.setattr(submersion_store, "check_store_access", lambda c: (False, "denied"))
    res = client.post("/api/credentials/test", json={"subsurface": subsurface, "submersion": submersion})
    assert res.status_code == 200
    body = res.json()
    assert body["subsurface"] is True and body["subsurface_message"] == "cloud ok"
    assert body["submersion"] is False and body["submersion_message"] == "denied"
    assert body["garmin"] is None


def test_credentials_test_checks_submersion_passphrase_after_connectivity(monkeypatch):
    """rework.md E11: once the store itself is reachable, /api/credentials/test
    also tries to unlock an end-to-end encrypted library, so a wrong or
    missing passphrase is reported here rather than only on the next sync."""
    from src.core.services.submersion import store as submersion_store
    from src.core.services.submersion.adapter import SubmersionAdapter
    client = TestClient(app)
    submersion = {"endpoint_url": "https://x", "region": "r", "bucket": "b",
                  "access_key_id": "id", "secret_access_key": "key"}
    monkeypatch.setattr(submersion_store, "check_store_access", lambda c: (True, "S3 store OK"))

    monkeypatch.setattr(SubmersionAdapter, "_resolve_encryption",
                        lambda self: (False, "the configured passphrase does not unlock this store's encrypted library"))
    body = client.post("/api/credentials/test", json={"submersion": submersion}).json()
    assert body["submersion"] is False and "does not unlock" in body["submersion_message"]

    monkeypatch.setattr(SubmersionAdapter, "_resolve_encryption", lambda self: (True, ""))
    body = client.post("/api/credentials/test", json={"submersion": submersion}).json()
    assert body["submersion"] is True and body["submersion_message"] == "S3 store OK"

    monkeypatch.setattr(SubmersionAdapter, "_resolve_encryption", lambda self: (True, "unlocked encrypted library abc"))
    body = client.post("/api/credentials/test", json={"submersion": submersion}).json()
    assert body["submersion"] is True and "unlocked encrypted library abc" in body["submersion_message"]


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
    v = c.get("/api/version").json()                        # the keys the sidebar label reads
    assert v["current_version"] == about["version"] and "update_available" in v
