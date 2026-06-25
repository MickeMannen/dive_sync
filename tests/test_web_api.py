import os
import json
import pytest
import shutil
from fastapi.testclient import TestClient

from src.web.app import app

@pytest.fixture
def mock_dirs():
    # Set up directories inside tests for testing the API
    garmin_test_dir = "./tests/garmin"
    divelogs_test_dir = "./tests/divelogs"
    
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
    with open("./tests/garmin/1.json", "r") as f:
        data = json.load(f)
        
    assert data["summary"]["metadataDTO"]["diveNumber"] == "10"
    assert data["summary"]["activityName"] == "New Location"
    assert data["details"]["diveInfo"]["buddy"] == "New Buddy"
    assert data["details"]["diveInfo"]["weight"] == 15.0
    assert data["details"]["diveInfo"]["visibility"] == 8.0
    assert called == [("1.json", "./tests/garmin/1.json")]

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
    with open("./tests/divelogs/1.json", "r") as f:
        data = json.load(f)
        
    assert data["location"] == "New Site"
    assert data["divesite"] == "Reef"
    assert data["notes"] == "Even better!"
    assert data["buddy"] == "Another Buddy"
    assert data["weights"] == "10"
    assert data["visibility"] == "4"
    assert called == [("1.json", "./tests/divelogs/1.json")]

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
