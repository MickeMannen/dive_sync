"""Write a Garmin dive ``.fit`` from any `UnifiedDive` (plans/convert.md I12).

Save as .fit on the Dive edit page patches the watch's own file when the
dive was opened from one (`fit_patch`); a dive from a UDDF or Subsurface
file, or any dive with no ``.fit`` behind it, is encoded from scratch here.
The encoder is Garmin's own `garmin_fit_sdk` (the FIT Protocol License;
the owner's decision of 2026-10-18 to ship it), and this is the one shipped
module that imports it: the reader stays on ``fitdecode``, the patcher and
the merge are byte-level and need no SDK. The message set and the field use
are ported from UWMedia's ``parsers/fit_writer.py`` (MIT, same owner),
which builds a Descent-style activity file from a simulated dive plan;
here the input is a `UnifiedDive`, so what the plan knew by construction
(computer, gases, tanks) comes from the dive's fields, and a value the
dive does not hold is left out of the file, never defaulted.

What is written, in file order:
- ``file_id``: type activity, manufacturer garmin, the Garmin product of
  the dive's computer model (`garmin_files.FIT_PRODUCT_NAMES`) or
  `APP_PRODUCT` (Garmin Connect's own id for a file no watch recorded)
  when the model is not a Garmin's, the serial when it is a number,
  ``time_created`` = the start;
- ``device_info`` for the computer (the "creator": product, serial, the
  firmware as ``software_version`` when it is a number) and one per
  tank transmitter the file names;
- ``dive_settings``: deco model (``zhl_16c`` when the dive says ZHL-16C),
  gradient factors, water type and density;
- one ``dive_gas`` per `gas_mixtures` entry, in order, enabled - the
  reader (`fit_reader`) pairs transmitter i with gas i, as Garmin Connect
  does, so the entries keep their index rather than being merged by mix;
- the timer start ``event``, then per sample a ``record`` (depth,
  temperature, next stop depth and time, time to surface, NDL, CNS, PO2,
  gas time, heart rate - whichever the sample has) and a ``tank_update``
  per tank reading; the gas switches (``dive_gas_switched`` with the gas
  index), alerts (``dive_alert`` by Garmin's name or the raw number) and
  transmitter connect/disconnect events at their time; the timer stop;
- ``tank_summary`` per transmitter (start and end pressure, volume used
  when it can be computed), the transmitter profile (message 147, as
  libdivecomputer names it: id, name, volume in tenths of a litre - not
  in Garmin's public profile, so appended as a raw message);
- ``dive_summary`` (dive number, bottom time, surface interval, CNS start
  and end, max and average depth), ``lap`` and ``session`` (start time,
  elapsed and timer time, sport diving with the dive mode as sub-sport,
  entry and exit position, water temperatures, depths) and ``activity``
  with ``local_timestamp`` = start + the dive's UTC offset, from which
  the reader recovers the zone as ``Etc/GMT∓N``.

Tanks: when any entry of `gas_mixtures` has tank data (a pressure, a
volume, a name or readings in the samples), every entry gets a transmitter
id (`TANK_SENSOR_BASE` + index) and a ``tank_summary``, so the file's tank
count equals its gas count and the index pairing is exact; with no tank
data anywhere, no tank message is written.

What no FIT holds is reported by `fit_drops`, not written: site, buddy,
notes, weight, visibility, a deco model other than ZHL-16C, the computer
maker and model as text when they are not Garmin's own, a serial that is
no number, a firmware that is no number, a water type or dive mode outside
Garmin's, a tank's role, the GF99 and setpoint channels, the events with
no FIT equivalent (mode change, setpoint change, bookmark), a gas switch
whose gas cannot be told, the other services' ids and the service-specific
fields (a Subsurface rating, tags, suit, divemaster).
"""
from __future__ import annotations

import logging
import os
import re
import struct
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from garmin_fit_sdk import Encoder, Profile

