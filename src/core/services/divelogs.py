import logging
import time
from datetime import datetime
from typing import List, Optional, Dict, Any
import requests

from src.core.adapter import BaseDiveAdapter
from src.core.models import UnifiedDive, GasMixture

logger = logging.getLogger("anti_gravity.divelogs")

class DivelogsAdapter(BaseDiveAdapter):
    def __init__(self, username: str, password: str, cooldown_seconds: float = 1.0):
        self.username = username
        self.password = password
        self.cooldown_seconds = cooldown_seconds
        self.session = requests.Session()
        self.bearer_token: Optional[str] = None
        self.imperial_units = False

    def login(self) -> bool:
        logger.info("Attempting Divelogs.org login...")
        if not self.username or not self.password:
            logger.error("No Divelogs credentials provided.")
            return False

        try:
            url = "https://divelogs.de/api/login"
            data = {"user": self.username, "pass": self.password}
            response = self.session.post(url, data=data, timeout=20)
            
            if response.status_code == 200:
                res_data = response.json()
                self.bearer_token = res_data.get("bearer_token")
                if self.bearer_token:
                    # Update session headers with Auth token
                    self.session.headers.update({
                        "Accept": "application/json",
                        "Authorization": f"Bearer {self.bearer_token}"
                    })
                    logger.info("Successfully authenticated with Divelogs.org.")
                    
                    # Fetch user unit preferences (imperial vs metric)
                    self._fetch_user_preferences()
                    return True
                else:
                    logger.error("Bearer token not found in login response.")
            else:
                logger.error("Divelogs login failed with status code: %d", response.status_code)
        except Exception as e:
            logger.error("Error during Divelogs login: %s", e)
        return False

    def _fetch_user_preferences(self) -> None:
        try:
            logger.info("Fetching Divelogs user profile settings...")
            url = "https://divelogs.de/api/user"
            response = self.session.get(url, timeout=20)
            if response.status_code == 200:
                profile = response.json()
                self.imperial_units = bool(profile.get("imperial", False))
                logger.info("User preferences loaded. Imperial system active: %s", self.imperial_units)
            else:
                logger.warning("Failed to load user profile settings (status: %d). Defaulting to Metric.", response.status_code)
        except Exception as e:
            logger.warning("Error fetching Divelogs user preferences: %s. Defaulting to Metric.", e)

    def fetch_dives(self, date_from: Optional[datetime] = None, date_to: Optional[datetime] = None) -> List[UnifiedDive]:
        if not self.bearer_token and not self.login():
            raise RuntimeError("Cannot fetch dives: Not authenticated with Divelogs.org.")

        logger.info("Fetching detailed dive logs from Divelogs.org...")
        try:
            url = "https://divelogs.de/api/dives"
            response = self.session.get(url, timeout=20)
            
            if response.status_code != 200:
                raise RuntimeError(f"Failed to query Divelogs dives (status: {response.status_code}).")
            
            dives_list = response.json()
            if not isinstance(dives_list, list):
                raise ValueError("Invalid response format from Divelogs api. Expected list.")

            logger.info("Retrieved %d dives from Divelogs.org.", len(dives_list))

            unified_dives: List[UnifiedDive] = []
            for item in dives_list:
                # Parse date and time
                date_str = item.get("date")
                time_str = item.get("time", "00:00:00")
                if not date_str:
                    continue

                dt = self._parse_datetime(f"{date_str} {time_str}")
                if not dt:
                    continue

                # Apply date filters
                if date_from and dt < date_from:
                    continue
                if date_to and dt > date_to:
                    continue

                mapped = self._map_to_unified(item)
                unified_dives.append(mapped)

            return unified_dives

        except Exception as e:
            logger.error("Error fetching dives from Divelogs.org: %s", e)
            raise

    def add_dive(self, dive: UnifiedDive) -> Optional[str]:
        if not self.bearer_token and not self.login():
            logger.error("Cannot add dive: Not authenticated with Divelogs.org.")
            return None

        logger.info("Adding dive at %s to Divelogs.org...", dive.date_time)
        try:
            payload = self._map_from_unified(dive)
            url = "https://divelogs.de/api/dive"
            response = self.session.post(url, json=payload, timeout=20)
            
            if response.status_code in [200, 201]:
                res_data = response.json()
                dive_id = str(res_data.get("id"))
                logger.info("Successfully added dive to Divelogs.org. Assigned ID: %s", dive_id)
                time.sleep(self.cooldown_seconds)
                return dive_id
            elif response.status_code == 400:
                res_json = response.json()
                errors = res_json.get("errors", [])
                if errors and "already exists" in str(errors[0]).lower():
                    logger.warning("Dive at %s already exists on Divelogs.org. Skipping creation.", dive.date_time)
                else:
                    logger.error("Divelogs rejected payload: %s", errors)
            else:
                logger.error("Failed to add dive to Divelogs. Status: %d, Response: %s", response.status_code, response.text)
        except Exception as e:
            logger.error("Error adding dive to Divelogs.org: %s", e)
        return None

    def update_dive(self, external_id: str, dive: UnifiedDive) -> bool:
        if not self.bearer_token and not self.login():
            logger.error("Cannot update dive: Not authenticated with Divelogs.org.")
            return False

        logger.info("Updating Divelogs.org Dive ID %s...", external_id)
        try:
            payload = self._map_from_unified(dive)
            url = f"https://divelogs.de/api/dive/{external_id}"
            response = self.session.put(url, json=payload, timeout=20)
            
            if response.status_code == 200:
                res_data = response.json()
                if res_data.get("success"):
                    logger.info("Successfully updated Divelogs.org Dive ID %s.", external_id)
                    time.sleep(self.cooldown_seconds)
                    return True
            logger.error("Failed to update Divelogs Dive ID %s. Response: %s", external_id, response.text)
        except Exception as e:
            logger.error("Error updating Divelogs.org Dive ID %s: %s", external_id, e)
        return False

    def _parse_datetime(self, dt_str: str) -> Optional[datetime]:
        try:
            return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    def _map_to_unified(self, data: Dict[str, Any]) -> UnifiedDive:
        date_str = data.get("date", "")
        time_str = data.get("time", "00:00:00")
        dt = self._parse_datetime(f"{date_str} {time_str}") or datetime.now()

        duration = int(data.get("duration") or 0)
        
        # Handle conversions for imperial units
        max_depth_val = data.get("maxdepth")
        max_depth = float(max_depth_val) if max_depth_val not in [None, ""] else 0.0
        
        avg_depth_val = data.get("meandepth")
        avg_depth = float(avg_depth_val) if avg_depth_val not in [None, ""] else None
        
        temp_val = data.get("depthtemp")
        temp_min = float(temp_val) if temp_val not in [None, ""] and float(temp_val) > 0.0 else None

        if self.imperial_units:
            # Feet to Meters
            max_depth = max_depth / 3.28084
            if avg_depth is not None:
                avg_depth = avg_depth / 3.28084
            # Fahrenheit to Celsius
            if temp_min is not None:
                temp_min = (temp_min - 32) * 5 / 9

        dive_id = str(data.get("id") or "")
        external_ids = {}
        if dive_id:
            external_ids["divelogs"] = dive_id

        # Mapped gases
        gas_mixtures = []
        tanks = data.get("tanks") or []
        for tank in tanks:
            o2_val = tank.get("o2")
            oxygen = float(o2_val) if o2_val not in [None, ""] else 21.0
            
            he_val = tank.get("he")
            helium = float(he_val) if he_val not in [None, ""] else 0.0
            
            start_p = tank.get("start_pressure")
            start_p = float(start_p) if start_p not in [None, ""] else None
            
            end_p = tank.get("end_pressure")
            end_p = float(end_p) if end_p not in [None, ""] else None
            
            vol = tank.get("vol")
            vol = float(vol) if vol not in [None, ""] else None

            if self.imperial_units:
                # PSI to Bar
                if start_p is not None:
                    start_p = start_p / 14.5038
                if end_p is not None:
                    end_p = end_p / 14.5038
                # cubic foot to liters (1 cuft = 28.3168 liters)
                if vol is not None:
                    vol = vol * 28.3168

            gas_mixtures.append(
                GasMixture(
                    oxygen=oxygen,
                    helium=helium,
                    start_pressure=start_p,
                    end_pressure=end_p,
                    tank_volume=vol
                )
            )

        dive_number_val = data.get("divenumber")
        dive_number = int(dive_number_val) if dive_number_val is not None and str(dive_number_val).isdigit() else None

        # Build location description
        location_parts = []
        if data.get("location"):
            location_parts.append(str(data["location"]))
        if data.get("divesite"):
            location_parts.append(str(data["divesite"]))
        location = ", ".join(location_parts) if location_parts else None

        notes = data.get("notes")

        dive = UnifiedDive(
            date_time=dt,
            duration=duration,
            max_depth=max_depth,
            avg_depth=avg_depth,
            temp_min=temp_min,
            external_ids=external_ids,
            gas_mixtures=gas_mixtures,
            location=location,
            notes=notes,
            dive_number=dive_number
        )

        try:
            from src.core.mapping_helper import MappingEngine
            MappingEngine.apply_divelogs_to_garmin_mapping(data, dive)
        except Exception as e:
            logger.warning("Failed to apply mapping file overrides for Divelogs map_to_unified: %s", e)

        return dive

    def _map_from_unified(self, dive: UnifiedDive) -> Dict[str, Any]:
        # Formulate payload for Divelogs API
        max_depth = dive.max_depth
        avg_depth = dive.avg_depth
        temp_val = dive.temp_min

        if self.imperial_units:
            # Meters to Feet
            max_depth = max_depth * 3.28084
            if avg_depth is not None:
                avg_depth = avg_depth * 3.28084
            # Celsius to Fahrenheit
            if temp_val is not None:
                temp_val = (temp_val * 9 / 5) + 32

        # Location splitting if location contains a comma
        location = ""
        divesite = dive.location or "Site"
        if dive.location and "," in dive.location:
            parts = dive.location.split(",", 1)
            location = parts[0].strip()
            divesite = parts[1].strip()

        tanks = []
        for idx, gas in enumerate(dive.gas_mixtures):
            start_p = gas.start_pressure
            end_p = gas.end_pressure
            vol = gas.tank_volume

            if self.imperial_units:
                # Bar to PSI
                if start_p is not None:
                    start_p = start_p * 14.5038
                if end_p is not None:
                    end_p = end_p * 14.5038
                # Liters to Cubic Feet
                if vol is not None:
                    vol = vol / 28.3168

            tanks.append({
                "o2": gas.oxygen,
                "he": gas.helium,
                "start_pressure": start_p,
                "end_pressure": end_p,
                "vol": vol
            })

        payload = {
            "date": dive.date_time.strftime("%Y-%m-%d"),
            "time": dive.date_time.strftime("%H:%M:%S"),
            "duration": dive.duration,
            "maxdepth": max_depth,
            "meandepth": avg_depth,
            "depthtemp": temp_val or 0.0,
            "location": location,
            "divesite": divesite,
            "notes": dive.notes or "",
            "divenumber": dive.dive_number or 0,
            "tanks": tanks
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
                    dive_log_path = m.get("dive_log_path")
                    if not internal_field or not dive_log_path:
                        continue
                    
                    if internal_field == "dive_number" and dive.dive_number is not None:
                        set_jsonpath(payload, dive_log_path, dive.dive_number)
                    elif internal_field == "max_depth":
                        set_jsonpath(payload, dive_log_path, dive.max_depth)
                    elif internal_field == "water_temperature" and dive.temp_min is not None:
                        set_jsonpath(payload, dive_log_path, dive.temp_min)
        except Exception as e:
            logger.warning("Failed to apply mapping file overrides for Divelogs map_from_unified: %s", e)

        return payload
