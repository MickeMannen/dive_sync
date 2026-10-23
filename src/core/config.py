import os
import json
import logging
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from src.core.fields import FieldLink, SyncRule, default_field_links, links_to_rules, rules_to_links

logger = logging.getLogger("dive_sync.config")

# ---------------------------------------------------------------------------
# Run direction (rework.md G0)
# ---------------------------------------------------------------------------
# A sync run has exactly one receiver: ``directionality`` names it as
# ``to_<service id>`` (``to_divelogs``, ``to_garmin``, ``to_subsurface``, ...)
# or with the pair-neutral spellings ``to_target`` / ``to_source``. The old
# ``bidirectional`` run mode - one run writing both sides - is gone: a two-way
# sync is two directed runs (two cron jobs, two CLI calls), never one, so the
# second always sees what the first wrote and nothing needs a tiebreak. An
# older settings file, profile, API payload or job that still says
# ``bidirectional`` is rewritten on the way in: the pair's declared source is
# the sender, so it becomes ``to_target`` (``to_divelogs`` for the implicit
# Garmin -> Divelogs pair), and a cron job that names a pair simply follows
# that pair's saved direction.

LEGACY_RUN_DIRECTION = "bidirectional"
DEFAULT_RUN_DIRECTION = "to_divelogs"       # the Garmin -> Divelogs pair
DEFAULT_PAIR_RUN_DIRECTION = "to_target"    # a named pair: its declared source sends

# ---------------------------------------------------------------------------
# Receiver rules and the explicit default pair (rework.md G1)
# ---------------------------------------------------------------------------
# A board is stored as receiver rules (``sync_pairs[].rules``, keyed by the
# receiving service id) plus the pair's ordered ``match_keys``. The old
# ``field_links`` list - top level for the once-implicit Garmin <-> Divelogs
# pair, per pair otherwise - and the top-level ``directionality`` are still
# accepted on the way in and folded into ``sync_pairs[0]``, the explicit
# ``garmin_divelogs`` pair, which every build from now on keeps in the file.
# ``SettingsModel.directionality`` / ``.field_links`` and
# ``SyncPairModel.field_links`` stay as properties over that pair's rules so
# the engine, CLI and both boards keep working unchanged until they read
# rules directly (G2, G5, G6).

SETTINGS_VERSION = 2
DEFAULT_PAIR_ID = "garmin_divelogs"
# spec name -> service id (the Subsurface Cloud adapter shares Subsurface's catalogue and ids)
SERVICE_ID_ALIASES = {"subsurface-cloud": "subsurface"}


def service_id_of_spec(spec: str) -> str:
    """``garmin`` -> ``garmin``, ``uddf:out.uddf`` -> ``uddf``, ``subsurface-cloud`` -> ``subsurface``."""
    name = (spec or "").strip().partition(":")[0].strip().lower()
    return SERVICE_ID_ALIASES.get(name, name)


def is_run_direction(value: Any) -> bool:
    """True for a value the engine can run: ``to_<something>``."""
    return isinstance(value, str) and value.startswith("to_") and len(value) > 3


def _migrate_direction(holder: Dict[str, Any], replacement: Optional[str], where: str,
                       changes: Optional[List[str]] = None) -> None:
    if isinstance(holder, dict) and holder.get("directionality") == LEGACY_RUN_DIRECTION:
        holder["directionality"] = replacement
        note = f"{where}: '{LEGACY_RUN_DIRECTION}' -> {replacement!r}"
        if changes is not None:
            changes.append(note)
        else:
            logger.warning("Run direction %s (rework.md G0: a run has one receiver; two-way = two directed runs).", note)


def _migrate_job_direction(job: Dict[str, Any], changes: Optional[List[str]] = None) -> None:
    if isinstance(job, dict):
        replacement = None if job.get("pair") else DEFAULT_RUN_DIRECTION
        _migrate_direction(job, replacement, f"cron_jobs[{job.get('id', '?')}].directionality", changes)


def migrate_run_directions(data: Dict[str, Any]) -> List[str]:
    """Rewrite every ``bidirectional`` run direction in a raw settings dict in
    place and return one line per change (empty when nothing was old)."""
    changes: List[str] = []
    if not isinstance(data, dict):
        return changes
    _migrate_direction(data, DEFAULT_RUN_DIRECTION, "directionality", changes)
    for pair in data.get("sync_pairs") or []:
        if isinstance(pair, dict):
            _migrate_direction(pair, DEFAULT_PAIR_RUN_DIRECTION,
                               f"sync_pairs[{pair.get('id', '?')}].directionality", changes)
    for job in data.get("cron_jobs") or []:
        _migrate_job_direction(job, changes)
    return changes

# ---------------------------------------------------------------------------
# Retired services (plans/subsurface-submersion.md F18)
# ---------------------------------------------------------------------------
# Submersion support was removed on 2026-10-23. A settings file, profile or
# API payload written by an older build may still hold a pair or a cron job
# with a Submersion side; those are dropped on the way in (a job that names a
# dropped pair goes with it), so the rest of the file keeps loading. Its other
# leftovers - ``create_device_dives_on_submersion`` at the top level or on a
# pair, a ``submersion`` block in credentials.json - are unknown keys the
# models ignore, and the next save leaves them out.

RETIRED_SERVICES = ("submersion",)


