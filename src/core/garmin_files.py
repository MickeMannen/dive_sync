"""How the local Garmin files are named, and the .fit files next to them.

Every Garmin dive is stored under one stem,
``<dive number>_<YYYY-MM-DD>_<HHMMSS>_<activity id>`` (local start time), for
both its cached JSON (``DATA_DIR/garmin/<account>/<stem>.json``) and its
original FIT file (``DATA_DIR/garmin_fit/<account>/<stem>.fit``). The name
sorts by dive number, reads as the dive at a glance and, when the file is
handed to another app (Submersion's FIT import), says which dive it is.

The activity id is the last part and never changes, so it is what a file is
found by; the number and time in front of it can go stale when the dive is
renumbered or retimed on Garmin, and ``rename_fit_files`` brings the FIT
names back in line with the JSON ones after a refresh.

The FIT files live outside the JSON cache directory on purpose: a Full
refresh clears that directory, and a FIT download is not something to lose
along with it.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import zipfile
from typing import Any, Dict, List, Optional

logger = logging.getLogger("dive_sync.garmin_files")

FIT_SUBDIR = "garmin_fit"
# Stands in for the dive number of a dive Garmin has not numbered, so the
# activity id stays the last of exactly four parts.
NO_NUMBER = "nonum"


def dive_stem(dive_number: Any, start_time_local: Any, activity_id: Any) -> str:
    """``12_2024-05-01_102345_17283746``. ``start_time_local`` is Garmin's
    ``startTimeLocal`` ("2024-05-01 10:23:45" or "2024-05-01T10:23:45.0")."""
    number = str(dive_number).strip() if dive_number not in (None, "") else ""
    if not number.isdigit():
        number = NO_NUMBER
    m = re.match(r"\s*(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?", str(start_time_local or ""))
    if m:
        date_part, time_part = m.group(1), m.group(2) + m.group(3) + (m.group(4) or "00")
    else:
        date_part, time_part = "0000-00-00", "000000"
    return f"{number}_{date_part}_{time_part}_{activity_id}"


def stem_for_payload(payload: Dict[str, Any]) -> Optional[str]:
    """The stem for a cached Garmin JSON payload (``{"summary", "details"}``),
    or None when it carries no activity id."""
    summary = payload.get("summary") or {}
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    activity_id = summary.get("activityId")
    if not activity_id:
        return None
    metadata = details.get("metadataDTO") or summary.get("metadataDTO") or {}
    dive_number = metadata.get("diveNumber")
    if dive_number is None:
        dive_number = summary.get("diveNumber")
    sum_dto = details.get("summaryDTO") or {}
    start = summary.get("startTimeLocal") or sum_dto.get("startTimeLocal")
    return dive_stem(dive_number, start, activity_id)


def activity_id_of(filename: str) -> Optional[str]:
    """The activity id a file named by ``dive_stem`` belongs to."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    parts = stem.split("_")
    return parts[-1] if len(parts) == 4 and parts[-1].isdigit() else None


def fit_dir(username: Optional[str] = None, base_dir: Optional[str] = None) -> str:
    parts = [base_dir or os.environ.get("DATA_DIR", "./data"), FIT_SUBDIR]
    if username:
        parts.append(username)
    return os.path.join(*parts)


def find_fit(activity_id: Any, username: Optional[str] = None, base_dir: Optional[str] = None) -> Optional[str]:
    """Path of the downloaded FIT for an activity, whatever its current name."""
    directory = fit_dir(username, base_dir)
    if not activity_id or not os.path.isdir(directory):
        return None
    wanted = str(activity_id)
    for name in os.listdir(directory):
        if name.endswith(".fit") and activity_id_of(name) == wanted:
            return os.path.join(directory, name)
    return None


def fit_index(username: Optional[str] = None, base_dir: Optional[str] = None) -> Dict[str, str]:
    """activity id -> FIT filename, for marking a whole dive list in one
    directory read."""
    directory = fit_dir(username, base_dir)
    if not os.path.isdir(directory):
        return {}
    out = {}
    for name in os.listdir(directory):
        activity_id = activity_id_of(name) if name.endswith(".fit") else None
        if activity_id:
            out[activity_id] = name
    return out


def extract_fit(data: bytes) -> bytes:
    """Garmin's "original" download is a zip holding the .fit; older or
    odd endpoints hand the bare file back. Either way, the FIT bytes."""
    if data[:2] != b"PK":
        return data
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".fit")] or archive.namelist()
        if not names:
            raise ValueError("Garmin returned an empty archive")
        return archive.read(names[0])


def save_fit(data: bytes, stem: str, username: Optional[str] = None, base_dir: Optional[str] = None) -> str:
    """Write a FIT under ``stem``, replacing any earlier copy of the same
    activity (which may carry an older name)."""
    directory = fit_dir(username, base_dir)
    os.makedirs(directory, exist_ok=True)
    old = find_fit(activity_id_of(stem), username, base_dir)
    path = os.path.join(directory, f"{stem}.fit")
    with open(path, "wb") as f:
        f.write(extract_fit(data))
    if old and os.path.abspath(old) != os.path.abspath(path):
        os.remove(old)
    return path


