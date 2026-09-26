"""Garmin file naming (<dive #>_<date>_<time>_<activity id>) for the cached
JSON and the downloaded .fit, the FIT column, and the FIT download paths."""
import io
import json
import os
import zipfile

import pytest
from fastapi.testclient import TestClient

from src.core import dive_cache, garmin_files
import src.core.scheduler as scheduler


def _zip(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, data)
    return buf.getvalue()


def test_dive_stem_formats():
    assert garmin_files.dive_stem(12, "2024-05-01 10:23:45", 17283746) == "12_2024-05-01_102345_17283746"
    assert garmin_files.dive_stem("3", "2024-05-01T09:05:00.0", "55") == "3_2024-05-01_090500_55"
    # an unnumbered dive keeps four parts, so the id is still found last
    stem = garmin_files.dive_stem(None, "2024-05-01 10:23", 55)
    assert stem == "nonum_2024-05-01_102300_55"
    assert garmin_files.activity_id_of(stem + ".fit") == "55"
    assert garmin_files.activity_id_of("12.json") is None


def test_save_fit_unzips_and_replaces_an_older_name(tmp_path):
    base = str(tmp_path)
    garmin_files.save_fit(_zip("x_ACTIVITY.fit", b"old"), "1_2024-05-01_100000_55", "u", base)
    path = garmin_files.save_fit(_zip("x_ACTIVITY.fit", b"new"), "2_2024-05-01_100000_55", "u", base)
    assert os.listdir(garmin_files.fit_dir("u", base)) == ["2_2024-05-01_100000_55.fit"]
    assert open(path, "rb").read() == b"new"
    # a bare (unzipped) FIT is stored as-is
    garmin_files.save_fit(b".FITdata", "3_2024-05-02_100000_56", "u", base)
    assert open(garmin_files.find_fit(56, "u", base), "rb").read() == b".FITdata"


def _refresh_engine(tmp_path, monkeypatch, listing, calls):
    from src.core.sync_engine import SyncEngine

    class FakeGarminClient:
        def connectapi(self, url, params=None):
            calls.append(url)
            if "tanksensor" in url:
                return {}
            activity_id = url.rsplit("/", 1)[-1]
            entry = next(e for e in listing if str(e["activityId"]) == activity_id)
            return {"activityId": activity_id, "metadataDTO": entry["metadataDTO"]}

        def get_activities(self, start, limit, activitytype=None):
            return list(listing) if start == 0 else []

        def get_activity_details(self, activity_id):
            return {}

    class FakeAdapter:
        client = FakeGarminClient()
        cooldown_seconds = 0

        def login(self):
            return True

    engine = SyncEngine(settings_path=str(tmp_path / "settings.json"),
                        credentials_path=str(tmp_path / "credentials.json"),
                        mock_data_dir=str(tmp_path / "mock"))
    adapter = FakeAdapter()
    monkeypatch.setattr(type(engine), "garmin", property(lambda self: adapter))
    monkeypatch.setattr(type(engine), "garmin_username", property(lambda self: "u"))
    return engine


def test_refresh_renames_old_cache_files_and_keeps_fits_in_step(tmp_path, monkeypatch):
    diving = {"typeKey": "diving"}
    listing = [{"activityId": 1, "activityName": "One", "diveNumber": 1, "activityType": diving,
                "startTimeLocal": "2026-06-01 10:00:00", "metadataDTO": {"diveNumber": 1}}]
    calls = []
    engine = _refresh_engine(tmp_path, monkeypatch, listing, calls)
    base = str(tmp_path / "cache")
    garmin_dir = os.path.join(base, "garmin", "u", "data")
    os.makedirs(garmin_dir)
    # cached under the old "<dive number>.json" name, complete and unchanged
    with open(os.path.join(garmin_dir, "1.json"), "w") as f:
        json.dump({"summary": listing[0], "details": {"metadataDTO": {"diveNumber": 1}}}, f)

    assert engine.download_and_save_raw_data(mock_data_dir=base, include_divelogs=False) is True
    assert os.listdir(garmin_dir) == ["1_2026-06-01_100000_1.json"]
    assert calls == []                     # renamed in place, not fetched again

    garmin_files.save_fit(b".FIT", "1_2026-06-01_100000_1", "u", base)
    listing[0] = dict(listing[0], diveNumber=9, metadataDTO={"diveNumber": 9})
    assert engine.download_and_save_raw_data(mock_data_dir=base, include_divelogs=False) is True
    assert os.listdir(garmin_dir) == ["9_2026-06-01_100000_1.json"]
    assert os.listdir(garmin_files.fit_dir("u", base)) == ["9_2026-06-01_100000_1.fit"]


def _cache_dive(base, account, activity_id, number, manual=False):
    directory = os.path.join(base, "garmin", account, "data")
    os.makedirs(directory, exist_ok=True)
    stem = garmin_files.dive_stem(number, "2026-06-22 10:00:00", activity_id)
    with open(os.path.join(directory, stem + ".json"), "w") as f:
        json.dump({"summary": {"activityId": activity_id, "startTimeLocal": "2026-06-22 10:00:00",
                               "isManualActivity": manual,
                               "metadataDTO": {"diveNumber": number}}, "details": {}}, f)
    return stem


