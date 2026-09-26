"""Read/edit/delete access to the locally cached dive JSON files
(data/garmin/, data/divelogs/, data/submersion/, data/subsurface/), plus
pushing edits/deletes to the remote service. This is the shared logic behind
any dive-editing UI (the desktop app's per-service Dives sections) - kept
here, rather than duplicated per-UI, so there is exactly one place that
understands each cached JSON shape.

Garmin and Divelogs are cached as the raw payload their API returned;
Submersion and Subsurface are cached as UnifiedDive (see
UNIFIED_CACHE_SERVICES below).
"""
import os
import re
import json
import logging
import shutil
from typing import Any, Dict, List, Optional, Tuple

from src.core import garmin_files
from src.core.config import ConfigManager
from src.core.models import recorded_water_temp

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


def _format_sac(tanks: List[Dict[str, Any]], avg_depth: Any, duration_seconds: Any) -> str:
    """Surface Air Consumption in litres per minute, e.g. "14.2".

    Gas breathed at the surface is ``pressure drop x cylinder volume``, and
    at depth a breath costs the ambient pressure in atmospheres, so::

        SAC = sum(bar used x litres) / (minutes x (avg depth / 10 + 1))

    Every cylinder is added up, which is what makes the number meaningful on
    a multi-tank dive. Both services report bar, litres, metres and seconds
    here, so there is no unit conversion to do.

    Returns "" unless the dive has everything the sum needs: a cylinder with
    a volume and both pressures, an average depth and a duration. A volume of
    0 (Divelogs stores that when the cylinder size was never filled in) is
    missing data, not a zero-litre tank."""
    try:
        minutes = float(duration_seconds) / 60.0
        depth = float(avg_depth)
    except (TypeError, ValueError):
        return ""
    if minutes <= 0 or depth <= 0:
        return ""

    litres = 0.0
    for tank in tanks:
        try:
            volume = float(tank.get("volume") or 0)
            start_p = float(tank.get("start_pressure") or 0)
            end_p = float(tank.get("end_pressure") or 0)
        except (TypeError, ValueError):
            continue
        used = start_p - end_p
        if volume > 0 and used > 0:
            litres += used * volume
    if litres <= 0:
        return ""

    ata = depth / 10.0 + 1.0
    return f"{litres / (minutes * ata):.1f}"


def _resolve_service_dir(service: str, username: Optional[str], base_dir: Optional[str] = None) -> Optional[str]:
    root = _base_dir(base_dir)
    path_direct = os.path.join(root, service)
    if username:
        path_user = os.path.join(root, service, username)
        if os.path.isdir(path_user) and os.listdir(path_user):
            return path_user
        # Nothing cached for this account yet. Fall back to a flat, pre-account
        # cache only - never to the service directory when it holds other
        # accounts' folders, or one account's page would list another's dives
        # (rework.md E19).
        if os.path.isdir(path_direct) and any(os.path.isdir(os.path.join(path_direct, n))
                                              for n in os.listdir(path_direct)):
            return None
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
    if username and is_unified_cache(service):
        # per-account cache: only that account's folder (ids may repeat across accounts)
        path = os.path.join(unified_cache_dir(service, username, base_dir), filename)
        return path if os.path.exists(path) else None
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
    if is_unified_cache(service):
        # <service>/dives/<account>/<file> (unified_cache_dir); the flat
        # <service>/dives/<file> names no account
        if len(parts) >= 4 and parts[-4] == service and parts[-3] == UNIFIED_CACHE_SUBDIR:
            return parts[-2]
        return None
    if len(parts) >= 3 and parts[-3] == service:
        return parts[-2]
    return None


def garmin_file_mtimes(username: Optional[str] = None, base_dir: Optional[str] = None) -> Dict[str, float]:
    """{path: mtime} of every cached Garmin dive file - where
    list_garmin_dives(known=...) starts from while a refresh runs."""
    service_dir = _resolve_service_dir("garmin", username, base_dir)
    mtimes: Dict[str, float] = {}
    for _, filepath in _iter_dive_files(service_dir) if service_dir else []:
        try:
            mtimes[filepath] = os.path.getmtime(filepath)
        except OSError:
            pass
    return mtimes


