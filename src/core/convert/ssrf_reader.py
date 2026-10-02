"""Subsurface's XML divelog (``.ssrf``, or Subsurface's ``.xml`` export) into
`UnifiedDive`s (plans/convert.md I6b): the input side of the Convert page's
Subsurface format, mirroring the I6 writer `ssrf_writer` (its constants, the
same ``extradata`` keys, the same event numbers) and the value parsing the
git-storage reader in `services/subsurface.py` already does for the same
unit strings (``12.3 m``, ``200.0 bar``, ``22.0 C``, ``45:30 min``).

Read, from a file Subsurface itself wrote or one of ours:

- ``settings/divecomputerid`` (serial and firmware by ``deviceid``;
  Subsurface's ``fingerprint`` lines are hashes, not serials, and are left
  alone), ``divesites/site`` (name and ``gps``) joined by ``divesiteid``;
- every ``dive``, inside a ``trip`` or not, in file order: number, local
  date and time, duration, rating / visibility stars / tags / suit /
  divemaster into ``service_fields`` as the Subsurface adapter keeps them,
  buddy and notes, cylinders (size, description as the tank name, O2/He,
  start and end, ``use`` back through `USE_TO_TANK_ROLE`; start and end
  from the first and last pressure sample when the cylinder has none, as
  the adapter does), weightsystems summed in kg;
- the first ``divecomputer`` (the others are named in a warning): model as
  vendor and model, ``dctype``, depth max / mean, water temperature (else
  the coldest sample), ``water salinity`` as the density and, for
  Subsurface's and Garmin's standard values, the water type, surfacetime,
  ``extradata`` (the deco model with its gradient factors, serial and
  firmware, ``GPS2`` as the exit position, bottom time, start and end CNS,
  the dive mode and water type the writer stores there, Subsurface's own
  ``Model`` and ``GPS1`` of a Garmin import, and ``dive_sync:<service>`` as
  the other services' ids); events (gaschange with its cylinder and mix,
  modechange, SP change, bookmarks; every other event an alert with the
  computer's own name and value); samples with time, depth, temperature,
  ``pressureN`` (legacy ``pressure`` and ``o2pressure`` too, and a plain
  ``sensorN='k'`` remapping slot N to cylinder k), ndl, tts, rbt, in_deco /
  stoptime / stopdepth, cns, po2, dc_supplied_ppo2 and heartbeat.

Subsurface's sticky rule applies: a sample's temperature, deco, CNS, PO2 and
heart-rate values persist until the next written one (the file holds them
only where they change); the tank pressures do not (Subsurface writes every
reading, 0 meaning none). ``po2`` is the setpoint while the dive is on a
rebreather (``dctype`` CCR / PSCR, followed through its modechange events)
and the computer's PO2 otherwise, which is how Subsurface itself stores an
open-circuit import. Imperial unit strings (``ft``, ``psi``, ``F``, ``lbs``,
``cuft`` with a working pressure) are converted. A sample whose only extra
is the first tank's pressure comes back as the Subsurface adapter produces
it: ``pressure`` set, ``channels`` None.

Not read (no `UnifiedDive` slot): the O2 sensor readings (``sensor1..3``
in bar), ``otu``, ``sac``, the surface pressure, air temperature, pictures,
the filter presets, a trip's own name and notes. No ``subsurface`` external
id is set: a dive in a file has no stable identity outside Subsurface.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple, Union
from xml.etree import ElementTree as ET

from src.core.convert.ssrf_writer import (
    DIVEMODE_OC,
    DIVEMODE_TEXT,
    EVENT_NAME_BOOKMARK,
    EVENT_NAME_GASCHANGE,
    EVENT_NAME_MODECHANGE,
    EVENT_NAME_SETPOINT,
    EVENT_TYPE_BOOKMARK,
    EVENT_TYPE_GASCHANGE,
    EVENT_TYPE_PO2,
    EXTRA_BOTTOM_TIME,
    EXTRA_CNS_END,
    EXTRA_CNS_START,
    EXTRA_DECO_MODEL,
    EXTRA_DIVE_MODE,
    EXTRA_EXIT_GPS,
    EXTRA_FIRMWARE,
    EXTRA_SERIAL,
    EXTRA_WATER_TYPE,
    SALINITY_BY_WATER_TYPE,
    SF_DIVEMASTER,
    SF_RATING,
    SF_SUIT,
    SF_TAGS,
    SF_VISIBILITY,
)
from src.core.models import (
    EVENT_ALERT,
    EVENT_BOOKMARK,
    EVENT_GAS_SWITCH,
    EVENT_MODE_CHANGE,
    EVENT_SETPOINT_CHANGE,
    KNOWN_DIVE_MODES,
    DiveEvent,
    GasMixture,
    SampleChannels,
    UnifiedDive,
    UnifiedSample,
    recorded_water_temp,
)
from src.core.services.subsurface import (
    EXTERNAL_ID_PREFIX,
    MANUAL_DC_MODEL,
    USE_TO_TANK_ROLE,
    parse_duration,
    parse_number,
)

logger = logging.getLogger("dive_sync.convert.ssrf_reader")

PathLike = Union[str, "os.PathLike[str]"]

EVENT_TYPE_GASCHANGE_LEGACY = 11   # SAMPLE_EVENT_GASCHANGE: the mix in ``value`` (o2 + he << 16), no cylinder
EXTRA_MODEL = "Model"              # Subsurface's Garmin import: the specific model behind libdivecomputer's family name
EXTRA_ENTRY_GPS = "GPS1"           # Subsurface's Garmin import: the entry position
SAMPLE_ATTR_DC_PPO2 = "dc_supplied_ppo2"

# Subsurface's divemode_text back to UnifiedDive.dive_mode; "OC" is settled per dive (one mix or several).
DIVEMODE_FROM_TEXT: Dict[str, str] = {v: k for k, v in DIVEMODE_TEXT.items()}
REBREATHER_MODES = ("ccr", "scr")

# Salinity (g/l) back to a water type: Subsurface's three values and Garmin's
# seawater (1025, what Subsurface's Garmin import and the I6 writer store for
# a FIT's "salt"). Anything else is a density without a type.
WATER_TYPE_BY_SALINITY: Dict[int, str] = {v: k for k, v in SALINITY_BY_WATER_TYPE.items()}
WATER_TYPE_BY_SALINITY[1025] = "salt"

# Makers whose name libdivecomputer writes as two words in front of the model.
TWO_WORD_VENDORS = ("Heinrichs Weikamp", "Deep Six", "Dive Rite", "Sea & Sea", "Scubapro Galileo")

FT_TO_M = 0.3048
PSI_TO_BAR = 0.0689476
LB_TO_KG = 0.45359237
CUFT_TO_L = 28.3168
ATM_BAR = 1.01325

_DECO_GF_RE = re.compile(r"(?:\bGF\s*)?(\d{1,3}|\?)\s*/\s*(\d{1,3}|\?)")
_ZHL_RE = re.compile(r"zhl[-_ ]?16\s*([a-c])\b", re.IGNORECASE)
_PRESSURE_ATTR_RE = re.compile(r"^pressure(\d*)$")
_SENSOR_ATTR_RE = re.compile(r"^sensor(\d+)$")
_GPS_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$")


class SsrfReadError(ValueError):
    """The file is not a Subsurface divelog (wrong root element)."""


# ---------------------------------------------------------------------------
# Value parsing: Subsurface's unit strings, imperial ones converted
# ---------------------------------------------------------------------------

def quantity(text: Optional[str]) -> Tuple[Optional[float], str]:
    """``("12.3 m")`` -> ``(12.3, "m")``: the leading number (the git-storage
    reader's `parse_number`) and the unit after it, lower case."""
    if text is None:
        return None, ""
    value = parse_number(text)
    if value is None:
        return None, ""
    unit = re.sub(r"^\s*-?\d+(?:\.\d+)?\s*", "", text).strip().lower()
    return value, unit


def depth_m(text: Optional[str]) -> Optional[float]:
    value, unit = quantity(text)
    if value is None:
        return None
    if unit.startswith("ft") or unit == "feet":
        return round(value * FT_TO_M, 3)
    if unit == "mm":
        return round(value / 1000.0, 3)
    return value


def temperature_c(text: Optional[str]) -> Optional[float]:
    value, unit = quantity(text)
    if value is None:
        return None
    if unit.startswith("f"):
        return round((value - 32.0) * 5.0 / 9.0, 3)
    if unit.startswith("k"):
        return round(value - 273.15, 3)
    return value


def pressure_bar(text: Optional[str]) -> Optional[float]:
    value, unit = quantity(text)
    if value is None:
        return None
    if unit.startswith("psi"):
        return round(value * PSI_TO_BAR, 3)
    if unit == "mbar":
        return round(value / 1000.0, 3)
    if unit == "pa":
        return round(value / 100000.0, 3)
    return value


def volume_l(text: Optional[str], workpressure_bar: Optional[float]) -> Optional[float]:
    """A cylinder size in litres of water capacity. ``cuft`` is the gas it
    holds at its working pressure (Subsurface's rule), so without one the
    size cannot be known and None is returned."""
    value, unit = quantity(text)
    if value is None:
        return None
    if unit.startswith("cuft") or unit.startswith("cu ft"):
        if not workpressure_bar:
            return None
        return round(value * CUFT_TO_L * ATM_BAR / workpressure_bar, 3)
    if unit == "ml":
        return round(value / 1000.0, 3)
    return value


def weight_kg(text: Optional[str]) -> Optional[float]:
    value, unit = quantity(text)
    if value is None:
        return None
    if unit.startswith("lb"):
        return round(value * LB_TO_KG, 3)
    if unit == "g":
        return round(value / 1000.0, 3)
    return value


def percent(text: Optional[str]) -> Optional[float]:
    value, _unit = quantity(text)
    return value


def seconds(text: Optional[str]) -> Optional[int]:
    """``"45:30 min"`` -> 2730 (the git-storage reader's `parse_duration`); a bare number is minutes, as in Subsurface."""
    if text is None or not text.strip():
        return None
    return parse_duration(text)


def parse_gps(text: Optional[str]) -> Tuple[Optional[float], Optional[float]]:
    """``"lat lng"`` (a site's ``gps``) or ``"lat, lng"`` (the GPS extradata)."""
    m = _GPS_RE.match(text or "")
    if not m:
        return None, None
    return float(m.group(1)), float(m.group(2))


def parse_deco_model(text: Optional[str]) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    """``"ZHL-16C GF 40/85"``, ``"Buhlmann ZHL-16C 40/85"`` (Subsurface's
    Garmin import), ``"GF 45/75"`` (its Shearwater import) or ``"VPM-B +2"``
    -> the model (a Bühlmann variant canonical as ``ZHL-16C``, anything else
    as written) and the gradient factors."""
    if not text or not text.strip():
        return None, None, None
    gf_low = gf_high = None
    rest = text.strip()
    m = _DECO_GF_RE.search(rest)
    if m:
        gf_low = int(m.group(1)) if m.group(1).isdigit() else None
        gf_high = int(m.group(2)) if m.group(2).isdigit() else None
        rest = (rest[:m.start()] + rest[m.end():]).strip()
    hit = _ZHL_RE.search(rest)
    model = f"ZHL-16{hit.group(1).upper()}" if hit else (rest or None)
    return model, gf_low, gf_high


def split_model(text: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """``"Shearwater Perdix 2"`` -> ``("Shearwater", "Perdix 2")``: the first
    word is the maker (two for the makers libdivecomputer names so), the
    rest the model. Subsurface's "manually added dive" is no computer."""
    text = (text or "").strip()
    if not text or text == MANUAL_DC_MODEL:
        return None, None
    for vendor in TWO_WORD_VENDORS:
        if text.lower().startswith(vendor.lower() + " "):
            return text[:len(vendor)], text[len(vendor):].strip() or None
    parts = text.split(None, 1)
    if len(parts) == 1:
        return None, parts[0]
    return parts[0], parts[1]


def _attr_or_child(el: ET.Element, name: str) -> Optional[str]:
    """``el``'s attribute ``name``, else the text of its child ``name``: the
    forms Subsurface's own parser accepts interchangeably."""
    value = el.get(name)
    if value is None:
        child = el.find(name)
        if child is not None:
            value = child.text
    return value.strip() if value is not None and value.strip() else None


def _child_text(el: Optional[ET.Element], name: str) -> Optional[str]:
    if el is None:
        return None
    child = el.find(name)
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text or None


def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.startswith("{"):
            el.tag = el.tag.split("}", 1)[1]


# ---------------------------------------------------------------------------
# Shared tables
# ---------------------------------------------------------------------------

class _Site:
    def __init__(self, name: Optional[str], lat: Optional[float], lng: Optional[float]):
        self.name, self.lat, self.lng = name, lat, lng


def _sites(root: ET.Element) -> Dict[str, _Site]:
    out: Dict[str, _Site] = {}
    for site in root.findall("divesites/site"):
        uuid = (site.get("uuid") or "").strip().lower()
        if not uuid:
            continue
        lat, lng = parse_gps(_attr_or_child(site, "gps"))
        out[uuid] = _Site(_attr_or_child(site, "name"), lat, lng)
    return out


def _computers(root: ET.Element) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """``deviceid`` -> (serial, firmware) from ``settings/divecomputerid``."""
    out: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    for dc in root.findall("settings/divecomputerid"):
        device_id = (dc.get("deviceid") or "").strip().lower()
        if device_id:
            serial = (dc.get("serial") or "").strip() or None
            firmware = (dc.get("firmware") or "").strip() or None
            out[device_id] = (serial, firmware)
    return out


# ---------------------------------------------------------------------------
# One dive
# ---------------------------------------------------------------------------

def _oc_mode(cylinders: Sequence[GasMixture]) -> str:
    """Subsurface's "OC" as `UnifiedDive.dive_mode` says it: one mix among the tanks in use, or several."""
    mixes = {(round(g.oxygen, 1), round(g.helium, 1)) for g in cylinders if g.tank_role != "not_used"}
    return "oc_single_gas" if len(mixes) <= 1 else "oc_multi_gas"


def _dive_mode(dctype: Optional[str], extradata: Dict[str, str], cylinders: Sequence[GasMixture]) -> str:
    """``dctype`` first (CCR / PSCR / Freedive), else the writer's "Dive mode"
    extradata (gauge, or which open-circuit flavour), else OC by the mixes."""
    if dctype and dctype.strip() in DIVEMODE_FROM_TEXT:
        return DIVEMODE_FROM_TEXT[dctype.strip()]
    stored = (extradata.get(EXTRA_DIVE_MODE) or "").strip()
    if stored:
        return stored
    return _oc_mode(cylinders)


def _mode_from_text(text: Optional[str], oc_mode: str) -> str:
    """A modechange event's ``divemode`` in `UnifiedDive.dive_mode`'s words."""
    text = (text or "").strip()
    if text in DIVEMODE_FROM_TEXT:
        return DIVEMODE_FROM_TEXT[text]
    if text.lower() in KNOWN_DIVE_MODES:
        return text.lower()
    return oc_mode if text in ("", DIVEMODE_OC) else text


def _cylinders(dive: ET.Element, warnings: List[str], label: str) -> List[GasMixture]:
    out: List[GasMixture] = []
    for index, cyl in enumerate(dive.findall("cylinder")):
        workpressure = pressure_bar(cyl.get("workpressure"))
        size_text = cyl.get("size")
        volume = volume_l(size_text, workpressure)
        if volume is None and size_text and "cu" in size_text.lower():
            warnings.append(f"{label}: cylinder {index + 1} is sized in cubic feet without a working pressure; its size is not read")
        o2 = percent(cyl.get("o2"))
        he = percent(cyl.get("he"))
        out.append(GasMixture(
            oxygen=o2 if o2 else 21.0,
            helium=he or 0.0,
            start_pressure=pressure_bar(cyl.get("start")),
            end_pressure=pressure_bar(cyl.get("end")),
            tank_volume=volume,
            tank_name=(cyl.get("description") or "").strip() or None,
            tank_role=USE_TO_TANK_ROLE.get((cyl.get("use") or "").strip()),
        ))
    return out


def _weight_kg(dive: ET.Element) -> Optional[float]:
    weights = [w for w in (weight_kg(ws.get("weight")) for ws in dive.findall("weightsystem")) if w is not None]
    return round(sum(weights), 3) if weights else None


def _extradata(dc: ET.Element) -> Dict[str, str]:
    """The computer's ``extradata`` pairs, the first of a repeated key winning."""
    out: Dict[str, str] = {}
    for el in dc.findall("extradata"):
        key, value = (el.get("key") or "").strip(), (el.get("value") or "").strip()
        if key and key not in out:
            out[key] = value
    return out


def _oxygen_slot(cylinders: Sequence[GasMixture]) -> int:
    """The cylinder a legacy ``o2pressure`` belongs to: the oxygen one, else the second."""
    for i, gas in enumerate(cylinders):
        if gas.tank_role == "oxygen":
            return i
    return 1


class _RawSample:
    """One ``sample`` element parsed, before the sticky values are resolved into a `UnifiedSample`."""
    __slots__ = ("time", "depth", "temp", "pressures", "values")

    def __init__(self) -> None:
        self.time: int = 0
        self.depth: Optional[float] = None
        self.temp: Optional[float] = None
        self.pressures: Dict[int, float] = {}
        self.values: Dict[str, object] = {}


def _raw_samples(dc: ET.Element, cylinders: Sequence[GasMixture]) -> List[_RawSample]:
    """Every ``sample`` with a depth, in file order; the pressure slots mapped
    to cylinders through Subsurface's ``sensorN='k'`` (sticky, identity by default)."""
    out: List[_RawSample] = []
    slot_to_cylinder: Dict[int, int] = {}
    o2_slot = _oxygen_slot(cylinders)
    for el in dc.findall("sample"):
        depth = depth_m(el.get("depth"))
        if depth is None:
            continue
        raw = _RawSample()
        raw.time = seconds(el.get("time")) or 0
        raw.depth = depth
        raw.temp = temperature_c(el.get("temp"))
        # the sensor mapping first: it applies to this sample's pressures whatever the attribute order
        for name, value in el.attrib.items():
            m = _SENSOR_ATTR_RE.match(name)
            if m and value.strip().isdigit():
                slot_to_cylinder[int(m.group(1))] = int(value)
        for name, value in el.attrib.items():
            m = _PRESSURE_ATTR_RE.match(name)
            if m:
                slot = int(m.group(1) or 0)
            elif name == "o2pressure":
                slot = o2_slot
            else:
                continue
            bar = pressure_bar(value)
            if bar:
                raw.pressures[slot_to_cylinder.get(slot, slot)] = bar
        for name in ("ndl", "tts", "rbt", "stoptime"):
            if name in el.attrib:
                raw.values[name] = seconds(el.get(name))
        if "in_deco" in el.attrib:
            raw.values["in_deco"] = (el.get("in_deco") or "").strip() not in ("0", "", "false")
        if "stopdepth" in el.attrib:
            raw.values["stopdepth"] = depth_m(el.get("stopdepth"))
        if "cns" in el.attrib:
            raw.values["cns"] = percent(el.get("cns"))
        if "po2" in el.attrib:
            raw.values["po2"] = pressure_bar(el.get("po2"))
        if SAMPLE_ATTR_DC_PPO2 in el.attrib:
            raw.values["dc_ppo2"] = pressure_bar(el.get(SAMPLE_ATTR_DC_PPO2))
        if "heartbeat" in el.attrib:
            hr = parse_number(el.get("heartbeat") or "")
            raw.values["heartbeat"] = int(hr) if hr is not None else None
        out.append(raw)
    return out


def _samples(raws: Sequence[_RawSample], mode_changes: Sequence[Tuple[int, str]], initial_mode: str) -> List[UnifiedSample]:
    """The `UnifiedSample`s with Subsurface's sticky rule applied, ``po2``
    read as the setpoint while the dive is on a rebreather."""
    sensors = {sensor for raw in raws for sensor in raw.pressures}
    primary = min(sensors) if sensors else None
    # a computer that reports its own PO2 separately (Subsurface's dc_supplied_ppo2) is a rebreather
    # one, so its ``po2`` is the setpoint throughout, bailout included
    has_dc_ppo2 = any("dc_ppo2" in raw.values for raw in raws)
    last: Dict[str, object] = {}
    last_temp: Optional[float] = None
    mode, change_at = initial_mode, 0
    out: List[UnifiedSample] = []
    for raw in raws:
        while change_at < len(mode_changes) and mode_changes[change_at][0] <= raw.time:
            mode = mode_changes[change_at][1]
            change_at += 1
        if raw.temp is not None:
            last_temp = raw.temp
        last.update(raw.values)
        rebreather = mode in REBREATHER_MODES or has_dc_ppo2
        po2 = last.get("po2")
        stopdepth = last.get("stopdepth")
        if stopdepth is None and "in_deco" in last and not last["in_deco"]:
            stopdepth = 0.0
        channels = SampleChannels(
            pressures=dict(raw.pressures),
            ndl=last.get("ndl"),
            tts=last.get("tts"),
            deco_stop_depth=stopdepth,
            deco_stop_time=last.get("stoptime"),
            cns=last.get("cns"),
            ppo2=last.get("dc_ppo2") if last.get("dc_ppo2") is not None else (None if rebreather else po2),
            setpoint=po2 if rebreather else None,
            gas_time=last.get("rbt"),
            heart_rate=last.get("heartbeat"),
        )
        pressure = raw.pressures.get(primary) if primary is not None else None
        held = channels.model_dump()
        if not held or (primary == 0 and held == {"pressures": {0: pressure}}):
            channels = None
        out.append(UnifiedSample(depth=raw.depth, temp=last_temp, time=raw.time, pressure=pressure, channels=channels))
    return out


def _find_cylinder(cylinders: Sequence[GasMixture], o2: Optional[float], he: Optional[float]) -> Optional[int]:
    if o2 is None:
        return None
    for i, gas in enumerate(cylinders):
        if abs(gas.oxygen - o2) < 0.15 and abs(gas.helium - (he or 0.0)) < 0.15:
            return i
    return None


def _events(dc: ET.Element, cylinders: Sequence[GasMixture], oc_mode: str) -> List[DiveEvent]:
    out: List[DiveEvent] = []
    for el in dc.findall("event"):
        time = seconds(el.get("time")) or 0
        type_text = (el.get("type") or "").strip()
        etype = int(type_text) if type_text.isdigit() else None
        name = (el.get("name") or "").strip() or None
        flags = parse_number(el.get("flags") or "")
        value = parse_number(el.get("value") or "")
        cylinder_text = (el.get("cylinder") or "").strip()
        tank = int(cylinder_text) if cylinder_text.isdigit() else None
        if name == EVENT_NAME_GASCHANGE or etype in (EVENT_TYPE_GASCHANGE, EVENT_TYPE_GASCHANGE_LEGACY):
            o2, he = percent(el.get("o2")), percent(el.get("he"))
            if o2 is None and value is not None and "o2" not in el.attrib:
                raw = int(value)
                o2, he = float(raw & 0xFFFF), float(raw >> 16)
            if tank is None and etype == EVENT_TYPE_GASCHANGE and flags and int(flags) > 0:
                tank = int(flags) - 1
            if tank is None:
                tank = _find_cylinder(cylinders, o2, he)
            if tank is not None and not (0 <= tank < len(cylinders)):
                tank = None
            if o2 is None and tank is not None:
                o2, he = cylinders[tank].oxygen, cylinders[tank].helium
            if o2 is not None and he is None:
                he = 0.0
            out.append(DiveEvent(time=time, type=EVENT_GAS_SWITCH, tank=tank, oxygen=o2, helium=he))
        elif name == EVENT_NAME_MODECHANGE or "divemode" in el.attrib:
            out.append(DiveEvent(time=time, type=EVENT_MODE_CHANGE, name=_mode_from_text(el.get("divemode"), oc_mode)))
        elif name == EVENT_NAME_SETPOINT or etype == EVENT_TYPE_PO2:
            setpoint = round(value / 1000.0, 3) if value is not None else None
            out.append(DiveEvent(time=time, type=EVENT_SETPOINT_CHANGE, value=setpoint))
        elif etype == EVENT_TYPE_BOOKMARK:
            out.append(DiveEvent(time=time, type=EVENT_BOOKMARK, name=None if name == EVENT_NAME_BOOKMARK else name))
        else:
            out.append(DiveEvent(time=time, type=EVENT_ALERT, name=name, value=value, tank=tank))
    out.sort(key=lambda e: e.time)
    return out


def _when(dive: ET.Element) -> Optional[datetime]:
    date, time = _attr_or_child(dive, "date"), _attr_or_child(dive, "time")
    if not date:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(f"{date} {time or ''}".strip(), fmt)
        except ValueError:
            continue
    return None


def _dive_label(dive: ET.Element, when: Optional[datetime], ordinal: int) -> str:
    number = dive.get("number")
    if number:
        return f"dive {number}"
    if when is not None:
        return f"dive of {when:%Y-%m-%d %H:%M}"
    return f"dive #{ordinal}"


def _read_dive(dive: ET.Element, sites: Dict[str, _Site], computers: Dict[str, Tuple[Optional[str], Optional[str]]],
               ordinal: int, warnings: List[str], file_label: str) -> Optional[UnifiedDive]:
    when = _when(dive)
    label = f"{file_label}: {_dive_label(dive, when, ordinal)}"
    if when is None:
        warnings.append(f"{label} has no date and is skipped")
        return None

    # --- the dive's own fields ---------------------------------------------
    site = sites.get((dive.get("divesiteid") or "").strip().lower())
    location = site.name if site else None
    lat, lng = (site.lat, site.lng) if site else (None, None)
    legacy = dive.find("location")          # pre-divesites files: <location gps='lat lng'>Name</location>
    if site is None and legacy is not None:
        location = (legacy.text or "").strip() or None
        lat, lng = parse_gps(legacy.get("gps"))
    tags = [t.strip() for t in (dive.get("tags") or "").split(",") if t.strip()]
    cylinders = _cylinders(dive, warnings, label)

    # --- the dive computer -------------------------------------------------
    dcs = dive.findall("divecomputer")
    if len(dcs) > 1:
        others = ", ".join((dc.get("model") or "unnamed computer").strip() or "unnamed computer" for dc in dcs[1:])
        warnings.append(f"{label}: only the first dive computer ({(dcs[0].get('model') or 'unnamed').strip()}) is read; "
                        f"{len(dcs) - 1} more not read: {others}")
    dc: Optional[ET.Element] = dcs[0] if dcs else (dive if dive.find("sample") is not None or dive.find("depth") is not None else None)
    model_text = (dc.get("model") or "").strip() if dc is not None and dc is not dive else ""
    extradata = _extradata(dc) if dc is not None else {}
    vendor, model = split_model(model_text)
    if extradata.get(EXTRA_MODEL) and model_text != MANUAL_DC_MODEL:
        model = extradata[EXTRA_MODEL]
    device_id = (dc.get("deviceid") or "").strip().lower() if dc is not None else ""
    serial, firmware = computers.get(device_id, (None, None))
    serial = serial or extradata.get(EXTRA_SERIAL) or None
    firmware = firmware or extradata.get(EXTRA_FIRMWARE) or None
    dive_mode = _dive_mode(dc.get("dctype") if dc is not None else None, extradata, cylinders)
    oc_mode = _oc_mode(cylinders)

    depth_el = dc.find("depth") if dc is not None else None
    if depth_el is None:
        depth_el = dive.find("depth")
    temp_el = dc.find("temperature") if dc is not None else None
    if temp_el is None:
        temp_el = dive.find("temperature")
    water_el = dc.find("water") if dc is not None else None
    salinity_text = (water_el.get("salinity") if water_el is not None else None) or dive.get("watersalinity")
    salinity, _unit = quantity(salinity_text)

    events = _events(dc, cylinders, oc_mode) if dc is not None else []
    mode_changes = [(e.time, e.name or oc_mode) for e in events if e.type == EVENT_MODE_CHANGE]
    raws = _raw_samples(dc, cylinders) if dc is not None else []
    samples = _samples(raws, mode_changes, dive_mode)

    # start and end pressures from the profile when the cylinder line has none (the adapter's rule)
    first_last: Dict[int, Tuple[float, float]] = {}
    for raw in raws:
        for sensor, bar in raw.pressures.items():
            first_last[sensor] = (first_last.get(sensor, (bar, bar))[0], bar)
    for index, gas in enumerate(cylinders):
        if index in first_last:
            if gas.start_pressure is None:
                gas.start_pressure = first_last[index][0]
            if gas.end_pressure is None:
                gas.end_pressure = first_last[index][1]

    duration = seconds(dive.get("duration"))
    if duration is None:
        duration = samples[-1].time if samples and samples[-1].time is not None else 0
    max_depth = depth_m(depth_el.get("max")) if depth_el is not None else None
    if max_depth is None and samples:
        max_depth = max(s.depth for s in samples)
    avg_depth = depth_m(depth_el.get("mean")) if depth_el is not None else None
    water = recorded_water_temp(temperature_c(temp_el.get("water"))) if temp_el is not None else None
    if water is None:
        temps = [s.temp for s in samples if s.temp is not None]
        water = recorded_water_temp(min(temps)) if temps else None

    deco_model, gf_low, gf_high = parse_deco_model(extradata.get(EXTRA_DECO_MODEL))
    exit_lat, exit_lng = parse_gps(extradata.get(EXTRA_EXIT_GPS))
    if lat is None and lng is None and extradata.get(EXTRA_ENTRY_GPS):
        lat, lng = parse_gps(extradata.get(EXTRA_ENTRY_GPS))
    water_type = (extradata.get(EXTRA_WATER_TYPE) or "").strip() or None
    if water_type is None and salinity is not None:
        water_type = WATER_TYPE_BY_SALINITY.get(int(round(salinity)))
    cns_end = percent(extradata.get(EXTRA_CNS_END))
    if cns_end is None:
        cns_end = percent(dive.get("cns"))
    external_ids = {key[len(EXTERNAL_ID_PREFIX):]: value for key, value in extradata.items()
                    if key.startswith(EXTERNAL_ID_PREFIX) and key[len(EXTERNAL_ID_PREFIX):] and value}
    surface_text = _child_text(dc, "surfacetime") if dc is not None else None
    rating, visibility = parse_number(dive.get("rating") or ""), parse_number(dive.get("visibility") or "")
    weight = _weight_kg(dive)
    number = parse_number(dive.get("number") or "")

    # provenance as the file says it: Subsurface's own hand-logged computer, or a named computer with a profile
    device_logged: Optional[bool] = None
    if model_text == MANUAL_DC_MODEL:
        device_logged = False
    elif samples and (vendor or model):
        device_logged = True

    return UnifiedDive(
        date_time=when,
        duration=int(duration or 0),
        max_depth=max_depth or 0.0,
        avg_depth=avg_depth,
        temp_min=water,
        external_ids=external_ids,
        gas_mixtures=cylinders,
        location=location,
        notes=_child_text(dive, "notes"),
        dive_number=int(number) if number is not None and number > 0 else None,
        weight=weight,
        weight_unit="kilogram" if weight is not None else None,
        buddy=_child_text(dive, "buddy"),
        lat=lat,
        lng=lng,
        samples=samples,
        device_logged=device_logged,
        service_fields={
            SF_SUIT: _child_text(dive, "suit"),
            SF_DIVEMASTER: _child_text(dive, "divemaster"),
            SF_RATING: int(rating) if rating is not None else None,
            SF_VISIBILITY: int(visibility) if visibility is not None else None,
            SF_TAGS: tags,
        },
        events=events,
        computer_vendor=vendor,
        computer_model=model,
        computer_serial=serial,
        computer_firmware=firmware,
        gf_low=gf_low,
        gf_high=gf_high,
        deco_model=deco_model,
        water_type=water_type,
        water_density=float(salinity) if salinity else None,
        dive_mode=dive_mode,
        exit_lat=exit_lat,
        exit_lng=exit_lng,
        surface_interval=seconds(surface_text) if surface_text else None,
        bottom_time=seconds(extradata.get(EXTRA_BOTTOM_TIME)) if extradata.get(EXTRA_BOTTOM_TIME) else None,
        cns_start=percent(extradata.get(EXTRA_CNS_START)),
        cns_end=cns_end,
    )


# ---------------------------------------------------------------------------
# The whole file
# ---------------------------------------------------------------------------

def parse_ssrf(root: ET.Element, name: str = "") -> Tuple[List[UnifiedDive], List[str]]:
    """The dives of a parsed ``<divelog>`` (trips flattened, file order) and
    the warnings worth showing: a dive without a date, a dive with several
    computers, a cylinder whose size could not be read. ``name`` prefixes
    the warnings. `SsrfReadError` when the root is not a divelog."""
    _strip_ns(root)
    if root.tag != "divelog":
        raise SsrfReadError(f"{name or 'file'}: not a Subsurface divelog (root element <{root.tag}>)")
    sites, computers = _sites(root), _computers(root)
    dives_root = root.find("dives")
    warnings: List[str] = []
    dives: List[UnifiedDive] = []
    label = name or "divelog"
    for ordinal, el in enumerate((dives_root if dives_root is not None else root).iter("dive"), start=1):
        dive = _read_dive(el, sites, computers, ordinal, warnings, label)
        if dive is not None:
            dives.append(dive)
    return dives, warnings


def read_ssrf(path: PathLike) -> Tuple[List[UnifiedDive], List[str]]:
    """The dives in a ``.ssrf`` / Subsurface ``.xml`` file and the warnings
    (see `parse_ssrf`). Raises `xml.etree.ElementTree.ParseError` for a file
    that is not XML and `SsrfReadError` for XML that is not a divelog."""
    path = os.fspath(path)
    root = ET.parse(path).getroot()
    dives, warnings = parse_ssrf(root, os.path.basename(path))
    logger.debug("Read %d dive(s) from %s", len(dives), path)
    return dives, warnings


def read_ssrf_string(text: str, name: str = "") -> Tuple[List[UnifiedDive], List[str]]:
    """`parse_ssrf` on the XML text itself."""
    return parse_ssrf(ET.fromstring(text), name)
