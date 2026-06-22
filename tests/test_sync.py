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
