from datetime import datetime
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

def test_matched_dives_update_fields():
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
    
    engine = SyncEngine()
    
    # Mock settings loading
    from src.core.config import SettingsModel, SyncFilters
    mock_settings = SettingsModel(
        directionality="to_divelogs",
        sync_filters=SyncFilters(only_new=False),
        grace_window_minutes=15,
        api_cooldown_seconds=0.1
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
