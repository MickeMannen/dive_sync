import os
import json
import pytest

from src.core import dive_cache


@pytest.fixture
def cache_dirs(tmp_path):
    garmin_dir = tmp_path / "garmin"
    divelogs_dir = tmp_path / "divelogs"
    garmin_dir.mkdir()
    divelogs_dir.mkdir()

    g_dive = {
        "summary": {
            "activityId": "10001",
            "activityName": "Limhamn Ön",
            "startTimeLocal": "2026-06-22 10:00:00",
            "metadataDTO": {"diveNumber": "1"},
            "summaryDTO": {"duration": 2700, "maxDepth": 18.2},
        },
        "details": {
            "diveInfo": {
                "buddy": "Micke",
                "weight": 12.0,
                "weightUnit": {"unitKey": "kilogram"},
                "visibility": 5.0,
                "visibilityUnit": {"unitKey": "meter"},
            }
        },
    }
    d_dive = {
        "id": "50001",
        "divenumber": "1",
        "date": "2026-06-22",
        "time": "10:02:00",
        "duration": 2700,
        "maxdepth": 18.2,
        "location": "Limhamn Ön",
        "notes": "Nice dive!",
        "buddy": "Micke",
        "weights": "12",
        "visibility": "5",
    }

    (garmin_dir / "1.json").write_text(json.dumps(g_dive))
    (divelogs_dir / "1.json").write_text(json.dumps(d_dive))

    return str(tmp_path)


def test_list_garmin_dives(cache_dirs):
    dives = dive_cache.list_garmin_dives(base_dir=cache_dirs)
    assert len(dives) == 1
    assert dives[0]["location"] == "Limhamn Ön"
    assert dives[0]["dive_number"] == "1"
    assert dives[0]["buddy"] == "Micke"
    assert dives[0]["weight"] == "12 kg"
    assert dives[0]["visibility"] == "5 m"
    assert dives[0]["duration"] == 45
    assert dives[0]["max_depth"] == "18.20"
    assert dives[0]["date"] == "2026-06-22"
    assert dives[0]["time"] == "10:00:00"


def test_list_garmin_dives_formats_float_duration_and_depth(tmp_path):
    garmin_dir = tmp_path / "garmin"
    garmin_dir.mkdir()
    (garmin_dir / "1.json").write_text(json.dumps({
        "summary": {
            "activityId": "1",
            "summaryDTO": {"duration": 2700.0, "maxDepth": 18.199999999},
        },
        "details": {},
    }))

    dives = dive_cache.list_garmin_dives(base_dir=str(tmp_path))

    assert dives[0]["duration"] == 45
    assert dives[0]["max_depth"] == "18.20"


def test_seconds_to_minutes_display_rounds():
    assert dive_cache._seconds_to_minutes_display(2700) == 45
    assert dive_cache._seconds_to_minutes_display(2730) == 46  # rounds to nearest minute
    assert dive_cache._seconds_to_minutes_display(0) == 0
    assert dive_cache._seconds_to_minutes_display(None) is None


def test_normalize_date_time_swaps_iso_t_separator():
    assert dive_cache._normalize_date_time("2026-06-22T10:15:30") == "2026-06-22 10:15:30"


def test_normalize_date_time_trims_fractional_seconds_and_offset():
    assert dive_cache._normalize_date_time("2026-06-22T10:15:30.000+02:00") == "2026-06-22 10:15:30"


def test_normalize_date_time_passes_through_already_normalized():
    assert dive_cache._normalize_date_time("2026-06-22 10:15:30") == "2026-06-22 10:15:30"


def test_normalize_date_time_empty():
    assert dive_cache._normalize_date_time("") == ""
    assert dive_cache._normalize_date_time(None) == ""


def test_list_garmin_dives_normalizes_iso_t_separated_date_time(tmp_path):
    garmin_dir = tmp_path / "garmin"
    garmin_dir.mkdir()
    (garmin_dir / "1.json").write_text(json.dumps({
        "summary": {
            "activityId": "1",
            "startTimeLocal": "2026-06-22T10:15:30.000",
        },
        "details": {},
    }))

    dives = dive_cache.list_garmin_dives(base_dir=str(tmp_path))

    assert dives[0]["date_time"] == "2026-06-22 10:15:30"


def test_list_divelogs_dives(cache_dirs):
    dives = dive_cache.list_divelogs_dives(base_dir=cache_dirs)
    assert len(dives) == 1
    assert dives[0]["location"] == "Limhamn Ön"
    assert dives[0]["buddy"] == "Micke"
    assert dives[0]["duration"] == 45
    assert dives[0]["date"] == "2026-06-22"
    assert dives[0]["time"] == "10:02:00"


def test_read_raw_dive(cache_dirs):
    data = dive_cache.read_raw_dive("garmin", "1.json", base_dir=cache_dirs)
    assert data["summary"]["activityId"] == "10001"


