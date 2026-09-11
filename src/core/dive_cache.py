"""Read/edit/delete access to the locally cached Garmin & Divelogs dive JSON
files (data/garmin/, data/divelogs/), plus pushing edits/deletes to the
remote service. This is the shared logic behind any dive-editing UI (the
desktop app's Garmin/Divelogs sections) - kept here, rather than duplicated
per-UI, so there is exactly one place that understands the raw Garmin/
Divelogs JSON shapes.
"""
import os
import re
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from src.core.config import ConfigManager

logger = logging.getLogger("dive_sync.dive_cache")


def _base_dir(base_dir: Optional[str] = None) -> str:
    return base_dir or os.environ.get("DATA_DIR", "./data")


def _normalize_date_time(raw: Any) -> str:
    """Normalize a raw date/time value into the "YYYY-MM-DD HH:MM:SS" shape
    Divelogs already naturally produces, so both the dive list table and the
    edit form's split Date/Time fields (which just split on the first space)
    see one consistent format regardless of source. Garmin's startTimeLocal
    is ISO-ish ("2026-06-22T10:15:30[.000][+02:00]") - swap the "T" for a
    space and trim anything past the seconds."""
    if not raw:
        return ""
    text = str(raw).strip().replace("T", " ")
    return text[:19] if len(text) >= 19 else text


def _split_date_time(date_time: str) -> Tuple[str, str]:
    """Split an already-normalized "YYYY-MM-DD HH:MM:SS" string into
    separate (date, time) parts, for the dive list table's separate Date/
    Time columns. Mirrors the edit form's own split so both show the same
    two values."""
    date_part, _, time_part = (date_time or "").partition(" ")
    return date_part, time_part


def _seconds_to_minutes_display(seconds: Any) -> Any:
    """Duration is stored (and edited via update_dive_fields) in seconds,
    matching both services' raw APIs, but displayed in the dive list/edit
    form as whole minutes - seconds granularity isn't meaningful there."""
    try:
        return int(round(float(seconds) / 60))
    except (TypeError, ValueError):
        return seconds


def _format_number(value: Any) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def _format_water_temp(temp_min: Any, temp_max: Any, temp_avg: Any) -> str:
    """Combines the three temperature readings both services can report into
    one compact display string for the dive list/edit form, e.g.
    "17-19°C (avg 18°C)". Missing readings are simply omitted."""
    parts = []
    if temp_min not in (None, "") and temp_max not in (None, ""):
        parts.append(f"{_format_number(temp_min)}-{_format_number(temp_max)}°C")
    elif temp_min not in (None, ""):
        parts.append(f"{_format_number(temp_min)}°C")
    elif temp_max not in (None, ""):
        parts.append(f"{_format_number(temp_max)}°C")
    if temp_avg not in (None, ""):
        parts.append(f"(avg {_format_number(temp_avg)}°C)")
    return " ".join(parts)


def _format_tanks(tanks: List[Dict[str, Any]]) -> str:
    """Formats a list of {oxygen, helium, start_pressure, end_pressure,
    volume, tank_name} dicts (one per cylinder) into one display string for
    the dive edit form's read-only "Tanks / Gas" field, e.g.
    "Tank 1: 21% O2, 12L, 200->50 bar; Tank 2: 32% O2, 50->80 bar"."""
    entries = []
    for idx, tank in enumerate(tanks, start=1):
        name = tank.get("tank_name") or f"Tank {idx}"
        bits = []

        oxygen = tank.get("oxygen")
        if oxygen not in (None, ""):
            gas = f"{_format_number(oxygen)}% O2"
            helium = tank.get("helium")
            if helium not in (None, "", 0, 0.0):
                gas += f"/{_format_number(helium)}% He"
            bits.append(gas)

        volume = tank.get("volume")
        if volume not in (None, ""):
            bits.append(f"{_format_number(volume)}L")

        start_p, end_p = tank.get("start_pressure"), tank.get("end_pressure")
        if start_p not in (None, "") or end_p not in (None, ""):
            sp = _format_number(start_p) if start_p not in (None, "") else "?"
            ep = _format_number(end_p) if end_p not in (None, "") else "?"
            bits.append(f"{sp}->{ep} bar")

        entries.append(f"{name}: {', '.join(bits)}" if bits else name)
    return "; ".join(entries)


