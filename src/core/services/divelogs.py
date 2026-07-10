import os
import json
import logging
import time
from datetime import datetime
from typing import List, Optional, Dict, Any
import requests

from src.core.adapter import BaseDiveAdapter
from src.core.models import UnifiedDive, GasMixture, UnifiedSample

logger = logging.getLogger("dive_sync.divelogs")

class DivelogsAdapter(BaseDiveAdapter):
    def __init__(self, username: str, password: str, cooldown_seconds: float = 1.0):
        self.username = username
        self.password = password
        self.cooldown_seconds = cooldown_seconds
        self.session = requests.Session()
        self.bearer_token: Optional[str] = None
        self.imperial_units = False

    def login(self) -> bool:
        logger.info("Attempting Divelogs.org login for user '%s'...", self.username)
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
                    logger.info("Successfully authenticated with Divelogs.org for user '%s'.", self.username)
                    
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
            logger.info("Fetching Divelogs user profile settings for user '%s'...", self.username)
            url = "https://divelogs.de/api/user"
            response = self.session.get(url, timeout=20)
            
            # Fetch dives list to count dives
            dives_count = 0
            try:
                dives_url = "https://divelogs.de/api/dives"
                dives_res = self.session.get(dives_url, timeout=20)
                if dives_res.status_code == 200:
                    dives_list = dives_res.json()
                    if isinstance(dives_list, list):
                        dives_count = len(dives_list)
            except Exception as e:
                logger.warning("Failed to fetch dives list for counting: %s", e)

            if response.status_code == 200:
                profile = response.json()
                self.imperial_units = bool(profile.get("imperial", False))
                logger.info("Fetching Divelogs user profile settings for user '%s'... - %d dives found", self.username, dives_count)
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

    def delete_dive(self, external_id: str) -> bool:
        if not self.bearer_token and not self.login():
            logger.error("Cannot delete dive: Not authenticated with Divelogs.org.")
            return False

        logger.info("Deleting Divelogs.org Dive ID %s...", external_id)
        try:
            url = f"https://divelogs.de/api/dive/{external_id}"
            response = self.session.delete(url, timeout=20)
            
            if response.status_code == 200:
                logger.info("Successfully deleted Divelogs.org Dive ID %s.", external_id)
                time.sleep(self.cooldown_seconds)
                return True
            logger.error("Failed to delete Divelogs Dive ID %s. Response: %s", external_id, response.text)
        except Exception as e:
            logger.error("Error deleting Divelogs.org Dive ID %s: %s", external_id, e)
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

            t_name = tank.get("tankname") or tank.get("tank") or None

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
                    tank_volume=vol,
                    tank_name=t_name
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

        # Parse weight from Divelogs
        raw_weight = data.get("weights")
        weight = None
        weight_unit = None
        if raw_weight is not None and raw_weight != "" and raw_weight != 0:
            import re
            try:
                weight = float(raw_weight)
                if weight != 0.0:
                    weight_unit = "pound" if self.imperial_units else "kilogram"
                else:
                    weight = None
            except (ValueError, TypeError):
                m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z\s]+)?$", str(raw_weight))
                if m:
                    val_str, unit_str = m.groups()
                    weight = float(val_str)
                    unit_str = unit_str.strip().lower() if unit_str else ""
                    if "kg" in unit_str or "kilogram" in unit_str:
                        weight_unit = "kilogram"
                    elif "lb" in unit_str or "pound" in unit_str:
                        weight_unit = "pound"
                    else:
                        weight_unit = "pound" if self.imperial_units else "kilogram"

        # Parse visibility from Divelogs
        raw_visibility = data.get("visibility")
        visibility = None
        visibility_unit = None
        if raw_visibility is not None and raw_visibility != "":
            import re
            try:
                visibility = float(raw_visibility)
                if visibility != 0.0:
                    visibility_unit = "foot" if self.imperial_units else "meter"
                else:
                    visibility = None
            except (ValueError, TypeError):
                m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z\s]+)?$", str(raw_visibility))
                if m:
                    val_str, unit_str = m.groups()
                    visibility = float(val_str)
                    unit_str = unit_str.strip().lower() if unit_str else ""
                    if "m" in unit_str or "meter" in unit_str:
                        visibility_unit = "meter"
                    elif "ft" in unit_str or "foot" in unit_str or "feet" in unit_str:
                        visibility_unit = "foot"
                    else:
                        visibility_unit = "foot" if self.imperial_units else "meter"

        buddy = data.get("buddy")

        # Parse GPS coordinates
        lat_val = data.get("lat")
        lat = float(lat_val) if lat_val not in [None, ""] else None
        lng_val = data.get("lng")
        lng = float(lng_val) if lng_val not in [None, ""] else None

        # Parse profile chart data samples
        samples = []
        sampledata = data.get("sampledata")
        samplerate_val = data.get("samplerate")
        samplerate = int(samplerate_val) if samplerate_val not in [None, ""] and int(samplerate_val) > 0 else 1
        
        if sampledata and isinstance(sampledata, list):
            for i, p in enumerate(sampledata):
                d_val = None
                t_val = None
                
                if isinstance(p, dict):
                    d_val = p.get("d")
                    t_val = p.get("t")
                elif isinstance(p, (int, float)):
                    d_val = p
                    
                if d_val is not None:
                    d_val = float(d_val)
                    if t_val is not None:
                        t_val = float(t_val)
                        
                    # Apply imperial to metric conversion
                    if self.imperial_units:
                        # feet to meters
                        d_val = d_val / 3.28084
                        if t_val is not None:
                            # Fahrenheit to Celsius
                            t_val = (t_val - 32) * 5 / 9
                            
                    samples.append(UnifiedSample(
                        depth=d_val,
                        temp=t_val,
                        time=i * samplerate
                    ))

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
                "vol": vol,
                "tankname": gas.tank_name or "",
                "tank": gas.tank_name or ""
            })

        # Convert weight from Garmin to Divelogs unit preference
        weights_val = 0.0
        if dive.weight is not None:
            w_val = dive.weight
            w_unit = (dive.weight_unit or "kilogram").lower()
            if self.imperial_units:
                # Divelogs expects pounds (lbs)
                if "kg" in w_unit or "kilogram" in w_unit:
                    w_val = w_val * 2.20462
            else:
                # Divelogs expects kilograms (kg)
                if "lb" in w_unit or "pound" in w_unit:
                    w_val = w_val / 2.20462
            weights_val = round(w_val, 2)

        # Convert and format visibility as a string
        visibility_val = ""
        if dive.visibility is not None:
            v_val = dive.visibility
            v_unit = (dive.visibility_unit or "").lower()
            if self.imperial_units:
                # Divelogs expects Imperial (feet / ft)
                if "meter" in v_unit or "m" in v_unit or not v_unit:
                    v_val = v_val * 3.28084
                visibility_val = f"{round(v_val, 1):g} ft"
            else:
                # Divelogs expects Metric (meters / m)
                if "foot" in v_unit or "feet" in v_unit or "ft" in v_unit:
                    v_val = v_val / 3.28084
                visibility_val = f"{round(v_val, 1):g} m"

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
            "weights": weights_val,
            "visibility": visibility_val,
            "buddy": dive.buddy or "",
            "tanks": tanks
        }
        if dive.lat is not None:
            payload["lat"] = dive.lat
        if dive.lng is not None:
            payload["lng"] = dive.lng

        # Add sampledata and samplerate if samples are present
        if dive.samples:
            sampledata = []
            for s in dive.samples:
                d_val = s.depth
                t_val = s.temp
                
                if self.imperial_units:
                    # convert depth to feet
                    d_val = d_val * 3.28084
                    # convert temp to fahrenheit
                    if t_val is not None:
                        t_val = (t_val * 9 / 5) + 32
                        
                if t_val is not None:
                    sampledata.append({"d": round(d_val, 2), "t": round(t_val, 2)})
                else:
                    sampledata.append(round(d_val, 2))
            
            payload["sampledata"] = sampledata
            
            # Calculate samplerate
            samplerate = 1
            times = [s.time for s in dive.samples if s.time is not None]
            if len(times) > 1:
                diffs = [times[i] - times[i-1] for i in range(1, len(times))]
                if diffs:
                    from collections import Counter
                    c = Counter(diffs)
                    most_common = c.most_common(1)[0][0]
                    if most_common > 0:
                        samplerate = int(most_common)
            payload["samplerate"] = samplerate
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