def test_read_raw_dive_not_found(cache_dirs):
    with pytest.raises(FileNotFoundError):
        dive_cache.read_raw_dive("garmin", "missing.json", base_dir=cache_dirs)


def test_update_dive_fields_garmin(cache_dirs):
    filepath = dive_cache.update_dive_fields(
        "garmin", "1.json", base_dir=cache_dirs,
        dive_number="10", location="New Location", buddy="New Buddy",
        weight="15 kg", visibility="8 m",
    )
    with open(filepath) as f:
        data = json.load(f)

    assert data["summary"]["metadataDTO"]["diveNumber"] == "10"
    assert data["summary"]["activityName"] == "New Location"
    assert data["details"]["diveInfo"]["buddy"] == "New Buddy"
    assert data["details"]["diveInfo"]["weight"] == 15.0
    assert data["details"]["diveInfo"]["visibility"] == 8.0


def test_update_dive_fields_divelogs(cache_dirs):
    filepath = dive_cache.update_dive_fields(
        "divelogs", "1.json", base_dir=cache_dirs,
        location="New Site, Reef", notes="Even better!", buddy="Another Buddy",
        weight="10", visibility="4",
    )
    with open(filepath) as f:
        data = json.load(f)

    assert data["location"] == "New Site"
    assert data["divesite"] == "Reef"
    assert data["notes"] == "Even better!"
    assert data["buddy"] == "Another Buddy"
    assert data["weights"] == "10"
    assert data["visibility"] == "4"


def test_update_dive_fields_converts_duration_minutes_to_seconds_garmin(cache_dirs):
    filepath = dive_cache.update_dive_fields("garmin", "1.json", base_dir=cache_dirs, duration=50)
    with open(filepath) as f:
        data = json.load(f)

    assert data["summary"]["duration"] == 3000
    assert data["summary"]["summaryDTO"]["bottomTime"] == 3000
    assert data["details"]["summaryDTO"]["duration"] == 3000

    # And the list view reflects it back as minutes.
    dives = dive_cache.list_garmin_dives(base_dir=cache_dirs)
    assert dives[0]["duration"] == 50


def test_update_dive_fields_converts_duration_minutes_to_seconds_divelogs(cache_dirs):
    filepath = dive_cache.update_dive_fields("divelogs", "1.json", base_dir=cache_dirs, duration=50)
    with open(filepath) as f:
        data = json.load(f)

    assert data["duration"] == 3000


def test_delete_dive_local(cache_dirs):
    filepath, external_id = dive_cache.delete_dive_local("garmin", "1.json", base_dir=cache_dirs)
    assert external_id == "10001"
    assert not os.path.exists(filepath)
    assert dive_cache.list_garmin_dives(base_dir=cache_dirs) == []


def test_delete_dive_local_not_found(cache_dirs):
    with pytest.raises(FileNotFoundError):
        dive_cache.delete_dive_local("garmin", "missing.json", base_dir=cache_dirs)


def test_push_remote_update_garmin(cache_dirs, monkeypatch):
    import src.core.config as config
    creds_file = os.path.join(cache_dirs, "credentials.json")
    creds = config.CredentialsModel(garmin=config.GarminCredentials(username="user@example.com", password="pw"))
    config.ConfigManager.save_credentials(creds, creds_file)
    original_load = config.ConfigManager.load_credentials
    monkeypatch.setattr(config.ConfigManager, "load_credentials", lambda path=creds_file: original_load(creds_file))

    from src.core.services.garmin import GarminAdapter
    calls = []
    monkeypatch.setattr(GarminAdapter, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(GarminAdapter, "_map_to_unified", lambda self, summary, details: "unified-dive")
    monkeypatch.setattr(GarminAdapter, "update_dive", lambda self, ext_id, dive: calls.append((ext_id, dive)) or True)

    filepath = os.path.join(cache_dirs, "garmin", "1.json")
    result = dive_cache.push_remote_update("garmin", filepath, username="user@example.com")

    assert result is True
    assert calls == [("10001", "unified-dive")]


def test_push_remote_delete_divelogs(cache_dirs, monkeypatch):
    import src.core.config as config
    creds_file = os.path.join(cache_dirs, "credentials.json")
    creds = config.CredentialsModel(divelogs=config.DivelogsCredentials(username="diver", password="pw"))
    config.ConfigManager.save_credentials(creds, creds_file)
    original_load = config.ConfigManager.load_credentials
    monkeypatch.setattr(config.ConfigManager, "load_credentials", lambda path=creds_file: original_load(creds_file))

    from src.core.services.divelogs import DivelogsAdapter
    calls = []
    monkeypatch.setattr(DivelogsAdapter, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(DivelogsAdapter, "delete_dive", lambda self, ext_id: calls.append(ext_id) or True)

    result = dive_cache.push_remote_delete("divelogs", "50001", username="diver")

    assert result is True
    assert calls == ["50001"]