def _names_retired_service(entry: Dict[str, Any]) -> bool:
    return any(service_id_of_spec(str(entry.get(side) or "")) in RETIRED_SERVICES
               for side in ("source", "target"))


def drop_retired_services(data: Dict[str, Any]) -> List[str]:
    """Remove the sync pairs and cron jobs of a raw settings dict that name a
    retired service (Submersion), in place, and return one line per removal
    (empty when there was nothing to drop)."""
    changes: List[str] = []
    if not isinstance(data, dict):
        return changes
    dropped_pairs = set()
    pairs = data.get("sync_pairs")
    if isinstance(pairs, list):
        kept = []
        for pair in pairs:
            if isinstance(pair, dict) and _names_retired_service(pair):
                dropped_pairs.add(pair.get("id"))
                changes.append(f"sync_pairs[{pair.get('id', '?')}] ({pair.get('source')} -> {pair.get('target')})")
            else:
                kept.append(pair)
        if len(kept) != len(pairs):
            data["sync_pairs"] = kept
    jobs = data.get("cron_jobs")
    if isinstance(jobs, list):
        kept = []
        for job in jobs:
            if isinstance(job, dict) and (_names_retired_service(job)
                                          or (job.get("pair") and job.get("pair") in dropped_pairs)):
                changes.append(f"cron_jobs[{job.get('id', '?')}]")
            else:
                kept.append(job)
        if len(kept) != len(jobs):
            data["cron_jobs"] = kept
    return changes


def _log_retired_services(where: str, changes: List[str]) -> None:
    logger.warning("%s named Submersion, which DiveSync no longer supports; dropped: %s",
                   where, "; ".join(changes))


SETTINGS_FILE = os.path.join(os.environ.get("DATA_DIR", "."), "settings.json")
CREDENTIALS_FILE = os.path.join(os.environ.get("DATA_DIR", "."), "credentials.json")

class SyncFilters(BaseModel):
    date_from: Optional[str] = Field(None, description="Sync start date, format YYYY-MM-DD")
    date_to: Optional[str] = Field(None, description="Sync end date, format YYYY-MM-DD")
    only_new: bool = Field(True, description="Sync only new dives since last run")
    sync_gases: bool = Field(True, description="Sync detailed gas mixtures (alias for 'the tanks links are not off')")
    use_garmin_cache: bool = Field(
        True,
        description="Read a Garmin dive from the local refresh cache when Garmin's activity listing shows it "
                    "unchanged, instead of fetching it again (three API calls per dive); fetched dives are cached. "
                    "The listing does not reveal an edit to only notes, buddy, weight or visibility, so such an "
                    "edit is picked up after a Full refresh or a sync with this off",
    )

class SyncScheduleSlot(BaseModel):
    hour: int = Field(..., ge=0, le=23)
    minute: int = Field(..., ge=0, le=59)

class CronJobModel(BaseModel):
    id: str = Field(..., description="Unique ID for this job")
    directionality: Optional[str] = Field(
        None,
        description="Which side this job writes: to_<service id>, to_source or to_target. None = the pair's saved "
                    "direction (the global one for the implicit Garmin -> Divelogs pair). One receiver per job; "
                    "a two-way schedule is two jobs (rework.md G0)",
    )
    frequency: str = Field("daily", description="hourly, daily, weekly, custom_minutes")
    hour: int = Field(0, ge=0, le=23)
    minute: int = Field(0, ge=0, le=59)
    day_of_week: int = Field(0, ge=0, le=6, description="0=Sunday, 6=Saturday for weekly")
    interval_minutes: int = Field(60, ge=1, description="Interval in minutes if frequency is custom_minutes")
    only_new: bool = Field(True, description="Sync only new dives since last run")
    sync_gases: bool = Field(True, description="Sync detailed gas mixtures")
    enabled: bool = Field(True, description="Whether this job is active")
    field_links: Optional[List[FieldLink]] = Field(
        None, description="Optional per-job mapping board; None means the global field_links apply"
    )
    pair: Optional[str] = Field(None, description="Id of a configured sync pair; None = Garmin -> Divelogs")
    # A job can instead name its two sides, like the Sync page: the pair (and
    # board) between them is found either way round (pairs.engine_for).
    source: Optional[str] = Field(None, description="Source service spec (with target, instead of pair)")
    target: Optional[str] = Field(None, description="Target service spec, the side the job writes")
    use_garmin_cache: Optional[bool] = Field(None, description="None = the saved default (sync_filters.use_garmin_cache)")
    garmin_fit_upload: Optional[bool] = Field(None, description="Beta (plans/gap-roadmap.md R13): True uploads the new dives this job creates on Garmin as .fit files; None = the pair's setting (garmin_fit_upload)")
    garmin_username: Optional[str] = Field(None, description="Which configured Garmin account to use; None picks the only one, or errors if several are configured")
    divelogs_username: Optional[str] = Field(None, description="Which configured Divelogs account to use; None picks the only one, or errors if several are configured")
    # plans/myssi-sync.md K5: a scheduled job with MySSI as a side picks its
    # account (the email) like the Sync page does; the scheduler forwards it
    # to pairs.engine_for as ssi_account.
    ssi_account: Optional[str] = Field(None, description="Which configured MySSI account (email) to use; None picks the only one, or errors if several are configured")

    @model_validator(mode="before")
    @classmethod
    def _drop_bidirectional(cls, data: Any) -> Any:
        _migrate_job_direction(data)
        return data

