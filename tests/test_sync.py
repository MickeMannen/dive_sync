from datetime import datetime
from src.core.models import UnifiedDive, GasMixture
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
