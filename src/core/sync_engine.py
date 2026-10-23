import os
import json
import logging
import shutil
import time
from datetime import datetime, timedelta
from typing import List, Tuple, Dict, Any, Optional, Set

from src.core.config import ConfigManager, SyncPairModel, SettingsModel, CredentialsModel, GarminCredentials, DivelogsCredentials
from src.core import dive_cache, garmin_files, layout, progress
from src.core.adapter import BaseDiveAdapter
from src.core.fields import (
    MATCH_KEY_MAX_HOURS,
    MATCH_KEY_TYPES,
    FieldLink,
    FieldSpec,
    SyncRule,
    are_gas_mixtures_different,  # noqa: F401  (re-exported; older code imported it from here)
    build_catalog,
    convert_value,
    copy_value,
    deserialize_value,
    get_field,
    is_empty,
    keep_tank_names,
    match_key_equal,
    serialize_value,
    links_to_rules,
    rules_to_links,
    set_field,
    values_equal,
)
from src.core.models import UnifiedDive, GasMixture
from src.core.conflicts import Conflict, ConflictStore, conflicts_path_for, pair_key, rule_conflict_keys
from src.core.templates import render, reverse_parse, take_part, validate_links

logger = logging.getLogger("dive_sync.sync_engine")


# --- matching across time zones (plans/gap-roadmap.md R11) -------------------
#
# Start times are compared as UTC instants when both dives know theirs (FIT,
# Garmin, a UDDF datetime with an offset) and as naive local times otherwise
# (Divelogs, MySSI, Subsurface, the Shearwater app, a UDDF without an offset).
# A local time logged in another zone than its twin's is off by the
# difference of the two zones - a whole number of quarter hours, 30 min to
# 14 h. Such a pair is matched only when the dives' own numbers agree too:
# both the max depth and the duration within the tolerances below, and the
# pair unambiguous (see SyncEngine.match_dives).
ZONE_SHIFT_STEP_MINUTES = 15        # every UTC offset is a multiple of 15 min (+05:45 Nepal, +08:45 Eucla)
ZONE_SHIFT_MIN_MINUTES = 30         # a shorter "shift" is clock drift, not a zone
ZONE_SHIFT_MAX_HOURS = 14           # UTC-12 .. UTC+14 as the owner diving across zones meets them
ZONE_SHIFT_SLACK_SECONDS = 180      # how far off a whole quarter hour two clocks may still be
DEPTH_TOLERANCE_M = 0.5             # two computers on one dive: their max depths within 0.5 m ...
DEPTH_TOLERANCE_FRACTION = 0.03     # ... or 3 % of the deeper, whichever is larger
DURATION_TOLERANCE_S = 90           # their durations within 90 s ...
DURATION_TOLERANCE_FRACTION = 0.03  # ... or 3 % of the longer, whichever is larger
SAME_DISTANCE_SECONDS = 60          # candidates whose start distances fall in the same minute are equally close


def _depth_tolerance(a: float, b: float) -> float:
    return max(DEPTH_TOLERANCE_M, DEPTH_TOLERANCE_FRACTION * max(a, b))


def _duration_tolerance(a: float, b: float) -> float:
    return max(DURATION_TOLERANCE_S, DURATION_TOLERANCE_FRACTION * max(a, b))


def profiles_agree(a: UnifiedDive, b: UnifiedDive) -> bool:
    """True when both dives know their max depth and duration and the two
    agree within the tolerances above - the check a match across a time-zone
    shift needs. A dive without a depth or a duration (0 / None) never
    agrees: a pair the clocks cannot pair is not paired on a guess."""
    da, db = a.max_depth or 0.0, b.max_depth or 0.0
    ta, tb = float(a.duration or 0), float(b.duration or 0)
    if da <= 0 or db <= 0 or ta <= 0 or tb <= 0:
        return False
    return abs(da - db) <= _depth_tolerance(da, db) and abs(ta - tb) <= _duration_tolerance(ta, tb)


def profile_mismatch(a: UnifiedDive, b: UnifiedDive) -> float:
    """How far apart two dives' max depth and duration are, each difference
    in units of its tolerance and summed (0 = identical); a value a dive
    lacks counts as one tolerance. Orders candidates that start equally
    close (``SyncEngine.match_dives``), lower first."""
    score = 0.0
    da, db = a.max_depth or 0.0, b.max_depth or 0.0
    score += abs(da - db) / _depth_tolerance(da, db) if da > 0 and db > 0 else 1.0
    ta, tb = float(a.duration or 0), float(b.duration or 0)
    score += abs(ta - tb) / _duration_tolerance(ta, tb) if ta > 0 and tb > 0 else 1.0
    return score


def zone_shift_minutes(delta_seconds: float, grace_seconds: float) -> Optional[int]:
    """The time-zone difference (minutes, signed) a start-time difference of
    ``delta_seconds`` can be: a multiple of 15 min between 30 min and 14 h,
    within ZONE_SHIFT_SLACK_SECONDS, and beyond the grace window (closer
    starts are an ordinary match). None when it cannot be one."""
    if abs(delta_seconds) <= grace_seconds:
        return None
    step = ZONE_SHIFT_STEP_MINUTES * 60
    steps = round(delta_seconds / step)
    shift = steps * ZONE_SHIFT_STEP_MINUTES
    if abs(delta_seconds - steps * step) > ZONE_SHIFT_SLACK_SECONDS:
        return None
    if not ZONE_SHIFT_MIN_MINUTES <= abs(shift) <= ZONE_SHIFT_MAX_HOURS * 60:
        return None
    return shift


def format_shift(minutes: int) -> str:
    """``+8:00`` / ``-3:30`` for a shift in minutes."""
    sign = "-" if minutes < 0 else "+"
    return f"{sign}{abs(minutes) // 60}:{abs(minutes) % 60:02d}"


class LinkOutcome:
    """What one rule did on one matched pair (see ``SyncEngine._apply_rule``)."""
    __slots__ = ("modified", "conflict", "action", "source_value", "target_value", "warnings")

    def __init__(self, action: str, source_value: Any = None, target_value: Any = None,
                 modified: Optional[Set[str]] = None, conflict: Optional[Conflict] = None,
                 warnings: Optional[List[str]] = None):
        self.action = action          # "equal" | "not_writable" | "kept" | "conflict" | "reverse_unparsed" | "take_unmatched" | "write:<key>[,<key>...]"
        self.source_value = source_value
        self.target_value = target_value
        self.modified = modified      # set of service ids whose dive changed, if any
        self.conflict = conflict
        self.warnings = warnings or []

class ActiveRule:
    """One thing a run does on every matched pair (rework.md G2): apply
    ``rule`` on ``receiver`` (the run's one writable side), reading from
    ``sender``. A ``split`` entry is the derived half of a composite that
    lives on the *other* receiver and carries a ``reverse`` pattern: on a run
    towards its sources' side the edited target text is parsed back into
    those source fields, with ``policy`` = the rule's ``reverse_conflict``
    (or its ``conflict``)."""
    __slots__ = ("rule", "receiver", "sender", "split", "rcv_spec", "snd_spec", "policy")

    def __init__(self, rule: SyncRule, receiver: str, sender: str, split: bool,
                 rcv_spec: FieldSpec, snd_spec: FieldSpec):
        self.rule = rule
        self.receiver = receiver
        self.sender = sender
        self.split = split
        self.rcv_spec = rcv_spec      # the receiver's field (a split: the first source field)
        self.snd_spec = snd_spec      # the sender's (first) field (a split: the composite's target)
        self.policy = (rule.reverse_conflict or rule.conflict) if split else rule.conflict

    @property
    def sender_key(self) -> str:
        return "template" if (self.rule.template and not self.split) else self.snd_spec.key

    @property
    def receiver_key(self) -> str:
        return ", ".join(self.rule.source) if self.split else self.rcv_spec.key

    @property
    def sender_type(self) -> str:
        return "text" if (self.rule.template and not self.split) else self.snd_spec.type

    @property
    def receiver_type(self) -> str:
        return "text" if self.split else self.rcv_spec.type


STATE_FILE = "sync_state.json"

# Run directions that are not tied to a service name (rework.md G0: a run
# writes exactly one side, so there is no "both" entry).
_GENERIC_DIRECTIONS = {"to_target", "to_source"}