class SyncPairModel(BaseModel):
    """One source/target pair the engine can run (rework.md F3). A service
    spec is a service id, optionally with an argument after a colon:
    ``garmin``, ``divelogs``, ``uddf:<file>``, ``subsurface:<directory>``.
    Relative paths resolve against DATA_DIR."""
    id: str = Field(..., description="Unique name of the pair, used by --pair and by cron jobs")
    source: str = Field("garmin", description="Service spec of side A")
    target: str = Field("divelogs", description="Service spec of side B")
    directionality: str = Field(DEFAULT_PAIR_RUN_DIRECTION,
                                description="Which side a run of this pair writes: to_<service id>, to_source or to_target (rework.md G0)")
    enabled: bool = True
    grace_window_minutes: Optional[int] = Field(None, description="Per-pair override of the matching window")
    rules: Optional[Dict[str, List[SyncRule]]] = Field(
        None, description="The board (rework.md G1): what each service accepts, keyed by receiving service id, e.g. "
                          "{'divelogs': [...], 'garmin': [...]}. None = the shipped defaults for this pair")
    match_keys: List[List[str]] = Field(
        default_factory=list,
        description="Ordered [source-side key, target-side key] pairs tried as match keys before start time")
    propagate_deletes: Optional[bool] = Field(None, description="Per-pair override of whether a dive deleted on one side is deleted on the other (rework.md C13); None = the global default")
    create_on_garmin: Optional[bool] = Field(None, description="Per-pair override of whether a new dive found only on the other side is created on Garmin (rework.md C16); None = the global default. Matched-dive updates are never affected")
    garmin_fit_upload: Optional[bool] = Field(None, description="Per-pair override of how a new dive is created on Garmin (plans/gap-roadmap.md R13, beta): True uploads it as a .fit file (profile, tanks and gases arrive), False posts the manual activity; None = the global default. Whether a dive is created on Garmin at all is still create_on_garmin's call")

    @model_validator(mode="before")
    @classmethod
    def _legacy_input(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        _migrate_direction(data, DEFAULT_PAIR_RUN_DIRECTION, f"sync_pairs[{data.get('id', '?')}].directionality")
        # rework.md G1: a per-pair link board (v1) becomes this pair's rules.
        # When both are given the links win: they are the editable view the
        # boards still post back, the rules the stored truth they replace.
        if "field_links" in data:
            links = data.pop("field_links")
            if links is not None:
                links = [l if isinstance(l, FieldLink) else FieldLink.model_validate(l) for l in links]
                rules, keys = links_to_rules(links, service_id_of_spec(data.get("source", "garmin")),
                                             service_id_of_spec(data.get("target", "divelogs")))
                data["rules"], data["match_keys"] = rules, keys
            elif "rules" not in data:
                data["rules"] = None
        return data

    @property
    def source_service(self) -> str:
        return service_id_of_spec(self.source)

    @property
    def target_service(self) -> str:
        return service_id_of_spec(self.target)

    @property
    def field_links(self) -> Optional[List[FieldLink]]:
        """Link view of ``rules`` (None = the defaults for this pair); see fields.rules_to_links."""
        if self.rules is None:
            return None
        return rules_to_links(self.rules, self.match_keys, self.source_service, self.target_service)

    @field_links.setter
    def field_links(self, links: Optional[List[FieldLink]]) -> None:
        if links is None:
            self.rules, self.match_keys = None, []
            return
        self.rules, self.match_keys = links_to_rules(list(links), self.source_service, self.target_service)

    def effective_field_links(self) -> List[FieldLink]:
        """The board this pair runs with: its own, or the shipped defaults."""
        if self.rules is not None:
            return self.field_links or []
        from src.core.pairs import default_links_for
        return default_links_for(self.source_service, self.target_service)


def default_pair_model(links: Optional[List[FieldLink]] = None, directionality: Optional[str] = None) -> "SyncPairModel":
    """The Garmin <-> Divelogs pair every settings file carries (rework.md G1)."""
    pair = SyncPairModel(id=DEFAULT_PAIR_ID, source="garmin", target="divelogs",
                         directionality=directionality or DEFAULT_RUN_DIRECTION)
    pair.field_links = links if links is not None else default_field_links()
    return pair


# When notify_url is POSTed to (plans/gap-roadmap.md R24): SettingsModel.notify_on
NOTIFY_ON_CHOICES = ("failure", "changes", "always")


class SettingsModel(BaseModel):
    settings_version: int = Field(SETTINGS_VERSION, description="File format version; 2 = receiver rules on pairs (rework.md G1)")
    sync_filters: SyncFilters = Field(default_factory=SyncFilters)
    grace_window_minutes: int = Field(15, description="Matching grace window in minutes")
    api_cooldown_seconds: float = Field(
        0.5,
        description="Cool-down delay in seconds between API requests. Halved from 1.0 on 2026-09-23 (rework.md E16): "
                    "a Garmin refresh spends most of its time here, and the owner has never hit a Garmin 429 in daily use. "
                    "Raise it again if a service starts rate-limiting",
    )
    schedule: List[SyncScheduleSlot] = Field(default_factory=list, description="Cron-like multi-slot schedule")
    cron_jobs: List[CronJobModel] = Field(default_factory=list, description="List of configured cron jobs")
    garmin_timezone: Optional[str] = Field(
        None,
        description="IANA zone to stamp on dives uploaded to Garmin Connect; empty = detect from the account's newest dive",
    )
    sync_pairs: List[SyncPairModel] = Field(
        default_factory=lambda: [default_pair_model()],
        description="Every sync pair, each with its own rules and direction; sync_pairs[0] is always the "
                    "'garmin_divelogs' pair (rework.md G1)",
    )
    notify_url: Optional[str] = Field(
        None,
        description="Webhook (ntfy / Gotify / generic) POSTed a short plain-text message when a scheduled or manual sync run fails; empty disables alerts (rework.md A11)",
    )
    notify_on: Literal["failure", "changes", "always"] = Field(
        "failure",
        description="When notify_url is sent a message (plans/gap-roadmap.md R24): 'failure' only when a run fails "
                    "(the default, as before), 'changes' also after a run that added, updated or deleted dives, "
                    "raised conflicts or had failed writes (a dry run writes nothing, so it never counts), "
                    "'always' after every run that finished, dry runs included, with a summary of its counts",
    )
    propagate_deletes: bool = Field(
        False,
        description="Default for the implicit Garmin<->Divelogs pair, and the fallback for any pair without its own override: delete the linked dive on the other side when one is deleted here (rework.md C13). Only checked during a full (non-incremental) sync - an incremental run's date-limited fetch cannot tell 'deleted' apart from 'outside this run's window', so deletion detection is skipped there entirely, on or off",
    )
    backup_retention_count: int = Field(
        10,
        description="How many automatic pre-write backups (DATA_DIR/backups/<timestamp>/<service>.json, one per run, skipped in dry-run) to keep before the oldest are deleted (rework.md C14). 0 keeps none",
    )
    create_on_garmin: bool = Field(
        False,
        description="Default for the implicit Garmin<->Divelogs pair, and the fallback for any pair without its own override: a new dive found only on the other side is uploaded to create a matching Garmin dive (rework.md C16). Off by default - Garmin dive creation from another source is opt-in. Matched-dive field updates are never affected by this switch",
    )
    garmin_fit_upload: bool = Field(
        False,
        description="Beta, off by default (plans/gap-roadmap.md R13): a new dive created on Garmin is uploaded as a .fit file written from the dive (profile, tanks, gases and the computer's channels arrive in Connect; what a .fit cannot hold is set through the activity afterwards or named in the log) instead of posted as a manual activity with the summary only. A dive without a profile is still posted as a manual activity. Only changes how a dive is created; create_on_garmin decides whether it is",
    )

    @field_validator("notify_on", mode="before")
    @classmethod
    def _known_notify_on(cls, value: Any) -> Any:
        """A missing, empty or unknown ``notify_on`` (a hand-edited
        settings.json, a profile from a newer build) falls back to 'failure'
        rather than making the whole settings file unreadable."""
        if value in NOTIFY_ON_CHOICES:
            return value
        if value not in (None, ""):
            logger.warning("Unknown notify_on %r in the settings; alerts are sent on failure only.", value)
        return "failure"

    @model_validator(mode="before")
    @classmethod
    def _fold_default_pair(cls, data: Any) -> Any:
        """rework.md G1: the top-level ``directionality`` / ``field_links`` (v1)
        describe the once-implicit Garmin <-> Divelogs pair; fold them into
        the explicit ``garmin_divelogs`` entry, creating it when missing. Given
        for the pair *and* at top level, the top level wins (it is what the
        status page's default form and both boards' "default" entry post)."""
        if not isinstance(data, dict):
            return data
        # plans/subsurface-submersion.md F18: pairs and jobs with a Submersion
        # side are dropped (ConfigManager.load_settings does it first and
        # saves; this catches a profile or an API payload).
        retired = drop_retired_services(data)
        if retired:
            _log_retired_services("Settings", retired)
        _migrate_direction(data, DEFAULT_RUN_DIRECTION, "directionality")
        legacy_links = data.pop("field_links", None)
        legacy_direction = data.pop("directionality", None)
        pairs = list(data.get("sync_pairs") or [])
        index = next((i for i, p in enumerate(pairs)
                      if (p.get("id") if isinstance(p, dict) else getattr(p, "id", None)) == DEFAULT_PAIR_ID), None)
        if index is None:
            pairs.insert(0, {"id": DEFAULT_PAIR_ID, "source": "garmin", "target": "divelogs",
                             "directionality": legacy_direction or DEFAULT_RUN_DIRECTION,
                             "field_links": legacy_links if legacy_links is not None
                             else [l.model_dump() for l in default_field_links()]})
        elif legacy_links is not None or legacy_direction:
            entry = pairs[index]
            entry = entry.model_dump() if isinstance(entry, SyncPairModel) else dict(entry)
            if legacy_links is not None:
                entry.pop("rules", None)
                entry.pop("match_keys", None)
                entry["field_links"] = legacy_links
            if legacy_direction:
                entry["directionality"] = legacy_direction
            pairs[index] = entry
        data["sync_pairs"] = pairs
        return data

    def default_pair(self) -> SyncPairModel:
        return next(p for p in self.sync_pairs if p.id == DEFAULT_PAIR_ID)

    def pair_for(self, source_id: str, target_id: str) -> Optional[SyncPairModel]:
        """The first pair whose service ids are exactly (source, target)."""
        return next((p for p in self.sync_pairs
                     if (p.source_service, p.target_service) == (source_id, target_id)), None)

    # Compatibility views over the garmin_divelogs pair (rework.md G1/G4)
    @property
    def directionality(self) -> str:
        return self.default_pair().directionality

    @directionality.setter
    def directionality(self, value: str) -> None:
        self.default_pair().directionality = value

    @property
    def field_links(self) -> List[FieldLink]:
        return self.default_pair().effective_field_links()

    @field_links.setter
    def field_links(self, links: List[FieldLink]) -> None:
        self.default_pair().field_links = list(links)

class GarminCredentials(BaseModel):
    username: str = ""
    password: str = ""
    # Blank: the account's own garmin/<account>/tokens folder (layout.py).
    # A relative folder is under DATA_DIR, an absolute one used as is.
    token_dir: str = ""

class DivelogsCredentials(BaseModel):
    username: str = ""
    password: str = ""

class SubsurfaceCredentials(BaseModel):
    """Subsurface Cloud: a git repository over HTTPS. The repo path and the
    branch are both the account email; login is HTTP basic auth."""
    email: str = ""
    password: str = ""
    base_url: str = Field("https://cloud.subsurface-divelog.org/", description="Cloud server; the generic host picks the nearest mirror")

    @property
    def configured(self) -> bool:
        return bool(self.email and self.password)

class ShearwaterCredentials(BaseModel):
    """One Shearwater app account's database (rework.md Track H). Not a
    login: the desktop app keeps one SQLite file per account on the computer
    it runs on (``users/<email>/dive_data.db``), and dive_sync reads and
    writes metadata in that file. Several of these are several accounts,
    like Subsurface Cloud's: the Sync page picks one per run. With none
    saved, the app's active account on this computer is used."""
    database: str = Field("", description="Path of the account's dive_data.db (relative to DATA_DIR when not absolute)")
    account: str = Field("", description="Label shown in pickers; blank = the account folder's name (the email)")

    @property
    def name(self) -> str:
        """The account as pickers and state files name it: the label, else
        the database's folder (the app names it after the email)."""
        if self.account.strip():
            return self.account.strip()
        from src.core.services.shearwater import account_of_path
        path = self._path()
        return account_of_path(path) if path else ""

    def _path(self) -> Optional[str]:
        if not self.database.strip():
            return None
        path = self.database.strip()
        return path if os.path.isabs(path) else os.path.join(os.environ.get("DATA_DIR", "."), path)

    def resolved_path(self) -> Optional[str]:
        """The file, or None when the path is blank or does not exist."""
        path = self._path()
        return path if path and os.path.isfile(path) else None

    @property
    def configured(self) -> bool:
        return self.resolved_path() is not None


class SsiCredentials(BaseModel):
    """One MySSI account (plans/myssi-sync.md): email, password and the
    current API token, kept so a later run reuses the token instead of
    logging in again. The desktop app keeps these in the OS keychain
    (desktop/credentials.py); credentials.json holds them for the CLI and
    the web dashboard, as a list (several accounts, one picked per run) or
    the single form."""
    email: str = ""
    password: str = ""
    token: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.email.strip() and self.password)


