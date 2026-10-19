"""UDDF 3.2 file adapter (rework.md F2).

UDDF (Universal Dive Data Format) is the one format both Subsurface and
Submersion import and export. This adapter reads and writes a single
``.uddf`` file with the standard library XML parser. Units in UDDF are SI:
metres, seconds, kelvin, pascal, cubic metres and kilograms; ``UnifiedDive``
uses Celsius, bar and litres, so the adapter converts.

Mapping (UDDF element -> UnifiedDive):
    profiledata/repetitiongroup/dive
        @id                                       external_ids["uddf"]
        informationbeforedive/datetime            date_time (local, naive); date_time_utc when
                                                  it carries an offset (read only, R11)
        informationbeforedive/divenumber          dive_number
        informationbeforedive/link@ref -> divesite/site   location, lat, lng
        informationbeforedive/link@ref -> diver/buddy     buddy (joined with ", ")
        informationafterdive/greatestdepth        max_depth
        informationafterdive/averagedepth         avg_depth
        informationafterdive/diveduration         duration
        informationafterdive/lowesttemperature    temp_min
        informationafterdive/notes/para           notes
        informationafterdive/rating/ratingvalue   service_fields["rating"]
        informationafterdive/visibility           visibility (m)
        informationbeforedive/airtemperature (K)  air_temp (R10)
        informationbeforedive/equipmentused/link@ref -> diver/owner/equipment/suit/name   suit (R10)
        tankdata (link@ref -> gasdefinitions/mix) gas_mixtures (a tankdata with no mix, volume,
                                                  pressure or reading is an unused slot, left out);
                                                  in a file with no tankdata at all, one tank per
                                                  gasdefinitions mix (diluents on a rebreather dive,
                                                  a bailout tank per mix switched to on open circuit; B14)
        samples/waypoint                          samples

The Shearwater app's files (generator "Shearwater Cloud Desktop", check.md
B12/B13) are read as the app writes them: PO2s in bar, tankdata without ids
referred to as T1..T4 (the n-th tankdata), a pressure of 0 = unknown, a
transmitter status exported as ~564 bar = no reading. DiveSync writes
pascal only; such a document is made standard before DiveSync writes into it.

Carried fields (plans/convert.md I5; what a dive computer's own file holds
beyond the synced fields, models.py "Carried, not synced"). Written only when
set, so a dive without them gives the same file as before I5:
    informationbeforedive/surfaceintervalbeforedive/passedtime   surface_interval
    informationbeforedive/equipmentused/leadquantity (kg)        weight
    informationbeforedive/equipmentused/link@ref -> diver/owner/equipment/divecomputer
        name, manufacturer/name, model, serialnumber,            computer_vendor/model/serial
        notes/para "Firmware <v>"                                computer_firmware
    informationbeforedive/link@ref -> decomodel/buehlmann        gf_low, gf_high (deco_model in the id)
    samples/waypoint/tankpressure@ref -> tankdata@id  (Pa)       channels.pressures (and .pressure for tank 0);
                                                                 a sample with .pressure and no channels: tank 0's
    samples/waypoint/nodecotime (s), decostop@decodepth/@duration channels.ndl, deco_stop_depth/time
    samples/waypoint/cns (%), calculatedpo2 (Pa), setpo2 (Pa)    channels.cns, ppo2, setpoint
    samples/waypoint/measuredpo2@ref, ppo2@ref (APD) -> o2sensor  channels.o2_cells (the cell's index = the
        (diver/owner/equipment/rebreather/o2sensor) (Pa)         o2sensor's place in the file, R18; an APD
                                                                 Inspiration's first three only, B15)
    samples/waypoint/heartbeat (bpm), gradientfactor (%),        channels.heart_rate, gf99,
        remainingbottomtime (s)                                  gas_time
    samples/waypoint/switchmix@ref -> mix                        events: gas_switch
    samples/waypoint/alarm                                       events: alert (the computer's name)
    samples/waypoint/divemode@type                               dive_mode (first waypoint), events: mode_change
    samples/waypoint/setpo2 at an event                          events: setpoint_change
    samples/waypoint/setmarker                                   events: bookmark
An event lands on the waypoint of its second (else the nearest earlier one).
    applicationdata/dive_sync/exitposition/latitude, longitude   exit_lat, exit_lng (R8: UDDF has no element
        for a second position, so it goes in the program-specific container the spec provides for exactly
        that, after informationbeforedive as the spec's examples place it; decimal degrees like a site's
        geography; read back from there)
Not written, UDDF having no slot: time to surface, the transmitter
connection events, water type and density, bottom time, the start and end
CNS, a 'gauge' dive mode, tank names, and a sample's pressure when the dive
has no tank to refer it to.

UDDF has no slot for another service's id. dive_sync writes its own ids in
``<dive id="dive_sync-<garmin id>">`` when it creates a dive and otherwise
uses whatever id the writing application put there; the engine's local
link table keeps pairs across runs.
"""
from __future__ import annotations

import bisect
import copy
import logging
import os
import re
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

from src.core.adapter import BaseDiveAdapter
from src.core.fields import FieldSpec
from src.core.models import (
    EVENT_ALERT,
    EVENT_BOOKMARK,
    EVENT_GAS_SWITCH,
    EVENT_MODE_CHANGE,
    EVENT_SETPOINT_CHANGE,
    DiveEvent,
    GasMixture,
    SampleChannels,
    UnifiedDive,
    UnifiedSample,
    recorded_water_temp,
)

logger = logging.getLogger("dive_sync.uddf")

SERVICE_ID = "uddf"
NS = "http://www.streit.cc/uddf/3.2/"
GENERATOR = "dive_sync"
FIRMWARE_NOTE = "Firmware "        # the divecomputer notes/para that holds the firmware (UDDF has no element for it)
REBREATHER_ID = "rebreather-dive_sync"   # the owner's rebreather that holds the o2sensors a measuredpo2 refers to (R18)
APPLICATION_TAG = GENERATOR               # dive_sync's own element inside a dive's applicationdata (R8)
LB_TO_KG = 0.45359237

# UnifiedDive.dive_mode (models.KNOWN_DIVE_MODES) <-> UDDF waypoint divemode@type.
# UDDF has no gauge mode. Reading 'opencircuit' back gives oc_single_gas when the
# dive breathed one mix and oc_multi_gas otherwise.
UDDF_DIVE_MODES: Dict[str, str] = {
    "oc_single_gas": "opencircuit", "oc_multi_gas": "opencircuit",
    "ccr": "closedcircuit", "scr": "semiclosedcircuit", "apnea": "apnoe",
}
REBREATHER_MODES: Tuple[str, ...] = ("closedcircuit", "semiclosedcircuit")
_DIVE_MODES_BACK: Dict[str, str] = {"closedcircuit": "ccr", "semiclosedcircuit": "scr", "apnoe": "apnea"}
# The events that have a waypoint element; the others (the transmitter
# connections of a Garmin) are left out.
WRITTEN_EVENT_TYPES: Tuple[str, ...] = (EVENT_GAS_SWITCH, EVENT_ALERT, EVENT_MODE_CHANGE, EVENT_SETPOINT_CHANGE, EVENT_BOOKMARK)


