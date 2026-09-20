import os
import json
import logging
import time
from datetime import datetime, timedelta
from typing import List, Tuple, Dict, Any, Optional, Set

from src.core.config import ConfigManager, SettingsModel, CredentialsModel, GarminCredentials, DivelogsCredentials
from src.core.adapter import BaseDiveAdapter
from src.core.fields import (
    FieldLink,
    FieldSpec,
    are_gas_mixtures_different,  # noqa: F401  (re-exported; older code imported it from here)
    build_catalog,
    convert_value,
    copy_value,
    get_field,
    is_empty,
    match_key_equal,
    set_field,
    validate_field_links,
    values_equal,
)
from src.core.models import UnifiedDive, GasMixture

logger = logging.getLogger("dive_sync.sync_engine")

STATE_FILE = "sync_state.json"

# Global directionality values that are not tied to a service name.
_GENERIC_DIRECTIONS = {"bidirectional", "to_target", "to_source"}


class SyncEngine:
    """Synchronises dives between two adapters, ``source`` (side A) and
    ``target`` (side B).

    The pair defaults to Garmin Connect -> Divelogs.org, built from the
    credentials file (or from local mock data when ``mock_data_dir`` is
    given). Any two ``BaseDiveAdapter`` instances can be passed instead via
    ``source_adapter`` / ``target_adapter``; each side is then addressed by
    its ``service_id`` in ``external_ids``, result keys
    (``uploaded_to_<id>``, ``updated_on_<id>``) and catalogue keys.
    ``engine.garmin`` / ``engine.divelogs`` remain as aliases for whichever
    side carries that service id.

    What happens on a matched pair is driven entirely by the settings'
    ``field_links`` (see ``src/core/fields.py``): every active link reads its
    source and target fields, compares them, and if they differ decides who
    wins from the global ``directionality`` (which sides may be written at
    all) and the link's ``conflict`` policy."""

    def __init__(self, settings_path: Optional[str] = None, credentials_path: Optional[str] = None,
                 mock_data_dir: Optional[str] = None, garmin_username: Optional[str] = None,
                 divelogs_username: Optional[str] = None, *,
                 source_adapter: Optional[BaseDiveAdapter] = None,
                 target_adapter: Optional[BaseDiveAdapter] = None):
        from src.core.config import SETTINGS_FILE, CREDENTIALS_FILE
        self.settings_path = settings_path or SETTINGS_FILE
        self.credentials_path = credentials_path or CREDENTIALS_FILE
        self.settings = ConfigManager.load_settings(self.settings_path)
        self.credentials = ConfigManager.load_credentials(self.credentials_path)

        self._garmin_username_override = garmin_username
        self._divelogs_username_override = divelogs_username

        state_dir = os.path.dirname(self.settings_path) or "."

        if source_adapter is not None or target_adapter is not None:
            if source_adapter is None or target_adapter is None:
                raise ValueError("Both source_adapter and target_adapter are required for a custom pair.")
            self.source: BaseDiveAdapter = source_adapter
            self.target: BaseDiveAdapter = target_adapter
            self.source_id = self._require_service_id(self.source)
            self.target_id = self._require_service_id(self.target)
            if (self.source_id, self.target_id) == ("garmin", "divelogs"):
                self.state_file = os.path.join(state_dir, STATE_FILE)
            else:
                self.state_file = os.path.join(state_dir, f"sync_state_{self.source_id}_{self.target_id}.json")
        elif mock_data_dir:
            # If subdirectories with username exist under mock_data_dir, use segmented state file
            g_path = os.path.join(mock_data_dir, "garmin", self.garmin_username) if self.garmin_username else None
            if g_path and os.path.exists(g_path):
                self.state_file = os.path.join(mock_data_dir, f"sync_state_{self.garmin_username}_{self.divelogs_username}.json")
            else:
                self.state_file = os.path.join(mock_data_dir, STATE_FILE)

            from src.core.services.mock_adapters import LocalMockGarminAdapter, LocalMockDivelogsAdapter
            logger.info("Initializing SyncEngine in OFFLINE/MOCK mode using data from: %s", mock_data_dir)
            self.source = LocalMockGarminAdapter(mock_data_dir=mock_data_dir, username=self.garmin_username)
            self.target = LocalMockDivelogsAdapter(mock_data_dir=mock_data_dir, username=self.divelogs_username)
            self.source_id = self._require_service_id(self.source)
            self.target_id = self._require_service_id(self.target)
        else:
            from src.core.services.garmin import GarminAdapter
            from src.core.services.divelogs import DivelogsAdapter
            if self.garmin_username and self.divelogs_username and (len(self.credentials.get_garmin_accounts()) > 1 or len(self.credentials.get_divelogs_accounts()) > 1):
                self.state_file = os.path.join(state_dir, f"sync_state_{self.garmin_username}_{self.divelogs_username}.json")
            else:
                self.state_file = os.path.join(state_dir, STATE_FILE)

            token_dir = self.garmin_creds.token_dir
            if not os.path.isabs(token_dir):
                token_dir = os.path.join(os.environ.get("DATA_DIR", "."), token_dir)

            self.source = GarminAdapter(
                username=self.garmin_creds.username,
                password=self.garmin_creds.password,
                token_dir=token_dir,
                cooldown_seconds=self.settings.api_cooldown_seconds
            )
            self.target = DivelogsAdapter(
                username=self.divelogs_creds.username,
                password=self.divelogs_creds.password,
                cooldown_seconds=self.settings.api_cooldown_seconds
            )
            self.source_id = self._require_service_id(self.source)
            self.target_id = self._require_service_id(self.target)

        if self.source_id == self.target_id:
            raise ValueError(f"Cannot sync a service with itself ({self.source_id}).")

        # Names and catalogue are captured now so that tests (and callers)
        # may later swap in duck-typed adapters without service metadata.
        self.source_name = getattr(self.source, "display_name", "") or self.source_id
        self.target_name = getattr(self.target, "display_name", "") or self.target_id
        self._slots = {self.source_id: "source", self.target_id: "target"}
        self.catalog: Dict[str, FieldSpec] = build_catalog(self.source.field_catalog(), self.target.field_catalog())

    # ------------------------------------------------------------------
    # Pair / adapter access
    # ------------------------------------------------------------------

    @staticmethod
    def _require_service_id(adapter: Any) -> str:
        service_id = getattr(adapter, "service_id", "")
        if not service_id:
            raise ValueError(f"Adapter {type(adapter).__name__} declares no service_id.")
        return service_id

    def adapter_for(self, service_id: str) -> BaseDiveAdapter:
        """The adapter on the side that carries ``service_id``."""
        slot = self._slots.get(service_id)
        if slot is None:
            raise AttributeError(f"This engine syncs {self.source_id} <-> {self.target_id}; it has no '{service_id}' side.")
        return getattr(self, slot)

    def _set_adapter_for(self, service_id: str, adapter: Any) -> None:
        slot = self._slots.get(service_id)
        if slot is None:
            raise AttributeError(f"This engine syncs {self.source_id} <-> {self.target_id}; it has no '{service_id}' side.")
        setattr(self, slot, adapter)

    @property
    def garmin(self) -> BaseDiveAdapter:
        """Alias for the side that is Garmin Connect (kept for callers and tests)."""
        return self.adapter_for("garmin")

    @garmin.setter
    def garmin(self, adapter: Any) -> None:
        self._set_adapter_for("garmin", adapter)

    @property
    def divelogs(self) -> BaseDiveAdapter:
        """Alias for the side that is Divelogs.org (kept for callers and tests)."""
        return self.adapter_for("divelogs")

    @divelogs.setter
    def divelogs(self, adapter: Any) -> None:
        self._set_adapter_for("divelogs", adapter)

    @property
    def garmin_creds(self) -> GarminCredentials:
        garmin_accounts = self.credentials.get_garmin_accounts()
        if not garmin_accounts:
            return GarminCredentials()
        if len(garmin_accounts) == 1:
            return garmin_accounts[0]
        username = self._garmin_username_override
        if not username:
            raise ValueError("Multiple Garmin accounts configured. Please specify --garmin username.")
        matching = [a for a in garmin_accounts if a.username == username]
        if not matching:
            raise ValueError(f"No Garmin account configured matching username: {username}")
        return matching[0]

    @property
    def divelogs_creds(self) -> DivelogsCredentials:
        divelogs_accounts = self.credentials.get_divelogs_accounts()
        if not divelogs_accounts:
            return DivelogsCredentials()
        if len(divelogs_accounts) == 1:
            return divelogs_accounts[0]
        username = self._divelogs_username_override
        if not username:
            raise ValueError("Multiple Divelogs accounts configured. Please specify --divelogs username.")
        matching = [a for a in divelogs_accounts if a.username == username]
        if not matching:
            raise ValueError(f"No Divelogs account configured matching username: {username}")
        return matching[0]

    @property
    def garmin_username(self) -> str:
        return self.garmin_creds.username

    @property
    def divelogs_username(self) -> str:
        return self.divelogs_creds.username

    @property
    def garmin_dir_name(self) -> str:
        if self.garmin_username:
            return os.path.join("garmin", self.garmin_username)
        return "garmin"

    @property
    def divelogs_dir_name(self) -> str:
        if self.divelogs_username:
            return os.path.join("divelogs", self.divelogs_username)
        return "divelogs"

    # ------------------------------------------------------------------
    # Sync state
    # ------------------------------------------------------------------

    def load_last_sync_time(self) -> Optional[datetime]:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                    ts = data.get("last_sync_time")
                    if ts:
                        return datetime.fromisoformat(ts)
            except Exception as e:
                logger.warning("Failed to load sync state: %s", e)
        return None

    def save_last_sync_time(self, dt: datetime) -> None:
        try:
            with open(self.state_file, "w") as f:
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

    # ------------------------------------------------------------------
    # Field links
    # ------------------------------------------------------------------

    def writable_sides(self) -> Set[str]:
        """Service ids the current ``directionality`` allows writing to.
        ``to_<service_id>`` (``to_divelogs``, ``to_garmin``) names the side;
        ``to_target`` / ``to_source`` are the pair-neutral spellings."""
        direction = self.settings.directionality
        if direction == "bidirectional":
            return {self.source_id, self.target_id}
        if direction in ("to_target", f"to_{self.target_id}"):
            return {self.target_id}
        if direction in ("to_source", f"to_{self.source_id}"):
            return {self.source_id}
        logger.warning(
            "Unknown directionality %r for pair %s -> %s (expected bidirectional, to_%s or to_%s); nothing will be written.",
            direction, self.source_id, self.target_id, self.source_id, self.target_id,
        )
        return set()

    def _checked_links(self) -> List[Tuple[FieldLink, FieldSpec, FieldSpec]]:
        """The settings' links that are valid against this pair's catalogue,
        each with its resolved (first) source spec and target spec. Invalid
        links are logged and skipped so one bad edit cannot stop a sync."""
        links = list(self.settings.field_links)
        problems = validate_field_links(links, self.catalog)
        bad_ids = set()
        for problem in problems:
            logger.warning("Ignoring field link: %s", problem)
            if problem.startswith("Link '"):
                bad_ids.add(problem.split("'", 2)[1])
        resolved = []
        for link in links:
            if link.id in bad_ids:
                continue
            resolved.append((link, self.catalog[link.source[0]], self.catalog[link.target]))
        return resolved

    def active_links(self) -> List[Tuple[FieldLink, FieldSpec, FieldSpec]]:
        """Links applied to matched dives this run: valid, not off, not
        composite (templates arrive with C18) and not a tanks link while
        ``sync_gases`` is off."""
        active = []
        for link, src_spec, tgt_spec in self._checked_links():
            if link.direction == "off":
                continue
            if link.is_composite:
                logger.warning("Link '%s' is a composite; templates are not rendered yet, skipping.", link.id)
                continue
            if not self.settings.sync_filters.sync_gases and "tanks" in (src_spec.type, tgt_spec.type):
                continue
            active.append((link, src_spec, tgt_spec))
        return active

    def match_key_links(self) -> List[Tuple[FieldLink, FieldSpec, FieldSpec]]:
        """Links flagged as match keys, in ``match_order``, resolved so the
        first spec is on this engine's source side and the second on its
        target side (a link may be drawn in either direction)."""
        keyed = []
        for link, src_spec, tgt_spec in self._checked_links():
            if link.match_order is None:
                continue
            if src_spec.service_id == self.source_id and tgt_spec.service_id == self.target_id:
                keyed.append((link.match_order, link, src_spec, tgt_spec))
            elif src_spec.service_id == self.target_id and tgt_spec.service_id == self.source_id:
                keyed.append((link.match_order, link, tgt_spec, src_spec))
        keyed.sort(key=lambda item: item[0])
        return [(link, a_spec, b_spec) for _, link, a_spec, b_spec in keyed]

    def _dive_for(self, service_id: str, a_dive: UnifiedDive, b_dive: UnifiedDive) -> UnifiedDive:
        return a_dive if service_id == self.source_id else b_dive

    @staticmethod
    def _brief(field_type: str, value: Any) -> Any:
        if field_type in ("samples", "tanks", "list") and value is not None:
            return f"{len(value)} items"
        return value

    def _apply_link(self, link: FieldLink, src_spec: FieldSpec, tgt_spec: FieldSpec,
                    a_dive: UnifiedDive, b_dive: UnifiedDive, writable: Set[str]) -> Optional[str]:
        """Apply one link to one matched pair in memory. Returns the service id
        whose dive was modified, or None.

        Rules: if the two values already agree, nothing happens. Otherwise the
        sides this link *and* the global directionality allow writing decide:
        with exactly one writable side the other side is the origin and is
        copied over (``prefer_non_empty`` / ``manual`` copy only into a blank
        field); with both sides writable the link's ``conflict`` policy picks
        the winner (``source_wins`` / ``target_wins`` always copy, the other
        two only fill blanks and leave a real conflict alone)."""
        src_dive = self._dive_for(src_spec.service_id, a_dive, b_dive)
        tgt_dive = self._dive_for(tgt_spec.service_id, a_dive, b_dive)
        src_val = get_field(src_dive, src_spec)
        tgt_val = get_field(tgt_dive, tgt_spec)
        src_as_tgt = convert_value(src_val, src_spec.type, tgt_spec.type, link.separator)
        if values_equal(tgt_spec.type, src_as_tgt, tgt_val):
            return None

        can_write_tgt = link.direction in ("bidirectional", "to_target") and tgt_spec.service_id in writable
        can_write_src = link.direction in ("bidirectional", "to_source") and src_spec.service_id in writable
        if not (can_write_tgt or can_write_src):
            return None

        src_empty = is_empty(src_spec.type, src_val)
        tgt_empty = is_empty(tgt_spec.type, tgt_val)
        policy = link.conflict
        fill_only = policy in ("prefer_non_empty", "manual")

        winner: Optional[str]
        if can_write_tgt and can_write_src:
            if policy == "source_wins":
                winner = "source"
            elif policy == "target_wins":
                winner = "target"
            elif tgt_empty and not src_empty:
                winner = "source"
            elif src_empty and not tgt_empty:
                winner = "target"
            else:
                winner = None
        else:
            origin = "source" if can_write_tgt else "target"
            if not fill_only:
                winner = origin
            else:
                dest_empty = tgt_empty if origin == "source" else src_empty
                origin_empty = src_empty if origin == "source" else tgt_empty
                winner = origin if (dest_empty and not origin_empty) else None

        src_shown = self._brief(src_spec.type, src_val)
        tgt_shown = self._brief(tgt_spec.type, tgt_val)
        if winner is None:
            if policy == "manual" and not src_empty and not tgt_empty:
                # C4 will record this to conflicts.json; until then it is only logged.
                logger.warning("  %s: conflict left for manual resolution (%s=%r, %s=%r) [link %s]",
                               tgt_spec.label, src_spec.key, src_shown, tgt_spec.key, tgt_shown, link.id)
            else:
                logger.info("  %s differs (%s=%r, %s=%r) -> kept, %s [link %s]",
                            tgt_spec.label, src_spec.key, src_shown, tgt_spec.key, tgt_shown, policy, link.id)
            return None

        if winner == "source":
            logger.info("  %s differs (%s=%r, %s=%r) -> %s := %s [link %s, %s]",
                        tgt_spec.label, src_spec.key, src_shown, tgt_spec.key, tgt_shown,
                        tgt_spec.key, src_spec.key, link.id, policy)
            set_field(tgt_dive, tgt_spec, copy_value(tgt_spec.type, src_as_tgt))
            return tgt_spec.service_id

        tgt_as_src = convert_value(tgt_val, tgt_spec.type, src_spec.type, link.separator)
        logger.info("  %s differs (%s=%r, %s=%r) -> %s := %s [link %s, %s]",
                    src_spec.label, src_spec.key, src_shown, tgt_spec.key, tgt_shown,
                    src_spec.key, tgt_spec.key, link.id, policy)
        set_field(src_dive, src_spec, copy_value(src_spec.type, tgt_as_src))
        return src_spec.service_id

    # ------------------------------------------------------------------
    # Sync run
    # ------------------------------------------------------------------

    def run_sync(self, dry_run: bool = False, date_from_override: Optional[str] = None,
                 date_to_override: Optional[str] = None, only_new_override: Optional[bool] = None,
                 direction_override: Optional[str] = None, sync_gases_override: Optional[bool] = None,
                 sync_fit_override: Optional[bool] = None,
                 field_links_override: Optional[List[FieldLink]] = None) -> Dict[str, Any]:
        """Perform bidirectional or directional synchronization.

        Settings are re-read from disk at the start of every run; per-run
        overrides (CLI flags, cron-job fields) are passed in explicitly so
        they survive that reload."""
        logger.info("Initializing Sync Run (Dry Run: %s)...", dry_run)

        # Reload settings to ensure we have the latest config
        self.settings = ConfigManager.load_settings(self.settings_path)

        # Apply command-line / job parameter overrides
        if date_from_override is not None:
            self.settings.sync_filters.date_from = date_from_override
        if date_to_override is not None:
            self.settings.sync_filters.date_to = date_to_override
        if only_new_override is not None:
            self.settings.sync_filters.only_new = only_new_override
        if direction_override is not None:
            self.settings.directionality = direction_override
        if sync_gases_override is not None:
            self.settings.sync_filters.sync_gases = sync_gases_override
        if sync_fit_override is not None:
            self.settings.sync_filters.sync_fit = sync_fit_override
        if field_links_override is not None:
            self.settings.field_links = list(field_links_override)

        # Login
        if not self.source.login():
            raise RuntimeError(f"Failed to authenticate with {self.source_name}.")
        if not self.target.login():
            raise RuntimeError(f"Failed to authenticate with {self.target_name}.")

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
        source_dives = self.source.fetch_dives(date_from=date_from, date_to=date_to)
        target_dives = self.target.fetch_dives(date_from=date_from, date_to=date_to)

        # Match dives
        matched_pairs, unique_source, unique_target = self.match_dives(source_dives, target_dives)

        logger.info("Match results: %d matched pairs, %d only in %s, %d only in %s",
                    len(matched_pairs), len(unique_source), self.source_name, len(unique_target), self.target_name)

        src, tgt = self.source_id, self.target_id
        sync_results: Dict[str, Any] = {
            "dry_run": dry_run,
            "directionality": self.settings.directionality,
            "source": src,
            "target": tgt,
            "matched_count": len(matched_pairs),
            f"uploaded_to_{tgt}": [],
            f"uploaded_to_{src}": [],
            f"updated_on_{tgt}": [],
            f"updated_on_{src}": [],
            "skipped": []
        }

        # Filter out gas mixtures if disabled in settings
        if not self.settings.sync_filters.sync_gases:
            for d in unique_source + unique_target:
                d.gas_mixtures = []
            for a, b in matched_pairs:
                a.gas_mixtures = []
                b.gas_mixtures = []

        writable = self.writable_sides()

        # 1. Upload dives only the source has to the target
        if tgt in writable:
            for dive in unique_source:
                logger.info("Sync action: Upload %s dive at %s to %s", self.source_name, dive.date_time, self.target_name)
                entry = {
                    "time": str(dive.date_time),
                    f"{src}_id": dive.external_ids.get(src),
                    "source_id": dive.external_ids.get(src),
                }
                if not dry_run:
                    new_id = self.target.add_dive(dive)
                    if new_id:
                        entry[f"new_{tgt}_id"] = new_id
                        entry["new_id"] = new_id
                        sync_results[f"uploaded_to_{tgt}"].append(entry)
                else:
                    entry["dry_run"] = True
                    sync_results[f"uploaded_to_{tgt}"].append(entry)

        # 2. Upload dives only the target has to the source
        if src in writable:
            for dive in unique_target:
                logger.info("Sync action: Upload %s dive at %s to %s", self.target_name, dive.date_time, self.source_name)
                entry = {
                    "time": str(dive.date_time),
                    f"{tgt}_id": dive.external_ids.get(tgt),
                    "source_id": dive.external_ids.get(tgt),
                }
                if not dry_run:
                    new_id = self.source.add_dive(dive)
                    if new_id:
                        entry[f"new_{src}_id"] = new_id
                        entry["new_id"] = new_id
                        sync_results[f"uploaded_to_{src}"].append(entry)
                else:
                    entry["dry_run"] = True
                    sync_results[f"uploaded_to_{src}"].append(entry)

        # 3. Matched dives: cross-link IDs, then apply every active field link
        links = self.active_links()
        for a_dive, b_dive in matched_pairs:
            a_id = a_dive.external_ids.get(src)
            b_id = b_dive.external_ids.get(tgt)

            needs_update = {src: False, tgt: False}
            is_linking = {src: False, tgt: False}

            if a_id and tgt not in a_dive.external_ids:
                a_dive.external_ids[tgt] = b_id
                needs_update[src] = True
                is_linking[src] = True

            if b_id and src not in b_dive.external_ids:
                b_dive.external_ids[src] = a_id
                needs_update[tgt] = True
                is_linking[tgt] = True

            for link, src_spec, tgt_spec in links:
                modified = self._apply_link(link, src_spec, tgt_spec, a_dive, b_dive, writable)
                if modified:
                    needs_update[modified] = True

            sides = (
                (src, self.source, a_dive, a_id, tgt, b_id, self.source_name, self.target_name),
                (tgt, self.target, b_dive, b_id, src, a_id, self.target_name, self.source_name),
            )
            for sid, adapter, dive, ext_id, other_sid, other_id, name, other_name in sides:
                if not (needs_update[sid] and sid in writable):
                    continue
                if is_linking[sid]:
                    logger.info("Sync action: Link %s ID %s and update fields in %s ID %s", other_name, other_id, name, ext_id)
                else:
                    logger.info("Sync action: Update fields in %s ID %s from %s", name, ext_id, other_name)

                entry = {"id": ext_id, f"linked_{other_sid}": other_id, "linked_id": other_id, "time": str(dive.date_time)}
                if not dry_run:
                    adapter.update_dive(ext_id, dive)
                    sync_results[f"updated_on_{sid}"].append(entry)
                else:
                    entry["dry_run"] = True
                    sync_results[f"updated_on_{sid}"].append(entry)

        # Update last sync time if not dry run
        if not dry_run:
            self.save_last_sync_time(datetime.now())

        logger.info("Sync Completed.")
        return sync_results

    def match_dives(self, source_list: List[UnifiedDive], target_list: List[UnifiedDive]) -> Tuple[List[Tuple[UnifiedDive, UnifiedDive]], List[UnifiedDive], List[UnifiedDive]]:
        """Pair up dives from the two sides.

        Tier 1: explicit external-ID links (each side storing the other's id).
        Tier 2: the links flagged as match keys on the board, in ``match_order``
                (the default board flags the dive-number link).
        Tier 3: start times within ``grace_window_minutes`` of each other."""
        matched_pairs: List[Tuple[UnifiedDive, UnifiedDive]] = []
        unique_source: List[UnifiedDive] = []
        unique_target: List[UnifiedDive] = []

        grace_seconds = self.settings.grace_window_minutes * 60
        src, tgt = self.source_id, self.target_id
        match_keys = self.match_key_links()

        # Keep track of matched indices
        matched_target_indices = set()

        for a_dive in source_list:
            match_found = False
            a_id = a_dive.external_ids.get(src)
            a_link_id = a_dive.external_ids.get(tgt)

            for idx, b_dive in enumerate(target_list):
                if idx in matched_target_indices:
                    continue

                b_id = b_dive.external_ids.get(tgt)
                b_link_id = b_dive.external_ids.get(src)

                # Tier 1: Match by explicit external ID links
                id_matched = False
                if a_link_id and b_id and str(a_link_id).strip() == str(b_id).strip():
                    id_matched = True
                elif a_id and b_link_id and str(a_id).strip() == str(b_link_id).strip():
                    id_matched = True

                # Tier 2: Match keys from the board
                key_matched = False
                if not id_matched:
                    for _link, a_spec, b_spec in match_keys:
                        if match_key_equal(a_spec.type, get_field(a_dive, a_spec), get_field(b_dive, b_spec)):
                            key_matched = True
                            break

                # Tier 3: Match by local naive timestamps (grace window)
                timestamp_matched = False
                if not id_matched and not key_matched:
                    diff = abs((a_dive.date_time - b_dive.date_time).total_seconds())
                    if diff <= grace_seconds:
                        timestamp_matched = True

                if id_matched or key_matched or timestamp_matched:
                    matched_pairs.append((a_dive, b_dive))
                    matched_target_indices.add(idx)
                    match_found = True
                    break

            if not match_found:
                unique_source.append(a_dive)

        for idx, b_dive in enumerate(target_list):
            if idx not in matched_target_indices:
                unique_target.append(b_dive)

        return matched_pairs, unique_source, unique_target

    def download_and_save_raw_data(
        self,
        mock_data_dir: str = "./data",
        overwrite: bool = False,
        include_garmin: bool = True,
        include_divelogs: bool = True,
    ) -> bool:
        """Download raw data from Garmin and/or Divelogs and save to directory
        structure. include_garmin/include_divelogs let a caller scope this to
        just one service (e.g. a per-tab "Refresh" that shouldn't also hit
        the other service's API)."""
        logger.info("Starting raw data download (garmin=%s, divelogs=%s)...", include_garmin, include_divelogs)

        garmin_dir = os.path.join(mock_data_dir, self.garmin_dir_name)
        divelogs_dir = os.path.join(mock_data_dir, self.divelogs_dir_name)

        if overwrite:
            import shutil
            if include_garmin and os.path.exists(garmin_dir):
                logger.info("Overwriting existing data. Clearing directory: %s", garmin_dir)
                shutil.rmtree(garmin_dir)
            if include_divelogs and os.path.exists(divelogs_dir):
                logger.info("Overwriting existing data. Clearing directory: %s", divelogs_dir)
                shutil.rmtree(divelogs_dir)

        if include_garmin:
            os.makedirs(garmin_dir, exist_ok=True)
        if include_divelogs:
            os.makedirs(divelogs_dir, exist_ok=True)

        # 1. Divelogs raw data
        if include_divelogs and self.divelogs_username:
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
        elif include_divelogs:
            logger.warning("Divelogs credentials not found, skipping Divelogs download.")

        # 2. Garmin raw data
        if include_garmin and self.garmin_username:
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
        elif include_garmin:
            logger.warning("Garmin credentials not found, skipping Garmin download.")
                
        logger.info("Raw data download completed. Files saved under %s", mock_data_dir)
        return True