def list_garmin_dives(username: Optional[str] = None, base_dir: Optional[str] = None,
                      known: Optional[Dict[str, float]] = None) -> List[Dict[str, Any]]:
    """``known`` ({path: mtime}, updated in place) lists only the files
    written since: a refresh fills the dives table as Garmin's dives arrive,
    a few seconds apart, without re-reading the whole cache each time."""
    dives = []
    service_dir = _resolve_service_dir("garmin", username, base_dir)
    if not service_dir:
        return dives

    # One directory read per account for the FIT column, not one per dive.
    fit_indexes: Dict[Optional[str], Dict[str, str]] = {}

    for filename, filepath in _iter_dive_files(service_dir):
        try:
            if known is not None:
                mtime = os.path.getmtime(filepath)
                if known.get(filepath) == mtime:
                    continue
            with open(filepath, "r") as f:
                data = json.load(f)
            if known is not None:
                known[filepath] = mtime

            account = _username_from_filepath("garmin", filepath)
            if account not in fit_indexes:
                fit_indexes[account] = garmin_files.fit_index(account, base_dir)

            summary = data.get("summary", {})
            details = data.get("details") or {}
            if not isinstance(details, dict):
                details = {}
            manual = garmin_files.is_manual_dive(data)
            fit_file = fit_indexes[account].get(str(summary.get("activityId") or "")) or ""

            sum_dto = details.get("summaryDTO", {}) or summary.get("summaryDTO", {}) or {}
            metadata = details.get("metadataDTO", {}) or summary.get("metadataDTO", {}) or {}

            dive_num = metadata.get("diveNumber") or ""
            date_time = _normalize_date_time(sum_dto.get("startTimeLocal") or summary.get("startTimeLocal") or "")
            duration = sum_dto.get("duration") or summary.get("duration") or 0
            max_depth = sum_dto.get("maxDepth") or summary.get("maxDepth") or 0.0
            location = details.get("activityName") or summary.get("activityName") or ""
            location_name = details.get("locationName") or summary.get("locationName") or ""
            notes = details.get("description") or summary.get("description") or ""

            avg_depth = sum_dto.get("averageDepth") or summary.get("averageDepth")
            try:
                avg_depth_display = f"{float(avg_depth):.2f}" if avg_depth is not None else ""
            except (TypeError, ValueError):
                avg_depth_display = str(avg_depth or "")

            # 0 = never entered (hand-logged dives): shown as no temperature
            temp_min, temp_max, temp_avg = (recorded_water_temp(sum_dto.get(k))
                                            for k in ("minTemperature", "maxTemperature", "averageTemperature"))
            water_temp = _format_water_temp(temp_min, temp_max, temp_avg)

            info = details.get("diveInfo") or summary.get("diveInfo") or {}
            # A tank with a transmitter takes its name and pressures from the
            # sensor (tankSensors[]), as in GarminAdapter._map_to_unified.
            sensor_doc = data.get("tanksensor")
            sensors = (sensor_doc.get("tankSensors") or []) if isinstance(sensor_doc, dict) else []
            by_index = {s.get("tankIndex"): s for s in sensors}

            def _sensor_pressure(sensor, key):
                value = sensor.get(key)
                if value is not None and "PSI" in str(sensor.get("pressureUnit") or "BAR").upper():
                    value = value / 14.5038
                return round(value, 1) if value is not None else None

            tanks = []
            for i, gas in enumerate(info.get("diveGases") or []):
                sensor = by_index.get(gas.get("gasIndex", i)) or by_index.get(i) or {}
                start_p = _sensor_pressure(sensor, "startingPressure")
                end_p = _sensor_pressure(sensor, "endingPressure")
                tanks.append({
                    "oxygen": gas.get("oxygenContent"),
                    "helium": gas.get("heliumContent"),
                    "start_pressure": start_p if start_p is not None else gas.get("tankStartingPressure"),
                    "end_pressure": end_p if end_p is not None else gas.get("tankEndingPressure"),
                    "volume": gas.get("tankSize"),
                    "tank_name": gas.get("tankName") or sensor.get("name"),
                })

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

            lat = sum_dto.get("startLatitude")
            lng = sum_dto.get("startLongitude")
            water_temp_value = next((t for t in (temp_avg, temp_min, temp_max) if t is not None), None)

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
                "sac": _format_sac(tanks, avg_depth, duration),
                "water_temp": water_temp,
                "water_temp_value": water_temp_value,
                "tanks": _format_tanks(tanks),
                "tanks_detail": tanks,
                "tanks_editable": False,  # Garmin's gas API is read-only (rework.md E4)
                "lat": lat,
                "lng": lng,
                "location": location,
                "activity_name": location,
                "location_name": location_name,
                "notes": notes,
                "weight": weight_str,
                "visibility": visibility_str,
                "buddy": buddy,
                "filename": filename,
                "account": account,
                "manual": manual,
                # Whether the dive's original .fit has been downloaded
                # (garmin_files); "✓" is what the dive tables show. A
                # hand-logged dive has no device file - Connect only makes
                # up a summary-only FIT from the typed-in fields - so it is
                # marked "manual" instead and never counted as missing one.
                "fit": "manual" if manual else ("✓" if fit_file else ""),
                "fit_file": fit_file,
            })
        except Exception as e:
            if known is None:
                logger.warning("Failed to parse cached Garmin dive file %s: %s", filename, e)
            else:       # replaced or pruned by the refresh; the final listing reports it
                logger.debug("Skipped Garmin dive file %s while refreshing: %s", filename, e)

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

            lat_val = data.get("lat")
            lng_val = data.get("lng")
            if lat_val in (0, 0.0) and lng_val in (0, 0.0):
                lat_val = lng_val = None

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
                "sac": _format_sac(tanks, data.get("meandepth"), data.get("duration")),
                "water_temp": water_temp,
                "water_temp_value": depthtemp,
                "tanks": _format_tanks(tanks),
                "tanks_detail": tanks,
                "tanks_editable": True,
                "lat": lat_val,
                "lng": lng_val,
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