# The listing fingerprint and cache reader moved to garmin_files, where the
# Garmin adapter's sync-time cache reuse shares them with the refresh below.
_garmin_listing_fingerprint = garmin_files.listing_fingerprint
_cached_garmin_listings = garmin_files.cached_listings


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

    What happens on a matched pair is driven entirely by the pair's receiver
    rules (see ``src/core/fields.py`` and rework.md Track G): a run writes
    exactly one side, and every rule of that receiver reads the sender's
    field(s), compares with its own, and if they differ lets the rule's
    ``conflict`` policy - read from the receiver's side - decide whether to
    take the sender's value."""

    def __init__(self, settings_path: Optional[str] = None, credentials_path: Optional[str] = None,
                 mock_data_dir: Optional[str] = None, garmin_username: Optional[str] = None,
                 divelogs_username: Optional[str] = None, *,
                 source_adapter: Optional[BaseDiveAdapter] = None,
                 target_adapter: Optional[BaseDiveAdapter] = None,
                 account_scoped_state: bool = False):
        from src.core.config import SETTINGS_FILE, CREDENTIALS_FILE
        self.settings_path = settings_path or SETTINGS_FILE
        self.credentials_path = credentials_path or CREDENTIALS_FILE
        self.settings = ConfigManager.load_settings(self.settings_path)
        self.credentials = ConfigManager.load_credentials(self.credentials_path)
        # rework.md G1: the engine runs one pair from settings.sync_pairs. Set
        # by pairs.engine_for when a named pair is asked for; otherwise the
        # pair whose service ids match the two adapters is used (the
        # garmin_divelogs one for the classic constructor), and a combination
        # with no saved pair runs a transient one with the shipped defaults.
        self.pair_id: Optional[str] = None
        self.run_overrides: Dict[str, Any] = {}
        self.pair: Optional[SyncPairModel] = None
        self.direction: str = "to_target"
        # The board this engine runs: receiver rules + ordered match keys
        # (rework.md G2). ``field_links`` below is their link view, kept for
        # the CLI/boards until G3/G5/G6 and for per-job link boards.
        self.rules: Dict[str, List[SyncRule]] = {}
        self.match_keys: List[List[str]] = []

        self._garmin_username_override = garmin_username
        self._divelogs_username_override = divelogs_username

        # settings.json's folder; the engine's own files go in its sync/
        # folder (layout.py), backups beside it (see _write_pre_sync_backup)
        state_dir = os.path.dirname(self.settings_path) or "."
        self.data_home = mock_data_dir or state_dir
        sync_dir = layout.sync_dir(state_dir)

        if source_adapter is not None or target_adapter is not None:
            if source_adapter is None or target_adapter is None:
                raise ValueError("Both source_adapter and target_adapter are required for a custom pair.")
            self.source: BaseDiveAdapter = source_adapter
            self.target: BaseDiveAdapter = target_adapter
            self.source_id = self._require_service_id(self.source)
            self.target_id = self._require_service_id(self.target)
            if (self.source_id, self.target_id) == ("garmin", "divelogs"):
                self.state_file = os.path.join(sync_dir, STATE_FILE)
            else:
                self.state_file = os.path.join(sync_dir, f"sync_state_{self.source_id}_{self.target_id}.json")
        elif mock_data_dir:
            # If subdirectories with username exist under mock_data_dir, use segmented state file
            mock_sync_dir = layout.sync_dir(mock_data_dir)
            g_path = layout.dives_dir("garmin", self.garmin_username, mock_data_dir) if self.garmin_username else None
            if g_path and os.path.exists(g_path):
                self.state_file = os.path.join(mock_sync_dir, f"sync_state_{self.garmin_username}_{self.divelogs_username}.json")
            else:
                self.state_file = os.path.join(mock_sync_dir, STATE_FILE)

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
                self.state_file = os.path.join(sync_dir, f"sync_state_{self.garmin_username}_{self.divelogs_username}.json")
            else:
                self.state_file = os.path.join(sync_dir, STATE_FILE)

            token_dir = layout.garmin_token_dir(self.garmin_creds.username, self.garmin_creds.token_dir)

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
        if account_scoped_state and not mock_data_dir:
            # The desktop app (rework.md E19): links, last sync time and
            # conflicts belong to one account combination, so a test account
            # never inherits a live account's history.
            from src.core.pairs import account_state_file, adapter_account
            self.state_file = account_state_file(state_dir, self.source_id, adapter_account(self.source),
                                                 self.target_id, adapter_account(self.target))

        # Names and catalogue are captured now so that tests (and callers)
        # may later swap in duck-typed adapters without service metadata.
        self.source_name = getattr(self.source, "display_name", "") or self.source_id
        self.target_name = getattr(self.target, "display_name", "") or self.target_id
        self._slots = {self.source_id: "source", self.target_id: "target"}
        # resolve_conflict's working copies and logins, kept for the life of
        # the engine: the Conflicts page saves many picks through one engine,
        # often several fields of the same dive, and each used to re-fetch it
        self._resolving: Dict[Tuple[str, str], UnifiedDive] = {}
        self._resolving_logins: Set[str] = set()
        self.catalog: Dict[str, FieldSpec] = build_catalog(self.source.field_catalog(), self.target.field_catalog())
        self.conflicts_file = conflicts_path_for(self.state_file)
        # How this run's log lines show a quantity (plans/units.md J6):
        # "metric" (the default, the values as recorded) or "imperial" (a
        # depth, temperature, pressure, volume, weight or visibility with
        # its label). Set by the CLI's --units and the scheduler from the web
        # dashboard's setting; only the log text changes, every value the
        # engine compares, writes and records stays SI.
        self.log_units: str = "metric"
        self.refresh_pair()

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

    # ------------------------------------------------------------------
    # Sync state
    # ------------------------------------------------------------------

    def load_state(self) -> Dict[str, Any]:
        """``{"last_sync_time": iso, "links": {"<source id>": "<target id>"}}``;
        missing or unreadable file -> empty state."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("links", {})
                    return data
            except Exception as e:
                logger.warning("Failed to load sync state: %s", e)
        return {"links": {}}

    def load_last_sync_time(self) -> Optional[datetime]:
        ts = self.load_state().get("last_sync_time")
        if ts:
            try:
                return datetime.fromisoformat(ts)
            except ValueError:
                logger.warning("Ignoring unreadable last_sync_time %r", ts)
        return None

    def load_links(self) -> Dict[str, str]:
        """Known pairs, source external id -> target external id. This is how
        two services that cannot store each other's id (Garmin, Divelogs)
        stay paired across runs, and how an uploaded dive is recognised on
        the next run instead of being re-matched by time."""
        links = self.load_state().get("links", {})
        return {self._canon(self.source_id, k): self._canon(self.target_id, v)
                for k, v in links.items() if k is not None and v is not None}

    def _canon(self, side: str, external_id: Any) -> str:
        """``external_id`` as links are compared: the side's canonical id
        (BaseDiveAdapter.canonical_id), e.g. a Subsurface dive's directory
        without the Dive-N file name that changes when it is renumbered."""
        adapter = self.source if side == self.source_id else self.target
        canon = getattr(adapter, "canonical_id", None)
        return canon(external_id) if callable(canon) else str(external_id).strip()

    def _canon_pair_key(self, conflict: Conflict) -> Tuple[Tuple[str, Optional[str]], ...]:
        """A stored conflict's pair as this run's ``seen_pairs`` are keyed:
        canonical ids, so an entry recorded before the dive was renumbered
        still belongs to the pair and is replaced rather than stacked."""
        return pair_key({service: None if ext is None else self._canon(service, ext)
                         for service, ext in conflict.dive_ids.items()})

    def request_full_compare(self, on: bool = True) -> None:
        """Set (or clear) the one-shot flag that makes the next run compare
        every matched dive regardless of the incremental window."""
        state = self.load_state()
        if on:
            state["full_compare_once"] = True
        else:
            state.pop("full_compare_once", None)
        try:
            os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
            with open(self.state_file, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error("Failed to save sync state: %s", e)

    def save_state(self, dt: Optional[datetime] = None, links: Optional[Dict[str, str]] = None,
                   clear_full_compare: bool = False, quiet: bool = False) -> None:
        """Write what is given into the state file, leaving the rest as it
        is; ``quiet`` says so at debug level only (the per-upload link
        saves of a run, `_save_links_now`)."""
        state = self.load_state()
        if dt is not None:
            state["last_sync_time"] = dt.isoformat()
        if links is not None:
            state["links"] = dict(links)
        if clear_full_compare:
            state.pop("full_compare_once", None)
        try:
            os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
            with open(self.state_file, "w") as f:
                json.dump(state, f, indent=2)
            (logger.debug if quiet else logger.info)("Saved sync state (%s, %d known pairs)",
                                                     state.get("last_sync_time"), len(state.get("links", {})))
        except Exception as e:
            logger.error("Failed to save sync state: %s", e)

    def _save_links_now(self, links: Dict[str, str]) -> None:
        """The links as they stand, written at once (check.md B5): a run
        that breaks off after this upload - an exception from the next
        dive, a killed process - must not lose the pair just made, or the
        next run creates the dive again on a service that stores no
        foreign id (Garmin, Divelogs). The last sync time is not touched:
        the dives not reached yet must still count as new next time. One
        small JSON file, so this costs nothing next to the upload itself."""
        self.save_state(links=links, quiet=True)

    def keep_stopped_run(self) -> None:
        """After progress.Stopped ended run_sync part way: keep what it wrote.
        The links to dives already created are saved, so the next run does
        not create them again, and batching adapters publish what they hold
        (git push, file save). The last sync time stays where it was: the
        dives not reached yet must still count as new next time."""
        links = getattr(self, "_run_links", None)
        if links is None:
            return
        self._run_links = None
        for adapter in (self.source, self.target):
            finish = getattr(adapter, "finish", None)
            if callable(finish):
                try:
                    finish()
                except Exception as e:
                    logger.error("Could not finish writing to %s after the stop: %s",
                                 getattr(adapter, "display_name", "") or type(adapter).__name__, e)
        self.save_state(links=links)

    def save_last_sync_time(self, dt: datetime) -> None:
        self.save_state(dt=dt)

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

    def _write_pre_sync_backup(self, source_dives: List[UnifiedDive], target_dives: List[UnifiedDive]) -> None:
        """DATA_DIR/backups/<timestamp>/<service_id>.json snapshot of what a
        sync run just fetched, written before any add/update/delete this run
        might make (rework.md C14) - a safety net independent of the CLI
        --backup flow (``backup()`` above), which the user has to remember to
        run. Reuses the same ``UnifiedDive`` JSON shape. Best-effort: a
        failure here must never abort the sync it's protecting."""
        try:
            # Anchored to this engine's own data folder (settings.json's, or
            # mock_data_dir) rather than reading DATA_DIR directly, so it
            # follows wherever this engine instance's data actually lives.
            backups_root = os.path.join(self.data_home, "backups")
            backup_dir = os.path.join(backups_root, datetime.now().strftime("%Y%m%dT%H%M%S%f"))
            os.makedirs(backup_dir, exist_ok=True)
            for service_id, dives in ((self.source_id, source_dives), (self.target_id, target_dives)):
                with open(os.path.join(backup_dir, f"{service_id}.json"), "w") as f:
                    json.dump([d.model_dump(mode="json") for d in dives], f, indent=2)
            logger.info("Pre-sync backup saved to %s", backup_dir)
            self._prune_old_backups(backups_root)
        except Exception as e:
            logger.warning("Pre-sync backup failed (continuing without it): %s", e)

    def _prune_old_backups(self, backups_root: str) -> None:
        keep = max(0, self.settings.backup_retention_count)
        try:
            entries = sorted(
                d for d in os.listdir(backups_root) if os.path.isdir(os.path.join(backups_root, d))
            )
        except FileNotFoundError:
            return
        stale = entries if keep == 0 else entries[:-keep]
        for name in stale:
            shutil.rmtree(os.path.join(backups_root, name), ignore_errors=True)

    # ------------------------------------------------------------------
    # Field links
    # ------------------------------------------------------------------

    def resolve_pair(self) -> SyncPairModel:
        """The settings pair this engine runs (see ``__init__``)."""
        if self.pair_id:
            pair = next((p for p in self.settings.sync_pairs if p.id == self.pair_id), None)
            if pair is None:
                raise ValueError(f"No sync pair named {self.pair_id!r} in settings")
            return pair
        pair = self.settings.pair_for(self.source_id, self.target_id)
        if pair is not None:
            return pair
        return SyncPairModel(id=f"{self.source_id}_{self.target_id}", source=self.source_id, target=self.target_id,
                             directionality="to_target")

    def refresh_pair(self) -> None:
        """(Re)read direction, board and per-pair options from the pair."""
        self.pair = self.resolve_pair()
        self.direction = self.pair.directionality
        if self.pair.rules is not None:
            self.rules = {receiver: list(items) for receiver, items in self.pair.rules.items()}
            self.match_keys = [list(k) for k in self.pair.match_keys]
        else:
            self.field_links = self.pair.effective_field_links()
        if self.pair.grace_window_minutes is not None:
            self.settings.grace_window_minutes = self.pair.grace_window_minutes
        if self.pair.propagate_deletes is not None:
            self.settings.propagate_deletes = self.pair.propagate_deletes
        if self.pair.create_on_garmin is not None:
            self.settings.create_on_garmin = self.pair.create_on_garmin
        if self.pair.garmin_fit_upload is not None:
            self.settings.garmin_fit_upload = self.pair.garmin_fit_upload

    @property
    def field_links(self) -> List[FieldLink]:
        """Link view of the run's rules (see fields.rules_to_links)."""
        return rules_to_links(self.rules, self.match_keys, self.source_id, self.target_id)

    @field_links.setter
    def field_links(self, links: List[FieldLink]) -> None:
        self.rules, self.match_keys = links_to_rules(list(links), self.source_id, self.target_id)

    def writable_sides(self) -> Set[str]:
        """The one service id the current ``directionality`` allows writing
        to - the run's receiver (rework.md G0). ``to_<service_id>``
        (``to_divelogs``, ``to_garmin``) names the side; ``to_target`` /
        ``to_source`` are the pair-neutral spellings. Returned as a set for
        the callers that test membership; it never holds more than one id -
        a two-way sync is two directed runs, never one."""
        direction = self.direction
        if direction in ("to_target", f"to_{self.target_id}"):
            return {self.target_id}
        if direction in ("to_source", f"to_{self.source_id}"):
            return {self.source_id}
        logger.warning(
            "Unknown directionality %r for pair %s -> %s (expected to_%s or to_%s; 'bidirectional' is no longer a "
            "run mode - run each direction separately); nothing will be written.",
            direction, self.source_id, self.target_id, self.source_id, self.target_id,
        )
        return set()

    def receiver_id(self) -> Optional[str]:
        """The service this run writes, or None when the direction is unusable."""
        sides = self.writable_sides()
        return next(iter(sides)) if sides else None

    @staticmethod
    def _log_no_new_dives(receiver_name: str, sender_name: str, dives: List[UnifiedDive]) -> None:
        """One plain line per run for a receiver that never takes new dives
        (BaseDiveAdapter.accepts_new_dives), instead of an error per dive."""
        oldest, newest = min(d.date_time for d in dives), max(d.date_time for d in dives)
        span = str(oldest.date()) if oldest.date() == newest.date() else f"{oldest.date()} to {newest.date()}"
        logger.info("  %s has %d dive(s) that %s does not have (%s). They are left as they are: %s only gets "
                    "dives from its own dive computer download, never from a sync. The dives both sides have "
                    "still get their details synced.",
                    sender_name, len(dives), receiver_name, span, receiver_name)

    @staticmethod
    def _refused_upload(adapter: BaseDiveAdapter, name: str, dive: UnifiedDive, sync_results: Dict[str, Any]) -> bool:
        """True (logged and counted under ``skipped`` with the reason) when
        the receiver says it cannot add this dive (BaseDiveAdapter
        cannot_add_reason; MySSI and a dive with two or more tanks), so it
        is never tried. Dry runs count it the same way."""
        reason_of = getattr(adapter, "cannot_add_reason", None)
        reason = reason_of(dive) if callable(reason_of) else None
        if not reason:
            return False
        logger.warning("  %s: dive %s skipped: %s", name, dive.date_time.strftime("%Y-%m-%d %H:%M"), reason)
        sync_results["skipped"].append({"reason": "receiver_cannot_add_dive", "detail": reason, "time": str(dive.date_time)})
        return True

    @staticmethod
    def _note_upload(adapter: BaseDiveAdapter, prepared: UnifiedDive, entry: Dict[str, Any]) -> None:
        """What the receiver says about how it will send this dive
        (BaseDiveAdapter.upload_details; Garmin's .fit upload, R13): its
        note logged under the upload line - in a dry run too, so the run
        lists what would be uploaded and how - and the other keys put on the
        dive's results entry. An adapter without the hook says nothing."""
        details_of = getattr(adapter, "upload_details", None)
        details = details_of(prepared) if callable(details_of) else None
        if not details:
            return
        details = dict(details)
        note = details.pop("note", None)
        if note:
            logger.info("  %s", note)
        entry.update(details)

    def _fit_upload_aside(self) -> str:
        """The tail of the create_on_garmin-off line when .fit upload is on
        for the run: it changes how a dive is created, not whether."""
        if self.settings.garmin_fit_upload:
            return " (upload as .fit is on, but it only applies once create_on_garmin is on for the pair)"
        return ""

    @staticmethod
    def _add_dive(adapter: BaseDiveAdapter, name: str, sid: str, dive: UnifiedDive, payload: UnifiedDive,
                  entry: Dict[str, Any], sync_results: Dict[str, Any]) -> Optional[str]:
        """``adapter.add_dive(payload)`` with the outcome counted (check.md
        B4): the new id (``entry`` goes under uploaded_to_<sid>); None with
        the adapter's ``add_skipped_reason`` set means the receiver left the
        dive out on purpose (it already has it) - counted under ``skipped``
        as ``receiver_already_has_dive``; None without one means the add was
        tried and not taken - a warning and an entry under ``failed``."""
        adapter.add_skipped_reason = None
        new_id = adapter.add_dive(payload)
        if new_id:
            entry[f"new_{sid}_id"] = new_id
            entry["new_id"] = new_id
            sync_results[f"uploaded_to_{sid}"].append(entry)
            return new_id
        reason = getattr(adapter, "add_skipped_reason", None)
        if reason:
            logger.info("  %s: dive at %s not added, %s; counted as skipped", name, dive.date_time, reason)
            sync_results["skipped"].append({"reason": "receiver_already_has_dive", "detail": reason,
                                            "time": str(dive.date_time)})
        else:
            logger.warning("  Failed to add the dive at %s to %s; see the lines above for why", dive.date_time, name)
            sync_results["failed"].append({"action": "add", "service": sid, "time": str(dive.date_time),
                                           "source_id": entry.get("source_id"),
                                           "reason": f"{name} did not add the dive"})
        return None

    def _checked_rules(self) -> Dict[str, List[SyncRule]]:
        """The run's rules that are valid against this pair's catalogue.
        Validation runs on the link view (whose link ids are the rule ids),
        so one save-time checker covers both forms; invalid rules are logged
        and skipped so one bad edit cannot stop a sync."""
        links = rules_to_links(self.rules, self.match_keys, self.source_id, self.target_id)
        bad_ids = set()
        for problem in validate_links(links, self.catalog):
            logger.warning("Ignoring rule: %s", problem)
            if problem.startswith("Link '"):
                bad_ids.add(problem.split("'", 2)[1].split("@", 1)[0])
        return {receiver: [r for r in items if r.id not in bad_ids] for receiver, items in self.rules.items()}

    def active_rules(self, receiver: Optional[str] = None) -> List[ActiveRule]:
        """What this run applies to every matched pair, in order: the
        receiver's own rules (``target_wins`` = never overwrite me, a no-op,
        and tanks rules while ``sync_gases`` is off are left out), then the
        reverse splits of the other side's composites."""
        receiver = receiver or self.receiver_id()
        if receiver is None:
            return []
        checked = self._checked_rules()
        gases = self.settings.sync_filters.sync_gases
        active: List[ActiveRule] = []
        for rule in checked.get(receiver, []):
            if rule.conflict == "target_wins":
                continue
            rcv_spec, snd_spec = self.catalog[rule.target], self.catalog[rule.source[0]]
            if not gases and "tanks" in (rcv_spec.type, snd_spec.type):
                continue
            active.append(ActiveRule(rule, receiver, snd_spec.service_id, False, rcv_spec, snd_spec))
        for other, items in checked.items():
            if other == receiver:
                continue
            for rule in items:
                if rule.template and rule.reverse and rule.sender_id == receiver:
                    if (rule.reverse_conflict or rule.conflict) == "target_wins":
                        continue
                    active.append(ActiveRule(rule, receiver, other, True,
                                             self.catalog[rule.source[0]], self.catalog[rule.target]))
        if getattr(self, "mirror", False):
            # a mirror run forces the sender's value on every rule it applies
            for entry in active:
                entry.policy = "source_wins"
        return active

    def match_key_specs(self) -> List[Tuple[FieldSpec, FieldSpec]]:
        """The pair's match keys, in order, resolved so the first spec is on
        this engine's source side and the second on its target side. Keys
        naming unknown or non number/datetime fields are logged and skipped."""
        out = []
        for entry in self.match_keys:
            if len(entry) != 2:
                logger.warning("Ignoring match key %r: expected [key, key]", entry)
                continue
            a_spec, b_spec = self.catalog.get(entry[0]), self.catalog.get(entry[1])
            if a_spec is None or b_spec is None:
                logger.warning("Ignoring match key %r: unknown field", entry)
                continue
            if a_spec.type not in MATCH_KEY_TYPES or b_spec.type not in MATCH_KEY_TYPES:
                logger.warning("Ignoring match key %r: only number or datetime fields can be match keys", entry)
                continue
            if a_spec.service_id == self.target_id and b_spec.service_id == self.source_id:
                a_spec, b_spec = b_spec, a_spec
            if (a_spec.service_id, b_spec.service_id) != (self.source_id, self.target_id):
                logger.warning("Ignoring match key %r: one key per side expected", entry)
                continue
            out.append((a_spec, b_spec))
        return out

    def _dive_for(self, service_id: str, a_dive: UnifiedDive, b_dive: UnifiedDive) -> UnifiedDive:
        return a_dive if service_id == self.source_id else b_dive

    @classmethod
    def _report_value(cls, field_type: str, value: Any) -> Any:
        """JSON-safe value for reports; long structures become a count."""
        if field_type in ("samples", "tanks"):
            return cls._brief(field_type, value)
        return serialize_value(field_type, value)

    @staticmethod
    def _brief(field_type: str, value: Any) -> Any:
        if field_type in ("samples", "tanks", "list") and value is not None:
            return f"{len(value)} items"
        return value

    def _shown(self, spec: FieldSpec, field_type: str, value: Any) -> Any:
        """``_brief`` for the log lines, in ``log_units``: under imperial a
        number of a quantity field (by the catalogue, src/core/display.py)
        reads converted with its label (``'99 ft'``); everything else, and
        everything in metric, is the value as before."""
        if self.log_units != "metric":
            from src.core.display import converted_text, quantity_kind
            text = converted_text(value, field_type, quantity_kind(spec), self.log_units)
            if text is not None:
                return text
        return self._brief(field_type, value)

    def _apply_rule(self, active: ActiveRule, a_dive: UnifiedDive, b_dive: UnifiedDive) -> LinkOutcome:
        """Apply one active rule to one matched pair in memory (rework.md G2).

        Everything is read from the receiver's side. If the sender's value
        (a template renders first) already equals the receiver's, nothing
        happens. Otherwise the policy decides whether the receiver *takes*
        the sender's value: ``source_wins`` always, even an empty one;
        ``prefer_source`` whenever the sender has a value; ``prefer_non_empty``
        and ``manual`` only to fill a blank receiver - ``manual`` also records
        a real disagreement (both non-empty, different) for the Conflicts
        queue. ``target_wins`` never gets here (never overwrite me). A split
        entry takes the sender's edited composite text apart with the
        composite's ``reverse`` pattern and writes the named source fields;
        its "receiver value" is the template rendered from those fields."""
        rule = active.rule
        rcv_dive = self._dive_for(active.receiver, a_dive, b_dive)
        snd_dive = self._dive_for(active.sender, a_dive, b_dive)
        dives = {self.source_id: a_dive, self.target_id: b_dive}
        warnings: List[str] = []
        snd_type, rcv_type = active.sender_type, active.receiver_type
        if active.split:
            snd_val = get_field(snd_dive, active.snd_spec)
            rcv_val, warnings = render(rule, dives, self.catalog)
            snd_as_rcv = snd_val
        elif rule.template:
            snd_val, warnings = render(rule, dives, self.catalog)
            rcv_val = get_field(rcv_dive, active.rcv_spec)
            snd_as_rcv = convert_value(snd_val, "text", rcv_type, rule.separator)
        else:
            snd_val = get_field(snd_dive, active.snd_spec)
            rcv_val = get_field(rcv_dive, active.rcv_spec)
            if rule.take and not is_empty(snd_type, snd_val):
                # "use only part of the value": the rule sends the part its
                # pattern picks; a value the pattern does not fit is skipped
                part = take_part(rule.take, snd_val)
                if part is None:
                    logger.info("  %s: %s=%r does not match the 'use only part' pattern, %s left alone [rule %s on %s]",
                                active.rcv_spec.label, active.sender_key, self._shown(active.snd_spec, snd_type, snd_val),
                                active.receiver_key, rule.id, active.receiver)
                    return LinkOutcome("take_unmatched", snd_val, rcv_val, warnings=warnings)
                snd_val = part
            snd_as_rcv = convert_value(snd_val, snd_type, rcv_type, rule.separator)
        for w in warnings:
            logger.warning("  %s (dive at %s)", w, rcv_dive.date_time)
        receiver = self.source if active.receiver == self.source_id else self.target
        as_stored = getattr(receiver, "as_stored", None)
        # what the receiver cannot hold (MySSI: a tank name, a profile off its grid) is no difference
        snd_compared = as_stored(rcv_type, snd_as_rcv) if callable(as_stored) else snd_as_rcv
        raw = (snd_val, rcv_val)
        if values_equal(rcv_type, snd_compared, rcv_val):
            return LinkOutcome("equal", *raw, warnings=warnings)

        snd_empty = is_empty(snd_type, snd_val)
        rcv_empty = is_empty(rcv_type, rcv_val)
        policy = active.policy
        shown = (self._shown(active.snd_spec, snd_type, snd_val), self._shown(active.rcv_spec, rcv_type, rcv_val))
        label = active.rcv_spec.label
        if policy == "source_wins":
            take = True
        elif policy == "prefer_source":
            take = not snd_empty
        else:  # prefer_non_empty, manual
            take = rcv_empty and not snd_empty

        if not take:
            if policy == "manual" and not snd_empty and not rcv_empty:
                logger.warning("  %s: conflict left for manual resolution (%s=%r, %s=%r) [rule %s on %s]",
                               label, active.sender_key, shown[0], active.receiver_key, shown[1], rule.id, active.receiver)
                return LinkOutcome("conflict", *raw, conflict=self._conflict_for(active, a_dive, b_dive, snd_val, rcv_val),
                                   warnings=warnings)
            logger.info("  %s differs (%s=%r, %s=%r) -> kept, %s [rule %s on %s]",
                        label, active.sender_key, shown[0], active.receiver_key, shown[1], policy, rule.id, active.receiver)
            return LinkOutcome("kept", *raw, warnings=warnings)

        if active.split:
            parsed = reverse_parse(rule, snd_val, self.catalog)
            if not parsed:
                logger.warning("  %s: %s changed but does not match the reverse pattern, %s left alone [rule %s]",
                               label, active.snd_spec.key, active.receiver_key, rule.id)
                return LinkOutcome("reverse_unparsed", *raw, warnings=warnings)
            written = []
            for key, value in parsed.items():
                spec = self.catalog[key]
                set_field(self._dive_for(spec.service_id, a_dive, b_dive), spec, copy_value(spec.type, value))
                written.append(key)
            logger.info("  %s differs (%s=%r, %s=%r) -> %s := parsed from %s [rule %s on %s, %s]",
                        label, active.sender_key, shown[0], active.receiver_key, shown[1],
                        ", ".join(written), active.snd_spec.key, rule.id, active.receiver, policy)
            return LinkOutcome(f"write:{','.join(written)}", *raw, modified={active.receiver}, warnings=warnings)

        logger.info("  %s differs (%s=%r, %s=%r) -> %s := %s [rule %s on %s, %s]",
                    label, active.sender_key, shown[0], active.receiver_key, shown[1],
                    active.rcv_spec.key, active.sender_key, rule.id, active.receiver, policy)
        value = copy_value(rcv_type, snd_as_rcv)
        if rcv_type == "tanks":
            value = keep_tank_names(value, rcv_val)
        set_field(rcv_dive, active.rcv_spec, value)
        return LinkOutcome(f"write:{active.rcv_spec.key}", *raw, modified={active.receiver}, warnings=warnings)

    def _conflict_for(self, active: ActiveRule, a_dive: UnifiedDive, b_dive: UnifiedDive,
                      snd_val: Any, rcv_val: Any) -> Conflict:
        """A queue entry. Plain rules record sender -> receiver; a composite
        (forward or split) always records sources -> composite target, the
        orientation ``resolve_conflict`` understands (the rendered side can
        only be pushed onto the target, never edited)."""
        rule = active.rule
        if active.split:
            src_service, tgt_service = active.receiver, active.sender
            src_key, tgt_key = rule.source[0], active.snd_spec.key
            src_type, tgt_type = "text", active.snd_spec.type
            src_val, tgt_val = rcv_val, snd_val
        else:
            src_service, tgt_service = active.sender, active.receiver
            src_key, tgt_key = rule.source[0], active.rcv_spec.key
            src_type, tgt_type = active.sender_type, active.rcv_spec.type
            src_val, tgt_val = snd_val, rcv_val
        src_dive = self._dive_for(src_service, a_dive, b_dive)
        tgt_dive = self._dive_for(tgt_service, a_dive, b_dive)
        return Conflict(
            id=Conflict.make_id(rule.id, src_service, tgt_service,
                                src_dive.external_ids.get(src_service), tgt_dive.external_ids.get(tgt_service)),
            link_id=rule.id,
            source_service=src_service,
            target_service=tgt_service,
            source_external_id=src_dive.external_ids.get(src_service),
            target_external_id=tgt_dive.external_ids.get(tgt_service),
            source_key=src_key,
            target_key=tgt_key,
            source_type=src_type,
            field_type=tgt_type,
            dive_ids={self.source_id: a_dive.external_ids.get(self.source_id),
                      self.target_id: b_dive.external_ids.get(self.target_id)},
            source_value=serialize_value(src_type, src_val),
            target_value=serialize_value(tgt_type, tgt_val),
            dive_time=str(a_dive.date_time),
        )

    def prepare_upload(self, dive: UnifiedDive, destination_id: str,
                       active: Optional[List[ActiveRule]] = None) -> UnifiedDive:
        """A deep copy of ``dive`` with every rule of ``destination_id`` whose
        sources are all on the dive's own service applied, so a new dive
        arrives with its composite / service-specific fields rendered. Rules
        that copy a unified attribute onto itself are no-ops and skipped,
        which keeps uploads of tanks (with names) and samples exactly as
        before. A split (the other side's composite, taken apart with its
        ``reverse`` pattern) fills its fields too; a field an earlier rule
        already filled keeps that value unless the split's policy is
        ``source_wins`` / ``prefer_source``, as on a matched dive. Without any
        rule the adapter's own fallback (e.g. the site-name comma split)
        still applies."""
        origin_id = self.target_id if destination_id == self.source_id else self.source_id
        prepared = dive.model_copy(deep=True)
        dives = {origin_id: prepared}
        entries = active if active is not None else self.active_rules(destination_id)
        for entry in entries:
            rule = entry.rule
            if entry.receiver != destination_id:
                continue
            if entry.split:
                self._apply_split_to_upload(entry, prepared)
                continue
            if not all(k.split(".", 1)[0] == origin_id for k in rule.source):
                continue
            if rule.template:
                value, warnings = render(rule, dives, self.catalog)
                for w in warnings:
                    logger.warning("  %s (upload of dive at %s)", w, dive.date_time)
                value = convert_value(value, "text", entry.rcv_spec.type, rule.separator)
            else:
                if entry.snd_spec.unified and entry.snd_spec.unified == entry.rcv_spec.unified:
                    continue
                value = convert_value(get_field(prepared, entry.snd_spec), entry.snd_spec.type,
                                      entry.rcv_spec.type, rule.separator)
            set_field(prepared, entry.rcv_spec, copy_value(entry.rcv_spec.type, value))
        return prepared

    def _apply_split_to_upload(self, entry: ActiveRule, prepared: UnifiedDive) -> None:
        text = get_field(prepared, entry.snd_spec)
        if is_empty(entry.snd_spec.type, text):
            return
        parsed = reverse_parse(entry.rule, text, self.catalog)
        if not parsed:
            logger.warning("  %s %r does not match the split pattern of rule %s; left to the default (upload of dive at %s)",
                           entry.snd_spec.label, text, entry.rule.id, prepared.date_time)
            return
        overwrite = entry.policy in ("source_wins", "prefer_source")
        for key, value in parsed.items():
            spec = self.catalog[key]
            if overwrite or is_empty(spec.type, get_field(prepared, spec)):
                set_field(prepared, spec, copy_value(spec.type, value))

    # ------------------------------------------------------------------
    # Sync run
    # ------------------------------------------------------------------

    def _configure_garmin_cache(self, enabled: bool) -> None:
        """Point each live Garmin side at its account's refresh cache, or
        detach it (GarminAdapter.cache_dir)."""
        from src.core.services.garmin import GarminAdapter
        for adapter in (self.source, self.target):
            if isinstance(adapter, GarminAdapter):
                adapter.cache_dir = garmin_files.cache_dir(adapter.username) if enabled else None
                logger.info("Garmin dives: %s", "reusing the local cache where unchanged" if enabled
                            else "fetching every dive from Garmin Connect (cache off)")

    def run_sync(self, dry_run: bool = False, date_from_override: Optional[str] = None,
                 date_to_override: Optional[str] = None, only_new_override: Optional[bool] = None,
                 direction_override: Optional[str] = None, sync_gases_override: Optional[bool] = None,
                 field_links_override: Optional[List[FieldLink]] = None,
                 rules_override: Optional[Dict[str, List[SyncRule]]] = None,
                 match_keys_override: Optional[List[List[str]]] = None,
                 grace_window_override: Optional[int] = None,
                 propagate_deletes_override: Optional[bool] = None,
                 create_on_garmin_override: Optional[bool] = None,
                 garmin_fit_upload_override: Optional[bool] = None,
                 use_garmin_cache_override: Optional[bool] = None,
                 mirror_override: Optional[bool] = None) -> Dict[str, Any]:
        """Perform one directed synchronization run: read both sides, write
        the one side ``directionality`` names (rework.md G0). A two-way sync
        is two such runs.

        Settings are re-read from disk at the start of every run; per-run
        overrides (CLI flags, cron-job fields) are passed in explicitly so
        they survive that reload."""
        logger.info("Initializing Sync Run (Dry Run: %s)...", dry_run)

        # Reload settings to ensure we have the latest config, then the pair
        self.settings = ConfigManager.load_settings(self.settings_path)
        self.refresh_pair()

        # Apply command-line / job parameter overrides
        if date_from_override is not None:
            self.settings.sync_filters.date_from = date_from_override
        if date_to_override is not None:
            self.settings.sync_filters.date_to = date_to_override
        if only_new_override is not None:
            self.settings.sync_filters.only_new = only_new_override
        if direction_override is not None:
            self.direction = direction_override
        if sync_gases_override is not None:
            self.settings.sync_filters.sync_gases = sync_gases_override
        if field_links_override is not None:          # a per-job link board (cron_jobs[].field_links)
            self.field_links = list(field_links_override)
        if rules_override is not None:
            self.rules = {receiver: list(items) for receiver, items in rules_override.items()}
        if match_keys_override is not None:
            self.match_keys = [list(k) for k in match_keys_override]
        if grace_window_override is not None:
            self.settings.grace_window_minutes = grace_window_override
        if propagate_deletes_override is not None:
            self.settings.propagate_deletes = propagate_deletes_override
        if create_on_garmin_override is not None:
            self.settings.create_on_garmin = create_on_garmin_override
        if garmin_fit_upload_override is not None:
            self.settings.garmin_fit_upload = garmin_fit_upload_override
        # Mirror (the Sync page's checkbox): the receiver becomes a copy of the
        # sender for this run - every dive compared (no incremental window),
        # missing dives created, every mapped field forced to the sender's
        # value (active_rules), and every receiver dive the sender does not
        # have deleted (below, after matching). Rules marked target_wins
        # ("never overwrite me") stay untouched: that is the board's choice.
        self.mirror = bool(mirror_override)
        if self.mirror:
            self.settings.sync_filters.only_new = False
            self.settings.propagate_deletes = True
            self.settings.create_on_garmin = True
            logger.info("Mirror run: %s will be made a copy of the other side (deletes dives it does not have)",
                        self.receiver_id())

        for adapter in (self.source, self.target):
            if hasattr(adapter, "upload_timezone") and self.settings.garmin_timezone:
                adapter.upload_timezone = self.settings.garmin_timezone
            # plans/gap-roadmap.md R13 (beta): how Garmin creates a new dive
            if hasattr(adapter, "fit_upload"):
                adapter.fit_upload = bool(self.settings.garmin_fit_upload)
                if adapter.fit_upload and self.receiver_id() == getattr(adapter, "service_id", None):
                    logger.info("%s: new dives with a profile are uploaded as .fit files (beta)",
                                getattr(adapter, "display_name", "") or adapter.service_id)

        # Login. Progress (src/core/progress.py, the web dashboard's bar): a
        # stage without a count is reported as text only; the Garmin fetch and
        # the per-dive work below report done/total.
        progress.report(0, 0, f"Logging in to {self.source_name} and {self.target_name}")
        if not self.source.login():
            raise RuntimeError(f"Failed to authenticate with {self.source_name}.")
        if not self.target.login():
            raise RuntimeError(f"Failed to authenticate with {self.target_name}.")
        progress.report(0, 0, f"Reading dives from {self.source_name} and {self.target_name}")

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

        # A saved board change may ask for one full pass over every matched
        # dive ("apply to all"): honour the flag once, then clear it.
        state = self.load_state()
        full_compare_once = bool(state.get("full_compare_once"))
        if full_compare_once:
            logger.info("Full compare requested after a mapping change: ignoring the incremental window for this run.")
            self.settings.sync_filters.only_new = False

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
        if use_garmin_cache_override is not None:
            self.settings.sync_filters.use_garmin_cache = use_garmin_cache_override
        self._configure_garmin_cache(self.settings.sync_filters.use_garmin_cache)
        source_dives = self.source.fetch_dives(date_from=date_from, date_to=date_to)
        target_dives = self.target.fetch_dives(date_from=date_from, date_to=date_to)

        # Automatic pre-write backup (rework.md C14): a snapshot of exactly
        # what was just fetched, before any add/update/delete this run might
        # make. Independent of the CLI --backup flow (SyncEngine.backup);
        # best-effort and never blocks the sync itself.
        if not dry_run:
            self._write_pre_sync_backup(source_dives, target_dives)

        # Deletion propagation (rework.md C13). Only ever checked on a full
        # (non-incremental) sync: an incremental run's date-limited fetch
        # cannot tell "deleted" apart from "outside this run's window" - a
        # dive from months ago simply isn't in the fetch either way - so
        # there is no reliable signal to act on here on an incremental run.
        # Must run before match_dives(): a dive whose linked partner just got
        # deleted looks, to the matcher, exactly like a brand new unmatched
        # dive, and would otherwise be uploaded right back to the side it was
        # just deleted from in this same run.
        # Deletes follow the direction (rework.md G0): only the run's receiver
        # is ever written, deletes included. A dive gone from the sender is
        # removed on the receiver; a dive gone from the receiver is left
        # alone here - the opposite directed run is the one that would remove
        # it from the sender - and the sender's copy falls through to the
        # normal unmatched-dive upload below, exactly as with the switch off.
        src, tgt = self.source_id, self.target_id
        known_links = self.load_links()
        # The same dict, grown as dives are linked: what keep_stopped_run
        # saves when the run is stopped part way.
        self._run_links = None if dry_run else known_links
        writable = self.writable_sides()
        deleted_entries: Dict[str, List[Dict[str, Any]]] = {f"deleted_on_{tgt}": [], f"deleted_on_{src}": []}
        # Dives a delete would have removed on a receiver that never takes
        # one from a sync (BaseDiveAdapter.accepts_mirror_deletes; MySSI,
        # Shearwater): logged, counted under skipped, left as they are.
        never_deleted: List[Dict[str, Any]] = []
        if not self.settings.sync_filters.only_new:
            fetched_source_ids = {self._canon(src, d.external_ids[src]) for d in source_dives if d.external_ids.get(src)}
            fetched_target_ids = {self._canon(tgt, d.external_ids[tgt]) for d in target_dives if d.external_ids.get(tgt)}
            for a_id, b_id in list(known_links.items()):
                source_gone = a_id not in fetched_source_ids
                target_gone = b_id not in fetched_target_ids
                if not source_gone and not target_gone:
                    continue
                if source_gone and target_gone:
                    # Gone on both sides already; nothing to propagate, just
                    # stop remembering a pair that no longer exists anywhere.
                    known_links.pop(a_id, None)
                    continue
                gone_side, gone_name = (src, self.source_name) if source_gone else (tgt, self.target_name)
                surviving_side, surviving_adapter, surviving_dives, stale_id = (
                    (tgt, self.target, target_dives, b_id) if source_gone
                    else (src, self.source, source_dives, a_id)
                )
                if surviving_side not in writable:
                    logger.info("  Dive %s is gone from %s, the receiver of this run; nothing is deleted on %s "
                               "(a run towards %s would). %s ID %s will be treated as a new unmatched dive "
                               "(likely re-uploaded to %s)",
                               stale_id, gone_name, surviving_side, surviving_side,
                               surviving_side, stale_id, gone_name)
                    continue
                if not self.settings.propagate_deletes:
                    logger.info("  Dive %s is gone from %s; propagate_deletes is off for this pair, so %s ID %s "
                               "will be treated as a new unmatched dive (likely re-uploaded to %s)",
                               stale_id, gone_name, surviving_side, stale_id, gone_name)
                    continue
                surviving_name = self.target_name if source_gone else self.source_name
                if not getattr(surviving_adapter, "accepts_mirror_deletes", True):
                    # A receiver no sync run may delete on (owner 2026-10-19:
                    # MySSI, where a delete is permanent). Said, counted and
                    # left; the link stays so the next full run says it again
                    # instead of treating the dive as new.
                    stale_dive = next((d for d in surviving_dives if d.external_ids.get(surviving_side)
                                       and self._canon(surviving_side, d.external_ids[surviving_side]) == stale_id), None)
                    logger.info("  Dive %s is gone from %s; propagate_deletes is on, but %s ID %s is not deleted "
                               "(a sync never deletes on %s)", a_id if source_gone else b_id, gone_name,
                               surviving_name, stale_id, surviving_name)
                    never_deleted.append({"reason": "sync_never_deletes_here", "id": stale_id,
                                          "time": str(stale_dive.date_time) if stale_dive else "",
                                          "gone_from": gone_side})
                    # out of this run's matching: not a new upload, not a mirror extra
                    surviving_dives[:] = [d for d in surviving_dives if d is not stale_dive]
                    continue
                logger.info("  Dive %s is gone from %s; propagate_deletes is on -> deleting %s ID %s",
                           stale_id, gone_name, surviving_side, stale_id)
                entry = {"id": stale_id, "gone_from": gone_side}
                if not dry_run:
                    if surviving_adapter.delete_dive(stale_id):
                        deleted_entries[f"deleted_on_{surviving_side}"].append(entry)
                    else:
                        logger.warning("  Failed to delete %s ID %s on %s", surviving_side, stale_id, surviving_side)
                        continue
                else:
                    entry["dry_run"] = True
                    deleted_entries[f"deleted_on_{surviving_side}"].append(entry)
                # Either deleted for real, or a dry run standing in for it:
                # drop the link and keep the dive out of this run's matching
                # so it isn't also reported as a new unmatched upload.
                known_links.pop(a_id, None)
                surviving_dives[:] = [d for d in surviving_dives
                                      if not d.external_ids.get(surviving_side)
                                      or self._canon(surviving_side, d.external_ids[surviving_side]) != stale_id]

        # Match dives (known pairs from earlier runs count as tier 1)
        matched_pairs, unique_source, unique_target = self.match_dives(source_dives, target_dives, known_links)
        for a_dive, b_dive in matched_pairs:
            a_id, b_id = a_dive.external_ids.get(self.source_id), b_dive.external_ids.get(self.target_id)
            if a_id and b_id:
                known_links[self._canon(src, a_id)] = self._canon(tgt, b_id)

        logger.info("Match results: %d matched pairs, %d only in %s, %d only in %s",
                    len(matched_pairs), len(unique_source), self.source_name, len(unique_target), self.target_name)

        # Mirror: the receiver keeps only dives the sender has - unless the
        # receiver never takes a mirror delete (BaseDiveAdapter
        # accepts_mirror_deletes; MySSI, where a delete is permanent): then
        # each dive it would have deleted is logged, counted as skipped and left.
        receiver = self.receiver_id()
        mirror_kept: List[Dict[str, Any]] = never_deleted
        if getattr(self, "mirror", False) and receiver in writable:
            extras = unique_target if receiver == tgt else unique_source
            adapter, name = (self.target, self.target_name) if receiver == tgt else (self.source, self.source_name)
            takes_deletes = getattr(adapter, "accepts_mirror_deletes", True)
            kept = []
            for dive in extras:
                rid = dive.external_ids.get(receiver)
                if not rid:
                    kept.append(dive)
                    continue
                if not takes_deletes:
                    logger.info("  Mirror: %s dive at %s (ID %s) has no match on the other side; not deleted "
                                "(a mirror never deletes on %s)", name, dive.date_time, rid, name)
                    mirror_kept.append({"reason": "mirror_never_deletes_here", "id": str(rid), "time": str(dive.date_time)})
                    kept.append(dive)
                    continue
                entry = {"id": str(rid), "time": str(dive.date_time), "reason": "mirror: not on the other side"}
                logger.info("  Mirror: %s dive at %s (ID %s) has no match on the other side -> deleting it",
                            name, dive.date_time, rid)
                if dry_run:
                    entry["dry_run"] = True
                elif not adapter.delete_dive(str(rid)):
                    logger.warning("  Failed to delete %s ID %s on %s", receiver, rid, name)
                    kept.append(dive)
                    continue
                deleted_entries[f"deleted_on_{receiver}"].append(entry)
            extras[:] = kept

        sync_results: Dict[str, Any] = {
            "dry_run": dry_run,
            "directionality": self.direction,
            "source": src,
            "target": tgt,
            "matched_count": len(matched_pairs),
            f"uploaded_to_{tgt}": [],
            f"uploaded_to_{src}": [],
            f"updated_on_{tgt}": [],
            f"updated_on_{src}": [],
            **deleted_entries,
            "skipped": mirror_kept,
            # writes the receiver refused or that broke (add_dive None without
            # a skip reason, update_dive False): {"action", "service", "id",
            # "time", "reason"} - said in the summaries, never counted as done
            "failed": [],
        }

        # Filter out gas mixtures if disabled in settings
        if not self.settings.sync_filters.sync_gases:
            for d in unique_source + unique_target:
                d.gas_mixtures = []
            for a, b in matched_pairs:
                a.gas_mixtures = []
                b.gas_mixtures = []

        active = self.active_rules()
        run_conflicts: List[Conflict] = []
        seen_pairs = set()
        sync_results["conflicts"] = []

        # Per-dive work, counted for the progress bar: uploads either way, then
        # every matched pair compared (and written where it differs).
        work_total = len(unique_source) + len(unique_target) + len(matched_pairs)
        work_done = [0]

        def advance(message: str) -> None:
            progress.report(work_done[0], work_total, message)
            work_done[0] += 1

        # 1. Upload dives only the source has to the target (rework.md C16:
        # creating a new dive on Garmin from another source is opt-in - a
        # matched dive's field updates below are never gated by this)
        if tgt in writable and tgt == "garmin" and not self.settings.create_on_garmin and unique_source:
            logger.info("  %d new dive(s) found only on %s; create_on_garmin is off, not creating them on Garmin%s",
                       len(unique_source), self.source_name, self._fit_upload_aside())
            sync_results["skipped"].extend(
                {"reason": "create_on_garmin_off", "time": str(d.date_time)} for d in unique_source)
        elif tgt in writable and not getattr(self.target, "accepts_new_dives", True) and unique_source:
            self._log_no_new_dives(self.target_name, self.source_name, unique_source)
            sync_results["skipped"].extend(
                {"reason": "receiver_takes_no_new_dives", "time": str(d.date_time)} for d in unique_source)
        elif tgt in writable:
            for dive in unique_source:
                advance(f"New dive {dive.date_time} -> {self.target_name}")
                if self._refused_upload(self.target, self.target_name, dive, sync_results):
                    continue
                logger.info("Sync action: Upload %s dive at %s to %s", self.source_name, dive.date_time, self.target_name)
                entry = {
                    "time": str(dive.date_time),
                    f"{src}_id": dive.external_ids.get(src),
                    "source_id": dive.external_ids.get(src),
                }
                prepared = self.prepare_upload(dive, tgt, active)
                self._note_upload(self.target, prepared, entry)
                if not dry_run:
                    new_id = self._add_dive(self.target, self.target_name, tgt, dive, prepared, entry, sync_results)
                    if new_id and dive.external_ids.get(src):
                        known_links[self._canon(src, dive.external_ids[src])] = self._canon(tgt, new_id)
                        self._save_links_now(known_links)
                else:
                    entry["dry_run"] = True
                    sync_results[f"uploaded_to_{tgt}"].append(entry)

        # 2. Upload dives only the target has to the source (same C16 gate)
        if src in writable and src == "garmin" and not self.settings.create_on_garmin and unique_target:
            logger.info("  %d new dive(s) found only on %s; create_on_garmin is off, not creating them on Garmin%s",
                       len(unique_target), self.target_name, self._fit_upload_aside())
            sync_results["skipped"].extend(
                {"reason": "create_on_garmin_off", "time": str(d.date_time)} for d in unique_target)
        elif src in writable and not getattr(self.source, "accepts_new_dives", True) and unique_target:
            self._log_no_new_dives(self.source_name, self.target_name, unique_target)
            sync_results["skipped"].extend(
                {"reason": "receiver_takes_no_new_dives", "time": str(d.date_time)} for d in unique_target)
        elif src in writable:
            for dive in unique_target:
                advance(f"New dive {dive.date_time} -> {self.source_name}")
                if self._refused_upload(self.source, self.source_name, dive, sync_results):
                    continue
                logger.info("Sync action: Upload %s dive at %s to %s", self.target_name, dive.date_time, self.source_name)
                entry = {
                    "time": str(dive.date_time),
                    f"{tgt}_id": dive.external_ids.get(tgt),
                    "source_id": dive.external_ids.get(tgt),
                }
                prepared = self.prepare_upload(dive, src, active)
                self._note_upload(self.source, prepared, entry)
                if not dry_run:
                    new_id = self._add_dive(self.source, self.source_name, src, dive, prepared, entry, sync_results)
                    if new_id and dive.external_ids.get(tgt):
                        known_links[self._canon(src, new_id)] = self._canon(tgt, dive.external_ids[tgt])
                        self._save_links_now(known_links)
                else:
                    entry["dry_run"] = True
                    sync_results[f"uploaded_to_{src}"].append(entry)

        # 3. Matched dives: cross-link IDs, then apply every active rule of the receiver
        work_done[0] = len(unique_source) + len(unique_target)     # uploads skipped by a gate still count as done
        # A receiver that takes new dives but no changes to its own
        # (BaseDiveAdapter.accepts_updates; MySSI until K7): the rules still
        # run so the run says what differs, but nothing is written there and
        # no conflict is queued for it - a resolution would be that write.
        takes_updates = {src: getattr(self.source, "accepts_updates", True),
                         tgt: getattr(self.target, "accepts_updates", True)}
        for a_dive, b_dive in matched_pairs:
            advance(f"Comparing dive {a_dive.date_time}")
            a_id = a_dive.external_ids.get(src)
            b_id = b_dive.external_ids.get(tgt)
            seen_pairs.add(pair_key({src: self._canon(src, a_id), tgt: self._canon(tgt, b_id)}))

            needs_update = {src: False, tgt: False}
            is_linking = {src: False, tgt: False}
            unsent_conflicts = {src: 0, tgt: 0}

            # Cross-reference in memory; only a service that can persist the
            # other's id gets an update for it (the local link table covers the rest)
            if a_id and tgt not in a_dive.external_ids:
                a_dive.external_ids[tgt] = b_id
                if getattr(self.source, "stores_external_ids", False):
                    needs_update[src] = True
                    is_linking[src] = True

            if b_id and src not in b_dive.external_ids:
                b_dive.external_ids[src] = a_id
                if getattr(self.target, "stores_external_ids", False):
                    needs_update[tgt] = True
                    is_linking[tgt] = True

            for entry in active:
                outcome = self._apply_rule(entry, a_dive, b_dive)
                for side in outcome.modified or ():
                    needs_update[side] = True
                if outcome.conflict:
                    if not takes_updates.get(entry.receiver, True):
                        unsent_conflicts[entry.receiver] += 1
                        continue
                    run_conflicts.append(outcome.conflict)
                    sync_results["conflicts"].append(outcome.conflict.model_dump(mode="json"))

            sides = (
                (src, self.source, a_dive, a_id, tgt, b_id, self.source_name, self.target_name),
                (tgt, self.target, b_dive, b_id, src, a_id, self.target_name, self.source_name),
            )
            for sid, adapter, dive, ext_id, other_sid, other_id, name, other_name in sides:
                if sid in writable and not takes_updates[sid] and (needs_update[sid] or unsent_conflicts[sid]):
                    which = dive.dive_number if dive.dive_number else ext_id
                    logger.info("  %s: changes to dive %s at %s not sent (updating a %s dive is not supported yet)",
                                name, which, dive.date_time, name)
                    sync_results["skipped"].append({"reason": "receiver_takes_no_updates", "id": str(ext_id),
                                                    "time": str(dive.date_time)})
                    continue
                if not (needs_update[sid] and sid in writable):
                    continue
                if is_linking[sid]:
                    logger.info("Sync action: Link %s ID %s and update fields in %s ID %s", other_name, other_id, name, ext_id)
                else:
                    logger.info("Sync action: Update fields in %s ID %s from %s", name, ext_id, other_name)

                entry = {"id": ext_id, f"linked_{other_sid}": other_id, "linked_id": other_id, "time": str(dive.date_time)}
                if not dry_run:
                    if adapter.update_dive(ext_id, dive):
                        sync_results[f"updated_on_{sid}"].append(entry)
                    else:
                        # check.md B3: a refused or broken update is a failure
                        # of the run, not an update (the adapter logged why)
                        logger.warning("  Failed to update %s ID %s (dive at %s); see the lines above for why",
                                       name, ext_id, dive.date_time)
                        sync_results["failed"].append({"action": "update", "service": sid, "id": str(ext_id),
                                                       "time": str(dive.date_time),
                                                       "reason": f"{name} did not take the update"})
                else:
                    entry["dry_run"] = True
                    sync_results[f"updated_on_{sid}"].append(entry)

        # Let batching adapters publish what was written (git push, file save)
        if not dry_run:
            for adapter in (self.source, self.target):
                finish = getattr(adapter, "finish", None)
                if callable(finish):
                    finish()

        # Persist conflicts (entries for pairs we saw are replaced) and state, unless dry run
        if not dry_run:
            # Touch conflicts.json only when there is something to record or a
            # file whose stale entries may need dropping; never create an empty one.
            if run_conflicts or (seen_pairs and os.path.exists(self.conflicts_file)):
                store = ConflictStore(self.conflicts_file)
                # Entries a board edited outside the apps (settings.json by
                # hand, Docker) no longer raises go too; the apps drop them
                # on save (conflicts.prune_stale_conflicts).
                store.prune(rule_conflict_keys(self.rules), {self.source_id, self.target_id})
                stored = store.replace_for_pairs(seen_pairs, run_conflicts, key=self._canon_pair_key)
                if stored:
                    logger.info("%d conflict(s) waiting for manual resolution in %s", len(stored), self.conflicts_file)
            self.save_state(dt=datetime.now(), links=known_links, clear_full_compare=full_compare_once)
        elif run_conflicts:
            logger.info("%d conflict(s) would be recorded (dry run).", len(run_conflicts))

        logger.info("Sync Completed.")
        return sync_results

    # ------------------------------------------------------------------
    # Conflicts and Test mapping
    # ------------------------------------------------------------------

    def list_conflicts(self) -> List[Conflict]:
        return ConflictStore(self.conflicts_file).load()

    def _find_dive(self, service_id: str, external_id: str, around: Optional[str]) -> Optional[UnifiedDive]:
        """The dive with ``external_id`` on a service, as the copy a
        resolution writes to. Reused across the picks of one engine (see
        ``_resolving``), asked for by id where the adapter can, and otherwise
        found in a two-day ``fetch_dives`` window around ``around``, whose
        other dives are kept for the picks that follow."""
        # ids are compared in their canonical form: a conflict holds the id
        # the dive had when it was read, and a Subsurface id changes with
        # the dive number (Dive-7 -> Dive-8) when the same run renumbers it
        adapter = self.adapter_for(service_id)
        key = (service_id, adapter.canonical_id(external_id))
        if key in self._resolving:
            return self._resolving[key]
        try:
            dive = adapter.fetch_dive(str(external_id))
        except NotImplementedError:
            dive = None
            date_from = date_to = None
            if around:
                try:
                    centre = datetime.fromisoformat(around)
                    date_from, date_to = centre - timedelta(days=1), centre + timedelta(days=1)
                except ValueError:
                    pass
            for found in adapter.fetch_dives(date_from=date_from, date_to=date_to):
                found_id = found.external_ids.get(service_id)
                if found_id is not None:
                    self._resolving.setdefault((service_id, adapter.canonical_id(found_id)), found)
                if found_id is not None and adapter.canonical_id(found_id) == key[1]:
                    dive = found
        if dive is not None:
            self._resolving[key] = dive
        return dive

    def resolve_conflict(self, conflict_id: str, winner: str) -> Conflict:
        """Write the chosen side's recorded value to the other side through the
        normal ``update_dive`` and drop the entry. ``winner`` is ``source`` or
        ``target`` as seen from the link. Raises ValueError when the id is
        unknown or the losing dive cannot be found; RuntimeError when the
        service refuses the update."""
        if winner not in ("source", "target"):
            raise ValueError("winner must be 'source' or 'target'")
        store = ConflictStore(self.conflicts_file)
        conflict = store.get(conflict_id)
        if conflict is None:
            raise ValueError(f"No conflict with id {conflict_id!r} in {self.conflicts_file}")
        src_spec = self.catalog.get(conflict.source_key)
        tgt_spec = self.catalog.get(conflict.target_key)
        if src_spec is None or tgt_spec is None:
            raise ValueError(f"Conflict {conflict.id} references a field this build does not know")

        if winner == "source":
            value = convert_value(deserialize_value(conflict.source_type, conflict.source_value),
                                  conflict.source_type, tgt_spec.type)
            loser_service, loser_ext, loser_spec = conflict.target_service, conflict.target_external_id, tgt_spec
        else:
            if src_spec.key not in self.catalog or conflict.source_type != src_spec.type:
                raise ValueError(f"Conflict {conflict.id}: the source is a rendered template and cannot receive a value; choose 'source'")
            value = convert_value(deserialize_value(conflict.field_type, conflict.target_value),
                                  conflict.field_type, src_spec.type)
            loser_service, loser_ext, loser_spec = conflict.source_service, conflict.source_external_id, src_spec

        adapter = self.adapter_for(loser_service)
        if loser_service not in self._resolving_logins:
            if not adapter.login():
                raise RuntimeError(f"Failed to authenticate with {loser_service}.")
            self._resolving_logins.add(loser_service)
        dive = self._find_dive(loser_service, loser_ext, conflict.dive_time)
        if dive is None:
            raise ValueError(f"Dive {loser_ext} was not found on {loser_service}; it may have been deleted")
        set_field(dive, loser_spec, copy_value(loser_spec.type, value))
        logger.info("Resolving conflict %s: %s := %s on %s dive %s", conflict.id, loser_spec.key,
                    self._brief(loser_spec.type, value), loser_service, loser_ext)
        try:
            if not adapter.update_dive(str(loser_ext), dive):
                raise RuntimeError(f"{loser_service} refused the update of dive {loser_ext}")
        except BaseException:
            # the copy now carries a value the service never took: the next
            # pick on this dive starts from a fresh fetch instead
            self._resolving.pop((loser_service, adapter.canonical_id(loser_ext)), None)
            raise
        finish = getattr(adapter, "finish", None)
        if callable(finish):
            finish()
        store.remove(conflict.id)
        return conflict

    def test_mapping(self, field_links: Optional[List[FieldLink]] = None, limit: int = 10,
                     rules: Optional[Dict[str, List[SyncRule]]] = None,
                     match_keys: Optional[List[List[str]]] = None) -> Dict[str, Any]:
        """Strictly read-only rehearsal of a board (the saved one, or an
        unsaved candidate as links or as rules): fetch the newest ``limit``
        dives per side, match them, and report per matched dive and rule what
        the engine would do for the saved direction. Nothing is written, no
        state is saved."""
        saved = (self.rules, self.match_keys)
        if field_links is not None:
            self.field_links = list(field_links)
        elif rules is not None:
            self.rules = {receiver: list(items) for receiver, items in rules.items()}
            self.match_keys = [list(k) for k in (match_keys or [])]
        try:
            problems = validate_links(self.field_links, self.catalog)
            if problems:
                return {"ok": False, "problems": problems, "rows": []}
            if not self.source.login():
                raise RuntimeError(f"Failed to authenticate with {self.source_name}.")
            if not self.target.login():
                raise RuntimeError(f"Failed to authenticate with {self.target_name}.")
            source_dives = self.source.fetch_recent_dives(limit)
            target_dives = self.target.fetch_recent_dives(limit)
            matched, unique_source, unique_target = self.match_dives(source_dives, target_dives)
            receiver = self.receiver_id()
            active = self.active_rules()
            rows: List[Dict[str, Any]] = []
            for a_dive, b_dive in matched:
                a_copy, b_copy = a_dive.model_copy(deep=True), b_dive.model_copy(deep=True)
                for entry in active:
                    outcome = self._apply_rule(entry, a_copy, b_copy)
                    rows.append({
                        "dive_time": str(a_dive.date_time),
                        "dive_ids": {self.source_id: a_dive.external_ids.get(self.source_id),
                                     self.target_id: b_dive.external_ids.get(self.target_id)},
                        "link": entry.rule.id,
                        "receiver": entry.receiver,
                        "split": entry.split,
                        "source_key": entry.sender_key,
                        "target_key": entry.receiver_key,
                        "source_value": self._report_value(entry.sender_type, outcome.source_value),
                        "target_value": self._report_value(entry.receiver_type, outcome.target_value),
                        "result": outcome.action,
                        "conflict": outcome.conflict is not None,
                        "warnings": outcome.warnings,
                    })
            return {
                "ok": True,
                "problems": [],
                "source": self.source_id,
                "target": self.target_id,
                "directionality": self.direction,
                "receiver": receiver,
                "fetched": {self.source_id: len(source_dives), self.target_id: len(target_dives)},
                "matched": len(matched),
                "unmatched": {self.source_id: [str(d.date_time) for d in unique_source],
                              self.target_id: [str(d.date_time) for d in unique_target]},
                "rows": rows,
            }
        finally:
            self.rules, self.match_keys = saved

    @staticmethod
    def _start_distance_hours(a: UnifiedDive, b: UnifiedDive) -> float:
        """Hours between two start times: on the UTC instants when both dives
        carry one (C15), otherwise on the naive local times."""
        if a.date_time_utc is not None and b.date_time_utc is not None:
            return abs((a.date_time_utc - b.date_time_utc).total_seconds()) / 3600.0
        return abs((a.date_time - b.date_time).total_seconds()) / 3600.0

    @staticmethod
    def _start_delta(a: UnifiedDive, b: UnifiedDive) -> Tuple[float, str]:
        """(seconds from ``a``'s start to ``b``'s, signed; the clock compared:
        ``"UTC"`` when both dives carry a UTC instant, else ``"local"``) - the
        same basis as ``_start_distance_hours``."""
        if a.date_time_utc is not None and b.date_time_utc is not None:
            return (b.date_time_utc - a.date_time_utc).total_seconds(), "UTC"
        return (b.date_time - a.date_time).total_seconds(), "local"

    def match_dives(self, source_list: List[UnifiedDive], target_list: List[UnifiedDive],
                    known_links: Optional[Dict[str, str]] = None) -> Tuple[List[Tuple[UnifiedDive, UnifiedDive]], List[UnifiedDive], List[UnifiedDive]]:
        """Pair up dives from the two sides.

        Tier 1: explicit external-ID links (each side storing the other's id)
                or a pair remembered in the sync state (``known_links``,
                source id -> target id; defaults to the stored table).
        Tier 2: the pair's ordered ``match_keys`` (the generic default board
                for services that let the user number dives keys on the dive number).
        Tier 3: start times within ``grace_window_minutes`` of each other -
                as UTC instants when both dives carry one, else as local
                times (C15, R11).
        Tier 4: a time-zone shift (plans/gap-roadmap.md R11): what is left
                whose start times differ by a whole number of quarter hours
                (30 min to 14 h, ``zone_shift_minutes``) and whose max depth
                and duration agree closely (``profiles_agree``) - the same
                dive logged on two clocks in different zones. A pair is made
                only when it is unambiguous: its shift is the one most
                candidates of both its dives share (on a trip every dive is
                off by the same hours), strictly; a tie leaves both dives
                unmatched, as before R11. Each such match is logged.

        Each tier runs over every dive before the next one starts, and within
        a tier the closest start times pair first; candidates whose start
        distances fall within the same minute go by ``profile_mismatch``
        (the dive whose depth and duration agree better) before list order.
        A start-time match (tier 3) is tried before the match keys (tier 2):
        the same start time is the same dive whatever its number says, while
        a number can go stale - a renumbered log would otherwise pair dive
        "#4" with another day's #4 and upload the real one again as new.
        Keys still pair dives whose clocks disagree (a time-zone shift)
        within MATCH_KEY_MAX_HOURS, before tier 4 guesses one."""
        grace_seconds = self.settings.grace_window_minutes * 60
        src, tgt = self.source_id, self.target_id
        match_keys = self.match_key_specs()
        if known_links is None:
            known_links = self.load_links()
        pairs: Dict[int, int] = {}          # source index -> target index
        taken: set = set()

        def canon(side, value):
            return self._canon(side, value) if value else None

        def id_match(a_dive: UnifiedDive, b_dive: UnifiedDive) -> bool:
            a_id, a_link = canon(src, a_dive.external_ids.get(src)), canon(tgt, a_dive.external_ids.get(tgt))
            b_id, b_link = canon(tgt, b_dive.external_ids.get(tgt)), canon(src, b_dive.external_ids.get(src))
            return bool((a_link and b_id and a_link == b_id) or (a_id and b_link and a_id == b_link)
                        or (a_id and b_id and known_links.get(a_id) == b_id))

        # Tier 1: explicit external-ID links or a remembered pair
        for i, a_dive in enumerate(source_list):
            for j, b_dive in enumerate(target_list):
                if j not in taken and id_match(a_dive, b_dive):
                    pairs[i] = j
                    taken.add(j)
                    break

        def pair_closest(accept) -> None:
            candidates = []
            for i, a_dive in enumerate(source_list):
                if i in pairs:
                    continue
                for j, b_dive in enumerate(target_list):
                    if j in taken:
                        continue
                    distance = self._start_distance_hours(a_dive, b_dive)
                    if accept(a_dive, b_dive, distance):
                        # equally close (same minute): the better depth/duration agreement first
                        candidates.append((int(distance * 3600 // SAME_DISTANCE_SECONDS),
                                           profile_mismatch(a_dive, b_dive), distance, i, j))
            for _bucket, _mismatch, _distance, i, j in sorted(candidates):
                if i not in pairs and j not in taken:
                    pairs[i] = j
                    taken.add(j)

        # Tier 3 first: start times within the grace window (UTC when both
        # sides know their offset, naive local otherwise)
        pair_closest(lambda a, b, hours: hours * 3600 <= grace_seconds)

        # Tier 2: match keys from the board, when the dives start within
        # MATCH_KEY_MAX_HOURS of each other (two logs numbered independently
        # share numbers across decades otherwise)
        def key_match(a_dive, b_dive, hours):
            return hours <= MATCH_KEY_MAX_HOURS and any(
                match_key_equal(a_spec.type, get_field(a_dive, a_spec), get_field(b_dive, b_spec))
                for a_spec, b_spec in match_keys)
        if match_keys:
            pair_closest(key_match)

        # Tier 4: a time-zone shift, only with the depth/duration check and
        # only when unambiguous
        self._pair_across_zone_shift(source_list, target_list, pairs, taken, grace_seconds)

        matched_pairs = [(source_list[i], target_list[pairs[i]]) for i in sorted(pairs)]
        unique_source = [a for i, a in enumerate(source_list) if i not in pairs]
        unique_target = [b for j, b in enumerate(target_list) if j not in taken]
        return matched_pairs, unique_source, unique_target

    def _pair_across_zone_shift(self, source_list: List[UnifiedDive], target_list: List[UnifiedDive],
                                pairs: Dict[int, int], taken: set, grace_seconds: float) -> None:
        """Tier 4 of ``match_dives`` (R11): pair the dives still unmatched
        whose start times differ by a possible time-zone shift and whose max
        depth and duration agree. A candidate's weight is how many candidates
        share its shift; it is paired when that weight is strictly the
        highest among every candidate of its source dive and every candidate
        of its target dive, and the round repeats on what is left. A dive
        that still has candidates when no round pairs anything stays
        unmatched, with one log line. Every pair made is logged with its
        shift. ``pairs`` / ``taken`` are updated in place."""
        src, tgt = self.source_id, self.target_id

        def label(service: str, dive: UnifiedDive) -> str:
            ident = dive.external_ids.get(service)
            when = dive.date_time.strftime("%Y-%m-%d %H:%M")
            return f"{service} dive {ident} ({when})" if ident else f"{service} dive at {when}"

        while True:
            candidates = []   # (i, j, shift minutes, delta seconds, clock)
            for i, a_dive in enumerate(source_list):
                if i in pairs:
                    continue
                for j, b_dive in enumerate(target_list):
                    if j in taken:
                        continue
                    delta, clock = self._start_delta(a_dive, b_dive)
                    shift = zone_shift_minutes(delta, grace_seconds)
                    if shift is not None and profiles_agree(a_dive, b_dive):
                        candidates.append((i, j, shift, delta, clock))
            if not candidates:
                return
            support: Dict[int, int] = {}
            by_source: Dict[int, list] = {}
            by_target: Dict[int, list] = {}
            for c in candidates:
                support[c[2]] = support.get(c[2], 0) + 1
                by_source.setdefault(c[0], []).append(c)
                by_target.setdefault(c[1], []).append(c)
            made = False
            for c in candidates:
                i, j, shift, delta, clock = c
                rivals = [o for o in by_source[i] + by_target[j] if o is not c]
                if any(support[o[2]] >= support[shift] for o in rivals):
                    continue
                pairs[i] = j
                taken.add(j)
                made = True
                a_dive, b_dive = source_list[i], target_list[j]
                logger.info("Matched %s with %s across a time-zone shift of %s h: their %s start times "
                            "differ by %s, and max depth (%.1f / %.1f m) and duration (%d / %d min) agree",
                            label(src, a_dive), label(tgt, b_dive), format_shift(shift), clock,
                            format_shift(int(round(delta / 60.0))), a_dive.max_depth, b_dive.max_depth,
                            round((a_dive.duration or 0) / 60), round((b_dive.duration or 0) / 60))
            if not made:
                for i in sorted({c[0] for c in candidates}):
                    others = ", ".join(label(tgt, target_list[c[1]]) for c in candidates if c[0] == i)
                    logger.info("Not matched across a time-zone shift: %s could be %s - more than one fits, "
                                "so none is taken", label(src, source_list[i]), others)
                return

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
        the other service's API).

        ``overwrite`` is a Garmin matter: it drops the Garmin cache so every
        dive is fetched again instead of reusing the unchanged ones (a Garmin
        dive costs three API calls). Divelogs is one request for the whole
        log, written in full and pruned on every download anyway."""
        logger.info("Starting raw data download (garmin=%s, divelogs=%s)...", include_garmin, include_divelogs)

        garmin_dir = layout.dives_dir("garmin", self.garmin_username, mock_data_dir)
        divelogs_dir = layout.dives_dir("divelogs", self.divelogs_username, mock_data_dir)

        if overwrite and include_garmin and os.path.exists(garmin_dir):
            logger.info("Re-fetching every Garmin dive. Clearing directory: %s", garmin_dir)
            shutil.rmtree(garmin_dir)

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
                    
                    divelogs_keep: set = set()
                    for index, item in enumerate(dives_list, 1):
                        dive_number = item.get("divenumber")
                        dive_id = item.get("id")
                        
                        filename = f"{dive_number}.json" if (dive_number is not None and str(dive_number).isdigit() and int(dive_number) > 0) else f"{dive_id}.json"
                        filepath = os.path.join(divelogs_dir, filename)
                        divelogs_keep.add(filename)

                        with open(filepath, "w") as f:
                            json.dump(item, f, indent=2)
                        logger.debug(" Saved %s", filepath)
                        
                    # same rule as Garmin: a dive deleted or renumbered on the
                    # service must not leave a ghost in the local cache (E18)
                    dive_cache.prune_cache_dir(divelogs_dir, divelogs_keep, "Divelogs")

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
                        
                        # Scuba modes only: apnea dives are listed under "diving" too
                        dives_batch = [act for act in response if garmin_files.is_scuba_activity(act)]
                        all_dives.extend(dives_batch)
                        
                        if len(response) < limit:
                            break
                        start += limit
                        time.sleep(self.garmin.cooldown_seconds)
                    except Exception as e:
                        logger.error("Failed to query activity list from Garmin Connect: %s", e)
                        return False
                
                total_dives = len(all_dives)
                # A dive already on disk whose listing entry is unchanged costs
                # three API calls and three cool-downs to fetch again for no
                # gain, which is what made a refresh take minutes (rework.md
                # E15). Note the listing does not carry notes, buddy, weight or
                # visibility, so an edit to only those is invisible here - that
                # is what overwrite=True (Full refresh) is for.
                cached = {} if overwrite else _cached_garmin_listings(garmin_dir)
                skipped = 0
                # Every cache file this pass accounts for. Anything else in the
                # directory is a dive that no longer exists on Garmin, or the
                # old name of one that was renumbered (rework.md E18).
                keep: set = set()
                logger.info("Found %d Garmin dive activities (%d already cached). Fetching details and saving raw JSONs...",
                            total_dives, len(cached))

                for index, activity in enumerate(all_dives, 1):
                    activity_id = activity.get("activityId")
                    if not activity_id:
                        continue

                    known = cached.get(str(activity_id))
                    if known is not None and _garmin_listing_fingerprint(known["summary"]) == _garmin_listing_fingerprint(activity):
                        skipped += 1
                        # Brings a file cached under an older naming scheme
                        # (plain "<dive number>.json") onto the current one
                        # without fetching the dive again.
                        wanted = f"{known['stem']}.json" if known.get("stem") else known["file"]
                        if wanted != known["file"]:
                            try:
                                os.replace(os.path.join(garmin_dir, known["file"]), os.path.join(garmin_dir, wanted))
                                logger.info(" Renamed cached Garmin file %s -> %s", known["file"], wanted)
                            except OSError as e:
                                logger.warning("Could not rename cached Garmin file %s: %s", known["file"], e)
                                wanted = known["file"]
                        keep.add(wanted)
                        progress.report(index, total_dives,
                                        f"Garmin dive {activity.get('startTimeLocal') or activity_id} (cached)", "garmin")
                        logger.debug(" [%d/%d] Garmin Activity ID %s unchanged; keeping the cached copy.",
                                     index, total_dives, activity_id)
                        continue

                    logger.info(" [%d/%d] Fetching details for Garmin Activity ID: %s...", index, total_dives, activity_id)
                    progress.report(index, total_dives,
                                    f"Garmin dive {activity.get('startTimeLocal') or activity_id}", "garmin")
                    try:
                        time.sleep(self.garmin.cooldown_seconds)
                        details = self.garmin.client.connectapi(f"/activity-service/activity/{activity_id}")
                        
                        # Parts that failed to download, so the next refresh
                        # knows to try this dive again instead of trusting an
                        # incomplete cache entry for ever (rework.md E15: the
                        # skip is keyed on the listing entry, which would not
                        # change just because a fetch had failed).
                        incomplete = []

                        # Fetch the activity details containing sensor telemetry graphs/charts over time
                        time.sleep(self.garmin.cooldown_seconds)
                        try:
                            logger.info(" Fetching telemetry/metrics details for Activity ID: %s...", activity_id)
                            activity_details = self.garmin.client.get_activity_details(activity_id)
                        except Exception as detail_err:
                            logger.warning("Failed to fetch activity details (telemetry) for %s: %s", activity_id, detail_err)
                            activity_details = None
                            incomplete.append("activityDetails")

                        # Fetch the tank sensor telemetry detail
                        time.sleep(self.garmin.cooldown_seconds)
                        try:
                            logger.info(" Fetching tank sensor telemetry for Activity ID: %s...", activity_id)
                            tanksensor = self.garmin.client.connectapi(
                                "/diving/v1/dive/detail/tanksensor",
                                params={"connectActivityId": activity_id}
                            )
                        except Exception as tank_err:
                            tanksensor = None
                            # A 404 is the normal answer for a dive logged
                            # without tank sensors - absent, not failed.
                            if "404" in str(tank_err):
                                logger.info("No tank sensor details for Garmin activity %s (404).", activity_id)
                            else:
                                logger.warning("Failed to fetch tank sensor details for %s: %s", activity_id, tank_err)
                                incomplete.append("tanksensor")

                        metadata = details.get("metadataDTO", {}) or activity.get("metadataDTO", {}) or {}
                        dive_number = metadata.get("diveNumber")
                        
                        filename = garmin_files.dive_stem(
                            dive_number, activity.get("startTimeLocal") or (details.get("summaryDTO") or {}).get("startTimeLocal"),
                            activity_id) + ".json"
                        filepath = os.path.join(garmin_dir, filename)
                        keep.add(filename)

                        raw_payload = {
                            "summary": activity,
                            "details": details,
                            "activityDetails": activity_details,
                            "tanksensor": tanksensor,
                        }
                        if incomplete:
                            raw_payload["incomplete"] = incomplete
                            logger.warning(" Garmin activity %s cached without %s; the next refresh will retry it.",
                                           activity_id, ", ".join(incomplete))
                        
                        # Written whole, then renamed in: the dives page
                        # lists the cache while a refresh runs and must never
                        # read a half-written dive.
                        with open(filepath + ".tmp", "w") as f:
                            json.dump(raw_payload, f, indent=2)
                        os.replace(filepath + ".tmp", filepath)
                        logger.debug(" Saved %s", filepath)
                    except Exception as e:
                        logger.error("Failed to fetch/save details for Garmin activity %s: %s", activity_id, e)
                        return False

                dive_cache.prune_cache_dir(garmin_dir, keep, "Garmin")
                garmin_files.rename_fit_files(garmin_dir, self.garmin_username, mock_data_dir)
                fetched = total_dives - skipped
                logger.info("Garmin download finished: %d dive(s) fetched, %d already cached and unchanged "
                            "(about %d API call(s) and %.0fs of cool-down saved).",
                            fetched, skipped, skipped * 3, skipped * 3 * self.garmin.cooldown_seconds)
            else:
                logger.error("Failed to log in to Garmin Connect.")
                return False
        elif include_garmin:
            logger.warning("Garmin credentials not found, skipping Garmin download.")
                
        logger.info("Raw data download completed. Files saved under %s", mock_data_dir)
        return True
