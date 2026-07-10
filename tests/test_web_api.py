import os
import json
import pytest
import shutil
from fastapi.testclient import TestClient

from src.web.app import app

TEST_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
garmin_test_dir = os.path.join(TEST_DATA_DIR, "garmin")
divelogs_test_dir = os.path.join(TEST_DATA_DIR, "divelogs")

@pytest.fixture
def mock_dirs():
    # Set up directories inside tests/data for testing the API
    # Ensure they are empty and exist
    if os.path.exists(garmin_test_dir):
        shutil.rmtree(garmin_test_dir)
    if os.path.exists(divelogs_test_dir):
        shutil.rmtree(divelogs_test_dir)
        
    os.makedirs(garmin_test_dir, exist_ok=True)
    os.makedirs(divelogs_test_dir, exist_ok=True)
    
    # Mock data
    g_dive = {
        "summary": {
            "activityId": "10001",
            "activityName": "Limhamn Ön",
            "startTimeLocal": "2026-06-22 10:00:00",
            "metadataDTO": {
                "diveNumber": "1"
            },
            "summaryDTO": {
                "duration": 2700,
                "maxDepth": 18.2
            }
        },
        "details": {
            "diveInfo": {
                "buddy": "Micke",
                "weight": 12.0,
                "weightUnit": {"unitKey": "kilogram"},
                "visibility": 5.0,
                "visibilityUnit": {"unitKey": "meter"}
            }
        }
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
        "visibility": "5"
    }
    
    with open(os.path.join(garmin_test_dir, "1.json"), "w") as f:
        json.dump(g_dive, f)
    with open(os.path.join(divelogs_test_dir, "1.json"), "w") as f:
        json.dump(d_dive, f)
        
    yield
    
    # Teardown
    if os.path.exists(garmin_test_dir):
        shutil.rmtree(garmin_test_dir)
    if os.path.exists(divelogs_test_dir):
        shutil.rmtree(divelogs_test_dir)

def test_get_dives(mock_dirs):
    client = TestClient(app)
    response = client.get("/api/dives")
    assert response.status_code == 200
    data = response.json()
    
    assert "garmin" in data
    assert "divelogs" in data
    
    assert len(data["garmin"]) == 1
    assert data["garmin"][0]["location"] == "Limhamn Ön"
    assert data["garmin"][0]["dive_number"] == "1"
    assert data["garmin"][0]["buddy"] == "Micke"
    
    assert len(data["divelogs"]) == 1
    assert data["divelogs"][0]["location"] == "Limhamn Ön"
    assert data["divelogs"][0]["buddy"] == "Micke"

