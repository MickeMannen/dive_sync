"""Convert page backend (plans/convert.md I8).

Open dive-computer files (Garmin ``.fit``, Connect's "export original" zip,
UDDF, Subsurface ``.ssrf``), show the dives they hold, and write the selected
ones as UDDF or Subsurface ``.ssrf`` - one Save-as dialog for one dive, a
folder dialog and one file per dive for several (decision Q8).

No account of the sync is involved and nothing is written into the data
folder. Files are read on a `jobs.Worker`. MySSI left this page with
plans/myssi-sync.md K4 (2026-10-19): it is a sync service with its own
Dives page now, so there is no Open MySSI, no Send to SSI and no
``myssi:`` pseudo-source here any more - every dive in the list comes
from a file. The file dialogs open in Documents the first time and then
in the last folder used (`preferences.get_convert_folder`, decision Q11).

A FIT whose dive is in one of the app's Garmin account caches - found by
the activity id in the file's name, or by the start time when the name
holds none (a renamed file, an export of another account's copy) - gets
its site, buddy, notes, weight, visibility and tank volumes from the
cached dive (`src.core.convert.enrich`, I9, decision Q7): on by default, a
checkbox on the page, remembered in `desktop_prefs.json`. The dives are
kept as read and the enriched ones derived from them, so the checkbox
re-applies without re-reading the files; the cache is read once per open,
on the worker, the Garmin dives page's account first.

The list is a working list (I9b): Open adds to it (a dive already loaded,
from the same file or with the same start and number, is skipped), Remove
takes the selected dives off it, Clear empties it; the files on disk are
never touched.

"Open folder…" (I13) reads every dive file directly in a chosen folder -
`formats.list_dive_files`: by extension, subfolders not searched (the
owner's choice), hidden files skipped - through the same path as Open
files…, so duplicates, the cache fill and the message are the same; a
folder holding nothing readable is a plain message, not a warning per
file. The folder becomes the remembered one.

"Open Cached" (I9c) is the same Open dialog started in the Garmin FIT cache
of the preferred account (`layout.garmin_fit_dir`, under DATA_DIR - a folder
the diver would not find by hand); it leaves the remembered last folder
alone, so a Save after it does not land inside the app's data folder. The
button is off without an account or a `.fit` file in the folder; the page
re-checks when it is shown.

The list and the detail pane show depths, temperatures, tank volumes and
pressures, weight and visibility in the unit system chosen on the Settings
page (plans/units.md J4, `_quantity`); the dives themselves and the
writers stay SI.

The page is "Dive edit" since I14: the detail card is an editor for every
field the model holds. The form's values come from `form_values` (strings
in the shown unit system, the J3 rounding), an edit arrives as the whole
form (`edit(row, fields)`) and only the fields whose text differs from
what the form showed are applied - so a rounded display never drifts a
stored value - converted back to SI at full precision through the helpers
shared with the Dives pages (`controllers/editing.py`) and applied by
`src.core.convert.edit` (`apply_edits`, `shift_dive` for the start time
and zone, which moves the whole profile). The edited dive replaces the
shown one in the list at once and is what Save as writes; the dive before
the first edit is kept in ``_originals[row]``. The cache fill (I9) happens
before any edit and an edited dive keeps its edits when the checkbox is
toggled.

Unsaved edits (I15): a row whose shown dive is not its baseline
(``_edited[row] is not _originals[row]``) is marked - the dot in the list,
"edited" on the card, `editedRows` / `hasEdits` - until Revert puts the
baseline back (`revert`, `revertSelected`, `revertAll`) or Save as writes
it (any format: the dive written becomes the baseline, `_mark_saved`, so
Revert then goes back to what was saved, and a failed write keeps the
mark). Remove and Clear on the page, and closing the app (main.qml), ask
before a marked dive is lost; the controller only counts (`revertCount`).

Merge (I17, reworked in I17b): `mergeChain` is the selected dives in
start order, each row flagged ``edited`` when it has unsaved edits (the
merge reads the files on disk, so such an edit is not in the merged file
and the dialog says so); `mergeProblem` is why they cannot be merged -
a dive not opened from a ``.fit`` (or a Connect zip), fewer than two,
another dive computer, an overlap, more than `fit_merge.MAX_GAP_SECONDS`
between two - the rule being `fit_merge.merge_problem`'s on the dives as
read, "" when they can; `mergeProblemOf(rows)` re-checks the dialog's
ticked rows. The page's Merge… button (under the list) is on with two or
more dives selected and the dialog shows the reason in red with Save… off.
`mergeSerialsOf(rows)` is what the dialog shows when a ticked log has no
dive computer serial (the choices among the logs' serials and the note;
I20); `merge(rows, url, close_gap, serial)` runs `fit_merge.merge_fits` on the worker,
refusing a target equal to a source, and on success adds the merged
file's dive to the list through the normal open path (its source is the
merged file; a stale row of the same file is dropped first) and selects
it; a refusal or a failure is `mergeError`, shown under the list, and
nothing is added.

Debug report (I21): `canReport` is on when the selection cannot be merged
(`mergeProblem`, two or more selected) or the last merge failed
(`mergeError`); `debugReportUrl()` suggests ``DiveSync debug report <date
time>.txt`` in the dialogs' folder and `saveDebugReport(url[, rows])`
writes `debug_report.merge_report` there - for the dialog's ticked rows
when given, else for the last failed request (its paths, mode, serial,
error and traceback are kept when a merge fails), else for the selection
- with the app's recent log lines (`logging_bridge.recent_lines`). It
never writes anywhere but the file picked.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from PySide6.QtCore import Property, QObject, QStandardPaths, QUrl, Signal, Slot

from desktop import accounts, logging_bridge
from desktop import preferences
from desktop.jobs import Worker
from src.core import layout
from src.core import units as core_units
from desktop.controllers.editing import display_number, js_number, optional_float, si_number
from src.core.convert import debug_report, edit, enrich, fit_merge, formats, profile
from src.core.models import (CCR_NOTE, KNOWN_DIVE_MODES, KNOWN_WATER_TYPES, SAC_COMPUTED, SAC_LOGGED, UnifiedDive, dive_sac,
                             has_ccr_data)

logger = logging.getLogger("dive_sync.desktop.convert")

# Channel names in the order the page lists them (SampleChannels fields).
CHANNEL_LABELS = (
    ("ndl", "NDL"), ("tts", "time to surface"), ("deco_stop_depth", "deco stop"), ("cns", "CNS"),
    ("ppo2", "PO2"), ("setpoint", "setpoint"), ("gf99", "GF99"), ("gas_time", "gas time"), ("heart_rate", "heart rate"),
)
EVENT_WORDS = {"gas_switch": "gas switch", "alert": "alert", "mode_change": "mode change",
               "setpoint_change": "setpoint change", "bookmark": "bookmark"}
MAX_EVENT_LINES = 40


def local_path(value: Any) -> str:
    """A path from what a QML dialog hands over: a ``QUrl``, a ``file://``
    string or a plain path."""
    if isinstance(value, QUrl):
        return value.toLocalFile() if value.isLocalFile() else value.toString()
    text = str(value or "")
    if text.startswith("file:"):
        return QUrl(text).toLocalFile()
    return text


def documents_folder() -> str:
    """Where the dialogs open the first time (decision Q11): the platform's
    Documents folder through QStandardPaths, the home folder without one."""
    folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    return folder or os.path.expanduser("~")


def source_label(path: str) -> str:
    """What the page shows as a dive's source: its file's name."""
    return os.path.basename(path)


def preferred_garmin_account() -> str:
    """The Garmin account the cache lookup tries first (rework.md E19): the
    one the Garmin dives page works with, else the Sync page's; "" with
    none configured (the caches are then read in name order)."""
    try:
        return accounts.selected("dives", "garmin") or accounts.selected("sync", "garmin")
    except Exception as e:  # no credentials at all: not a reason to skip the cache
        logger.debug("No preferred Garmin account: %s", e)
        return ""


def garmin_cache_folder(account: str = "") -> str:
    """The folder holding the ``.fit`` files the app downloaded for a Garmin
    account (`layout.garmin_fit_dir`, under DATA_DIR); "" without an account.
    Nothing is created: the Garmin page's Download dives fills it."""
    return layout.garmin_fit_dir(account) if account else ""


def has_fit_files(folder: str) -> bool:
    """Whether ``folder`` exists and holds at least one ``.fit`` file."""
    if not folder or not os.path.isdir(folder):
        return False
    try:
        with os.scandir(folder) as entries:
            return any(e.is_file() and e.name.lower().endswith(".fit") for e in entries)
    except OSError as e:
        logger.debug("Cannot list %s: %s", folder, e)
        return False


def _num(value: Optional[float], unit: str = "", digits: int = 1) -> str:
    if value is None:
        return ""
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".") if digits else f"{value:.0f}"
    return f"{text} {unit}".strip()


