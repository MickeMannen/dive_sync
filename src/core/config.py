import os
import json
import logging
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field, ValidationError

from src.core.fields import FieldLink, default_field_links

logger = logging.getLogger("dive_sync.config")

SETTINGS_FILE = os.path.join(os.environ.get("DATA_DIR", "."), "settings.json")
CREDENTIALS_FILE = os.path.join(os.environ.get("DATA_DIR", "."), "credentials.json")

class SyncFilters(BaseModel):
    date_from: Optional[str] = Field(None, description="Sync start date, format YYYY-MM-DD")
    date_to: Optional[str] = Field(None, description="Sync end date, format YYYY-MM-DD")
    only_new: bool = Field(True, description="Sync only new dives since last run")
    sync_gases: bool = Field(True, description="Sync detailed gas mixtures (alias for 'the tanks links are not off')")
    # rework.md E6: no UI exposes this any more (it never did anything for a
    # regular sync - only SyncEngine.backup() reads it, for the CLI --backup
    # FIT-file extraction); still settable by hand in settings.json or via
    # POST /api/settings for that one purpose.
    sync_fit: bool = Field(False, description="Include FIT files in a --backup export")

class SyncScheduleSlot(BaseModel):
    hour: int = Field(..., ge=0, le=23)
    minute: int = Field(..., ge=0, le=59)

class CronJobModel(BaseModel):
    id: str = Field(..., description="Unique ID for this job")
    directionality: str = Field("bidirectional", description="bidirectional, to_divelogs, to_garmin")
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
    garmin_username: Optional[str] = Field(None, description="Which configured Garmin account to use; None picks the only one, or errors if several are configured")
    divelogs_username: Optional[str] = Field(None, description="Which configured Divelogs account to use; None picks the only one, or errors if several are configured")

class SyncPairModel(BaseModel):
    """One source/target pair the engine can run (rework.md F3). A service
    spec is a service id, optionally with an argument after a colon:
    ``garmin``, ``divelogs``, ``uddf:<file>``, ``subsurface:<directory>``.
    Relative paths resolve against DATA_DIR."""
    id: str = Field(..., description="Unique name of the pair, used by --pair and by cron jobs")
    source: str = Field("garmin", description="Service spec of side A")
    target: str = Field("divelogs", description="Service spec of side B")
    directionality: str = Field("bidirectional", description="bidirectional, to_<service id>, to_source, to_target")
    enabled: bool = True
    grace_window_minutes: Optional[int] = Field(None, description="Per-pair override of the matching window")
    field_links: Optional[List[FieldLink]] = Field(None, description="Per-pair board; None = the defaults for this pair")
    propagate_deletes: Optional[bool] = Field(None, description="Per-pair override of whether a dive deleted on one side is deleted on the other (rework.md C13); None = the global default")

class SettingsModel(BaseModel):
    directionality: str = Field("bidirectional", description="bidirectional, to_divelogs, to_garmin")
    sync_filters: SyncFilters = Field(default_factory=SyncFilters)
    grace_window_minutes: int = Field(15, description="Matching grace window in minutes")
    api_cooldown_seconds: float = Field(1.0, description="Cool-down delay in seconds between API requests")
    schedule: List[SyncScheduleSlot] = Field(default_factory=list, description="Cron-like multi-slot schedule")
    cron_jobs: List[CronJobModel] = Field(default_factory=list, description="List of configured cron jobs")
    field_links: List[FieldLink] = Field(
        default_factory=default_field_links,
        description="The mapping board: which field feeds which, in what direction, with what conflict policy",
    )
    garmin_timezone: Optional[str] = Field(
        None,
        description="IANA zone to stamp on dives uploaded to Garmin Connect; empty = detect from the account's newest dive",
    )
    sync_pairs: List[SyncPairModel] = Field(
        default_factory=list,
        description="Named source/target pairs beyond the implicit Garmin -> Divelogs one",
    )
    notify_url: Optional[str] = Field(
        None,
        description="Webhook (ntfy / Gotify / generic) POSTed a short plain-text message when a scheduled or manual sync run fails; empty disables alerts (rework.md A11)",
    )
    propagate_deletes: bool = Field(
        False,
        description="Default for the implicit Garmin<->Divelogs pair, and the fallback for any pair without its own override: delete the linked dive on the other side when one is deleted here (rework.md C13). Only checked during a full (non-incremental) sync - an incremental run's date-limited fetch cannot tell 'deleted' apart from 'outside this run's window', so deletion detection is skipped there entirely, on or off",
    )
    backup_retention_count: int = Field(
        10,
        description="How many automatic pre-write backups (DATA_DIR/backups/<timestamp>/<service>.json, one per run, skipped in dry-run) to keep before the oldest are deleted (rework.md C14). 0 keeps none",
    )

class GarminCredentials(BaseModel):
    username: str = ""
    password: str = ""
    token_dir: str = "tokens/garmin"

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