def test_listing_marks_downloaded_fits(tmp_path):
    base = str(tmp_path)
    with_fit = _cache_dive(base, "u", 101, 1)
    _cache_dive(base, "u", 102, 2)
    garmin_files.save_fit(b".FIT", with_fit, "u", base)
    _cache_dive(base, "u", 103, 3, manual=True)
    rows = {r["id"]: r for r in dive_cache.list_garmin_dives(base_dir=base)}
    assert rows["101"]["fit"] == "✓" and rows["101"]["fit_file"] == with_fit + ".fit"
    assert rows["102"]["fit"] == "" and rows["102"]["account"] == "u"
    # hand-logged: marked, and not "missing" (non-empty) so bulk download skips it
    assert rows["103"]["fit"] == "manual" and rows["103"]["manual"] is True


def test_download_garmin_fits(tmp_path, monkeypatch):
    base = str(tmp_path)
    ok = _cache_dive(base, "u", 101, 1)
    no_file = _cache_dive(base, "u", 102, 2)
    manual = _cache_dive(base, "u", 104, 4, manual=True)
    logins, asked = [], []

    class FakeGarmin:
        def __init__(self, username, password, token_dir=None, cooldown_seconds=0):
            pass

        def login(self):
            logins.append(1)
            return True

        def download_fit(self, activity_id):
            asked.append(activity_id)
            return _zip("a.fit", b"fit-" + activity_id.encode()) if activity_id == "101" else None

    class Creds:
        username, password, token_dir = "u", "p", "t"

    import src.core.services.garmin as garmin_module
    monkeypatch.setattr(garmin_module, "GarminAdapter", FakeGarmin)
    monkeypatch.setattr(dive_cache, "_active_credentials", lambda service, username: Creds())

    result = dive_cache.download_garmin_fits([ok + ".json", no_file + ".json", manual + ".json", "missing.json"],
                                             base_dir=base)
    assert result["downloaded"] == [ok + ".json"]
    assert set(result["failed"]) == {no_file + ".json", manual + ".json", "missing.json"}
    assert "hand-logged" in result["failed"][manual + ".json"]
    assert "104" not in asked              # a manual dive is never requested
    assert len(logins) == 1                # one login for the whole batch
    assert open(garmin_files.find_fit(101, "u", base), "rb").read() == b"fit-101"


def test_web_garmin_dives_and_fit_endpoints(tmp_path, monkeypatch):
    base = str(tmp_path)
    monkeypatch.setenv("DATA_DIR", base)
    stem = _cache_dive(base, "u", 101, 1)
    _cache_dive(base, "u", 102, 2)
    _cache_dive(base, "u", 103, 3, manual=True)
    garmin_files.save_fit(b".FIT", stem, "u", base)
    client = TestClient(__import__("src.web.app", fromlist=["app"]).app)

    dives = {d["id"]: d for d in client.get("/api/dives/garmin").json()["dives"]}
    assert dives["101"]["fit"] == "✓" and dives["102"]["fit"] == ""
    assert dives["103"]["fit"] == "manual" and dives["103"]["manual"] is True

    res = client.get("/api/dives/garmin/fit/101", params={"account": "u"})
    assert res.status_code == 200 and res.content == b".FIT"
    assert stem + ".fit" in res.headers["content-disposition"]
    assert client.get("/api/dives/garmin/fit/102", params={"account": "u"}).status_code == 404
    assert client.get("/api/dives/garmin/fit/101", params={"account": ".."}).status_code == 400

    started = []
    monkeypatch.setattr(scheduler, "run_fit_download_thread", lambda filenames: started.append(filenames))
    res = client.post("/api/dives/garmin/fit", json={})
    assert res.json() == {"status": "started", "count": 1}
    monkeypatch.setattr(scheduler, "is_download_running", True)
    assert client.post("/api/dives/garmin/fit", json={}).status_code == 409


# -- sync-time reuse of the refresh cache (SyncFilters.use_garmin_cache) -----

def _listing_entry(activity_id, number, name="Dive"):
    return {"activityId": activity_id, "activityName": name, "diveNumber": number,
            "activityType": {"typeKey": "diving"}, "startTimeLocal": "2026-06-01 10:00:00",
            "metadataDTO": {"diveNumber": number}}