# ---------------------------------------------------------------------------
# Services cached as UnifiedDive (rework.md F3 services: Submersion, Subsurface)
# ---------------------------------------------------------------------------
#
# Garmin and Divelogs are cached as the raw payload their API returned, and
# every reader here re-parses that native shape. The later services have no
# such single payload - their adapters build a UnifiedDive out of a changeset
# log or a git checkout - so their cache holds the UnifiedDive itself. That
# makes the readers below trivial (the field names are already the unified
# ones) and the write path exact: the file IS what update_dive() takes.
UNIFIED_CACHE_SERVICES = ("submersion", "subsurface")


def is_unified_cache(service: str) -> bool:
    return service in UNIFIED_CACHE_SERVICES


def _unified_dive_filename(dive: Dict[str, Any], service: str, index: int) -> str:
    external = (dive.get("external_ids") or {}).get(service)
    stem = str(external or dive.get("dive_number") or f"dive_{index}")
    return _safe_filename(stem) + ".json"


def _safe_filename(stem: str) -> str:
    """Submersion ids are UUIDs and Subsurface's are date-based strings; keep
    them recognisable but never let one escape the cache directory."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", stem) or "dive"


UNIFIED_CACHE_SUBDIR = "dives"


def unified_cache_dir(service: str, username: Optional[str] = None, base_dir: Optional[str] = None) -> str:
    """``DATA_DIR/<service>/dives`` - deliberately NOT ``DATA_DIR/<service>``.

    SubmersionAdapter keeps its device identity and HLC clock in
    ``DATA_DIR/submersion`` (device.json, hlc_<id>.json): that device id is
    how the Submersion sync mesh knows this installation, and losing it makes
    dive_sync republish as a brand-new device, orphaning everything it
    published before. Caching dives in that same directory would have put
    them one ``overwrite=True`` (which clears the directory) away from being
    wiped out, and would have had the listing try to parse those state files
    as dives. The cache gets its own subdirectory instead."""
    parts = [_base_dir(base_dir), service, UNIFIED_CACHE_SUBDIR]
    if username:
        parts.append(account_dir_name(username))
    return os.path.join(*parts)


def account_dir_name(username: str) -> str:
    """One account's cache folder under ``<service>/dives`` (a Subsurface
    Cloud email): readable, but never able to leave the cache directory."""
    return re.sub(r"[^A-Za-z0-9_.@-]", "_", username.strip()) or "account"


def prune_cache_dir(directory: str, keep: "set[str]", label: str = "") -> List[str]:
    """Delete cached ``.json`` files that no longer correspond to a live dive
    (rework.md E18). Returns the filenames removed.

    A refresh used only ever to *write*, so a dive deleted on the service kept
    its cached file for ever, and a dive renumbered there got a second file
    under the new name while the old one lingered - both showed up as ghosts
    in the dive table. ``keep`` is every filename this pass wrote or
    deliberately left alone, so both cases fall out of one rule.

    Only ever called after a complete, successful pass. It also refuses to
    empty a non-empty cache: an API hiccup that answers with an empty list
    must not be able to delete the whole local copy."""
    if not os.path.isdir(directory):
        return []
    present = {name for name in os.listdir(directory) if name.endswith(".json")}
    stale = sorted(present - set(keep))
    if not stale:
        return []
    if not keep:
        logger.warning("Not pruning %s: this pass kept no dives at all, which looks like a failed "
                       "listing rather than an empty account (%d cached file(s) left alone).",
                       directory, len(present))
        return []
    for name in stale:
        try:
            os.remove(os.path.join(directory, name))
        except OSError as e:
            logger.warning("Could not remove stale cache file %s: %s", name, e)
    logger.info("Removed %d %scache file(s) with no matching dive on the service: %s",
                len(stale), f"{label} " if label else "", ", ".join(stale))
    return stale


def save_unified_dives(service: str, dives: List[Any], username: Optional[str] = None,
                       base_dir: Optional[str] = None, overwrite: bool = False,
                       prune: bool = False) -> int:
    """Write UnifiedDive objects (or plain dicts) into the cache directory for
    ``service``, one JSON file each. Returns how many were written.

    ``prune`` removes cached files these dives did not account for, so pass it
    only when ``dives`` really is every dive the service has (a download), not
    when saving a subset."""
    directory = unified_cache_dir(service, username, base_dir)
    if overwrite and os.path.isdir(directory):
        logger.info("Overwriting existing data. Clearing directory: %s", directory)
        shutil.rmtree(directory)
    os.makedirs(directory, exist_ok=True)

    written = 0
    keep = set()
    for index, dive in enumerate(dives, 1):
        data = dive if isinstance(dive, dict) else dive.model_dump(mode="json")
        filename = _unified_dive_filename(data, service, index)
        keep.add(filename)
        with open(os.path.join(directory, filename), "w") as f:
            json.dump(data, f, indent=2)
        written += 1
    if prune:
        prune_cache_dir(directory, keep, service)
    return written


def download_service_dives(service: str, overwrite: bool = False, base_dir: Optional[str] = None,
                           username: Optional[str] = None) -> int:
    """Fetch every dive from a UnifiedDive-cached service and write the cache
    for it. The Garmin/Divelogs equivalent lives in
    SyncEngine.download_and_save_raw_data, which has to speak each of those
    APIs directly; here the adapter already hands back UnifiedDives, which is
    exactly what this cache stores.

    ``username`` (the desktop app, rework.md E19) picks the account and
    caches it in its own folder; the flat cache from before accounts is then
    removed, being re-downloaded per account rather than guessed at."""
    if not is_unified_cache(service):
        raise ValueError(f"{service!r} is not cached as UnifiedDive; use SyncEngine.download_and_save_raw_data.")
    adapter = _unified_adapter(service, username)
    if not adapter.login():
        raise RuntimeError(f"Failed to log in to {service}.")
    try:
        dives = adapter.fetch_dives()
    finally:
        adapter.finish()
    written = save_unified_dives(service, dives, username=username, base_dir=base_dir, overwrite=overwrite, prune=True)
    if username:
        _remove_flat_unified_cache(service, base_dir)
    logger.info("Cached %d %s dive(s)%s.", written, service, f" for {username}" if username else "")
    return written


def _remove_flat_unified_cache(service: str, base_dir: Optional[str] = None) -> None:
    directory = unified_cache_dir(service, None, base_dir)
    stale = [n for n in os.listdir(directory) if n.endswith(".json")] if os.path.isdir(directory) else []
    for name in stale:
        try:
            os.remove(os.path.join(directory, name))
        except OSError as e:
            logger.warning("Could not remove %s from the pre-account %s cache: %s", name, service, e)
    if stale:
        logger.info("Removed %d %s dive(s) cached before accounts were kept apart.", len(stale), service)


def _unified_row(data: Dict[str, Any], service: str, filename: str) -> Dict[str, Any]:
    """One cached UnifiedDive as the same row dict list_garmin_dives() and
    list_divelogs_dives() produce, so the dive table and editor need no
    per-service cases."""
    tanks = [
        {
            "oxygen": gas.get("oxygen"),
            "helium": gas.get("helium"),
            "start_pressure": gas.get("start_pressure"),
            "end_pressure": gas.get("end_pressure"),
            "volume": gas.get("tank_volume"),
            "tank_name": gas.get("tank_name"),
        }
        for gas in (data.get("gas_mixtures") or [])
    ]
    date_time = _normalize_date_time(data.get("date_time"))
    date_part, time_part = _split_date_time(date_time)
    weight = data.get("weight")
    visibility = data.get("visibility")
    return {
        "id": str((data.get("external_ids") or {}).get(service) or ""),
        "dive_number": data.get("dive_number") or "",
        "date_time": date_time,
        "date": date_part,
        "time": time_part,
        "duration": _seconds_to_minutes_display(data.get("duration") or 0),
        "max_depth": f"{float(data['max_depth']):.2f}" if data.get("max_depth") is not None else "",
        "avg_depth": f"{float(data['avg_depth']):.2f}" if data.get("avg_depth") is not None else "",
        "sac": _format_sac(tanks, data.get("avg_depth"), data.get("duration")),
        # a 0 °C cached before it counted as "not recorded" is shown as none
        "water_temp": _format_water_temp(*(recorded_water_temp(data.get(k)) for k in ("temp_min", "temp_max", "temp_avg"))),
        "water_temp_value": next((t for t in (recorded_water_temp(data.get("temp_avg")), recorded_water_temp(data.get("temp_min")))
                                  if t is not None), None),
        "tanks": _format_tanks(tanks),
        "tanks_detail": tanks,
        "tanks_editable": True,
        "lat": data.get("lat"),
        "lng": data.get("lng"),
        "location": data.get("location") or "",
        "notes": data.get("notes") or "",
        "weight": "" if weight is None else f"{_format_number(weight)} {_weight_unit(data)}".strip(),
        "visibility": "" if visibility is None else f"{_format_number(visibility)} {_visibility_unit(data)}".strip(),
        "buddy": data.get("buddy") or "",
        "filename": filename,
        # a recorded depth profile: its depths and duration follow from it
        # (Subsurface writes them only for hand-logged dives)
        "has_profile": bool(data.get("samples")),
    }


def _weight_unit(data: Dict[str, Any]) -> str:
    return "kg" if "kilogram" in str(data.get("weight_unit") or "kilogram").lower() else "lbs"


def _visibility_unit(data: Dict[str, Any]) -> str:
    return "m" if "meter" in str(data.get("visibility_unit") or "meter").lower() else "ft"


def list_unified_dives(service: str, username: Optional[str] = None,
                       base_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    dives: List[Dict[str, Any]] = []
    # Only the cache subdirectory, never the service directory itself - the
    # adapter's own state files live up there (see unified_cache_dir).
    service_dir = unified_cache_dir(service, username, base_dir)
    if not os.path.isdir(service_dir):
        return dives
    for filename, filepath in _iter_dive_files(service_dir):
        try:
            with open(filepath, "r") as f:
                dives.append(_unified_row(json.load(f), service, filename))
        except Exception as e:
            logger.warning("Failed to parse cached %s dive file %s: %s", service, filename, e)
    dives.sort(key=lambda x: x["date_time"], reverse=True)
    return dives


def list_dives(service: str, username: Optional[str] = None,
               base_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """The cached dive rows for any supported service."""
    if service == "garmin":
        return list_garmin_dives(username, base_dir)
    if service == "divelogs":
        return list_divelogs_dives(username, base_dir)
    if is_unified_cache(service):
        return list_unified_dives(service, username, base_dir)
    raise ValueError(f"Unknown service: {service!r}")


def _parse_unified_samples(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {"depth": s.get("depth"), "temp": s.get("temp"), "time": s.get("time")}
        for s in (raw.get("samples") or [])
        if s.get("depth") is not None
    ]


def read_raw_dive(service: str, filename: str, username: Optional[str] = None, base_dir: Optional[str] = None) -> Dict[str, Any]:
    filepath = _find_dive_file(service, filename, username, base_dir)
    if not filepath:
        raise FileNotFoundError(f"Dive file not found: {service}/{filename}")
    with open(filepath, "r") as f:
        return json.load(f)


def _parse_garmin_samples(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Depth/temperature profile points from the raw cache file's
    ``activityDetails`` (rework.md E1). A second, simplified read of the same
    shape GarminAdapter._map_to_unified parses for sync - kept separate
    rather than shared, same as every other field in this module: this path
    reads the local cache directly (no adapter/login involved), matching the
    module's own documented purpose."""
    activity_details = (raw or {}).get("activityDetails")
    if not isinstance(activity_details, dict):
        return []
    descriptors = activity_details.get("metricDescriptors") or []
    metrics_data = activity_details.get("activityDetailMetrics") or []
    if not isinstance(descriptors, list) or not isinstance(metrics_data, list):
        return []

    duration_idx = depth_idx = temp_idx = None
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

    samples = []
    for item in metrics_data:
        if not isinstance(item, dict):
            continue
        m_list = item.get("metrics")
        if not m_list or not isinstance(m_list, list):
            continue
        if depth_idx is None or depth_idx >= len(m_list):
            continue
        depth_val = m_list[depth_idx]
        if depth_val is None:
            continue
        time_sec = None
        if duration_idx is not None and duration_idx < len(m_list) and m_list[duration_idx] is not None:
            time_sec = int(round(float(m_list[duration_idx])))
        temp_c = None
        if temp_idx is not None and temp_idx < len(m_list) and m_list[temp_idx] is not None:
            temp_c = float(m_list[temp_idx])
        samples.append({"time": time_sec, "depth": float(depth_val), "temp": temp_c})
    return samples