def rename_fit_files(json_dir: str, username: Optional[str] = None, base_dir: Optional[str] = None) -> int:
    """Give every downloaded FIT the name of its dive's cached JSON, so a
    dive renumbered or retimed on Garmin keeps a matching pair. Returns how
    many were renamed."""
    fits = fit_index(username, base_dir)
    if not fits or not os.path.isdir(json_dir):
        return 0
    directory = fit_dir(username, base_dir)
    renamed = 0
    for name in os.listdir(json_dir):
        activity_id = activity_id_of(name) if name.endswith(".json") else None
        current = fits.get(activity_id) if activity_id else None
        wanted = os.path.splitext(name)[0] + ".fit"
        if current and current != wanted:
            try:
                os.replace(os.path.join(directory, current), os.path.join(directory, wanted))
                renamed += 1
                logger.info("Renamed FIT %s -> %s", current, wanted)
            except OSError as e:
                logger.warning("Could not rename FIT %s: %s", current, e)
    return renamed


# -- the cached JSON (DATA_DIR/garmin/<account>/<stem>.json) ------------------

# Fields of a Garmin activity-listing entry that reflect the dive itself. A
# cached dive whose stored listing entry still matches on all of these is not
# re-fetched (rework.md E15). Deliberately not full dict equality: the listing
# also carries per-session noise (userRoles, userPro, ownerDisplayName) that
# would make every dive look changed and defeat the whole thing.
LISTING_KEYS = (
    "activityId", "activityName", "locationName", "diveNumber", "duration",
    "elapsedDuration", "maxDepth", "avgDepth", "bottomTime", "minTemperature",
    "startTimeLocal", "startTimeGMT", "beginTimestamp", "endTimeGMT",
    "isFavorite", "isManualActivity", "diveCount", "lapCount",
)


def is_manual_dive(payload: Dict[str, Any]) -> bool:
    """Whether a cached Garmin dive was logged by hand. Connect says so as
    ``manualActivity`` on the listing entry and in ``metadataDTO``;
    ``isManualActivity`` is accepted too."""
    summary = payload.get("summary") or {}
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    for source in (summary, summary.get("metadataDTO") or {}, details, details.get("metadataDTO") or {}):
        if source.get("manualActivity") or source.get("isManualActivity"):
            return True
    return False


def listing_fingerprint(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {k: entry.get(k) for k in LISTING_KEYS}


def cache_dir(username: Optional[str], base_dir: Optional[str] = None) -> str:
    """Where a Garmin refresh caches this account's dives."""
    parts = [base_dir or os.environ.get("DATA_DIR", "./data"), "garmin"]
    if username:
        parts.append(username)
    return os.path.join(*parts)


def cached_listings(garmin_dir: str) -> Dict[str, Dict[str, Any]]:
    """activity id -> ``{"file": <name>, "summary": <listing entry stored last
    time>, "stem": <current name for it>}``. Reading the whole cache costs a
    few dozen local file reads; one Garmin API call costs a cool-down, so this
    pays for itself at the first skipped dive. The filename comes back too, so
    a skipped dive can be kept when a refresh prunes (rework.md E18)."""
    out: Dict[str, Dict[str, Any]] = {}
    if not os.path.isdir(garmin_dir):
        return out
    for name in os.listdir(garmin_dir):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(garmin_dir, name), "r") as f:
                payload = json.load(f)
        except Exception:
            continue                        # unreadable cache entry: just re-fetch it
        summary = (payload or {}).get("summary") or {}
        # Only count it as cached when the download actually completed: a
        # dive whose telemetry or tank fetch failed is written out anyway (so
        # the rest is not lost) but marked, or the unchanged listing entry
        # would make it skip for ever with a hole in it.
        if payload.get("details") is None or payload.get("incomplete"):
            continue
        activity_id = summary.get("activityId")
        if activity_id is not None:
            out[str(activity_id)] = {"file": name, "summary": summary,
                                     "stem": stem_for_payload(payload)}
    return out


def read_cached(garmin_dir: str, name: str) -> Dict[str, Any]:
    with open(os.path.join(garmin_dir, name), "r") as f:
        return json.load(f)


def write_cached(garmin_dir: str, payload: Dict[str, Any], replaces: Optional[str] = None) -> str:
    """Write one dive's raw payload under its stem name. ``replaces`` is the
    name it was cached under before, removed when the name has changed
    (renumbered / retimed dive, or the old ``<dive #>.json`` scheme)."""
    os.makedirs(garmin_dir, exist_ok=True)
    stem = stem_for_payload(payload) or f"{NO_NUMBER}_0000-00-00_000000_0"
    name = f"{stem}.json"
    with open(os.path.join(garmin_dir, name), "w") as f:
        json.dump(payload, f, indent=2)
    if replaces and replaces != name:
        try:
            os.remove(os.path.join(garmin_dir, replaces))
        except OSError:
            pass
    return name
