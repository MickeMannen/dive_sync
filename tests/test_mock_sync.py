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

    # Write localized settings
    settings_data = {
        "directionality": "bidirectional",
        "sync_filters": {
            "date_from": None,
            "date_to": None,
            "only_new": False,
            "sync_gases": True,
            "sync_fit": False
        },
        "grace_window_minutes": 15,
        "api_cooldown_seconds": 0.0,
        "schedule": []
    }
    settings_path = os.path.join(mock_data_dir, "settings.json")
    with open(settings_path, "w") as f:
        json.dump(settings_data, f)

    # Initialize SyncEngine in mock mode
    engine = SyncEngine(settings_path=settings_path, mock_data_dir=mock_data_dir)

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
    # Write localized settings
    settings_data = {
        "directionality": "bidirectional",
        "sync_filters": {
            "date_from": None,
            "date_to": None,
            "only_new": False,
            "sync_gases": True,
            "sync_fit": False
        },
        "grace_window_minutes": 15,
        "api_cooldown_seconds": 0.0,
        "schedule": []
    }
    settings_path = os.path.join(tmp_path, "settings.json")
    with open(settings_path, "w") as f:
        json.dump(settings_data, f)

    # Initialize SyncEngine with live mode (mock_data_dir=None)
    engine = SyncEngine(settings_path=settings_path, mock_data_dir=None)
    
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


