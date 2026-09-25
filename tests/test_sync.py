from datetime import datetime
import pytest
from src.core.models import UnifiedDive, GasMixture, UnifiedSample
from src.core.sync_engine import SyncEngine

def test_unified_dive_validation():
    dive = UnifiedDive(
        date_time=datetime(2026, 6, 22, 12, 0, 0),
        duration=3600,
        max_depth=18.5,
        gas_mixtures=[
            GasMixture(oxygen=21.0, helium=0.0, start_pressure=200.0, end_pressure=50.0, tank_volume=12.0)
        ],
        location="Malmö, Limhamn Ön"
    )
    assert dive.duration == 3600
    assert dive.max_depth == 18.5
    assert len(dive.gas_mixtures) == 1
    assert dive.gas_mixtures[0].oxygen == 21.0

def test_matching_logic():
    # Setup two lists of dives with slight timestamp offset
    g_dive1 = UnifiedDive(
        date_time=datetime(2026, 6, 22, 12, 0, 0),
        duration=3000,
        max_depth=20.0,
        external_ids={"garmin": "1001"}
    )
    g_dive2 = UnifiedDive(
        date_time=datetime(2026, 6, 22, 16, 0, 0),
        duration=3000,
        max_depth=15.0,
        external_ids={"garmin": "1002"}
    )
    
    # Divelogs dive 1 is 5 minutes later (should match since grace is 15 mins)
    d_dive1 = UnifiedDive(
        date_time=datetime(2026, 6, 22, 12, 5, 0),
        duration=3000,
        max_depth=20.0,
        external_ids={"divelogs": "2001"}
    )
    # Divelogs dive 2 is 25 minutes later (should NOT match)
    d_dive2 = UnifiedDive(
        date_time=datetime(2026, 6, 22, 16, 25, 0),
        duration=3000,
        max_depth=15.0,
        external_ids={"divelogs": "2002"}
    )

    engine = SyncEngine()
    engine.settings.grace_window_minutes = 15
    
    matched, unique_g, unique_d = engine.match_dives([g_dive1, g_dive2], [d_dive1, d_dive2])
    
    assert len(matched) == 1
    assert matched[0][0].external_ids["garmin"] == "1001"
    assert matched[0][1].external_ids["divelogs"] == "2001"
    
    assert len(unique_g) == 1
    assert unique_g[0].external_ids["garmin"] == "1002"
    
    assert len(unique_d) == 1
    assert unique_d[0].external_ids["divelogs"] == "2002"

def test_weight_and_visibility_mapping():
    from src.core.services.garmin import GarminAdapter
    from src.core.services.divelogs import DivelogsAdapter
    
    # 1. Test Garmin payload mapping to UnifiedDive
    garmin_details = {
        "activityId": 12345,
        "summaryDTO": {
            "startTimeLocal": "2026-06-22T12:00:00",
            "duration": 3000,
            "maxDepth": 15.0
        },
        "diveInfo": {
            "weight": 5.0,
            "weightUnit": {
                "unitId": 8,
                "unitKey": "kilogram",
                "factor": 1000.0
            },
            "visibility": 9.0,
            "visibilityUnit": {
                "unitId": 1,
                "unitKey": "meter",
                "factor": 100.0
            }
        }
    }
    
    g_adapter = GarminAdapter("dummy", "dummy")
    dive = g_adapter._map_to_unified({}, garmin_details)
    
    assert dive.weight == 5.0
    assert dive.weight_unit == "kilogram"
    assert dive.visibility == 9.0
    assert dive.visibility_unit == "meter"
    
    # 2. Test mapping from UnifiedDive to Divelogs payload (Metric)
    dl_adapter_metric = DivelogsAdapter("dummy", "dummy")
    dl_adapter_metric.imperial_units = False
    
    payload_metric = dl_adapter_metric._map_from_unified(dive)
    assert payload_metric["weights"] == 5.0
    assert payload_metric["visibility"] == "9 m"
    
    # 3. Test mapping from UnifiedDive to Divelogs payload (Imperial)
    dl_adapter_imperial = DivelogsAdapter("dummy", "dummy")
    dl_adapter_imperial.imperial_units = True
    
    payload_imperial = dl_adapter_imperial._map_from_unified(dive)
    assert payload_imperial["weights"] == 11.02
    assert payload_imperial["visibility"] == "29.5 ft"

    # 4. Test mapping back from Divelogs payload to UnifiedDive (Metric)
    dl_data_metric = {
        "date": "2026-06-22",
        "time": "12:00:00",
        "duration": 3000,
        "maxdepth": 15.0,
        "weights": 5.0,
        "visibility": "9 m"
    }
    dive_back_metric = dl_adapter_metric._map_to_unified(dl_data_metric)
    assert dive_back_metric.weight == 5.0
    assert dive_back_metric.weight_unit == "kilogram"
    assert dive_back_metric.visibility == 9.0
    assert dive_back_metric.visibility_unit == "meter"
    
    # 5. Test mapping back from Divelogs payload to UnifiedDive (Imperial)
    dl_data_imperial = {
        "date": "2026-06-22",
        "time": "12:00:00",
        "duration": 3000,
        "maxdepth": 15.0,
        "weights": 11.02,
        "visibility": "29.5 ft"
    }
    dive_back_imperial = dl_adapter_imperial._map_to_unified(dl_data_imperial)
    assert dive_back_imperial.weight == 11.02
    assert dive_back_imperial.weight_unit == "pound"
    assert dive_back_imperial.visibility == 29.5
    assert dive_back_imperial.visibility_unit == "foot"

