import os
import json
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
from src.core.models import UnifiedDive, GasMixture, UnifiedSample

logger = logging.getLogger("dive_sync.garmin")

class GarminAdapter(BaseDiveAdapter):
    def __init__(self, username: str, password: str, token_dir: str = "tokens/garmin", cooldown_seconds: float = 1.0):
        self.username = username
        self.password = password
        self.token_dir = token_dir
        self.cooldown_seconds = cooldown_seconds
        
        os.makedirs(self.token_dir, exist_ok=True)
        import re
        safe_username = re.sub(r'[^a-zA-Z0-9_.-]', '_', self.username)
        self.tokenstore_path = os.path.join(self.token_dir, f"garmin_tokens_{safe_username}.json")
        
        # Initialize garminconnect Garmin client
        # Uses curl_cffi under the hood to bypass SSO rate limits and emulate browser profiles
        self.client = Garmin(self.username, self.password)
        self.logged_in = False

    def login(self) -> bool:
        logger.info("Attempting Garmin Connect login for user '%s' via python-garminconnect...", self.username)
        try:
            # garminconnect automatically checks for cached tokens in the tokenstore_path
            # and performs credentials login with browser emulation only if needed
            self.client.login(self.tokenstore_path)
            self.logged_in = True
            logger.info("Successfully authenticated with Garmin Connect for user '%s'.", self.username)
            self._fetch_user_preferences()
            return True
        except GarminConnectTooManyRequestsError as e:
            logger.error("Failed to authenticate with Garmin Connect for user '%s': Rate limit exceeded (HTTP 429). "
                         "Garmin Connect has rate-limited login requests. Please wait 10-15 minutes "
                         "before retrying, and verify that your credentials are correct.", self.username)
            return False
        except GarminConnectAuthenticationError as e:
            logger.error("Authentication failed for user '%s': Invalid credentials or MFA prompt required. Details: %s", self.username, e)
            return False
        except Exception as e:
            if "429" in str(e):
                logger.error("Failed to authenticate with Garmin Connect for user '%s': Rate limit exceeded (HTTP 429). "
                             "Garmin Connect has rate-limited login requests. Please wait 10-15 minutes "
                             "before retrying, and verify that your credentials are correct.", self.username)
            else:
                logger.error("Failed to authenticate with Garmin Connect for user '%s': %s", self.username, e)
            return False

    def _fetch_user_preferences(self) -> None:
        try:
            logger.info("Fetching Garmin user profile settings for user '%s'...", self.username)
            
            start = 0
            limit = 50
            all_dives = []

            while True:
                response = self.client.get_activities(start, limit, activitytype="diving")
                if not response:
                    break
                
                dives_batch = [
                    act for act in response 
                    if act.get("activityType", {}).get("typeKey") == "diving" or 
                       (act.get("activityTypeDTO", {}).get("typeKey") or "").endswith("diving") or
                       "diving" in (act.get("activityType", {}).get("typeKey") or "")
                ]
                all_dives.extend(dives_batch)
                
                if len(response) < limit:
                    break
                start += limit
                time.sleep(self.cooldown_seconds)
            
            dives_count = len(all_dives)
            logger.info("Fetching Garmin user profile settings for user '%s'... - %d dives found", self.username, dives_count)
        except Exception as e:
            logger.warning("Error fetching Garmin user preferences: %s", e)

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
                       (act.get("activityTypeDTO", {}).get("typeKey") or "").endswith("diving") or
                       "diving" in (act.get("activityType", {}).get("typeKey") or "")
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
               (act.get("activityTypeDTO", {}).get("typeKey") or "").endswith("diving") or
               "diving" in (act.get("activityType", {}).get("typeKey") or "")
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
                
                # Fetch detailed activity metrics containing chart/profile data
                activity_details = None
                try:
                    time.sleep(self.cooldown_seconds)
                    activity_details = self.client.get_activity_details(activity_id)
                except Exception as detail_err:
                    logger.warning("Failed to fetch activity details (telemetry) for %s: %s", activity_id, detail_err)

                mapped_dive = self._map_to_unified(activity, details, activity_details)
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
            # 1. Fetch current details from Garmin to compare
            time.sleep(self.cooldown_seconds)
            current_raw = self.client.connectapi(f"/activity-service/activity/{external_id}")
            
            # Map existing raw activity to UnifiedDive using our parser
            current_dive = self._map_to_unified({}, current_raw)
            
            # 2. Build partial payload based on differences
            payload: Dict[str, Any] = {
                "activityId": int(external_id) if str(external_id).isdigit() else external_id
            }
            
            # Compare and add changed fields
            if dive.location != current_dive.location:
                payload["activityName"] = dive.location or "Sync Dive"
                
            if dive.notes != current_dive.notes:
                payload["description"] = dive.notes or ""
                
            if dive.dive_number != current_dive.dive_number:
                payload["metadataDTO"] = {
                    "diveNumber": str(dive.dive_number) if dive.dive_number is not None else None
                }
                
            # If weight, visibility, or buddy changes, we need to populate diveInfo
            dive_info_changed = False
            dive_info_payload = {}
            
            if dive.buddy != current_dive.buddy:
                dive_info_payload["buddy"] = dive.buddy
                dive_info_changed = True
                
            if dive.weight != current_dive.weight or dive.weight_unit != current_dive.weight_unit:
                dive_info_payload["weight"] = dive.weight
                if dive.weight is not None:
                    unit_key = (dive.weight_unit or "kilogram").lower()
                    if unit_key == "kg":
                        unit_key = "kilogram"
                    elif unit_key in ["lb", "lbs"]:
                        unit_key = "pound"
                    factor = 1000.0 if unit_key == "kilogram" else 453.59237
                    unit_id = 8 if unit_key == "kilogram" else 9
                    dive_info_payload["weightUnit"] = {
                        "unitId": unit_id,
                        "unitKey": unit_key,
                        "factor": factor
                    }
                else:
                    dive_info_payload["weightUnit"] = None
                dive_info_changed = True
                
            if dive.visibility != current_dive.visibility or dive.visibility_unit != current_dive.visibility_unit:
                dive_info_payload["visibility"] = dive.visibility
                if dive.visibility is not None:
                    unit_key = (dive.visibility_unit or "meter").lower()
                    if unit_key == "m":
                        unit_key = "meter"
                    elif unit_key in ["ft", "feet"]:
                        unit_key = "foot"
                    factor = 100.0 if unit_key == "meter" else 30.48
                    unit_id = 1 if unit_key == "meter" else 2
                    dive_info_payload["visibilityUnit"] = {
                        "unitId": unit_id,
                        "unitKey": unit_key,
                        "factor": factor
                    }
                else:
                    dive_info_payload["visibilityUnit"] = None
                dive_info_changed = True
                
            if dive_info_changed:
                existing_dive_info = current_raw.get("diveInfo") or {}
                if isinstance(existing_dive_info, dict):
                    merged_dive_info = dict(existing_dive_info)
                    merged_dive_info.update(dive_info_payload)
                    payload["diveInfo"] = merged_dive_info
                else:
                    payload["diveInfo"] = dive_info_payload

            # Check summaryDTO coordinates changes
            if dive.lat != current_dive.lat or dive.lng != current_dive.lng:
                summary_dto = {}
                existing_summary = current_raw.get("summaryDTO") or {}
                if isinstance(existing_summary, dict):
                    summary_dto = dict(existing_summary)
                
                if dive.lat is not None:
                    summary_dto["startLatitude"] = dive.lat
                else:
                    summary_dto.pop("startLatitude", None)
                    
                if dive.lng is not None:
                    summary_dto["startLongitude"] = dive.lng
                else:
                    summary_dto.pop("startLongitude", None)
                    
                payload["summaryDTO"] = summary_dto
                
            # If no differences are found, skip update
            if len(payload) <= 1:
                logger.info("No changed fields detected for Garmin Connect Activity ID %s. Skipping update.", external_id)
                return True
                
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

    def _map_to_unified(self, summary: Dict[str, Any], details: Dict[str, Any], activity_details: Optional[Dict[str, Any]] = None) -> UnifiedDive:
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

        weight = info.get("weight")
        if weight is not None:
            weight = float(weight)
        weight_unit = info.get("weightUnit", {}).get("unitKey") if isinstance(info.get("weightUnit"), dict) else None

        visibility = info.get("visibility")
        if visibility is not None:
            visibility = float(visibility)
        visibility_unit = info.get("visibilityUnit", {}).get("unitKey") if isinstance(info.get("visibilityUnit"), dict) else None

        buddy = info.get("buddy")

        # Parse GPS coordinates (startLatitude/startLongitude, falling back to endLatitude/endLongitude)
        lat = sum_dto.get("startLatitude")
        if lat is None:
            lat = sum_dto.get("endLatitude")
        if lat is None:
            lat = summary.get("startLatitude")
        if lat is None:
            lat = summary.get("endLatitude")

        lng = sum_dto.get("startLongitude")
        if lng is None:
            lng = sum_dto.get("endLongitude")
        if lng is None:
            lng = summary.get("startLongitude")
        if lng is None:
            lng = summary.get("endLongitude")

        lat = float(lat) if lat is not None else None
        lng = float(lng) if lng is not None else None

        # Parse profile chart data samples
        samples = []
        if activity_details and isinstance(activity_details, dict):
            descriptors = activity_details.get("metricDescriptors") or []
            metrics_data = activity_details.get("activityDetailMetrics") or []
            
            if isinstance(descriptors, list) and isinstance(metrics_data, list):
                duration_idx = None
                depth_idx = None
                temp_idx = None
                
                for desc in descriptors:
                    if not isinstance(desc, dict):
                        continue
                    key = desc.get("key")
                    idx = desc.get("metricsIndex")
                    if key == "sumDuration":
                        duration_idx = idx
                    elif key == "directDepth" or (isinstance(key, str) and "depth" in key.lower()):
                        if depth_idx is None or key == "directDepth":
                            depth_idx = idx
                    elif isinstance(key, str) and ("temperature" in key.lower() or "temp" in key.lower()):
                        if temp_idx is None or key == "directAirTemperature":
                            temp_idx = idx
                
                for item in metrics_data:
                    if not isinstance(item, dict):
                        continue
                    m_list = item.get("metrics")
                    if not m_list or not isinstance(m_list, list):
                        continue
                    
                    if depth_idx is not None and depth_idx < len(m_list):
                        depth_val = m_list[depth_idx]
                        if depth_val is not None:
                            depth = float(depth_val)
                            
                            time_sec = None
                            if duration_idx is not None and duration_idx < len(m_list):
                                dur_val = m_list[duration_idx]
                                if dur_val is not None:
                                    time_sec = int(round(float(dur_val)))
                                    
                            temp_c = None
                            if temp_idx is not None and temp_idx < len(m_list):
                                temp_val = m_list[temp_idx]
                                if temp_val is not None:
                                    temp_c = float(temp_val)
                                    
                            samples.append(UnifiedSample(
                                depth=depth,
                                temp=temp_c,
                                time=time_sec
                            ))

        dive = UnifiedDive(
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
            dive_number=dive_number,
            weight=weight,
            weight_unit=weight_unit,
            visibility=visibility,
            visibility_unit=visibility_unit,
            buddy=buddy,
            lat=lat,
            lng=lng,
            samples=samples
        )

        try:
            merged_garmin = {}
            if isinstance(summary, dict):
                merged_garmin.update(summary)
            if isinstance(details, dict):
                merged_garmin.update(details)
            if isinstance(activity_details, dict):
                merged_garmin.update(activity_details)
            from src.core.mapping_helper import MappingEngine
            MappingEngine.apply_garmin_to_divelogs_mapping(merged_garmin, dive)
        except Exception as e:
            logger.warning("Failed to apply mapping file overrides for Garmin map_to_unified: %s", e)

        return dive

    def _map_from_unified(self, dive: UnifiedDive) -> Dict[str, Any]:
        # Determine activity type: single gas by default; multi-gas if 2 or more different gas mixes are used.
        unique_mixes = set()
        for gas in dive.gas_mixtures:
            unique_mixes.add((gas.oxygen, gas.helium))
        activity_type = "multi_gas_diving" if len(unique_mixes) > 1 else "single_gas_diving"
        
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

        dive_info = {
            "entryType": "Shore",
            "diveGases": garmin_gases
        }
        if dive.weight is not None:
            dive_info["weight"] = dive.weight
            if dive.weight_unit:
                unit_key = dive.weight_unit.lower()
                if unit_key == "kg":
                    unit_key = "kilogram"
                elif unit_key in ["lb", "lbs"]:
                    unit_key = "pound"
                factor = 1000.0 if unit_key == "kilogram" else 453.59237
                unit_id = 8 if unit_key == "kilogram" else 9
                dive_info["weightUnit"] = {
                    "unitId": unit_id,
                    "unitKey": unit_key,
                    "factor": factor
                }
        if dive.visibility is not None:
            dive_info["visibility"] = dive.visibility
            if dive.visibility_unit:
                unit_key = dive.visibility_unit.lower()
                if unit_key == "m":
                    unit_key = "meter"
                elif unit_key in ["ft", "feet"]:
                    unit_key = "foot"
                factor = 100.0 if unit_key == "meter" else 30.48
                unit_id = 1 if unit_key == "meter" else 2
                dive_info["visibilityUnit"] = {
                    "unitId": unit_id,
                    "unitKey": unit_key,
                    "factor": factor
                }

        if dive.buddy is not None:
            dive_info["buddy"] = dive.buddy

        summary_dto = {
            "startTimeLocal": dive.date_time.strftime("%Y-%m-%dT%H:%M:%S.0"),
            "duration": dive.duration,
            "bottomTime": dive.duration,
            "maxDepth": dive.max_depth,
            "averageDepth": dive.avg_depth,
            "minTemperature": dive.temp_min,
            "averageTemperature": dive.temp_avg,
            "maxTemperature": dive.temp_max
        }
        if dive.lat is not None:
            summary_dto["startLatitude"] = dive.lat
        if dive.lng is not None:
            summary_dto["startLongitude"] = dive.lng

        payload = {
            "activityTypeDTO": {
                "typeKey": activity_type
            },
            "accessControlRuleDTO": {
                "typeId": 2,
                "typeKey": "private"
            },
            "timeZoneUnitDTO": {
                "unitKey": "UTC"
            },
            "activityName": dive.location or "Sync Dive",
            "description": dive.notes or "",
            "metadataDTO": {
                "diveNumber": str(dive.dive_number) if dive.dive_number is not None else None,
                "autoCalcCalories": True
            },
            "summaryDTO": summary_dto,
            "diveInfo": dive_info
        }
        # Override fields using divelogs_to_garmin mappings
        try:
            mapping_path = os.path.join(os.path.dirname(__file__), "..", "mapping", "divelogs_to_garmin.json")
            if os.path.exists(mapping_path):
                with open(mapping_path, "r") as f:
                    mapping_def = json.load(f)
                from src.core.mapping_helper import set_jsonpath
                mappings = mapping_def.get("mappings", [])
                for m in mappings:
                    internal_field = m.get("internal_field")
                    garmin_api_path = m.get("garmin_api_path")
                    if not internal_field or not garmin_api_path:
                        continue
                    
                    if internal_field == "dive_number" and dive.dive_number is not None:
                        set_jsonpath(payload, garmin_api_path, str(dive.dive_number))
                    elif internal_field == "max_depth":
                        set_jsonpath(payload, garmin_api_path, dive.max_depth)
                    elif internal_field == "water_temperature" and dive.temp_min is not None:
                        set_jsonpath(payload, garmin_api_path, dive.temp_min)
                    elif internal_field == "latitude" and dive.lat is not None:
                        set_jsonpath(payload, garmin_api_path, dive.lat)
                    elif internal_field == "longitude" and dive.lng is not None:
                        set_jsonpath(payload, garmin_api_path, dive.lng)
        except Exception as e:
            logger.warning("Failed to apply mapping file overrides for Garmin map_from_unified: %s", e)

        return payload