def test_update_dive_garmin(mock_dirs, monkeypatch):
    client = TestClient(app)
    
    called = []
    def mock_push(filename, filepath):
        called.append((filename, filepath))
    import src.web.app
    monkeypatch.setattr(src.web.app, "push_garmin_update_background", mock_push)
    
    # Update Garmin dive
    payload = {
        "service": "garmin",
        "filename": "1.json",
        "dive_number": "10",
        "location": "New Location",
        "buddy": "New Buddy",
        "weight": "15 kg",
        "visibility": "8 m"
    }
    
    response = client.post("/api/dives/update", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    
    # Read modified file and verify
    with open(os.path.join(garmin_test_dir, "1.json"), "r") as f:
        data = json.load(f)
        
    assert data["summary"]["metadataDTO"]["diveNumber"] == "10"
    assert data["summary"]["activityName"] == "New Location"
    assert data["details"]["diveInfo"]["buddy"] == "New Buddy"
    assert data["details"]["diveInfo"]["weight"] == 15.0
    assert data["details"]["diveInfo"]["visibility"] == 8.0
    assert called == [("1.json", os.path.join(garmin_test_dir, "1.json"))]

def test_update_dive_divelogs(mock_dirs, monkeypatch):
    client = TestClient(app)
    
    called = []
    def mock_push(filename, filepath):
        called.append((filename, filepath))
    import src.web.app
    monkeypatch.setattr(src.web.app, "push_divelogs_update_background", mock_push)
    
    # Update Divelogs dive
    payload = {
        "service": "divelogs",
        "filename": "1.json",
        "location": "New Site, Reef",
        "notes": "Even better!",
        "buddy": "Another Buddy",
        "weight": "10",
        "visibility": "4"
    }
    
    response = client.post("/api/dives/update", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    
    # Read modified file and verify
    with open(os.path.join(divelogs_test_dir, "1.json"), "r") as f:
        data = json.load(f)
        
    assert data["location"] == "New Site"
    assert data["divesite"] == "Reef"
    assert data["notes"] == "Even better!"
    assert data["buddy"] == "Another Buddy"
    assert data["weights"] == "10"
    assert data["visibility"] == "4"
    assert called == [("1.json", os.path.join(divelogs_test_dir, "1.json"))]

def test_download_raw_dives(mock_dirs, monkeypatch):
    client = TestClient(app)
    
    # Mock download_and_save_raw_data to avoid API calls
    from src.core.sync_engine import SyncEngine
    monkeypatch.setattr(SyncEngine, "download_and_save_raw_data", lambda self, mock_data_dir, overwrite: True)
    
    response = client.post("/api/dives/download?overwrite=true")
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert "started in background" in response.json()["message"]
    
    # Check status endpoint
    response = client.get("/api/dives/download/status")
    assert response.status_code == 200
    assert "is_running" in response.json()

def test_get_raw_dive(mock_dirs):
    client = TestClient(app)
    response = client.get("/api/dives/raw?service=garmin&filename=1.json")
    assert response.status_code == 200
    data = response.json()
    assert data["summary"]["activityId"] == "10001"

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
        "garmin_username": "test@garmin.com",
        "garmin_password": "garminpassword",
        "garmin_token_dir": "tokens/garmin",
        "divelogs_username": "test_divelogs",
        "divelogs_password": "divelogspassword"
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

def test_credentials_test_api(monkeypatch):
    client = TestClient(app)
    
    from src.core.services.garmin import GarminAdapter
    from src.core.services.divelogs import DivelogsAdapter
    
    monkeypatch.setattr(GarminAdapter, "login", lambda self: True)
    monkeypatch.setattr(DivelogsAdapter, "login", lambda self: True)
    
    payload = {
        "garmin_username": "user",
        "garmin_password": "pass",
        "divelogs_username": "user",
        "divelogs_password": "pass"
    }
    res = client.post("/api/credentials/test", json=payload)
    assert res.status_code == 200
    assert res.json()["garmin"] is True
    assert res.json()["divelogs"] is True

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
        "directionality": "bidirectional",
        "sync_filters": {
            "date_from": "2026-01-01",
            "date_to": "2026-12-31",
            "only_new": True,
            "sync_gases": True,
            "sync_fit": False
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
                "sync_fit": True,
                "enabled": True
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
    assert cron_jobs[0]["sync_fit"] is True
    assert cron_jobs[0]["enabled"] is True

    # 4. Trigger sync with overrides
    trigger_payload = {
        "dry_run": True,
        "directionality": "to_divelogs",
        "only_new": False
    }
    res = client.post("/api/sync/trigger", json=trigger_payload)
    assert res.status_code == 200
    assert res.json()["status"] == "success"


def test_delete_dive_garmin(mock_dirs, monkeypatch):
    client = TestClient(app)
    
    called = []
    def mock_delete(filepath, activity_id):
        called.append((filepath, activity_id))
        
    import src.web.app
    monkeypatch.setattr(src.web.app, "delete_garmin_dive_background", mock_delete)
    
    # Verify file exists initially
    response = client.get("/api/dives")
    assert len(response.json()["garmin"]) == 1
    
    # Delete the Garmin dive
    res = client.delete("/api/dives?service=garmin&filename=1.json")
    assert res.status_code == 200
    assert res.json()["status"] == "success"
    
    # Verify it is deleted from list
    response = client.get("/api/dives")
    assert len(response.json()["garmin"]) == 0
    assert len(called) == 1


def test_delete_dive_divelogs(mock_dirs, monkeypatch):
    client = TestClient(app)
    
    called = []
    def mock_delete(filepath, dive_id):
        called.append((filepath, dive_id))
        
    import src.web.app
    monkeypatch.setattr(src.web.app, "delete_divelogs_dive_background", mock_delete)
    
    # Verify file exists initially
    response = client.get("/api/dives")
    assert len(response.json()["divelogs"]) == 1
    
    # Delete the Divelogs dive
    res = client.delete("/api/dives?service=divelogs&filename=1.json")
    assert res.status_code == 200
    assert res.json()["status"] == "success"
    
    # Verify it is deleted from list
    response = client.get("/api/dives")
    assert len(response.json()["divelogs"]) == 0
    assert len(called) == 1


def test_delete_dive_not_found(mock_dirs):
    client = TestClient(app)
    res = client.delete("/api/dives?service=garmin&filename=nonexistent.json")
    assert res.status_code == 404

