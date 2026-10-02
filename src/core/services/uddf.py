"""UDDF 3.2 file adapter (rework.md F2).

UDDF (Universal Dive Data Format) is the one format both Subsurface and
Submersion import and export. This adapter reads and writes a single
``.uddf`` file with the standard library XML parser. Units in UDDF are SI:
metres, seconds, kelvin, pascal, cubic metres and kilograms; ``UnifiedDive``
uses Celsius, bar and litres, so the adapter converts.

Mapping (UDDF element -> UnifiedDive):
    profiledata/repetitiongroup/dive
        @id                                       external_ids["uddf"]
        informationbeforedive/datetime            date_time (local, naive)
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
        tankdata (link@ref -> gasdefinitions/mix) gas_mixtures
        samples/waypoint                          samples

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
    samples/waypoint/heartbeat (bpm), gradientfactor (%),        channels.heart_rate, gf99,
        remainingbottomtime (s)                                  gas_time
    samples/waypoint/switchmix@ref -> mix                        events: gas_switch
    samples/waypoint/alarm                                       events: alert (the computer's name)
    samples/waypoint/divemode@type                               dive_mode (first waypoint), events: mode_change
    samples/waypoint/setpo2 at an event                          events: setpoint_change
    samples/waypoint/setmarker                                   events: bookmark
An event lands on the waypoint of its second (else the nearest earlier one).
Not written, UDDF having no slot: time to surface, the transmitter
connection events, water type and density, exit position, bottom time, the
start and end CNS, a 'gauge' dive mode, tank names, and a sample's pressure
when the dive has no tank to refer it to.

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
LB_TO_KG = 0.45359237

# UnifiedDive.dive_mode (models.KNOWN_DIVE_MODES) <-> UDDF waypoint divemode@type.
# UDDF has no gauge mode. Reading 'opencircuit' back gives oc_single_gas when the
# dive breathed one mix and oc_multi_gas otherwise.
UDDF_DIVE_MODES: Dict[str, str] = {
    "oc_single_gas": "opencircuit", "oc_multi_gas": "opencircuit",
    "ccr": "closedcircuit", "scr": "semiclosedcircuit", "apnea": "apnoe",
}
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
    decomodels = {m.get("id", ""): m for m in root.findall("decomodel/buehlmann") if m.get("id")}

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
            tanks: List[GasMixture] = []
            tank_ids: List[str] = []
            tank_mix: List[str] = []
            for tank in dive.findall("tankdata"):
                o2, he = 21.0, 0.0
                link = tank.find("link")
                if link is not None and link.get("ref", "") in mixes:
                    o2, he = mixes[link.get("ref", "")]
                vol = _float(tank, "tankvolume")
                tanks.append(GasMixture(
                    oxygen=round(o2, 1), helium=round(he, 1),
                    start_pressure=pa_to_bar(_float(tank, "tankpressurebegin")),
                    end_pressure=pa_to_bar(_float(tank, "tankpressureend")),
                    tank_volume=None if vol is None else round(vol * 1000.0, 3),
                    tank_name=tank.get("id") or None,
                ))
                tank_ids.append(tank.get("id", ""))
                tank_mix.append(link.get("ref", "") if link is not None else "")
            samples, events, dive_mode = _read_waypoints(dive, tank_ids, tank_mix, mixes)
            computer = _read_computer(dive, computers)
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
            gf_low = _float(decomodel, "gradientfactorlow")
            gf_high = _float(decomodel, "gradientfactorhigh")
            dives.append(UnifiedDive(
                date_time=when,
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
            ))
    return root, dives


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


def _read_waypoints(dive: ET.Element, tank_ids: Sequence[str], tank_mix: Sequence[str],
                    mixes: Dict[str, Tuple[float, float]]) -> Tuple[List[UnifiedSample], List[DiveEvent], Optional[str]]:
    """The samples (with channels when a waypoint holds more than depth,
    time and temperature), the events the waypoints carry, and the
    ``divemode`` of the first waypoint that names one."""
    samples: List[UnifiedSample] = []
    events: List[DiveEvent] = []
    tank_index = {tid: i for i, tid in enumerate(tank_ids) if tid}
    mix_tank = {}
    for i, mix_id in enumerate(tank_mix):
        mix_tank.setdefault(mix_id, i)
    # UDDF's 'opencircuit' is oc_single_gas when the dive had one mix, else oc_multi_gas.
    open_circuit = "oc_single_gas" if len({mixes.get(m, (21.0, 0.0)) for m in tank_mix}) <= 1 else "oc_multi_gas"
    modes_back = dict(_DIVE_MODES_BACK, opencircuit=open_circuit)
    first_mode: Optional[str] = None
    last_mode: Optional[str] = None
    for wp in dive.findall("samples/waypoint"):
        depth = _float(wp, "depth")
        if depth is None:
            continue
        t = _float(wp, "divetime")
        time = None if t is None else int(round(t))
        channels = SampleChannels(
            ndl=_int_text(wp, "nodecotime"),
            deco_stop_depth=_attr_float(wp.find("decostop"), "decodepth"),
            deco_stop_time=_attr_int(wp.find("decostop"), "duration"),
            cns=_float(wp, "cns"),
            ppo2=pa_to_bar(_float(wp, "calculatedpo2")),
            setpoint=pa_to_bar(_float(wp, "setpo2")),
            gf99=_float(wp, "gradientfactor"),
            gas_time=_int_text(wp, "remainingbottomtime"),
            heart_rate=_int_text(wp, "heartbeat"),
        )
        for tp in wp.findall("tankpressure"):
            idx = tank_index.get(tp.get("ref", ""), 0 if len(tank_ids) <= 1 else None)
            bar = pa_to_bar(_float(tp, "."))
            if idx is not None and bar is not None:
                channels.pressures[idx] = bar
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
            events.append(DiveEvent(time=when, type=EVENT_GAS_SWITCH, tank=mix_tank.get(ref),
                                    oxygen=round(o2, 1) if o2 is not None else None,
                                    helium=round(he, 1) if he is not None else None))
        for alarm in wp.findall("alarm"):
            events.append(DiveEvent(time=when, type=EVENT_ALERT, name=(alarm.text or "").strip() or None))
        for marker in wp.findall("setmarker"):
            events.append(DiveEvent(time=when, type=EVENT_BOOKMARK, name=(marker.text or "").strip() or None))
        mode_el = wp.find("divemode")
        mode = (mode_el.get("type") or (mode_el.text or "").strip()) if mode_el is not None else None
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
        diver = self.root.find("diver")
        owner = diver.find("owner")
        if owner is None:
            owner = ET.Element("owner", {"id": "owner"})
            diver.insert(0, owner)                       # the spec puts the owner before the buddies
        equipment = owner.find("equipment")
        if equipment is None:
            equipment = _sub(owner, "equipment")
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
        if unified.surface_interval is not None:
            _sub(_sub(before, "surfaceintervalbeforedive"), "passedtime", _fmt(float(unified.surface_interval)))
        computer_id = self._computer_id(unified)
        if computer_id or unified.weight is not None:
            used = _sub(before, "equipmentused")
            if computer_id:
                _sub(used, "link", ref=computer_id)
            if unified.weight is not None:
                _sub(used, "leadquantity", _fmt(round(weight_kg(unified.weight, unified.weight_unit), 3)))

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
                                            mode if index == 0 else None)

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
                               unified: UnifiedDive, tank_ids: Sequence[str], mode: Optional[str]) -> None:
        """The waypoint elements beyond depth, divetime and temperature: the
        sample's channels and the events that land on it (I5). Nothing is
        written for a sample without channels or events, so a dive without
        them gives the same waypoint as before. The elements follow the
        three profile ones in the order of the UDDF 3.2 waypoint list."""
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
            FieldSpec(key="uddf.tanks", label="Tanks", type="tanks", unified="tanks"),
            FieldSpec(key="uddf.samples", label="Dive profile", type="samples", unified="samples"),
            FieldSpec(key="uddf.rating", label="Rating", type="number"),
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