class CredentialsModel(BaseModel):
    garmin: Union[List[GarminCredentials], GarminCredentials] = Field(default_factory=GarminCredentials)
    divelogs: Union[List[DivelogsCredentials], DivelogsCredentials] = Field(default_factory=DivelogsCredentials)
    # A list in the desktop app (rework.md E19: several accounts, one picked
    # per page); the web UI and Docker keep writing the single-account form.
    subsurface: Union[List[SubsurfaceCredentials], SubsurfaceCredentials] = Field(default_factory=SubsurfaceCredentials)
    # A list (several Shearwater accounts, one picked per run, rework.md
    # Track H) or the single form the web UI writes.
    shearwater: Union[List[ShearwaterCredentials], ShearwaterCredentials] = Field(default_factory=list)
    # MySSI accounts (plans/myssi-sync.md K1): a list, or the single form.
    ssi: Union[List[SsiCredentials], SsiCredentials] = Field(default_factory=list)

    def get_ssi_accounts(self) -> List[SsiCredentials]:
        """The MySSI accounts with an email, in either shape."""
        entries = self.ssi if isinstance(self.ssi, list) else [self.ssi]
        return [a for a in entries if a.email.strip()]

    def saved_shearwater_accounts(self) -> List[ShearwaterCredentials]:
        """The entries with a path, whether or not the file exists."""
        entries = self.shearwater if isinstance(self.shearwater, list) else [self.shearwater]
        return [a for a in entries if a.database.strip()]

    def get_shearwater_accounts(self) -> List[ShearwaterCredentials]:
        """The usable Shearwater databases: the saved ones whose file exists,
        or, with none saved at all, the app's active account on this
        computer (find_live_database) so a fresh install needs no setup."""
        saved = self.saved_shearwater_accounts()
        if saved:
            return [a for a in saved if a.configured]
        from src.core.services.shearwater import find_live_database
        live = find_live_database()
        return [ShearwaterCredentials(database=live)] if live else []

    def configured_services(self) -> List[str]:
        """Service ids that have usable credentials (or, for Shearwater, a
        database file), for status displays."""
        out = []
        if any(a.username for a in self.get_garmin_accounts()):
            out.append("garmin")
        if any(a.username for a in self.get_divelogs_accounts()):
            out.append("divelogs")
        if any(a.configured for a in self.get_subsurface_accounts()):
            out.append("subsurface")
        if self.get_shearwater_accounts():
            out.append("shearwater")
        if any(a.configured for a in self.get_ssi_accounts()):
            out.append("ssi")
        return out

    def configured_specs(self) -> List[str]:
        """What the pair, board and endpoint builders (pairs.board_pairs,
        pairs.sync_endpoints) take. Every configured service goes by its id
        (Shearwater's account, i.e. its file, is picked per run like a
        Subsurface Cloud account); the builders also accept a full spec such
        as ``shearwater:<path>`` for a file named by hand."""
        return list(self.configured_services())

    def get_garmin_accounts(self) -> List[GarminCredentials]:
        if isinstance(self.garmin, list):
            return self.garmin
        if getattr(self.garmin, "username", None):
            return [self.garmin]
        return []

    def get_divelogs_accounts(self) -> List[DivelogsCredentials]:
        if isinstance(self.divelogs, list):
            return self.divelogs
        if getattr(self.divelogs, "username", None):
            return [self.divelogs]
        return []

    def get_subsurface_accounts(self) -> List[SubsurfaceCredentials]:
        if isinstance(self.subsurface, list):
            return [a for a in self.subsurface if a.email]
        if self.subsurface.email:
            return [self.subsurface]
        return []

    def first_subsurface_account(self) -> SubsurfaceCredentials:
        """The single account the web UI and setup_credentials.py work with."""
        accounts = self.get_subsurface_accounts()
        return accounts[0] if accounts else SubsurfaceCredentials()