def _resolve_service_dir(service: str, username: Optional[str], base_dir: Optional[str] = None) -> Optional[str]:
    root = _base_dir(base_dir)
    if username:
        path_user = os.path.join(root, service, username)
        if os.path.isdir(path_user) and os.listdir(path_user):
            return path_user
    path_direct = os.path.join(root, service)
    if os.path.isdir(path_direct) and os.listdir(path_direct):
        return path_direct
    return None


def _iter_dive_files(service_dir: str) -> List[Tuple[str, str]]:
    files = []
    for name in os.listdir(service_dir):
        path_name = os.path.join(service_dir, name)
        if os.path.isdir(path_name):
            for subname in os.listdir(path_name):
                if subname.endswith(".json") and subname != "sync_state.json":
                    files.append((subname, os.path.join(path_name, subname)))
        elif name.endswith(".json") and name != "sync_state.json":
            files.append((name, path_name))
    return files


def _find_dive_file(service: str, filename: str, username: Optional[str] = None, base_dir: Optional[str] = None) -> Optional[str]:
    root = _base_dir(base_dir)
    if username:
        path = os.path.join(root, service, username, filename)
        if os.path.exists(path):
            return path
    path = os.path.join(root, service, filename)
    if os.path.exists(path):
        return path
    service_dir = os.path.join(root, service)
    if os.path.isdir(service_dir):
        for sub in os.listdir(service_dir):
            sub_path = os.path.join(service_dir, sub)
            if os.path.isdir(sub_path):
                candidate = os.path.join(sub_path, filename)
                if os.path.exists(candidate):
                    return candidate
    return None


def _username_from_filepath(service: str, filepath: str) -> Optional[str]:
    parts = filepath.replace("\\", "/").split("/")
    if len(parts) >= 3 and parts[-3] == service:
        return parts[-2]
    return None


