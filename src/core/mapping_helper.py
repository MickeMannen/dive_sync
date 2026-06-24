import os
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional, List
from src.core.models import UnifiedDive, GasMixture

logger = logging.getLogger("anti_gravity.mapping")

def resolve_jsonpath(obj: Any, path: str) -> Any:
    if not isinstance(path, str) or not path.startswith("$."):
        return None
    
    parts = path[2:].split(".")
    current = [obj]
    
    for part in parts:
        next_level = []
        is_list_wildcard = part.endswith("[*]")
        key = part[:-3] if is_list_wildcard else part
        
        for item in current:
            if isinstance(item, dict):
                if key in item:
                    val = item[key]
                    if is_list_wildcard and isinstance(val, list):
                        next_level.extend(val)
                    elif not is_list_wildcard:
                        next_level.append(val)
            elif isinstance(item, list):
                for subitem in item:
                    if isinstance(subitem, dict) and key in subitem:
                        val = subitem[key]
                        if is_list_wildcard and isinstance(val, list):
                            next_level.extend(val)
                        elif not is_list_wildcard:
                            next_level.append(val)
                            
        current = next_level
        if not current:
            break
            
    if not current:
        return None
    if "[*]" in path:
        return current
    return current[0] if current else None


def set_jsonpath(obj: Dict[str, Any], path: str, value: Any) -> None:
    if not isinstance(path, str) or not path.startswith("$."):
        return
    parts = path[2:].split(".")
    
    current = obj
    for part in parts[:-1]:
        is_list_wildcard = part.endswith("[*]")
        key = part[:-3] if is_list_wildcard else part
        
        if is_list_wildcard:
            if key not in current or not isinstance(current[key], list):
                current[key] = []
        else:
            if key not in current or not isinstance(current[key], dict):
                current[key] = {}
            current = current[key]
            
    last_part = parts[-1]
    is_list_wildcard = last_part.endswith("[*]")
    key = last_part[:-3] if is_list_wildcard else last_part
    
    if is_list_wildcard:
        if isinstance(value, list):
            current[key] = value
        else:
            current[key] = [value]
    else:
        current[key] = value


class MappingEngine:
    @staticmethod
    def apply_divelogs_to_garmin_mapping(data: Dict[str, Any], dive: UnifiedDive) -> None:
        """Apply mappings from divelogs_to_garmin.json to override UnifiedDive attributes from Divelogs payload."""
        path = os.path.join(os.path.dirname(__file__), "mapping", "divelogs_to_garmin.json")
        if not os.path.exists(path):
            return
        
        try:
            with open(path, "r") as f:
                mapping_def = json.load(f)
            
            mappings = mapping_def.get("mappings", [])
            for m in mappings:
                internal_field = m.get("internal_field")
                dive_log_path = m.get("dive_log_path")
                if not internal_field or not dive_log_path:
                    continue
                
                val = resolve_jsonpath(data, dive_log_path)
                if val is None:
                    continue
                
                if internal_field == "dive_number":
                    dive.dive_number = int(val) if str(val).isdigit() else val
                elif internal_field == "max_depth":
                    dive.max_depth = float(val)
                elif internal_field == "water_temperature":
                    dive.temp_min = float(val)
                elif internal_field == "tank_start_pressure":
                    if isinstance(val, list):
                        for idx, p in enumerate(val):
                            if idx < len(dive.gas_mixtures):
                                dive.gas_mixtures[idx].start_pressure = float(p) if p is not None else None
                            else:
                                dive.gas_mixtures.append(GasMixture(start_pressure=float(p) if p is not None else None))
                    else:
                        if dive.gas_mixtures:
                            dive.gas_mixtures[0].start_pressure = float(val) if val is not None else None
                        else:
                            dive.gas_mixtures.append(GasMixture(start_pressure=float(val) if val is not None else None))
        except Exception as e:
            logger.warning("Failed to apply divelogs_to_garmin mapping: %s", e)

    @staticmethod
    def apply_garmin_to_divelogs_mapping(merged_garmin: Dict[str, Any], dive: UnifiedDive) -> None:
        """Apply mappings from garmin_to_divelogs.json to override UnifiedDive attributes from Garmin payload."""
        path = os.path.join(os.path.dirname(__file__), "mapping", "garmin_to_divelogs.json")
        if not os.path.exists(path):
            return
            
        try:
            with open(path, "r") as f:
                mapping_def = json.load(f)
                
            mappings = mapping_def.get("mappings", [])
            for m in mappings:
                if "target_dive_log_field" in m and "source_garmin_path" in m:
                    target_field = m["target_dive_log_field"]
                    source_path = m["source_garmin_path"]
                    
                    val = resolve_jsonpath(merged_garmin, source_path)
                    if val is None:
                        continue
                        
                    if target_field == "divenumber":
                        dive.dive_number = int(val) if str(val).isdigit() else val
                    elif target_field == "maxdepth":
                        dive.max_depth = float(val)
                    elif target_field == "meandepth":
                        dive.avg_depth = float(val)
                    elif target_field == "depthtemp":
                        dive.temp_min = float(val)
                    elif target_field == "location":
                        dive.location = str(val)
                    elif target_field == "divesite":
                        if dive.location:
                            dive.location = f"{dive.location}, {val}"
                        else:
                            dive.location = str(val)
                    elif target_field == "duration":
                        dive.duration = int(val)
                    elif target_field == "weights":
                        dive.weight = float(val)
                    elif target_field == "weight_unit":
                        dive.weight_unit = str(val)
                    elif target_field == "visibility":
                        dive.visibility = float(val)
                    elif target_field == "visibility_unit":
                        dive.visibility_unit = str(val)
                elif "target_tank_fields" in m:
                    tank_fields = m["target_tank_fields"]
                    vol_path = tank_fields.get("vol")
                    sp_path = tank_fields.get("start_pressure")
                    ep_path = tank_fields.get("end_pressure")
                    o2_path = tank_fields.get("o2")
                    
                    vols = resolve_jsonpath(merged_garmin, vol_path) if vol_path else None
                    sps = resolve_jsonpath(merged_garmin, sp_path) if sp_path else None
                    eps = resolve_jsonpath(merged_garmin, ep_path) if ep_path else None
                    o2s = resolve_jsonpath(merged_garmin, o2_path) if o2_path else None
                    
                    list_len = max(
                        len(vols) if isinstance(vols, list) else 0,
                        len(sps) if isinstance(sps, list) else 0,
                        len(eps) if isinstance(eps, list) else 0,
                        len(o2s) if isinstance(o2s, list) else 0
                    )
                    
                    if list_len > 0:
                        dive.gas_mixtures = []
                        for i in range(list_len):
                            vol = vols[i] if isinstance(vols, list) and i < len(vols) else vols
                            sp = sps[i] if isinstance(sps, list) and i < len(sps) else sps
                            ep = eps[i] if isinstance(eps, list) and i < len(eps) else eps
                            o2 = o2s[i] if isinstance(o2s, list) and i < len(o2s) else o2s
                            
                            dive.gas_mixtures.append(
                                GasMixture(
                                    oxygen=float(o2) if o2 is not None else 21.0,
                                    start_pressure=float(sp) if sp is not None else None,
                                    end_pressure=float(ep) if ep is not None else None,
                                    tank_volume=float(vol) if vol is not None else None
                                )
                            )
        except Exception as e:
            logger.warning("Failed to apply garmin_to_divelogs mapping: %s", e)
