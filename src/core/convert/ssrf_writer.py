"""Subsurface's XML divelog (``.ssrf``) from `UnifiedDive`s (plans/convert.md
I6, decided Q4): the file Subsurface opens with File > Open or Import.

The layout and every value format follow Subsurface's own writer
(``core/save-xml.cpp``), checked against its export of a Garmin dive and
Submersion's ``.ssrf.xml`` exports, so a file written here reads like one
Subsurface wrote:

    <divelog program='subsurface' version='3'>
    <settings>
    <divecomputerid model='...' deviceid='8 hex' serial='...' firmware='...'/>
    </settings>
    <divesites>
    <site uuid='8 hex' name='...' gps='lat lng'>
    </site>
    </divesites>
    <dives>
    <dive number='N' rating='1-5' visibility='1-5' tags='a, b' divesiteid='8 hex' date='YYYY-MM-DD' time='hh:mm:ss' duration='mm:ss min'>
      <divemaster>, <buddy>, <notes>, <suit> (text elements)
      <cylinder size='11.1 l' description='...' o2='32.0%' he='..%' start='200.0 bar' end='50.0 bar' use='...' />
      <weightsystem weight='6.0 kg' description='weight' />
      <divecomputer model='...' deviceid='8 hex' diveid='8 hex' dctype='CCR'>
      <depth max='24.131 m' mean='10.138 m' />
      <temperature water='29.0 C' />
      <water salinity='1025 g/l' />
      <surfacetime>78:10 min</surfacetime>
      <extradata key='...' value='...' />
      <event time='m:ss min' type='25' flags='1' name='gaschange' cylinder='0' o2='32.0%' />
      <sample time='m:ss min' depth='1.416 m' temp='30.0 C' pressure0='192.91 bar' ndl='..' tts='..' rbt='..' in_deco='1' stoptime='..' stopdepth='..' cns='3%' po2='0.37 bar' heartbeat='72' />
      </divecomputer>
    </dive>
    </dives>
    </divelog>

Attribute quoting (single quotes, ``&lt; &gt; &amp; &apos; &quot;``, control
characters as ``?``), `put_milli` numbers ("11.1", "192.98", "30.0", from
``services/subsurface.py``'s `fmt_milli`), ``m:ss min`` durations, a site's
GPS to six decimals and 8-hex-digit ids are Subsurface's. As in Subsurface, a
sample writes its depth always, its temperature and the deco, CNS and PO2
values only when they change from the previous sample (the first NDL
always), every tank pressure it has (``pressureN`` by the tank's index in
``gas_mixtures``; a sample with no per-tank channels writes its one
`UnifiedSample.pressure` as ``pressure0``), and the heart rate whenever it
has one. The ``po2`` attribute is Subsurface's setpoint: a sample with a
setpoint writes that; one without writes the computer's PO2, as Subsurface
itself stores libdivecomputer's PO2 of an open-circuit dive.

What Subsurface has no element for goes into the dive computer's
``extradata`` under the keys in `EXTRA_*` (the deco model and gradient
factors as "ZHL-16C GF 40/85", the serial and firmware, the exit position as
"GPS2", bottom time, start and end CNS, the dive mode where ``dctype`` has
no word for it, a custom water type) and, as the git-storage writer does for
a sync, the other services' ids as ``dive_sync:<service>`` so a later sync
of the Subsurface log recognises the dive (tier 1). Dropped, named by
`ssrf_drops`: the visibility distance (Subsurface rates visibility with
stars), the GF99 channel, the tank roles Subsurface's cylinder ``use`` has
no word for, and the computer's PO2 on a dive that also has a setpoint.

Step I6b's reader mirrors this module: it uses the same constants and reads
back everything written here.
"""
from __future__ import annotations

import os
import zlib
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple, Union