def list_garmin_dives(username: Optional[str] = None, base_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    dives = []
    service_dir = _resolve_service_dir("garmin", username, base_dir)
    if not service_dir:
        return dives

    for filename, filepath in _iter_dive_files(service_dir):
        try:
            with open(filepath, "r") as f:
                data = json.load(f)

            summary = data.get("summary", {})
            details = data.get("details") or {}
            if not isinstance(details, dict):
                details = {}

            sum_dto = details.get("summaryDTO", {}) or summary.get("summaryDTO", {}) or {}
            metadata = details.get("metadataDTO", {}) or summary.get("metadataDTO", {}) or {}

            dive_num = metadata.get("diveNumber") or filename.replace(".json", "")
            date_time = _normalize_date_time(sum_dto.get("startTimeLocal") or summary.get("startTimeLocal") or "")
            duration = sum_dto.get("duration") or summary.get("duration") or 0
            max_depth = sum_dto.get("maxDepth") or summary.get("maxDepth") or 0.0
            location = details.get("activityName") or summary.get("activityName") or ""
            notes = details.get("description") or summary.get("description") or ""

            avg_depth = sum_dto.get("averageDepth") or summary.get("averageDepth")
            try:
                avg_depth_display = f"{float(avg_depth):.2f}" if avg_depth is not None else ""
            except (TypeError, ValueError):
                avg_depth_display = str(avg_depth or "")

            water_temp = _format_water_temp(
                sum_dto.get("minTemperature"), sum_dto.get("maxTemperature"), sum_dto.get("averageTemperature")
            )

            info = details.get("diveInfo") or summary.get("diveInfo") or {}
            tanks = [
                {
                    "oxygen": gas.get("oxygenContent"),
                    "helium": gas.get("heliumContent"),
                    "start_pressure": gas.get("tankStartingPressure"),
                    "end_pressure": gas.get("tankEndingPressure"),
                    "volume": gas.get("tankSize"),
                    "tank_name": gas.get("tankName"),
                }
                for gas in (info.get("diveGases") or [])
            ]

            weight = info.get("weight")
            weight_unit = info.get("weightUnit", {}).get("unitKey") if isinstance(info.get("weightUnit"), dict) else ""
            visibility = info.get("visibility")
            visibility_unit = info.get("visibilityUnit", {}).get("unitKey") if isinstance(info.get("visibilityUnit"), dict) else ""
            buddy = info.get("buddy") or ""

            weight_str = ""
            if weight is not None:
                w_unit = "kg" if "kilogram" in str(weight_unit).lower() else "lbs"
                weight_str = f"{weight:g} {w_unit}"

            visibility_str = ""
            if visibility is not None:
                v_unit = "m" if "meter" in str(visibility_unit).lower() else "ft"
                visibility_str = f"{visibility:g} {v_unit}"

            duration_display = _seconds_to_minutes_display(duration)
            # Garmin's API returns maxDepth as a float with a long tail
            # (e.g. 18.199999999) - format for display.
            try:
                max_depth_display = f"{float(max_depth):.2f}"
            except (TypeError, ValueError):
                max_depth_display = max_depth

            date_part, time_part = _split_date_time(date_time)
            dives.append({
                "id": str(summary.get("activityId") or ""),
                "dive_number": dive_num,
                "date_time": date_time,
                "date": date_part,
                "time": time_part,
                "duration": duration_display,
                "max_depth": max_depth_display,
                "avg_depth": avg_depth_display,
                "water_temp": water_temp,
                "tanks": _format_tanks(tanks),
                "location": location,
                "notes": notes,
                "weight": weight_str,
                "visibility": visibility_str,
                "buddy": buddy,
                "filename": filename,
            })
        except Exception as e:
            logger.warning("Failed to parse cached Garmin dive file %s: %s", filename, e)

    dives.sort(key=lambda x: x["date_time"], reverse=True)
    return dives


def list_divelogs_dives(username: Optional[str] = None, base_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    dives = []
    service_dir = _resolve_service_dir("divelogs", username, base_dir)
    if not service_dir:
        return dives

    for filename, filepath in _iter_dive_files(service_dir):
        try:
            with open(filepath, "r") as f:
                data = json.load(f)

            dive_num = data.get("divenumber") or filename.replace(".json", "")
            date = data.get("date") or ""
            time = data.get("time") or "00:00:00"
            garmin_id = data.get("garmin_id") or ""

            location_parts = []
            if data.get("location"):
                location_parts.append(str(data["location"]))
            if data.get("divesite"):
                location_parts.append(str(data["divesite"]))
            location = ", ".join(location_parts) if location_parts else ""

            avg_depth = data.get("meandepth")
            try:
                avg_depth_display = f"{float(avg_depth):.2f}" if avg_depth not in (None, "") else ""
            except (TypeError, ValueError):
                avg_depth_display = str(avg_depth or "")

            try:
                depthtemp = float(data.get("depthtemp"))
                depthtemp = depthtemp if depthtemp > 0.0 else None
            except (TypeError, ValueError):
                depthtemp = None
            water_temp = _format_water_temp(depthtemp, None, None)

            tanks = [
                {
                    "oxygen": tank.get("o2"),
                    "helium": tank.get("he"),
                    "start_pressure": tank.get("start_pressure"),
                    "end_pressure": tank.get("end_pressure"),
                    "volume": tank.get("vol"),
                    "tank_name": tank.get("tankname") or tank.get("tank"),
                }
                for tank in (data.get("tanks") or [])
            ]

            weights_val = data.get("weights")
            weight_str = ""
            if weights_val not in [None, "", 0]:
                try:
                    w_num = float(weights_val)
                    weight_str = f"{w_num:g}"
                except (ValueError, TypeError):
                    weight_str = str(weights_val)

            visibility_str = str(data.get("visibility") or "")
            buddy = data.get("buddy") or ""

            normalized_date_time = _normalize_date_time(f"{date} {time}")
            date_part, time_part = _split_date_time(normalized_date_time)
            dives.append({
                "id": str(data.get("id") or ""),
                "dive_number": dive_num,
                "date_time": normalized_date_time,
                "date": date_part,
                "time": time_part,
                "duration": _seconds_to_minutes_display(data.get("duration") or 0),
                "max_depth": data.get("maxdepth") or 0.0,
                "avg_depth": avg_depth_display,
                "water_temp": water_temp,
                "tanks": _format_tanks(tanks),
                "location": location,
                "notes": data.get("notes") or "",
                "garmin_id": garmin_id,
                "weight": weight_str,
                "visibility": visibility_str,
                "buddy": buddy,
                "filename": filename,
            })
        except Exception as e:
            logger.warning("Failed to parse cached Divelogs dive file %s: %s", filename, e)

    dives.sort(key=lambda x: x["date_time"], reverse=True)
    return dives


def read_raw_dive(service: str, filename: str, username: Optional[str] = None, base_dir: Optional[str] = None) -> Dict[str, Any]:
    filepath = _find_dive_file(service, filename, username, base_dir)
    if not filepath:
        raise FileNotFoundError(f"Dive file not found: {service}/{filename}")
    with open(filepath, "r") as f:
        return json.load(f)


def update_dive_fields(
    service: str,
    filename: str,
    username: Optional[str] = None,
    base_dir: Optional[str] = None,
    dive_number: Optional[str] = None,
    date_time: Optional[str] = None,
    duration: Optional[int] = None,
    max_depth: Optional[float] = None,
    location: Optional[str] = None,
    notes: Optional[str] = None,
    weight: Optional[str] = None,
    visibility: Optional[str] = None,
    buddy: Optional[str] = None,
) -> str:
    """Apply the given (non-None) field overrides to the cached dive file and
    write it back. Returns the filepath that was written, for the caller to
    hand to push_remote_update(). An empty string clears a text field; a
    field left as None (the default) is left untouched.

    `duration` is in minutes (matching the dive list/edit form's display -
    see _seconds_to_minutes_display) and is converted to seconds here before
    being written, since that's what both services' raw JSON stores."""
    filepath = _find_dive_file(service, filename, username, base_dir)
    if not filepath:
        raise FileNotFoundError(f"Dive file not found: {service}/{filename}")

    if duration is not None:
        duration = duration * 60

    with open(filepath, "r") as f:
        dive_data = json.load(f)

    if service == "garmin":
        summary = dive_data.get("summary", {})
        details = dive_data.get("details") or {}
        if not isinstance(details, dict):
            details = {}
            dive_data["details"] = details

        if dive_number is not None:
            summary.setdefault("metadataDTO", {})
            details.setdefault("metadataDTO", {})
            summary["metadataDTO"]["diveNumber"] = dive_number
            details["metadataDTO"]["diveNumber"] = dive_number

        if date_time is not None:
            summary.setdefault("summaryDTO", {})
            details.setdefault("summaryDTO", {})
            summary["startTimeLocal"] = date_time
            details["startTimeLocal"] = date_time
            summary["summaryDTO"]["startTimeLocal"] = date_time
            details["summaryDTO"]["startTimeLocal"] = date_time

        if duration is not None:
            summary.setdefault("summaryDTO", {})
            details.setdefault("summaryDTO", {})
            summary["duration"] = duration
            details["duration"] = duration
            summary["summaryDTO"]["duration"] = duration
            details["summaryDTO"]["duration"] = duration
            summary["summaryDTO"]["bottomTime"] = duration
            details["summaryDTO"]["bottomTime"] = duration

        if max_depth is not None:
            summary.setdefault("summaryDTO", {})
            details.setdefault("summaryDTO", {})
            summary["maxDepth"] = max_depth
            details["maxDepth"] = max_depth
            summary["summaryDTO"]["maxDepth"] = max_depth
            details["summaryDTO"]["maxDepth"] = max_depth

        if location is not None:
            summary["activityName"] = location
            details["activityName"] = location
            details["locationName"] = location
            summary["locationName"] = location

        if notes is not None:
            val = None if notes == "" else notes
            summary["description"] = val
            details["description"] = val

        if not isinstance(summary.get("diveInfo"), dict):
            summary["diveInfo"] = {}
        if not isinstance(details.get("diveInfo"), dict):
            details["diveInfo"] = {}

        if weight is not None:
            weight_val = None
            weight_unit = "kilogram"
            match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]+)?$", weight)
            if match:
                weight_val = float(match.group(1))
                unit_str = (match.group(2) or "").strip().lower()
                if "lb" in unit_str or "pound" in unit_str:
                    weight_unit = "pound"
            else:
                try:
                    weight_val = float(weight)
                except ValueError:
                    pass

            summary["diveInfo"]["weight"] = weight_val
            details["diveInfo"]["weight"] = weight_val
            if weight_val is not None:
                factor = 1000.0 if weight_unit == "kilogram" else 453.59237
                u_info = {"unitId": 8 if weight_unit == "kilogram" else 9, "unitKey": weight_unit, "factor": factor}
                summary["diveInfo"]["weightUnit"] = u_info
                details["diveInfo"]["weightUnit"] = u_info
            else:
                summary["diveInfo"]["weightUnit"] = None
                details["diveInfo"]["weightUnit"] = None

        if visibility is not None:
            vis_val = None
            vis_unit = "meter"
            match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]+)?$", visibility)
            if match:
                vis_val = float(match.group(1))
                unit_str = (match.group(2) or "").strip().lower()
                if "ft" in unit_str or "foot" in unit_str or "feet" in unit_str:
                    vis_unit = "foot"
            else:
                try:
                    vis_val = float(visibility)
                except ValueError:
                    pass

            summary["diveInfo"]["visibility"] = vis_val
            details["diveInfo"]["visibility"] = vis_val
            if vis_val is not None:
                factor = 100.0 if vis_unit == "meter" else 30.48
                u_info = {"unitId": 1 if vis_unit == "meter" else 2, "unitKey": vis_unit, "factor": factor}
                summary["diveInfo"]["visibilityUnit"] = u_info
                details["diveInfo"]["visibilityUnit"] = u_info
            else:
                summary["diveInfo"]["visibilityUnit"] = None
                details["diveInfo"]["visibilityUnit"] = None

        if buddy is not None:
            val = None if buddy == "" else buddy
            summary["diveInfo"]["buddy"] = val
            details["diveInfo"]["buddy"] = val

    elif service == "divelogs":
        if date_time is not None:
            dt_parts = date_time.split(" ", 1)
            dive_data["date"] = dt_parts[0]
            if len(dt_parts) > 1:
                dive_data["time"] = dt_parts[1]

        if duration is not None:
            dive_data["duration"] = duration

        if max_depth is not None:
            dive_data["maxdepth"] = max_depth

        if location is not None:
            if "," in location:
                parts = location.split(",", 1)
                dive_data["location"] = parts[0].strip()
                dive_data["divesite"] = parts[1].strip()
            else:
                dive_data["location"] = location
                dive_data["divesite"] = ""

        if notes is not None:
            dive_data["notes"] = notes

        if weight is not None:
            dive_data["weights"] = weight

        if visibility is not None:
            dive_data["visibility"] = visibility

        if buddy is not None:
            dive_data["buddy"] = buddy

    else:
        raise ValueError(f"Unknown service: {service!r}")

    with open(filepath, "w") as f:
        json.dump(dive_data, f, indent=2)

    logger.info("Updated cached %s dive %s.", service, filename)
    return filepath