def _garmin_adapter(tmp_path, listing, calls, telemetry_fails=False):
    from src.core.services.garmin import GarminAdapter

    class FakeClient:
        def get_activities(self, start, limit, activitytype=None):
            return list(listing) if start == 0 else []

        def connectapi(self, url, params=None):
            calls.append(url)
            if "tanksensor" in url:
                return {}
            return {"activityId": url.rsplit("/", 1)[-1], "metadataDTO": {"diveNumber": listing[0]["diveNumber"]},
                    "summaryDTO": {"maxDepth": 12.0, "duration": 1800}}

        def get_activity_details(self, activity_id):
            calls.append(f"details/{activity_id}")
            if telemetry_fails:
                raise RuntimeError("boom")
            return {}

    adapter = GarminAdapter("u", "p", token_dir=str(tmp_path / "tokens"), cooldown_seconds=0)
    adapter.client = FakeClient()
    adapter.logged_in = True
    adapter._detected_timezone = "UTC"
    return adapter


def test_sync_fetch_reuses_an_unchanged_cached_dive(tmp_path):
    listing = [_listing_entry(1, 5)]
    cache = str(tmp_path / "garmin" / "u" / "data")
    os.makedirs(cache)
    with open(os.path.join(cache, "5.json"), "w") as f:     # old naming scheme is fine too
        json.dump({"summary": listing[0], "details": {"metadataDTO": {"diveNumber": 5},
                                                      "summaryDTO": {"maxDepth": 21.5, "duration": 2400}}}, f)
    calls = []
    adapter = _garmin_adapter(tmp_path, listing, calls)
    adapter.cache_dir = cache
    dives = adapter.fetch_dives()
    assert calls == []                                      # no per-dive API call
    assert len(dives) == 1 and dives[0].max_depth == 21.5   # the cached details were used


def test_sync_fetch_refetches_a_changed_dive_and_recaches_it(tmp_path):
    listing = [_listing_entry(1, 5)]
    cache = str(tmp_path / "garmin" / "u" / "data")
    os.makedirs(cache)
    with open(os.path.join(cache, "5.json"), "w") as f:
        json.dump({"summary": dict(listing[0], activityName="Old name"), "details": {}}, f)
    calls = []
    adapter = _garmin_adapter(tmp_path, listing, calls)
    adapter.cache_dir = cache
    dives = adapter.fetch_dives()
    assert len(calls) == 3 and dives[0].max_depth == 12.0
    assert os.listdir(cache) == ["5_2026-06-01_100000_1.json"]   # rewritten, old name gone
    assert json.load(open(os.path.join(cache, "5_2026-06-01_100000_1.json")))["summary"]["activityName"] == "Dive"


def test_sync_fetch_marks_a_partial_download_so_it_is_retried(tmp_path):
    listing = [_listing_entry(1, 5)]
    cache = str(tmp_path / "garmin" / "u" / "data")
    calls = []
    adapter = _garmin_adapter(tmp_path, listing, calls, telemetry_fails=True)
    adapter.cache_dir = cache
    adapter.fetch_dives()
    payload = json.load(open(os.path.join(cache, "5_2026-06-01_100000_1.json")))
    assert payload["incomplete"] == ["activityDetails"]
    calls.clear()
    adapter.fetch_dives()
    assert len(calls) == 3                                  # not trusted: fetched again


def test_sync_fetch_without_cache_always_fetches_and_writes_nothing(tmp_path):
    listing = [_listing_entry(1, 5)]
    calls = []
    adapter = _garmin_adapter(tmp_path, listing, calls)
    adapter.fetch_dives()
    assert len(calls) == 3
    assert not os.path.exists(tmp_path / "garmin")


def test_engine_points_garmin_sides_at_the_cache(tmp_path, monkeypatch):
    from src.core.sync_engine import SyncEngine
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    engine = SyncEngine(settings_path=str(tmp_path / "settings.json"),
                        credentials_path=str(tmp_path / "credentials.json"),
                        mock_data_dir=str(tmp_path / "mock"))
    garmin = _garmin_adapter(tmp_path, [], [])
    monkeypatch.setattr(engine, "source", garmin)
    engine._configure_garmin_cache(True)
    assert garmin.cache_dir == os.path.join(str(tmp_path), "garmin", "u", "data")
    engine._configure_garmin_cache(False)
    assert garmin.cache_dir is None


def test_web_trigger_passes_the_cache_choice(monkeypatch):
    client = TestClient(__import__("src.web.app", fromlist=["app"]).app)
    called = []
    monkeypatch.setattr(scheduler, "run_sync_thread", lambda dry_run, custom_settings=None: called.append(custom_settings))
    assert client.post("/api/sync/trigger", json={"use_garmin_cache": False}).status_code == 200
    assert called[0]["use_garmin_cache"] is False


def test_is_manual_dive_reads_connects_manual_activity_flag():
    # what Connect actually returns: manualActivity on the listing entry and in metadataDTO
    assert garmin_files.is_manual_dive({"summary": {"manualActivity": True}, "details": {}})
    assert garmin_files.is_manual_dive({"summary": {}, "details": {"metadataDTO": {"manualActivity": True}}})
    assert garmin_files.is_manual_dive({"summary": {"isManualActivity": True}})
    assert not garmin_files.is_manual_dive({"summary": {"manualActivity": False},
                                            "details": {"metadataDTO": {"manualActivity": False}}})