from src.core.models import (
    EVENT_ALERT,
    EVENT_BOOKMARK,
    EVENT_GAS_SWITCH,
    EVENT_MODE_CHANGE,
    EVENT_SETPOINT_CHANGE,
    DiveEvent,
    GasMixture,
    UnifiedDive,
    UnifiedSample,
)
from src.core.services.subsurface import (
    EXTERNAL_ID_PREFIX,
    MANUAL_DC_MODEL,
    SERVICE_ID,
    TANK_ROLE_TO_USE,
    fmt_milli,
)
from src.core.site_matcher import find_site

PathLike = Union[str, "os.PathLike[str]"]

PROGRAM, DATAFORMAT_VERSION = "subsurface", "3"

# Subsurface's event type numbers (libdivecomputer's parser_sample_event_t
# plus Subsurface's own string event), as save-xml.cpp writes them.
EVENT_TYPE_BOOKMARK = 8        # SAMPLE_EVENT_BOOKMARK: Subsurface's bookmarks and "modechange" events
EVENT_TYPE_PO2 = 20            # SAMPLE_EVENT_PO2: the planner's "SP change", value = the setpoint in mbar
EVENT_TYPE_GASCHANGE = 25      # SAMPLE_EVENT_GASCHANGE2: "gaschange", flags = cylinder index + 1
EVENT_TYPE_STRING = 26         # SAMPLE_EVENT_STRING: a named event (Garmin's alerts come through as these)
EVENT_NAME_GASCHANGE = "gaschange"
EVENT_NAME_MODECHANGE = "modechange"
EVENT_NAME_SETPOINT = "SP change"
EVENT_NAME_BOOKMARK = "bookmark"

# UnifiedDive.dive_mode -> Subsurface's divemode_text ("OC" is the default and never written).
DIVEMODE_TEXT: Dict[str, str] = {"ccr": "CCR", "scr": "PSCR", "apnea": "Freedive"}
DIVEMODE_OC = "OC"

# Salinity Subsurface assumes for a water type (core/units.h) when the computer gave no density.
SALINITY_BY_WATER_TYPE: Dict[str, int] = {"fresh": 1000, "salt": 1030, "en13319": 1020}

# extradata keys: Subsurface's own where it has one (its Garmin import writes
# "Deco model", "Serial", "FW Version", "GPS2"), ours for the rest.
EXTRA_DECO_MODEL = "Deco model"
EXTRA_SERIAL = "Serial"
EXTRA_FIRMWARE = "FW Version"
EXTRA_EXIT_GPS = "GPS2"
EXTRA_BOTTOM_TIME = "Bottom time"
EXTRA_CNS_START = "CNS start"
EXTRA_CNS_END = "CNS end"
EXTRA_DIVE_MODE = "Dive mode"
EXTRA_WATER_TYPE = "Water type"
WEIGHT_DESCRIPTION = "weight"   # the git-storage writer's description of a dive's one weight line

# The dive-level fields the Subsurface adapter keeps in service_fields.
SF_RATING, SF_VISIBILITY, SF_TAGS, SF_SUIT, SF_DIVEMASTER = "rating", "visibility_stars", "tags", "suit", "divemaster"


# ---------------------------------------------------------------------------
# Value formats (save-xml.cpp / membuffer.cpp)
# ---------------------------------------------------------------------------

def xml_quote(text: str, attribute: bool = True) -> str:
    """Subsurface's `put_quoted`: ``<``, ``>`` and ``&`` always escaped, the
    quotes only inside an attribute, C0 control characters other than tab,
    newline and carriage return replaced by ``?``."""
    out: List[str] = []
    for ch in text:
        code = ord(ch)
        if (1 <= code <= 8) or code in (11, 12) or (14 <= code <= 31):
            out.append("?")
        elif ch == "<":
            out.append("&lt;")
        elif ch == ">":
            out.append("&gt;")
        elif ch == "&":
            out.append("&amp;")
        elif ch == "'" and attribute:
            out.append("&apos;")
        elif ch == '"' and attribute:
            out.append("&quot;")
        else:
            out.append(ch)
    return "".join(out)


def _text(value: Optional[str]) -> Optional[str]:
    """Subsurface's `show_utf8`: leading and trailing whitespace trimmed, an
    empty string is nothing to write."""
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def fmt_min(seconds: int) -> str:
    """``m:ss min``: Subsurface's `put_duration` and sample times (no padding of the minutes)."""
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d} min"


