"""Save a dive edited on the Dive edit page back as a Garmin ``.fit`` by
patching the file it was opened from (plans/convert.md I14b).

A FIT is rewritten at the record level, never decoded and encoded again:
the file's own definition records say where every field of every data
record sits and what size, base type and byte order it has, so an edited
value is written into its slot and every other byte - messages and fields
the app does not know, developer data, the transmitter profiles Garmin's
public profile does not name - is copied as it was. Fixed-size records keep
the layout, so only the fields the edit touched and the two checksums
change. No encoder and nothing of Garmin's SDK is involved.

The walker (`fit_crc`, `_Definition`, `_walk`, `_fields`) and the timestamp
shift are a port of UWMedia's ``parsers/fit_time.py`` (MIT, same owner;
Log Viewer's "Adjust time"): every ``timestamp`` field (253), the
``date_time`` fields dive files carry (``file_id.time_created``,
``session`` / ``lap.start_time``) and the 5-bit offset of a compressed
timestamp header move by the same delta, and ``activity.local_timestamp``
is set to the new activity timestamp plus the new UTC offset, which is
where `fit_reader` takes the dive's zone from.

What is patched, field by field (`FIELD_SLOTS`, `TANK_SLOTS`): the dive
number, bottom time, surface interval and CNS in ``dive_summary``; max and
average depth in ``dive_summary``, ``session`` and ``lap``; the duration in
``session``, ``lap`` and ``activity``; the entry and exit position
(degrees to semicircles) in ``session`` and ``lap``; the gradient factors,
water type and water density in ``dive_settings``; the dive mode as the
sub-sport of ``session`` and ``lap``; the average water temperature (and
the min and max on a dive without a profile) in ``session`` and ``lap``;
per tank, the O2 and He of its ``dive_gas`` record, the start and end
pressure of its ``tank_summary`` and the volume of its transmitter profile
(message 147, field 77, tenths of a litre), matched the way the reader
pairs them (transmitter i with gas i). Every data record of a message is
patched alike, so the two ``dive_summary`` records agree.

What a FIT cannot hold is reported, not written: site, buddy, notes,
weight, visibility, the deco model and computer texts, the events, a tank
name or role, a tank or gas added on the page (the file has no record for
it), the min and max water temperature of a dive with a profile (the
samples give them), and any field whose slot the file does not carry.
"""
from __future__ import annotations

import io
import logging
import os
import struct
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from src.core.convert.fit_reader import SENSOR_PROFILE, SENSOR_PROFILE_VOLUME, read_fit
from src.core.models import GasMixture, UnifiedDive

logger = logging.getLogger("dive_sync.convert.fit_patch")

PathLike = Union[str, "os.PathLike[str]"]

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)

# Global message numbers (Garmin's FIT profile; fitdecode.profile names them).
FILE_ID, SESSION, LAP, RECORD, EVENT, ACTIVITY = 0, 18, 19, 20, 21, 34
DIVE_SETTINGS, DIVE_GAS, DIVE_SUMMARY, TANK_UPDATE, TANK_SUMMARY = 258, 259, 268, 319, 323

TIMESTAMP_FIELD = 253
# (global message number, field number) of the other date_time fields dive files carry.
DATE_TIME_FIELDS = {(FILE_ID, 4), (SESSION, 2), (LAP, 2)}      # file_id.time_created, session/lap.start_time
ACTIVITY_LOCAL_TIMESTAMP = 5
UINT32 = 0x86
INVALID_UINT32 = 0xFFFFFFFF
# FIT date_time values below this are seconds since device power-on, not dates.
MIN_DATE_TIME = 0x10000000

# base type byte -> (struct code, size, the type's "invalid"/unset value; None: the float NaN pattern)
BASE_TYPES: Dict[int, Tuple[str, int, Optional[int]]] = {
    0x00: ("B", 1, 0xFF), 0x01: ("b", 1, 0x7F), 0x02: ("B", 1, 0xFF), 0x07: ("s", 1, 0x00),
    0x83: ("h", 2, 0x7FFF), 0x84: ("H", 2, 0xFFFF), 0x85: ("i", 4, 0x7FFFFFFF), 0x86: ("I", 4, 0xFFFFFFFF),
    0x88: ("f", 4, None), 0x89: ("d", 8, None),
    0x0A: ("B", 1, 0x00), 0x8B: ("H", 2, 0x0000), 0x8C: ("I", 4, 0x00000000), 0x0D: ("B", 1, 0xFF),
    0x8E: ("q", 8, 0x7FFFFFFFFFFFFFFF), 0x8F: ("Q", 8, 0xFFFFFFFFFFFFFFFF), 0x90: ("Q", 8, 0),
}
FLOAT_TYPES = frozenset({0x88, 0x89})

