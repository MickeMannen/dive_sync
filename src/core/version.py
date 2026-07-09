import os
import time
import logging
import requests
from typing import Dict, Any

logger = logging.getLogger("dive_sync.version")

VERSION_CHECK_CACHE = {
    "last_checked": 0.0,
    "latest_version": None,
    "release_url": "https://github.com/mickemannen/dive_sync/releases/latest",
    "update_available": False
}

def parse_version(v_str: str):
    if not v_str:
        return (0, 0, 0)
    clean = v_str.lower().lstrip('v').strip()
    parts = clean.split('-')[0].split('.')
    try:
        return tuple(int(x) for x in parts if x.isdigit())
    except Exception:
        return (0, 0, 0)

def is_newer(current: str, latest: str) -> bool:
    if not current or current in ["unknown", "local-dev"]:
        return False
    return parse_version(latest) > parse_version(current)

def get_version_info() -> Dict[str, Any]:
    current_version = os.environ.get("APP_VERSION", "local-dev")
    current_time = time.time()
    
    # Check cache (1 hour)
    if VERSION_CHECK_CACHE["last_checked"] and (current_time - VERSION_CHECK_CACHE["last_checked"] < 3600):
        return {
            "current_version": current_version,
            "latest_version": VERSION_CHECK_CACHE["latest_version"],
            "release_url": VERSION_CHECK_CACHE["release_url"],
            "update_available": VERSION_CHECK_CACHE["update_available"]
        }
        
    latest_version = None
    update_available = False
    release_url = "https://github.com/mickemannen/dive_sync/releases/latest"
    
    try:
        headers = {"User-Agent": "dive-sync-app"}
        res = requests.get("https://api.github.com/repos/mickemannen/dive_sync/releases/latest", headers=headers, timeout=2)
        if res.status_code == 200:
            data = res.json()
            latest_version = data.get("tag_name")
            if latest_version:
                release_url = data.get("html_url", release_url)
                update_available = is_newer(current_version, latest_version)
                
                # Update cache
                VERSION_CHECK_CACHE["latest_version"] = latest_version
                VERSION_CHECK_CACHE["release_url"] = release_url
                VERSION_CHECK_CACHE["update_available"] = update_available
    except Exception as e:
        logger.debug("Failed to check for new version on GitHub: %s", e)
        
    VERSION_CHECK_CACHE["last_checked"] = current_time
    
    return {
        "current_version": current_version,
        "latest_version": latest_version or VERSION_CHECK_CACHE["latest_version"] or current_version,
        "release_url": release_url,
        "update_available": update_available
    }
