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


def test_format_sac_sums_every_cylinder():
    """SAC = litres breathed / (minutes x atmospheres at the average depth).
    A real twinset dive: 2 x 11.1L, 162.57->78.04 and 160.69->106.38 bar over
    4129s at 8.196m average, which is 1541L in 68.8min at 1.82 ata."""
    tanks = [{"volume": 11.1, "start_pressure": 162.57, "end_pressure": 78.04},
             {"volume": 11.1, "start_pressure": 160.69, "end_pressure": 106.38}]
    assert dive_cache._format_sac(tanks, 8.196, 4129.51) == "12.3"
    # One cylinder of the same dive on its own is the smaller half of that.
    assert dive_cache._format_sac(tanks[:1], 8.196, 4129.51) == "7.5"


def test_format_sac_needs_a_volume_a_drop_a_depth_and_a_time():
    # 150 bar x 12L = 1800L, over 60min at 20m (3 ata) = 10.0 L/min.
    full = [{"volume": 12.0, "start_pressure": 200.0, "end_pressure": 50.0}]
    assert dive_cache._format_sac(full, 20.0, 3600) == "10.0"
    # Divelogs stores 0 for a cylinder whose size was never filled in: that is
    # missing data, not a zero-litre tank, so there is no rate to show.
    assert dive_cache._format_sac([{**full[0], "volume": 0}], 20.0, 3600) == ""
    assert dive_cache._format_sac([{**full[0], "end_pressure": 200.0}], 20.0, 3600) == ""   # no drop
    assert dive_cache._format_sac([], 20.0, 3600) == ""
    assert dive_cache._format_sac(full, 0, 3600) == ""          # no average depth
    assert dive_cache._format_sac(full, 20.0, 0) == ""          # no duration
    assert dive_cache._format_sac(full, None, None) == ""
    assert dive_cache._format_sac([{"volume": "x", "start_pressure": "y", "end_pressure": "z"}], 20.0, 3600) == ""


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


def test_get_samples_garmin(tmp_path):
    garmin_dir = tmp_path / "garmin"
    garmin_dir.mkdir()
    (garmin_dir / "1.json").write_text(json.dumps({
        "summary": {"activityId": "1"},
        "details": {},
        "activityDetails": {
            "metricDescriptors": [
                {"key": "sumDuration", "metricsIndex": 0},
                {"key": "directDepth", "metricsIndex": 1},
                {"key": "directAirTemperature", "metricsIndex": 2},
            ],
            "activityDetailMetrics": [
                {"metrics": [0, 0.0, 26.5]},
                {"metrics": [30, 5.2, 25.0]},
                {"metrics": [60, None, 24.5]},   # no depth -> dropped
            ],
        },
    }))

    samples = dive_cache.get_samples("garmin", "1.json", base_dir=str(tmp_path))

    assert samples == [
        {"time": 0, "depth": 0.0, "temp": 26.5},
        {"time": 30, "depth": 5.2, "temp": 25.0},
    ]


def test_get_samples_garmin_no_activity_details(tmp_path):
    garmin_dir = tmp_path / "garmin"
    garmin_dir.mkdir()
    (garmin_dir / "1.json").write_text(json.dumps({"summary": {"activityId": "1"}, "details": {}}))

    assert dive_cache.get_samples("garmin", "1.json", base_dir=str(tmp_path)) == []


def test_get_samples_divelogs(tmp_path):
    divelogs_dir = tmp_path / "divelogs"
    divelogs_dir.mkdir()
    (divelogs_dir / "1.json").write_text(json.dumps({
        "id": "1",
        "samplerate": 30,
        "sampledata": [{"d": 0.0, "t": 26.5}, {"d": 5.2, "t": 25.0}, {"d": 8.1}],
    }))

    samples = dive_cache.get_samples("divelogs", "1.json", base_dir=str(tmp_path))

    assert samples == [
        {"time": 0, "depth": 0.0, "temp": 26.5},
        {"time": 30, "depth": 5.2, "temp": 25.0},
        {"time": 60, "depth": 8.1, "temp": None},
    ]


