import os
import base64
import logging
import time
from datetime import datetime
from typing import List, Optional, Dict, Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError
)

from src.core.adapter import BaseDiveAdapter
from src.core.models import UnifiedDive, GasMixture

logger = logging.getLogger("anti_gravity.garmin")

class GarminAdapter(BaseDiveAdapter):
    def __init__(self, username: str, password: str, token_dir: str = "tokens/garmin", cooldown_seconds: float = 1.0):
        self.username = username
        self.password = password
        self.token_dir = token_dir
        self.cooldown_seconds = cooldown_seconds
        
        os.makedirs(self.token_dir, exist_ok=True)
        self.tokenstore_path = os.path.join(self.token_dir, "garmin_tokens.json")
        
        # Initialize garminconnect Garmin client
        # Uses curl_cffi under the hood to bypass SSO rate limits and emulate browser profiles
        self.client = Garmin(self.username, self.password)
        self.logged_in = False

    def login(self) -> bool:
        logger.info("Attempting Garmin Connect login via python-garminconnect...")
        try:
            # garminconnect automatically checks for cached tokens in the tokenstore_path
            # and performs credentials login with browser emulation only if needed
            self.client.login(self.tokenstore_path)
            self.logged_in = True
            logger.info("Successfully authenticated with Garmin Connect.")
            return True
        except GarminConnectTooManyRequestsError as e:
            logger.error("Failed to authenticate with Garmin Connect: Rate limit exceeded (HTTP 429). "
                         "Garmin Connect has rate-limited login requests. Please wait 10-15 minutes "
                         "before retrying, and verify that your credentials are correct.")
            return False
        except GarminConnectAuthenticationError as e:
            logger.error("Authentication failed: Invalid credentials or MFA prompt required. Details: %s", e)
            return False
        except Exception as e:
            if "429" in str(e):
                logger.error("Failed to authenticate with Garmin Connect: Rate limit exceeded (HTTP 429). "
                             "Garmin Connect has rate-limited login requests. Please wait 10-15 minutes "
                             "before retrying, and verify that your credentials are correct.")
            else:
                logger.error("Failed to authenticate with Garmin Connect: %s", e)
            return False

    def fetch_dives(self, date_from: Optional[datetime] = None, date_to: Optional[datetime] = None) -> List[UnifiedDive]:
        if not self.logged_in and not self.login():
            raise RuntimeError("Cannot fetch dives: Not authenticated with Garmin Connect.")

        logger.info("Fetching dive activities list from Garmin Connect...")
        start = 0
        limit = 50
        all_dives: List[Dict[str, Any]] = []

        while True:
            try:
                # Retrieve simple activities
                logger.info(" Retrieving activity history from Garmin Connect (offset: %d)...", start)
                response = self.client.get_activities(start, limit, activitytype="diving")
                if not response:
                    break
                
                # Filter locally to diving activity type
                dives_batch = [
                    act for act in response 
                    if act.get("activityType", {}).get("typeKey") == "diving" or 
                       (act.get("activityTypeDTO", {}).get("typeKey") or "").endswith("diving")
                ]
                all_dives.extend(response)
                
                if len(response) < limit:
                    break
                start += limit
                time.sleep(self.cooldown_seconds)
            except Exception as e:
                logger.error("Failed to query activity list from Garmin Connect: %s", e)
                raise

        # Filter to diving type specifically
        diving_activities = [
            act for act in all_dives 
            if act.get("activityType", {}).get("typeKey") == "diving" or 
               (act.get("activityTypeDTO", {}).get("typeKey") or "").endswith("diving")
        ]

        # Filter by date ranges before fetching details to determine accurate progress count
        target_activities = []
        for activity in diving_activities:
            start_time_str = activity.get("startTimeLocal")
            if not start_time_str:
                continue
            start_time = self._parse_datetime(start_time_str)
            if not start_time:
                continue
            if date_from and start_time < date_from:
                continue
            if date_to and start_time > date_to:
                continue
            target_activities.append((activity, start_time))

        total_targets = len(target_activities)
        logger.info("Found %d dive activities matching date filters.", total_targets)

        unified_dives: List[UnifiedDive] = []
        for index, (activity, start_time) in enumerate(target_activities, 1):
            activity_id = activity.get("activityId")
            if not activity_id:
                continue

            logger.info(" [%d/%d] Fetching Garmin Dive Activity ID: %s (%s)...", 
                        index, total_targets, activity_id, start_time)
            try:
                # Fetch full detailed JSON using the wrapped client.connectapi
                time.sleep(self.cooldown_seconds)
                details = self.client.connectapi(f"/activity-service/activity/{activity_id}")
                
                mapped_dive = self._map_to_unified(activity, details)
                unified_dives.append(mapped_dive)
            except Exception as e:
                logger.error("Failed to fetch details for activity %s: %s", activity_id, e)
                raise RuntimeError(f"Failed to fetch details for Garmin activity {activity_id}: {e}") from e

        return unified_dives

    def fetch_fit_file(self, activity_id: str) -> Optional[str]:
        if not self.logged_in and not self.login():
            return None
        try:
            logger.info("Downloading .fit file for activity %s...", activity_id)
            url = f"/download-service/files/activity/{activity_id}"
            fit_bytes = self.client.download(url)
            if fit_bytes:
                return base64.b64encode(fit_bytes).decode("utf-8")
        except Exception as e:
            logger.error("Failed to download FIT file for activity %s: %s", activity_id, e)
        return None

    def add_dive(self, dive: UnifiedDive) -> Optional[str]:
        if not self.logged_in and not self.login():
            logger.error("Cannot add dive: Not authenticated with Garmin Connect.")
            return None

        logger.info("Adding dive at %s to Garmin Connect...", dive.date_time)
        try:
            payload = self._map_from_unified(dive)
            url = "/activity-service/activity"
            res_data = self.client.client.post("connectapi", url, json=payload, api=True)
            
            if res_data and isinstance(res_data, dict):
                activity_id = str(res_data.get("activityId"))
                logger.info("Successfully added dive to Garmin Connect. Assigned Activity ID: %s", activity_id)
                time.sleep(self.cooldown_seconds)
                return activity_id
            else:
                logger.error("Failed to add dive to Garmin. Status code or payload invalid.")
        except Exception as e:
            logger.error("Error adding dive to Garmin Connect: %s", e)
        return None

    def update_dive(self, external_id: str, dive: UnifiedDive) -> bool:
        if not self.logged_in and not self.login():
            logger.error("Cannot update dive: Not authenticated with Garmin Connect.")
            return False

        logger.info("Updating Garmin Connect Dive Activity ID %s...", external_id)
        try:
            payload = self._map_from_unified(dive)
            url = f"/activity-service/activity/{external_id}"
            res_data = self.client.client.put("connectapi", url, json=payload, api=True)
            
            logger.info("Successfully updated Garmin Connect Activity ID %s.", external_id)
            time.sleep(self.cooldown_seconds)
            return True
        except Exception as e:
            logger.error("Error updating Garmin Connect Activity ID %s: %s", external_id, e)
        return False

    def _parse_datetime(self, dt_str: str) -> Optional[datetime]:
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S.%f"
        ]
        for fmt in formats:
            try:
                # Strip potential trailing timezone offsets or GMT indicators
                clean_str = dt_str.split(".00")[0].split("+")[0].split("Z")[0]
                return datetime.strptime(clean_str, fmt)
            except ValueError:
                continue
        return None

    def _map_to_unified(self, summary: Dict[str, Any], details: Dict[str, Any]) -> UnifiedDive:
        info = details.get("diveInfo", {}) or summary.get("diveInfo", {}) or {}
        sum_dto = details.get("summaryDTO", {}) or summary.get("summaryDTO", {}) or {}
        metadata = details.get("metadataDTO", {}) or summary.get("metadataDTO", {}) or {}
        
        start_time_str = sum_dto.get("startTimeLocal") or summary.get("startTimeLocal")
        start_time = self._parse_datetime(start_time_str) if start_time_str else datetime.now()

        duration = int(sum_dto.get("duration") or summary.get("duration") or 0)
        max_depth = float(sum_dto.get("maxDepth") or summary.get("maxDepth") or 0.0)
        avg_depth = sum_dto.get("averageDepth") or summary.get("averageDepth")
        if avg_depth is not None:
            avg_depth = float(avg_depth)
        
        temp_min = sum_dto.get("minTemperature")
        if temp_min is not None:
            temp_min = float(temp_min)
            
        temp_max = sum_dto.get("maxTemperature")
        if temp_max is not None:
            temp_max = float(temp_max)

        temp_avg = sum_dto.get("averageTemperature")
        if temp_avg is not None:
            temp_avg = float(temp_avg)

        activity_id = str(details.get("activityId") or summary.get("activityId") or "")
        external_ids = {}
        if activity_id:
            external_ids["garmin"] = activity_id

        # Mapped gases
        gas_mixtures = []
        dive_gases = info.get("diveGases") or []
        for gas in dive_gases:
            gas_mixtures.append(
                GasMixture(
                    oxygen=float(gas.get("oxygenContent") or 21.0),
                    helium=float(gas.get("heliumContent") or 0.0),
                    start_pressure=gas.get("tankStartingPressure"),
                    end_pressure=gas.get("tankEndingPressure"),
                    tank_volume=gas.get("tankSize")
                )
            )

        dive_number_val = metadata.get("diveNumber")
        dive_number = int(dive_number_val) if dive_number_val is not None and str(dive_number_val).isdigit() else None

        location = details.get("activityName") or summary.get("activityName") or details.get("locationName") or summary.get("locationName")
        notes = details.get("description") or summary.get("description")

        return UnifiedDive(
            date_time=start_time,
            duration=duration,
            max_depth=max_depth,
            avg_depth=avg_depth,
            temp_min=temp_min,
            temp_max=temp_max,
            temp_avg=temp_avg,
            external_ids=external_ids,
            gas_mixtures=gas_mixtures,
            location=location,
            notes=notes,
            dive_number=dive_number
        )

    def _map_from_unified(self, dive: UnifiedDive) -> Dict[str, Any]:
        activity_type = "multi_gas_diving" if len(dive.gas_mixtures) > 1 else "single_gas_diving"
        
        garmin_gases = []
        for idx, gas in enumerate(dive.gas_mixtures):
            g_dict: Dict[str, Any] = {
                "gasIndex": idx,
                "oxygenContent": gas.oxygen,
                "heliumContent": gas.helium,
                "status": 1,
                "gasMode": 0
            }
            if gas.start_pressure is not None:
                g_dict["tankStartingPressure"] = gas.start_pressure
                g_dict["tankStartingPressureUnit"] = {"unitKey": "bar"}
            if gas.end_pressure is not None:
                g_dict["tankEndingPressure"] = gas.end_pressure
                g_dict["tankEndingPressureUnit"] = {"unitKey": "bar"}
            if gas.tank_volume is not None:
                g_dict["tankSize"] = gas.tank_volume
                g_dict["tankSizeUnit"] = {"unitKey": "liter"}
            
            garmin_gases.append(g_dict)

        payload = {
            "activityTypeDTO": {
                "typeKey": activity_type
            },
            "activityName": dive.location or "Sync Dive",
            "description": dive.notes or "",
            "metadataDTO": {
                "diveNumber": str(dive.dive_number) if dive.dive_number is not None else None
            },
            "summaryDTO": {
                "startTimeLocal": dive.date_time.strftime("%Y-%m-%d %H:%M:%S"),
                "duration": dive.duration,
                "bottomTime": dive.duration,
                "maxDepth": dive.max_depth,
                "averageDepth": dive.avg_depth,
                "minTemperature": dive.temp_min,
                "averageTemperature": dive.temp_avg,
                "maxTemperature": dive.temp_max
            },
            "diveInfo": {
                "entryType": "Shore",
                "diveGases": garmin_gases
            }
        }
        return payload