def _minutes(seconds: Optional[int]) -> str:
    if seconds is None:
        return ""
    return f"{int(round(seconds / 60.0))} min"


# Units (rework.md Track J, plans/units.md J4). The dives the page holds are
# SI `UnifiedDive`s (metres, bar, °C, litres; weight and visibility in the
# dive's own unit, `weight_unit` / `visibility_unit`), and so is everything
# the writers get. Only the rows and the detail pane convert, here,
# through src/core/units.py: in metric every value is shown exactly as before
# (`_num`'s trimmed decimals), in imperial converted and rounded per the
# display rule. The profile samples stay SI; ProfileChart.qml labels its axis.
_METRIC_TEXT = {"depth": ("m", 1), "pressure": ("bar", 0), "temperature": ("°C", 1), "volume": ("l", 1), "weight": ("kg", 1),
                "sac": ("L/min", 1)}


def _quantity(kind: str, value: Optional[float], system: str = core_units.METRIC, own: str = core_units.METRIC) -> str:
    """A number of ``kind`` (depth, pressure, temperature, volume, weight)
    as the page shows it in ``system``: ``""`` for None; in the system it is
    held in (``own``, SI unless a weight / visibility carries the dive's
    imperial unit) exactly as before Track J (`_num`, trimmed decimals);
    otherwise converted through src/core/units.py and rounded per the
    display rule, with that system's label."""
    if value is None:
        return ""
    own, system = core_units.normalize(own), core_units.normalize(system)
    if own == system:
        unit, digits = _METRIC_TEXT[kind] if own == core_units.METRIC else (core_units.unit_label(kind, own), core_units.decimals(kind, own))
        return _num(value, unit, digits)
    return core_units.format_with_label(kind, core_units.convert_back(kind, value, own), system)


def _own_system(unit: Optional[str], imperial_prefix: str) -> str:
    """The system a dive's ``weight_unit`` / ``visibility_unit`` names
    (``"pound"`` / ``"feet"``, as the services spell them); metric for none."""
    return core_units.IMPERIAL if (unit or "").startswith(imperial_prefix) else core_units.METRIC


