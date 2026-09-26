"""How the local Garmin files are named, and the .fit files next to them.

Every Garmin dive is stored under one stem,
``<dive number>_<YYYY-MM-DD>_<HHMMSS>_<activity id>`` (local start time), for
both its cached JSON (``garmin/<account>/data/<stem>.json``) and its
original FIT file (``garmin/<account>/fit/<stem>.fit``; see layout.py). The name
sorts by dive number, reads as the dive at a glance and, when the file is
handed to another app (Submersion's FIT import), says which dive it is.

The activity id is the last part and never changes, so it is what a file is
found by; the number and time in front of it can go stale when the dive is
renumbered or retimed on Garmin, and ``rename_fit_files`` brings the FIT
names back in line with the JSON ones after a refresh.

The FIT files live beside the JSON cache directory, not in it, on purpose:
a Full refresh clears that directory, and a FIT download is not something to
lose along with it.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import struct
import zipfile
from typing import Any, Dict, List, Optional

from src.core import layout

logger = logging.getLogger("dive_sync.garmin_files")

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
    return layout.garmin_fit_dir(username, base_dir)


def find_fit(activity_id: Any, username: Optional[str] = None, base_dir: Optional[str] = None) -> Optional[str]:
    """Path of the downloaded FIT for an activity, whatever its current name;
    without ``username``, in whichever account has it."""
    if not activity_id:
        return None
    if username:
        directories = [fit_dir(username, base_dir)]
    else:
        directories = [os.path.join(os.path.dirname(d), layout.FIT_SUBDIR) for _, d in layout.all_dives_dirs("garmin", base_dir)]
    wanted = str(activity_id)
    for directory in directories:
        if not os.path.isdir(directory):
            continue
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


# -- the cached JSON (garmin/<account>/data/<stem>.json) ----------------------

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


# The dive computer a dive was recorded on. Connect's activity JSON names it
# only by numbers - the watch's serial (``deviceMetaDataDTO.deviceId``) and
# Connect's own model id (``deviceTypePk``), which nobody publishes a list of
# - but the watch's FIT file carries its FIT product number, and that one is
# listed: FIT_PRODUCT_NAMES is libdivecomputer's table
# (subsurface/libdc src/garmin-models.h), which has the models Garmin's FIT
# SDK does not know yet (the X50i, 4518). A serial is one watch, so one
# downloaded FIT names every dive recorded on it (``device_products``).
FIT_PRODUCT_NAMES = {
    2859: "Descent Mk1",
    2991: "Descent Mk1 APAC",
    3258: "Descent Mk2(i)",
    3702: "Descent Mk2(i) APAC",
    3542: "Descent Mk2 S",
    3930: "Descent Mk2 S APAC",
    4005: "Descent G1",
    4222: "Descent Mk3(i) 43mm",
    4223: "Descent Mk3(i) 51mm",
    4518: "Descent X50i",
    4588: "Descent G2",
    4534: "fēnix 8 43mm",
    4532: "fēnix 8 Solar 47mm",
    4533: "fēnix 8 Solar 51mm APAC",
    4776: "fēnix 8 Solar 51mm",
    4536: "fēnix 8 47/51mm APAC",
    4775: "fēnix 8 47/51mm",
    4631: "fēnix 8 Pro",
}
# Connect's model id -> name, for a watch none of whose FIT files has been
# downloaded yet. Only ids seen next to a FIT product can go in here.
DEVICE_TYPE_NAMES = {
    37090: FIT_PRODUCT_NAMES[4223],
    37191: FIT_PRODUCT_NAMES[4518],
}
_FIT_FILE_ID = 0                    # global message number of file_id
# path -> (mtime, (serial, product)); a FIT file never changes once
# downloaded, so each is read once per process however often the list reloads.
_fit_device_cache: Dict[str, Any] = {}


def fit_device(path: str) -> Optional[tuple]:
    """``(serial, product)`` from a FIT file's file_id message, or None when
    it cannot be read. Only file_id is needed - it is the first data message
    of a FIT file - so this reads its few fields directly rather than
    decoding the whole file."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    cached = _fit_device_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, "rb") as f:
            result = _parse_file_id(f.read(64 * 1024))
    except (OSError, ValueError, KeyError, struct.error, IndexError):
        result = None
    _fit_device_cache[path] = (mtime, result)
    return result