def fmt_percent(value: float) -> str:
    """A gas fraction as Subsurface's `put_gasmix` writes it: one decimal from a permille value ("32.0%")."""
    permille = int(round(value * 10))
    return f"{permille // 10}.{permille % 10}%"


def fmt_degrees(value: float) -> str:
    """Subsurface's `put_degrees`: micro-degrees as a signed number with six decimals."""
    udeg = int(round(value * 1_000_000))
    sign = "-" if udeg < 0 else ""
    udeg = abs(udeg)
    return f"{sign}{udeg // 1_000_000}.{udeg % 1_000_000:06d}"


def hex_id(*parts: object) -> str:
    """A stable 8-hex-digit id (the shape of Subsurface's uuid, deviceid and
    diveid) from the parts, never below 0x10000000: Subsurface writes these
    with ``%8x``, so a shorter value would come out with leading spaces."""
    value = zlib.crc32("\n".join(str(p) for p in parts).encode("utf-8")) & 0xFFFFFFFF
    if value < 0x10000000:
        value |= 0x10000000
    return f"{value:08x}"


def _milli(value: Optional[float]) -> int:
    """The integer Subsurface compares two samples' values by (mm, mbar, m°C)."""
    return 0 if value is None else int(round(value * 1000))


def _is_air(gas: GasMixture) -> bool:
    """Subsurface's `sanitize_gasmix`: 20.9-21.1 % O2 with no helium is air and gets no ``o2`` attribute."""
    return 209 <= int(round(gas.oxygen * 10)) <= 211 and not gas.helium


def _gasmix_attrs(oxygen: Optional[float], helium: Optional[float]) -> str:
    """`put_gasmix`: ``o2`` when it is not zero, ``he`` only with an ``o2``."""
    if not oxygen:
        return ""
    out = f" o2='{fmt_percent(oxygen)}'"
    if helium:
        out += f" he='{fmt_percent(helium)}'"
    return out


def _attr(name: str, value: Optional[str]) -> str:
    """`` name='value'`` for a text value, nothing for none."""
    value = _text(value)
    return f" {name}='{xml_quote(value)}'" if value is not None else ""


def _kg(dive: UnifiedDive) -> Optional[float]:
    if dive.weight is None:
        return None
    pounds = (dive.weight_unit or "").lower().startswith("p")
    return round(dive.weight / 2.20462, 3) if pounds else float(dive.weight)


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------

class _Site:
    def __init__(self, uuid: str, name: Optional[str], lat: Optional[float], lng: Optional[float]):
        self.uuid, self.name, self.lat, self.lng = uuid, name, lat, lng