def delete_dive_local(service: str, filename: str, username: Optional[str] = None, base_dir: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """Remove the cached dive file. Returns (filepath, external_id) - the
    external_id (Garmin activityId / Divelogs id), if one could be read
    before deletion, for the caller to pass to push_remote_delete()."""
    filepath = _find_dive_file(service, filename, username, base_dir)
    if not filepath:
        raise FileNotFoundError(f"Dive file not found: {service}/{filename}")

    external_id = None
    try:
        with open(filepath, "r") as f:
            dive_data = json.load(f)
        if service == "garmin":
            external_id = dive_data.get("summary", {}).get("activityId")
        elif service == "divelogs":
            external_id = dive_data.get("id")
    except Exception as e:
        logger.error("Failed to read dive file %s before deletion: %s", filename, e)

    os.remove(filepath)
    logger.info("Deleted local cache file %s: %s", filename, filepath)
    return filepath, str(external_id) if external_id else None


def _active_credentials(service: str, username: Optional[str]):
    creds = ConfigManager.load_credentials()
    accounts = creds.get_garmin_accounts() if service == "garmin" else creds.get_divelogs_accounts()
    if not username:
        if len(accounts) == 1:
            username = accounts[0].username
        else:
            raise ValueError(
                f"Multiple {service} accounts configured but no username was given to disambiguate."
            )
    matching = [a for a in accounts if a.username == username]
    if not matching:
        raise ValueError(f"No {service} credentials found for username: {username}")
    return matching[0]


def push_remote_update(service: str, filepath: str, username: Optional[str] = None) -> bool:
    """Push a cached dive file's current contents to the remote service.
    Call after update_dive_fields() has written the file."""
    username = username or _username_from_filepath(service, filepath)
    active_creds = _active_credentials(service, username)
    if not active_creds.username or not active_creds.password:
        logger.warning("%s credentials not found, skipping remote update.", service)
        return False

    with open(filepath, "r") as f:
        dive_data = json.load(f)

    if service == "garmin":
        summary = dive_data.get("summary", {})
        details = dive_data.get("details") or {}
        activity_id = summary.get("activityId")
        if not activity_id:
            logger.error("No Garmin activityId found in cache for %s; cannot update remotely.", filepath)
            return False

        from src.core.services.garmin import GarminAdapter
        adapter = GarminAdapter(active_creds.username, active_creds.password, token_dir=active_creds.token_dir)
        unified_dive = adapter._map_to_unified(summary, details)
        logger.info("Updating Garmin Connect for Activity ID %s...", activity_id)
        success = adapter.update_dive(str(activity_id), unified_dive)
    elif service == "divelogs":
        dive_id = dive_data.get("id")
        if not dive_id:
            logger.error("No Divelogs ID found in cache for %s; cannot update remotely.", filepath)
            return False

        from src.core.services.divelogs import DivelogsAdapter
        adapter = DivelogsAdapter(active_creds.username, active_creds.password)
        unified_dive = adapter._map_to_unified(dive_data)
        logger.info("Updating Divelogs.org for Dive ID %s...", dive_id)
        success = adapter.update_dive(str(dive_id), unified_dive)
    else:
        raise ValueError(f"Unknown service: {service!r}")

    if success:
        logger.info("%s successfully updated remotely.", service.capitalize())
    else:
        logger.error("%s remote update failed.", service.capitalize())
    return success


def push_remote_delete(service: str, external_id: str, username: Optional[str] = None, filepath: Optional[str] = None) -> bool:
    username = username or (_username_from_filepath(service, filepath) if filepath else None)
    active_creds = _active_credentials(service, username)
    if not active_creds.username or not active_creds.password:
        logger.warning("%s credentials not found, skipping remote delete.", service)
        return False

    if service == "garmin":
        from src.core.services.garmin import GarminAdapter
        adapter = GarminAdapter(active_creds.username, active_creds.password, token_dir=active_creds.token_dir)
    elif service == "divelogs":
        from src.core.services.divelogs import DivelogsAdapter
        adapter = DivelogsAdapter(active_creds.username, active_creds.password)
    else:
        raise ValueError(f"Unknown service: {service!r}")

    logger.info("Deleting %s remotely for ID %s...", service, external_id)
    success = adapter.delete_dive(str(external_id))
    if success:
        logger.info("%s successfully deleted remotely for ID %s.", service.capitalize(), external_id)
    else:
        logger.error("%s remote delete failed for ID %s.", service.capitalize(), external_id)
    return success