class SubmersionCredentials(BaseModel):
    """Submersion sync store. Only S3-compatible stores (Backblaze B2, Cloudflare
    R2, Garage, ...) are supported for unattended sync; a local folder store
    (Dropbox / iCloud folder) is a later, desktop-only option."""
    store_type: str = Field("s3", description="'s3' or 'folder'")
    endpoint_url: str = Field("", description="S3 endpoint, e.g. https://s3.eu-central-003.backblazeb2.com")
    region: str = Field("", description="S3 region, e.g. eu-central-003 for Backblaze B2")
    bucket: str = ""
    prefix: str = Field("submersion-sync/", description="Key prefix Submersion writes its ssv1.* files under")
    access_key_id: str = Field("", description="B2: the application key id")
    secret_access_key: str = Field("", description="B2: the application key")
    path_style: bool = Field(False, description="Use path-style addressing (needed by some self-hosted stores)")
    folder_path: str = Field("", description="store_type 'folder': the synced folder on this machine")

    @property
    def configured(self) -> bool:
        if self.store_type == "folder":
            return bool(self.folder_path)
        return bool(self.endpoint_url and self.bucket and self.access_key_id and self.secret_access_key)

class CredentialsModel(BaseModel):
    garmin: Union[List[GarminCredentials], GarminCredentials] = Field(default_factory=GarminCredentials)
    divelogs: Union[List[DivelogsCredentials], DivelogsCredentials] = Field(default_factory=DivelogsCredentials)
    subsurface: SubsurfaceCredentials = Field(default_factory=SubsurfaceCredentials)
    submersion: SubmersionCredentials = Field(default_factory=SubmersionCredentials)

    def configured_services(self) -> List[str]:
        """Service ids that have usable credentials, for status displays."""
        out = []
        if any(a.username for a in self.get_garmin_accounts()):
            out.append("garmin")
        if any(a.username for a in self.get_divelogs_accounts()):
            out.append("divelogs")
        if self.subsurface.configured:
            out.append("subsurface")
        if self.submersion.configured:
            out.append("submersion")
        return out

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
                if "field_links" not in data:
                    # rework.md C12: a settings.json old enough to predate
                    # field_links entirely gets the board it would have had
                    # back then, not today's default - which now differs
                    # (manual vs. prefer_non_empty) - so loading it doesn't
                    # silently change what happens on the next sync.
                    from src.core.fields import pre_c12_default_field_links
                    data["field_links"] = [link.model_dump() for link in pre_c12_default_field_links()]
                return SettingsModel.model_validate(data)
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


# ---------------------------------------------------------------------------
# Portable sync profile (rework.md Track C, step C9)
# ---------------------------------------------------------------------------

PROFILE_VERSION = 1
PROFILE_KEY = "dive_sync_profile"
# Sections a profile may carry, in the order they are written. Credentials
# live in CredentialsModel and can never end up here by construction.
PROFILE_SECTIONS = (
    "directionality",
    "sync_filters",
    "grace_window_minutes",
    "api_cooldown_seconds",
    "field_links",
    "garmin_timezone",
    "sync_pairs",
    "schedule",
    "cron_jobs",
    "notify_url",
    "propagate_deletes",
    "backup_retention_count",
)


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


def _describe_links(old: List[FieldLink], new: List[FieldLink]) -> Optional[str]:
    old_by, new_by = {l.id: l for l in old}, {l.id: l for l in new}
    added = [i for i in new_by if i not in old_by]
    removed = [i for i in old_by if i not in new_by]
    changed = [i for i in new_by if i in old_by and new_by[i] != old_by[i]]
    if not (added or removed or changed):
        return None
    parts = [f"field_links: {len(old)} -> {len(new)} links"]
    if added:
        parts.append(f"added {', '.join(added)}")
    if removed:
        parts.append(f"removed {', '.join(removed)}")
    if changed:
        parts.append(f"changed {', '.join(changed)}")
    return "; ".join(parts)


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
        if key != PROFILE_KEY and key not in PROFILE_SECTIONS:
            summary.ignored_keys.append(key)

    for section in PROFILE_SECTIONS:
        if section not in data:
            continue
        value = data[section]
        if section == "field_links" and catalog is not None and isinstance(value, list):
            kept = []
            for item in value:
                keys = list(item.get("source", [])) + [item.get("target")] if isinstance(item, dict) else []
                if any(k not in catalog for k in keys):
                    summary.skipped_links.append(str(item.get("id", "?")) if isinstance(item, dict) else "?")
                else:
                    kept.append(item)
            value = kept
        merged[section] = value
        summary.sections.append(section)

    try:
        new_settings = SettingsModel.model_validate(merged)
    except ValidationError as e:
        raise ProfileError(f"The profile contains invalid values: {e}") from e

    for section in summary.sections:
        old_val = getattr(current, section)
        new_val = getattr(new_settings, section)
        if section == "field_links":
            line = _describe_links(old_val, new_val)
            if line:
                summary.changes.append(line)
        elif section in ("cron_jobs", "schedule", "sync_pairs"):
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