class SsrfDocument:
    """One ``.ssrf`` file in the making: `add_dive` for every dive, then
    `to_string` or `save`. Dive sites are shared between the dives (same
    name, or a position within 200 m, as the Subsurface adapter resolves
    them) and the computers of the ``settings`` section are one per
    model and serial."""

    def __init__(self) -> None:
        self.sites: List[_Site] = []
        self.computers: Dict[Tuple[str, str], Tuple[str, str, str]] = {}   # (model, serial) -> (deviceid, serial, firmware)
        self.dives: List[str] = []

    # -- shared tables -----------------------------------------------------

    def _site_for(self, dive: UnifiedDive) -> Optional[_Site]:
        name = _text(dive.location)
        has_gps = dive.lat is not None and dive.lng is not None
        if name is None and not has_gps:
            return None
        found = find_site(self.sites, name, dive.lat, dive.lng,
                          get_name=lambda s: s.name, get_gps=lambda s: (s.lat, s.lng))
        if found is not None:
            return found
        uuid = hex_id("site", name or "", fmt_degrees(dive.lat) if has_gps else "", fmt_degrees(dive.lng) if has_gps else "")
        taken = {s.uuid for s in self.sites}
        while uuid in taken:
            uuid = f"{(int(uuid, 16) + 1) & 0xFFFFFFFF or 0x10000000:08x}"
        site = _Site(uuid, name, dive.lat if has_gps else None, dive.lng if has_gps else None)
        self.sites.append(site)
        return site

    @staticmethod
    def computer_model(dive: UnifiedDive) -> Optional[str]:
        """``<vendor> <model>`` as Subsurface names a computer; "manually added
        dive" (Subsurface's own hand-logged computer) for a dive with neither
        a computer nor a profile; None when the computer is unknown but a
        profile was recorded."""
        parts = [p for p in (_text(dive.computer_vendor), _text(dive.computer_model)) if p]
        if parts:
            return " ".join(parts)
        return None if dive.samples else MANUAL_DC_MODEL

    def _device_id(self, dive: UnifiedDive) -> Optional[str]:
        """The computer's ``deviceid``, registered for the ``settings`` section
        when the dive names a computer or a serial."""
        model, serial = self.computer_model(dive), _text(dive.computer_serial)
        if (model is None or model == MANUAL_DC_MODEL) and serial is None:
            return None
        key = (model or "", serial or "")
        if key not in self.computers:
            self.computers[key] = (hex_id("device", *key), serial or "", _text(dive.computer_firmware) or "")
        return self.computers[key][0]

    # -- one dive ----------------------------------------------------------

    def add_dive(self, dive: UnifiedDive) -> None:
        """Render ``dive`` as a ``<dive>`` block, registering its site and computer."""
        site = self._site_for(dive)
        sf = dive.service_fields or {}
        out: List[str] = []
        head = "<dive"
        if dive.dive_number:
            head += f" number='{int(dive.dive_number)}'"
        if sf.get(SF_RATING):
            head += f" rating='{int(sf[SF_RATING])}'"
        if sf.get(SF_VISIBILITY):
            head += f" visibility='{int(sf[SF_VISIBILITY])}'"
        tags = [t for t in (_text(str(t)) for t in (sf.get(SF_TAGS) or [])) if t]
        if tags:
            head += f" tags='{', '.join(xml_quote(t) for t in tags)}'"
        if site is not None:
            head += f" divesiteid='{site.uuid}'"
        when: datetime = dive.date_time
        head += f" date='{when:%Y-%m-%d}' time='{when:%H:%M:%S}'"
        if dive.duration and int(dive.duration) > 0:
            head += f" duration='{fmt_min(int(dive.duration))}'"
        out.append(head + ">")
        for tag, value in (("divemaster", sf.get(SF_DIVEMASTER)), ("buddy", dive.buddy), ("notes", dive.notes), ("suit", sf.get(SF_SUIT))):
            text = _text(value)
            if text is not None:
                out.append(f"  <{tag}>{xml_quote(text, attribute=False)}</{tag}>")
        for gas in dive.gas_mixtures:
            out.append(self._cylinder(gas))
        kg = _kg(dive)
        if kg:
            out.append(f"  <weightsystem weight='{fmt_milli(kg)} kg' description='{WEIGHT_DESCRIPTION}' />")
        out.extend(self._divecomputer(dive))
        out.append("</dive>")
        self.dives.append("\n".join(out) + "\n")

    @staticmethod
    def _cylinder(gas: GasMixture) -> str:
        line = "  <cylinder"
        if gas.tank_volume:
            line += f" size='{fmt_milli(gas.tank_volume)} l'"
        line += _attr("description", gas.tank_name)
        if not _is_air(gas):
            line += _gasmix_attrs(gas.oxygen, gas.helium)
        if gas.start_pressure:
            line += f" start='{fmt_milli(gas.start_pressure)} bar'"
        if gas.end_pressure:
            line += f" end='{fmt_milli(gas.end_pressure)} bar'"
        use = TANK_ROLE_TO_USE.get(gas.tank_role or "")
        if use:
            line += f" use='{xml_quote(use)}'"
        return line + " />"

    def _divecomputer(self, dive: UnifiedDive) -> List[str]:
        out: List[str] = []
        model = self.computer_model(dive)
        head = "  <divecomputer" + _attr("model", model)
        device_id = self._device_id(dive)
        if device_id is not None:
            head += f" deviceid='{device_id}'"
            head += f" diveid='{hex_id('dive', model or '', dive.computer_serial or '', dive.date_time.isoformat(), dive.dive_number or '')}'"
        dctype = DIVEMODE_TEXT.get(dive.dive_mode or "")
        if dctype:
            head += f" dctype='{dctype}'"
        out.append(head + ">")
        if dive.max_depth or dive.avg_depth:
            depth = f"  <depth max='{fmt_milli(dive.max_depth or 0.0)} m'"
            if dive.avg_depth is not None:
                depth += f" mean='{fmt_milli(dive.avg_depth)} m'"
            out.append(depth + " />")
        if dive.temp_min is not None:
            out.append(f"  <temperature water='{fmt_milli(dive.temp_min)} C' />")
        salinity = self.salinity(dive)
        if salinity:
            out.append(f"  <water salinity='{salinity} g/l' />")
        if dive.surface_interval:
            out.append(f"  <surfacetime>{fmt_min(int(dive.surface_interval))}</surfacetime>")
        for key, value in self.extradata(dive):
            out.append(f"  <extradata key='{xml_quote(key)}' value='{xml_quote(value)}' />")
        for event in sorted(dive.events, key=lambda e: e.time):
            out.append(self._event(event, dive))
        out.extend(self._samples(dive))
        out.append("  </divecomputer>")
        return out

    @staticmethod
    def salinity(dive: UnifiedDive) -> Optional[int]:
        """``<water salinity>`` in g/l: the computer's density, else Subsurface's value for the water type."""
        if dive.water_density:
            return int(round(dive.water_density))
        return SALINITY_BY_WATER_TYPE.get(dive.water_type or "")

    @staticmethod
    def deco_model_text(dive: UnifiedDive) -> Optional[str]:
        """"ZHL-16C GF 40/85", "GF 40/85" or "ZHL-16C": the one string Subsurface's import keeps the deco model in."""
        parts: List[str] = []
        if _text(dive.deco_model):
            parts.append(_text(dive.deco_model))
        if dive.gf_low is not None or dive.gf_high is not None:
            parts.append(f"GF {dive.gf_low if dive.gf_low is not None else '?'}/{dive.gf_high if dive.gf_high is not None else '?'}")
        return " ".join(parts) or None

    @classmethod
    def extradata(cls, dive: UnifiedDive) -> List[Tuple[str, str]]:
        """The ``extradata`` pairs of the dive's computer, in writing order."""
        out: List[Tuple[str, str]] = []
        deco = cls.deco_model_text(dive)
        if deco:
            out.append((EXTRA_DECO_MODEL, deco))
        if _text(dive.computer_serial):
            out.append((EXTRA_SERIAL, _text(dive.computer_serial)))
        if _text(dive.computer_firmware):
            out.append((EXTRA_FIRMWARE, _text(dive.computer_firmware)))
        if dive.dive_mode and dive.dive_mode not in DIVEMODE_TEXT:
            out.append((EXTRA_DIVE_MODE, dive.dive_mode))
        if dive.water_type and dive.water_type not in SALINITY_BY_WATER_TYPE:
            out.append((EXTRA_WATER_TYPE, dive.water_type))
        if dive.exit_lat is not None and dive.exit_lng is not None:
            out.append((EXTRA_EXIT_GPS, f"{fmt_degrees(dive.exit_lat)}, {fmt_degrees(dive.exit_lng)}"))
        if dive.bottom_time:
            out.append((EXTRA_BOTTOM_TIME, fmt_min(int(dive.bottom_time))))
        if dive.cns_start is not None:
            out.append((EXTRA_CNS_START, f"{fmt_milli(dive.cns_start)}%"))
        if dive.cns_end is not None:
            out.append((EXTRA_CNS_END, f"{fmt_milli(dive.cns_end)}%"))
        for service, value in sorted(dive.external_ids.items()):
            if service != SERVICE_ID and value:
                out.append((EXTERNAL_ID_PREFIX + service, str(value)))
        return out

    @staticmethod
    def _event(event: DiveEvent, dive: UnifiedDive) -> str:
        """`save_one_event`: time, type, flags, value or divemode, name, then the gas of a gas change."""
        line = f"  <event time='{fmt_min(event.time)}'"
        if event.type == EVENT_GAS_SWITCH:
            line += f" type='{EVENT_TYPE_GASCHANGE}'"
            if event.tank is not None:
                line += f" flags='{int(event.tank) + 1}'"
            line += f" name='{EVENT_NAME_GASCHANGE}'"
            if event.tank is not None:
                line += f" cylinder='{int(event.tank)}'"
            oxygen, helium = event.oxygen, event.helium
            if oxygen is None and event.tank is not None and 0 <= event.tank < len(dive.gas_mixtures):
                gas = dive.gas_mixtures[event.tank]
                oxygen, helium = gas.oxygen, gas.helium
            if oxygen is not None and not (209 <= int(round(oxygen * 10)) <= 211 and not helium):
                line += _gasmix_attrs(oxygen, helium)
            return line + " />"
        if event.type == EVENT_MODE_CHANGE:
            mode = DIVEMODE_TEXT.get(event.name or "", DIVEMODE_OC)
            return line + f" type='{EVENT_TYPE_BOOKMARK}' divemode='{mode}' name='{EVENT_NAME_MODECHANGE}' />"
        if event.type == EVENT_SETPOINT_CHANGE:
            line += f" type='{EVENT_TYPE_PO2}'"
            if event.value:
                line += f" value='{_milli(event.value)}'"
            return line + f" name='{EVENT_NAME_SETPOINT}' />"
        if event.type == EVENT_BOOKMARK:
            return line + f" type='{EVENT_TYPE_BOOKMARK}'" + _attr("name", event.name or EVENT_NAME_BOOKMARK) + " />"
        # an alert, or a kind of event only the reader names: a string event with the computer's own name
        line += f" type='{EVENT_TYPE_STRING}'"
        if event.value:
            line += f" value='{int(round(event.value))}'"
        line += _attr("name", event.name or event.type.replace("_", " "))
        if event.tank is not None:
            line += f" cylinder='{int(event.tank)}'"
        return line + " />"

    @staticmethod
    def _samples(dive: UnifiedDive) -> List[str]:
        """`save_samples`: the sticky values (temperature, NDL, TTS, RBT,
        in_deco, stop, CNS, PO2) only when they change; the NDL starts at -1
        so the first one is always written, the others at 0."""
        out: List[str] = []
        last_temp, last_ndl, last_tts, last_rbt = 0, -1, 0, 0
        last_in_deco, last_stoptime, last_stopdepth, last_cns, last_po2 = False, 0, 0, 0, 0
        for sample in dive.samples:
            line = f"  <sample time='{fmt_min(int(sample.time or 0))}' depth='{fmt_milli(sample.depth)} m'"
            if sample.temp is not None and _milli(sample.temp) and _milli(sample.temp) != last_temp:
                last_temp = _milli(sample.temp)
                line += f" temp='{fmt_milli(sample.temp)} C'"
            ch = sample.channels
            pressures = dict(ch.pressures) if ch and ch.pressures else ({0: sample.pressure} if sample.pressure is not None else {})
            for tank, bar in sorted(pressures.items()):
                if _milli(bar):
                    line += f" pressure{int(tank)}='{fmt_milli(bar)} bar'"
            if ch is not None:
                if ch.ndl is not None and int(ch.ndl) != last_ndl:
                    last_ndl = int(ch.ndl)
                    line += f" ndl='{fmt_min(last_ndl)}'"
                if ch.tts is not None and int(ch.tts) != last_tts:
                    last_tts = int(ch.tts)
                    line += f" tts='{fmt_min(last_tts)}'"
                if ch.gas_time is not None and int(ch.gas_time) != last_rbt:
                    last_rbt = int(ch.gas_time)
                    line += f" rbt='{fmt_min(last_rbt)}'"
                if ch.deco_stop_depth is not None:
                    in_deco = ch.deco_stop_depth > 0
                    if in_deco != last_in_deco:
                        last_in_deco = in_deco
                        line += f" in_deco='{1 if in_deco else 0}'"
                if ch.deco_stop_time is not None and int(ch.deco_stop_time) != last_stoptime:
                    last_stoptime = int(ch.deco_stop_time)
                    line += f" stoptime='{fmt_min(last_stoptime)}'"
                if ch.deco_stop_depth is not None and _milli(ch.deco_stop_depth) != last_stopdepth:
                    last_stopdepth = _milli(ch.deco_stop_depth)
                    line += f" stopdepth='{fmt_milli(ch.deco_stop_depth)} m'"
                if ch.cns is not None and int(round(ch.cns)) != last_cns:
                    last_cns = int(round(ch.cns))
                    line += f" cns='{last_cns}%'"
                po2 = ch.setpoint if ch.setpoint is not None else ch.ppo2
                if po2 is not None and _milli(po2) != last_po2:
                    last_po2 = _milli(po2)
                    line += f" po2='{fmt_milli(po2)} bar'"
                if ch.heart_rate:
                    line += f" heartbeat='{int(ch.heart_rate)}'"
            out.append(line + " />")
        return out

    # -- the whole file ------------------------------------------------------

    def to_string(self) -> str:
        out = [f"<divelog program='{PROGRAM}' version='{DATAFORMAT_VERSION}'>", "<settings>"]
        for (model, _serial), (device_id, serial, firmware) in self.computers.items():
            out.append(f"<divecomputerid{_attr('model', model)} deviceid='{device_id}'{_attr('serial', serial)}{_attr('firmware', firmware)}/>")
        out += ["</settings>", "<divesites>"]
        for site in self.sites:
            line = f"<site uuid='{site.uuid}'" + _attr("name", site.name)
            if site.lat is not None and site.lng is not None:
                line += f" gps='{fmt_degrees(site.lat)} {fmt_degrees(site.lng)}'"
            out += [line + ">", "</site>"]
        out += ["</divesites>", "<dives>"]
        text = "\n".join(out) + "\n" + "".join(self.dives)
        return text + "</dives>\n</divelog>\n"

    def save(self, path: PathLike) -> None:
        path = os.fspath(path)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(self.to_string())