class TestSync:
    @staticmethod
    def _setup_mock_directories(tmp_path, dive_numbers, copy_garmin=True, copy_divelogs=True):
        garmin_dest = os.path.join(tmp_path, "garmin")
        divelogs_dest = os.path.join(tmp_path, "divelogs")
        os.makedirs(garmin_dest, exist_ok=True)
        os.makedirs(divelogs_dest, exist_ok=True)

        real_garmin_dir = "./tests/garmin"
        real_divelogs_dir = "./tests/divelogs"

        import shutil
        for num in dive_numbers:
            filename = f"{num}.json"
            g_src = os.path.join(real_garmin_dir, filename)
            d_src = os.path.join(real_divelogs_dir, filename)

            if copy_garmin and os.path.exists(g_src):
                shutil.copy(g_src, os.path.join(garmin_dest, filename))
            if copy_divelogs and os.path.exists(d_src):
                shutil.copy(d_src, os.path.join(divelogs_dest, filename))

        return str(tmp_path)

    @staticmethod
    def _write_settings(tmp_path, directionality="bidirectional", only_new=False):
        settings_data = {
            "directionality": directionality,
            "sync_filters": {
                "date_from": None,
                "date_to": None,
                "only_new": only_new,
                "sync_gases": True,
                "sync_fit": False
            },
            "grace_window_minutes": 15,
            "api_cooldown_seconds": 0.0,
            "schedule": []
        }
        path = os.path.join(tmp_path, "settings.json")
        with open(path, "w") as f:
            json.dump(settings_data, f, indent=2)
        return path

    def test_bidirectional_sync_with_modifications(self, tmp_path):
        if not os.path.exists("./tests/garmin/502.json") or not os.path.exists("./tests/divelogs/488.json"):
            pytest.skip("Real downloaded mock files 488.json and/or 502.json are not present on disk.")

        # Setup: Dive 502 only in Garmin (modified), Dive 488 only in Divelogs (modified)
        mock_dir = self._setup_mock_directories(tmp_path, [502], copy_garmin=True, copy_divelogs=False)
        self._setup_mock_directories(tmp_path, [488], copy_garmin=False, copy_divelogs=True)

        # Let's modify the Garmin 502 data to check if sync uses the modified values
        g502_path = os.path.join(mock_dir, "garmin", "502.json")
        with open(g502_path, "r") as f:
            g502_data = json.load(f)
        
        # Modify activityName and description
        g502_data["summary"]["activityName"] = "Modified Garmin Location Mabul"
        g502_data["summary"]["description"] = "Modified Garmin Notes for 502"
        g502_data["details"]["activityName"] = "Modified Garmin Location Mabul"
        g502_data["details"]["description"] = "Modified Garmin Notes for 502"
        
        with open(g502_path, "w") as f:
            json.dump(g502_data, f, indent=2)

        # Let's modify the Divelogs 488 data to check if sync uses the modified values
        d488_path = os.path.join(mock_dir, "divelogs", "488.json")
        with open(d488_path, "r") as f:
            d488_data = json.load(f)
        
        d488_data["location"] = "Modified Phuket Island"
        d488_data["divesite"] = "King Cruiser Wreck"
        d488_data["notes"] = "Modified Divelogs notes for 488"
        
        with open(d488_path, "w") as f:
            json.dump(d488_data, f, indent=2)

        # Run Sync bidirectional
        settings_path = self._write_settings(tmp_path, directionality="bidirectional", only_new=False)
        engine = SyncEngine(settings_path=settings_path, mock_data_dir=mock_dir)

        results = engine.run_sync(dry_run=False)

        # Assert uploads
        assert len(results["uploaded_to_divelogs"]) == 1
        assert len(results["uploaded_to_garmin"]) == 1

        # Check created Divelogs 502 JSON contains the modified location & notes
        d502_path = os.path.join(mock_dir, "divelogs", "502.json")
        assert os.path.exists(d502_path)
        with open(d502_path, "r") as f:
            d502_created = json.load(f)
        assert d502_created["divesite"] == "Modified Garmin Location Mabul"
        assert d502_created["notes"] == "Modified Garmin Notes for 502"

        # Check created Garmin 488 JSON contains the modified location & notes
        g488_path = os.path.join(mock_dir, "garmin", "488.json")
        assert os.path.exists(g488_path)
        with open(g488_path, "r") as f:
            g488_created = json.load(f)
        assert g488_created["summary"]["activityName"] == "Modified Phuket Island, King Cruiser Wreck"
        assert g488_created["summary"]["description"] == "Modified Divelogs notes for 488"

    def test_to_garmin_directionality(self, tmp_path):
        if not os.path.exists("./tests/garmin/502.json") or not os.path.exists("./tests/divelogs/488.json"):
            pytest.skip("Real downloaded mock files 488.json and/or 502.json are not present on disk.")

        # Setup: Dive 502 in Divelogs (modified), Garmin directory empty for 502
        mock_dir = self._setup_mock_directories(tmp_path, [502], copy_garmin=False, copy_divelogs=True)
        # Also copy Garmin 488, Divelogs empty for 488
        self._setup_mock_directories(tmp_path, [488], copy_garmin=True, copy_divelogs=False)

        # Modify Divelogs 502 notes
        d502_path = os.path.join(mock_dir, "divelogs", "502.json")
        with open(d502_path, "r") as f:
            d502_data = json.load(f)
        d502_data["notes"] = "Test to_garmin notes"
        with open(d502_path, "w") as f:
            json.dump(d502_data, f, indent=2)

        settings_path = self._write_settings(tmp_path, directionality="to_garmin", only_new=False)
        engine = SyncEngine(settings_path=settings_path, mock_data_dir=mock_dir)

        results = engine.run_sync(dry_run=False)

        # Divelogs 502 should be uploaded to Garmin
        assert len(results["uploaded_to_garmin"]) == 1
        g502_path = os.path.join(mock_dir, "garmin", "502.json")
        assert os.path.exists(g502_path)
        with open(g502_path, "r") as f:
            g502_created = json.load(f)
        assert g502_created["summary"]["description"] == "Test to_garmin notes"

        # Unique Garmin 488 should NOT be uploaded to Divelogs because direction is to_garmin
        assert len(results["uploaded_to_divelogs"]) == 0
        assert not os.path.exists(os.path.join(mock_dir, "divelogs", "488.json"))

    def test_to_divelogs_directionality(self, tmp_path):
        if not os.path.exists("./tests/garmin/502.json") or not os.path.exists("./tests/divelogs/488.json"):
            pytest.skip("Real downloaded mock files 488.json and/or 502.json are not present on disk.")

        # Setup: Dive 502 in Garmin (modified), Divelogs directory empty for 502
        mock_dir = self._setup_mock_directories(tmp_path, [502], copy_garmin=True, copy_divelogs=False)
        # Also copy Divelogs 488, Garmin empty for 488
        self._setup_mock_directories(tmp_path, [488], copy_garmin=False, copy_divelogs=True)

        # Modify Garmin 502 description
        g502_path = os.path.join(mock_dir, "garmin", "502.json")
        with open(g502_path, "r") as f:
            g502_data = json.load(f)
        g502_data["summary"]["description"] = "Test to_divelogs description"
        g502_data["details"]["description"] = "Test to_divelogs description"
        with open(g502_path, "w") as f:
            json.dump(g502_data, f, indent=2)

        settings_path = self._write_settings(tmp_path, directionality="to_divelogs", only_new=False)
        engine = SyncEngine(settings_path=settings_path, mock_data_dir=mock_dir)

        results = engine.run_sync(dry_run=False)

        # Garmin 502 should be uploaded to Divelogs
        assert len(results["uploaded_to_divelogs"]) == 1
        d502_path = os.path.join(mock_dir, "divelogs", "502.json")
        assert os.path.exists(d502_path)
        with open(d502_path, "r") as f:
            d502_created = json.load(f)
        assert d502_created["notes"] == "Test to_divelogs description"

        # Unique Divelogs 488 should NOT be uploaded to Garmin because direction is to_divelogs
        assert len(results["uploaded_to_garmin"]) == 0
        assert not os.path.exists(os.path.join(mock_dir, "garmin", "488.json"))

    def test_declarative_jsonpath_mappings(self):
        from src.core.mapping_helper import resolve_jsonpath, set_jsonpath, MappingEngine
        from src.core.models import UnifiedDive, GasMixture
        
        # Test JSONPath resolution
        sample_data = {
            "summarizedDiveInfo": {
                "maxDepth": 18.5,
                "tankSummaryList": [
                    {"tankVolume": 12.0, "startPressure": 200.0},
                    {"tankVolume": 10.0, "startPressure": 180.0}
                ]
            }
        }
        
        assert resolve_jsonpath(sample_data, "$.summarizedDiveInfo.maxDepth") == 18.5
        assert resolve_jsonpath(sample_data, "$.summarizedDiveInfo.tankSummaryList[*].startPressure") == [200.0, 180.0]
        
        # Test JSONPath setting
        target = {}
        set_jsonpath(target, "$.summarizedDiveInfo.maxDepth", 15.0)
        assert target["summarizedDiveInfo"]["maxDepth"] == 15.0
        
        # Test Garmin payload mapping to UnifiedDive
        dive = UnifiedDive(date_time=datetime.now(), duration=3600, max_depth=0.0)
        MappingEngine.apply_garmin_to_divelogs_mapping(sample_data, dive)
        assert dive.max_depth == 18.5
        assert len(dive.gas_mixtures) == 2
        assert dive.gas_mixtures[0].start_pressure == 200.0
        assert dive.gas_mixtures[1].tank_volume == 10.0

    def test_run_sync_direction_override(self, tmp_path):
        if not os.path.exists("./tests/garmin/502.json") or not os.path.exists("./tests/divelogs/488.json"):
            pytest.skip("Real downloaded mock files 488.json and/or 502.json are not present on disk.")

        # Setup: Garmin has dive 502, Divelogs is empty
        mock_dir = self._setup_mock_directories(tmp_path, [502], copy_garmin=True, copy_divelogs=False)

        # Write settings with directionality = "to_garmin"
        settings_path = self._write_settings(tmp_path, directionality="to_garmin", only_new=False)
        engine = SyncEngine(settings_path=settings_path, mock_data_dir=mock_dir)

        # Calling run_sync without override should use "to_garmin" which does NOT sync Garmin -> Divelogs
        res1 = engine.run_sync(dry_run=False)
        assert len(res1["uploaded_to_divelogs"]) == 0
        assert not os.path.exists(os.path.join(mock_dir, "divelogs", "502.json"))

        # Calling run_sync WITH override "to_divelogs" should override config and perform sync Garmin -> Divelogs!
        res2 = engine.run_sync(dry_run=False, direction_override="to_divelogs")
        assert len(res2["uploaded_to_divelogs"]) == 1
        assert os.path.exists(os.path.join(mock_dir, "divelogs", "502.json"))