def test_matched_dives_update_fields(tmp_path):
    g_dive = UnifiedDive(
        date_time=datetime(2026, 6, 22, 12, 0, 0),
        duration=3000,
        max_depth=20.0,
        external_ids={"garmin": "1001", "divelogs": "2001"},
        notes="Garmin notes",
        buddy="Guy",
        weight=5.0,
        weight_unit="kilogram",
        visibility=9.0,
        visibility_unit="meter",
        lat=4.23,
        lng=118.63,
        samples=[UnifiedSample(depth=1.2, temp=30.0, time=0)]
    )
    
    d_dive = UnifiedDive(
        date_time=datetime(2026, 6, 22, 12, 0, 0),
        duration=3000,
        max_depth=20.0,
        external_ids={"garmin": "1001", "divelogs": "2001"},
        notes="Old notes",
        buddy=None,
        weight=0.0,
        weight_unit=None,
        visibility=0.0,
        visibility_unit=None,
        lat=None,
        lng=None,
        samples=[]
    )
    
    engine = SyncEngine(settings_path=str(tmp_path / "settings.json"), credentials_path=str(tmp_path / "credentials.json"))

    # Mock settings loading
    from src.core.config import SettingsModel, SyncFilters
    from src.core.fields import legacy_field_links
    mock_settings = SettingsModel(
        directionality="to_divelogs",
        sync_filters=SyncFilters(only_new=False),
        grace_window_minutes=15,
        api_cooldown_seconds=0.1,
        field_links=legacy_field_links(),  # this test checks the old Garmin-wins semantics
    )
    
    import src.core.config
    original_load = src.core.config.ConfigManager.load_settings
    src.core.config.ConfigManager.load_settings = lambda *args, **kwargs: mock_settings
    
    class MockGarmin:
        def login(self):
            return True
        def fetch_dives(self, date_from=None, date_to=None):
            return [g_dive]
        def update_dive(self, ext_id, dive):
            pass
            
    class MockDivelogs:
        def __init__(self):
            self.updated_dive = None
        def login(self):
            return True
        def fetch_dives(self, date_from=None, date_to=None):
            return [d_dive]
        def update_dive(self, ext_id, dive):
            self.updated_id = ext_id
            self.updated_dive = dive
            return True
            
    engine.garmin = MockGarmin()
    engine.divelogs = MockDivelogs()
    
    try:
        results = engine.run_sync(dry_run=False)
        
        assert len(results["updated_on_divelogs"]) == 1
        assert len(results["updated_on_garmin"]) == 0
        
        updated = engine.divelogs.updated_dive
        assert updated is not None
        assert updated.notes == "Garmin notes"
        assert updated.buddy == "Guy"
        assert updated.weight == 5.0
        assert updated.visibility == 9.0
        assert updated.lat == 4.23
        assert updated.lng == 118.63
        assert len(updated.samples) == 1
        assert updated.samples[0].depth == 1.2
    finally:
        # Restore ConfigManager load_settings
        src.core.config.ConfigManager.load_settings = original_load