# UnifiedDive words -> the FIT enums (fitdecode.profile FIELD_TYPES; 63 is Garmin's ccr_diving the profile lacks).
WATER_TYPES: Dict[str, int] = {"fresh": 0, "salt": 1, "en13319": 2, "custom": 3}
SUB_SPORTS: Dict[str, int] = {"oc_single_gas": 53, "oc_multi_gas": 54, "gauge": 55, "ccr": 63}
SEMICIRCLES_PER_DEGREE = 2 ** 31 / 180.0
SENSOR_PROFILE_VOLUME_SCALE = 10.0      # 111 -> 11.1 L, as the reader divides it

_CRC_TABLE = (0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
              0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400)


class FitPatchError(ValueError):
    """The source is not a FIT file the patcher can walk, or the patch
    cannot be made (a time out of the FIT's range)."""


def fit_crc(data: bytes, crc: int = 0) -> int:
    """The FIT CRC-16 of ``data`` (ported from UWMedia's fit_time.py)."""
    for byte in data:
        for nibble in (byte & 0x0F, byte >> 4):
            tmp = _CRC_TABLE[crc & 0x0F]
            crc = (crc >> 4) & 0x0FFF
            crc = crc ^ tmp ^ _CRC_TABLE[nibble]
    return crc


class _Definition:
    """A definition record: what the data records of a local message
    number hold, in file order (ported from UWMedia's fit_time.py)."""

    def __init__(self, mesg_num: int, endian: str, fields, dev_size: int, raw: bytes = b""):
        self.mesg_num = mesg_num
        self.endian = endian
        self.fields = fields  # [(field number, size, base type)]
        self.dev_size = dev_size
        self.raw = raw  # the definition message as it is in the file, header byte included

    @property
    def size(self) -> int:
        return sum(size for _, size, _ in self.fields) + self.dev_size


def _walk(data: bytearray, on_message: Callable[[_Definition, int, int, bool], None]) -> None:
    """Call ``on_message(definition, offset of the message's fields, header
    offset, compressed)`` for every data message of every FIT file chained
    in ``data``. Raises `FitPatchError` for something that is not a FIT
    file (ported from UWMedia's fit_time.py)."""
    pos = 0
    while pos < len(data):
        if len(data) - pos < 12:
            raise FitPatchError("not a FIT file (too short)")
        header_size = data[pos]
        data_size = struct.unpack_from("<I", data, pos + 4)[0]
        if header_size not in (12, 14) or bytes(data[pos + 8:pos + 12]) != b".FIT":
            raise FitPatchError("not a FIT file")
        end = pos + header_size + data_size
        if end + 2 > len(data):
            raise FitPatchError("the FIT file is truncated")
        definitions: Dict[int, _Definition] = {}
        p = pos + header_size
        while p < end:
            header = data[p]
            if header & 0x80:  # compressed timestamp header: a data message
                definition = definitions.get((header >> 5) & 0x03)
                if definition is None:
                    raise FitPatchError("FIT data message without a definition")
                on_message(definition, p + 1, p, True)
                p += 1 + definition.size
            elif header & 0x40:  # definition message
                local = header & 0x0F
                endian = ">" if data[p + 2] == 1 else "<"
                mesg_num = struct.unpack_from(endian + "H", data, p + 3)[0]
                count = data[p + 5]
                fields = [(data[p + 6 + 3 * i], data[p + 7 + 3 * i], data[p + 8 + 3 * i]) for i in range(count)]
                q = p + 6 + 3 * count
                dev_size = 0
                if header & 0x20:  # developer fields
                    dev_count = data[q]
                    dev_size = sum(data[q + 2 + 3 * i] for i in range(dev_count))
                    q += 1 + 3 * dev_count
                definitions[local] = _Definition(mesg_num, endian, fields, dev_size, bytes(data[p:q]))
                p = q
            else:
                definition = definitions.get(header & 0x0F)
                if definition is None:
                    raise FitPatchError("FIT data message without a definition")
                on_message(definition, p + 1, p, False)
                p += 1 + definition.size
        pos = end + 2