def _parse_divelogs_samples(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Depth/temperature profile points from the raw cache file's
    ``sampledata`` (rework.md E1). Values are read as stored, with no
    imperial-to-metric conversion - same as every other numeric field this
    module reads from a Divelogs raw file (e.g. max_depth in
    list_divelogs_dives above); DivelogsAdapter.to_unified is the one place
    that knows the account's unit preference, and it isn't involved here."""
    sampledata = (raw or {}).get("sampledata")
    if not isinstance(sampledata, list):
        return []
    try:
        samplerate = int(raw.get("samplerate")) if raw.get("samplerate") not in (None, "") else 1
    except (TypeError, ValueError):
        samplerate = 1
    samplerate = samplerate if samplerate > 0 else 1

    samples = []
    for i, p in enumerate(sampledata):
        d_val = t_val = None
        if isinstance(p, dict):
            d_val, t_val = p.get("d"), p.get("t")
        elif isinstance(p, (int, float)):
            d_val = p
        if d_val is None:
            continue
        samples.append({"time": i * samplerate, "depth": float(d_val), "temp": float(t_val) if t_val is not None else None})
    return samples


def get_samples(service: str, filename: str, username: Optional[str] = None, base_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Depth/temperature profile for one cached dive, for the desktop
    editor's graph (rework.md E1). Empty list if the dive has none cached."""
    raw = read_raw_dive(service, filename, username, base_dir)
    if service == "garmin":
        return _parse_garmin_samples(raw)
    if is_unified_cache(service):
        return _parse_unified_samples(raw)
    return _parse_divelogs_samples(raw)


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
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    water_temp: Optional[float] = None,
    tanks: Optional[List[Dict[str, Any]]] = None,
    activity_name: Optional[str] = None,
    location_name: Optional[str] = None,
) -> str:
    """Apply the given (non-None) field overrides to the cached dive file and
    write it back. Returns the filepath that was written, for the caller to
    hand to push_remote_update(). An empty string clears a text field; a
    field left as None (the default) is left untouched.

    `duration` is in minutes (matching the dive list/edit form's display -
    see _seconds_to_minutes_display) and is converted to seconds here before
    being written, since that's what both services' raw JSON stores.

    `water_temp` sets minTemperature/maxTemperature/averageTemperature (Garmin)
    or depthtemp (Divelogs) all to the one entered value - both services can
    model a min/max/avg range, but the edit form only offers one number, so a
    manual edit here always collapses to a single reading.

    `tanks` (a list of {oxygen, helium, start_pressure, end_pressure, volume,
    tank_name} dicts, replacing the cached list wholesale) is a no-op for
    Garmin: its gas API is read-only (rework.md E4), so the desktop editor
    never offers tank editing for a Garmin-sourced dive in the first place.

    `activity_name` / `location_name` (Garmin only) set the activity's title
    and its location name separately; `location` sets both to one text, as it
    always has."""
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

        if lat is not None or lng is not None:
            summary.setdefault("summaryDTO", {})
            details.setdefault("summaryDTO", {})
            if lat is not None:
                summary["summaryDTO"]["startLatitude"] = lat
                details["summaryDTO"]["startLatitude"] = lat
            if lng is not None:
                summary["summaryDTO"]["startLongitude"] = lng
                details["summaryDTO"]["startLongitude"] = lng

        if water_temp is not None:
            summary.setdefault("summaryDTO", {})
            details.setdefault("summaryDTO", {})
            for key in ("minTemperature", "maxTemperature", "averageTemperature"):
                summary["summaryDTO"][key] = water_temp
                details["summaryDTO"][key] = water_temp

        # tanks: no-op here, Garmin's gas API is read-only (rework.md E4)

        if location is not None:
            summary["activityName"] = location
            details["activityName"] = location
            details["locationName"] = location
            summary["locationName"] = location

        if activity_name is not None:
            summary["activityName"] = activity_name
            details["activityName"] = activity_name

        if location_name is not None:
            summary["locationName"] = location_name or None
            details["locationName"] = location_name or None

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

        if lat is not None:
            dive_data["lat"] = lat
        if lng is not None:
            dive_data["lng"] = lng

        if water_temp is not None:
            dive_data["depthtemp"] = water_temp

        if tanks is not None:
            dive_data["tanks"] = [
                {
                    "o2": t.get("oxygen"),
                    "he": t.get("helium"),
                    "start_pressure": t.get("start_pressure"),
                    "end_pressure": t.get("end_pressure"),
                    "vol": t.get("volume"),
                    "tankname": t.get("tank_name"),
                }
                for t in tanks
            ]

    elif is_unified_cache(service):
        # The cache file is already a UnifiedDive, so each edit is a plain
        # assignment onto the unified field of the same name.
        if dive_number is not None:
            try:
                dive_data["dive_number"] = int(dive_number) if str(dive_number).strip() else None
            except (TypeError, ValueError):
                logger.warning("Ignoring non-numeric dive number %r for %s", dive_number, service)
        if date_time is not None:
            dive_data["date_time"] = date_time.replace(" ", "T") if date_time else None
        if duration is not None:
            dive_data["duration"] = duration
        if max_depth is not None:
            dive_data["max_depth"] = max_depth
        if location is not None:
            dive_data["location"] = location
        if notes is not None:
            dive_data["notes"] = notes
        if buddy is not None:
            dive_data["buddy"] = buddy
        if lat is not None:
            dive_data["lat"] = lat
        if lng is not None:
            dive_data["lng"] = lng
        if weight is not None:
            dive_data["weight"] = _leading_number(weight)
        if visibility is not None:
            dive_data["visibility"] = _leading_number(visibility)
        if water_temp is not None:
            # One entered number collapses the min/max/avg range, exactly as
            # it does for the other two services.
            dive_data["temp_min"] = dive_data["temp_max"] = dive_data["temp_avg"] = water_temp
        if tanks is not None:
            dive_data["gas_mixtures"] = [
                {
                    "oxygen": t.get("oxygen") if t.get("oxygen") is not None else 21.0,
                    "helium": t.get("helium") if t.get("helium") is not None else 0.0,
                    "start_pressure": t.get("start_pressure"),
                    "end_pressure": t.get("end_pressure"),
                    "tank_volume": t.get("volume"),
                    "tank_name": t.get("tank_name"),
                }
                for t in tanks
            ]

    else:
        raise ValueError(f"Unknown service: {service!r}")

    with open(filepath, "w") as f:
        json.dump(dive_data, f, indent=2)

    logger.info("Updated cached %s dive %s.", service, filename)
    return filepath