def test_get_samples_divelogs_no_sampledata(cache_dirs):
    assert dive_cache.get_samples("divelogs", "1.json", base_dir=cache_dirs) == []


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



def test_update_dive_fields_garmin_activity_and_location_name_apart(cache_dirs):
    """The Garmin editor's two fields: "Gozo, Blue Hole" as the title and
    "Blue Hole" as the location name, each written only where it belongs."""
    filepath = dive_cache.update_dive_fields("garmin", "1.json", base_dir=cache_dirs,
                                             activity_name="Gozo, Blue Hole", location_name="Blue Hole")
    with open(filepath) as f:
        data = json.load(f)
    for part in ("summary", "details"):
        assert data[part]["activityName"] == "Gozo, Blue Hole" and data[part]["locationName"] == "Blue Hole"
    row = next(r for r in dive_cache.list_garmin_dives(base_dir=cache_dirs) if r["filename"] == "1.json")
    assert (row["activity_name"], row["location_name"], row["location"]) == ("Gozo, Blue Hole", "Blue Hole", "Gozo, Blue Hole")
    # clearing the location name leaves the title alone
    dive_cache.update_dive_fields("garmin", "1.json", base_dir=cache_dirs, activity_name="Gozo, Blue Hole", location_name="")
    row = next(r for r in dive_cache.list_garmin_dives(base_dir=cache_dirs) if r["filename"] == "1.json")
    assert (row["activity_name"], row["location_name"]) == ("Gozo, Blue Hole", "")

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


def test_update_dive_fields_garmin_gps_and_water_temp(cache_dirs):
    filepath = dive_cache.update_dive_fields(
        "garmin", "1.json", base_dir=cache_dirs, lat=4.805835, lng=103.686585, water_temp=28.5,
    )
    with open(filepath) as f:
        data = json.load(f)

    for side in ("summary", "details"):
        assert data[side]["summaryDTO"]["startLatitude"] == 4.805835
        assert data[side]["summaryDTO"]["startLongitude"] == 103.686585
        assert data[side]["summaryDTO"]["minTemperature"] == 28.5
        assert data[side]["summaryDTO"]["maxTemperature"] == 28.5
        assert data[side]["summaryDTO"]["averageTemperature"] == 28.5

    dives = dive_cache.list_garmin_dives(base_dir=cache_dirs)
    assert dives[0]["lat"] == 4.805835 and dives[0]["lng"] == 103.686585
    assert dives[0]["water_temp_value"] == 28.5
    assert dives[0]["tanks_editable"] is False


def test_update_dive_fields_garmin_tanks_is_a_no_op(cache_dirs):
    """Garmin's gas API is read-only (rework.md E4) - a tanks override must
    not raise and must not appear anywhere in the written file."""
    filepath = dive_cache.update_dive_fields(
        "garmin", "1.json", base_dir=cache_dirs,
        tanks=[{"oxygen": 32, "helium": 0, "start_pressure": 200, "end_pressure": 50, "volume": 12, "tank_name": "T1"}],
    )
    with open(filepath) as f:
        data = json.load(f)
    assert "tanks" not in data["summary"] and "tanks" not in data["details"]


