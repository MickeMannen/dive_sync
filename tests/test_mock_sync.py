import os
import json
from datetime import datetime
import pytest
from src.core.sync_engine import SyncEngine
from src.core.models import UnifiedDive, GasMixture

def test_mock_sync_offline_mode(tmp_path):
    # Set up mock data directory structure
    mock_data_dir = str(tmp_path)
    garmin_dir = os.path.join(mock_data_dir, "garmin")
    divelogs_dir = os.path.join(mock_data_dir, "divelogs")
    os.makedirs(garmin_dir, exist_ok=True)
    os.makedirs(divelogs_dir, exist_ok=True)

    # 1. Write Garmin mock files
    # Garmin Dive 1 - will match Divelogs Dive 1
    g_dive1 = {
        "summary": {
            "activityId": "10001",
            "activityName": "Limhamn Ön",
            "startTimeLocal": "2026-06-22 10:00:00",
            "metadataDTO": {
                "diveNumber": 1
            },
            "summaryDTO": {
                "duration": 2700,
                "maxDepth": 18.2,
                "averageDepth": 10.5
            }
        },
        "details": {}
    }
    # Garmin Dive 2 - unique in Garmin
    g_dive2 = {
        "summary": {
            "activityId": "10002",
            "activityName": "Klagshamn",
            "startTimeLocal": "2026-06-23 15:30:00",
            "metadataDTO": {
                "diveNumber": 2
            },
            "summaryDTO": {
                "duration": 3000,
                "maxDepth": 12.1,
                "averageDepth": 8.0
            }
        },
        "details": {}
    }

    with open(os.path.join(garmin_dir, "1.json"), "w") as f:
        json.dump(g_dive1, f)
    with open(os.path.join(garmin_dir, "2.json"), "w") as f:
        json.dump(g_dive2, f)

    # 2. Write Divelogs mock files
    # Divelogs Dive 1 - matches Garmin Dive 1 (grace window logic)
    d_dive1 = {
        "id": "50001",
        "divenumber": 1,
        "date": "2026-06-22",
        "time": "10:02:00",  # 2 minutes offset, matches 15-minute grace window
        "duration": 2700,
        "maxdepth": 18.2,
        "meandepth": 10.5,
        "location": "Limhamn Ön",
        "notes": "Nice dive!"
    }
    # Divelogs Dive 3 - unique in Divelogs
    d_dive3 = {
        "id": "50003",
        "divenumber": 3,
        "date": "2026-06-24",
        "time": "11:00:00",
        "duration": 2400,
        "maxdepth": 25.0,
        "meandepth": 15.0,
        "location": "Västra Hamnen"
    }

    with open(os.path.join(divelogs_dir, "1.json"), "w") as f:
        json.dump(d_dive1, f)
    with open(os.path.join(divelogs_dir, "3.json"), "w") as f:
        json.dump(d_dive3, f)

    # Initialize SyncEngine in mock mode
    engine = SyncEngine(mock_data_dir=mock_data_dir)
    
    # We want bidirectional sync
    engine.settings.directionality = "bidirectional"
    engine.settings.grace_window_minutes = 15

    # Run sync
    results = engine.run_sync(dry_run=False)

    # Verify sync results
    assert results["matched_count"] == 1
    
    # Garmin Dive 2 (unique Garmin) should be uploaded to Divelogs
    assert len(results["uploaded_to_divelogs"]) == 1
    assert results["uploaded_to_divelogs"][0]["garmin_id"] == "10002"

    # Divelogs Dive 3 (unique Divelogs) should be uploaded to Garmin
    assert len(results["uploaded_to_garmin"]) == 1
    assert results["uploaded_to_garmin"][0]["divelogs_id"] == "50003"

    # Garmin Dive 1 and Divelogs Dive 1 should be matched and cross-linked
    assert len(results["updated_on_garmin"]) == 1
    assert results["updated_on_garmin"][0]["id"] == "10001"
    assert results["updated_on_garmin"][0]["linked_divelogs"] == "50001"

    assert len(results["updated_on_divelogs"]) == 1
    assert results["updated_on_divelogs"][0]["id"] == "50001"
    assert results["updated_on_divelogs"][0]["linked_garmin"] == "10001"

    # Verify files created/updated in the directories
    # A new Divelogs mock file for dive 2 should exist (Garmin 2 uploaded)
    assert os.path.exists(os.path.join(divelogs_dir, "2.json"))
    with open(os.path.join(divelogs_dir, "2.json"), "r") as f:
        uploaded_d2 = json.load(f)
        assert uploaded_d2["divenumber"] == 2
        assert uploaded_d2["divesite"] == "Klagshamn"

    # A new Garmin mock file for dive 3 should exist (Divelogs 3 uploaded)
    assert os.path.exists(os.path.join(garmin_dir, "3.json"))
    with open(os.path.join(garmin_dir, "3.json"), "r") as f:
        uploaded_g3 = json.load(f)
        assert uploaded_g3["summary"]["metadataDTO"]["diveNumber"] == "3"
        assert uploaded_g3["summary"]["activityName"] == "Västra Hamnen"