class ConfigManager:
    @staticmethod
    def load_settings(path: str = SETTINGS_FILE) -> SettingsModel:
        if not os.path.exists(path):
            defaults = SettingsModel()
            ConfigManager.save_settings(defaults, path)
            return defaults
        with open(path, "r") as f:
            try:
                data = json.load(f)
                version = data.get("settings_version") if isinstance(data, dict) else None
                has_default_pair = any(isinstance(p, dict) and p.get("id") == DEFAULT_PAIR_ID
                                       for p in (data.get("sync_pairs") or []))
                if version is None and "field_links" not in data and not has_default_pair:
                    # rework.md C12: a settings.json old enough to predate
                    # field_links entirely gets the board it would have had
                    # back then, not today's default - which now differs
                    # (manual vs. prefer_non_empty) - so loading it doesn't
                    # silently change what happens on the next sync.
                    from src.core.fields import pre_c12_default_field_links
                    data["field_links"] = [link.model_dump() for link in pre_c12_default_field_links()]
                # rework.md G0: 'bidirectional' is no longer a run mode. Rewrite
                # it here, say so once, and save the file back so the next load
                # is silent.
                migrated = migrate_run_directions(data)
                # plans/subsurface-submersion.md F18: Submersion's pairs and
                # jobs go, said once, and the file is saved without them.
                retired = drop_retired_services(data)
                # rework.md G1: link boards become receiver rules on the explicit
                # garmin_divelogs pair; rewritten once, then saved as version 2.
                legacy_board = ("field_links" in data or "directionality" in data
                                or any(isinstance(p, dict) and "field_links" in p for p in (data.get("sync_pairs") or []))
                                or (version or 1) < SETTINGS_VERSION)
                settings = SettingsModel.model_validate(data)
                if migrated:
                    logger.warning(
                        "Settings file %s used the retired 'bidirectional' run direction; a run now writes one side "
                        "only and a two-way sync is two directed runs (rework.md G0). Rewritten and saved: %s",
                        path, "; ".join(migrated),
                    )
                if legacy_board:
                    logger.info(
                        "Settings file %s upgraded to version %d: the mapping board is now stored as receiver rules on "
                        "sync_pairs[].rules, and the Garmin <-> Divelogs pair is the explicit '%s' entry (rework.md G1).",
                        path, SETTINGS_VERSION, DEFAULT_PAIR_ID,
                    )
                if retired:
                    _log_retired_services(f"Settings file {path}", retired)
                if migrated or legacy_board or retired:
                    ConfigManager.save_settings(settings, path)
                return settings
            except Exception as e:
                logger.warning("Settings file %s could not be read (%s); using defaults for this run.", path, e)
                return SettingsModel()

    @staticmethod
    def save_settings(settings: SettingsModel, path: str = SETTINGS_FILE) -> None:
        with open(path, "w") as f:
            json.dump(settings.model_dump(), f, indent=2)

    @staticmethod
    def load_credentials(path: str = CREDENTIALS_FILE) -> CredentialsModel:
        if not os.path.exists(path):
            defaults = CredentialsModel()
            ConfigManager.save_credentials(defaults, path)
            return defaults
        with open(path, "r") as f:
            try:
                data = json.load(f)
                return CredentialsModel.model_validate(data)
            except Exception as e:
                logger.warning("Credentials file %s could not be read (%s); using empty credentials.", path, e)
                return CredentialsModel()

    @staticmethod
    def save_credentials(credentials: CredentialsModel, path: str = CREDENTIALS_FILE) -> None:
        with open(path, "w") as f:
            json.dump(credentials.model_dump(), f, indent=2)

    @staticmethod
    def store_ssi_token(email: str, token: str, path: str = CREDENTIALS_FILE) -> bool:
        """Keep the API token MySSI handed out for the ``ssi`` account
        ``email`` (plans/myssi-sync.md K2: `SsiClient`'s ``on_token`` hook,
        wired by pairs.build_adapter), so the next run reuses it instead of
        logging in again. Only an existing account in an existing file is
        touched (the desktop app materialises the file for one operation
        and reads the token back into the keychain afterwards); True when
        the file was written."""
        if not os.path.exists(path):
            return False
        credentials = ConfigManager.load_credentials(path)
        email = (email or "").strip()
        entries = credentials.ssi if isinstance(credentials.ssi, list) else [credentials.ssi]
        account = next((a for a in entries if a.email.strip() == email), None)
        if account is None or account.token == (token or ""):
            return False
        account.token = token or ""
        ConfigManager.save_credentials(credentials, path)
        return True