def _slug(text: object) -> str:
    """``text`` as the body of an XML id (letters, digits, '-', '_', '.')."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text)).strip("_") or "x"


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _strip_ns(tree: ET.Element) -> None:
    for el in tree.iter():
        if isinstance(el.tag, str) and el.tag.startswith("{"):
            el.tag = el.tag.split("}", 1)[1]


def _text(el: Optional[ET.Element], path: str) -> Optional[str]:
    if el is None:
        return None
    found = el.find(path)
    if found is None or found.text is None:
        return None
    return found.text.strip()


def _float(el: Optional[ET.Element], path: str) -> Optional[float]:
    t = _text(el, path)
    try:
        return float(t) if t not in (None, "") else None
    except ValueError:
        return None


def kelvin_to_c(k: Optional[float]) -> Optional[float]:
    return None if k is None else round(k - 273.15, 2)


def c_to_kelvin(c: Optional[float]) -> Optional[float]:
    return None if c is None else round(c + 273.15, 2)


def pa_to_bar(pa: Optional[float]) -> Optional[float]:
    return None if pa is None else round(pa / 100000.0, 2)


def bar_to_pa(bar: Optional[float]) -> Optional[float]:
    return None if bar is None else round(bar * 100000.0)


# The Shearwater app (its desktop program, "Shearwater Cloud Desktop") writes
# every PO2 of a waypoint in bar where UDDF says pascal (calculatedpo2
# 0.85 on a 0.85 bar sample), a tank pressure it does not know as 0 Pa, and
# leaves its tankdata without ids, its tankpressure refs being "T1".."T4"
# (check.md B12/B13). The owner's ruling (2026-10-20): keep the standard -
# pascal on read and write everywhere - and read the app's own files as
# what they are, recognised by the generator name, never by the size of a
# value. DiveSync never writes bar: a document read from such a file is
# converted to pascal before DiveSync writes into it (UddfDocument).
SHEARWATER_APP_GENERATOR = "shearwater cloud"     # a generator name starting so (case-insensitive) is the app
PO2_ELEMENTS: Tuple[str, ...] = ("calculatedpo2", "setpo2", "measuredpo2", "ppo2")
# The app exports a transmitter's status word (no reading, no signal) as a
# pressure masked to 12 bits of 2 psi - 8186-8190 psi, about 564 bar - so
# on its files a tankpressure at or above this is no reading.
SHEARWATER_STATUS_BAR = 0xFF0 * 2 / 14.5037738          # ~562.6 bar
APD_CELLS = 3          # an APD Inspiration's cells; its o2sensors 4-6 are the second controller's view of them
APD_COMPUTER_ID = "INSPIRATION"
APD_GENERATOR = "AP DiveSight"


def _is_apd_inspiration(root: ET.Element) -> bool:
    """A file from APD's DiveSight / an Inspiration rebreather."""
    return (_text(root, "generator/name") or "") == APD_GENERATOR or any(
        dc.get("id") == APD_COMPUTER_ID for dc in root.iter("divecomputer"))


_POSITIONAL_TANK_REF = re.compile(r"^T(\d+)$")


def is_shearwater_app_file(root: ET.Element) -> bool:
    """Whether the (namespace-stripped) document was written by the
    Shearwater app, by its ``generator/name``."""
    return (_text(root, "generator/name") or "").lower().startswith(SHEARWATER_APP_GENERATOR)


def _po2_bar(value: Optional[float], in_bar: bool) -> Optional[float]:
    """A waypoint PO2 in bar: pascal as UDDF has it, or bar as read from a
    file whose writer is known to write bar (``in_bar``)."""
    if value is None:
        return None
    return round(value, 2) if in_bar else pa_to_bar(value)


def parse_uddf_datetime(text: str) -> Optional[datetime]:
    text = text.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


_UDDF_OFFSET = re.compile(r"[+-]\d{2}:?\d{2}$")