def _clock(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def mix_name(oxygen: Optional[float], helium: Optional[float]) -> str:
    """``air``, ``EAN32``, ``TMX 18/45``."""
    o2 = oxygen if oxygen is not None else 21.0
    he = helium or 0.0
    if he:
        return f"TMX {o2:g}/{he:g}"
    if abs(o2 - 21.0) > 0.5:
        return f"EAN{o2:g}"
    return "air"


# The editor (I14). `form_values` is what the card's fields show for a dive:
# strings in the shown unit system with the J3 rounding (metric as stored,
# imperial per the display rule), minutes for the times, "+07:00" for the
# zone; `ConvertController.edit` compares the form sent back against these
# to find the fields that were touched.
FORM_KINDS = {"max_depth": "depth", "avg_depth": "depth", "temp_min": "temperature", "temp_max": "temperature",
              "temp_avg": "temperature", "air_temp": "temperature", "sac": "sac"}
OWN_UNIT_KINDS = {"weight": ("weight", "weight_unit", "pound"), "visibility": ("depth", "visibility_unit", "f")}
MINUTE_FIELDS = {"duration": "Duration", "surface_interval": "Surface interval", "bottom_time": "Bottom time"}
PLAIN_NUMBER_FIELDS = ("dive_number", "water_density", "gf_low", "gf_high", "cns_start", "cns_end")
COORD_FIELDS = ("lat", "lng", "exit_lat", "exit_lng")
TEXT_FORM_FIELDS = edit.TEXT_FIELDS + tuple(edit.CHOICE_FIELDS)
TANK_FORM_KINDS = (("volume", "tank_volume", "volume"), ("start_pressure", "start_pressure", "pressure"),
                   ("end_pressure", "end_pressure", "pressure"))   # (form key, model key, kind)


def _minutes_text(seconds: Optional[int]) -> str:
    """Seconds as the form's minutes: ``"46.33"``, ``""`` for None."""
    return "" if seconds is None else js_number(round(seconds / 60.0, 2))


def _form_number(kind: str, value: Optional[float], system: str) -> str:
    """A stored SI number as the form shows it: the display rule in
    imperial (`display_number`); in metric as stored, a float's tail cut at
    six decimals (J3's echo rule) so a value converted from imperial earlier
    does not show as 4.535923700100354."""
    if system == core_units.METRIC and isinstance(value, float):
        return js_number(round(value, 6))
    return display_number(kind, value, system)


def _own_value_text(kind: str, value: Optional[float], own: str, system: str) -> str:
    """A weight / visibility held in the dive's own system, shown in
    ``system``: as held when the two agree, else converted and rounded."""
    if value is None:
        return ""
    if core_units.normalize(own) == core_units.normalize(system):
        return js_number(round(value, 6)) if isinstance(value, float) else js_number(value)
    return core_units.format_value(kind, core_units.convert_back(kind, value, own), system)


def form_values(dive: UnifiedDive, system: str = core_units.METRIC) -> Dict[str, Any]:
    """The editor's fields for ``dive`` as strings in ``system`` (see the
    note above); ``tanks`` is one dict per tank with the table's columns."""
    out: Dict[str, Any] = {
        "date": dive.date_time.strftime("%Y-%m-%d"),
        "time": dive.date_time.strftime("%H:%M:%S"),
        "utc_offset": edit.utc_offset_text(dive),
    }
    for key, label in MINUTE_FIELDS.items():
        out[key] = _minutes_text(getattr(dive, key))
    for key, kind in FORM_KINDS.items():
        out[key] = _form_number(kind, getattr(dive, key), system)
    for key in PLAIN_NUMBER_FIELDS:
        out[key] = js_number(getattr(dive, key))
    for key in COORD_FIELDS:
        value = getattr(dive, key)
        out[key] = "" if value is None else js_number(round(value, 6))
    for key, (kind, unit_attr, prefix) in OWN_UNIT_KINDS.items():
        out[key] = _own_value_text(kind, getattr(dive, key), _own_system(getattr(dive, unit_attr), prefix), system)
    for key in TEXT_FORM_FIELDS:
        out[key] = getattr(dive, key) or ""
    out["sac_hint"] = sac_hint(dive, system)
    out["tanks"] = [{
        "tank_name": gm.tank_name or "",
        "oxygen": js_number(gm.oxygen), "helium": js_number(gm.helium),
        "volume": _form_number("volume", gm.tank_volume, system),
        "start_pressure": _form_number("pressure", gm.start_pressure, system),
        "end_pressure": _form_number("pressure", gm.end_pressure, system),
        "role": gm.tank_role or "",
    } for gm in dive.gas_mixtures]
    return out


def sac_hint(dive: UnifiedDive, system: str = core_units.METRIC) -> str:
    """The SAC field's placeholder when the computer logged none: the value
    computed from the tanks, average depth and duration (R9), in ``system``
    - ``"computed from the tanks: 14.2"``; ``""`` when the dive holds a
    logged SAC (the field shows it) or nothing can be computed."""
    value, source = dive_sac(dive)
    if source != SAC_COMPUTED or value is None:
        return ""
    return f"computed from the tanks: {core_units.format_value('sac', value, system)}"


def sac_text(dive: UnifiedDive, system: str = core_units.METRIC) -> str:
    """The SAC line of the details: ``"14.2 L/min (from the dive computer)"``
    or ``"14.2 L/min (computed from the tanks)"``; ``""`` for neither."""
    value, source = dive_sac(dive)
    if value is None:
        return ""
    note = "from the dive computer" if source == SAC_LOGGED else "computed from the tanks"
    return f"{_quantity('sac', value, system)} ({note})"


def form_choices(dive: UnifiedDive) -> Dict[str, List[str]]:
    """The choice fields' options: blank, the known words, and the dive's
    own word when a reader kept an unknown one (so it is not lost)."""
    out = {}
    for key, known in (("dive_mode", KNOWN_DIVE_MODES), ("water_type", KNOWN_WATER_TYPES)):
        value = getattr(dive, key) or ""
        out[key] = [""] + list(known) + ([value] if value and value not in known else [])
    return out


def _texts_equal(a: Any, b: Any) -> bool:
    return ("" if a is None else str(a)).strip() == ("" if b is None else str(b)).strip()


def si_changes(dive: UnifiedDive, fields: Dict[str, Any], system: str = core_units.METRIC) -> Dict[str, Any]:
    """The fields of a form sent back that differ from what the form showed
    for ``dive`` in ``system`` (`form_values`), as `edit.apply_edits` takes
    them: SI at full precision for a quantity typed in imperial (`si_number`),
    the dive's own unit for weight and visibility, seconds for the minutes,
    text as typed for the rest (apply_edits validates it). An untouched
    field is left out, so the stored value survives exactly; a tank table
    is included whole when any cell or the row count changed, with every
    untouched cell carrying the stored value. Raises `edit.EditError` for a
    minutes field that is no number."""
    shown = form_values(dive, system)
    out: Dict[str, Any] = {}
    for key, value in fields.items():
        if key == "tanks" or key not in shown:
            continue
        if _texts_equal(value, shown[key]):
            continue
        text = ("" if value is None else str(value)).strip()
        if key in FORM_KINDS:
            out[key] = si_number(FORM_KINDS[key], text, shown[key], getattr(dive, key), system)
        elif key in MINUTE_FIELDS:
            minutes = optional_float(text)
            if text and minutes is None:
                raise edit.EditError(f"{MINUTE_FIELDS[key]}: not a number of minutes: '{text}'")
            out[key] = "" if minutes is None else int(round(minutes * 60))
        elif key in OWN_UNIT_KINDS:
            kind, unit_attr, prefix = OWN_UNIT_KINDS[key]
            own, number = _own_system(getattr(dive, unit_attr), prefix), optional_float(text)
            if number is None or core_units.normalize(own) == core_units.normalize(system):
                out[key] = text
            else:
                out[key] = core_units.convert(kind, core_units.convert_back(kind, number, system), own)
        else:
            out[key] = text
    if "tanks" in fields and fields["tanks"] is not None:
        typed = [dict(t) for t in fields["tanks"]]
        if len(typed) != len(shown["tanks"]) or any(
                not _texts_equal(t.get(k), s.get(k)) for t, s in zip(typed, shown["tanks"]) for k in s):
            rows = []
            for i, tank in enumerate(typed):
                shown_tank = shown["tanks"][i] if i < len(shown["tanks"]) else {}
                original = dive.gas_mixtures[i] if i < len(dive.gas_mixtures) else None
                row: Dict[str, Any] = {}
                for form_key, model_key in (("tank_name", "tank_name"), ("role", "tank_role"), ("oxygen", "oxygen"), ("helium", "helium")):
                    if form_key in tank:
                        untouched = _texts_equal(tank[form_key], shown_tank.get(form_key)) and original is not None
                        row[model_key] = getattr(original, model_key) if untouched else tank[form_key]
                for form_key, model_key, kind in TANK_FORM_KINDS:
                    if form_key in tank:
                        row[model_key] = si_number(kind, tank[form_key], js_number(shown_tank.get(form_key)),
                                                   getattr(original, model_key) if original is not None else None, system)
                rows.append(row)
            out["tanks"] = rows
    return out


def dive_row(index: int, dive: UnifiedDive, source: str = "", system: str = core_units.METRIC) -> Dict[str, Any]:
    """One line of the page's dive list; the depth in ``system`` (J4)."""
    return {
        "index": index,
        "date": dive.date_time.strftime("%Y-%m-%d"),
        "time": dive.date_time.strftime("%H:%M"),
        "dive_number": dive.dive_number if dive.dive_number is not None else "",
        "max_depth": _quantity("depth", dive.max_depth, system),
        "duration": _minutes(dive.duration),
        "location": dive.location or "",
        "source": source,
    }


def dive_details(dive: UnifiedDive, source: str = "", enriched: str = "", system: str = core_units.METRIC) -> Dict[str, Any]:
    """Everything the right-hand pane shows for one dive: the summary, the
    tanks, the profile samples (ProfileChart's shape), which extra channels
    the file has and the events. ``enriched`` is what the Garmin cache
    filled in (`enrich.Enrichment.summary`), blank for nothing. Depths,
    temperatures, tank volumes and pressures, weight and visibility are
    shown in ``system`` (J4, `_quantity`); the samples stay SI."""
    computer = " ".join(p for p in (dive.computer_vendor, dive.computer_model) if p)
    extras = [p for p in (f"serial {dive.computer_serial}" if dive.computer_serial else "",
                          f"firmware {dive.computer_firmware}" if dive.computer_firmware else "") if p]
    if extras:
        computer = f"{computer} ({', '.join(extras)})" if computer else ", ".join(extras)
    tanks = []
    for i, gm in enumerate(dive.gas_mixtures):
        tanks.append({
            "index": i + 1,
            "name": gm.tank_name or "",
            "mix": mix_name(gm.oxygen, gm.helium),
            "oxygen": _num(gm.oxygen, "%"), "helium": _num(gm.helium, "%"),
            "volume": _quantity("volume", gm.tank_volume, system),
            "start_pressure": _quantity("pressure", gm.start_pressure, system),
            "end_pressure": _quantity("pressure", gm.end_pressure, system),
            "role": (gm.tank_role or "").replace("_", " "),
        })
    samples = [{"time": s.time if s.time is not None else 0, "depth": s.depth, "temp": s.temp} for s in dive.samples]
    present = set()
    pressure_tanks = set()
    for s in dive.samples:
        if s.channels is None:
            if s.pressure is not None:
                pressure_tanks.add(0)
            continue
        pressure_tanks.update(s.channels.pressures.keys())
        for key, _label in CHANNEL_LABELS:
            if getattr(s.channels, key) is not None:
                present.add(key)
    channels = []
    if pressure_tanks:
        which = ", ".join(f"tank {i + 1}" for i in sorted(pressure_tanks))
        channels.append(f"tank pressure ({which})")
    channels += [label for key, label in CHANNEL_LABELS if key in present]
    events = []
    for e in dive.events[:MAX_EVENT_LINES]:
        kind = EVENT_WORDS.get(e.type, e.type.replace("_", " "))
        detail = ""
        if e.type == "gas_switch":
            if e.tank is not None:
                detail = f"to tank {e.tank + 1}"
            elif e.oxygen is not None:
                detail = f"to {mix_name(e.oxygen, e.helium)}"
        elif e.type == "setpoint_change" and e.value is not None:
            detail = f"{e.value:g} bar"
        elif e.name:
            detail = e.name
        elif e.tank is not None:
            detail = f"tank {e.tank + 1}"
        events.append(f"{_clock(e.time)}  {kind}" + (f": {detail}" if detail else ""))
    if len(dive.events) > MAX_EVENT_LINES:
        events.append(f"... and {len(dive.events) - MAX_EVENT_LINES} more")
    weight = _quantity("weight", dive.weight, system, _own_system(dive.weight_unit, "pound"))
    visibility = _quantity("depth", dive.visibility, system, _own_system(dive.visibility_unit, "f"))
    gf = f"{dive.gf_low}/{dive.gf_high}" if dive.gf_low is not None and dive.gf_high is not None else ""
    ids = ", ".join(f"{k} {v}" for k, v in sorted(dive.external_ids.items()) if v)
    return {
        "title": dive.date_time.strftime("%Y-%m-%d %H:%M") + (f" — {dive.location}" if dive.location else ""),
        "date_time": dive.date_time.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": dive.timezone or "",
        "dive_number": dive.dive_number if dive.dive_number is not None else "",
        "duration": _minutes(dive.duration),
        "bottom_time": _minutes(dive.bottom_time),
        "surface_interval": _minutes(dive.surface_interval),
        "max_depth": _quantity("depth", dive.max_depth, system), "avg_depth": _quantity("depth", dive.avg_depth, system),
        "temp_min": _quantity("temperature", dive.temp_min, system), "temp_max": _quantity("temperature", dive.temp_max, system),
        "temp_avg": _quantity("temperature", dive.temp_avg, system),
        "air_temp": _quantity("temperature", dive.air_temp, system), "suit": dive.suit or "",
        "location": dive.location or "", "buddy": dive.buddy or "", "notes": dive.notes or "",
        "weight": weight, "visibility": visibility,
        "lat": _num(dive.lat, "", 6), "lng": _num(dive.lng, "", 6),
        "exit_lat": _num(dive.exit_lat, "", 6), "exit_lng": _num(dive.exit_lng, "", 6),
        "computer": computer, "gf": gf, "deco_model": dive.deco_model or "",
        "dive_mode": (dive.dive_mode or "").replace("_", " "), "water_type": dive.water_type or "",
        "water_density": _num(dive.water_density, "kg/m³", 0),
        "cns": (f"{_num(dive.cns_start, '%', 0)} → {_num(dive.cns_end, '%', 0)}"
                if dive.cns_start is not None or dive.cns_end is not None else ""),
        # the SAC the page shows and where it comes from (R9); RMV and OTU as the computer logged them
        "sac": sac_text(dive, system), "sac_source": dive_sac(dive)[1],
        "rmv": _quantity("sac", dive.rmv, system), "otu": _num(dive.otu, "", 1),
        # CCR is untested (R18b): the page says so on a dive with rebreather data
        "ccr_note": CCR_NOTE if has_ccr_data(dive) else "",
        "external_ids": ids,
        "source": source,
        "enriched": enriched,
        "tanks": tanks,
        "samples": samples,
        "sample_count": len(samples),
        # the chart's gas-switch ticks and deco ceiling band (I19); depths in metres, the chart scales
        "markers": profile.gas_switch_markers(dive),
        "ceiling": profile.ceiling_series(dive),
        "channels": channels,
        "events": events,
        "event_count": len(dive.events),
        # the editor (I14)
        "form": form_values(dive, system),
        "choices": form_choices(dive),
        "locked": bool(dive.samples),
        "locked_fields": list(edit.locked_fields(dive)),
    }


class ConvertController(QObject):
    divesChanged = Signal()
    selectionChanged = Signal()
    messageChanged = Signal()
    busyChanged = Signal()
    folderChanged = Signal()
    cacheChanged = Signal()        # the Open Cached button (I9c)
    enrichChanged = Signal()       # the checkbox
    editChanged = Signal()         # editError, the edited rows / hasEdits (I14, I15)
    marksChanged = Signal()        # revertCount: follows the selection and the edits (I15)
    fitTargetChanged = Signal()    # fitSaveTip / fitDrops: follows the selection, the dives and the edits (I14b)
    mergeChanged = Signal()        # mergeChain / mergeProblem / mergeError: follow the selection, the dives and the edits (I17, I17b)

    def __init__(self, parent: Optional[QObject] = None, units=None):
        """``units`` is the app's ``UnitsController`` (plans/units.md J4):
        the list and the detail pane show its system and follow
        ``unitsChanged``; without one the page is metric."""
        super().__init__(parent)
        self._units_controller = units
        if units is not None:
            units.unitsChanged.connect(self._on_units_changed)
        for signal in (self.selectionChanged, self.divesChanged, self.editChanged):
            signal.connect(self.fitTargetChanged)
            signal.connect(self.mergeChanged)
        for signal in (self.selectionChanged, self.editChanged):
            signal.connect(self.marksChanged)
        self._dives: List[UnifiedDive] = []     # what the page shows (enriched when the checkbox is on, edited after an edit)
        self._dives_as_read: List[UnifiedDive] = []
        self._edited: Dict[int, UnifiedDive] = {}     # row -> the dive with its edits (I14; kept after a save, see _mark_saved)
        self._originals: Dict[int, UnifiedDive] = {}  # row -> the baseline Revert goes back to: the dive before its first edit, or as last saved (I15)
        self._edit_error = ""
        self._merge_error = ""                  # why the last merge was refused or failed (I17)
        self._last_failed: Optional[Dict[str, Any]] = None   # the request that failed, for the debug report (I21)
        self._fit_sources: Dict[str, bool] = {}   # path -> opened from a .fit / Connect zip (read once per file)
        self._cached: Dict[str, enrich.CachedDive] = {}   # the Garmin cache entries of the dives read (enrich.load_cached_dives keys)
        self._enrichments: List[enrich.Enrichment] = []
        self._enrich_enabled = preferences.get_convert_enrich()
        self._read_text = ""                    # the "n dives from m files" line, without the enrich count
        self._sources: List[str] = []           # one file name per dive (what the page shows)
        self._paths: List[str] = []             # the same as full paths (what "already loaded" compares)
        self._files: List[str] = []
        self._warnings: List[str] = []          # from the last read
        self._selection: List[int] = []
        self._current = -1                      # the dive the detail pane shows
        self._anchor = -1                       # for shift-click ranges
        self._message = ""
        self._save_warnings: List[str] = []
        self._busy = False
        self._worker = None

    # -- units (plans/units.md J4) -------------------------------------------

    def _system(self) -> str:
        """The unit system the page shows: the UnitsController's, else metric."""
        if self._units_controller is None:
            return core_units.METRIC
        return core_units.normalize(self._units_controller.units)

    def _on_units_changed(self) -> None:
        """The Settings page switched the system: the rows and the detail
        pane are rebuilt from the (unchanged, SI) dives."""
        self.divesChanged.emit()
        self.selectionChanged.emit()

    # -- files and dives ---------------------------------------------------

    @Property("QVariantList", notify=divesChanged)
    def dives(self):
        system = self._system()
        return [dive_row(i, d, self._sources[i] if i < len(self._sources) else "", system) for i, d in enumerate(self._dives)]

    @Property(int, notify=divesChanged)
    def diveCount(self) -> int:
        return len(self._dives)

    @Property("QVariantList", notify=divesChanged)
    def files(self):
        """The files whose dives are in the list, as names."""
        return [os.path.basename(f) for f in self._files]

    @Property("QVariantList", notify=divesChanged)
    def warnings(self):
        """What the readers reported about the files opened last."""
        return list(self._warnings)

    @Property("QVariantList", notify=selectionChanged)
    def selection(self):
        return list(self._selection)

    @Property(int, notify=selectionChanged)
    def selectedCount(self) -> int:
        return len(self._selection)

    @Property(int, notify=selectionChanged)
    def currentRow(self) -> int:
        return self._current

    @Property("QVariantMap", notify=selectionChanged)
    def selected(self):
        """The dive the detail pane shows (the one clicked last); {} with none."""
        if 0 <= self._current < len(self._dives):
            return dive_details(self._dives[self._current], self._sources[self._current],
                                self._enriched_summary(self._current), self._system())
        return {}

    @Slot(int, result="QVariantMap")
    def sampleDetails(self, index: int):
        """What the profile chart shows for sample ``index`` of the selected
        dive under the mouse (I19): ``profile.sample_details`` in the current
        unit system - time, depth, temperature, NDL or the deco stop, the
        active gas, each tank's pressure, CNS, PO2, setpoint, heart rate and
        gas time, only the lines the sample has. ``{}`` with no dive."""
        if 0 <= self._current < len(self._dives):
            return profile.sample_details(self._dives[self._current], int(index), self._system())
        return {}

    @Property(bool, notify=enrichChanged)
    def enrichFromCache(self) -> bool:
        """The checkbox (decision Q7): fill a FIT's site, buddy, notes, weight,
        visibility and tank volumes from the cached Garmin dive with the same
        activity id. On by default; remembered in desktop_prefs.json."""
        return self._enrich_enabled

    @Slot(bool)
    def setEnrichFromCache(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._enrich_enabled:
            return
        self._enrich_enabled = enabled
        preferences.set_convert_enrich(enabled)
        self.enrichChanged.emit()
        self._reapply_enrich()

    @Property(int, notify=divesChanged)
    def enrichedCount(self) -> int:
        """How many of the dives shown got something from the Garmin cache."""
        return len(self._enrichments)

    def _enriched_summary(self, index: int) -> str:
        for e in self._enrichments:
            if e.index == index:
                return e.summary
        return ""

    @Property(str, notify=messageChanged)
    def message(self) -> str:
        return self._message

    @Property("QVariantList", notify=messageChanged)
    def saveWarnings(self):
        """What the last write left out (the writer's drop lines)."""
        return list(self._save_warnings)

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy

    @Property("QVariantList", constant=True)
    def openNameFilters(self):
        return formats.open_name_filters()

    @Property("QVariantList", constant=True)
    def targets(self):
        """The file targets: [{id, label, extension, patch, title}], from
        the registry; ``patch`` marks Save as .fit, which patches the opened
        file's bytes for a dive opened from a .fit (I14b) and writes any
        other dive from scratch (I12, `fitEncodes`); `fitSaveTip` says when
        it is off."""
        return [{"id": f.id, "label": f.label, "extension": f.extension, "patch": f.patches_source,
                 "title": f"Save as {f.extension}…" if f.patches_source else f"Save as {f.label}…"}
                for f in formats.writable_formats()]

    @Property(str, notify=fitTargetChanged)
    def fitSaveTip(self) -> str:
        """Why Save as .fit is off for the selection ("" when it is on): it
        is on for any dives of one kind - all opened from a .fit or a
        Connect zip (patched), or none of them (written from scratch) -
        and off for a selection mixing the two (`formats.cannot_write_reason`)."""
        if not self._selection:
            return ""
        return formats.cannot_write_reason(formats.FIT, self._selected_sources())

    @Property(bool, notify=fitTargetChanged)
    def fitEncodes(self) -> bool:
        """Whether Save as .fit writes the selected dives from scratch (I12:
        none of them was opened from a .fit) rather than patching their
        own files (I14b); False with nothing selected."""
        rows = [i for i in self._selection if 0 <= i < len(self._dives)]
        return bool(rows) and not any(self._is_fit_source(i) for i in rows)

    @Property("QVariantList", notify=fitTargetChanged)
    def fitDrops(self):
        """What Save as .fit would not carry for the selected dives, as bare
        names for the page's "Not in a .fit" line: for a dive opened from a
        .fit the edited (or cache-filled) fields no FIT holds - site, buddy,
        notes, weight, visibility, a tank added on the page… -, for any
        other dive everything it holds that no FIT does (`fit_writer.fit_drops`);
        [] when nothing is lost or Save as .fit is off. A dive from a .fit
        equal to the one read needs no look at the file."""
        if self.fitSaveTip:
            return []
        rows = [i for i in self._selection if 0 <= i < len(self._dives)
                and (not self._is_fit_source(i) or self._dives[i] != self._dives_as_read[i])]
        if not rows:
            return []
        try:
            lines = formats.describe_drops([self._dives[i] for i in rows], formats.FIT,
                                           sources=[self._paths[i] for i in rows], originals=[self._dives_as_read[i] for i in rows])
        except Exception as e:  # a preview never breaks the page
            logger.debug("No FIT drop preview: %s", e)
            return []
        return [name for line in lines for name in line[len(formats.FIT_DROPS_PREFIX):].split(", ") if name]

    @Slot(str, result="QVariantList")
    def saveNameFilters(self, format_id: str):
        return formats.save_name_filters(format_id)

    @Property(str, notify=folderChanged)
    def dialogFolder(self) -> str:
        """Where the dialogs open, as a file URL: Documents the first time,
        then the last folder used (decision Q11)."""
        return QUrl.fromLocalFile(preferences.get_convert_folder(documents_folder())).toString()

    def _remember_folder(self, path: str) -> None:
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        if folder:
            preferences.set_convert_folder(folder)
            self.folderChanged.emit()

    # -- the Garmin FIT cache (I9c) --------------------------------------------

    @Property(str, notify=cacheChanged)
    def cacheAccount(self) -> str:
        """The Garmin account whose FIT cache Open Cached shows: the one the
        Garmin dives page works with (`preferred_garmin_account`); "" with none."""
        return preferred_garmin_account()

    @Property(str, notify=cacheChanged)
    def cacheFolder(self) -> str:
        """That account's FIT cache folder as a file URL for the dialog's
        ``currentFolder``; "" without an account."""
        folder = garmin_cache_folder(self.cacheAccount)
        return QUrl.fromLocalFile(folder).toString() if folder else ""

    @Property(bool, notify=cacheChanged)
    def cacheAvailable(self) -> bool:
        """Whether Open Cached has anything to open: an account, and a
        ``.fit`` file in its cache folder."""
        return has_fit_files(garmin_cache_folder(self.cacheAccount))

    @Property(str, notify=cacheChanged)
    def cacheTip(self) -> str:
        """The button's help: what it opens when it can, else why it is off."""
        account = self.cacheAccount
        if not account:
            return ("No Garmin account is set up. Open Cached opens the .fit files this app downloads from "
                    "Garmin Connect; add an account on the Settings page and use the Garmin page's Download dives.")
        if not self.cacheAvailable:
            return (f"Nothing cached yet for {account}: the Garmin page's Download dives fetches the .fit files "
                    "from Garmin Connect, and they are kept here.")
        return (f"Opens the .fit files this app downloaded from Garmin Connect (the Garmin page's dives) "
                f"for {account}. The dives are added to the list.")

    @Slot()
    def refreshCache(self) -> None:
        """Re-read the cache state (the page calls it when shown, so dives
        downloaded on the Garmin page meanwhile enable the button)."""
        self.cacheChanged.emit()

    def _set_message(self, text: str, save_warnings: Optional[List[str]] = None) -> None:
        self._message = text
        self._save_warnings = list(save_warnings or [])
        self.messageChanged.emit()

    def _set_busy(self, value: bool) -> None:
        self._busy = value
        self.busyChanged.emit()

    @Slot("QVariantList")
    def openFiles(self, urls) -> None:
        """Read every file picked in the Open dialog (several at once, Q6)
        and add their dives to the list (I9b); a dive already in it is
        skipped and counted in the message. The files' folder becomes the
        one the dialogs open in next."""
        self._open(urls, remember=True)

    @Slot("QVariantList")
    def openCachedFiles(self, urls) -> None:
        """`openFiles` for the Open Cached dialog (I9c): the same read, but
        the app's cache folder is not remembered as the last folder used,
        so a Save after it still opens where the diver's own files are."""
        self._open(urls, remember=False)

    @Slot(str)
    def openFolder(self, url) -> None:
        """Read every dive file directly in the folder picked (I13): the
        files `formats.list_dive_files` names, through `openFiles`' path -
        the same worker, duplicate skipping, cache fill and message - so a
        file that is no dive is a warning as there. Subfolders are not
        searched. A folder with nothing to read gives one message, not a
        warning per file. The folder becomes the one the dialogs open in."""
        folder = local_path(url)
        if not folder or self._busy:
            return
        try:
            files = formats.list_dive_files(folder)
        except NotADirectoryError:
            self._set_message(f"Not a folder: {folder}")
            return
        except OSError as e:
            self._set_message(f"Could not read: {folder}: {e.strerror or e}")
            return
        self._remember_folder(folder)
        if not files:
            self._set_message(f"No dive files in {folder}")
            return
        self._open([os.fspath(f) for f in files], remember=False)     # remembered above, as the folder itself

    def _open(self, urls, remember: bool, dedupe: bool = True, after=None) -> None:
        """Read ``urls`` on the worker and add their dives to the list.
        ``dedupe=False`` takes a dive even when its start and number are
        already listed (the merged file's dive has the first log's); ``after``
        is called once the dives are in the list (the merge sets its message)."""
        paths = [local_path(u) for u in urls or []]
        paths = [p for p in paths if p]
        if not paths or self._busy:
            return
        self._set_busy(True)
        self._set_message(f"Reading {len(paths)} file{'s' if len(paths) != 1 else ''}…")
        preferred = preferred_garmin_account()

        def work():
            result = formats.read_files(paths)
            # the cache entries of the dives read, looked up here so the
            # checkbox can re-apply them without touching the disk again
            cached = enrich.load_cached_dives(result.dives, preferred_account=preferred)
            return result, cached

        def done(outcome):
            result, cached = outcome
            self._set_busy(False)
            self._apply_read(result, cached, append=True, dedupe=dedupe)
            if remember:
                self._remember_folder(paths[0])
            if after is not None:
                after()

        def fail(text):
            self._set_busy(False)
            self._set_message(f"Could not read: {text}")

        worker = Worker(work, parent=self)
        worker.finished_ok.connect(done)
        worker.failed.connect(fail)
        self._worker = worker
        worker.start()

    def _enrich(self, dives: List[UnifiedDive]) -> List[UnifiedDive]:
        """The dives to show: filled from the Garmin cache entries read with
        them (`self._cached`) when the checkbox is on, else as read. Records
        what was filled for the detail pane and the page."""
        if not self._enrich_enabled or not self._cached:
            self._enrichments = []
            return list(dives)
        result = enrich.enrich_dives(dives, self._cached)
        self._enrichments = list(result.enrichments)
        return result.dives

    def _derive_dives(self) -> None:
        """The dives to show: the ones as read, filled from the cache when
        the checkbox is on, and for a row edited on the page its edited dive
        (an edit is made on the filled dive and wins over the checkbox)."""
        dives = self._enrich(self._dives_as_read)
        for row, dive in self._edited.items():
            if row < len(dives):
                dives[row] = dive
        self._dives = dives

    def _reapply_enrich(self) -> None:
        """The checkbox changed: derive the shown dives from the ones as read
        again, keeping the selection."""
        self._derive_dives()
        self.divesChanged.emit()
        self.selectionChanged.emit()
        if self._read_text:
            self._set_message(self._read_text + self._enrich_text())

    def _enrich_text(self) -> str:
        n = len(self._enrichments)
        return f" {n} filled from the Garmin cache." if n else ""

    @staticmethod
    def _paths_of(result: formats.ReadResult) -> List[str]:
        """The file each dive came from, one path per dive: ``result.sources``
        when the reader filled it; for a result built by hand, the one file
        for every dive, or one file per dive when the counts line up, else blanks."""
        if len(result.sources) == len(result.dives):
            return [os.fspath(p) for p in result.sources]
        if len(result.files) == 1:
            return [result.files[0][0]] * len(result.dives)
        if len(result.files) == len(result.dives):
            return [path for path, _fmt in result.files]
        return [""] * len(result.dives)

    @staticmethod
    def _dive_key(dive: UnifiedDive):
        """What makes two dives the same one for the list: the start and the number."""
        return dive.date_time, dive.dive_number

    def _apply_read(self, result: formats.ReadResult, cached: Optional[Dict[str, enrich.CachedDive]] = None,
                    append: bool = False, dedupe: bool = True) -> None:
        """Show a `formats.ReadResult` - in place of the list, or added to it
        (``append``, what Open does; a dive from a file already in the list,
        or with the start and number of one already there, is skipped). The
        tests feed a result directly. ``cached`` is what the worker found in
        the Garmin cache for the dives read; None looks it up here (never
        raises: a broken entry is a log line). ``dedupe=False`` skips the
        already-loaded check (the merged file's dive, I17)."""
        if cached is None:
            cached = enrich.load_cached_dives(result.dives, preferred_account=preferred_garmin_account())
        paths = self._paths_of(result)
        if not append:
            self._dives_as_read, self._sources, self._paths, self._files, self._cached = [], [], [], [], {}
            self._edited, self._originals = {}, {}
            self._set_edit_error("")
            self._set_merge_error("")
        known_files = set(self._files) | {p for p in self._paths if p}
        known_keys = {self._dive_key(d) for d in self._dives_as_read}
        first_new = len(self._dives_as_read)
        added = skipped = 0
        for dive, path in zip(result.dives, paths):
            key = self._dive_key(dive)
            if dedupe and ((path and path in known_files) or key in known_keys):
                skipped += 1
                continue
            known_keys.add(key)
            self._dives_as_read.append(dive)
            self._sources.append(source_label(path))
            self._paths.append(path)
            if path and path not in self._files:
                self._files.append(path)
            added += 1
        self._cached.update(cached)
        self._derive_dives()
        self._warnings = list(result.warnings)
        if added or not append:
            self._selection = [first_new] if self._dives else []
            self._current = self._anchor = first_new if self._dives else -1
        self.divesChanged.emit()
        self.selectionChanged.emit()
        self.editChanged.emit()
        n, m, total = len(result.dives), len(result.files), len(self._dives)
        where = f"{m} file{'s' if m != 1 else ''}"
        if append and total > added:
            text = f"{added} dive{'s' if added != 1 else ''} from {where} added; {total} in the list."
            if skipped:
                text += f" {skipped} already loaded."
        else:
            text = f"{n} dive{'s' if n != 1 else ''} from {where}."
        if result.warnings:
            text += f" {len(result.warnings)} warning{'s' if len(result.warnings) != 1 else ''}."
        self._read_text = text
        self._set_message(text + self._enrich_text())

    def _drop_rows(self, rows: Sequence[int]) -> None:
        """Take ``rows`` out of the list (the dives as read, sources, paths,
        files, edits and marks remapped); the selection is the caller's."""
        gone = set(rows)
        self._dives_as_read = [d for i, d in enumerate(self._dives_as_read) if i not in gone]
        self._sources = [s for i, s in enumerate(self._sources) if i not in gone]
        self._paths = [p for i, p in enumerate(self._paths) if i not in gone]
        left = set(self._paths)
        self._files = [p for p in self._files if p in left]
        remap = {old: new for new, old in enumerate(i for i in range(len(self._dives)) if i not in gone)}
        self._edited = {remap[i]: d for i, d in self._edited.items() if i in remap}
        self._originals = {remap[i]: d for i, d in self._originals.items() if i in remap}
        self._set_edit_error("")
        self._derive_dives()

    @Slot()
    def removeSelected(self) -> None:
        """Take the selected dives off the list (I9b): the files on disk are
        untouched. The row that moves into the first removed one's place is
        selected next; a file none of whose dives is left is forgotten, so
        opening it again adds them back. Unsaved edits go with the rows:
        the page asks first (`revertCount`), the controller does not."""
        rows = sorted(r for r in set(self._selection) if 0 <= r < len(self._dives_as_read))
        if not rows:
            return
        self._drop_rows(rows)
        if self._dives:
            row = min(rows[0], len(self._dives) - 1)
            self._selection, self._current, self._anchor = [row], row, row
        else:
            self._selection, self._current, self._anchor = [], -1, -1
        self.divesChanged.emit()
        self.selectionChanged.emit()
        self.editChanged.emit()
        n, total = len(rows), len(self._dives)
        self._read_text = f"Removed {n} dive{'s' if n != 1 else ''}; {total} in the list." if total else ""
        self._set_message(self._read_text + self._enrich_text() if total else f"Removed {n} dive{'s' if n != 1 else ''}.")

    @Slot()
    def clear(self) -> None:
        """Empty the list; nothing on disk changes. Unsaved edits go with
        it: the page asks first (`hasEdits`), the controller does not."""
        self._apply_read(formats.ReadResult(), {})
        self._read_text = ""
        self._set_message("")

    # -- the editor (I14) --------------------------------------------------

    @Property(str, notify=editChanged)
    def editError(self) -> str:
        """Why the last edit was refused (`edit.EditError`'s text); blank
        after an edit that was taken."""
        return self._edit_error

    def _is_marked(self, row: int) -> bool:
        """Whether ``row`` has unsaved edits: its shown dive is another
        object than its baseline (an edit makes one, a save or a Revert
        puts the baseline in its place)."""
        return row in self._edited and self._edited[row] is not self._originals.get(row)

    def _marked_rows(self) -> List[int]:
        return sorted(r for r in self._edited if self._is_marked(r))

    @Property("QVariantList", notify=editChanged)
    def editedRows(self):
        """The rows with unsaved edits: edited on the page and not saved or
        reverted since (the dot in the list, "edited" on the card; I15)."""
        return self._marked_rows()

    @Property(bool, notify=editChanged)
    def hasEdits(self) -> bool:
        """Whether any dive in the list has unsaved edits (I15): main.qml
        asks before closing the app, the page before Clear."""
        return bool(self._marked_rows())

    @Property(int, notify=marksChanged)
    def revertCount(self) -> int:
        """How many of the selected dives have unsaved edits: what Revert
        (one selected) or "Revert N" (several) puts back, and what Remove
        would lose (the page asks first when it is more than 0)."""
        return sum(1 for r in self._selection if self._is_marked(r))

    def _set_edit_error(self, text: str) -> None:
        if text != self._edit_error:
            self._edit_error = text
            self.editChanged.emit()

    def originalOf(self, row: int) -> Optional[UnifiedDive]:
        """The baseline Revert goes back to: the dive as it was shown before
        its first edit, or as Save as last wrote it; None when the row was
        never edited (or was reverted to what the file gave)."""
        return self._originals.get(row)

    def _revert_rows(self, rows: Sequence[int]) -> None:
        """Put the baseline back for ``rows`` (I15's Revert): the row shows
        `originalOf` again and its mark goes; the file on disk is not
        touched. A baseline that is what the list would show anyway (the
        dive as read, filled from the cache as the checkbox says) is simply
        dropped; one that is not - a dive saved once, a cache fill toggled
        since the first edit - stays as the row's dive, unmarked."""
        rows = [r for r in rows if self._is_marked(r)]
        if not rows:
            return
        baselines = {row: self._originals.pop(row) for row in rows}
        for row in rows:
            self._edited.pop(row, None)
        self._derive_dives()
        for row, baseline in baselines.items():
            if self._dives[row] != baseline:
                self._edited[row] = self._originals[row] = baseline
                self._dives[row] = baseline
        self._edit_error = ""
        self.divesChanged.emit()
        self.selectionChanged.emit()
        self.editChanged.emit()

    @Slot(int)
    def revert(self, row: int) -> None:
        """The card's Revert for one dive: the row shows `originalOf(row)`
        again, the mark goes, `selected` follows. Nothing for an unmarked row."""
        self._revert_rows([row])

    @Slot()
    def revertSelected(self) -> None:
        """"Revert N": every selected dive with unsaved edits goes back."""
        self._revert_rows(list(self._selection))

    @Slot()
    def revertAll(self) -> None:
        """Every unsaved edit in the list goes back (the "Discard" of
        main.qml's closing question)."""
        self._revert_rows(self._marked_rows())

    def _mark_saved(self, rows: Sequence[int]) -> None:
        """Save as wrote ``rows``: a marked row's dive as written becomes
        its baseline, so the mark goes and Revert goes back to what was
        saved. The dives as read stay what they are (Save as .fit diffs
        against them). Called only after a write that did not fail."""
        marked = [r for r in rows if self._is_marked(r)]
        for row in marked:
            self._originals[row] = self._edited[row]
        if marked:
            self.editChanged.emit()

    @Slot(int, "QVariantMap", result=str)
    def edit(self, row: int, fields) -> str:
        """Apply the card's form to the dive at ``row`` (the whole form, as
        the page sends it on every field commit): only the fields whose text
        differs from what the form showed are applied (`si_changes`), so an
        untouched field never drifts; a date, time or zone change shifts the
        whole dive (`edit.shift_dive`). The edited dive replaces the shown
        one at once - the list row, the detail card and what Save as writes -
        and the dive before the first edit is kept (`originalOf`) and the
        row marked until Revert or Save as (I15). Returns the error text
        (also `editError`) when a value is refused, in which case nothing
        changes; "" otherwise."""
        if not 0 <= row < len(self._dives):
            return ""
        dive = self._dives[row]
        try:
            changes = si_changes(dive, dict(fields or {}), self._system())
            if not changes:
                self._set_edit_error("")
                return ""
            edited = edit.apply_edits(dive, changes)
        except edit.EditError as e:
            self._set_edit_error(str(e))
            return str(e)
        self._originals.setdefault(row, dive)
        self._edited[row] = edited
        self._dives[row] = edited
        self._edit_error = ""
        self.divesChanged.emit()
        self.selectionChanged.emit()
        self.editChanged.emit()
        return ""

    # -- merge (I17, I17b) -------------------------------------------------

    def _set_merge_error(self, text: str) -> None:
        if text != self._merge_error:
            self._merge_error = text
            self.mergeChanged.emit()

    def _is_fit_source(self, row: int) -> bool:
        """Whether the dive at ``row`` was opened from a ``.fit`` or a Connect
        zip (what the merge reads and Save as .fit patches; `formats.source_is`),
        read once per file."""
        path = self._paths[row] if 0 <= row < len(self._paths) else ""
        if not path:
            return False
        if path not in self._fit_sources:
            self._fit_sources[path] = formats.source_is(formats.FIT, path)
        return self._fit_sources[path]

    @staticmethod
    def _utc_start(dive: UnifiedDive) -> datetime:
        return dive.date_time_utc if dive.date_time_utc is not None else dive.date_time

    def _merge_rows(self) -> List[int]:
        """The rows the Merge dialog shows: the selected dives in start order
        (the dives as read - the files on disk are what is merged). [] with
        nothing selected."""
        rows = [r for r in self._selection if 0 <= r < len(self._dives_as_read)]
        return sorted(rows, key=lambda r: self._utc_start(self._dives_as_read[r]))

    def _merge_problem_of(self, rows: Sequence[int]) -> str:
        """Why the dives at ``rows`` (in the order given) cannot be merged, ""
        when they can: a dive not opened from a .fit / Connect zip is named
        first (the merge reads Garmin .fit logs), then
        `fit_merge.merge_problem`'s rule on the dives as read - two or more,
        the same dive computer, in start order, no overlap, each starting
        within the gap limit of the previous one's end."""
        rows = [int(r) for r in rows if 0 <= int(r) < len(self._dives_as_read)]
        for row in rows:
            if not self._is_fit_source(row):
                dive = self._dives_as_read[row]
                name = self._sources[row] or (f"dive {dive.dive_number}" if dive.dive_number is not None else "a dive without a file")
                return f"{name} is not a Garmin .fit log; Merge joins .fit logs only"
        return fit_merge.merge_problem([self._dives_as_read[r] for r in rows]) or ""

    @Property("QVariantList", notify=mergeChanged)
    def mergeChain(self):
        """The Merge dialog's rows: the selected dives in start order (see
        `_merge_rows`), each as {row, date, time, dive_number, duration,
        source, edited} - the values as read from the file, and ``edited``
        when the row has unsaved edits, which the merge does not see. []
        with nothing selected; `mergeProblem` says when they do not merge."""
        return [{
            "row": row,
            "date": dive.date_time.strftime("%Y-%m-%d"),
            "time": dive.date_time.strftime("%H:%M:%S"),
            "dive_number": dive.dive_number if dive.dive_number is not None else "",
            "duration": _minutes(dive.duration),
            "source": self._sources[row],
            "edited": self._is_marked(row),
        } for row in self._merge_rows() for dive in (self._dives_as_read[row],)]

    @Property(str, notify=mergeChanged)
    def mergeProblem(self) -> str:
        """Why the selected dives (in start order) cannot be merged, "" when
        they can (`_merge_problem_of`): a dive not from a .fit, fewer than
        two, another dive computer, an overlap, more than the gap limit
        apart. The dialog shows it in red with Save… off."""
        return self._merge_problem_of(self._merge_rows())

    @Slot("QVariantList", result=str)
    def mergeProblemOf(self, rows) -> str:
        """`mergeProblem` for the dialog's ticked rows (in the order given),
        re-checked when a tick changes."""
        return self._merge_problem_of([int(r) for r in (rows or [])])

    @Slot("QVariantList", result="QVariantMap")
    def mergeSerialsOf(self, rows) -> dict:
        """What the Merge dialog needs about the dive computer serial of
        the dives at ``rows`` (the ticked rows, in order; I20): ``choices``
        - the distinct serials among them in order of first appearance
        (`fit_merge.serial_choices`; the first is the first log's when it
        has one, the dialog's default) -, ``missing`` - the names of the
        dives without one (`fit_merge.missing_serials`) - and ``note``: with
        two or more rows and a dive without a serial, why the dialog asks
        ("dive 9 has no dive computer serial; the merged file needs one so
        Garmin Connect knows which watch it came from") or, when no dive
        has one, that the merged file will have none; "" otherwise."""
        rows = [int(r) for r in (rows or []) if 0 <= int(r) < len(self._dives_as_read)]
        dives = [self._dives_as_read[r] for r in rows]
        choices, missing = fit_merge.serial_choices(dives), fit_merge.missing_serials(dives)
        note = ""
        if len(rows) > 1 and missing:
            if choices:
                names = missing[0] if len(missing) == 1 else ", ".join(missing[:-1]) + " and " + missing[-1]
                note = (f"{names} {'has' if len(missing) == 1 else 'have'} no dive computer serial; the merged file needs one "
                        "so Garmin Connect knows which watch it came from.")
            else:
                note = "No log carries a dive computer serial; Garmin Connect may not accept the merged file."
        return {"choices": choices, "missing": missing, "note": note}

    @Property(str, notify=mergeChanged)
    def mergeError(self) -> str:
        """Why the last merge was refused or failed (`fit_merge.MergeError`'s
        text, or the write's); blank after one that succeeded."""
        return self._merge_error

    @Slot(result=str)
    def mergeFileUrl(self) -> str:
        """The Save dialog's file for the merge: `fit_merge.merged_file_name`
        of the first selected log's file (in start order) beside that
        file (else in the dialogs' folder), as a file URL; blank with
        fewer than two dives selected or a first one without a file."""
        chain = self._merge_rows()
        if len(chain) < 2 or not self._paths[chain[0]]:
            return ""
        source = self._paths[chain[0]]
        folder = os.path.dirname(source) if os.path.isdir(os.path.dirname(source)) else preferences.get_convert_folder(documents_folder())
        return QUrl.fromLocalFile(os.path.join(folder, fit_merge.merged_file_name(source))).toString()

    @Slot("QVariantList", str, bool)
    @Slot("QVariantList", str, bool, str)
    def merge(self, rows, url: str, close_gap: bool, serial: str = "") -> None:
        """Join the logs of ``rows`` (the dialog's ticked chain, in order;
        two or more) into the file picked, keeping the real times and
        filling the gaps or closing them (``close_gap``), on the worker.
        ``serial`` is the dive computer serial the merged file gets when
        the first log has none (the dialog's choice among
        `mergeSerialsOf`'s; "" for none chosen, when `fit_merge` takes the
        first serial any log carries, or none at all with a warning).
        A target equal to one of the logs is refused (`mergeError`) and
        nothing written; `fit_merge.MergeError` or a write failure is
        `mergeError` too and nothing is added. On success the merged file's
        dive joins the list - its source the merged file; a row of that
        same file (an earlier merge overwritten) is dropped first - and is
        selected; the originals stay in the list and on disk."""
        rows = [int(r) for r in (rows or [])]
        dest = local_path(url)
        if self._busy or not dest:
            return
        request: Dict[str, Any] = {"paths": [self._paths[r] for r in rows if 0 <= r < len(self._paths)],
                                   "mode": bool(close_gap), "serial": (serial or "").strip(), "traceback": None}

        def refuse(text: str) -> None:
            self._last_failed = {**request, "error": text}
            self._set_merge_error(text)

        if len(rows) < 2 or any(not 0 <= r < len(self._paths) or not self._paths[r] for r in rows):
            refuse("Two or more logs are needed; pick the first and at least one later log.")
            return
        paths = request["paths"]
        if any(os.path.abspath(dest) == os.path.abspath(p) for p in paths):
            refuse("The merged file cannot replace one of the logs; choose another name.")
            return
        self._set_merge_error("")
        self._set_busy(True)
        self._set_message(f"Merging {len(paths)} logs…")

        def work():
            try:
                return fit_merge.merge_fits(paths, dest, bool(close_gap), request["serial"] or None)
            except Exception as e:  # the worker reports str(e); the report wants the traceback too
                request["traceback"] = debug_report.format_exception(e)
                raise

        def report(result: fit_merge.MergeResult):
            def one(n, word):
                return f"{n} {word}{'s' if n != 1 else ''}"
            if close_gap:
                how = f"the surface time removed, {one(result.closed_seconds, 'second')} taken out of the dive"
            else:
                how = f"the surface time added, {one(result.filled_samples, 'sample')} filling it"
            text = f"Merged {one(result.files, 'log')} into {dest}: {how}."
            if result.warnings:
                text += " " + " ".join(w.rstrip(".") + "." for w in result.warnings)
            self._read_text = text
            self._set_message(text + self._enrich_text())

        def done(result):
            self._set_busy(False)
            self._last_failed = None
            stale = [r for r, p in enumerate(self._paths) if p and os.path.abspath(p) == os.path.abspath(dest)]
            if stale:
                self._drop_rows(stale)
                self._selection, self._current, self._anchor = [], -1, -1
            self._open([dest], remember=True, dedupe=False, after=lambda: report(result))

        def fail(text):
            self._set_busy(False)
            self._set_message(self._read_text + self._enrich_text() if self._read_text else "")
            refuse(text)

        worker = Worker(work, parent=self)
        worker.finished_ok.connect(done)
        worker.failed.connect(fail)
        self._worker = worker
        worker.start()

    # -- debug report (I21) --------------------------------------------------

    @Property(bool, notify=mergeChanged)
    def canReport(self) -> bool:
        """Whether there is a refusal or a failure to report: the selection
        (two or more) cannot be merged (`mergeProblem`), or the last merge
        failed (`mergeError`). The page shows Generate debug report… in
        the dialog next to the reason and under the list next to the error."""
        return bool(self._merge_error) or (len(self._merge_rows()) > 1 and bool(self.mergeProblem))

    @Slot(result=str)
    def debugReportUrl(self) -> str:
        """The Save dialog's file for the report: ``DiveSync debug report
        <YYYY-MM-DD HHMM>.txt`` in the dialogs' folder (the last one used,
        else Documents), as a file URL."""
        name = f"DiveSync debug report {datetime.now().strftime('%Y-%m-%d %H%M')}.txt"
        return QUrl.fromLocalFile(os.path.join(preferences.get_convert_folder(documents_folder()), name)).toString()

    def _report_request(self, rows: Optional[Sequence[int]]) -> Dict[str, Any]:
        """What the report describes: the dialog's ticked ``rows`` when
        given (the refusal being `mergeProblemOf` theirs), else the last
        failed merge (paths, mode, serial, error, traceback), else the
        selection in start order with its `mergeProblem`."""
        if rows is not None:
            rows = [int(r) for r in rows if 0 <= int(r) < len(self._paths)]
            return {"paths": [self._paths[r] for r in rows if self._paths[r]], "problem": self._merge_problem_of(rows) or None,
                    "error": None, "traceback": None, "mode": None, "serial": None}
        if self._last_failed is not None:
            return {"paths": self._last_failed["paths"], "problem": None, "error": self._last_failed.get("error"),
                    "traceback": self._last_failed.get("traceback"), "mode": self._last_failed.get("mode"),
                    "serial": self._last_failed.get("serial") or None}
        rows = self._merge_rows()
        return {"paths": [self._paths[r] for r in rows if self._paths[r]], "problem": self._merge_problem_of(rows) or None,
                "error": None, "traceback": None, "mode": None, "serial": None}

    @Slot(str)
    @Slot(str, "QVariantList")
    def saveDebugReport(self, url: str, rows=None) -> None:
        """Write `debug_report.merge_report` for `_report_request` (``rows``:
        the dialog's ticked rows, None from under the list) and the recent
        log lines to the file picked (``url``), and say so in `message`;
        a write failure is `mergeError`. Nothing else is written anywhere."""
        dest = local_path(url)
        if not dest:
            return
        request = self._report_request(rows)
        text = debug_report.merge_report(request["paths"], problem=request["problem"], error=request["error"],
                                         traceback_text=request["traceback"], mode=request["mode"], serial=request["serial"],
                                         log_lines=logging_bridge.recent_lines())
        try:
            with open(dest, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            self._set_merge_error(f"Could not write {os.path.basename(dest)}: {e.strerror or e}")
            return
        self._set_message(f"Wrote the debug report to {dest}. Send it to the developer with a note of what you did.")

    # -- selection ---------------------------------------------------------

    @Slot(int, bool, bool)
    def clickRow(self, row: int, toggle: bool = False, extend: bool = False) -> None:
        """A click on a dive: plain selects it alone, Ctrl/Cmd toggles it,
        Shift selects the range from the last plain click."""
        if not 0 <= row < len(self._dives):
            return
        if extend and self._anchor >= 0:
            lo, hi = sorted((self._anchor, row))
            rows = list(range(lo, hi + 1))
            self._selection = sorted(set(self._selection) | set(rows)) if toggle else rows
        elif toggle:
            if row in self._selection:
                self._selection = [r for r in self._selection if r != row]
            else:
                self._selection = sorted(self._selection + [row])
            self._anchor = row
        else:
            self._selection = [row]
            self._anchor = row
        self._current = row
        self.selectionChanged.emit()

    @Slot(int)
    def select(self, row: int) -> None:
        self.clickRow(row, False, False)

    @Slot()
    def selectAll(self) -> None:
        self._selection = list(range(len(self._dives)))
        if self._current < 0 and self._dives:
            self._current = self._anchor = 0
        self.selectionChanged.emit()

    def _selected_dives(self) -> List[UnifiedDive]:
        return [self._dives[i] for i in self._selection if 0 <= i < len(self._dives)]

    def _selected_sources(self) -> List[str]:
        """The file each selected dive came from, in selection order ("" for none)."""
        return [self._paths[i] if i < len(self._paths) else "" for i in self._selection if 0 <= i < len(self._dives)]

    def _selected_originals(self) -> List[UnifiedDive]:
        """The selected dives as read from their files (before the cache
        fill and the edits): what the FIT patcher diffs against."""
        return [self._dives_as_read[i] for i in self._selection if 0 <= i < len(self._dives)]

    # -- save as -----------------------------------------------------------

    @Slot(str, result=str)
    def suggestedFileName(self, format_id: str) -> str:
        """The Save-as dialog's file name for the one selected dive (I4's
        ``<date> <time> dive <n>.<ext>``; Save as .fit on a dive opened from
        a .fit: ``<original name> (edited).fit``); blank unless exactly one
        is selected."""
        dives = self._selected_dives()
        if len(dives) != 1:
            return ""
        return formats.output_file_name(dives[0], format_id, self._selected_sources()[0])

    @Slot(str, result=str)
    def suggestedFileUrl(self, format_id: str) -> str:
        """`suggestedFileName` inside the dialogs' folder (Save as .fit on a
        dive opened from a .fit: beside that file), as the file URL the
        Save-as dialog's ``currentFile`` takes; blank unless one dive is selected."""
        name = self.suggestedFileName(format_id)
        if not name:
            return ""
        folder = preferences.get_convert_folder(documents_folder())
        if formats.get_format(format_id).patches_source and self._is_fit_source(self._selection[0]):
            source = self._selected_sources()[0]
            if source and os.path.isdir(os.path.dirname(source)):
                folder = os.path.dirname(source)
        return QUrl.fromLocalFile(os.path.join(folder, name)).toString()

    def _write_args(self, format_id: str) -> Dict[str, Any]:
        """The FIT target's extra arguments (the selected dives' source
        files and the dives as read, from which the registry tells a dive
        to patch from one to encode); nothing for the other formats."""
        if not formats.get_format(format_id).patches_source:
            return {}
        return {"sources": self._selected_sources(), "originals": self._selected_originals()}

    @Slot(str, result="QVariantList")
    def targetDrops(self, format_id: str):
        """What writing the selected dives as ``format_id`` would leave out."""
        try:
            return formats.describe_drops(self._selected_dives(), format_id, **self._write_args(format_id))
        except Exception as e:  # a preview never breaks the page
            logger.debug("No drop preview for %s: %s", format_id, e)
            return []

    @Slot(str, str)
    def saveAs(self, format_id: str, url: str) -> None:
        """Write the one selected dive to the file picked in the Save-as
        dialog. Save as .fit patches the dive's own file into the one picked
        (`formats.write_file` refuses the opened file itself) when the dive
        came from a .fit, and writes it from scratch otherwise. A dive
        written is saved: its unsaved-edit mark goes (`_mark_saved`); a
        write that fails keeps it."""
        dives = self._selected_dives()
        path = local_path(url)
        if not dives or not path:
            return
        if len(dives) != 1:
            self._set_message("Several dives are selected: pick a folder instead, one file is written per dive.")
            return
        tip = self.fitSaveTip if formats.get_format(format_id).patches_source else ""
        if tip:
            self._set_message(tip)
            return
        try:
            warnings = formats.write_file(dives, format_id, path, **self._write_args(format_id))
        except Exception as e:
            self._set_message(f"Could not write {os.path.basename(path)}: {e}")
            return
        self._mark_saved(self._selection)
        self._remember_folder(path)
        self._set_message(f"Wrote {path}", warnings)

    @Slot(str, str)
    def saveEach(self, format_id: str, folder_url: str) -> None:
        """Write the selected dives into the folder picked, one file per dive
        (decision Q8); a name already taken gets a (2), (3) suffix. The
        dives written are saved: their unsaved-edit marks go (`_mark_saved`)."""
        dives = self._selected_dives()
        folder = local_path(folder_url)
        if not dives or not folder:
            return
        tip = self.fitSaveTip if formats.get_format(format_id).patches_source else ""
        if tip:
            self._set_message(tip)
            return
        try:
            result = formats.write_each(dives, format_id, folder, **self._write_args(format_id))
        except Exception as e:
            self._set_message(f"Could not write into {folder}: {e}")
            return
        self._mark_saved(self._selection)
        self._remember_folder(folder)
        n = len(result.paths)
        self._set_message(f"Wrote {n} file{'s' if n != 1 else ''} to {folder}", result.warnings)
