import os
import json
import logging
from datetime import datetime
from typing import List, Optional

from src.core.adapter import BaseDiveAdapter
from src.core.models import UnifiedDive
from src.core.services.garmin import GarminAdapter
from src.core.services.divelogs import DivelogsAdapter

logger = logging.getLogger("anti_gravity.mock_adapters")

class LocalMockGarminAdapter(BaseDiveAdapter):
    def __init__(self, mock_data_dir: str = "./tests"):
        self.mock_dir = os.path.join(mock_data_dir, "garmin")
        # Dummy adapter to reuse parsing & mapping logic
        self.helper = GarminAdapter("dummy", "dummy", token_dir="/tmp")

    def login(self) -> bool:
        logger.info("[MOCK] Garmin Connect login bypassed (authenticated locally).")
        return True

    def fetch_dives(self, date_from: Optional[datetime] = None, date_to: Optional[datetime] = None) -> List[UnifiedDive]:
        logger.info("[MOCK] Loading Garmin raw mock data from: %s", self.mock_dir)
        if not os.path.exists(self.mock_dir):
            logger.warning("[MOCK] Garmin mock directory %s does not exist.", self.mock_dir)
            return []

        dives: List[UnifiedDive] = []
        for filename in os.listdir(self.mock_dir):
            if filename.endswith(".json"):
                filepath = os.path.join(self.mock_dir, filename)
                try:
                    with open(filepath, "r") as f:
                        data = json.load(f)
                    
                    summary = data.get("summary", {})
                    details = data.get("details", {})
                    activity_details = data.get("activityDetails")
                    mapped = self.helper._map_to_unified(summary, details, activity_details)

                    # Apply date filters
                    if date_from and mapped.date_time < date_from:
                        continue
                    if date_to and mapped.date_time > date_to:
                        continue

                    dives.append(mapped)
                except Exception as e:
                    logger.error("[MOCK] Error reading Garmin mock file %s: %s", filename, e)
                    raise
        return dives

    def add_dive(self, dive: UnifiedDive) -> Optional[str]:
        # Generate dummy activity ID
        activity_id = str(dive.dive_number or len(os.listdir(self.mock_dir)) + 100000000)
        logger.info("[MOCK] Simulating add Garmin dive. Assigned Mock Activity ID: %s", activity_id)

        payload = self.helper._map_from_unified(dive)
        payload["activityId"] = activity_id

        # Wrap in expected mock format
        raw_mock = {"summary": payload, "details": payload}
        
        os.makedirs(self.mock_dir, exist_ok=True)
        filename = f"{dive.dive_number or activity_id}.json"
        with open(os.path.join(self.mock_dir, filename), "w") as f:
            json.dump(raw_mock, f, indent=2)

        return activity_id

    def update_dive(self, external_id: str, dive: UnifiedDive) -> bool:
        logger.info("[MOCK] Simulating update Garmin dive Activity ID: %s", external_id)
        
        filename = f"{dive.dive_number or external_id}.json"
        filepath = os.path.join(self.mock_dir, filename)
        
        existing_data = {}
        if os.path.exists(filepath):
            try:
                with open(filepath, "r") as f:
                    existing_data = json.load(f)
            except Exception as e:
                logger.warning("[MOCK] Failed to load existing Garmin mock file %s: %s", filepath, e)
                
        payload = self.helper._map_from_unified(dive)
        payload["activityId"] = int(external_id) if str(external_id).isdigit() else external_id
        
        if "summary" not in existing_data or not existing_data["summary"]:
            existing_data["summary"] = {}
        if "details" not in existing_data or not existing_data["details"]:
            existing_data["details"] = {}
            
        existing_data["summary"]["activityId"] = payload["activityId"]
        existing_data["details"]["activityId"] = payload["activityId"]

        # Merge simple top-level keys
        for key in ["activityTypeDTO", "activityName", "description"]:
            if key in payload:
                existing_data["summary"][key] = payload[key]
                existing_data["details"][key] = payload[key]
                
        # Merge sub-dictionaries
        for key in ["summaryDTO", "metadataDTO", "diveInfo"]:
            if key in payload:
                # Save existing gases if we're merging diveInfo
                existing_summary_gases = None
                existing_details_gases = None
                if key == "diveInfo":
                    if "summary" in existing_data and "diveInfo" in existing_data["summary"]:
                        existing_summary_gases = existing_data["summary"]["diveInfo"].get("diveGases")
                    if "details" in existing_data and "diveInfo" in existing_data["details"]:
                        existing_details_gases = existing_data["details"]["diveInfo"].get("diveGases")

                if key not in existing_data["summary"] or not existing_data["summary"][key]:
                    existing_data["summary"][key] = {}
                existing_data["summary"][key].update(payload[key])
                
                if key not in existing_data["details"] or not existing_data["details"][key]:
                    existing_data["details"][key] = {}
                existing_data["details"][key].update(payload[key])

                # Restore existing gases if payload gases are empty
                if key == "diveInfo":
                    new_gases = payload["diveInfo"].get("diveGases", [])
                    if not new_gases:
                        if existing_summary_gases:
                            existing_data["summary"]["diveInfo"]["diveGases"] = existing_summary_gases
                        if existing_details_gases:
                            existing_data["details"]["diveInfo"]["diveGases"] = existing_details_gases

        os.makedirs(self.mock_dir, exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(existing_data, f, indent=2)
        return True

class LocalMockDivelogsAdapter(BaseDiveAdapter):
    def __init__(self, mock_data_dir: str = "./tests"):
        self.mock_dir = os.path.join(mock_data_dir, "divelogs")
        # Dummy adapter to reuse parsing & mapping logic
        self.helper = DivelogsAdapter("dummy", "dummy")

    def login(self) -> bool:
        logger.info("[MOCK] Divelogs.org login bypassed (authenticated locally).")
        return True

    def fetch_dives(self, date_from: Optional[datetime] = None, date_to: Optional[datetime] = None) -> List[UnifiedDive]:
        logger.info("[MOCK] Loading Divelogs raw mock data from: %s", self.mock_dir)
        if not os.path.exists(self.mock_dir):
            logger.warning("[MOCK] Divelogs mock directory %s does not exist.", self.mock_dir)
            return []

        dives: List[UnifiedDive] = []
        for filename in os.listdir(self.mock_dir):
            if filename.endswith(".json"):
                filepath = os.path.join(self.mock_dir, filename)
                try:
                    with open(filepath, "r") as f:
                        data = json.load(f)
                    
                    mapped = self.helper._map_to_unified(data)

                    # Apply date filters
                    if date_from and mapped.date_time < date_from:
                        continue
                    if date_to and mapped.date_time > date_to:
                        continue

                    dives.append(mapped)
                except Exception as e:
                    logger.error("[MOCK] Error reading Divelogs mock file %s: %s", filename, e)
                    raise
        return dives

    def add_dive(self, dive: UnifiedDive) -> Optional[str]:
        # Generate dummy ID
        dive_id = str(dive.dive_number or len(os.listdir(self.mock_dir)) + 5000)
        logger.info("[MOCK] Simulating add Divelogs dive. Assigned Mock ID: %s", dive_id)

        payload = self.helper._map_from_unified(dive)
        payload["id"] = int(dive_id)

        os.makedirs(self.mock_dir, exist_ok=True)
        filename = f"{dive.dive_number or dive_id}.json"
        with open(os.path.join(self.mock_dir, filename), "w") as f:
            json.dump(payload, f, indent=2)

        return dive_id

    def update_dive(self, external_id: str, dive: UnifiedDive) -> bool:
        logger.info("[MOCK] Simulating update Divelogs dive ID: %s", external_id)
        
        filename = f"{dive.dive_number or external_id}.json"
        filepath = os.path.join(self.mock_dir, filename)
        
        existing_data = {}
        if os.path.exists(filepath):
            try:
                with open(filepath, "r") as f:
                    existing_data = json.load(f)
            except Exception as e:
                logger.warning("[MOCK] Failed to load existing Divelogs mock file %s: %s", filepath, e)
                
        payload = self.helper._map_from_unified(dive)
        payload["id"] = int(external_id) if str(external_id).isdigit() else external_id
        
        existing_data.update(payload)
        
        if "tanks" in payload:
            existing_data["tanks"] = payload["tanks"]
            
        os.makedirs(self.mock_dir, exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(existing_data, f, indent=2)
        return True