def test_update_dive_fields_divelogs_gps_water_temp_and_tanks(cache_dirs):
    filepath = dive_cache.update_dive_fields(
        "divelogs", "1.json", base_dir=cache_dirs, lat=4.805835, lng=103.686585, water_temp=28.5,
        tanks=[{"oxygen": 32.0, "helium": 0.0, "start_pressure": 200.0, "end_pressure": 50.0, "volume": 12.0, "tank_name": "T1"}],
    )
    with open(filepath) as f:
        data = json.load(f)

    assert data["lat"] == 4.805835 and data["lng"] == 103.686585
    assert data["depthtemp"] == 28.5
    assert data["tanks"] == [{"o2": 32.0, "he": 0.0, "start_pressure": 200.0, "end_pressure": 50.0, "vol": 12.0, "tankname": "T1"}]

    dives = dive_cache.list_divelogs_dives(base_dir=cache_dirs)
    assert dives[0]["lat"] == 4.805835 and dives[0]["lng"] == 103.686585
    assert dives[0]["water_temp_value"] == 28.5
    assert dives[0]["tanks_editable"] is True
    assert dives[0]["tanks_detail"] == [{"oxygen": 32.0, "helium": 0.0, "start_pressure": 200.0, "end_pressure": 50.0, "volume": 12.0, "tank_name": "T1"}]


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


# ------------------------------------------------- UnifiedDive-cached services

def _unified_dive(**overrides):
    from datetime import datetime
    from src.core.models import GasMixture, UnifiedDive, UnifiedSample
    base = dict(
        date_time=datetime(2026, 8, 29, 10, 15, 0),
        duration=4129, max_depth=18.3, avg_depth=8.196,
        temp_min=27.0, temp_max=29.0, temp_avg=28.0,
        external_ids={"submersion": "1E5A-9"},
        gas_mixtures=[
            GasMixture(oxygen=21, helium=0, start_pressure=162.57, end_pressure=78.04, tank_volume=11.1, tank_name="Left"),
            GasMixture(oxygen=21, helium=0, start_pressure=160.69, end_pressure=106.38, tank_volume=11.1, tank_name="Right"),
        ],
        location="Racha Yai Bay 2", notes="drift", dive_number=42,
        weight=6.0, weight_unit="kilogram", visibility=20.0, visibility_unit="meter",
        buddy="Ann", lat=7.6, lng=98.37,
        samples=[UnifiedSample(depth=0, temp=28, time=0), UnifiedSample(depth=18.3, temp=27, time=600)],
    )
    base.update(overrides)
    return UnifiedDive(**base)


def test_unified_cache_rows_match_the_other_services(tmp_path):
    """Submersion and Subsurface cache the UnifiedDive itself, and the dive
    table must not be able to tell: same row keys, same formatting."""
    dive_cache.save_unified_dives("submersion", [_unified_dive()], base_dir=str(tmp_path))
    rows = dive_cache.list_dives("submersion", base_dir=str(tmp_path))
    assert len(rows) == 1
    row = rows[0]
    assert row["filename"] == "1E5A-9.json" and row["id"] == "1E5A-9"
    assert row["date"] == "2026-08-29" and row["time"] == "10:15:00"
    assert row["duration"] == 69 and row["max_depth"] == "18.30" and row["avg_depth"] == "8.20"   # two decimals, as Garmin
    assert row["sac"] == "12.3"                      # both cylinders counted
    assert row["water_temp"] == "27-29°C (avg 28°C)" and row["water_temp_value"] == 28.0
    assert row["weight"] == "6 kg" and row["visibility"] == "20 m"
    assert row["location"] == "Racha Yai Bay 2" and row["buddy"] == "Ann" and row["dive_number"] == 42
    assert row["tanks"].startswith("Left: 21% O2, 11.1L, 162.57->78.04 bar")
    assert row["tanks_editable"] is True and len(row["tanks_detail"]) == 2
    # Every key a Garmin row has, so one dive table serves all four services.
    garmin_keys = {"id", "dive_number", "date_time", "date", "time", "duration", "max_depth", "avg_depth",
                   "sac", "water_temp", "water_temp_value", "tanks", "tanks_detail", "tanks_editable",
                   "lat", "lng", "location", "notes", "weight", "visibility", "buddy", "filename"}
    assert garmin_keys <= set(row)