def parse_uddf_utc(text: str) -> Optional[datetime]:
    """The start as a naive UTC instant when the UDDF ``datetime`` carries a
    non-zero UTC offset (``2025-06-06T09:05:27+08:00``), else None. UDDF
    allows an ISO 8601 offset, though most writers (DiveSync's included)
    leave it out. A ``Z`` or a zero offset is not taken as a zone: the
    Shearwater app writes the computer's local clock with a ``Z``
    (``tests/test_uddf.py`` SHEARWATER_STYLE: the app's DiveDate 15:21:08
    is exported as ``15:21:08Z``), so such a time says nothing about UTC.
    The local time stays what ``parse_uddf_datetime`` reads, the clock as
    written; this only lets matching compare instants when the other side
    knows its own (plans/gap-roadmap.md R11). Read only: the writer still
    writes the local time without an offset."""
    text = (text or "").strip()
    if not _UDDF_OFFSET.search(text):
        return None
    if text[-5] in "+-" and text[-4:].isdigit():
        text = text[:-2] + ":" + text[-2:]          # +0800 -> +08:00 (Python 3.10's fromisoformat)
    try:
        aware = datetime.fromisoformat(text)
    except ValueError:
        return None
    offset = aware.utcoffset()
    if not offset:                                  # naive, Z or +00:00: no zone known
        return None
    return (aware - offset).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def read_uddf(path: str) -> Tuple[ET.Element, List[UnifiedDive]]:
    """Parse a UDDF file. Returns the (namespace-stripped) root and the dives."""
    tree = ET.parse(path)
    root = tree.getroot()
    _strip_ns(root)
    sites: Dict[str, Tuple[str, Optional[float], Optional[float]]] = {}
    for site in root.findall("divesite/site"):
        lat = _float(site, "geography/latitude")
        lng = _float(site, "geography/longitude")
        sites[site.get("id", "")] = (_text(site, "name") or "", lat, lng)
    buddies: Dict[str, str] = {}
    for buddy in root.findall("diver/buddy"):
        name = " ".join(x for x in (_text(buddy, "personal/firstname"), _text(buddy, "personal/lastname")) if x)
        buddies[buddy.get("id", "")] = name or (_text(buddy, "personal/nickname") or "")
    mixes: Dict[str, Tuple[float, float]] = {}
    for mix in root.findall("gasdefinitions/mix"):
        o2 = _float(mix, "o2")
        he = _float(mix, "he")
        mixes[mix.get("id", "")] = ((o2 or 0.21) * 100.0, (he or 0.0) * 100.0)
    computers = {dc.get("id", ""): dc for dc in root.iter("divecomputer") if dc.get("id")}
    suits = {el.get("id", ""): (_text(el, "name") or "") for el in root.iter("suit") if el.get("id")}
    # a rebreather's cells: the o2sensor elements in file order, wherever the rebreather is (the owner's or a buddy's)
    o2sensors = {el.get("id", ""): i for i, el in enumerate(el for el in root.iter("o2sensor") if el.get("id"))}
    decomodels = {m.get("id", ""): m for m in root.findall("decomodel/buehlmann") if m.get("id")}
    shearwater = is_shearwater_app_file(root)
    # A file with no tankdata at all: the gasdefinitions mixes are the gas list (B14); not DiveSync's
    # own files, where a dive without tanks has none and the mixes belong to the other dives' tanks
    gases_from_mixes = bool(mixes) and root.find(".//tankdata") is None and _text(root, "generator/name") != GENERATOR
    # APD's Inspiration logs both controllers' readings of its three cells; the first controller's are
    # the cells, as Subsurface keeps them (B15)
    skip_cells = [sensor_id for sensor_id, index in o2sensors.items() if index >= APD_CELLS] if _is_apd_inspiration(root) else []

    dives: List[UnifiedDive] = []
    for group in root.findall("profiledata/repetitiongroup"):
        for dive in group.findall("dive"):
            before = dive.find("informationbeforedive")
            after = dive.find("informationafterdive")
            when_text = _text(before, "datetime")
            when = parse_uddf_datetime(when_text) if when_text else None
            if when is None:
                logger.warning("UDDF dive %s has no usable datetime; skipped", dive.get("id"))
                continue
            location = lat = lng = None
            buddy_names: List[str] = []
            decomodel: Optional[ET.Element] = None
            if before is not None:
                for link in before.findall("link"):
                    ref = link.get("ref", "")
                    if ref in sites:
                        location, lat, lng = sites[ref]
                    elif ref in buddies:
                        buddy_names.append(buddies[ref])
                    elif ref in decomodels:
                        decomodel = decomodels[ref]
            tanks, tank_refs, tank_mix = _read_tanks(dive, mixes, shearwater)
            switch_tanks: Optional[Dict[Tuple[str, bool], int]] = None
            if gases_from_mixes:
                tanks, tank_mix, switch_tanks = _tanks_from_mixes(dive, mixes)
            samples, events, dive_mode = _read_waypoints(dive, tank_refs, tank_mix, mixes, o2sensors, shearwater_app=shearwater,
                                                         switch_tanks=switch_tanks, skip_cells=skip_cells)
            computer = _read_computer(dive, computers)
            suit = next((suits[link.get("ref", "")] for used in dive.iter("equipmentused") for link in used.findall("link")
                         if link.get("ref", "") in suits), None) or None
            weight = next((w for w in (_float(used, "leadquantity") for used in dive.iter("equipmentused")) if w is not None), None)
            duration = _float(after, "diveduration")
            if duration is None and samples and samples[-1].time is not None:
                duration = samples[-1].time
            max_depth = _float(after, "greatestdepth")
            if max_depth is None and samples:
                max_depth = max(s.depth for s in samples)
            notes = "\n".join(p.text.strip() for p in (after.findall("notes/para") if after is not None else []) if p.text)
            number_text = _text(before, "divenumber")
            visibility = _float(after, "visibility")
            rating = _float(after, "rating/ratingvalue")
            surface_interval = _float(before, "surfaceintervalbeforedive/passedtime")
            exit_lat, exit_lng = _read_exit_position(dive)
            gf_low = _float(decomodel, "gradientfactorlow")
            gf_high = _float(decomodel, "gradientfactorhigh")
            dives.append(UnifiedDive(
                date_time=when,
                date_time_utc=parse_uddf_utc(when_text),
                duration=int(round(duration or 0)),
                max_depth=max_depth or 0.0,
                avg_depth=_float(after, "averagedepth"),
                temp_min=recorded_water_temp(kelvin_to_c(_float(after, "lowesttemperature"))),
                external_ids={SERVICE_ID: dive.get("id") or f"uddf-{when.strftime('%Y%m%dT%H%M%S')}"},
                gas_mixtures=tanks,
                location=location or None,
                notes=notes or None,
                dive_number=int(number_text) if number_text and number_text.isdigit() else None,
                weight=weight,
                weight_unit="kilogram" if weight is not None else None,
                visibility=visibility,
                visibility_unit="meter" if visibility is not None else None,
                buddy=", ".join(b for b in buddy_names if b) or None,
                lat=lat,
                lng=lng,
                samples=samples,
                service_fields={"rating": int(rating) if rating is not None else None},
                events=events,
                computer_vendor=computer.get("vendor"),
                computer_model=computer.get("model"),
                computer_serial=computer.get("serial"),
                computer_firmware=computer.get("firmware"),
                gf_low=int(round(gf_low)) if gf_low is not None else None,
                gf_high=int(round(gf_high)) if gf_high is not None else None,
                deco_model=_deco_model_of(decomodel.get("id", "")) if decomodel is not None else None,
                dive_mode=dive_mode,
                surface_interval=int(round(surface_interval)) if surface_interval is not None else None,
                air_temp=kelvin_to_c(_float(before, "airtemperature")),
                suit=suit,
                exit_lat=exit_lat,
                exit_lng=exit_lng,
            ))
    return root, dives


def _is_reading(bar: Optional[float], shearwater: bool) -> bool:
    """Whether a tankpressure value is a reading (not missing, not the
    Shearwater app's transmitter status)."""
    return bar is not None and not (shearwater and bar >= SHEARWATER_STATUS_BAR)


def _read_tanks(dive: ET.Element, mixes: Dict[str, Tuple[float, float]],
                shearwater: bool = False) -> Tuple[List[GasMixture], Dict[str, int], List[str]]:
    """The dive's ``tankdata`` as gases, the tankpressure ref -> tank index
    map and each tank's mix id. A ref is the tankdata's id; a ``T<n>`` that
    names no tankdata is the n-th tankdata (the Shearwater app writes its
    tankdata without ids and refers to them as T1..T4, B13), and on a dive
    with one tankdata any ref is that tank's. A tankdata with no mix link,
    no volume, no pressure and no sample reading is an unused slot (the app
    writes six, two of them in use) and left out. On a Shearwater app file
    a pressure of 0 is unknown, not 0 bar. A tank without an id is named
    after the T<n> ref that points at it."""
    raw = dive.findall("tankdata")
    by_ref: Dict[str, int] = {tank.get("id"): i for i, tank in enumerate(raw) if tank.get("id")}
    positional: Dict[int, str] = {}
    readings: set = set()
    refs = {tp.get("ref", "") for tp in dive.iter("tankpressure")
            if _is_reading(pa_to_bar(_float(tp, ".")), shearwater)}
    for ref in sorted(refs):
        if ref in by_ref:
            readings.add(by_ref[ref])
            continue
        hit = _POSITIONAL_TANK_REF.match(ref)
        if hit and 1 <= int(hit.group(1)) <= len(raw):
            index = int(hit.group(1)) - 1
            by_ref[ref] = index
            positional.setdefault(index, ref)
            readings.add(index)
        elif len(raw) == 1:
            readings.add(0)

    def pressure(tank: ET.Element, tag: str) -> Optional[float]:
        value = pa_to_bar(_float(tank, tag))
        return None if shearwater and value == 0 else value

    tanks: List[GasMixture] = []
    tank_mix: List[str] = []
    new_index: Dict[int, int] = {}
    for i, tank in enumerate(raw):
        link = tank.find("link")
        mix_id = link.get("ref", "") if link is not None else ""
        vol = _float(tank, "tankvolume")
        start, end = pressure(tank, "tankpressurebegin"), pressure(tank, "tankpressureend")
        if link is None and vol is None and not start and not end and i not in readings:
            continue
        o2, he = mixes.get(mix_id, (21.0, 0.0))
        new_index[i] = len(tanks)
        tanks.append(GasMixture(
            oxygen=round(o2, 1), helium=round(he, 1),
            start_pressure=start, end_pressure=end,
            tank_volume=None if vol is None else round(vol * 1000.0, 3),
            tank_name=tank.get("id") or positional.get(i) or None,
        ))
        tank_mix.append(mix_id)
    tank_refs = {ref: new_index[i] for ref, i in by_ref.items() if i in new_index}
    if len(tanks) == 1:
        tank_refs.update({ref: 0 for ref in refs if ref not in tank_refs})
    return tanks, tank_refs, tank_mix