from src.core import garmin_files
from src.core.convert.edit import utc_offset_of
from src.core.convert.fit_patch import recompute_crcs
from src.core.convert.fit_reader import SENSOR_PROFILE, SENSOR_PROFILE_ID, SENSOR_PROFILE_NAME, SENSOR_PROFILE_VOLUME
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

logger = logging.getLogger("dive_sync.convert.fit_writer")

PathLike = Union[str, "os.PathLike[str]"]

FIT_EPOCH = datetime(1989, 12, 31)
# The line the writers report their drops with; the Dive edit page strips
# the prefix and shows the names ("Not in a .fit, save as UDDF or
# Subsurface for these: …").
FIT_DROPS_PREFIX = "Garmin FIT: not in a .fit, save as UDDF or Subsurface for these: "

# file_id.product when the dive's computer is not a Garmin model the app
# knows: Garmin Connect's own product id, the one it stamps on the FIT it
# makes for a dive no watch recorded ("connect", 65534).
APP_PRODUCT = 65534
# Transmitter ids the file names its tanks under, TANK_SENSOR_BASE + the
# tank's index in gas_mixtures - a FIT only knows a tank by its transmitter.
TANK_SENSOR_BASE = 100001
MAX_SERIAL = 0xFFFFFFFF          # uint32z: 0 is "unset"
MAX_FIRMWARE = 655.35            # device_info.software_version is a uint16 in hundredths
MAX_PO2 = 2.54                   # record.po2 is a uint8 in hundredths, and 255 is its "unset" mark
MAX_SENSOR_NAME = 31             # bytes of a transmitter name in the profile message

# UnifiedDive.dive_mode -> session.sub_sport (the reader's DIVE_MODES inverted; apnea
# is written as Garmin names it although the reader refuses apnea files).
SUB_SPORTS: Dict[str, str] = {
    "oc_single_gas": "single_gas_diving", "oc_multi_gas": "multi_gas_diving",
    "gauge": "gauge_diving", "ccr": "ccr_diving", "apnea": "apnea_diving",
}
WATER_TYPES = ("fresh", "salt", "en13319", "custom")
ZHL_16C = "zhl_16c"
# The dive events a FIT has a message for; the others are named by fit_drops.
EVENT_TANK_POD = {"tank_pod_connected", "tank_pod_disconnected"}
EVENT_LABELS = {EVENT_MODE_CHANGE: "mode change", EVENT_SETPOINT_CHANGE: "setpoint change", EVENT_BOOKMARK: "bookmark"}

_NUMBER = re.compile(r"^\d+(?:\.\d+)?$")


def _mesg_num(name: str) -> int:
    """Profile["messages"] is keyed by the number; its entries carry the name."""
    return next(num for num, m in Profile["messages"].items() if m["name"] == name)


def _type_key(type_name: str, value: str) -> Optional[int]:
    """The number of a named value of a profile type (``dive_alert``,
    ``safety_stop_started`` -> 22); None when the type has no such name."""
    for key, name in Profile["types"].get(type_name, {}).items():
        if name == value:
            return int(key)
    return None


def _semicircles(degrees: Optional[float]) -> Optional[int]:
    return int(round(degrees * 2 ** 31 / 180.0)) if degrees is not None else None


def _round(value: Optional[float], digits: int = 0) -> Optional[Any]:
    if value is None:
        return None
    return int(round(value)) if digits == 0 else round(float(value), digits)