def test_gps_and_profile_graph_mapping():
    from src.core.services.garmin import GarminAdapter
    from src.core.services.divelogs import DivelogsAdapter
    
    # 1. Garmin GPS fallback tests
    g_adapter = GarminAdapter("dummy", "dummy")
    
    # Case A: has startLatitude and startLongitude
    details_a = {
        "summaryDTO": {
            "startTimeLocal": "2026-06-22T12:00:00",
            "duration": 3000,
            "maxDepth": 15.0,
            "startLatitude": 4.23,
            "startLongitude": 118.63,
            "endLatitude": 4.24,
            "endLongitude": 118.64
        }
    }
    dive_a = g_adapter._map_to_unified({}, details_a)
    assert dive_a.lat == 4.23
    assert dive_a.lng == 118.63
    
    # Case B: has no startLatitude/startLongitude but has endLatitude/endLongitude
    details_b = {
        "summaryDTO": {
            "startTimeLocal": "2026-06-22T12:00:00",
            "duration": 3000,
            "maxDepth": 15.0,
            "endLatitude": 4.24,
            "endLongitude": 118.64
        }
    }
    dive_b = g_adapter._map_to_unified({}, details_b)
    assert dive_b.lat == 4.24
    assert dive_b.lng == 118.64

    # Case C: has None metricDescriptors and/or None activityDetailMetrics
    details_c = {
        "summaryDTO": {
            "startTimeLocal": "2026-06-22T12:00:00",
            "duration": 3000,
            "maxDepth": 15.0
        }
    }
    activity_details_none = {
        "metricDescriptors": None,
        "activityDetailMetrics": None
    }
    dive_c = g_adapter._map_to_unified({}, details_c, activity_details_none)
    assert len(dive_c.samples) == 0

    # 2. Garmin profile telemetry mapping
    activity_details = {
        "metricDescriptors": [
            {"metricsIndex": 0, "key": "sumDuration"},
            {"metricsIndex": 1, "key": "directDepth"},
            {"metricsIndex": 2, "key": "directAirTemperature"}
        ],
        "activityDetailMetrics": [
            {"metrics": [0.0, 1.2, 30.0]},
            {"metrics": [10.0, 5.5, 29.5]},
            {"metrics": [20.0, 10.2, 28.0]}
        ]
    }
    
    dive_telemetry = g_adapter._map_to_unified({}, details_a, activity_details)
    assert len(dive_telemetry.samples) == 3
    assert dive_telemetry.samples[0].depth == 1.2
    assert dive_telemetry.samples[0].temp == 30.0
    assert dive_telemetry.samples[0].time == 0
    
    assert dive_telemetry.samples[1].depth == 5.5
    assert dive_telemetry.samples[1].temp == 29.5
    assert dive_telemetry.samples[1].time == 10
    
    assert dive_telemetry.samples[2].depth == 10.2
    assert dive_telemetry.samples[2].temp == 28.0
    assert dive_telemetry.samples[2].time == 20

    # 3. Divelogs mapping payload (Metric)
    dl_adapter_metric = DivelogsAdapter("dummy", "dummy")
    dl_adapter_metric.imperial_units = False
    
    payload_metric = dl_adapter_metric._map_from_unified(dive_telemetry)
    assert payload_metric["lat"] == 4.23
    assert payload_metric["lng"] == 118.63
    assert payload_metric["sampledata"] == [
        {"d": 1.2, "t": 30.0},
        {"d": 5.5, "t": 29.5},
        {"d": 10.2, "t": 28.0}
    ]
    assert payload_metric["samplerate"] == 10
    
    # 4. Divelogs mapping payload (Imperial)
    dl_adapter_imperial = DivelogsAdapter("dummy", "dummy")
    dl_adapter_imperial.imperial_units = True
    
    payload_imperial = dl_adapter_imperial._map_from_unified(dive_telemetry)
    assert payload_imperial["lat"] == 4.23
    assert payload_imperial["lng"] == 118.63
    assert payload_imperial["sampledata"][0] == {"d": 3.94, "t": 86.0}
    assert payload_imperial["sampledata"][1] == {"d": 18.04, "t": 85.1}
    assert payload_imperial["samplerate"] == 10

    # 5. Mapping back from Divelogs payload to UnifiedDive (Metric)
    dl_data_metric = {
        "date": "2026-06-22",
        "time": "12:00:00",
        "duration": 3000,
        "maxdepth": 15.0,
        "lat": 4.23,
        "lng": 118.63,
        "sampledata": [
            {"d": 1.2, "t": 30.0},
            {"d": 5.5, "t": 29.5},
            {"d": 10.2, "t": 28.0}
        ],
        "samplerate": 10
    }
    
    dive_back_metric = dl_adapter_metric._map_to_unified(dl_data_metric)
    assert dive_back_metric.lat == 4.23
    assert dive_back_metric.lng == 118.63
    assert len(dive_back_metric.samples) == 3
    assert dive_back_metric.samples[0].depth == 1.2
    assert dive_back_metric.samples[0].temp == 30.0
    assert dive_back_metric.samples[0].time == 0
    assert dive_back_metric.samples[2].time == 20
    
    # 6. Mapping back from Divelogs payload to UnifiedDive (Imperial)
    dl_data_imperial = {
        "date": "2026-06-22",
        "time": "12:00:00",
        "duration": 3000,
        "maxdepth": 15.0,
        "lat": 4.23,
        "lng": 118.63,
        "sampledata": [
            {"d": 3.94, "t": 86.0},
            {"d": 18.04, "t": 85.1}
        ],
        "samplerate": 10
    }
    
    dive_back_imperial = dl_adapter_imperial._map_to_unified(dl_data_imperial)
    assert dive_back_imperial.lat == 4.23
    assert dive_back_imperial.lng == 118.63
    assert len(dive_back_imperial.samples) == 2
    assert round(dive_back_imperial.samples[0].depth, 2) == 1.2
    assert round(dive_back_imperial.samples[0].temp, 2) == 30.0
    assert dive_back_imperial.samples[1].time == 10