def _fields(definition: _Definition, start: int) -> Iterator[Tuple[int, int, int, int]]:
    """(field number, offset, size, base type) of each field of a message."""
    offset = start
    for num, size, base in definition.fields:
        yield num, offset, size, base
        offset += size


def _read_u32(data, offset: int, endian: str) -> int:
    return struct.unpack_from(endian + "I", data, offset)[0]


def recompute_crcs(data: bytearray) -> List[Tuple[int, int]]:
    """Recompute the CRC of every FIT file chained in ``data``: the 14-byte
    header's own CRC when it carries one (a zero means "not computed" and
    is left so) and the file CRC over the header and the records. Returns
    the byte spans written, as (offset, size)."""
    spans: List[Tuple[int, int]] = []
    pos = 0
    while pos < len(data):
        header_size = data[pos]
        end = pos + header_size + struct.unpack_from("<I", data, pos + 4)[0]
        if header_size == 14 and struct.unpack_from("<H", data, pos + 12)[0] != 0:
            struct.pack_into("<H", data, pos + 12, fit_crc(data[pos:pos + 12]))
            spans.append((pos + 12, 2))
        struct.pack_into("<H", data, end, fit_crc(data[pos:end]))
        spans.append((end, 2))
        pos = end + 2
    return spans


# ---------------------------------------------------------------------------
# Where each UnifiedDive field lives
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Slot:
    """One FIT field a dive value is written into: the message, the field
    number and how the value becomes the raw integer (``scale`` as the
    profile's, ``enum`` for a word, ``semicircles`` for degrees)."""
    mesg: int
    num: int
    scale: float = 1.0
    enum: Optional[Mapping[str, int]] = None
    semicircles: bool = False
    as_float: bool = False


# UnifiedDive attribute -> (label, the slots it is written into; the first is the reader's own)
FIELD_SLOTS: Dict[str, Tuple[str, Tuple[Slot, ...]]] = {
    "dive_number": ("dive number", (Slot(DIVE_SUMMARY, 10),)),
    "bottom_time": ("bottom time", (Slot(DIVE_SUMMARY, 11, 1000),)),
    "surface_interval": ("surface interval", (Slot(DIVE_SUMMARY, 4),)),
    "cns_start": ("CNS start", (Slot(DIVE_SUMMARY, 5),)),
    "cns_end": ("CNS end", (Slot(DIVE_SUMMARY, 6),)),
    "max_depth": ("max depth", (Slot(DIVE_SUMMARY, 3, 1000), Slot(SESSION, 141, 1000), Slot(LAP, 123, 1000))),
    "avg_depth": ("avg depth", (Slot(DIVE_SUMMARY, 2, 1000), Slot(SESSION, 140, 1000), Slot(LAP, 122, 1000))),
    "duration": ("duration", (Slot(SESSION, 8, 1000), Slot(LAP, 8, 1000), Slot(ACTIVITY, 0, 1000))),
    "lat": ("latitude", (Slot(SESSION, 3, semicircles=True), Slot(LAP, 3, semicircles=True))),
    "lng": ("longitude", (Slot(SESSION, 4, semicircles=True), Slot(LAP, 4, semicircles=True))),
    "exit_lat": ("exit latitude", (Slot(SESSION, 38, semicircles=True), Slot(LAP, 5, semicircles=True))),
    "exit_lng": ("exit longitude", (Slot(SESSION, 39, semicircles=True), Slot(LAP, 6, semicircles=True))),
    "gf_low": ("GF low", (Slot(DIVE_SETTINGS, 2),)),
    "gf_high": ("GF high", (Slot(DIVE_SETTINGS, 3),)),
    "water_type": ("water type", (Slot(DIVE_SETTINGS, 4, enum=WATER_TYPES),)),
    "water_density": ("water density", (Slot(DIVE_SETTINGS, 5, as_float=True),)),
    "dive_mode": ("dive mode", (Slot(SESSION, 6, enum=SUB_SPORTS), Slot(LAP, 39, enum=SUB_SPORTS))),
    "temp_avg": ("water temp avg", (Slot(SESSION, 57), Slot(LAP, 50))),
    "temp_max": ("water temp max", (Slot(SESSION, 58), Slot(LAP, 51))),
    "temp_min": ("water temp min", (Slot(SESSION, 150), Slot(LAP, 124))),
}
# Fields of a dive with a profile whose value the reader takes from the samples.
PROFILE_TEMPERATURES = ("temp_min", "temp_max")
# Per tank: GasMixture attribute -> (label, where): the gas record, the tank summary or the transmitter profile.
TANK_SLOTS: Dict[str, Tuple[str, str, int, float]] = {
    "oxygen": ("O2", "gas", 1, 1.0),
    "helium": ("He", "gas", 0, 1.0),
    "start_pressure": ("start pressure", "summary", 1, 100.0),
    "end_pressure": ("end pressure", "summary", 2, 100.0),
    "tank_volume": ("volume", "profile", SENSOR_PROFILE_VOLUME, SENSOR_PROFILE_VOLUME_SCALE),
}
TANK_TEXTS = {"tank_name": "name", "tank_role": "role"}
# What no FIT holds: attribute -> label.
NOT_IN_A_FIT: Dict[str, str] = {
    "location": "site", "buddy": "buddy", "notes": "notes", "weight": "weight", "visibility": "visibility",
    "deco_model": "deco model", "computer_vendor": "computer maker", "computer_model": "computer model",
    "computer_serial": "computer serial", "computer_firmware": "computer firmware", "events": "events",
    "samples": "profile samples",
}