def write_ssrf(dives: Sequence[UnifiedDive], path: PathLike) -> List[str]:
    """Write ``dives`` as one ``.ssrf`` file at ``path``; returns `ssrf_drops`."""
    document = SsrfDocument()
    for dive in dives:
        document.add_dive(dive)
    document.save(path)
    return ssrf_drops(dives)


def ssrf_drops(dives: Sequence[UnifiedDive]) -> List[str]:
    """What the writer leaves out of these dives, one line each, for the
    page to show. The timezone and the average and maximum water temperature
    are not mentioned: no Subsurface log carries a zone, and the profile
    holds the temperatures."""
    dropped: List[str] = []
    if any(d.visibility is not None and d.service_fields.get(SF_VISIBILITY) is None for d in dives):
        dropped.append("the visibility distance is not written (Subsurface rates visibility with stars)")
    if any(s.channels and s.channels.gf99 is not None for d in dives for s in d.samples):
        dropped.append("the GF99 channel is not written")
    roles = sorted({g.tank_role for d in dives for g in d.gas_mixtures if g.tank_role and g.tank_role not in TANK_ROLE_TO_USE})
    if roles:
        dropped.append(f"the {', '.join(roles)} tank role{'s are' if len(roles) > 1 else ' is'} not written (Subsurface's cylinder use has no word for {'them' if len(roles) > 1 else 'it'})")
    for d in dives:
        if any(s.channels and s.channels.setpoint is not None for s in d.samples) and any(s.channels and s.channels.ppo2 is not None for s in d.samples):
            dropped.append("the computer's PO2 is not written where the dive has a setpoint (Subsurface's po2 is the setpoint)")
            break
    return [f"Subsurface: {item}" for item in dropped]