def test_multi_account_handling(tmp_path):
    import os
    import json
    import pytest
    from src.core.config import CredentialsModel
    
    settings_data = {
        "directionality": "to_divelogs",
        "sync_filters": {},
        "grace_window_minutes": 15,
        "api_cooldown_seconds": 0.0,
        "schedule": []
    }
    
    creds_data = {
        "garmin": [
            {"username": "user1@garmin", "password": "pass1", "token_dir": str(tmp_path / "tokens" / "user1")},
            {"username": "user2@garmin", "password": "pass2", "token_dir": str(tmp_path / "tokens" / "user2")}
        ],
        "divelogs": [
            {"username": "user1_divelogs", "password": "pass1"},
            {"username": "user2_divelogs", "password": "pass2"}
        ]
    }
    
    settings_path = os.path.join(tmp_path, "settings.json")
    creds_path = os.path.join(tmp_path, "credentials.json")
    
    with open(settings_path, "w") as f:
        json.dump(settings_data, f)
    with open(creds_path, "w") as f:
        json.dump(creds_data, f)
        
    creds_model = CredentialsModel.model_validate(creds_data)
    assert len(creds_model.get_garmin_accounts()) == 2
    assert len(creds_model.get_divelogs_accounts()) == 2
    assert creds_model.get_garmin_accounts()[0].username == "user1@garmin"
    
    with pytest.raises(ValueError, match="Multiple Garmin accounts configured"):
        SyncEngine(settings_path=settings_path, credentials_path=creds_path)
        
    with pytest.raises(ValueError, match="No Garmin account configured matching username"):
        SyncEngine(settings_path=settings_path, credentials_path=creds_path, garmin_username="invalid_user")
        
    engine = SyncEngine(
        settings_path=settings_path, 
        credentials_path=creds_path, 
        garmin_username="user2@garmin",
        divelogs_username="user1_divelogs"
    )
    
    assert engine.garmin_username == "user2@garmin"
    assert engine.divelogs_username == "user1_divelogs"
    assert engine.garmin_dir_name == os.path.join("garmin", "user2@garmin")
    assert engine.divelogs_dir_name == os.path.join("divelogs", "user1_divelogs")


def test_garmin_location_overlay_does_not_duplicate():
    from src.core.services.garmin import GarminAdapter
    g = GarminAdapter("dummy", "dummy")
    base = {"summaryDTO": {"startTimeLocal": "2026-06-22T12:00:00", "duration": 3000, "maxDepth": 15.0}}
    # Garmin's own naming: activity name embeds the location name
    dive = g._map_to_unified({}, dict(base, activityName="P.Tenggul Single-Gas Dive", locationName="P.Tenggul"))
    assert dive.location == "P.Tenggul Single-Gas Dive"
    # identical names (what earlier sync rounds produced)
    dive = g._map_to_unified({}, dict(base, activityName="Sweden, Malmö, Limhamn Ön", locationName="Sweden, Malmö, Limhamn Ön"))
    assert dive.location == "Sweden, Malmö, Limhamn Ön"
    # genuinely different: still joined
    dive = g._map_to_unified({}, dict(base, activityName="Wreck", locationName="Larnaca"))
    assert dive.location == "Larnaca, Wreck"
    dive = g._map_to_unified({}, dict(base, activityName="Wreck"))
    assert dive.location == "Wreck"


def test_divelogs_zero_coordinates_mean_none():
    from src.core.services.divelogs import DivelogsAdapter
    d = DivelogsAdapter("dummy", "dummy")
    dive = d._map_to_unified({"date": "2026-06-22", "time": "12:00:00", "duration": 100, "maxdepth": 10.0, "lat": 0, "lng": 0})
    assert dive.lat is None and dive.lng is None
    dive = d._map_to_unified({"date": "2026-06-22", "time": "12:00:00", "duration": 100, "maxdepth": 10.0, "lat": 0, "lng": 5.0})
    assert (dive.lat, dive.lng) == (0.0, 5.0)