def _tanks_from_mixes(dive: ET.Element, mixes: Dict[str, Tuple[float, float]]
                      ) -> Tuple[List[GasMixture], List[str], Dict[Tuple[str, bool], int]]:
    """The gas list of a dive in a file without any ``tankdata`` (check.md
    B14): one tank per ``gasdefinitions`` mix, in the file's order, as
    Subsurface imports such a file. On a rebreather dive (a waypoint's
    divemode closed or semi-closed circuit) every mix is a diluent, and a
    mix the diver switches to while on open circuit gets a tank of its own
    with the bailout role, after the diluents. Returns the tanks, each
    tank's mix id and the gas-switch map (mix id, on open circuit) -> tank."""
    waypoints = dive.findall("samples/waypoint")
    rebreather = any((wp.find("divemode").get("type") or (wp.find("divemode").text or "").strip()) in REBREATHER_MODES
                     for wp in waypoints if wp.find("divemode") is not None)
    tanks: List[GasMixture] = []
    tank_mix: List[str] = []
    switches: Dict[Tuple[str, bool], int] = {}
    for mix_id, (o2, he) in mixes.items():
        switches[(mix_id, False)] = switches[(mix_id, True)] = len(tanks)
        tanks.append(GasMixture(oxygen=round(o2, 1), helium=round(he, 1), tank_role="diluent" if rebreather else None))
        tank_mix.append(mix_id)
    if rebreather:
        mode: Optional[str] = None
        bailouts: Dict[str, int] = {}
        for wp in waypoints:
            mode_el = wp.find("divemode")
            if mode_el is not None:
                mode = (mode_el.get("type") or (mode_el.text or "").strip()) or mode
            if mode in REBREATHER_MODES:
                continue
            for sw in wp.findall("switchmix"):
                ref = sw.get("ref", "")
                if ref not in mixes or ref in bailouts:
                    continue
                o2, he = mixes[ref]
                bailouts[ref] = switches[(ref, True)] = len(tanks)
                tanks.append(GasMixture(oxygen=round(o2, 1), helium=round(he, 1), tank_role="bailout"))
                tank_mix.append(ref)
    return tanks, tank_mix, switches


def _read_exit_position(dive: ET.Element) -> Tuple[Optional[float], Optional[float]]:
    """The exit position written under the dive's ``applicationdata`` (R8);
    (None, None) unless both coordinates are there."""
    position = dive.find(f"applicationdata/{APPLICATION_TAG}/exitposition")
    lat, lng = _float(position, "latitude"), _float(position, "longitude")
    return (lat, lng) if lat is not None and lng is not None else (None, None)


def _deco_model_of(decomodel_id: str) -> Optional[str]:
    """The deco model named by a ``buehlmann`` id written by this module
    (``buehlmann-ZHL-16C-gf40-85``) or by Shearwater (``zhl16c``)."""
    hit = re.search(r"zhl[-_]?16([a-c])", decomodel_id, re.IGNORECASE)
    return f"ZHL-16{hit.group(1).upper()}" if hit else None


def _read_computer(dive: ET.Element, computers: Dict[str, ET.Element]) -> Dict[str, Optional[str]]:
    """The dive computer the dive's ``equipmentused`` links to (looked for
    under informationbeforedive, where the spec puts it, and anywhere else
    in the dive: ATMOS writes it after the dive)."""
    for used in dive.iter("equipmentused"):
        for link in used.findall("link"):
            dc = computers.get(link.get("ref", ""))
            if dc is None:
                continue
            firmware = next((p.text.strip()[len(FIRMWARE_NOTE):] for p in dc.findall("notes/para")
                             if p.text and p.text.strip().startswith(FIRMWARE_NOTE)), None)
            return {"vendor": _text(dc, "manufacturer/name") or None, "model": _text(dc, "model") or _text(dc, "name") or None,
                    "serial": _text(dc, "serialnumber") or None, "firmware": firmware or None}
    return {}


def _read_waypoints(dive: ET.Element, tank_refs: Dict[str, int], tank_mix: Sequence[str],
                    mixes: Dict[str, Tuple[float, float]],
                    o2sensors: Optional[Dict[str, int]] = None,
                    shearwater_app: bool = False,
                    switch_tanks: Optional[Dict[Tuple[str, bool], int]] = None,
                    skip_cells: Sequence[str] = ()) -> Tuple[List[UnifiedSample], List[DiveEvent], Optional[str]]:
    """The samples (with channels when a waypoint holds more than depth,
    time and temperature), the events the waypoints carry, and the
    ``divemode`` of the first waypoint that names one. A ``measuredpo2``
    is the cell its ``ref`` names (``o2sensors``: o2sensor id -> index in
    the file); a ref no o2sensor declares gets the next free index in the
    order the dive first uses it, so a file that only lists the sensors by
    their refs still gives one cell each. ``tank_refs`` maps a
    tankpressure ref to its tank (`_read_tanks`). On a Shearwater app
    file (``shearwater_app``, B12/B13) the PO2 elements are bar and a tank
    pressure in the transmitter-status range (`SHEARWATER_STATUS_BAR`) is
    no reading. APD's ``ppo2`` with an o2sensor ref is a cell like
    ``measuredpo2`` (B15); a ref in ``skip_cells`` is left out.
    ``switch_tanks`` ((mix id, on open circuit) -> tank) names the tank of
    a gas switch when the tanks were built from the mixes (B14)."""
    po2_in_bar = shearwater_app
    samples: List[UnifiedSample] = []
    events: List[DiveEvent] = []
    cell_index: Dict[str, int] = dict(o2sensors or {})
    mix_tank = {}
    for i, mix_id in enumerate(tank_mix):
        mix_tank.setdefault(mix_id, i)
    # UDDF's 'opencircuit' is oc_single_gas when the dive had one mix, else oc_multi_gas.
    open_circuit = "oc_single_gas" if len({mixes.get(m, (21.0, 0.0)) for m in tank_mix}) <= 1 else "oc_multi_gas"
    modes_back = dict(_DIVE_MODES_BACK, opencircuit=open_circuit)
    first_mode: Optional[str] = None
    last_mode: Optional[str] = None
    uddf_mode: Optional[str] = None          # the mode in force, as the file spells it
    for wp in dive.findall("samples/waypoint"):
        depth = _float(wp, "depth")
        if depth is None:
            continue
        mode_el = wp.find("divemode")
        mode = (mode_el.get("type") or (mode_el.text or "").strip()) if mode_el is not None else None
        uddf_mode = mode or uddf_mode
        t = _float(wp, "divetime")
        time = None if t is None else int(round(t))
        channels = SampleChannels(
            ndl=_int_text(wp, "nodecotime"),
            deco_stop_depth=_attr_float(wp.find("decostop"), "decodepth"),
            deco_stop_time=_attr_int(wp.find("decostop"), "duration"),
            cns=_float(wp, "cns"),
            ppo2=_po2_bar(_float(wp, "calculatedpo2"), po2_in_bar),
            setpoint=_po2_bar(_float(wp, "setpo2"), po2_in_bar),
            gf99=_float(wp, "gradientfactor"),
            gas_time=_int_text(wp, "remainingbottomtime"),
            heart_rate=_int_text(wp, "heartbeat"),
        )
        for tp in wp.findall("tankpressure"):
            idx = tank_refs.get(tp.get("ref", ""), 0 if len(tank_mix) <= 1 else None)
            bar = pa_to_bar(_float(tp, "."))
            if idx is not None and _is_reading(bar, shearwater_app):
                channels.pressures[idx] = bar
        for mp in wp.findall("measuredpo2") + [el for el in wp.findall("ppo2") if el.get("ref")]:
            bar = _po2_bar(_float(mp, "."), po2_in_bar)
            ref = mp.get("ref", "")
            if bar is None or ref in skip_cells:
                continue
            if ref not in cell_index:
                cell_index[ref] = max(cell_index.values(), default=-1) + 1
            channels.o2_cells[cell_index[ref]] = bar
        pressure = channels.pressures.get(0)
        previous = samples[-1].time if samples and samples[-1].time is not None else 0
        # A waypoint whose only extra is the first tank's pressure gives the
        # sample the source adapters (Shearwater, Subsurface) produce:
        # `pressure` set, no channels - so a re-sync sees the same dive.
        held = channels.model_dump()
        if held == {"pressures": {0: pressure}}:
            held = {}
        samples.append(UnifiedSample(depth=depth, temp=kelvin_to_c(_float(wp, "temperature")), time=time,
                                     pressure=pressure, channels=channels if held else None))
        when = time if time is not None else previous
        for sw in wp.findall("switchmix"):
            ref = sw.get("ref", "")
            o2, he = mixes.get(ref, (None, None))
            tank = (switch_tanks.get((ref, uddf_mode not in REBREATHER_MODES)) if switch_tanks is not None
                    else mix_tank.get(ref))
            events.append(DiveEvent(time=when, type=EVENT_GAS_SWITCH, tank=tank,
                                    oxygen=round(o2, 1) if o2 is not None else None,
                                    helium=round(he, 1) if he is not None else None))
        for alarm in wp.findall("alarm"):
            events.append(DiveEvent(time=when, type=EVENT_ALERT, name=(alarm.text or "").strip() or None))
        for marker in wp.findall("setmarker"):
            events.append(DiveEvent(time=when, type=EVENT_BOOKMARK, name=(marker.text or "").strip() or None))
        if mode:
            mode = modes_back.get(mode, mode)
            if first_mode is None:
                first_mode = mode
            elif mode != last_mode:
                events.append(DiveEvent(time=when, type=EVENT_MODE_CHANGE, name=mode))
            last_mode = mode
    return samples, events, first_mode