# ---------------------------------------------------------------------------
# Portable sync profile (rework.md Track C, step C9)
# ---------------------------------------------------------------------------

PROFILE_VERSION = 2
PROFILE_KEY = "dive_sync_profile"
# Sections a profile may carry, in the order they are written. Credentials
# live in CredentialsModel and can never end up here by construction.
# Version 2 (rework.md G1): the board and direction of the Garmin <-> Divelogs
# pair live in sync_pairs like every other pair's; a version-1 profile's
# top-level ``directionality`` / ``field_links`` are still imported and folded
# into that pair (LEGACY_PROFILE_SECTIONS).
PROFILE_SECTIONS = (
    "sync_filters",
    "grace_window_minutes",
    "api_cooldown_seconds",
    "garmin_timezone",
    "sync_pairs",
    "schedule",
    "cron_jobs",
    "notify_url",
    "notify_on",
    "propagate_deletes",
    "backup_retention_count",
    "create_on_garmin",
    "garmin_fit_upload",
)
LEGACY_PROFILE_SECTIONS = ("directionality", "field_links")


class ProfileError(ValueError):
    """The profile cannot be imported; the message is meant for the user."""


class ProfileImportSummary(BaseModel):
    version: int
    sections: List[str] = Field(default_factory=list, description="Sections the profile replaced")
    ignored_keys: List[str] = Field(default_factory=list, description="Top-level keys this build does not know")
    skipped_links: List[str] = Field(default_factory=list, description="Link ids dropped because they reference unknown fields")
    changes: List[str] = Field(default_factory=list, description="Human-readable diff against the current settings")

    def as_text(self) -> str:
        lines = [f"Profile version {self.version}"]
        lines += [f"  {c}" for c in self.changes] or ["  no changes"]
        if self.skipped_links:
            lines.append(f"  skipped links (unknown fields): {', '.join(self.skipped_links)}")
        if self.ignored_keys:
            lines.append(f"  ignored keys: {', '.join(self.ignored_keys)}")
        return "\n".join(lines)