def test_divelogs_profile_is_resampled_onto_a_uniform_grid():
    from src.core.services.divelogs import DivelogsAdapter
    from src.core.fields import resample_profile
    # Garmin-style irregular spacing: 1 s near the start, then 7 s
    irregular = [UnifiedSample(depth=0.0, temp=30.0, time=0), UnifiedSample(depth=1.0, temp=30.0, time=1),
                 UnifiedSample(depth=2.0, temp=30.0, time=2), UnifiedSample(depth=9.0, temp=29.0, time=9),
                 UnifiedSample(depth=10.0, temp=29.0, time=10), UnifiedSample(depth=17.0, temp=28.0, time=17)]
    rate, grid = resample_profile(irregular)
    assert rate == 1 and [s.time for s in grid] == list(range(0, 18))
    assert grid[5].depth == 5.0 and grid[5].temp == pytest.approx(29.57, abs=0.01)  # interpolated
    assert grid[-1].depth == 17.0

    # uniform input passes through untouched
    uniform = [UnifiedSample(depth=float(i), temp=None, time=i * 10) for i in range(5)]
    assert resample_profile(uniform) == (10, uniform)
    # untimed input: rate 1, unchanged
    untimed = [UnifiedSample(depth=1.0), UnifiedSample(depth=2.0)]
    assert resample_profile(untimed) == (1, untimed)

    d = DivelogsAdapter("dummy", "dummy")
    dive = UnifiedDive(date_time=datetime(2026, 6, 22, 12), duration=18, max_depth=17.0, samples=irregular)
    payload = d._map_from_unified(dive)
    assert payload["samplerate"] == 1 and len(payload["sampledata"]) == 18
    assert payload["sampledata"][9] == {"d": 9.0, "t": 29.0}


def test_garmin_reads_utc_and_zone_and_stamps_uploads_with_a_real_zone():
    from src.core.services.garmin import GarminAdapter
    g = GarminAdapter("dummy", "dummy")
    dive = g._map_to_unified({}, {
        "activityId": 1, "timeZoneUnitDTO": {"unitId": 135, "unitKey": "Asia/Hong_Kong", "timeZone": "Asia/Hong_Kong"},
        "summaryDTO": {"startTimeLocal": "1991-10-06T20:00:00.0", "startTimeGMT": "1991-10-06T12:00:00.0", "duration": 100, "maxDepth": 10.0},
    })
    assert dive.date_time == datetime(1991, 10, 6, 20) and dive.date_time_utc == datetime(1991, 10, 6, 12)
    assert dive.timezone == "Asia/Hong_Kong"

    # a Garmin-origin dive keeps its own zone on the way out
    assert g._map_from_unified(dive)["timeZoneUnitDTO"] == {"unitKey": "Asia/Hong_Kong"}

    # a Divelogs-origin dive (no zone): not logged in -> UTC, no network
    foreign = UnifiedDive(date_time=datetime(2026, 6, 22, 12), duration=100, max_depth=10.0)
    assert g._map_from_unified(foreign)["timeZoneUnitDTO"] == {"unitKey": "UTC"}

    # explicit override wins
    g.upload_timezone = "Asia/Kuala_Lumpur"
    assert g._map_from_unified(foreign)["timeZoneUnitDTO"] == {"unitKey": "Asia/Kuala_Lumpur"}

    # detection from the newest dive on the account
    g2 = GarminAdapter("dummy", "dummy")
    g2.logged_in = True
    g2.cooldown_seconds = 0
    class Client:
        def get_activities(self, start, limit, activitytype=None):
            if start:
                return []
            return [{"activityId": "old", "activityType": {"typeKey": "diving"}, "startTimeLocal": "2020-01-01 10:00:00"},
                    {"activityId": "new", "activityType": {"typeKey": "diving"}, "startTimeLocal": "2026-01-01 10:00:00"}]
        def connectapi(self, url, params=None):
            assert url.endswith("/new")
            return {"timeZoneUnitDTO": {"unitKey": "Asia/Kuala_Lumpur"}}
    g2.client = Client()
    assert g2.resolve_upload_timezone() == "Asia/Kuala_Lumpur"
    assert g2._map_from_unified(foreign)["timeZoneUnitDTO"] == {"unitKey": "Asia/Kuala_Lumpur"}