def _int_text(el: ET.Element, path: str) -> Optional[int]:
    value = _float(el, path)
    return None if value is None else int(round(value))


def _attr_float(el: Optional[ET.Element], name: str) -> Optional[float]:
    if el is None or el.get(name) in (None, ""):
        return None
    try:
        return float(el.get(name))
    except ValueError:
        return None


def _attr_int(el: Optional[ET.Element], name: str) -> Optional[int]:
    value = _attr_float(el, name)
    return None if value is None else int(round(value))


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _sub(parent: ET.Element, tag: str, text: Optional[object] = None, **attrs: str) -> ET.Element:
    el = ET.SubElement(parent, tag, attrs)
    if text is not None:
        el.text = str(text)
    return el


def _fmt(value: float) -> str:
    return f"{value:.6g}" if isinstance(value, float) else str(value)


def weight_kg(weight: float, unit: Optional[str]) -> float:
    """A UnifiedDive weight in kilograms (UDDF's unit); pounds converted,
    no unit taken as kilograms."""
    return weight * LB_TO_KG if (unit or "").lower().startswith(("p", "lb")) else weight


def events_by_sample(samples: Sequence[UnifiedSample], events: Sequence[DiveEvent]) -> Dict[int, List[DiveEvent]]:
    """The events that have a waypoint element, grouped by the index of the
    sample they land on: the sample of the event's second, else the nearest
    earlier one (the first when the event came before every sample)."""
    out: Dict[int, List[DiveEvent]] = {}
    if not samples:
        return out
    times: List[int] = []
    for s in samples:   # a sample without a time counts at the previous one's
        times.append(s.time if s.time is not None else (times[-1] if times else 0))
    for event in events:
        if event.type not in WRITTEN_EVENT_TYPES:
            continue
        index = max(0, bisect.bisect_right(times, event.time) - 1)
        out.setdefault(index, []).append(event)
    return out


def _to_standard_units(root: ET.Element) -> None:
    """Turn a Shearwater app document (B12) into a standard one in place,
    before DiveSync writes a dive into it: every PO2 element from bar to
    pascal, every tank pressure of 0 (the app's "unknown") and every
    transmitter status exported as a pressure (`SHEARWATER_STATUS_BAR`) removed, and the
    generator renamed to DiveSync's - the file is DiveSync's from the next
    save on, so it must read back as pascal like every other file. The
    app's T<n> refs and unused tankdata need nothing: the reader handles
    them in any file."""
    for tag in PO2_ELEMENTS:
        for el in root.iter(tag):
            try:
                el.text = _fmt(bar_to_pa(float((el.text or "").strip())))
            except ValueError:
                continue
    for tank in root.iter("tankdata"):
        for tag in ("tankpressurebegin", "tankpressureend"):
            el = tank.find(tag)
            if el is not None and _float(tank, tag) == 0:
                tank.remove(el)
    for wp in root.iter("waypoint"):
        for tp in wp.findall("tankpressure"):
            if not _is_reading(pa_to_bar(_float(tp, ".")), True):
                wp.remove(tp)
    name = root.find("generator/name")
    if name is not None:
        name.text = GENERATOR


