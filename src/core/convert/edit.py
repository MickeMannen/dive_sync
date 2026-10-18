"""Editing a dive in memory (plans/convert.md I14, the Dive edit page).

Two pure functions over `UnifiedDive`, no Qt, no file: `shift_dive` moves
a dive's start (UWMedia's "Adjust time") and `apply_edits` sets any other
field from the form's values, validating choices and numbers with an error
text a page can show. Both return a new dive and leave the one given
untouched, so a page can keep the original beside the edited one (I15's
Revert).

Values are SI (metres, bar, °C, litres, seconds) except weight and
visibility, which the model holds in the dive's own unit
(``weight_unit`` / ``visibility_unit``): a page converts what the diver
typed before calling here.

**What a shift means in this model.** A sample's and an event's ``time``
is an offset from the start of the dive, not a clock time, so moving the
start moves every sample's and event's instant by the same delta while
their offsets, and the duration, stay exactly what they were: the FIT
timestamps UWMedia rewrites one by one are, here, the one ``date_time``.
``date_time_utc`` follows the new local start and the new zone.

**Time zones.** The model's ``timezone`` is an IANA name when a reader had
one (``Asia/Kuala_Lumpur``; a FIT gives a fixed offset as ``Etc/GMT-7``).
The page shows and edits it as a UTC offset (``+07:00``), which is all a
dive file can say. An offset typed on the page becomes ``Etc/GMT∓N`` for
whole hours (the reader's own spelling) and ``UTC+05:30`` otherwise -
not an IANA name, but the most that can be said for a half-hour zone, and
nothing of the conversions reads it.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.core.models import KNOWN_DIVE_MODES, KNOWN_WATER_TYPES, GasMixture, UnifiedDive

# The fields that come from the profile: read-only when the dive has samples.
PROFILE_FIELDS: Tuple[str, ...] = ("duration", "max_depth", "avg_depth")

# Text fields: blank clears them.
TEXT_FIELDS: Tuple[str, ...] = (
    "location", "buddy", "notes", "computer_vendor", "computer_model", "computer_serial", "computer_firmware", "deco_model",
)
# Numbers: field -> (label, minimum, maximum, integer?, required?)
NUMBER_FIELDS: Dict[str, Tuple[str, Optional[float], Optional[float], bool, bool]] = {
    "duration": ("Duration", 0, None, True, True),
    "max_depth": ("Max depth", 0, None, False, True),
    "avg_depth": ("Avg depth", 0, None, False, False),
    "temp_min": ("Water temp (min)", -5, 60, False, False),
    "temp_max": ("Water temp (max)", -5, 60, False, False),
    "temp_avg": ("Water temp (avg)", -5, 60, False, False),
    "dive_number": ("Dive number", 0, None, True, False),
    "surface_interval": ("Surface interval", 0, None, True, False),
    "bottom_time": ("Bottom time", 0, None, True, False),
    "water_density": ("Water density", 900, 1200, False, False),
    "lat": ("Latitude", -90, 90, False, False),
    "lng": ("Longitude", -180, 180, False, False),
    "exit_lat": ("Exit latitude", -90, 90, False, False),
    "exit_lng": ("Exit longitude", -180, 180, False, False),
    "weight": ("Weight", 0, None, False, False),
    "visibility": ("Visibility", 0, None, False, False),
    "gf_low": ("GF low", 0, 100, True, False),
    "gf_high": ("GF high", 0, 100, True, False),
    "cns_start": ("CNS start", 0, None, False, False),
    "cns_end": ("CNS end", 0, None, False, False),
}
CHOICE_FIELDS: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "dive_mode": ("Dive mode", KNOWN_DIVE_MODES),
    "water_type": ("Water type", KNOWN_WATER_TYPES),
}
TANK_NUMBERS: Dict[str, Tuple[str, Optional[float], Optional[float]]] = {
    "oxygen": ("O2", 0, 100), "helium": ("He", 0, 100),
    "tank_volume": ("Volume", 0, None), "start_pressure": ("Start pressure", 0, None), "end_pressure": ("End pressure", 0, None),
}
EDITABLE_FIELDS: Tuple[str, ...] = (
    ("date", "time", "utc_offset") + tuple(NUMBER_FIELDS) + TEXT_FIELDS + tuple(CHOICE_FIELDS) + ("tanks",)
)

_OFFSET = re.compile(r"^\s*(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\s*$", re.IGNORECASE)
_ETC_ZONE = re.compile(r"^Etc/GMT([+-])(\d{1,2})$")


class EditError(ValueError):
    """A value the editor cannot take; the text is what the page shows."""


# ---------------------------------------------------------------------------
# Time zones as offsets
# ---------------------------------------------------------------------------

def parse_utc_offset(text: Any) -> Optional[timedelta]:
    """``"+07:00"``, ``"+7"``, ``"-03:30"``, ``"UTC+8"`` -> the offset;
    blank -> None (no zone). Anything else is an `EditError`."""
    text = ("" if text is None else str(text)).strip()
    if not text or text.upper() in ("UTC", "GMT", "Z"):
        return timedelta(0) if text else None
    m = _OFFSET.match(text)
    if not m:
        raise EditError(f"Time zone: use a UTC offset such as +07:00 or -03:30, not '{text}'")
    sign, hours, minutes = m.group(1), int(m.group(2)), int(m.group(3) or 0)
    if hours > 14 or minutes > 59:
        raise EditError(f"Time zone: {text} is not a real UTC offset (-12:00 to +14:00)")
    offset = timedelta(hours=hours, minutes=minutes)
    return -offset if sign == "-" else offset


def format_utc_offset(offset: Optional[timedelta]) -> str:
    """``timedelta(hours=7)`` -> ``"+07:00"``; None -> ``""``."""
    if offset is None:
        return ""
    total = int(round(offset.total_seconds() / 60))
    sign = "-" if total < 0 else "+"
    hours, minutes = divmod(abs(total), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def zone_offset(name: Optional[str], at: Optional[datetime] = None) -> Optional[timedelta]:
    """The UTC offset of a ``timezone`` value: the fixed ``Etc/GMT∓N`` and
    ``UTC+05:30`` spellings directly, an IANA name through the zone
    database at ``at`` (the dive's local start); None for none or unknown."""
    name = (name or "").strip()
    if not name:
        return None
    if name.upper() in ("UTC", "ETC/UTC", "GMT", "ETC/GMT", "Z"):
        return timedelta(0)
    m = _ETC_ZONE.match(name)
    if m:
        hours = int(m.group(2))
        return timedelta(hours=-hours if m.group(1) == "+" else hours)   # the Etc zones carry the inverted sign
    if name.upper().startswith(("UTC", "GMT")):
        try:
            return parse_utc_offset(name)
        except EditError:
            return None
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(name)
    except Exception:
        return None
    when = at or datetime(2000, 1, 1)
    return zone.utcoffset(when.replace(tzinfo=zone))


def zone_for_offset(offset: Optional[timedelta]) -> Optional[str]:
    """The ``timezone`` value for an offset the page edited: ``Etc/GMT-7``
    for UTC+7 (the FIT reader's spelling; whole hours only), ``Etc/UTC``
    for zero, ``UTC+05:30`` for a half-hour offset; None for no offset."""
    if offset is None:
        return None
    minutes = int(round(offset.total_seconds() / 60))
    if minutes == 0:
        return "Etc/UTC"
    hours, rest = divmod(abs(minutes), 60)
    if rest or hours > 14:
        return "UTC" + format_utc_offset(offset)
    return f"Etc/GMT{'-' if minutes > 0 else '+'}{hours}"


def utc_offset_of(dive: UnifiedDive) -> Optional[timedelta]:
    """The dive's UTC offset: from its two clocks when it has both (exact),
    else from its zone name; None when nothing says."""
    if dive.date_time_utc is not None:
        return dive.date_time - dive.date_time_utc
    return zone_offset(dive.timezone, dive.date_time)


def utc_offset_text(dive: UnifiedDive) -> str:
    """The dive's zone as the page shows it: ``"+07:00"``, ``""`` for none."""
    return format_utc_offset(utc_offset_of(dive))


# ---------------------------------------------------------------------------
# The shift
# ---------------------------------------------------------------------------

def shift_dive(dive: UnifiedDive, new_start_local: datetime, new_tz: Any = ...) -> UnifiedDive:
    """A copy of ``dive`` starting at ``new_start_local`` (naive, local wall
    clock) in zone ``new_tz`` (a ``timezone`` value, None for none; left
    out: the dive's own). Every sample and event keeps its offset from the
    start, so each moves by the same delta as the start; the duration is
    unchanged; ``date_time_utc`` is recomputed from the new local start and
    the zone's offset (kept where no zone is known at all, shifted by the
    wall-clock delta when the dive had a UTC clock but no zone). A zone
    change alone (same wall clock) moves only the UTC instant."""
    if new_start_local.tzinfo is not None:
        new_start_local = new_start_local.replace(tzinfo=None)
    out = dive.model_copy(deep=True)
    delta = new_start_local - dive.date_time
    out.date_time = new_start_local
    if new_tz is not ...:
        out.timezone = new_tz or None
    offset = zone_offset(out.timezone, new_start_local)
    if offset is None and dive.date_time_utc is not None and new_tz is ...:
        offset = dive.date_time - dive.date_time_utc           # the old UTC clock's offset, zone unnamed
    if offset is not None:
        out.date_time_utc = new_start_local - offset
    elif dive.date_time_utc is not None:
        out.date_time_utc = dive.date_time_utc + delta
    return out


# ---------------------------------------------------------------------------
# Field edits
# ---------------------------------------------------------------------------

def locked_fields(dive: UnifiedDive) -> Tuple[str, ...]:
    """The fields the profile decides (`PROFILE_FIELDS`) when the dive has
    one; nothing on a hand-logged dive."""
    return PROFILE_FIELDS if dive.samples else ()


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _number(label: str, value: Any, low: Optional[float], high: Optional[float], integer: bool) -> float:
    try:
        number = float(str(value).strip()) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        raise EditError(f"{label}: not a number: '{value}'") from None
    if number != number:   # NaN
        raise EditError(f"{label}: not a number: '{value}'")
    if low is not None and number < low:
        raise EditError(f"{label}: {value} is below {low:g}")
    if high is not None and number > high:
        raise EditError(f"{label}: {value} is above {high:g}")
    return int(round(number)) if integer else number


def _text(value: Any) -> Optional[str]:
    text = "" if value is None else str(value)
    return text.strip() or None


def _tank(index: int, data: Mapping[str, Any], old: Optional[GasMixture]) -> GasMixture:
    """One row of the tank table as a `GasMixture`: a missing key keeps the
    old tank's value, a blank clears it (O2 blank is air, He blank is none)."""
    values: Dict[str, Any] = {}
    for key in ("tank_name", "tank_role"):
        if key in data:
            values[key] = _text(data[key])
        elif old is not None:
            values[key] = getattr(old, key)
    for key, (label, low, high) in TANK_NUMBERS.items():
        if key in data:
            if _blank(data[key]):
                values[key] = {"oxygen": 21.0, "helium": 0.0}.get(key)
            else:
                values[key] = _number(f"Tank {index + 1} {label}", data[key], low, high, False)
        elif old is not None:
            values[key] = getattr(old, key)
    tank = GasMixture(**values)
    if tank.oxygen + tank.helium > 100.0 + 1e-9:
        raise EditError(f"Tank {index + 1}: O2 {tank.oxygen:g} % and He {tank.helium:g} % add up to more than 100 %")
    return tank


def apply_edits(dive: UnifiedDive, fields: Mapping[str, Any]) -> UnifiedDive:
    """A copy of ``dive`` with ``fields`` set: the keys of `EDITABLE_FIELDS`
    (a key left out is untouched; a blank clears an optional field).
    Numbers are SI, except weight and visibility in the dive's own unit;
    ``duration``, ``surface_interval`` and ``bottom_time`` are seconds;
    ``date`` (``YYYY-MM-DD``), ``time`` (``HH:MM`` or ``HH:MM:SS``) and
    ``utc_offset`` (``+07:00``) go through `shift_dive`; ``dive_mode`` /
    ``water_type`` must be one of the known words (or blank); ``tanks`` is
    the whole tank table (dicts with tank_name, oxygen, helium, tank_volume,
    start_pressure, end_pressure, tank_role). A field the profile decides
    (`locked_fields`) is refused on a dive with samples. Raises `EditError`
    with the text to show; the dive given is never changed."""
    unknown = sorted(k for k in fields if k not in EDITABLE_FIELDS)
    if unknown:
        raise EditError(f"Not an editable field: {', '.join(unknown)}")
    locked = [k for k in locked_fields(dive) if k in fields]
    if locked:
        raise EditError(f"{', '.join(k.replace('_', ' ') for k in locked)}: set by the dive profile, not editable")
    out = dive.model_copy(deep=True)
    for key, (label, low, high, integer, required) in NUMBER_FIELDS.items():
        if key not in fields:
            continue
        if _blank(fields[key]):
            if required:
                raise EditError(f"{label}: a value is needed")
            setattr(out, key, None)
        else:
            setattr(out, key, _number(label, fields[key], low, high, integer))
    for key in TEXT_FIELDS:
        if key in fields:
            setattr(out, key, _text(fields[key]))
    for key, (label, choices) in CHOICE_FIELDS.items():
        if key not in fields:
            continue
        value = _text(fields[key])
        if value is not None and value not in choices:
            raise EditError(f"{label}: '{value}' is not one of {', '.join(choices)}")
        setattr(out, key, value)
    if "tanks" in fields:
        rows = list(fields["tanks"] or [])
        out.gas_mixtures = [_tank(i, row, dive.gas_mixtures[i] if i < len(dive.gas_mixtures) else None)
                            for i, row in enumerate(rows)]
    if out.temp_min is not None and out.temp_max is not None and out.temp_min > out.temp_max:
        raise EditError(f"Water temp: the minimum {out.temp_min:g} is above the maximum {out.temp_max:g}")
    if out.gf_low is not None and out.gf_high is not None and out.gf_low > out.gf_high:
        raise EditError(f"Gradient factors: GF low {out.gf_low} is above GF high {out.gf_high}")
    if "weight" in fields and out.weight is not None and not out.weight_unit:
        out.weight_unit = "kilogram"
    if "weight" in fields and out.weight is None:
        out.weight_unit = None
    if "visibility" in fields and out.visibility is not None and not out.visibility_unit:
        out.visibility_unit = "meter"
    if "visibility" in fields and out.visibility is None:
        out.visibility_unit = None
    if any(k in fields for k in ("date", "time", "utc_offset")):
        out = shift_dive(out, _start_from(out, fields), parse_tz(fields) if "utc_offset" in fields else ...)
    return out


def parse_tz(fields: Mapping[str, Any]) -> Optional[str]:
    """The ``timezone`` value for the form's ``utc_offset`` text."""
    return zone_for_offset(parse_utc_offset(fields.get("utc_offset")))


def _start_from(dive: UnifiedDive, fields: Mapping[str, Any]) -> datetime:
    """The new local start from the form's date and time texts (either may
    be left out: the dive's own is kept)."""
    date_text = str(fields.get("date", "")).strip() if "date" in fields else dive.date_time.strftime("%Y-%m-%d")
    time_text = str(fields.get("time", "")).strip() if "time" in fields else dive.date_time.strftime("%H:%M:%S")
    try:
        day = datetime.strptime(date_text, "%Y-%m-%d").date()
    except ValueError:
        raise EditError(f"Date: use YYYY-MM-DD, not '{date_text}'") from None
    clock = None
    for pattern in ("%H:%M:%S", "%H:%M"):
        try:
            clock = datetime.strptime(time_text, pattern).time()
            break
        except ValueError:
            continue
    if clock is None:
        raise EditError(f"Time: use HH:MM or HH:MM:SS, not '{time_text}'")
    return datetime.combine(day, clock)


def tank_rows(dive: UnifiedDive) -> List[Dict[str, Any]]:
    """The dive's tanks as the dicts ``apply_edits`` takes back (SI)."""
    return [{"tank_name": g.tank_name, "oxygen": g.oxygen, "helium": g.helium, "tank_volume": g.tank_volume,
             "start_pressure": g.start_pressure, "end_pressure": g.end_pressure, "tank_role": g.tank_role}
            for g in dive.gas_mixtures]