def test_garmin_update_dive_sends_minimal_summary_dto_not_full_echo():
    """update_dive() must never echo the whole existing summaryDTO back: a
    real dive's minElevation is negative (depth under the surface), and
    Garmin's PUT re-validates every field it receives, rejecting a negative
    minElevation with 400 MEASUREMENT_NOT_VALID even though it's the dive's
    own unchanged value. Discovered live 2026-09-23 (docs/garmin_diving_api.md);
    this is also what makes water temperature (D4) writable through the same
    call. Only the fields that actually changed may be sent."""
    from src.core.services.garmin import GarminAdapter
    g = GarminAdapter("dummy", "dummy")
    g.logged_in = True
    g.cooldown_seconds = 0

    current_raw = {
        "activityId": "1",
        "activityName": "Dive",
        "summaryDTO": {
            "startLatitude": 4.805835, "startLongitude": 103.686585,
            "minElevation": -8.8, "maxElevation": 0.2,
            "minTemperature": 29.0, "maxTemperature": 30.0, "averageTemperature": 29.0,
            "maxDepth": 24.0,
        },
        "diveInfo": {},
    }
    put_calls = []

    class FakeApiClient:
        @staticmethod
        def put(_domain, _path, json=None, api=None):
            put_calls.append(json)
            return {}

    class FakeClient:
        client = FakeApiClient()

        def connectapi(self, url, params=None):
            return current_raw

    g.client = FakeClient()

    new_dive = UnifiedDive(
        date_time=datetime(2026, 6, 27, 11, 24), duration=2884, max_depth=24.0,
        location="Dive", lat=4.805935, lng=103.686585, temp_min=29.3, temp_max=30.3, temp_avg=29.3,
    )
    assert g.update_dive("1", new_dive) is True
    assert len(put_calls) == 1
    summary_dto = put_calls[0]["summaryDTO"]
    # lng is unchanged, but travels alongside lat anyway: Garmin silently
    # no-ops a lone startLatitude with no startLongitude (also found live
    # 2026-09-23, see docs/garmin_diving_api.md).
    assert summary_dto == {
        "startLatitude": 4.805935, "startLongitude": 103.686585,
        "minTemperature": 29.3, "maxTemperature": 30.3, "averageTemperature": 29.3,
        # start time and duration differ from the stored dive, so they go too
        # (rework.md G8: these are writable after all); maxDepth is unchanged
        # at 24.0, so the changed-fields-only rule keeps it out
        "startTimeLocal": "2026-06-27T11:24:00.000000", "duration": 2884,
    }
    assert "minElevation" not in summary_dto and "maxElevation" not in summary_dto
    assert "maxDepth" not in summary_dto
    # startTimeGMT is never sent: Garmin recomputes it from the local time and
    # the activity's zone (G8), so a GMT of our own could only disagree
    assert "startTimeGMT" not in summary_dto


def test_garmin_update_dive_sends_depth_and_duration_changes(tmp_path):
    """rework.md G8, probed live 2026-09-23: duration, max depth, average
    depth and start time are writable through the minimal summaryDTO PUT, on
    hand-entered and device-logged dives alike. They were locked before on the
    assumption Garmin derives them from the recording."""
    from src.core.services.garmin import GarminAdapter
    g = GarminAdapter("dummy", "dummy")
    g.logged_in = True
    g.cooldown_seconds = 0
    current_raw = {
        "activityId": "2",
        "activityName": "Dive",          # matches the dives below, so only summaryDTO can differ
        "summaryDTO": {"startTimeLocal": "2026-06-27T11:24:00.0", "duration": 2884,
                       "maxDepth": 24.0, "averageDepth": 12.5, "minElevation": -24.0},
        "diveInfo": {},
    }
    put_calls = []

    class FakeApiClient:
        @staticmethod
        def put(_domain, _path, json=None, api=None):
            put_calls.append(json)
            return {}

    class FakeClient:
        client = FakeApiClient()

        def connectapi(self, url, params=None):
            return current_raw

    g.client = FakeClient()

    # every one of the four differs from what is stored
    changed = UnifiedDive(date_time=datetime(2026, 6, 27, 11, 30), duration=3000,
                          max_depth=26.5, avg_depth=13.0, location="Dive")
    assert g.update_dive("2", changed) is True
    assert put_calls[0]["summaryDTO"] == {
        "startTimeLocal": "2026-06-27T11:30:00.000000", "duration": 3000,
        "maxDepth": 26.5, "averageDepth": 13.0,
    }

    # nothing differs -> nothing to send, so no PUT at all (and in particular
    # no summaryDTO echoing minElevation back, which would be rejected)
    put_calls.clear()
    same = UnifiedDive(date_time=datetime(2026, 6, 27, 11, 24), duration=2884,
                       max_depth=24.0, avg_depth=12.5, location="Dive")
    assert g.update_dive("2", same) is True
    assert put_calls == [], put_calls


def test_engine_passes_zone_override_to_garmin(tmp_path):
    import json, os
    from src.core.sync_engine import SyncEngine
    settings = {"garmin_timezone": "Europe/Stockholm", "sync_filters": {"only_new": False}}
    path = os.path.join(tmp_path, "settings.json")
    with open(path, "w") as f:
        json.dump(settings, f)
    engine = SyncEngine(settings_path=path, mock_data_dir=str(tmp_path))
    engine.run_sync(dry_run=True)
    assert engine.source.helper.upload_timezone is None  # mock wraps a helper adapter, no override needed
    assert engine.settings.garmin_timezone == "Europe/Stockholm"


# ---------------------------------------------------------------- E5: tank order and role