class UddfDocument:
    """In-memory UDDF document that keeps ids stable across writes."""

    def __init__(self, root: Optional[ET.Element] = None):
        if root is None:
            root = ET.Element("uddf", {"version": "3.2.0"})
            gen = _sub(root, "generator")
            _sub(gen, "name", GENERATOR)
            _sub(gen, "type", "logbook")
            _sub(root, "diver")
            _sub(root, "divesite")
            _sub(root, "gasdefinitions")
            _sub(_sub(root, "profiledata"), "repetitiongroup", id="rg-dive_sync")
        self.root = root
        if is_shearwater_app_file(root):
            _to_standard_units(root)
        for tag in ("diver", "divesite", "gasdefinitions", "profiledata"):
            if root.find(tag) is None:
                _sub(root, tag)
        if root.find("profiledata/repetitiongroup") is None:
            _sub(root.find("profiledata"), "repetitiongroup", id="rg-dive_sync")

    # -- lookups ----------------------------------------------------------

    def _mix_id(self, o2: float, he: float) -> str:
        gases = self.root.find("gasdefinitions")
        for mix in gases.findall("mix"):
            if abs((_float(mix, "o2") or 0) * 100 - o2) < 0.05 and abs((_float(mix, "he") or 0) * 100 - he) < 0.05:
                return mix.get("id", "")
        mix_id = f"mix-{int(round(o2))}-{int(round(he))}"
        mix = _sub(gases, "mix", id=mix_id)
        _sub(mix, "name", f"{'Air' if abs(o2 - 21) < 0.5 and he == 0 else f'EAN{int(round(o2))}' if he == 0 else f'TX{int(round(o2))}/{int(round(he))}'}")
        _sub(mix, "o2", _fmt(round(o2 / 100.0, 4)))
        _sub(mix, "he", _fmt(round(he / 100.0, 4)))
        return mix_id

    def _site_id(self, name: Optional[str], lat: Optional[float], lng: Optional[float]) -> Optional[str]:
        if not name and lat is None:
            return None
        from src.core.site_matcher import find_site
        sites = self.root.find("divesite")
        existing = list(sites.findall("site"))
        hit = find_site(existing, name, lat, lng,
                        get_name=lambda s: _text(s, "name"),
                        get_gps=lambda s: (_float(s, "geography/latitude"), _float(s, "geography/longitude")))
        if hit is not None:
            return hit.get("id")
        site_id = f"site-{len(existing) + 1}"
        while sites.find(f"site[@id='{site_id}']") is not None:
            site_id += "x"
        site = _sub(sites, "site", id=site_id)
        _sub(site, "name", name or f"{lat:.6f}, {lng:.6f}")
        if lat is not None and lng is not None:
            geo = _sub(site, "geography")
            _sub(geo, "latitude", _fmt(float(lat)))
            _sub(geo, "longitude", _fmt(float(lng)))
        return site_id

    def _buddy_ids(self, buddy: Optional[str]) -> List[str]:
        if not buddy:
            return []
        diver = self.root.find("diver")
        ids = []
        for name in [b.strip() for b in buddy.split(",") if b.strip()]:
            hit = None
            for el in diver.findall("buddy"):
                full = " ".join(x for x in (_text(el, "personal/firstname"), _text(el, "personal/lastname")) if x)
                if full.lower() == name.lower():
                    hit = el
                    break
            if hit is None:
                hit = _sub(diver, "buddy", id=f"buddy-{len(diver.findall('buddy')) + 1}")
                personal = _sub(hit, "personal")
                first, _, last = name.partition(" ")
                _sub(personal, "firstname", first)
                if last:
                    _sub(personal, "lastname", last)
            ids.append(hit.get("id"))
        return ids

    def _computer_id(self, unified: UnifiedDive) -> Optional[str]:
        """The ``diver/owner/equipment/divecomputer`` for the dive's computer
        (created on first use, matched by serial, else by vendor and model);
        None when the dive names no computer."""
        if not (unified.computer_model or unified.computer_serial):
            return None
        equipment = self._owner_equipment()
        for dc in equipment.findall("divecomputer"):
            if unified.computer_serial and _text(dc, "serialnumber") == unified.computer_serial:
                return dc.get("id")
            if not unified.computer_serial and not _text(dc, "serialnumber") and _text(dc, "model") == unified.computer_model \
                    and (_text(dc, "manufacturer/name") or None) == unified.computer_vendor:
                return dc.get("id")
        dc_id = f"dc-{_slug(unified.computer_vendor or 'computer')}-{_slug(unified.computer_serial or unified.computer_model)}"
        while self.root.find(f".//divecomputer[@id='{dc_id}']") is not None:
            dc_id += "x"
        dc = _sub(equipment, "divecomputer", id=dc_id)
        _sub(dc, "name", " ".join(x for x in (unified.computer_vendor, unified.computer_model) if x) or unified.computer_serial)
        if unified.computer_vendor:
            _sub(_sub(dc, "manufacturer", id=f"man-{_slug(unified.computer_vendor)}"), "name", unified.computer_vendor)
        if unified.computer_model:
            _sub(dc, "model", unified.computer_model)
        if unified.computer_serial:
            _sub(dc, "serialnumber", unified.computer_serial)
        if unified.computer_firmware:
            _sub(_sub(dc, "notes"), "para", f"{FIRMWARE_NOTE}{unified.computer_firmware}")
        return dc_id

    def _suit_id(self, unified: UnifiedDive) -> Optional[str]:
        """The ``diver/owner/equipment/suit`` for the dive's suit (R10),
        created on first use and matched by its name; None for no suit."""
        name = (unified.suit or "").strip()
        if not name:
            return None
        equipment = self._owner_equipment()
        for suit in equipment.findall("suit"):
            if (_text(suit, "name") or "").strip() == name:
                return suit.get("id")
        suit_id = f"suit-{_slug(name)}"
        while self.root.find(f".//suit[@id='{suit_id}']") is not None:
            suit_id += "x"
        _sub(_sub(equipment, "suit", id=suit_id), "name", name)
        return suit_id

    def _o2sensor_ids(self, unified: UnifiedDive) -> Tuple[Optional[str], List[str]]:
        """The ``diver/owner/equipment/rebreather`` id and its ``o2sensor``
        ids for the dive's cells, one per cell up to the highest index any
        sample has (created on first use under one rebreather,
        ``o2sensor-<n>`` 1-based like Subsurface's sensor1); (None, [])
        when no sample has a cell."""
        top = max((i for s in unified.samples if s.channels is not None for i in s.channels.o2_cells), default=-1)
        if top < 0:
            return None, []
        equipment = self._owner_equipment()
        rebreather = next((r for r in equipment.findall("rebreather") if r.findall("o2sensor")), None)
        if rebreather is None:
            rebreather = _sub(equipment, "rebreather", id=REBREATHER_ID)
            _sub(rebreather, "name", "Rebreather")
        sensors = rebreather.findall("o2sensor")
        while len(sensors) <= top:
            sensor_id = f"o2sensor-{len(sensors) + 1}"
            while self.root.find(f".//o2sensor[@id='{sensor_id}']") is not None:
                sensor_id += "x"
            sensor = _sub(rebreather, "o2sensor", id=sensor_id)
            _sub(sensor, "name", f"Cell {len(sensors) + 1}")
            sensors.append(sensor)
        return rebreather.get("id"), [sensor.get("id", "") for sensor in sensors[:top + 1]]

    def _owner_equipment(self) -> ET.Element:
        """``diver/owner/equipment``, created when missing (the owner before
        the buddies, as the spec orders them)."""
        diver = self.root.find("diver")
        owner = diver.find("owner")
        if owner is None:
            owner = ET.Element("owner", {"id": "owner"})
            diver.insert(0, owner)                       # the spec puts the owner before the buddies
        equipment = owner.find("equipment")
        if equipment is None:
            equipment = _sub(owner, "equipment")
        return equipment

    def _decomodel_id(self, unified: UnifiedDive) -> Optional[str]:
        """The root ``decomodel/buehlmann`` holding the dive's gradient
        factors (the model's name in its id), created before ``profiledata``
        on first use; None when the dive has no gradient factors."""
        if unified.gf_low is None and unified.gf_high is None:
            return None
        root = self.root
        decomodel = root.find("decomodel")
        if decomodel is None:
            decomodel = ET.Element("decomodel")
            root.insert(list(root).index(root.find("profiledata")), decomodel)
        model_id = f"buehlmann-{_slug(unified.deco_model) + '-' if unified.deco_model else ''}gf{unified.gf_low}-{unified.gf_high}"
        for model in decomodel.findall("buehlmann"):
            if model.get("id") == model_id:
                return model_id
        model = _sub(decomodel, "buehlmann", id=model_id)
        if unified.gf_high is not None:
            _sub(model, "gradientfactorhigh", unified.gf_high)
        if unified.gf_low is not None:
            _sub(model, "gradientfactorlow", unified.gf_low)
        return model_id

    # -- dives ------------------------------------------------------------

    def find_dive(self, dive_id: str) -> Optional[ET.Element]:
        for dive in self.root.findall("profiledata/repetitiongroup/dive"):
            if dive.get("id") == dive_id:
                return dive
        return None

    def write_dive(self, unified: UnifiedDive, dive_id: Optional[str] = None) -> str:
        """Create or replace a ``<dive>``; returns its id."""
        group = self.root.find("profiledata/repetitiongroup")
        existing = self.find_dive(dive_id) if dive_id else None
        if dive_id is None:
            source = next((f"{k}-{v}" for k, v in unified.external_ids.items() if k != SERVICE_ID and v), None)
            dive_id = f"{GENERATOR}-{source}" if source else f"{GENERATOR}-{unified.date_time.strftime('%Y%m%dT%H%M%S')}"
            while self.find_dive(dive_id) is not None:
                dive_id += "x"
        if existing is not None:
            index = list(group).index(existing)
            group.remove(existing)
        else:
            index = len(group)
        dive = ET.Element("dive", {"id": dive_id})
        group.insert(index, dive)

        before = _sub(dive, "informationbeforedive")
        site_id = self._site_id(unified.location, unified.lat, unified.lng)
        if site_id:
            _sub(before, "link", ref=site_id)
        for bid in self._buddy_ids(unified.buddy):
            _sub(before, "link", ref=bid)
        decomodel_id = self._decomodel_id(unified)
        if decomodel_id:
            _sub(before, "link", ref=decomodel_id)
        if unified.dive_number is not None:
            _sub(before, "divenumber", unified.dive_number)
        _sub(before, "datetime", unified.date_time.strftime("%Y-%m-%dT%H:%M:%S"))
        if unified.air_temp is not None:
            _sub(before, "airtemperature", _fmt(c_to_kelvin(unified.air_temp)))
        if unified.surface_interval is not None:
            _sub(_sub(before, "surfaceintervalbeforedive"), "passedtime", _fmt(float(unified.surface_interval)))
        computer_id = self._computer_id(unified)
        rebreather_id, sensor_ids = self._o2sensor_ids(unified)
        suit_id = self._suit_id(unified)
        if computer_id or rebreather_id or suit_id or unified.weight is not None:
            used = _sub(before, "equipmentused")
            if computer_id:
                _sub(used, "link", ref=computer_id)
            if rebreather_id:
                _sub(used, "link", ref=rebreather_id)
            if suit_id:
                _sub(used, "link", ref=suit_id)
            if unified.weight is not None:
                _sub(used, "leadquantity", _fmt(round(weight_kg(unified.weight, unified.weight_unit), 3)))

        if unified.exit_lat is not None and unified.exit_lng is not None:
            # the exit position in dive_sync's own applicationdata element (R8)
            position = _sub(_sub(_sub(dive, "applicationdata"), APPLICATION_TAG), "exitposition")
            _sub(position, "latitude", f"{float(unified.exit_lat):.6f}")       # six decimals (~0.1 m), as Subsurface keeps them
            _sub(position, "longitude", f"{float(unified.exit_lng):.6f}")

        tank_ids: List[str] = []
        for idx, gas in enumerate(unified.gas_mixtures):
            tank = _sub(dive, "tankdata", id=f"{dive_id}-tank{idx}")
            tank_ids.append(tank.get("id"))
            _sub(tank, "link", ref=self._mix_id(gas.oxygen or 21.0, gas.helium or 0.0))
            if gas.tank_volume is not None:
                _sub(tank, "tankvolume", _fmt(round(gas.tank_volume / 1000.0, 6)))
            if gas.start_pressure is not None:
                _sub(tank, "tankpressurebegin", _fmt(bar_to_pa(gas.start_pressure)))
            if gas.end_pressure is not None:
                _sub(tank, "tankpressureend", _fmt(bar_to_pa(gas.end_pressure)))

        if unified.samples:
            samples = _sub(dive, "samples")
            events_at = events_by_sample(unified.samples, unified.events)
            mode = UDDF_DIVE_MODES.get(unified.dive_mode or "")
            for index, s in enumerate(unified.samples):
                wp = _sub(samples, "waypoint")
                _sub(wp, "depth", _fmt(round(s.depth, 3)))
                if s.time is not None:
                    _sub(wp, "divetime", _fmt(float(s.time)))
                if s.temp is not None:
                    _sub(wp, "temperature", _fmt(c_to_kelvin(s.temp)))
                self._write_waypoint_extras(wp, s, events_at.get(index, []), unified, tank_ids,
                                            mode if index == 0 else None, sensor_ids)

        after = _sub(dive, "informationafterdive")
        _sub(after, "greatestdepth", _fmt(round(unified.max_depth, 3)))
        if unified.avg_depth is not None:
            _sub(after, "averagedepth", _fmt(round(unified.avg_depth, 3)))
        _sub(after, "diveduration", _fmt(float(unified.duration)))
        if unified.temp_min is not None:
            _sub(after, "lowesttemperature", _fmt(c_to_kelvin(unified.temp_min)))
        if unified.visibility is not None:
            vis = unified.visibility / 3.28084 if (unified.visibility_unit or "").lower().startswith("f") else unified.visibility
            _sub(after, "visibility", _fmt(round(vis, 2)))
        rating = unified.service_fields.get("rating")
        if rating is not None:
            _sub(_sub(after, "rating"), "ratingvalue", int(rating))
        if unified.notes:
            notes = _sub(after, "notes")
            for para in str(unified.notes).split("\n"):
                _sub(notes, "para", para)
        return dive_id

    def _write_waypoint_extras(self, wp: ET.Element, s: UnifiedSample, events: Sequence[DiveEvent],
                               unified: UnifiedDive, tank_ids: Sequence[str], mode: Optional[str],
                               sensor_ids: Sequence[str] = ()) -> None:
        """The waypoint elements beyond depth, divetime and temperature: the
        sample's channels and the events that land on it (I5), a
        rebreather's cells as one ``measuredpo2`` per cell referring to its
        ``o2sensor`` (R18). Nothing is written for a sample without channels
        or events, so a dive without them gives the same waypoint as before.
        The elements follow the three profile ones in the order of the UDDF
        3.2 waypoint list."""
        ch = s.channels
        for event in events:
            if event.type == EVENT_ALERT:
                _sub(wp, "alarm", event.name or "alert")
        if ch is not None:
            if ch.ppo2 is not None:
                _sub(wp, "calculatedpo2", _fmt(bar_to_pa(ch.ppo2)))
            if ch.cns is not None:
                _sub(wp, "cns", _fmt(round(ch.cns, 2)))
            if ch.deco_stop_depth is not None and ch.deco_stop_depth > 0:
                attrs = {"kind": "mandatory", "decodepth": _fmt(round(ch.deco_stop_depth, 3))}
                if ch.deco_stop_time is not None:
                    attrs["duration"] = _fmt(float(ch.deco_stop_time))
                _sub(wp, "decostop", **attrs)
        for event in events:
            if event.type == EVENT_MODE_CHANGE and event.name in UDDF_DIVE_MODES:
                mode = UDDF_DIVE_MODES[event.name]
        if mode:
            _sub(wp, "divemode", type=mode)
        if ch is not None:
            if ch.gf99 is not None:
                _sub(wp, "gradientfactor", _fmt(round(ch.gf99, 2)))
            if ch.heart_rate is not None:
                _sub(wp, "heartbeat", ch.heart_rate)
            for idx in sorted(ch.o2_cells):
                if 0 <= idx < len(sensor_ids):
                    _sub(wp, "measuredpo2", _fmt(bar_to_pa(ch.o2_cells[idx])), ref=sensor_ids[idx])
            if ch.ndl is not None:
                _sub(wp, "nodecotime", _fmt(float(ch.ndl)))
            if ch.gas_time is not None:
                _sub(wp, "remainingbottomtime", _fmt(float(ch.gas_time)))
        for event in events:
            if event.type == EVENT_BOOKMARK:
                _sub(wp, "setmarker", event.name or "bookmark")
        setpoint = ch.setpoint if ch is not None else None
        for event in events:
            if event.type == EVENT_SETPOINT_CHANGE and event.value is not None:
                setpoint = event.value
        if setpoint is not None:
            _sub(wp, "setpo2", _fmt(bar_to_pa(setpoint)))
        for event in events:
            if event.type == EVENT_GAS_SWITCH:
                mix = self._event_mix(event, unified)
                if mix:
                    _sub(wp, "switchmix", ref=mix)
        if ch is not None:
            for idx in sorted(ch.pressures):
                if 0 <= idx < len(tank_ids):
                    _sub(wp, "tankpressure", _fmt(bar_to_pa(ch.pressures[idx])), ref=tank_ids[idx])
        elif s.pressure is not None and tank_ids:
            # A sample with one pressure and no channels (Shearwater, Subsurface,
            # Submersion): the reading is the first tank's (owner, 2026-10-02).
            _sub(wp, "tankpressure", _fmt(bar_to_pa(s.pressure)), ref=tank_ids[0])

    def _event_mix(self, event: DiveEvent, unified: UnifiedDive) -> Optional[str]:
        """The ``mix`` a gas switch refers to: the tank's gas when the event
        names a tank, else the mix the event names."""
        if event.tank is not None and 0 <= event.tank < len(unified.gas_mixtures):
            gas = unified.gas_mixtures[event.tank]
            return self._mix_id(gas.oxygen or 21.0, gas.helium or 0.0)
        if event.oxygen is not None:
            return self._mix_id(event.oxygen, event.helium or 0.0)
        return None

    def remove_dive(self, dive_id: str) -> bool:
        group = self.root.find("profiledata/repetitiongroup")
        dive = self.find_dive(dive_id)
        if dive is None:
            return False
        group.remove(dive)
        return True

    def save(self, path: str) -> None:
        # A deep copy: _restore_ns renames tags in place, and a shallow copy
        # shared the children, so after the first save the in-memory tree was
        # namespaced and the next write_dive of the same run could not find
        # profiledata/repetitiongroup any more (found with a 27-dive
        # Shearwater import, 2026-09-29).
        root = copy.deepcopy(self.root)
        _restore_ns(root)
        ET.indent(root, space="  ")
        ET.register_namespace("", NS)
        ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def _restore_ns(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and not el.tag.startswith("{"):
            el.tag = _q(el.tag)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class UddfAdapter(BaseDiveAdapter):
    """One ``.uddf`` file as a dive service. A missing file is created on the
    first write; reading a missing file yields no dives."""

    service_id = SERVICE_ID
    display_name = "UDDF file"
    stores_external_ids = False

    @classmethod
    def field_catalog(cls) -> List[FieldSpec]:
        return [
            FieldSpec(key="uddf.date_time", label="Start time", type="datetime", unified="date_time", writable=False),
            FieldSpec(key="uddf.duration", label="Duration", type="number", unified="duration", unit="s"),
            FieldSpec(key="uddf.max_depth", label="Max depth", type="number", unified="max_depth", unit="m"),
            FieldSpec(key="uddf.avg_depth", label="Average depth", type="number", unified="avg_depth", unit="m"),
            FieldSpec(key="uddf.temp_min", label="Lowest temperature", type="number", unified="temp_min", unit="°C"),
            FieldSpec(key="uddf.dive_number", label="Dive number", type="number", unified="dive_number"),
            FieldSpec(key="uddf.location", label="Dive site", type="text", unified="location"),
            FieldSpec(key="uddf.notes", label="Notes", type="text", unified="notes"),
            FieldSpec(key="uddf.buddy", label="Buddy", type="text", unified="buddy"),
            FieldSpec(key="uddf.visibility", label="Visibility", type="number", unified="visibility"),
            FieldSpec(key="uddf.gps", label="Site position", type="gps", unified="gps"),
            FieldSpec(key="uddf.exit_gps", label="Exit position (dive_sync applicationdata)", type="gps", unified="exit_gps"),
            FieldSpec(key="uddf.tanks", label="Tanks", type="tanks", unified="tanks"),
            FieldSpec(key="uddf.samples", label="Dive profile", type="samples", unified="samples"),
            FieldSpec(key="uddf.rating", label="Rating", type="number"),
            FieldSpec(key="uddf.air_temp", label="Air temperature", type="number", unified="air_temp", unit="°C"),
            FieldSpec(key="uddf.suit", label="Suit", type="text", unified="suit"),
        ]

    def __init__(self, path: str):
        self.path = path
        self.document: Optional[UddfDocument] = None

    def login(self) -> bool:
        try:
            if os.path.exists(self.path):
                root, _ = read_uddf(self.path)
                self.document = UddfDocument(root)
            else:
                self.document = UddfDocument()
            return True
        except ET.ParseError as e:
            logger.error("UDDF file %s is not valid XML: %s", self.path, e)
            return False

    def _doc(self) -> UddfDocument:
        if self.document is None and not self.login():
            raise RuntimeError(f"UDDF file not readable: {self.path}")
        return self.document

    def fetch_dives(self, date_from: Optional[datetime] = None, date_to: Optional[datetime] = None) -> List[UnifiedDive]:
        if not os.path.exists(self.path):
            self._doc()
            return []
        _root, dives = read_uddf(self.path)
        self.document = UddfDocument(_root)
        out = []
        for d in dives:
            if date_from and d.date_time < date_from:
                continue
            if date_to and d.date_time > date_to:
                continue
            out.append(d)
        return out

    def add_dive(self, dive: UnifiedDive) -> Optional[str]:
        doc = self._doc()
        dive_id = doc.write_dive(dive)
        doc.save(self.path)
        logger.info("UDDF: added dive %s to %s", dive_id, self.path)
        return dive_id

    def update_dive(self, external_id: str, dive: UnifiedDive) -> bool:
        doc = self._doc()
        if doc.find_dive(external_id) is None:
            logger.error("UDDF: no dive with id %s in %s", external_id, self.path)
            return False
        doc.write_dive(dive, dive_id=external_id)
        doc.save(self.path)
        return True

    def delete_dive(self, external_id: str) -> bool:
        doc = self._doc()
        if not doc.remove_dive(external_id):
            return False
        doc.save(self.path)
        return True