@dataclass
class PatchReport:
    """What `patch_fit` did: the fields written (``patched``), the edited
    fields the file cannot take (``not_carried``, the page's "not in a
    .fit" list), the shift applied to every timestamp (``shifted_seconds``)
    and the byte spans written as (offset, size), the checksums included -
    every byte outside them is the source's."""
    patched: List[str] = field(default_factory=list)
    not_carried: List[str] = field(default_factory=list)
    shifted_seconds: int = 0
    spans: List[Tuple[int, int]] = field(default_factory=list)
    source_member: Optional[str] = None

    @property
    def changed(self) -> bool:
        return bool(self.patched) or self.shifted_seconds != 0


@dataclass
class _Record:
    """One data record: its definition and where its fields start."""
    definition: _Definition
    start: int
    header: int
    compressed: bool

    def slot(self, num: int) -> Optional[Tuple[int, int, int]]:
        """(offset, size, base type) of field ``num``, None when the record has none."""
        for field_num, offset, size, base in _fields(self.definition, self.start):
            if field_num == num:
                return offset, size, base
        return None


class _Patcher:
    """One patch run over the bytes of one FIT file."""

    def __init__(self, data: bytearray):
        self.data = data
        self.records: List[_Record] = []
        self.by_mesg: Dict[int, List[_Record]] = {}
        self.report = PatchReport()
        _walk(data, self._collect)
        if not self.records:
            raise FitPatchError("not a FIT file: no data messages")

    def _collect(self, definition: _Definition, start: int, header: int, compressed: bool) -> None:
        record = _Record(definition, start, header, compressed)
        self.records.append(record)
        self.by_mesg.setdefault(definition.mesg_num, []).append(record)

    # -- raw access -----------------------------------------------------

    def read(self, record: _Record, num: int) -> Optional[Union[int, float]]:
        """A scalar field's raw value, None when the record lacks it, it is
        an array or it holds the type's invalid value."""
        slot = record.slot(num)
        if slot is None:
            return None
        offset, size, base = slot
        spec = BASE_TYPES.get(base)
        if spec is None or spec[1] != size or base == 0x07:
            return None
        value = struct.unpack_from(record.definition.endian + spec[0], self.data, offset)[0]
        if spec[2] is not None and value == spec[2]:
            return None
        return value

    def write(self, record: _Record, num: int, raw: Optional[Union[int, float]]) -> Optional[str]:
        """Write ``raw`` (None: the type's invalid value) into field ``num``
        of ``record``. Returns the reason when it cannot be written (no such
        field, an array or string field, out of the type's range); None when done."""
        slot = record.slot(num)
        if slot is None:
            return "the file has no field for it"
        offset, size, base = slot
        spec = BASE_TYPES.get(base)
        if spec is None or spec[1] != size or base == 0x07:
            return "the field is not a single number in this file"
        code, _size, invalid = spec
        endian = record.definition.endian
        if raw is None:
            if base in FLOAT_TYPES:
                struct.pack_into(endian + ("I" if size == 4 else "Q"), self.data, offset, (1 << (8 * size)) - 1)
            else:
                struct.pack_into(endian + code, self.data, offset, invalid)
        else:
            try:
                if base in FLOAT_TYPES:
                    struct.pack_into(endian + code, self.data, offset, float(raw))
                else:
                    value = int(raw)
                    if invalid is not None and value == invalid:
                        return "the value is the field's own 'unset' mark"
                    struct.pack_into(endian + code, self.data, offset, value)
            except (struct.error, OverflowError, ValueError):
                return "out of the field's range"
        self.report.spans.append((offset, size))
        return None

    # -- the fields -------------------------------------------------------

    def patch_value(self, label: str, slots: Sequence[Slot], value: Any) -> None:
        """Write ``value`` into every data record of every slot's message;
        patched when the reader's own slot (the first) took it in at least
        one record, not carried (with the reason) otherwise."""
        done = False
        reason = "the file has no field for it"
        for i, slot in enumerate(slots):
            records = self.by_mesg.get(slot.mesg, [])
            try:
                raw = _raw(slot, value)
            except (KeyError, TypeError, ValueError) as e:
                reason = str(e.args[0]) if e.args else str(e)
                break
            written = False
            for record in records:
                problem = self.write(record, slot.num, raw)
                if problem is None:
                    written = True
                elif i == 0:
                    reason = problem
            if i == 0:
                done = written
        if done:
            self.report.patched.append(label)
        else:
            self.report.not_carried.append(f"{label} ({reason})")

    # -- timestamps -------------------------------------------------------

    def shift_times(self, delta: int, offset_minutes: Optional[int]) -> None:
        """Move every timestamp by ``delta`` seconds (the port of UWMedia's
        adjust_fit_time) and set ``activity.local_timestamp`` to the new
        activity timestamp plus ``offset_minutes`` (left alone when None)."""
        data = self.data

        def shift(offset: int, endian: str) -> Optional[int]:
            value = _read_u32(data, offset, endian)
            if value == INVALID_UINT32 or value < MIN_DATE_TIME:
                return None
            value += delta
            if not MIN_DATE_TIME <= value < INVALID_UINT32:
                raise FitPatchError("the new start time is outside what a FIT file can hold")
            if delta:
                struct.pack_into(endian + "I", data, offset, value)
                self.report.spans.append((offset, 4))
            return value

        for record in self.records:
            definition = record.definition
            if record.compressed and delta:
                # The 5-bit time offset is relative to the last full timestamp, which moves by delta too.
                data[record.header] = (data[record.header] & 0xE0) | ((data[record.header] + delta) & 0x1F)
                self.report.spans.append((record.header, 1))
            new_timestamp, local_offset = None, None
            for num, offset, size, base in _fields(definition, record.start):
                if size != 4 or base & 0x1F != UINT32 & 0x1F:
                    continue
                if num == TIMESTAMP_FIELD:
                    new_timestamp = shift(offset, definition.endian)
                elif (definition.mesg_num, num) in DATE_TIME_FIELDS:
                    shift(offset, definition.endian)
                elif definition.mesg_num == ACTIVITY and num == ACTIVITY_LOCAL_TIMESTAMP:
                    local_offset = offset
            if local_offset is not None and new_timestamp is not None and offset_minutes is not None:
                struct.pack_into(definition.endian + "I", data, local_offset, new_timestamp + offset_minutes * 60)
                self.report.spans.append((local_offset, 4))
        self.report.shifted_seconds = delta

    def has_local_timestamp(self) -> bool:
        return any(r.slot(ACTIVITY_LOCAL_TIMESTAMP) is not None for r in self.by_mesg.get(ACTIVITY, []))

    # -- tanks and gases ----------------------------------------------------

    def gas_records(self) -> List[_Record]:
        """The enabled ``dive_gas`` records in file order - gas i of the reader."""
        out = []
        for record in self.by_mesg.get(DIVE_GAS, []):
            status = self.read(record, 2)
            if status == 0:      # disabled
                continue
            out.append(record)
        return out

    def tank_sensors(self) -> List[int]:
        """The transmitters in the reader's order: ``tank_summary`` first, then
        the sensors only ``tank_update`` names."""
        sensors: List[int] = []
        for mesg in (TANK_SUMMARY, TANK_UPDATE):
            for record in self.by_mesg.get(mesg, []):
                sensor = self.read(record, 0)
                if sensor is not None and sensor not in sensors:
                    sensors.append(int(sensor))
        return sensors

    def tank_records(self, sensor: int, mesg: int) -> List[_Record]:
        """The records of ``mesg`` whose sensor id field (0) is ``sensor``."""
        return [r for r in self.by_mesg.get(mesg, []) if self.read(r, 0) == sensor]