def test_divelogs_sends_tank_index_and_preserves_order():
    from src.core.services.divelogs import DivelogsAdapter
    from src.core.models import GasMixture
    d = DivelogsAdapter("dummy", "dummy")
    dive = UnifiedDive(date_time=datetime(2026, 6, 22, 12), duration=100, max_depth=10.0, gas_mixtures=[
        GasMixture(oxygen=21.0, start_pressure=200.0, end_pressure=60.0, tank_volume=11.1, tank_name="Left"),
        GasMixture(oxygen=32.0, start_pressure=210.0, end_pressure=80.0, tank_volume=7.0, tank_name="Stage"),
    ])
    payload = d._map_from_unified(dive)
    assert [t["index"] for t in payload["tanks"]] == [0, 1]
    assert [t["tankname"] for t in payload["tanks"]] == ["Left", "Stage"]
    # round trip back: order preserved, no role invented
    back = d._map_to_unified({"date": "2026-06-22", "time": "12:00:00", "duration": 100, "maxdepth": 10.0,
                              "tanks": payload["tanks"]})
    assert [g.tank_name for g in back.gas_mixtures] == ["Left", "Stage"]
    assert all(g.tank_role is None for g in back.gas_mixtures)


def test_garmin_tank_role_stays_unmapped():
    """Garmin's diveGases 'status' field has no verified meaning (E4); reading
    must not invent a role from it."""
    from src.core.services.garmin import GarminAdapter
    g = GarminAdapter("dummy", "dummy")
    details = {"summaryDTO": {"startTimeLocal": "2026-06-22T12:00:00", "duration": 100, "maxDepth": 10.0},
              "diveInfo": {"diveGases": [{"gasIndex": 0, "oxygenContent": 21, "status": 0},
                                        {"gasIndex": 1, "oxygenContent": 21, "status": 2}]}}
    dive = g._map_to_unified({}, details)
    assert len(dive.gas_mixtures) == 2 and all(t.tank_role is None for t in dive.gas_mixtures)


def test_garmin_download_skips_dives_already_cached(tmp_path, monkeypatch):
    """rework.md E15: a refresh used to cost three API calls and three
    cool-downs per dive, every dive, every time. A dive already on disk whose
    listing entry is unchanged is now left alone."""
    import json as _json
    from src.core.sync_engine import SyncEngine, _garmin_listing_fingerprint

    diving = {"typeKey": "diving"}
    listing = [
        {"activityId": 1, "activityName": "One", "diveNumber": 1, "maxDepth": 18.0, "activityType": diving,
         "startTimeLocal": "2026-06-01 10:00:00", "metadataDTO": {"diveNumber": 1}},
        {"activityId": 2, "activityName": "Two", "diveNumber": 2, "maxDepth": 22.0, "activityType": diving,
         "startTimeLocal": "2026-06-02 10:00:00", "metadataDTO": {"diveNumber": 2}},
        {"activityId": 3, "activityName": "Three", "diveNumber": 3, "maxDepth": 12.0, "activityType": diving,
         "startTimeLocal": "2026-06-03 10:00:00", "metadataDTO": {"diveNumber": 3}},
    ]
    detail_calls = []

    class FakeGarminClient:
        def connectapi(self, url, params=None):
            if "tanksensor" in url:
                return {}
            detail_calls.append(url.rsplit("/", 1)[-1])
            return {"activityId": url.rsplit("/", 1)[-1], "metadataDTO": {}}

        def get_activities(self, start, limit, activitytype=None):
            return listing if start == 0 else []

        def get_activity_details(self, activity_id):
            return {}

    # a minimal stand-in for the Garmin adapter the download path uses
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
    monkeypatch.setattr(type(engine), "garmin_dir_name", property(lambda self: "garmin"))

    base = str(tmp_path / "cache")
    assert engine.download_and_save_raw_data(mock_data_dir=base, include_divelogs=False) is True
    assert sorted(detail_calls) == ["1", "2", "3"], detail_calls

    # second run: nothing changed, so nothing is fetched again
    detail_calls.clear()
    assert engine.download_and_save_raw_data(mock_data_dir=base, include_divelogs=False) is True
    assert detail_calls == [], detail_calls

    # a dive edited on Garmin (its listing entry changed) is re-fetched, alone
    listing[1] = dict(listing[1], activityName="Two, renamed")   # edited on Garmin
    detail_calls.clear()
    assert engine.download_and_save_raw_data(mock_data_dir=base, include_divelogs=False) is True
    assert detail_calls == ["2"], detail_calls

    # ... and overwrite=True rebuilds everything, for edits the listing hides
    detail_calls.clear()
    assert engine.download_and_save_raw_data(mock_data_dir=base, overwrite=True, include_divelogs=False) is True
    assert sorted(detail_calls) == ["1", "2", "3"], detail_calls

    # the fingerprint ignores per-session noise, or nothing would ever match
    entry = dict(listing[0], userRoles=["a"], ownerDisplayName="me")
    assert _garmin_listing_fingerprint(entry) == _garmin_listing_fingerprint(listing[0])