def _text(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value or None


# ---------------------------------------------------------------------------
# What of the dive the file can name
# ---------------------------------------------------------------------------

def product_of(dive: UnifiedDive) -> Optional[int]:
    """The Garmin product number of the dive's computer model, when the maker
    is Garmin (or unsaid) and the model is one `garmin_files.FIT_PRODUCT_NAMES`
    knows; None otherwise."""
    vendor, model = _text(dive.computer_vendor), _text(dive.computer_model)
    if model is None or (vendor is not None and vendor.lower() != "garmin"):
        return None
    for number, name in garmin_files.FIT_PRODUCT_NAMES.items():
        if name.lower() == model.lower():
            return number
    return None


def serial_of(dive: UnifiedDive) -> Optional[int]:
    """The computer serial as the FIT's number, when the text is one (1 … 2^32-1)."""
    serial = _text(dive.computer_serial)
    if serial is None or not serial.isdigit():
        return None
    value = int(serial)
    return value if 0 < value <= MAX_SERIAL else None


def firmware_of(dive: UnifiedDive) -> Optional[float]:
    """The firmware as ``device_info.software_version`` takes it (a number, hundredths)."""
    firmware = _text(dive.computer_firmware)
    if firmware is None or not _NUMBER.match(firmware):
        return None
    value = round(float(firmware), 2)
    return value if 0 <= value <= MAX_FIRMWARE else None


def deco_model_of(dive: UnifiedDive) -> Optional[str]:
    """``zhl_16c`` when the dive's deco model says so (any spelling of ZHL-16C); None otherwise."""
    text = _text(dive.deco_model)
    if text is None:
        return None
    return ZHL_16C if re.sub(r"[\s\-_]", "", text).upper() in ("ZHL16C", "BUHLMANNZHL16C", "BÜHLMANNZHL16C") else None


def has_tank_data(dive: UnifiedDive) -> bool:
    """Whether any gas entry is a tank the file should name a transmitter for."""
    for gas in dive.gas_mixtures:
        if gas.start_pressure is not None or gas.end_pressure is not None or gas.tank_volume is not None or _text(gas.tank_name):
            return True
    for sample in dive.samples:
        if sample.pressure is not None or (sample.channels is not None and sample.channels.pressures):
            return True
    return False


def _gas_index_of(event: DiveEvent, gases: Sequence[GasMixture]) -> Optional[int]:
    """The gas_mixtures index a gas switch names: its ``tank`` when it is
    one, else the first entry with the mix it names."""
    if event.tank is not None and 0 <= event.tank < len(gases):
        return int(event.tank)
    if event.oxygen is not None:
        for i, gas in enumerate(gases):
            if abs(gas.oxygen - event.oxygen) < 0.05 and abs((gas.helium or 0.0) - (event.helium or 0.0)) < 0.05:
                return i
    return None


def _alert_data(event: DiveEvent) -> Optional[int]:
    """The ``dive_alert`` value of an alert: the number of Garmin's name for
    it, or the number itself when the name is one (how the reader keeps an
    alert Garmin's profile does not name yet). An alert named otherwise
    has no Garmin code and is dropped, whatever value it carries."""
    name = _text(event.name)
    if name is None:
        return None
    key = _type_key("dive_alert", name)
    if key is not None:
        return key
    return int(name) if name.isdigit() else None


def fit_drops(dive: UnifiedDive) -> List[str]:
    """What `write_fit` leaves out of ``dive``, as the bare names the Dive
    edit page lists ("site", "buddy", …), in a fixed order; [] when the
    file holds everything the dive has."""
    out: List[str] = []
    for attr, label in (("location", "site"), ("buddy", "buddy"), ("notes", "notes")):
        if _text(getattr(dive, attr)):
            out.append(label)
    if dive.weight is not None:
        out.append("weight")
    if dive.visibility is not None:
        out.append("visibility")
    if _text(dive.deco_model) and deco_model_of(dive) is None:
        out.append("deco model")
    vendor = _text(dive.computer_vendor)
    if vendor is not None and vendor.lower() != "garmin":
        out.append("computer maker")
    if _text(dive.computer_model) and product_of(dive) is None:
        out.append("computer model")
    if _text(dive.computer_serial) and serial_of(dive) is None:
        out.append("computer serial")
    if _text(dive.computer_firmware) and firmware_of(dive) is None:
        out.append("computer firmware")
    if dive.water_type is not None and dive.water_type not in WATER_TYPES:
        out.append("water type")
    if dive.dive_mode is not None and dive.dive_mode not in SUB_SPORTS:
        out.append("dive mode")
    if any(_text(g.tank_role) for g in dive.gas_mixtures):
        out.append("tank role")
    if any(s.channels is not None and s.channels.gf99 is not None for s in dive.samples):
        out.append("GF99 channel")
    if any(s.channels is not None and s.channels.setpoint is not None for s in dive.samples):
        out.append("setpoint channel")
    kinds: List[str] = []
    for event in dive.events:
        if event.type == EVENT_GAS_SWITCH:
            label = "gas switch events without a gas" if _gas_index_of(event, dive.gas_mixtures) is None else None
        elif event.type == EVENT_ALERT:
            label = "alerts outside Garmin's list" if _alert_data(event) is None else None
        elif event.type in EVENT_TANK_POD:
            label = None if has_tank_data(dive) and event.tank is not None and 0 <= event.tank < len(dive.gas_mixtures) else "transmitter events without a tank"
        else:
            label = f"{EVENT_LABELS.get(event.type, event.type.replace('_', ' '))} events"
        if label and label not in kinds:
            kinds.append(label)
    out += kinds
    if any(v for v in dive.external_ids.values()):
        out.append("service ids")
    for key, value in dive.service_fields.items():
        if value not in (None, "", [], {}):
            out.append(key.replace("_", " "))
    return out


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------

def _start_seconds(dive: UnifiedDive) -> Tuple[int, Optional[int]]:
    """(the start as FIT seconds UTC, the UTC offset in seconds or None):
    from the dive's UTC clock, else its local clock less the zone's
    offset, else the local clock taken as UTC with no zone written."""
    offset = utc_offset_of(dive)
    if dive.date_time_utc is not None:
        start_utc = dive.date_time_utc
    elif offset is not None:
        start_utc = dive.date_time - offset
    else:
        start_utc = dive.date_time
    start = int(round((start_utc.replace(tzinfo=None) - FIT_EPOCH).total_seconds()))
    offset_seconds = int(round(offset.total_seconds() / 60.0)) * 60 if offset is not None else None
    return start, offset_seconds


def _sensor_profile(local: int, sensor: int, index: int, name: Optional[str], volume: Optional[float]) -> bytes:
    """A definition and data record of the transmitter profile (message
    147: id as uint32z, index, the name as a string, the volume in tenths
    of a litre), which Garmin's public profile - and so the SDK's encoder -
    does not know."""
    fields: List[Tuple[int, bytes, int]] = [(SENSOR_PROFILE_ID, struct.pack("<I", sensor), 0x8C),
                                            (254, struct.pack("<H", index), 0x84)]
    if name:
        raw = name.encode("utf-8")[:MAX_SENSOR_NAME] + b"\x00"
        fields.append((SENSOR_PROFILE_NAME, raw, 0x07))
    if volume is not None:
        fields.append((SENSOR_PROFILE_VOLUME, struct.pack("<H", max(0, min(0xFFFE, int(round(volume * 10))))), 0x84))
    definition = bytearray([0x40 | local, 0, 0]) + struct.pack("<H", SENSOR_PROFILE) + bytes([len(fields)])
    data = bytearray([local])
    for num, raw, base in fields:
        definition += bytes((num, len(raw), base))
        data += raw
    return bytes(definition + data)


def _append_raw(data: bytes, messages: Sequence[bytes]) -> bytes:
    """``messages`` (raw FIT records) added at the end of the encoder's
    file: the header's data size and both CRCs recomputed."""
    if not messages:
        return data
    out = bytearray(data[:-2]) + b"".join(messages) + b"\x00\x00"
    struct.pack_into("<I", out, 4, len(out) - 14 - 2)
    recompute_crcs(out)
    return bytes(out)


class _Writer:
    """One file in the making (`write_fit` drives it)."""

    def __init__(self, dive: UnifiedDive):
        self.dive = dive
        self.encoder = Encoder()
        self.start, self.offset = _start_seconds(dive)
        self.duration = max(0, int(dive.duration or 0))
        self.end = self.start + self.duration
        self.product = product_of(dive) or APP_PRODUCT
        self.serial = serial_of(dive)
        self.gases = list(dive.gas_mixtures)
        self.sensors: Dict[int, int] = (
            {i: TANK_SENSOR_BASE + i for i in range(len(self.gases))} if has_tank_data(dive) else {})
        self.sub_sport = SUB_SPORTS.get(dive.dive_mode or "")

    def write(self, name: str, **fields: Any) -> None:
        self.encoder.write_mesg({"mesg_num": _mesg_num(name), **{k: v for k, v in fields.items() if v is not None}})

    # -- the sections -------------------------------------------------------

    def header(self) -> None:
        self.write("file_id", type="activity", manufacturer="garmin", product=self.product,
                   serial_number=self.serial, time_created=self.start)
        self.write("device_info", timestamp=self.start, device_index="creator", manufacturer="garmin",
                   product=self.product, serial_number=self.serial, software_version=firmware_of(self.dive),
                   source_type="local")
        for i, sensor in self.sensors.items():
            self.write("device_info", timestamp=self.start, device_index=i + 1, manufacturer="garmin",
                       serial_number=sensor, source_type="antplus")

    def settings(self) -> None:
        dive = self.dive
        water_type = dive.water_type if dive.water_type in WATER_TYPES else None
        fields = dict(model=deco_model_of(dive), gf_low=_round(dive.gf_low), gf_high=_round(dive.gf_high),
                      water_type=water_type, water_density=dive.water_density)
        if any(v is not None for v in fields.values()):
            self.write("dive_settings", message_index=0, **fields)
        for i, gas in enumerate(self.gases):
            mode = "closed_circuit_diluent" if (gas.tank_role or "") == "diluent" else None
            self.write("dive_gas", message_index=i, helium_content=_round(gas.helium or 0.0),
                       oxygen_content=_round(gas.oxygen), status="enabled", mode=mode)

    def _event(self, event: DiveEvent) -> None:
        stamp = self.start + max(0, int(event.time))
        if event.type == EVENT_GAS_SWITCH:
            index = _gas_index_of(event, self.gases)
            if index is not None:
                self.write("event", timestamp=stamp, event="dive_gas_switched", event_type="marker", data=index)
        elif event.type == EVENT_ALERT:
            data = _alert_data(event)
            if data is not None:
                self.write("event", timestamp=stamp, event="dive_alert", event_type="marker", data=data)
        elif event.type in EVENT_TANK_POD and event.tank is not None and event.tank in self.sensors:
            self.write("event", timestamp=stamp, event=event.type, event_type="marker", data=self.sensors[event.tank])

    def _record(self, sample: UnifiedSample) -> None:
        stamp = self.start + max(0, int(sample.time or 0))
        ch = sample.channels
        fields: Dict[str, Any] = dict(timestamp=stamp, depth=_round(sample.depth, 3), temperature=_round(sample.temp))
        if ch is not None:
            fields.update(next_stop_depth=_round(ch.deco_stop_depth, 3), next_stop_time=_round(ch.deco_stop_time),
                          time_to_surface=_round(ch.tts), ndl_time=_round(ch.ndl), cns_load=_round(ch.cns),
                          po2=_round(min(ch.ppo2, MAX_PO2), 2) if ch.ppo2 is not None else None,
                          air_time_remaining=_round(ch.gas_time), heart_rate=_round(ch.heart_rate))
        self.write("record", **fields)
        pressures: Dict[int, float] = dict(ch.pressures) if ch is not None and ch.pressures else {}
        if not pressures and sample.pressure is not None:
            pressures = {0: sample.pressure}
        for tank, bar in sorted(pressures.items()):
            if tank in self.sensors and bar is not None:
                self.write("tank_update", timestamp=stamp, sensor=self.sensors[tank], pressure=_round(bar, 2))

    def profile(self) -> None:
        """The timer, the samples with their tank readings and the events, in time order."""
        self.write("event", timestamp=self.start, event="timer", event_type="start", event_group=0)
        events = sorted(self.dive.events, key=lambda e: e.time)
        samples = [s for s in self.dive.samples if s.time is not None]
        samples.sort(key=lambda s: s.time or 0)
        e = 0
        for sample in samples:
            while e < len(events) and events[e].time <= (sample.time or 0):
                self._event(events[e])
                e += 1
            self._record(sample)
        for event in events[e:]:
            self._event(event)
        self.write("event", timestamp=self.end, event="timer", event_type="stop_all", event_group=0)

    def summaries(self) -> List[bytes]:
        """The tank summaries, the dive summary, lap, session and activity;
        returns the raw transmitter-profile records to append."""
        dive, raw = self.dive, []
        for i, sensor in self.sensors.items():
            gas = self.gases[i]
            used = None
            if gas.start_pressure is not None and gas.end_pressure is not None and gas.tank_volume:
                used = round(max(0.0, (gas.start_pressure - gas.end_pressure) * gas.tank_volume), 2)
            self.write("tank_summary", timestamp=self.end, sensor=sensor, start_pressure=_round(gas.start_pressure, 2),
                       end_pressure=_round(gas.end_pressure, 2), volume_used=used)
            if _text(gas.tank_name) or gas.tank_volume is not None:
                raw.append(_sensor_profile(15, sensor, i, _text(gas.tank_name), gas.tank_volume))
        self.write("dive_summary", timestamp=self.end, reference_mesg="session", reference_index=0,
                   avg_depth=_round(dive.avg_depth, 3), max_depth=_round(dive.max_depth, 3),
                   surface_interval=_round(dive.surface_interval), start_cns=_round(dive.cns_start),
                   end_cns=_round(dive.cns_end), dive_number=_round(dive.dive_number),
                   bottom_time=_round(dive.bottom_time, 3))
        common = dict(start_time=self.start, total_elapsed_time=float(self.duration), total_timer_time=float(self.duration),
                      sport="diving", sub_sport=self.sub_sport,
                      start_position_lat=_semicircles(dive.lat), start_position_long=_semicircles(dive.lng),
                      end_position_lat=_semicircles(dive.exit_lat), end_position_long=_semicircles(dive.exit_lng),
                      avg_temperature=_round(dive.temp_avg), max_temperature=_round(dive.temp_max),
                      min_temperature=_round(dive.temp_min),
                      avg_depth=_round(dive.avg_depth, 3), max_depth=_round(dive.max_depth, 3))
        self.write("lap", message_index=0, timestamp=self.end, event="lap", event_type="stop", **common)
        # Real Descent files stamp session and activity with the dive's start, which the reader reads the start from.
        self.write("session", message_index=0, timestamp=self.start, event="session", event_type="stop",
                   first_lap_index=0, num_laps=1, trigger="activity_end", **common)
        self.write("activity", timestamp=self.start, total_timer_time=float(self.duration), num_sessions=1,
                   type="manual", event="activity", event_type="stop",
                   local_timestamp=self.start + self.offset if self.offset is not None else None)
        return raw

    def encode(self) -> bytes:
        self.header()
        self.settings()
        self.profile()
        raw = self.summaries()
        return _append_raw(self.encoder.close(), raw)


def encode_fit(dive: UnifiedDive) -> bytes:
    """The bytes of a Garmin ``.fit`` holding ``dive`` (what `write_fit` writes)."""
    return _Writer(dive).encode()


def write_fit(dive: UnifiedDive, path: PathLike) -> List[str]:
    """Write ``dive`` as a Garmin ``.fit`` at ``path`` (overwritten; its
    folder created). Returns the drop line (`fit_drops` behind
    `FIT_DROPS_PREFIX`) when the dive holds what no FIT can, else []."""
    data = encode_fit(dive)
    path = os.fspath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    drops = fit_drops(dive)
    logger.debug("Wrote dive %s as %s: %d samples, %d tanks, %d bytes%s", dive.dive_number, path,
                 len(dive.samples), len(dive.gas_mixtures), len(data), f", dropped {', '.join(drops)}" if drops else "")
    return [FIT_DROPS_PREFIX + ", ".join(drops)] if drops else []