def export_profile(settings: SettingsModel) -> Dict[str, Any]:
    """Everything in SettingsModel, versioned. Nothing secret can be here."""
    dump = settings.model_dump(mode="json")
    profile: Dict[str, Any] = {PROFILE_KEY: PROFILE_VERSION}
    for section in PROFILE_SECTIONS:
        profile[section] = dump[section]
    return profile


def _describe_rules(pair_id: str, old: Optional[Dict[str, List[SyncRule]]],
                    new: Optional[Dict[str, List[SyncRule]]]) -> Optional[str]:
    """One line per pair whose rules differ: '<pair>: N -> M rules; added ...'.
    Rule ids are qualified by receiver ('divelogs.buddy' reads 'buddy on divelogs')."""
    def flat(rules):
        return {f"{receiver}:{r.id}": r for receiver, items in (rules or {}).items() for r in items}
    old_by, new_by = flat(old), flat(new)
    added = [i for i in new_by if i not in old_by]
    removed = [i for i in old_by if i not in new_by]
    changed = [i for i in new_by if i in old_by and new_by[i] != old_by[i]]
    if not (added or removed or changed) and (old is None) == (new is None):
        return None
    what = "defaults" if new is None else f"{len(new_by)} rules"
    was = "defaults" if old is None else f"{len(old_by)} rules"
    parts = [f"rules[{pair_id}]: {was} -> {what}"]
    if added:
        parts.append(f"added {', '.join(added)}")
    if removed:
        parts.append(f"removed {', '.join(removed)}")
    if changed:
        parts.append(f"changed {', '.join(changed)}")
    return "; ".join(parts)


def _describe_pairs(old: List[SyncPairModel], new: List[SyncPairModel]) -> List[str]:
    old_by, new_by = {p.id: p for p in old}, {p.id: p for p in new}
    lines: List[str] = []
    added = [i for i in new_by if i not in old_by]
    removed = [i for i in old_by if i not in new_by]
    if added or removed:
        lines.append("sync_pairs: " + "; ".join(
            part for part in (f"added {', '.join(added)}" if added else "", f"removed {', '.join(removed)}" if removed else "") if part))
    for pair_id, pair in new_by.items():
        before = old_by.get(pair_id)
        if before is None:
            continue
        rules_line = _describe_rules(pair_id, before.rules, pair.rules)
        if rules_line:
            lines.append(rules_line)
        if before.match_keys != pair.match_keys:
            lines.append(f"match_keys[{pair_id}]: {before.match_keys} -> {pair.match_keys}")
        for attr in ("source", "target", "directionality", "enabled", "grace_window_minutes",
                     "propagate_deletes", "create_on_garmin", "garmin_fit_upload"):
            if getattr(before, attr) != getattr(pair, attr):
                lines.append(f"sync_pairs[{pair_id}].{attr}: {getattr(before, attr)!r} -> {getattr(pair, attr)!r}")
    return lines