def test_unified_cache_samples_and_edits_round_trip(tmp_path):
    from src.core.models import UnifiedDive
    dive_cache.save_unified_dives("subsurface", [_unified_dive(external_ids={"subsurface": "2026-08-29-10:15"})],
                                  base_dir=str(tmp_path))
    row = dive_cache.list_dives("subsurface", base_dir=str(tmp_path))[0]
    assert row["filename"] == "2026-08-29-10_15.json"      # ':' is not a filename
    assert dive_cache.get_samples("subsurface", row["filename"], base_dir=str(tmp_path)) == [
        {"depth": 0.0, "temp": 28.0, "time": 0}, {"depth": 18.3, "temp": 27.0, "time": 600}]

    path = dive_cache.update_dive_fields(
        "subsurface", row["filename"], base_dir=str(tmp_path), location="Edited site", buddy="Bo",
        duration=70, water_temp=26.5, visibility="15 m", weight="7 kg", dive_number="43",
        tanks=[{"oxygen": 32, "helium": 0, "volume": 12, "start_pressure": 200, "end_pressure": 60,
                "tank_name": "Single"}])
    after = dive_cache.list_dives("subsurface", base_dir=str(tmp_path))[0]
    assert after["location"] == "Edited site" and after["buddy"] == "Bo" and after["dive_number"] == 43
    assert after["duration"] == 70 and after["weight"] == "7 kg" and after["visibility"] == "15 m"
    assert after["water_temp"] == "26.5-26.5°C (avg 26.5°C)"
    assert after["tanks"] == "Single: 32% O2, 12L, 200->60 bar"
    # The file stays a valid UnifiedDive - it is what gets pushed back, so a
    # broken one would only surface at the remote update.
    saved = UnifiedDive(**json.load(open(path)))
    assert saved.duration == 4200 and saved.dive_number == 43 and len(saved.gas_mixtures) == 1

    _, external_id = dive_cache.delete_dive_local("subsurface", row["filename"], base_dir=str(tmp_path))
    assert external_id == "2026-08-29-10:15"
    assert dive_cache.list_dives("subsurface", base_dir=str(tmp_path)) == []


def test_unified_push_remote_update_sends_the_cached_dive(tmp_path, monkeypatch):
    from src.core.models import UnifiedDive
    dive_cache.save_unified_dives("submersion", [_unified_dive()], base_dir=str(tmp_path))
    row = dive_cache.list_dives("submersion", base_dir=str(tmp_path))[0]
    path = os.path.join(dive_cache.unified_cache_dir("submersion", base_dir=str(tmp_path)), row["filename"])

    sent = {}

    class FakeAdapter:
        def login(self):
            return True

        def update_dive(self, external_id, dive):
            sent["id"], sent["dive"] = external_id, dive
            return True

        def finish(self):
            sent["finished"] = True

    monkeypatch.setattr(dive_cache, "_unified_adapter", lambda service: FakeAdapter())
    assert dive_cache.push_remote_update("submersion", path) is True
    assert sent["id"] == "1E5A-9" and sent["finished"] is True
    assert isinstance(sent["dive"], UnifiedDive) and sent["dive"].location == "Racha Yai Bay 2"


def test_unified_download_uses_the_adapter(tmp_path, monkeypatch):
    class FakeAdapter:
        def login(self):
            return True

        def fetch_dives(self, date_from=None, date_to=None):
            return [_unified_dive()]

        def finish(self):
            pass

    monkeypatch.setattr(dive_cache, "_unified_adapter", lambda service: FakeAdapter())
    assert dive_cache.download_service_dives("submersion", base_dir=str(tmp_path)) == 1
    assert len(dive_cache.list_dives("submersion", base_dir=str(tmp_path))) == 1
    with pytest.raises(ValueError):
        dive_cache.download_service_dives("garmin", base_dir=str(tmp_path))


