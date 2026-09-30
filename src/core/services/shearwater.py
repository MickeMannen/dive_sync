"""Shearwater app adapter (rework.md Track H, features.md B21).

The Shearwater app - Shearwater's "Shearwater Cloud" desktop program (macOS/
Windows, Unity); DiveSync calls it the Shearwater app because DiveSync only
ever touches the app's local database, never the cloud - keeps one SQLite file per
account, ``dive_data.db``. This adapter reads it as a dive service and
writes metadata back the way the app does, so the app uploads it to
Shearwater Cloud on its next sync. It never adds or deletes dives there: a
dive is a computer download with a profile blob and cloud bookkeeping that
nothing else can produce.

Writing (``update_dive``), copied from a diff of an edit made in the app
(rework.md Track H, 2026-09-29): the column gets the text in the app's own
format, ``dive_details.LastModified`` and the dive's
``SyncV3MetadataDiveDetail.LastModifiedServerTime`` are set to the same UTC
instant and ``FieldTimeStampJson[<column>]`` to that instant in
milliseconds, one stamp per column touched. The app's sync planner then
reads the dive as changed locally and uploads it. The app must be closed
while this happens (plain rollback journal, and the app caches rows): a
write is refused while its journal file exists or its process is running,
and the file is copied to ``DATA_DIR/backups/shearwater/`` first.

Tables read (see rework.md Track H for the full anatomy):

    dive_details       one row per dive, ``DiveId`` primary key (stable):
                       the computer's summary and every editable column
    log_data           ``log_id`` = ``DiveId``; ``calculated_values_from_samples``
                       is a JSON with the average depth and temperatures
                       (the ``dive_details`` copies of those are 0.0)

Mapping (column -> UnifiedDive):

    DiveDate                    date_time (local, naive - like Garmin's)
    DiveLengthTime              duration (s)
    Depth                       max_depth (m, text)
    calculated AverageDepth     avg_depth
    calculated MinTemp/MaxTemp  temp_min / temp_max (°C)
    DiveNumber                  dive_number
    Site                        location   (decided 2026-09-29: Site is the
                                            site name; Location is the wider
                                            area, kept as a service field)
    Buddy / Notes / Weight      buddy / notes / weight
    GnssEntryLocation           lat, lng   (format guessed: "lat,lng")
    TankProfileData + Tank<n>PressureStart/End   gas_mixtures (PSI -> bar)
    every other editable column service_fields[<name>], COLUMNS below

On the board the fields carry the app's own words in snake case:
``shearwater.site`` is the app's Site (the unified location) and
``shearwater.location`` its Location (the area), ``shearwater.environment``
its Environment, and so on - one spelling per field (2026-09-30: the pair
``shearwater.Location`` / ``shearwater.location`` confused the owner).

Units in the file: depth and duration metric/SI as text, tank pressures
**PSI** as text, temperatures °C. The profile samples live only inside the
computer's native log (``log_data.data_bytes_1``, gzip after a 4-byte
prefix), decoded by ``shearwater_log.py`` (rework.md H5): every sample the
computer logged, with depth, temperature, time and the first transmitter's
pressure; a log that cannot be decoded gives a dive without a profile and
one warning, never a failed load.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.core.adapter import BaseDiveAdapter
from src.core.fields import FieldSpec
from src.core.models import GasMixture, UnifiedDive, UnifiedSample
from src.core.services.shearwater_log import ShearwaterLogError, decode_profile

logger = logging.getLogger("dive_sync.shearwater")

SERVICE_ID = "shearwater"
PSI_PER_BAR = 14.5037738
KG_PER_LB = 0.45359237
BACKUPS_TO_KEEP = 10
APP_PROCESS_NAME = "Shearwater Cloud"
# .NET DateTime.MinValue in ms since the epoch: "never set" in FieldTimeStampJson
DOTNET_MIN_MS = -62135596800000

# Editable columns that have no UnifiedDive attribute: board name (snake
# case, the app's own word) -> column. Read into service_fields under the
# board name, addressed on the board as shearwater.<name>.
COLUMNS: Dict[str, str] = {
    "location": "Location", "environment": "Environment", "environment_notes": "EnvironmentNotes",
    "visibility": "Visibility", "weather": "Weather", "conditions": "Conditions", "platform": "Platform",
    "air_temperature": "AirTemperature", "gas_notes": "GasNotes", "tank_size": "TankSize", "gear_notes": "GearNotes",
    "dress": "Dress", "apparatus": "Apparatus", "thermal_comfort": "ThermalComfort", "workload": "Workload",
    "problems": "Problems", "malfunctions": "Malfunctions", "symptoms": "Symptoms",
    "exposure_to_altitude": "ExposureToAltitude", "other1": "Other1", "other2": "Other2", "other3": "Other3",
}
NAME_OF_COLUMN = {column: name for name, column in COLUMNS.items()}
# Dropdown columns store the option's English label.
DROPDOWN_OPTIONS: Dict[str, List[str]] = {
    # Proposed from the app's option vocabulary (rework.md Track H); the
    # owner confirms the grouping against the app's dropdowns (H3).
    "Environment": ["Ocean/Sea", "Lake/Quarry", "River/Spring", "Cave/Cavern", "Pool", "Under Ice", "Chamber", "Other"],
    "Platform": ["Beach/Shore", "Charter boat", "Small Boat", "Live-aboard", "Barge", "Pier", "Landside",
                 "Hyperbaric Facility", "Other"],
    "Weather": ["Sunny", "Cloudy", "Rainy", "Foggy", "Windy", "Night"],
    "Conditions": ["Waves", "Surge", "Current", "Cold", "Night", "Other"],
    "ThermalComfort": ["Very Cold", "Cold", "Cool", "Warm/Neutral", "Hot", "Very Hot"],
    "Workload": ["Resting", "Light", "Moderate", "Severe", "Exhausting"],
    "Dress": ["Dive Skin", "Wet Suit", "Semi-Dry suit", "Dry Suit", "Hot water suit", "Other"],
    "Apparatus": ["Single Tank", "Doubles", "Sidemount", "Rebreather", "Surface Supplied", "Free", "Experimental", "Other"],
    "Problems": ["No Problem", "Equalization", "Buoyancy", "Out of air", "Shared air", "Rapid ascent", "Sea sickness", "Other"],
    "Malfunctions": ["None", "BC", "Face mask", "Fins", "Depth gauge", "Pressure gauge", "Weight belt",
                     "Breathing apparatus", "Deco reel", "Thermal protection", "Suit buoyancy", "Other"],
    "ExposureToAltitude": ["None", "Commercial aircraft", "Unpressurized aircraft", "MedEvac aircraft", "Helicopter",
                           "Ground transportation"],
}
NUMBER_COLUMNS = ("AirTemperature",)
SERVICE_COLUMNS: Tuple[str, ...] = tuple(COLUMNS.values())
_LABELS = {"environment_notes": "Environment notes", "gas_notes": "Gas notes", "tank_size": "Tank size",
           "gear_notes": "Gear notes", "thermal_comfort": "Thermal comfort", "exposure_to_altitude": "Exposure to altitude",
           "air_temperature": "Air temperature", "location": "Location (the area; Site is the dive site)",
           "other1": "Other 1", "other2": "Other 2", "other3": "Other 3"}


# ---------------------------------------------------------------------------
# Where the app keeps its file
# ---------------------------------------------------------------------------

MAC_CONTAINER = os.path.join("Library", "Containers", "research.shearwater.cloud", "Data", "Library",
                             "Application Support", "research.shearwater.cloud")
# The Windows build's folder has not been checked; these are the usual
# places a desktop app of that name would use.
WINDOWS_CANDIDATES = (os.path.join("Shearwater Research", "Shearwater Cloud"), "research.shearwater.cloud",
                      "Shearwater Cloud")


def _users_dirs() -> List[str]:
    home = os.path.expanduser("~")
    dirs = [os.path.join(home, MAC_CONTAINER, "users")]
    for env in ("LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(env)
        if base:
            dirs += [os.path.join(base, c, "users") for c in WINDOWS_CANDIDATES]
    return dirs


def find_live_databases() -> List[Tuple[str, str]]:
    """Every account the Shearwater app has on this computer,
    as (account email, path of its dive_data.db), the app's active account
    first. Account folders are named after the email; the app's
    ``loadinguser`` placeholder (an empty database) is left out."""
    out: List[Tuple[str, str]] = []
    for users in _users_dirs():
        if not os.path.isdir(users):
            continue
        active = ""
        marker = os.path.join(users, "active_account")
        if os.path.isfile(marker):
            try:
                with open(marker, "r", encoding="utf-8") as f:
                    active = f.read().strip()
            except OSError:
                active = ""
        folders = sorted(d for d in os.listdir(users) if os.path.isdir(os.path.join(users, d)) and "@" in d)
        if active in folders:
            folders.remove(active)
            folders.insert(0, active)
        for folder in folders:
            path = os.path.join(users, folder, "dive_data.db")
            if os.path.isfile(path):
                out.append((folder, path))
    return out


def find_live_database() -> Optional[str]:
    """The app's active account's ``dive_data.db`` on this computer (the
    only account's when no ``active_account`` marker names one), or None."""
    found = find_live_databases()
    return found[0][1] if found else None


def account_of_path(path: str) -> str:
    """The account a database path belongs to: the app names each account's
    folder after the email, so that folder; for a copy elsewhere, the file's
    folder name (or the file name at a root)."""
    folder = os.path.basename(os.path.dirname(os.path.abspath(path)))
    return folder or os.path.basename(path)


# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------

def parse_number(text: Any) -> Optional[float]:
    """A float from the app's text columns; None for blank/None/garbage."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(text))
    return float(m.group(0).replace(",", ".")) if m else None


def psi_to_bar(text: Any) -> Optional[float]:
    psi = parse_number(text)
    return round(psi / PSI_PER_BAR, 2) if psi else None


def bar_to_psi_text(bar: Optional[float]) -> str:
    return "" if bar is None else f"{bar * PSI_PER_BAR:.2f}"


def parse_gnss(text: Any) -> Tuple[Optional[float], Optional[float]]:
    """``GnssEntryLocation`` -> (lat, lng). The column's format has not been
    seen filled in yet (rework.md Track H); "lat,lng" / "lat lng" and a JSON
    object with Latitude/Longitude are accepted."""
    if not text:
        return None, None
    s = str(text).strip()
    if s.startswith("{"):
        try:
            d = json.loads(s)
            lat = parse_number(d.get("Latitude", d.get("latitude", d.get("lat"))))
            lng = parse_number(d.get("Longitude", d.get("longitude", d.get("lng", d.get("lon")))))
            return (lat, lng) if lat is not None and lng is not None else (None, None)
        except (ValueError, AttributeError):
            return None, None
    nums = re.findall(r"-?\d+(?:\.\d+)?", s)
    if len(nums) >= 2:
        return float(nums[0]), float(nums[1])
    return None, None


def parse_local(text: Any) -> Optional[datetime]:
    if not text:
        return None
    try:
        return datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return datetime.fromisoformat(str(text))
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Row -> UnifiedDive
# ---------------------------------------------------------------------------

def parse_tanks(row: Dict[str, Any]) -> List[GasMixture]:
    """Gases from ``TankProfileData`` (the app's JSON of gas profiles and the
    four transmitter slots) plus the editable ``Tank<n>PressureStart/End``
    columns, which win over the JSON's pressures when filled in. Slots that
    are switched off and carry no pressure are dropped; without any slot the
    gas profiles alone give the mixes."""
    try:
        data = json.loads(row.get("TankProfileData") or "{}")
    except ValueError:
        data = {}
    out: List[GasMixture] = []
    for i, slot in enumerate(data.get("TankData") or []):
        if not isinstance(slot, dict):
            continue
        profile = slot.get("GasProfile") or {}
        transmitter = slot.get("DiveTransmitter") or {}
        column = f"Tank{i + 1}Pressure" if i < 4 else None
        start = psi_to_bar(row.get(f"{column}Start")) if column else None
        end = psi_to_bar(row.get(f"{column}End")) if column else None
        if start is None:
            start = psi_to_bar(slot.get("StartPressurePSI"))
        if end is None:
            end = psi_to_bar(slot.get("EndPressurePSI"))
        if not transmitter.get("IsOn") and start is None and end is None:
            continue
        out.append(GasMixture(oxygen=float(profile.get("O2Percent", 21) or 21),
                              helium=float(profile.get("HePercent", 0) or 0),
                              start_pressure=start, end_pressure=end,
                              tank_name=transmitter.get("Name") or None))
    if out:
        return out
    seen = set()
    for profile in data.get("GasProfiles") or []:
        mix = (float(profile.get("O2Percent", 21) or 21), float(profile.get("HePercent", 0) or 0))
        if mix in seen:
            continue
        seen.add(mix)
        out.append(GasMixture(oxygen=mix[0], helium=mix[1]))
    return out


LOG_FORMAT = "sw-pnf"


def profile_of(dive_id: Any, log_format: Optional[str], blob: Optional[bytes]) -> List[UnifiedSample]:
    """The dive's profile from its ``log_data`` row, or ``[]`` with one
    warning: another log format, a missing log, a blob the decoder rejects
    (legacy layout, bad gzip, missing records, a freedive log, samples that
    disagree with the header) or any other error. A profile never makes
    loading a dive fail (decision 2026-09-30)."""
    if blob is None:
        logger.warning("Shearwater: dive %s has no log in log_data; no profile", dive_id)
        return []
    if log_format != LOG_FORMAT:
        logger.warning("Shearwater: dive %s: log format %r is not %s; no profile", dive_id, log_format, LOG_FORMAT)
        return []
    try:
        return decode_profile(blob)
    except ShearwaterLogError as e:
        logger.warning("Shearwater: dive %s: no profile - %s", dive_id, e)
    except Exception as e:  # noqa: BLE001 - a profile must never make loading a dive fail
        logger.warning("Shearwater: dive %s: no profile - unexpected %s: %s", dive_id, type(e).__name__, e)
    return []


def row_to_unified(row: Dict[str, Any], calculated: Optional[Dict[str, Any]] = None,
                   samples: Optional[List[UnifiedSample]] = None) -> Optional[UnifiedDive]:
    when = parse_local(row.get("DiveDate"))
    if when is None:
        logger.warning("Shearwater: dive %s has no readable DiveDate; skipped", row.get("DiveId"))
        return None
    calc = calculated or {}

    def calc_or_column(key: str) -> Optional[float]:
        value = parse_number(calc.get(key))
        if value is None or value == 0.0:
            value = parse_number(row.get(key))
        return value if value else None

    lat, lng = parse_gnss(row.get("GnssEntryLocation"))
    weight = parse_number(row.get("Weight"))
    service_fields: Dict[str, Any] = {}
    for name, column in COLUMNS.items():
        value = row.get(column)
        if value is None or value == "":
            continue
        service_fields[name] = parse_number(value) if column in NUMBER_COLUMNS else str(value)
    return UnifiedDive(
        date_time=when,
        duration=int(parse_number(row.get("DiveLengthTime")) or 0),
        max_depth=parse_number(row.get("Depth")) or 0.0,
        avg_depth=calc_or_column("AverageDepth"),
        temp_min=calc_or_column("MinTemp"),
        temp_max=calc_or_column("MaxTemp"),
        temp_avg=calc_or_column("AverageTemp"),
        external_ids={SERVICE_ID: str(row["DiveId"])},
        gas_mixtures=parse_tanks(row),
        location=row.get("Site") or None,
        notes=row.get("Notes") or None,
        dive_number=int(parse_number(row.get("DiveNumber"))) if parse_number(row.get("DiveNumber")) else None,
        weight=weight,
        # the app labels the column "Weight (lb kg)"; the unit is not stored
        # with the value, so metric is assumed until seen otherwise (Track H)
        weight_unit="kilogram" if weight is not None else None,
        buddy=row.get("Buddy") or None,
        lat=lat, lng=lng,
        samples=list(samples or []),
        device_logged=True,
        service_fields=service_fields,
    )


# ---------------------------------------------------------------------------
# Writing helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    """UTC now; a seam for the tests."""
    return datetime.now(timezone.utc)


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number_text(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _weight_text(weight: Optional[float], unit: Optional[str]) -> Optional[str]:
    """The column is labelled "Weight (lb kg)" in the app and holds one
    number; kilograms are written (pounds converted), matching how it is
    read."""
    if weight is None:
        return None
    kg = weight * KG_PER_LB if (unit or "").lower().startswith(("lb", "pound")) else weight
    return _number_text(round(kg, 2))


def _gnss_text(lat: Optional[float], lng: Optional[float]) -> Optional[str]:
    if lat is None or lng is None:
        return None
    return f"{lat:.6f},{lng:.6f}"


def _same(current: Any, wanted: Optional[str]) -> bool:
    """Column text vs the text we would write: blank and NULL are one
    value, and numbers compare as numbers ('2857.24' == '2857.240')."""
    cur = _text(current)
    if cur is None and wanted is None:
        return True
    if cur is None or wanted is None:
        return False
    if cur == wanted:
        return True
    a, b = parse_number(cur), parse_number(wanted)
    return a is not None and b is not None and abs(a - b) < 0.005 and re.fullmatch(r"-?[\d.,]+", cur) is not None \
        and re.fullmatch(r"-?[\d.,]+", wanted) is not None


def app_is_running() -> bool:
    """Best effort: is the Shearwater app open on this
    computer? macOS/Linux ask pgrep; Windows asks tasklist; anything
    failing counts as "not running" so a missing tool never blocks a
    write on a copy of the file."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {APP_PROCESS_NAME}.exe"],
                                 capture_output=True, text=True, timeout=5).stdout
            return f"{APP_PROCESS_NAME}.exe".lower() in out.lower()
        return subprocess.run(["pgrep", "-x", APP_PROCESS_NAME], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class ShearwaterAdapter(BaseDiveAdapter):
    """The Shearwater app's ``dive_data.db`` as a dive service:
    dives are read, their metadata is written the way the app writes it, and
    ``add_dive`` / ``delete_dive`` always refuse."""

    service_id = SERVICE_ID
    display_name = "Shearwater app"
    stores_external_ids = False
    accepts_new_dives = False      # only the app's computer download adds a dive

    @classmethod
    def field_catalog(cls) -> List[FieldSpec]:
        computer = [
            FieldSpec(key="shearwater.date_time", label="Start time", type="datetime", unified="date_time", writable=False),
            FieldSpec(key="shearwater.duration", label="Duration", type="number", unified="duration", unit="s", writable=False),
            FieldSpec(key="shearwater.max_depth", label="Max depth", type="number", unified="max_depth", unit="m", writable=False),
            FieldSpec(key="shearwater.avg_depth", label="Average depth", type="number", unified="avg_depth", unit="m", writable=False),
            FieldSpec(key="shearwater.temp_min", label="Lowest temperature", type="number", unified="temp_min", unit="°C", writable=False),
            FieldSpec(key="shearwater.temp_max", label="Highest temperature", type="number", unified="temp_max", unit="°C", writable=False),
            FieldSpec(key="shearwater.tanks", label="Tanks", type="tanks", unified="tanks", writable=False),
            FieldSpec(key="shearwater.samples", label="Dive profile", type="samples", unified="samples", writable=False),
        ]
        # every column the app lets the user edit. The tanks stay read-only:
        # the gas profile is the computer's, and the editable
        # Tank<n>PressureStart/End columns are not written either (a
        # round trip through the transmitter's own pressures would look like
        # a change on every run)
        editable = [
            # the number is the computer's own log number (and the app renumbers
            # nothing else); another service's numbering must not be written
            # over it - a first live run did exactly that (2026-09-30)
            FieldSpec(key="shearwater.dive_number", label="Dive number", type="number", unified="dive_number", writable=False),
            FieldSpec(key="shearwater.site", label="Site", type="text", unified="location"),
            FieldSpec(key="shearwater.buddy", label="Buddy", type="text", unified="buddy"),
            FieldSpec(key="shearwater.notes", label="Notes", type="text", unified="notes"),
            FieldSpec(key="shearwater.weight", label="Weight", type="number", unified="weight"),
            FieldSpec(key="shearwater.gps", label="Entry position", type="gps", unified="gps"),
        ]
        for name, column in COLUMNS.items():
            editable.append(FieldSpec(key=f"shearwater.{name}", label=_LABELS.get(name, column),
                                      type="number" if column in NUMBER_COLUMNS else "text",
                                      unit="°C" if column == "AirTemperature" else None))
        return computer + editable

    def __init__(self, path: str):
        self.path = path
        # what pairs.adapter_account names this side by (per-account sync
        # state, pickers): the account folder's name
        self.account = account_of_path(path)

    # -- access ---------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def login(self) -> bool:
        if not os.path.isfile(self.path):
            logger.error("Shearwater: no database at %s", self.path)
            return False
        try:
            with self._connect() as conn:
                names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.DatabaseError as e:
            logger.error("Shearwater: %s is not a readable SQLite file: %s", self.path, e)
            return False
        if "dive_details" not in names:
            logger.error("Shearwater: %s has no dive_details table; not a Shearwater app database", self.path)
            return False
        return True

    _SELECT = ("SELECT d.*, l.calculated_values_from_samples AS _calculated, l.format AS _format, "
               "l.data_bytes_1 AS _log FROM dive_details d "
               "LEFT JOIN log_data l ON l.log_id = d.DiveId")

    @staticmethod
    def _unified(row: sqlite3.Row) -> Optional[UnifiedDive]:
        data = dict(row)
        calculated = None
        raw = data.pop("_calculated", None)
        log_format = data.pop("_format", None)
        blob = data.pop("_log", None)
        if raw:
            try:
                calculated = json.loads(raw)
            except ValueError:
                calculated = None
        return row_to_unified(data, calculated, profile_of(data.get("DiveId"), log_format, blob))

    def fetch_dives(self, date_from: Optional[datetime] = None, date_to: Optional[datetime] = None) -> List[UnifiedDive]:
        out: List[UnifiedDive] = []
        with self._connect() as conn:
            for row in conn.execute(self._SELECT + " ORDER BY d.DiveDate"):
                dive = self._unified(row)
                if dive is None:
                    continue
                if date_from and dive.date_time < date_from:
                    continue
                if date_to and dive.date_time > date_to:
                    continue
                out.append(dive)
        return out

    def fetch_dive(self, external_id: str) -> Optional[UnifiedDive]:
        with self._connect() as conn:
            row = conn.execute(self._SELECT + " WHERE d.DiveId = ?", (str(external_id),)).fetchone()
        return self._unified(row) if row is not None else None

    # -- writes ---------------------------------------------------------------

    def add_dive(self, dive: UnifiedDive) -> Optional[str]:
        # the engine never gets here (accepts_new_dives); a safety net for other callers
        logger.error("Shearwater: the dive at %s was not added - only the Shearwater app's own computer "
                     "download adds dives to its database", dive.date_time)
        return None

    def update_dive(self, external_id: str, dive: UnifiedDive) -> bool:
        """Write ``dive``'s metadata into the app's row for ``external_id``
        the way the app does (module docstring). Only columns whose value
        differs are touched; a dropdown value the app does not offer is
        skipped with a warning rather than written. Returns False when the
        dive is unknown or the file must not be written right now."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM dive_details WHERE DiveId = ?", (str(external_id),)).fetchone()
        if row is None:
            logger.error("Shearwater: no dive %s in %s", external_id, self.path)
            return False
        current = dict(row)
        changes = self._changes(current, dive)
        if not changes:
            logger.info("Shearwater: dive %s already up to date", external_id)
            return True
        return self._write(str(external_id), changes)

    def _write(self, external_id: str, changes: Dict[str, Optional[str]]) -> bool:
        """Write ``changes`` ({column: text or None}) to the dive's row the way
        the app does (stamps, LastModified, LastModifiedServerTime), after the
        safety checks and the one backup per run."""
        problem = self._write_blocker()
        if problem:
            logger.error("Shearwater: not writing dive %s: %s", external_id, problem)
            return False
        self._backup_once()
        now = _now()
        stamp_text = now.strftime("%Y-%m-%d %H:%M:%S")
        stamp_ms = int(now.timestamp() * 1000)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            assignments = ", ".join(f'"{column}" = ?' for column in changes)
            conn.execute(f'UPDATE dive_details SET {assignments}, "LastModified" = ? WHERE "DiveId" = ?',
                         [*changes.values(), stamp_text, str(external_id)])
            meta = conn.execute("SELECT FieldTimeStampJson, LastModifiedDevice FROM SyncV3MetadataDiveDetail WHERE Id = ?",
                                (str(external_id),)).fetchone()
            if meta is not None:
                try:
                    stamps = json.loads(meta[0] or "{}")
                except ValueError:
                    stamps = {}
                for column in changes:
                    stamps[column] = stamp_ms
                conn.execute("UPDATE SyncV3MetadataDiveDetail SET FieldTimeStampJson = ?, LastModifiedServerTime = ? WHERE Id = ?",
                             (json.dumps(stamps, separators=(",", ":")), stamp_text, str(external_id)))
            else:
                # a dive the app has not synced yet: give it the bookkeeping
                # row the app would, on this file's device id
                device = conn.execute("SELECT LastModifiedDevice FROM SyncV3MetadataDiveDetail LIMIT 1").fetchone()
                device_id = device[0] if device and device[0] else str(uuid.uuid4())
                stamps = {column: stamp_ms for column in changes}
                conn.execute("INSERT INTO SyncV3MetadataDiveDetail (Id, LastModifiedDevice, LastModifiedServerTime, "
                             "CreatedDevice, CreatedTime, FieldTimeStampJson, Version) VALUES (?, ?, ?, ?, ?, ?, 1)",
                             (str(external_id), device_id, stamp_text, device_id, stamp_text,
                              json.dumps(stamps, separators=(",", ":"))))
            conn.commit()
        except sqlite3.DatabaseError as e:
            conn.rollback()
            logger.error("Shearwater: writing dive %s failed: %s", external_id, e)
            return False
        finally:
            conn.close()
        logger.info("Shearwater: updated dive %s (%s)", external_id, ", ".join(changes))
        return True

    # -- what a write consists of ---------------------------------------------

    @staticmethod
    def _changes(current: Dict[str, Any], dive: UnifiedDive) -> Dict[str, Optional[str]]:
        """{column: new text} for every editable column whose value in
        ``dive`` differs from the row. A unified attribute that is None
        clears its column (the rule engine sends None only when it means
        it); a service field absent from ``dive.service_fields`` is left
        alone."""
        wanted: Dict[str, Optional[str]] = {
            "Site": _text(dive.location),
            "Buddy": _text(dive.buddy),
            "Notes": _text(dive.notes),
            "Weight": _weight_text(dive.weight, dive.weight_unit),
            "GnssEntryLocation": _gnss_text(dive.lat, dive.lng),
        }
        for name, column in COLUMNS.items():
            if name not in dive.service_fields:
                continue
            value = dive.service_fields[name]
            if value is None or value == "":
                wanted[column] = None
            elif column in NUMBER_COLUMNS:
                number = parse_number(value)
                wanted[column] = _number_text(number) if number is not None else None
            else:
                text = str(value).strip()
                options = DROPDOWN_OPTIONS.get(column)
                if options and text not in options:
                    match = next((o for o in options if o.lower() == text.lower()), None)
                    if match is None:
                        logger.warning("Shearwater: %r is not an option of %s (%s); not written",
                                       text, column, ", ".join(options))
                        continue
                    text = match
                wanted[column] = text or None
        changes: Dict[str, Optional[str]] = {}
        for column, value in wanted.items():
            if _same(current.get(column), value):
                continue
            changes[column] = value
        return changes

    def _write_blocker(self) -> Optional[str]:
        """Why the file must not be written now, or None: the app's own
        journal is there (a write in progress or a crash), or the app is
        running (it caches rows and would overwrite ours on its next save)."""
        for suffix in ("-journal", "-wal"):
            if os.path.exists(self.path + suffix):
                return f"the app's {suffix[1:]} file exists beside the database; close the Shearwater app first"
        if app_is_running():
            return "the Shearwater app is running; close it before syncing into its database"
        return None

    _backed_up = False

    def _backup_once(self) -> None:
        """One copy of the file per adapter (i.e. per run) under
        DATA_DIR/backups/shearwater/, the newest BACKUPS_TO_KEEP kept.
        Best-effort: a failing backup is logged, not fatal."""
        if self._backed_up:
            return
        self._backed_up = True
        try:
            folder = os.path.join(os.environ.get("DATA_DIR", "."), "backups", "shearwater")
            os.makedirs(folder, exist_ok=True)
            name = f"{_now().strftime('%Y%m%dT%H%M%S')}-{os.path.basename(self.path)}"
            shutil.copy2(self.path, os.path.join(folder, name))
            logger.info("Shearwater: database copied to %s before writing", os.path.join(folder, name))
            stale = sorted(f for f in os.listdir(folder) if f.endswith(os.path.basename(self.path)))[:-BACKUPS_TO_KEEP]
            for old in stale:
                os.remove(os.path.join(folder, old))
        except OSError as e:
            logger.warning("Shearwater: backup before writing failed (continuing): %s", e)

    def delete_dive(self, external_id: str) -> bool:
        logger.error("Shearwater: dives are never deleted from the Shearwater app database (dive %s kept)", external_id)
        return False