def test_garmin_download_removes_dives_deleted_or_renumbered_on_garmin(tmp_path, monkeypatch):
    """rework.md E18: the cache only ever grew. A dive deleted on Garmin kept
    its file, and one given a new dive number got a second file while the old
    one stayed behind - both showed as ghosts in the desktop dive table."""
    import json as _json
    import os as _os
    from src.core.sync_engine import SyncEngine

    diving = {"typeKey": "diving"}
    listing = [
        {"activityId": 1, "activityName": "One", "diveNumber": 1, "activityType": diving,
         "startTimeLocal": "2026-06-01 10:00:00", "metadataDTO": {"diveNumber": 1}},
        {"activityId": 2, "activityName": "Two", "diveNumber": 2, "activityType": diving,
         "startTimeLocal": "2026-06-02 10:00:00", "metadataDTO": {"diveNumber": 2}},
    ]

    class FakeGarminClient:
        def connectapi(self, url, params=None):
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
    monkeypatch.setattr(type(engine), "garmin_dir_name", property(lambda self: "garmin"))

    base = str(tmp_path / "cache")
    garmin_dir = _os.path.join(base, "garmin")
    assert engine.download_and_save_raw_data(mock_data_dir=base, include_divelogs=False) is True
    assert sorted(_os.listdir(garmin_dir)) == ["1_2026-06-01_100000_1.json", "2_2026-06-02_100000_2.json"]

    # dive 2 deleted on Garmin -> its cache file goes with it
    listing.pop()
    assert engine.download_and_save_raw_data(mock_data_dir=base, include_divelogs=False) is True
    assert sorted(_os.listdir(garmin_dir)) == ["1_2026-06-01_100000_1.json"]

    # dive 1 renumbered to 9 on Garmin -> written under its new number, the
    # old name not left behind
    listing[0] = dict(listing[0], diveNumber=9, metadataDTO={"diveNumber": 9})
    assert engine.download_and_save_raw_data(mock_data_dir=base, include_divelogs=False) is True
    assert sorted(_os.listdir(garmin_dir)) == ["9_2026-06-01_100000_1.json"]
    assert _json.load(open(_os.path.join(garmin_dir, "9_2026-06-01_100000_1.json")))["summary"]["diveNumber"] == 9

    # a listing that comes back empty is treated as a failure, not an empty
    # account: the cache is left alone rather than wiped
    listing.clear()
    assert engine.download_and_save_raw_data(mock_data_dir=base, include_divelogs=False) is True
    assert sorted(_os.listdir(garmin_dir)) == ["9_2026-06-01_100000_1.json"]


def test_garmin_download_retries_a_dive_whose_telemetry_failed(tmp_path, monkeypatch):
    """The skip is keyed on the listing entry, which does not change just
    because a fetch failed - so a dive cached without its profile would be
    skipped for ever. It is marked incomplete and retried instead."""
    import json as _json
    import os as _os
    from src.core.sync_engine import SyncEngine

    diving = {"typeKey": "diving"}
    listing = [{"activityId": 7, "activityName": "Seven", "diveNumber": 7, "activityType": diving,
                "startTimeLocal": "2026-06-07 10:00:00", "metadataDTO": {"diveNumber": 7}}]
    telemetry_fails = {"yes": True}
    detail_calls = []

    class FakeGarminClient:
        def connectapi(self, url, params=None):
            if "tanksensor" in url:
                raise RuntimeError("404 not found")        # normal: no tank sensor on this dive
            detail_calls.append(url.rsplit("/", 1)[-1])
            return {"activityId": url.rsplit("/", 1)[-1], "metadataDTO": {}}

        def get_activities(self, start, limit, activitytype=None):
            return listing if start == 0 else []

        def get_activity_details(self, activity_id):
            if telemetry_fails["yes"]:
                raise RuntimeError("429 Too Many Requests")
            return {"samples": "ok"}

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
    monkeypatch.setattr(type(engine), "garmin_dir_name", property(lambda self: "garmin"))

    base = str(tmp_path / "cache")
    assert engine.download_and_save_raw_data(mock_data_dir=base, include_divelogs=False) is True
    cached_file = _os.path.join(base, "garmin", "7_2026-06-07_100000_7.json")
    payload = _json.load(open(cached_file))
    # the rest of the dive is kept, but the hole is recorded - and a 404 on
    # the tank sensor is "absent", not "failed"
    assert payload["details"] and payload["activityDetails"] is None
    assert payload["incomplete"] == ["activityDetails"]

    # so the next refresh fetches it again rather than trusting the cache
    detail_calls.clear()
    telemetry_fails["yes"] = False
    assert engine.download_and_save_raw_data(mock_data_dir=base, include_divelogs=False) is True
    assert detail_calls == ["7"], detail_calls
    payload = _json.load(open(cached_file))
    assert payload["activityDetails"] == {"samples": "ok"} and "incomplete" not in payload

    # now that it is whole, it is skipped
    detail_calls.clear()
    assert engine.download_and_save_raw_data(mock_data_dir=base, include_divelogs=False) is True
    assert detail_calls == [], detail_calls