def test_download_and_save_raw_data(tmp_path, monkeypatch):
    # Initialize SyncEngine with live mode (mock_data_dir=None)
    engine = SyncEngine(mock_data_dir=None)
    
    # Set dummy credentials so the engine attempts to download
    engine.credentials.garmin.username = "test_user@garmin"
    engine.credentials.divelogs.username = "test_user_divelogs"
    
    # Mock Garmin login and API client
    monkeypatch.setattr(engine.garmin, "login", lambda: True)
    class MockGarminClient:
        def get_activities(self, start, limit, activitytype=None):
            # Return one diving activity on first call, empty list on second call
            if start == 0:
                return [
                    {
                        "activityId": "999001",
                        "activityType": {"typeKey": "diving"},
                        "startTimeLocal": "2026-06-22 10:00:00",
                        "metadataDTO": {"diveNumber": 10}
                    }
                ]
            return []
            
        def connectapi(self, url, params=None):
            if "/tanksensor" in url:
                assert params == {"connectActivityId": "999001"}
                return {
                    "sensorData": "mock_tanksensor_data"
                }
            assert url == "/activity-service/activity/999001"
            return {
                "activityId": "999001",
                "activityName": "Mock Live Garmin Dive",
                "metadataDTO": {"diveNumber": 10}
            }

        def get_activity_details(self, activity_id):
            assert activity_id == "999001"
            return {
                "activityId": "999001",
                "chartData": {
                    "sensorTelemetry": "mock_data"
                }
            }
            
    engine.garmin.client = MockGarminClient()
    
    # Mock Divelogs login and session
    monkeypatch.setattr(engine.divelogs, "login", lambda: True)
    
    class MockResponse:
        status_code = 200
        def json(self):
            return [
                {
                    "id": "777001",
                    "divenumber": 12,
                    "date": "2026-06-22",
                    "time": "10:00:00",
                    "location": "Mock Live Divelogs site"
                }
            ]
            
    monkeypatch.setattr(engine.divelogs.session, "get", lambda url, timeout=None: MockResponse())
    
    # Speed up tests by disabling cooldown sleep
    engine.garmin.cooldown_seconds = 0
    engine.divelogs.cooldown_seconds = 0
    
    # Run the downloader with overwrite=True and a leftover file
    mock_data_dir = str(tmp_path)
    garmin_leftover = os.path.join(mock_data_dir, "garmin", "999.json")
    os.makedirs(os.path.dirname(garmin_leftover), exist_ok=True)
    with open(garmin_leftover, "w") as f:
        f.write("{}")
 
    success = engine.download_and_save_raw_data(mock_data_dir=mock_data_dir, overwrite=True)
    
    assert success
    assert not os.path.exists(garmin_leftover)
    
    # Verify Garmin raw file
    garmin_file = os.path.join(mock_data_dir, "garmin", "10.json")
    assert os.path.exists(garmin_file)
    with open(garmin_file, "r") as f:
        data = json.load(f)
        assert data["summary"]["activityId"] == "999001"
        assert data["details"]["activityName"] == "Mock Live Garmin Dive"
        assert data["activityDetails"]["chartData"]["sensorTelemetry"] == "mock_data"
        assert data["tanksensor"]["sensorData"] == "mock_tanksensor_data"
        
    # Verify Divelogs raw file
    divelogs_file = os.path.join(mock_data_dir, "divelogs", "12.json")
    assert os.path.exists(divelogs_file)
    with open(divelogs_file, "r") as f:
        data = json.load(f)
        assert data["id"] == "777001"
        assert data["location"] == "Mock Live Divelogs site"

def test_update_dive_preserves_other_fields(tmp_path):
    mock_data_dir = str(tmp_path)
    garmin_dir = os.path.join(mock_data_dir, "garmin")
    os.makedirs(garmin_dir, exist_ok=True)
    
    # Write a detailed mock file with extra fields
    g_dive_initial = {
        "summary": {
            "activityId": "12345",
            "activityName": "Initial Name",
            "startTimeLocal": "2026-06-22 10:00:00",
            "metadataDTO": {"diveNumber": 1}
        },
        "details": {
            "activityId": "12345",
            "diveInfo": {
                "buddy": "Guy",
                "diveGases": [
                    {
                        "gasIndex": 0,
                        "tankStartingPressure": 200.0,
                        "tankSize": 12.0
                    }
                ]
            }
        }
    }
    
    filepath = os.path.join(garmin_dir, "1.json")
    with open(filepath, "w") as f:
        json.dump(g_dive_initial, f)
        
    # Perform update_dive using mock Garmin adapter
    from src.core.services.mock_adapters import LocalMockGarminAdapter
    adapter = LocalMockGarminAdapter(mock_data_dir=mock_data_dir)
    
    updated_dive = UnifiedDive(
        date_time=datetime(2026, 6, 22, 10, 0, 0),
        duration=3000,
        max_depth=15.0,
        dive_number=1,
        external_ids={"garmin": "12345", "divelogs": "999"}
    )
    
    success = adapter.update_dive("12345", updated_dive)
    assert success
    
    # Read modified mock file and verify buddy and tank details are preserved
    with open(filepath, "r") as f:
        updated_data = json.load(f)
        
    # Preserved fields
    assert updated_data["details"]["diveInfo"]["buddy"] == "Guy"
    assert updated_data["details"]["diveInfo"]["diveGases"][0]["tankStartingPressure"] == 200.0
    assert updated_data["details"]["diveInfo"]["diveGases"][0]["tankSize"] == 12.0
    
    # Updated fields
    assert updated_data["details"]["summaryDTO"]["duration"] == 3000
    assert updated_data["details"]["summaryDTO"]["maxDepth"] == 15.0