def _leading_number(text: Any) -> Optional[float]:
    """"6 kg" -> 6.0. The edit form shows weight/visibility with their unit
    appended, but a UnifiedDive keeps the number and the unit apart."""
    if text in (None, ""):
        return None
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)", str(text))
    return float(m.group(1)) if m else None


def dive_external_id(service: str, filename: str, username: Optional[str] = None,
                     base_dir: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """(filepath, the dive's id on its service) of a cached dive, reading
    only: what a staged delete needs to remove it online before the cache
    file goes."""
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
        elif is_unified_cache(service):
            external_id = (dive_data.get("external_ids") or {}).get(service)
    except Exception as e:
        logger.error("Failed to read dive file %s: %s", filename, e)
    return filepath, str(external_id) if external_id else None


def delete_dive_local(service: str, filename: str, username: Optional[str] = None, base_dir: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """Remove the cached dive file. Returns (filepath, external_id) - the
    external_id (Garmin activityId / Divelogs id), if one could be read
    before deletion, for the caller to pass to push_remote_delete()."""
    filepath, external_id = dive_external_id(service, filename, username, base_dir)
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


def _unified_adapter(service: str, username: Optional[str] = None):
    """The configured adapter for a UnifiedDive-cached service: the one
    Submersion store, or a Subsurface Cloud account - ``username`` is its
    email or its cache folder name (account_dir_name)."""
    from src.core.config import ConfigManager
    from src.core.pairs import build_adapter
    spec = "subsurface-cloud" if service == "subsurface" else service
    email = None
    if username and service == "subsurface":
        accounts = ConfigManager.load_credentials().get_subsurface_accounts()
        email = next((a.email for a in accounts if username in (a.email, account_dir_name(a.email))), username)
    return build_adapter(spec, ConfigManager.load_settings(), subsurface_username=email)


def download_garmin_fits(filenames: List[str], username: Optional[str] = None,
                         base_dir: Optional[str] = None) -> Dict[str, Any]:
    """Download the original .fit of each cached Garmin dive in
    ``filenames`` (their JSON cache names), saved under the same name as the
    JSON (see garmin_files). One login covers the lot. Returns
    ``{"downloaded": [...], "failed": {filename: reason}}``.

    A hand-logged dive lands in ``failed`` without a download: Connect
    answers for one with a FIT it generates from the typed-in summary (no
    profile, no samples), which is no use to an importer."""
    from src.core import progress
    from src.core.services.garmin import GarminAdapter

    result: Dict[str, Any] = {"downloaded": [], "failed": {}}
    # (filename, filepath, account) - resolved first, so a bad name costs no API call
    targets = []
    for filename in filenames:
        filepath = _find_dive_file("garmin", filename, username, base_dir)
        if not filepath:
            result["failed"][filename] = "not in the local cache"
            continue
        targets.append((filename, filepath, username or _username_from_filepath("garmin", filepath)))
    if not targets:
        return result

    adapters: Dict[Optional[str], Any] = {}
    for index, (filename, filepath, account) in enumerate(targets, 1):
        progress.report(index - 1, len(targets), f"FIT for {filename}", "garmin")
        try:
            with open(filepath, "r") as f:
                payload = json.load(f)
            stem = garmin_files.stem_for_payload(payload)
            activity_id = garmin_files.activity_id_of(stem) if stem else None
            if not activity_id:
                result["failed"][filename] = "no Garmin activity id"
                continue
            if garmin_files.is_manual_dive(payload):
                result["failed"][filename] = "hand-logged dive: Garmin has no dive-computer file for it"
                continue
            if account not in adapters:
                creds = _active_credentials("garmin", account)
                adapter = GarminAdapter(creds.username, creds.password, token_dir=creds.token_dir,
                                        cooldown_seconds=ConfigManager.load_settings().api_cooldown_seconds)
                adapters[account] = adapter if adapter.login() else None
            adapter = adapters[account]
            if adapter is None:
                result["failed"][filename] = "Garmin login failed"
                continue
            data = adapter.download_fit(activity_id)
            if not data:
                result["failed"][filename] = "Garmin has no original file for this dive (hand-logged?)"
                continue
            path = garmin_files.save_fit(data, stem, account, base_dir)
            logger.info("Saved FIT for Garmin activity %s: %s", activity_id, path)
            result["downloaded"].append(filename)
        except Exception as e:
            logger.error("Failed to download FIT for %s: %s", filename, e)
            result["failed"][filename] = str(e)
    progress.report(len(targets), len(targets), "FIT download finished", "garmin")
    return result


def push_remote_update(service: str, filepath: str, username: Optional[str] = None) -> bool:
    """Push a cached dive file's current contents to the remote service.
    Call after update_dive_fields() has written the file."""
    if is_unified_cache(service):
        from src.core.models import UnifiedDive
        with open(filepath, "r") as f:
            dive_data = json.load(f)
        external_id = (dive_data.get("external_ids") or {}).get(service)
        if not external_id:
            logger.error("No %s id found in cache for %s; cannot update remotely.", service, filepath)
            return False
        adapter = _unified_adapter(service, username or _username_from_filepath(service, filepath))
        if not adapter.login():
            logger.error("Failed to log in to %s; cannot update remotely.", service)
            return False
        logger.info("Updating %s for dive %s...", service, external_id)
        try:
            success = adapter.update_dive(str(external_id), UnifiedDive(**dive_data))
        finally:
            adapter.finish()
        if success:
            logger.info("%s successfully updated remotely.", service.capitalize())
        else:
            logger.error("%s remote update failed.", service.capitalize())
        return success

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
    if is_unified_cache(service):
        adapter = _unified_adapter(service, username or (_username_from_filepath(service, filepath) if filepath else None))
        if not adapter.login():
            logger.error("Failed to log in to %s; cannot delete remotely.", service)
            return False
        logger.info("Deleting %s remotely for ID %s...", service, external_id)
        try:
            return adapter.delete_dive(str(external_id))
        finally:
            adapter.finish()

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