def _parse_file_id(data: bytes) -> Optional[tuple]:
    """Walk the records up to the first file_id data message; fields 2
    (product) and 3 (serial number) are what is kept."""
    header_size = data[0]
    if data[8:12] != b".FIT":
        raise ValueError("not a FIT file")
    pos = header_size
    definitions: Dict[int, tuple] = {}         # local type -> (global, endian, fields, dev size)
    while pos < len(data):
        header = data[pos]
        pos += 1
        if header & 0x80:                   # compressed timestamp: a data message
            local, is_definition, has_dev = (header >> 5) & 0x03, False, False
        else:
            local, is_definition, has_dev = header & 0x0F, bool(header & 0x40), bool(header & 0x20)
        if is_definition:
            endian = "<" if data[pos + 1] == 0 else ">"
            global_num = struct.unpack(endian + "H", data[pos + 2:pos + 4])[0]
            count = data[pos + 4]
            pos += 5
            fields = [(data[pos + i * 3], data[pos + i * 3 + 1]) for i in range(count)]
            pos += count * 3
            dev_size = 0
            if has_dev:
                dev_count = data[pos]
                dev_size = sum(data[pos + 1 + i * 3 + 1] for i in range(dev_count))
                pos += 1 + dev_count * 3
            definitions[local] = (global_num, endian, fields, dev_size)
            continue
        global_num, endian, fields, dev_size = definitions[local]
        if global_num != _FIT_FILE_ID:
            pos += sum(size for _, size in fields) + dev_size
            continue
        values: Dict[int, int] = {}
        for number, size in fields:
            raw = data[pos:pos + size]
            pos += size
            if size in (2, 4):
                values[number] = struct.unpack(endian + ("H" if size == 2 else "I"), raw)[0]
        serial = values.get(3)
        product = values.get(2)
        return (str(serial) if serial not in (None, 0, 0xFFFFFFFF) else "",
                product if product not in (None, 0xFFFF) else None)
    return None


def device_ids(payload: Dict[str, Any]) -> tuple:
    """``(serial, model id)`` of the watch that recorded a cached Garmin
    dive: Connect's ``deviceId`` ("" for none, as on a hand-logged dive)
    and ``deviceTypePk`` (None when missing)."""
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    summary = payload.get("summary") or {}
    metadata = details.get("metadataDTO") or summary.get("metadataDTO") or {}
    device = metadata.get("deviceMetaDataDTO") or {}
    serial = device.get("deviceId") or summary.get("deviceId")
    try:
        type_pk = int(device.get("deviceTypePk"))
    except (TypeError, ValueError):
        type_pk = None
    return ("" if serial in (None, "", 0, "0") else str(serial)), type_pk


def device_products(fit_paths: List[str], known: Optional[Dict[str, int]] = None) -> Dict[str, int]:
    """serial -> FIT product, from downloaded FIT files. ``known`` (updated
    in place) is what earlier calls found; a serial already in it is kept,
    so callers pass one FIT per watch not yet named rather than every dive's."""
    products = known if known is not None else {}
    for path in fit_paths:
        found = fit_device(path)
        if found and found[0] and found[1] is not None and found[0] not in products:
            products[found[0]] = found[1]
    return products


def device_name(manual: bool, serial: str, type_pk: Optional[int],
                products: Optional[Dict[str, int]] = None) -> str:
    """The dive computer a Garmin dive was recorded on, for display.
    "Hand-logged" for a dive typed in on Connect (it has no device); else
    the model its watch's FIT product number names (``products``: serial ->
    FIT product, from ``device_products``), else the one Connect's model id
    names, else the number itself, so an unknown model can still be told
    apart and added to the tables."""
    if manual:
        return "Hand-logged"
    product = (products or {}).get(serial)
    if product in FIT_PRODUCT_NAMES:
        return FIT_PRODUCT_NAMES[product]
    if type_pk in DEVICE_TYPE_NAMES:
        return DEVICE_TYPE_NAMES[type_pk]
    if product is not None:
        return f"Garmin product {product}"
    return f"Garmin device type {type_pk}" if type_pk is not None else ""


def listing_fingerprint(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {k: entry.get(k) for k in LISTING_KEYS}


def cache_dir(username: Optional[str], base_dir: Optional[str] = None) -> str:
    """Where a Garmin refresh caches this account's dives."""
    return layout.dives_dir("garmin", username, base_dir)


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