def test_unified_cache_never_touches_the_adapters_device_state(tmp_path):
    """SubmersionAdapter keeps device.json and hlc_<id>.json in
    DATA_DIR/submersion - the device identity the Submersion sync mesh knows
    this installation by. The dive cache must not share that directory: an
    overwrite download clears the cache directory, and losing the identity
    would make dive_sync republish as a brand-new device."""
    from src.core.services.submersion.adapter import SubmersionAdapter
    from src.core.config import SubmersionCredentials

    state_dir = os.path.join(str(tmp_path), "submersion")
    os.makedirs(state_dir)
    adapter = SubmersionAdapter(SubmersionCredentials(store_type="folder", folder_path=str(tmp_path / "store")),
                                device_state_dir=state_dir)
    device_file = os.path.join(state_dir, "device.json")
    assert os.path.exists(device_file)
    device_id = json.load(open(device_file))["device_id"]
    assert adapter.device_id == device_id
    adapter.clock.tick()        # the HLC file appears on the first write
    assert any(n.startswith("hlc_") for n in os.listdir(state_dir))

    cache_dir = dive_cache.unified_cache_dir("submersion", base_dir=str(tmp_path))
    assert cache_dir == os.path.join(state_dir, "dives")

    dive_cache.save_unified_dives("submersion", [_unified_dive()], base_dir=str(tmp_path))
    # An overwrite download clears the cache and nothing else.
    dive_cache.save_unified_dives("submersion", [_unified_dive()], base_dir=str(tmp_path), overwrite=True)
    assert os.path.exists(device_file) and json.load(open(device_file))["device_id"] == device_id
    assert any(n.startswith("hlc_") for n in os.listdir(state_dir))

    # ...and the listing reads only the cache, never the state files beside it.
    rows = dive_cache.list_dives("submersion", base_dir=str(tmp_path))
    assert len(rows) == 1 and rows[0]["id"] == "1E5A-9"
    # The editing path still finds a dive in the subdirectory.
    assert dive_cache.get_samples("submersion", rows[0]["filename"], base_dir=str(tmp_path))


def test_prune_cache_dir_removes_only_unaccounted_files(tmp_path):
    """rework.md E18: a refresh used only to write, so a dive deleted on the
    service kept its cached file for ever and a renumbered one left its old
    file behind as a duplicate."""
    import os
    from src.core import dive_cache
    d = tmp_path / "cache"
    d.mkdir()
    for name in ("1.json", "2.json", "3.json", "notes.txt"):
        (d / name).write_text("{}")

    removed = dive_cache.prune_cache_dir(str(d), {"1.json", "3.json"}, "Garmin")
    assert removed == ["2.json"]
    assert sorted(os.listdir(d)) == ["1.json", "3.json", "notes.txt"]   # non-JSON left alone

    # nothing to do is not an error
    assert dive_cache.prune_cache_dir(str(d), {"1.json", "3.json"}) == []

    # an empty keep-set looks like a failed listing, so it never empties a cache
    assert dive_cache.prune_cache_dir(str(d), set()) == []
    assert sorted(os.listdir(d)) == ["1.json", "3.json", "notes.txt"]

    # a missing directory is fine
    assert dive_cache.prune_cache_dir(str(tmp_path / "nope"), {"x.json"}) == []


def test_unified_download_prunes_but_a_plain_save_does_not(tmp_path):
    """`prune` is opt-in: `download_service_dives` passes every dive the
    service has, a bare save_unified_dives call may be saving a subset."""
    import os
    from src.core import dive_cache
    a = _unified_dive()
    b = _unified_dive(external_ids={"submersion": "second"})
    dive_cache.save_unified_dives("submersion", [a, b], base_dir=str(tmp_path))
    directory = dive_cache.unified_cache_dir("submersion", None, str(tmp_path))
    assert len(os.listdir(directory)) == 2

    # saving only one of them leaves the other alone by default
    dive_cache.save_unified_dives("submersion", [a], base_dir=str(tmp_path))
    assert len(os.listdir(directory)) == 2

    # ... but a download says "this is all of them"
    dive_cache.save_unified_dives("submersion", [a], base_dir=str(tmp_path), prune=True)
    assert len(os.listdir(directory)) == 1