def _filter_unknown_fields(rules: Optional[Dict[str, Any]], catalog: Dict[str, Any], skipped: List[str]) -> Optional[Dict[str, Any]]:
    """Drop rules that reference fields not in ``catalog``; list their ids."""
    if not isinstance(rules, dict):
        return rules
    kept: Dict[str, Any] = {}
    for receiver, items in rules.items():
        out = []
        for item in items or []:
            keys = (list(item.get("source", [])) + [item.get("target")]) if isinstance(item, dict) else []
            if any(k not in catalog for k in keys):
                skipped.append(str(item.get("id", "?")) if isinstance(item, dict) else "?")
            else:
                out.append(item)
        kept[receiver] = out
    return kept


def import_profile(data: Dict[str, Any], current: SettingsModel,
                   catalog: Optional[Dict[str, Any]] = None) -> "tuple[SettingsModel, ProfileImportSummary]":
    """Build new settings from ``current`` with every section present in
    ``data`` replaced wholesale. Refuses a newer profile version. Links that
    reference fields not in ``catalog`` (when given) are skipped and listed.
    Returns the new settings and a summary; nothing is written."""
    if not isinstance(data, dict) or PROFILE_KEY not in data:
        raise ProfileError("This file is not a Dive Sync profile (missing the 'dive_sync_profile' version field).")
    version = data[PROFILE_KEY]
    if not isinstance(version, int) or isinstance(version, bool):
        raise ProfileError(f"Profile version must be a whole number, got {version!r}.")
    if version > PROFILE_VERSION:
        raise ProfileError(
            f"This profile was written by a newer Dive Sync (profile version {version}); "
            f"this build reads version {PROFILE_VERSION}. Update Dive Sync to import it."
        )

    summary = ProfileImportSummary(version=version)
    merged = current.model_dump(mode="json")
    for key in data:
        if key != PROFILE_KEY and key not in PROFILE_SECTIONS and key not in LEGACY_PROFILE_SECTIONS:
            summary.ignored_keys.append(key)

    garmin_divelogs_ids = {"garmin", "divelogs"}
    for section in PROFILE_SECTIONS + LEGACY_PROFILE_SECTIONS:
        if section not in data:
            continue
        value = data[section]
        if section == "field_links" and catalog is not None and isinstance(value, list):
            # v1: the Garmin <-> Divelogs board as links
            kept = []
            for item in value:
                keys = list(item.get("source", [])) + [item.get("target")] if isinstance(item, dict) else []
                if any(k not in catalog for k in keys):
                    summary.skipped_links.append(str(item.get("id", "?")) if isinstance(item, dict) else "?")
                else:
                    kept.append(item)
            value = kept
        elif section == "sync_pairs" and catalog is not None and isinstance(value, list):
            # v2: rules of the Garmin <-> Divelogs pair (the catalogue given is that pair's)
            value = [dict(p) if isinstance(p, dict) else p for p in value]
            for pair in value:
                if isinstance(pair, dict) and {service_id_of_spec(pair.get("source", "")),
                                               service_id_of_spec(pair.get("target", ""))} == garmin_divelogs_ids:
                    if "rules" in pair:
                        pair["rules"] = _filter_unknown_fields(pair["rules"], catalog, summary.skipped_links)
        if section in LEGACY_PROFILE_SECTIONS:
            # Folded into the garmin_divelogs pair by SettingsModel; a v1 profile
            # without a sync_pairs section keeps the current other pairs.
            merged[section] = value
            if "sync_pairs" not in summary.sections:
                summary.sections.append("sync_pairs")
            continue
        merged[section] = value
        summary.sections.append(section)

    try:
        new_settings = SettingsModel.model_validate(merged)
    except ValidationError as e:
        raise ProfileError(f"The profile contains invalid values: {e}") from e

    for section in summary.sections:
        old_val = getattr(current, section)
        new_val = getattr(new_settings, section)
        if section == "sync_pairs":
            summary.changes.extend(_describe_pairs(old_val, new_val))
        elif section in ("cron_jobs", "schedule"):
            if old_val != new_val:
                summary.changes.append(f"{section}: {len(old_val)} -> {len(new_val)} entries")
        elif section == "sync_filters":
            for name, old_f in old_val.model_dump().items():
                new_f = new_val.model_dump()[name]
                if old_f != new_f:
                    summary.changes.append(f"sync_filters.{name}: {old_f!r} -> {new_f!r}")
        elif old_val != new_val:
            summary.changes.append(f"{section}: {old_val!r} -> {new_val!r}")
    return new_settings, summary


def write_profile(settings: SettingsModel, path: str) -> None:
    with open(path, "w") as f:
        json.dump(export_profile(settings), f, indent=2)


def read_profile(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        raise ProfileError(f"Profile file not found: {path}")
    except json.JSONDecodeError as e:
        raise ProfileError(f"Profile file {path} is not valid JSON: {e}")
