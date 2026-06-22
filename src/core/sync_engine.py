import os
import json
import logging
import time
from datetime import datetime, timedelta
from typing import List, Tuple, Dict, Any, Optional

from src.core.config import ConfigManager, SettingsModel, CredentialsModel
from src.core.services.garmin import GarminAdapter
from src.core.services.divelogs import DivelogsAdapter
from src.core.models import UnifiedDive

logger = logging.getLogger("anti_gravity.sync_engine")

STATE_FILE = "sync_state.json"

class SyncEngine:
    def __init__(self, settings_path: str = "settings.json", credentials_path: str = "credentials.json", mock_data_dir: Optional[str] = None):
        self.settings_path = settings_path
        self.credentials_path = credentials_path
        self.settings = ConfigManager.load_settings(settings_path)
        self.credentials = ConfigManager.load_credentials(credentials_path)
        
        # Instantiate adapters
        if mock_data_dir:
            from src.core.services.mock_adapters import LocalMockGarminAdapter, LocalMockDivelogsAdapter
            logger.info("Initializing SyncEngine in OFFLINE/MOCK mode using data from: %s", mock_data_dir)
            self.garmin = LocalMockGarminAdapter(mock_data_dir=mock_data_dir)
            self.divelogs = LocalMockDivelogsAdapter(mock_data_dir=mock_data_dir)
        else:
            self.garmin = GarminAdapter(
                username=self.credentials.garmin.username,
                password=self.credentials.garmin.password,
                token_dir=self.credentials.garmin.token_dir,
                cooldown_seconds=self.settings.api_cooldown_seconds
            )
            self.divelogs = DivelogsAdapter(
                username=self.credentials.divelogs.username,
                password=self.credentials.divelogs.password,
                cooldown_seconds=self.settings.api_cooldown_seconds
            )

    def load_last_sync_time(self) -> Optional[datetime]:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    data = json.load(f)
                    ts = data.get("last_sync_time")
                    if ts:
                        return datetime.fromisoformat(ts)
            except Exception as e:
                logger.warning("Failed to load sync state: %s", e)
        return None

    def save_last_sync_time(self, dt: datetime) -> None:
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({"last_sync_time": dt.isoformat()}, f)
            logger.info("Saved sync state with timestamp: %s", dt)
        except Exception as e:
            logger.error("Failed to save sync state: %s", e)

    def backup(self, garmin_backup_path: Optional[str] = None, divelogs_backup_path: Optional[str] = None) -> bool:
        """Run backup to local JSON files of all history from selected services."""
        logger.info("Starting backup process...")
        
        # Determine paths
        g_path = garmin_backup_path or "garmin_backup.json"
        d_path = divelogs_backup_path or "divelogs_backup.json"

        # Garmin backup
        if self.credentials.garmin.username:
            if self.garmin.login():
                logger.info("Fetching Garmin dives for backup...")
                g_dives = self.garmin.fetch_dives()
                # Optional FIT file extraction for backup
                if self.settings.sync_filters.sync_fit:
                    for d in g_dives:
                        g_id = d.external_ids.get("garmin")
                        if g_id:
                            fit_b64 = self.garmin.fetch_fit_file(g_id)
                            d.fit_file = fit_b64
                
                with open(g_path, "w") as f:
                    json.dump([d.model_dump(mode='json') for d in g_dives], f, indent=2)
                logger.info("Garmin backup saved to %s (%d dives)", g_path, len(g_dives))
            else:
                logger.error("Failed to log in to Garmin for backup.")
                return False
        
        # Divelogs backup
        if self.credentials.divelogs.username:
            if self.divelogs.login():
                logger.info("Fetching Divelogs dives for backup...")
                d_dives = self.divelogs.fetch_dives()
                with open(d_path, "w") as f:
                    json.dump([d.model_dump(mode='json') for d in d_dives], f, indent=2)
                logger.info("Divelogs backup saved to %s (%d dives)", d_path, len(d_dives))
            else:
                logger.error("Failed to log in to Divelogs for backup.")
                return False
        
        return True

    def run_sync(self, dry_run: bool = False, date_from_override: Optional[str] = None, date_to_override: Optional[str] = None, only_new_override: Optional[bool] = None) -> Dict[str, Any]:
        """Perform bidirectional or directional synchronization."""
        logger.info("Initializing Sync Run (Dry Run: %s)...", dry_run)
        
        # Reload settings to ensure we have the latest config
        self.settings = ConfigManager.load_settings(self.settings_path)
        
        # Apply command-line parameter overrides
        if date_from_override is not None:
            self.settings.sync_filters.date_from = date_from_override
        if date_to_override is not None:
            self.settings.sync_filters.date_to = date_to_override
        if only_new_override is not None:
            self.settings.sync_filters.only_new = only_new_override
        
        # Login
        if not self.garmin.login():
            raise RuntimeError("Failed to authenticate with Garmin Connect.")
        if not self.divelogs.login():
            raise RuntimeError("Failed to authenticate with Divelogs.org.")

        # Determine datetime filters
        date_from: Optional[datetime] = None
        date_to: Optional[datetime] = None

        if self.settings.sync_filters.date_from:
            try:
                date_from = datetime.strptime(self.settings.sync_filters.date_from, "%Y-%m-%d")
            except ValueError:
                logger.warning("Invalid sync_filters.date_from format. Use YYYY-MM-DD.")
        
        if self.settings.sync_filters.date_to:
            try:
                date_to = datetime.strptime(self.settings.sync_filters.date_to, "%Y-%m-%d")
            except ValueError:
                logger.warning("Invalid sync_filters.date_to format. Use YYYY-MM-DD.")

        # Handle "only new dives"
        if self.settings.sync_filters.only_new:
            last_sync = self.load_last_sync_time()
            if last_sync:
                # If we have a last sync time, filter to only new dives since then minus 1 day grace
                filter_start = last_sync - timedelta(days=1)
                if not date_from or filter_start > date_from:
                    date_from = filter_start
                    logger.info("Incremental Sync Active. Fetching dives starting from: %s", date_from)

        # Fetch dives
        garmin_dives = self.garmin.fetch_dives(date_from=date_from, date_to=date_to)
        divelogs_dives = self.divelogs.fetch_dives(date_from=date_from, date_to=date_to)

        # Match dives
        matched_pairs, unique_garmin, unique_divelogs = self.match_dives(garmin_dives, divelogs_dives)

        logger.info("Match results: %d matched pairs, %d only in Garmin, %d only in Divelogs",
                    len(matched_pairs), len(unique_garmin), len(unique_divelogs))

        sync_results = {
            "dry_run": dry_run,
            "directionality": self.settings.directionality,
            "matched_count": len(matched_pairs),
            "uploaded_to_divelogs": [],
            "uploaded_to_garmin": [],
            "updated_on_divelogs": [],
            "updated_on_garmin": [],
            "skipped": []
        }

        # Filter out gas mixtures if disabled in settings
        if not self.settings.sync_filters.sync_gases:
            for d in unique_garmin + unique_divelogs:
                d.gas_mixtures = []
            for g, d in matched_pairs:
                g.gas_mixtures = []
                d.gas_mixtures = []

        direction = self.settings.directionality

        # 1. Garmin -> Divelogs (if bidirectional or to_divelogs)
        if direction in ["bidirectional", "to_divelogs"]:
            for dive in unique_garmin:
                logger.info("Sync action: Upload Garmin dive at %s to Divelogs.org", dive.date_time)
                if not dry_run:
                    new_id = self.divelogs.add_dive(dive)
                    if new_id:
                        sync_results["uploaded_to_divelogs"].append({
                            "time": str(dive.date_time),
                            "new_divelogs_id": new_id,
                            "garmin_id": dive.external_ids.get("garmin")
                        })
                else:
                    sync_results["uploaded_to_divelogs"].append({
                        "time": str(dive.date_time),
                        "garmin_id": dive.external_ids.get("garmin"),
                        "dry_run": True
                    })

        # 2. Divelogs -> Garmin (if bidirectional or to_garmin)
        if direction in ["bidirectional", "to_garmin"]:
            for dive in unique_divelogs:
                logger.info("Sync action: Upload Divelogs dive at %s to Garmin Connect", dive.date_time)
                
                # Fetch FIT file if it exists/requested? Divelogs dives do not contain fit files,
                # but if we upload to Garmin, Garmin expects a Garmin JSON format
                if not dry_run:
                    new_id = self.garmin.add_dive(dive)
                    if new_id:
                        sync_results["uploaded_to_garmin"].append({
                            "time": str(dive.date_time),
                            "new_garmin_id": new_id,
                            "divelogs_id": dive.external_ids.get("divelogs")
                        })
                else:
                    sync_results["uploaded_to_garmin"].append({
                        "time": str(dive.date_time),
                        "divelogs_id": dive.external_ids.get("divelogs"),
                        "dry_run": True
                    })

        # 3. Synchronize cross-references for matched dives (linking IDs if missing)
        # That is, if Garmin is missing 'divelogs' external ID, or Divelogs is missing 'garmin' ID, we update them.
        for g_dive, d_dive in matched_pairs:
            g_id = g_dive.external_ids.get("garmin")
            d_id = d_dive.external_ids.get("divelogs")

            # Check if links need updating
            needs_garmin_update = False
            needs_divelogs_update = False

            if g_id and "divelogs" not in g_dive.external_ids:
                g_dive.external_ids["divelogs"] = d_id
                needs_garmin_update = True
            
            if d_id and "garmin" not in d_dive.external_ids:
                d_dive.external_ids["garmin"] = g_id
                needs_divelogs_update = True

            if needs_garmin_update:
                logger.info("Sync action: Link Divelogs ID %s in Garmin Activity ID %s", d_id, g_id)
                if not dry_run:
                    self.garmin.update_dive(g_id, g_dive)
                    sync_results["updated_on_garmin"].append({"id": g_id, "linked_divelogs": d_id})
                else:
                    sync_results["updated_on_garmin"].append({"id": g_id, "linked_divelogs": d_id, "dry_run": True})

            if needs_divelogs_update:
                logger.info("Sync action: Link Garmin ID %s in Divelogs Dive ID %s", g_id, d_id)
                if not dry_run:
                    self.divelogs.update_dive(d_id, d_dive)
                    sync_results["updated_on_divelogs"].append({"id": d_id, "linked_garmin": g_id})
                else:
                    sync_results["updated_on_divelogs"].append({"id": d_id, "linked_garmin": g_id, "dry_run": True})

        # Update last sync time if not dry run
        if not dry_run:
            self.save_last_sync_time(datetime.now())

        logger.info("Sync Completed.")
        return sync_results

    def match_dives(self, garmin_list: List[UnifiedDive], divelogs_list: List[UnifiedDive]) -> Tuple[List[Tuple[UnifiedDive, UnifiedDive]], List[UnifiedDive], List[UnifiedDive]]:
        matched_pairs: List[Tuple[UnifiedDive, UnifiedDive]] = []
        unique_garmin: List[UnifiedDive] = []
        unique_divelogs: List[UnifiedDive] = []

        grace_seconds = self.settings.grace_window_minutes * 60

        # Keep track of matched indices
        matched_divelogs_indices = set()

        for g_dive in garmin_list:
            match_found = False
            for idx, d_dive in enumerate(divelogs_list):
                if idx in matched_divelogs_indices:
                    continue
                
                # Compare local naive timestamps
                diff = abs((g_dive.date_time - d_dive.date_time).total_seconds())
                if diff <= grace_seconds:
                    matched_pairs.append((g_dive, d_dive))
                    matched_divelogs_indices.add(idx)
                    match_found = True
                    break
            
            if not match_found:
                unique_garmin.append(g_dive)

        for idx, d_dive in enumerate(divelogs_list):
            if idx not in matched_divelogs_indices:
                unique_divelogs.append(d_dive)

        return matched_pairs, unique_garmin, unique_divelogs

    def download_and_save_raw_data(self, mock_data_dir: str = "./tests", overwrite: bool = False) -> bool:
        """Download all raw data from Garmin and Divelogs and save to directory structure."""
        logger.info("Starting raw data download...")
        
        garmin_dir = os.path.join(mock_data_dir, "garmin")
        divelogs_dir = os.path.join(mock_data_dir, "divelogs")
        
        if overwrite:
            import shutil
            logger.info("Overwriting existing data. Clearing directories: %s and %s", garmin_dir, divelogs_dir)
            if os.path.exists(garmin_dir):
                shutil.rmtree(garmin_dir)
            if os.path.exists(divelogs_dir):
                shutil.rmtree(divelogs_dir)

        os.makedirs(garmin_dir, exist_ok=True)
        os.makedirs(divelogs_dir, exist_ok=True)
        
        # 1. Garmin raw data
        if self.credentials.garmin.username:
            logger.info("Authenticating with Garmin Connect...")
            if self.garmin.login():
                logger.info("Fetching Garmin dive activities list...")
                start = 0
                limit = 50
                all_dives: List[Dict[str, Any]] = []
                
                while True:
                    try:
                        logger.info(" Retrieving activity history from Garmin Connect (offset: %d)...", start)
                        response = self.garmin.client.get_activities(start, limit, activitytype="diving")
                        if not response:
                            break
                        
                        # Filter to diving activity type
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
                        time.sleep(self.garmin.cooldown_seconds)
                    except Exception as e:
                        logger.error("Failed to query activity list from Garmin Connect: %s", e)
                        return False
                
                total_dives = len(all_dives)
                logger.info("Found %d Garmin dive activities. Fetching details and saving raw JSONs...", total_dives)
                
                for index, activity in enumerate(all_dives, 1):
                    activity_id = activity.get("activityId")
                    if not activity_id:
                        continue
                    
                    logger.info(" [%d/%d] Fetching details for Garmin Activity ID: %s...", index, total_dives, activity_id)
                    try:
                        time.sleep(self.garmin.cooldown_seconds)
                        details = self.garmin.client.connectapi(f"/activity-service/activity/{activity_id}")
                        
                        # Fetch the activity details containing sensor telemetry graphs/charts over time
                        time.sleep(self.garmin.cooldown_seconds)
                        try:
                            logger.info(" Fetching telemetry/metrics details for Activity ID: %s...", activity_id)
                            activity_details = self.garmin.client.get_activity_details(activity_id)
                        except Exception as detail_err:
                            logger.warning("Failed to fetch activity details (telemetry) for %s: %s", activity_id, detail_err)
                            activity_details = None

                        # Fetch the tank sensor telemetry detail
                        time.sleep(self.garmin.cooldown_seconds)
                        try:
                            logger.info(" Fetching tank sensor telemetry for Activity ID: %s...", activity_id)
                            tanksensor = self.garmin.client.connectapi(
                                "/diving/v1/dive/detail/tanksensor",
                                params={"connectActivityId": activity_id}
                            )
                        except Exception as tank_err:
                            logger.warning("Failed to fetch tank sensor details for %s: %s", activity_id, tank_err)
                            tanksensor = None

                        metadata = details.get("metadataDTO", {}) or activity.get("metadataDTO", {}) or {}
                        dive_number = metadata.get("diveNumber")
                        
                        filename = f"{dive_number}.json" if (dive_number is not None and str(dive_number).isdigit()) else f"activity_{activity_id}.json"
                        filepath = os.path.join(garmin_dir, filename)
                        
                        raw_payload = {
                            "summary": activity,
                            "details": details,
                            "activityDetails": activity_details,
                            "tanksensor": tanksensor
                        }
                        
                        with open(filepath, "w") as f:
                            json.dump(raw_payload, f, indent=2)
                        logger.debug(" Saved %s", filepath)
                    except Exception as e:
                        logger.error("Failed to fetch/save details for Garmin activity %s: %s", activity_id, e)
                        return False
            else:
                logger.error("Failed to log in to Garmin Connect.")
                return False
        else:
            logger.warning("Garmin credentials not found, skipping Garmin download.")
        
        # 2. Divelogs raw data
        if self.credentials.divelogs.username:
            logger.info("Authenticating with Divelogs.org...")
            if self.divelogs.login():
                logger.info("Fetching detailed dive logs list from Divelogs.org...")
                try:
                    url = "https://divelogs.de/api/dives"
                    response = self.divelogs.session.get(url, timeout=20)
                    if response.status_code != 200:
                        logger.error("Failed to query Divelogs dives (status: %d)", response.status_code)
                        return False
                    
                    dives_list = response.json()
                    if not isinstance(dives_list, list):
                        logger.error("Invalid response format from Divelogs API.")
                        return False
                    
                    total_divelogs = len(dives_list)
                    logger.info("Retrieved %d dives from Divelogs.org. Saving raw JSONs...", total_divelogs)
                    
                    for index, item in enumerate(dives_list, 1):
                        dive_number = item.get("divenumber")
                        dive_id = item.get("id")
                        
                        filename = f"{dive_number}.json" if (dive_number is not None and str(dive_number).isdigit() and int(dive_number) > 0) else f"{dive_id}.json"
                        filepath = os.path.join(divelogs_dir, filename)
                        
                        with open(filepath, "w") as f:
                            json.dump(item, f, indent=2)
                        logger.debug(" Saved %s", filepath)
                        
                except Exception as e:
                    logger.error("Error fetching/saving Divelogs.org raw data: %s", e)
                    return False
            else:
                logger.error("Failed to log in to Divelogs.org.")
                return False
        else:
            logger.warning("Divelogs credentials not found, skipping Divelogs download.")
                
        logger.info("Raw data download completed. Files saved under %s", mock_data_dir)
        return True