def _raw(slot: Slot, value: Any) -> Optional[Union[int, float]]:
    """The raw field value of ``value`` for ``slot``: None stays None (the
    field is cleared), a word goes through the slot's enum (KeyError for an
    unknown one), degrees become semicircles, a number is scaled and rounded."""
    if value is None:
        return None
    if slot.enum is not None:
        if value not in slot.enum:
            raise KeyError(f"'{value}' is not a value a FIT knows")
        return slot.enum[value]
    if slot.semicircles:
        return int(round(float(value) * SEMICIRCLES_PER_DEGREE))
    if slot.as_float:
        return float(value)
    return int(round(float(value) * slot.scale))


def _differs(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        if a is None or b is None:
            return True
        return abs(float(a) - float(b)) > 1e-9
    return a != b


def _utc_delta(original: UnifiedDive, edited: UnifiedDive) -> int:
    """The seconds every timestamp moves: by the UTC clocks when both dives
    have one (a zone change alone moves the UTC instant, not the wall clock),
    else by the wall clocks."""
    if original.date_time_utc is not None and edited.date_time_utc is not None:
        return int(round((edited.date_time_utc - original.date_time_utc).total_seconds()))
    return int(round((edited.date_time - original.date_time).total_seconds()))


def _offset_minutes(dive: UnifiedDive) -> Optional[int]:
    if dive.date_time_utc is None:
        return None
    return int(round((dive.date_time - dive.date_time_utc).total_seconds() / 60.0))


def _tank_label(index: int) -> str:
    return f"tank {index + 1}"


def _patch_tanks(patcher: _Patcher, original: Sequence[GasMixture], edited: Sequence[GasMixture]) -> None:
    gases, sensors = patcher.gas_records(), patcher.tank_sensors()
    report = patcher.report
    for i, tank in enumerate(edited):
        before = original[i] if i < len(original) else None
        if before is None:
            report.not_carried.append(f"{_tank_label(i)} (added on the page: the file has no record for it)")
            continue
        for attr, word in TANK_TEXTS.items():
            if _differs(getattr(tank, attr), getattr(before, attr)):
                report.not_carried.append(f"{_tank_label(i)} {word}")
        for attr, (word, where, num, scale) in TANK_SLOTS.items():
            value = getattr(tank, attr)
            if not _differs(value, getattr(before, attr)):
                continue
            label = f"{_tank_label(i)} {word}"
            records: List[_Record] = []
            reason = ""
            if where == "gas":
                if i < len(gases):
                    records = [gases[i]]
                elif gases:
                    reason = f"the file has {len(gases)} gas record{'s' if len(gases) != 1 else ''}; this tank shows the first gas"
                else:
                    reason = "the file has no gas record"
            else:
                sensor = sensors[i] if i < len(sensors) else None
                if sensor is None:
                    reason = "the file has no transmitter record for this tank"
                else:
                    records = patcher.tank_records(sensor, TANK_SUMMARY if where == "summary" else SENSOR_PROFILE)
                    if not records:
                        reason = ("the file has no tank summary for this transmitter" if where == "summary"
                                  else "the file has no transmitter profile for this tank")
            if not records:
                report.not_carried.append(f"{label} ({reason})")
                continue
            raw = None if value is None else int(round(float(value) * scale))
            problems = [patcher.write(record, num, raw) for record in records]
            if all(p is None for p in problems):
                report.patched.append(label)
            else:
                report.not_carried.append(f"{label} ({next(p for p in problems if p)})")
    for i in range(len(edited), len(original)):
        report.not_carried.append(f"{_tank_label(i)} removed (a FIT keeps its transmitter records)")


def _patch_fields(patcher: _Patcher, original: UnifiedDive, edited: UnifiedDive) -> None:
    report = patcher.report
    for attr, label in NOT_IN_A_FIT.items():
        if _differs(getattr(edited, attr), getattr(original, attr)):
            report.not_carried.append(label)
    for attr, (label, slots) in FIELD_SLOTS.items():
        value = getattr(edited, attr)
        if not _differs(value, getattr(original, attr)):
            continue
        if attr in PROFILE_TEMPERATURES and edited.samples:
            report.not_carried.append(f"{label} (the profile samples give it)")
            continue
        patcher.patch_value(label, slots, value)
    _patch_tanks(patcher, original.gas_mixtures, edited.gas_mixtures)


def _patch_times(patcher: _Patcher, original: UnifiedDive, edited: UnifiedDive) -> None:
    delta = _utc_delta(original, edited)
    old_offset, new_offset = _offset_minutes(original), _offset_minutes(edited)
    zone_changed = new_offset != old_offset or (edited.timezone or None) != (original.timezone or None)
    if not delta and not zone_changed:
        return
    offset_for_file: Optional[int] = None
    if patcher.has_local_timestamp():
        offset_for_file = new_offset if new_offset is not None else old_offset
    elif zone_changed:
        patcher.report.not_carried.append("time zone (the file has no local time field)")
    patcher.shift_times(delta, offset_for_file)
    if delta:
        sign = "+" if delta > 0 else "-"
        patcher.report.patched.append(f"start time ({sign}{abs(delta)} s on every timestamp)")
    if zone_changed and offset_for_file is not None:
        patcher.report.patched.append("time zone")


def _select_member(archive: zipfile.ZipFile, entries: Sequence[str], original: UnifiedDive) -> str:
    """Of several ``.fit`` entries, the one holding ``original`` (same UTC
    start and dive number); the first when none does."""
    for entry in entries:
        try:
            dive = read_fit(archive.read(entry), name=os.path.basename(entry))
        except Exception:
            continue
        if dive.date_time_utc == original.date_time_utc and dive.dive_number == original.dive_number:
            return entry
    return entries[0]


def fit_bytes(source: Union[bytes, bytearray, PathLike], original: Optional[UnifiedDive] = None) -> Tuple[bytes, Optional[str]]:
    """The bytes of the FIT in ``source`` (a path or bytes; a Connect zip is
    unwrapped, a zip of several gives the entry whose dive is ``original``)
    and the zip member's name (None for a bare file)."""
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
    else:
        with open(os.fspath(source), "rb") as f:
            data = f.read()
    if data[:2] != b"PK":
        return data, None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = sorted(n for n in archive.namelist() if n.lower().endswith(".fit") and not n.endswith("/"))
            if not entries:
                raise FitPatchError("the zip holds no .fit file")
            member = _select_member(archive, entries, original) if len(entries) > 1 and original is not None else entries[0]
            return archive.read(member), os.path.basename(member)
    except zipfile.BadZipFile as e:
        raise FitPatchError(f"not a FIT file or a zip of one: {e}") from e


def patch_fit(source: Union[bytes, bytearray, PathLike], dest: Optional[PathLike],
              original: UnifiedDive, edited: UnifiedDive) -> PatchReport:
    """Write ``source`` (a ``.fit`` or a Connect zip holding one, as a path
    or bytes) to ``dest`` with every field that differs between
    ``original`` (the dive as `fit_reader` read it from that file) and
    ``edited`` patched in place, the checksums recomputed and every other
    byte as it was; ``dest`` None makes the report without writing (what
    the page shows before a save). The report says what was patched, what
    the file cannot hold and the timestamp shift. `FitPatchError` for a
    source that is not a FIT; the original file is never written."""
    data, member = fit_bytes(source, original)
    patcher = _Patcher(bytearray(data))
    patcher.report.source_member = member
    _patch_times(patcher, original, edited)
    _patch_fields(patcher, original, edited)
    if dest is not None:
        patcher.report.spans += recompute_crcs(patcher.data)
        dest = os.fspath(dest)
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(bytes(patcher.data))
        logger.info("Patched %s into %s: %d field(s), shift %d s, not carried: %s", member or "fit", dest,
                    len(patcher.report.patched), patcher.report.shifted_seconds, ", ".join(patcher.report.not_carried) or "-")
    return patcher.report
