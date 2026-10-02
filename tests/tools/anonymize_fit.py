#!/usr/bin/env python3
"""Turn one of the owner's Garmin dive .fit files into a committable fixture
(plans/convert.md I2).

    python tests/tools/anonymize_fit.py <in.fit> <out.fit> [--max-bytes N] [--every N]
    python tests/tools/anonymize_fit.py --describe <fixture.fit>

The file is rewritten message by message at the byte level (no decode and
re-encode), so everything that is kept stays exactly as the watch wrote it.
The source file is only read.

What changes:
- messages: only the ones a dive reader needs are kept (KEEP_MESSAGES: file_id,
  file_creator, activity, session, lap, event, device_info, record,
  dive_settings, dive_gas, dive_summary, tank_update, tank_summary and the
  sensor profile, message 147, of each transmitter used on the dive). Every
  other message is dropped: user_profile, device_settings, sport, zones,
  alarms, battery, heart-rate and wellness messages, and every message the FIT
  profile does not name. Events of a type the profile does not name go too.
- serial numbers: every ``serial_number`` -> 1000000001, 1000000002, ...
- transmitters: every sensor / ANT channel id -> 2000000001, 2000000002, ...
  (the same real id always becomes the same placeholder, so tank_update,
  tank_summary, device_info, the sensor profile and the tank events still
  refer to each other); ``ant_device_number`` is blanked.
- positions: every field in semicircles that holds a position is moved by
  one fixed offset, chosen so the session's start position lands on 10.0 S
  30.0 W (open ocean in the mid-Atlantic, plainly made up). Distances and directions between the
  positions (entry to exit, the lap's bounding box) stay as they were, so a
  reader can still tell the exit from the entry; where on earth the dive was
  does not survive. A file without a position stays without one.
- text: transmitter names -> "Tank 1", "Tank 2"; every other text field and
  every byte-array field is blanked.
- fields the FIT profile does not name are cut out of the message (definition
  and data), except the transmitter id in device_info (field 24) and the
  sensor profile's fields in SENSOR_FIELDS. Blanked means: set to the base
  type's invalid value, which decoders skip.
- developer data fields are cut off.
- records are thinned to every Nth so the file fits ``--max-bytes`` (default
  DEFAULT_MAX_BYTES; N is the smallest up to MAX_EVERY that fits, else only the
  mandatory records stay; or ``--every``). Mandatory: the first, last,
  deepest, coldest and warmest record, and the record next to every event
  (gas switch, alert, tank event). Tank updates are thinned on the same
  interval, per transmitter: one at least every N seconds, plus each
  transmitter's first and last (its start and end pressure). The dive is
  thinned, not cut short: the summary messages (session, dive_summary,
  tank_summary) keep describing the whole dive, so the records still agree
  with them.
- header data size, header CRC and file CRC are recomputed.

Times (UTC and the local offset), the dive number, depths, temperatures,
pressures, gases, deco data, the dive settings and the watch and transmitter
model and firmware are kept as they are.

Before anything is written the output is searched for every identifier found
in the source (serials, sensor ids, texts, positions, byte arrays); a hit
aborts the run. The run ends with a "what remains" listing: every text, every
serial or id-like value and every position still in the fixture, to be read by
the owner before the file is committed. ``--describe`` lists every message
and field of a finished fixture with its values (abridged).
"""
from __future__ import annotations

import argparse
import bisect
import os
import struct
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from garmin_fit_sdk import Decoder, Profile, Stream

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
DEFAULT_MAX_BYTES = 30000
MAX_EVERY = 60
# The leak search looks for texts and byte arrays of at least this length (a
# hardware address is 6 bytes); shorter ones turn up by chance in the data.
MIN_TEXT = 4
MIN_BYTE_ARRAY = 6

PLACEHOLDER_SERIAL_BASE = 1000000001
PLACEHOLDER_SENSOR_BASE = 2000000001
PLACEHOLDER_TANK_NAME = "Tank {n}"
# Open ocean in the mid-Atlantic, far from any coast or dive site: plainly made up.
FIXED_LAT_DEG = -10.0
FIXED_LONG_DEG = -30.0

# Global message numbers.
FILE_ID, DEVICE_INFO, RECORD, EVENT, SESSION = 0, 23, 20, 21, 18
DIVE_SETTINGS, TANK_UPDATE, TANK_SUMMARY = 258, 319, 323
SENSOR_PROFILE = 147  # not in Garmin's public profile; named by libdivecomputer's garmin_parser.c
KEEP_MESSAGES = frozenset({FILE_ID, 49, 34, SESSION, 19, EVENT, DEVICE_INFO, RECORD,
                           DIVE_SETTINGS, 259, 268, TANK_UPDATE, TANK_SUMMARY, SENSOR_PROFILE})

# The sensor profile's fields (libdivecomputer's names; 91 is the name the
# diver typed, seen in Descent Mk3i files). All other fields of it are cut.
SENSOR_FIELDS: Dict[int, str] = {
    0: "ant_channel_id", 2: "name", 3: "enabled", 52: "sensor_type", 74: "pressure_units",
    75: "rated_pressure", 76: "reserve_pressure", 77: "volume", 78: "used_for_gas_rate",
    91: "typed_name", 254: "message_index",
}
SENSOR_NAME_FIELDS = (2, 91)
# Unnamed fields that hold a transmitter id: (message, field).
SENSOR_ID_FIELDS: Tuple[Tuple[int, int], ...] = (
    (TANK_UPDATE, 0), (TANK_SUMMARY, 0), (SENSOR_PROFILE, 0), (DEVICE_INFO, 24),
    (DIVE_SETTINGS, 62), (DIVE_SETTINGS, 63),
)
# Unnamed fields that are not cut (their value goes through the identifier mapping).
KEEP_UNNAMED: Set[Tuple[int, int]] = {(DEVICE_INFO, 24)}

# FIT base types by number (base type byte & 0x1F): name, struct code, size,
# invalid value. Floats are handled as raw integers, they are never interpreted.
BASE_TYPES: Dict[int, Tuple[str, str, int, int]] = {
    0: ("enum", "B", 1, 0xFF), 1: ("sint8", "b", 1, 0x7F), 2: ("uint8", "B", 1, 0xFF),
    3: ("sint16", "h", 2, 0x7FFF), 4: ("uint16", "H", 2, 0xFFFF),
    5: ("sint32", "i", 4, 0x7FFFFFFF), 6: ("uint32", "I", 4, 0xFFFFFFFF),
    7: ("string", "B", 1, 0x00), 8: ("float32", "I", 4, 0xFFFFFFFF),
    9: ("float64", "Q", 8, 0xFFFFFFFFFFFFFFFF), 10: ("uint8z", "B", 1, 0x00),
    11: ("uint16z", "H", 2, 0x0000), 12: ("uint32z", "I", 4, 0x00000000),
    13: ("byte", "B", 1, 0xFF), 14: ("sint64", "q", 8, 0x7FFFFFFFFFFFFFFF),
    15: ("uint64", "Q", 8, 0xFFFFFFFFFFFFFFFF), 16: ("uint64z", "Q", 8, 0),
}
STRING, BYTE = 7, 13
Z_TYPES = frozenset({10, 11, 12, 16})

_CRC_TABLE = (0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
              0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400)


class FitError(ValueError):
    """The input is not a FIT file this tool can rewrite."""


class LeakError(RuntimeError):
    """An identifier of the source file is still in the output."""


def fit_crc(data: bytes, crc: int = 0) -> int:
    """The FIT protocol's CRC-16 over ``data`` (continuing from ``crc``)."""
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc


def semicircles(degrees: float) -> int:
    return round(degrees * 2 ** 31 / 180.0)


def degrees(semis: int) -> float:
    return semis * 180.0 / 2 ** 31


@dataclass(frozen=True)
class FieldDef:
    num: int
    size: int
    base: int  # the base type byte as written (0x86 for uint32)

    @property
    def kind(self) -> int:
        return self.base & 0x1F


@dataclass(frozen=True)
class Definition:
    """A definition message. ``dev_size`` is the size of the developer fields
    behind the normal ones in the source's data messages; they are cut off on
    reading and never written."""
    local: int
    global_num: int
    big_endian: bool
    fields: Tuple[FieldDef, ...]
    dev_size: int = 0

    @property
    def size(self) -> int:
        return sum(f.size for f in self.fields)

    def encode(self) -> bytes:
        out = bytearray([0x40 | self.local, 0, 1 if self.big_endian else 0])
        out += struct.pack(">H" if self.big_endian else "<H", self.global_num)
        out.append(len(self.fields))
        for f in self.fields:
            out += bytes((f.num, f.size, f.base))
        return bytes(out)

    def locate(self, num: int) -> Optional[Tuple[int, FieldDef]]:
        offset = 0
        for f in self.fields:
            if f.num == num:
                return offset, f
            offset += f.size
        return None


@dataclass(eq=False)
class Message:
    """A data message: its definition and its bytes (developer fields cut)."""
    definition: Definition
    payload: bytearray

    @property
    def global_num(self) -> int:
        return self.definition.global_num

    def _code(self, f: FieldDef) -> Optional[str]:
        base = BASE_TYPES.get(f.kind)
        if base is None or f.size % base[2]:
            return None  # unknown base type or a size that does not fit it: raw bytes
        return (">" if self.definition.big_endian else "<") + base[1] * (f.size // base[2])

    def get(self, num: int) -> Optional[List[int]]:
        """The field's raw values (one per array element; a string's bytes),
        or None when the message has no such field."""
        found = self.definition.locate(num)
        if found is None:
            return None
        offset, f = found
        code = self._code(f)
        raw = self.payload[offset:offset + f.size]
        return list(struct.unpack(code, raw)) if code else list(raw)

    def value(self, num: int) -> Optional[int]:
        """The field's first value, or None when absent or invalid."""
        values = self.get(num)
        if not values:
            return None
        found = self.definition.locate(num)
        base = BASE_TYPES.get(found[1].kind) if found else None
        if base is not None and values[0] == base[3]:
            return None
        return values[0]

    def text(self, num: int) -> Optional[str]:
        values = self.get(num)
        if values is None:
            return None
        return bytes(values).split(b"\x00", 1)[0].decode("utf-8", errors="replace")

    def set(self, num: int, values: Sequence[int]) -> None:
        offset, f = self.definition.locate(num)  # type: ignore[misc]
        code = self._code(f)
        raw = struct.pack(code, *values) if code else bytes(values)
        self.payload[offset:offset + f.size] = raw

    def set_text(self, num: int, text: str) -> None:
        """Write ``text`` into a string field, cut to fit and NUL-padded."""
        _offset, f = self.definition.locate(num)  # type: ignore[misc]
        raw = text.encode("utf-8")[:max(f.size - 1, 0)]
        self.set(num, list(raw.ljust(f.size, b"\x00")))

    def blank(self, num: int) -> None:
        """Overwrite the field with its base type's invalid value."""
        found = self.definition.locate(num)
        if found is None:
            return
        _offset, f = found
        base = BASE_TYPES.get(f.kind)
        if base is None or f.size % base[2]:
            self.set(num, [0xFF] * f.size)
        else:
            self.set(num, [base[3]] * (f.size // base[2]))

    def cut(self, nums: Set[int]) -> None:
        """Remove the fields ``nums`` from the message: it gets a definition
        without them and its bytes lose theirs."""
        d = self.definition
        payload, offset = bytearray(), 0
        for f in d.fields:
            if f.num not in nums:
                payload += self.payload[offset:offset + f.size]
            offset += f.size
        self.definition = Definition(d.local, d.global_num, d.big_endian,
                                     tuple(f for f in d.fields if f.num not in nums))
        self.payload = payload

    def is_blank(self, num: int) -> bool:
        found = self.definition.locate(num)
        values = self.get(num)
        if found is None or values is None:
            return True
        base = BASE_TYPES.get(found[1].kind)
        invalid = base[3] if base is not None and not found[1].size % base[2] else 0xFF
        return all(v == invalid for v in values)


def parse_fit(raw: bytes) -> Tuple[bytes, List[Message]]:
    """Split a FIT file into its header bytes and its data messages, each with
    the definition that was in force. Refuses chained files, a wrong CRC and
    compressed-timestamp headers (a dive file from the watch has none)."""
    if len(raw) < 14 or raw[0] not in (12, 14) or raw[8:12] != b".FIT":
        raise FitError("not a FIT file")
    header_size = raw[0]
    data_size = struct.unpack_from("<I", raw, 4)[0]
    if header_size + data_size + 2 != len(raw):
        raise FitError("file size does not match the header (chained or truncated file)")
    if fit_crc(raw[:-2]) != struct.unpack_from("<H", raw, len(raw) - 2)[0]:
        raise FitError("file CRC does not match")
    pos, end = header_size, header_size + data_size
    definitions: Dict[int, Definition] = {}
    messages: List[Message] = []
    while pos < end:
        head = raw[pos]
        pos += 1
        if head & 0x80:
            raise FitError("compressed timestamp headers are not supported")
        local = head & 0x0F
        if head & 0x40:
            big = raw[pos + 1] == 1
            global_num = struct.unpack_from(">H" if big else "<H", raw, pos + 2)[0]
            count = raw[pos + 4]
            pos += 5
            fields = tuple(FieldDef(raw[pos + 3 * i], raw[pos + 3 * i + 1], raw[pos + 3 * i + 2])
                           for i in range(count))
            pos += 3 * count
            dev_size = 0
            if head & 0x20:
                dev_count = raw[pos]
                dev_size = sum(raw[pos + 2 + 3 * i] for i in range(dev_count))
                pos += 1 + 3 * dev_count
            definitions[local] = Definition(local, global_num, big, fields, dev_size)
        else:
            definition = definitions.get(local)
            if definition is None:
                raise FitError(f"data message for local type {local} before its definition")
            messages.append(Message(definition, bytearray(raw[pos:pos + definition.size])))
            pos += definition.size + definition.dev_size
    if pos != end:
        raise FitError("messages run past the data size")
    return raw[:header_size], messages


def build_fit(header: bytes, messages: Sequence[Message]) -> bytes:
    """Write the messages behind ``header``; a definition is written whenever
    the one in force for that local type differs. Data size and both CRCs are
    recomputed."""
    body = bytearray()
    in_force: Dict[int, Tuple[int, bool, Tuple[FieldDef, ...]]] = {}
    for m in messages:
        d = m.definition
        key = (d.global_num, d.big_endian, d.fields)
        if in_force.get(d.local) != key:
            body += d.encode()
            in_force[d.local] = key
        body.append(d.local)
        body += m.payload
    head = bytearray(header)
    struct.pack_into("<I", head, 4, len(body))
    if len(head) >= 14:
        struct.pack_into("<H", head, 12, fit_crc(bytes(head[:12])))
    out = bytes(head) + bytes(body)
    return out + struct.pack("<H", fit_crc(out))


# --- the FIT profile: names and units --------------------------------------

def message_name(global_num: int) -> str:
    if global_num == SENSOR_PROFILE:
        return "sensor_profile(147)"
    info = Profile["messages"].get(global_num)
    return info["name"] if info else f"mesg_{global_num}"


def _profile_field(global_num: int, num: int) -> Optional[Dict[str, Any]]:
    info = Profile["messages"].get(global_num)
    return info["fields"].get(num) if info else None


def field_name(global_num: int, num: int) -> Optional[str]:
    """The field's name in the FIT profile (or SENSOR_FIELDS), None when unnamed."""
    if global_num == SENSOR_PROFILE:
        return SENSOR_FIELDS.get(num)
    info = _profile_field(global_num, num)
    return info["name"] if info else None


def field_num(global_num: int, name: str) -> Optional[int]:
    info = Profile["messages"].get(global_num)
    for num, f in (info["fields"].items() if info else ()):
        if f["name"] == name:
            return num
    return None


def is_position(global_num: int, num: int) -> bool:
    info = _profile_field(global_num, num)
    return bool(info) and info.get("units") == "semicircles"


def _named_events() -> Set[int]:
    return {int(k) for k in Profile["types"]["event"]}


def _event_name(number: Optional[int]) -> str:
    types = Profile["types"]["event"]
    return str(types.get(number, types.get(str(number), f"event_{number}")))


# --- thinning ---------------------------------------------------------------

def records_to_keep(times: Sequence[int], every: int, anchors: Iterable[int] = (),
                    always: Iterable[int] = ()) -> List[int]:
    """Which records stay, as sorted indexes into ``times`` (the records'
    timestamps, in file order, non-decreasing).

    Kept: every ``every``-th record, the first and the last, each index in
    ``always`` (the deepest, the coldest, ...), and for each anchor time (an
    event) the record of that second, or else the nearest
    one (the earlier of two equally near)."""
    count = len(times)
    if not count:
        return []
    keep = set(range(0, count, max(1, every)))
    keep.update((0, count - 1))
    keep.update(i for i in always if 0 <= i < count)
    for anchor in anchors:
        right = bisect.bisect_left(times, anchor)
        if right < count and times[right] == anchor:
            keep.add(right)
        elif right == 0:
            keep.add(0)
        elif right == count:
            keep.add(count - 1)
        else:
            left = right - 1
            keep.add(left if anchor - times[left] <= times[right] - anchor else right)
    return sorted(keep)


def tank_updates_to_keep(updates: Sequence[Tuple[int, int]], interval: int) -> List[int]:
    """Which tank updates stay, as sorted indexes into ``updates`` (each a
    ``(sensor, timestamp)`` pair, in file order).

    Per sensor: the first and the last update, and every update at least
    ``interval`` seconds after the last one kept for that sensor."""
    by_sensor: Dict[int, List[int]] = {}
    for i, (sensor, _time) in enumerate(updates):
        by_sensor.setdefault(sensor, []).append(i)
    keep: Set[int] = set()
    step = max(1, interval)
    for indexes in by_sensor.values():
        last = None
        for i in indexes:
            time = updates[i][1]
            if last is None or time - last >= step:
                keep.add(i)
                last = time
        keep.add(indexes[-1])
    return sorted(keep)


# --- anonymising --------------------------------------------------------------

@dataclass
class Identifiers:
    """What the source file holds that must not reach the output."""
    serials: List[int] = field(default_factory=list)
    sensors: List[int] = field(default_factory=list)
    texts: List[str] = field(default_factory=list)
    positions: List[int] = field(default_factory=list)
    byte_arrays: List[bytes] = field(default_factory=list)
    # The (lat, long) the offset is measured from: the session's start
    # position, else the first latitude and longitude in the file.
    origin: Tuple[Optional[int], Optional[int]] = (None, None)
    session_axes: Set[int] = field(default_factory=set)

    def moved(self, value: int, is_long: bool) -> int:
        """``value`` (semicircles) moved by the fixed offset of its axis."""
        base = self.origin[1 if is_long else 0]
        target = semicircles(FIXED_LONG_DEG if is_long else FIXED_LAT_DEG)
        moved = value + (target - base if base is not None else 0)
        limit = 2 ** 31 - 2
        return max(-limit, min(limit, moved))

    def serial_placeholder(self, value: int) -> int:
        return PLACEHOLDER_SERIAL_BASE + self.serials.index(value)

    def sensor_placeholder(self, value: int) -> int:
        return PLACEHOLDER_SENSOR_BASE + self.sensors.index(value)

    def placeholder(self, value: int) -> Optional[int]:
        """The placeholder for a 32-bit value that is a known identifier."""
        if value in self.sensors:
            return self.sensor_placeholder(value)
        if value in self.serials:
            return self.serial_placeholder(value)
        return None


def _add(items: List[Any], value: Any) -> None:
    if value not in items:
        items.append(value)


def collect_identifiers(messages: Sequence[Message]) -> Identifiers:
    """Every serial, sensor id, text, position and byte array in the source,
    from the messages that will be dropped too (order of first appearance)."""
    ids = Identifiers()
    for m in messages:
        g = m.global_num
        for f in m.definition.fields:
            if field_name(g, f.num) == "serial_number":
                value = m.value(f.num)
                if value:
                    _add(ids.serials, value)
            if (g, f.num) in SENSOR_ID_FIELDS:
                value = m.value(f.num)
                if value:
                    _add(ids.sensors, value)
            if f.kind == STRING:
                text = m.text(f.num)
                if text:
                    _add(ids.texts, text)
            elif f.kind == BYTE and f.size >= MIN_BYTE_ARRAY and not m.is_blank(f.num):
                _add(ids.byte_arrays, bytes(m.get(f.num) or []))
            elif is_position(g, f.num):
                value = m.value(f.num)
                if value is not None:
                    _add(ids.positions, value)
                    _note_origin(ids, g, str(field_name(g, f.num)), value)
    return ids


def _is_long(name: Optional[str]) -> bool:
    return str(name).endswith("long")


def _note_origin(ids: Identifiers, global_num: int, name: str, value: int) -> None:
    """Keep the first latitude and longitude seen; the session's start
    position replaces them (it is the dive's entry point)."""
    axis = 1 if _is_long(name) else 0
    origin = list(ids.origin)
    if global_num == SESSION and name.startswith("start_position"):
        origin[axis] = value
        ids.session_axes.add(axis)
    elif origin[axis] is None and axis not in ids.session_axes:
        origin[axis] = value
    ids.origin = (origin[0], origin[1])


@dataclass
class Report:
    source_size: int = 0
    output_size: int = 0
    max_bytes: int = DEFAULT_MAX_BYTES
    every: int = 1
    records_before: int = 0
    records_after: int = 0
    tank_updates_before: int = 0
    tank_updates_after: int = 0
    kept: Counter = field(default_factory=Counter)
    dropped: Counter = field(default_factory=Counter)
    changed: Counter = field(default_factory=Counter)
    cut_unnamed: Counter = field(default_factory=Counter)
    developer_fields_cut: int = 0
    counts: Dict[str, int] = field(default_factory=dict)

    def lines(self) -> List[str]:
        out = [f"size: {self.source_size} -> {self.output_size} bytes"
               + ("" if self.output_size <= self.max_bytes else f"  (over the {self.max_bytes} byte target)"),
               f"records: {self.records_before} -> {self.records_after} ("
               + (f"every {self.every}. record, plus" if self.every < max(self.records_before, 2) else "only")
               + " the first, last, deepest, coldest, warmest and the ones next to events)",
               f"tank updates: {self.tank_updates_before} -> {self.tank_updates_after}"
               " (one every N seconds per transmitter, plus each one's first and last)",
               "identifiers found in the source: " + ", ".join(f"{v} {k}" for k, v in self.counts.items())]
        out.append("messages kept: " + ", ".join(f"{k} x{v}" for k, v in sorted(self.kept.items())))
        out.append("messages dropped: " + (", ".join(f"{k} x{v}" for k, v in sorted(self.dropped.items())) or "none"))
        out.append("values changed: " + (", ".join(f"{k} x{v}" for k, v in sorted(self.changed.items())) or "none"))
        cut: Dict[str, List[int]] = {}
        for (name, num), _count in sorted(self.cut_unnamed.items()):
            cut.setdefault(name, []).append(num)
        out.append("unnamed fields cut: " + ("; ".join(
            f"{name} {', '.join(str(n) for n in nums)}" for name, nums in cut.items()) or "none"))
        if self.developer_fields_cut:
            out.append(f"developer fields cut from {self.developer_fields_cut} message(s)")
        return out


def _keep_message(m: Message, used_sensors: Set[int], named_events: Set[int]) -> bool:
    g = m.global_num
    if g not in KEEP_MESSAGES:
        return False
    if g == SENSOR_PROFILE:
        return m.value(0) in used_sensors
    if g == EVENT:
        return m.value(field_num(EVENT, "event") or 0) in named_events
    return True


def scrub_message(m: Message, ids: Identifiers, report: Report) -> None:
    """Apply the rules in the module docstring to one kept message, in place."""
    g = m.global_num
    name = message_name(g)
    unnamed = {f.num for f in m.definition.fields
               if field_name(g, f.num) is None and (g, f.num) not in KEEP_UNNAMED}
    if unnamed:
        for num in unnamed:
            report.cut_unnamed[(name, num)] += 1
        m.cut(unnamed)
    sensor = m.value(0) if g == SENSOR_PROFILE else None  # read before it is replaced below
    for f in m.definition.fields:
        fname = field_name(g, f.num)
        if f.kind == STRING:
            if not m.text(f.num):
                continue
            if g == SENSOR_PROFILE and f.num in SENSOR_NAME_FIELDS:
                n = ids.sensors.index(sensor) + 1 if sensor in ids.sensors else 0
                m.set_text(f.num, PLACEHOLDER_TANK_NAME.format(n=n))
                report.changed["transmitter name"] += 1
            else:
                m.blank(f.num)
                report.changed["text blanked"] += 1
        elif f.kind == BYTE:
            if not m.is_blank(f.num):
                m.blank(f.num)
                report.changed["byte array blanked"] += 1
        elif fname == "ant_device_number":
            if not m.is_blank(f.num):
                m.blank(f.num)
                report.changed["ANT device number blanked"] += 1
        elif is_position(g, f.num):
            value = m.value(f.num)
            if value is not None:
                m.set(f.num, [ids.moved(value, _is_long(fname))])
                report.changed["position"] += 1
        elif f.size == 4 and f.kind in (5, 6, 12):
            value = m.value(f.num)
            if value is None:
                continue
            if fname == "serial_number":
                m.set(f.num, [ids.serial_placeholder(value)])
                report.changed["serial number"] += 1
            else:
                placeholder = ids.placeholder(value)
                if placeholder is not None:
                    m.set(f.num, [placeholder])
                    report.changed["sensor id" if value in ids.sensors else "serial number"] += 1


def _needles(ids: Identifiers) -> List[Tuple[str, bytes]]:
    """Byte patterns of the source's identifiers, to search the output for."""
    allowed = {PLACEHOLDER_TANK_NAME.format(n=n) for n in range(len(ids.sensors) + 1)}
    needles: List[Tuple[str, bytes]] = []
    for label, values in (("serial number", ids.serials), ("sensor id", ids.sensors)):
        for value in values:
            needles.append((label, struct.pack("<I", value)))
            needles.append((label, struct.pack(">I", value)))
    for value in ids.positions:
        if abs(value) > 0xFFFF and value not in (semicircles(FIXED_LAT_DEG), semicircles(FIXED_LONG_DEG)):
            needles.append(("position", struct.pack("<i", value)))
            needles.append(("position", struct.pack(">i", value)))
    for text in ids.texts:
        if len(text) >= MIN_TEXT and text not in allowed:
            needles.append(("text", text.encode("utf-8")))
    for raw in ids.byte_arrays:
        if len(set(raw)) > 1:
            needles.append(("byte array", raw))
    return needles


def find_leaks(output: bytes, ids: Identifiers) -> List[str]:
    """The kinds of source identifier still present in ``output`` (with the
    offset; never the value itself)."""
    return [f"{label} at byte {output.find(needle)}"
            for label, needle in _needles(ids) if needle in output]


def check_decodes(data: bytes) -> List[str]:
    """Problems garmin-fit-sdk (and fitdecode, when installed) have with ``data``."""
    problems: List[str] = []
    stream = Stream.from_byte_array(bytearray(data))
    decoder = Decoder(stream)
    if not decoder.check_integrity():
        problems.append("garmin-fit-sdk: integrity check failed")
    _messages, errors = Decoder(Stream.from_byte_array(bytearray(data))).read()
    problems.extend(f"garmin-fit-sdk: {e}" for e in errors)
    try:
        import io

        import fitdecode
    except ImportError:
        return problems
    try:
        with fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.RAISE) as reader:
            for _frame in reader:
                pass
    except Exception as e:  # fitdecode raises its own error types
        problems.append(f"fitdecode: {e}")
    return problems


def anonymize(raw: bytes, max_bytes: int = DEFAULT_MAX_BYTES, every: Optional[int] = None) -> Tuple[bytes, Report]:
    """The anonymised, thinned file and a report of what was done. Raises
    FitError for an input it cannot rewrite and LeakError when an identifier
    of the source survives."""
    header, messages = parse_fit(raw)
    ids = collect_identifiers(messages)
    report = Report(source_size=len(raw), max_bytes=max_bytes)
    report.counts = {"serial number(s)": len(ids.serials), "sensor id(s)": len(ids.sensors),
                     "text(s)": len(ids.texts), "position value(s)": len(ids.positions),
                     "byte array(s)": len(ids.byte_arrays)}
    report.developer_fields_cut = sum(1 for m in messages if m.definition.dev_size)

    used_sensors = {m.value(0) for m in messages if m.global_num in (TANK_UPDATE, TANK_SUMMARY)}
    named_events = _named_events()
    kept: List[Message] = []
    for m in messages:
        if _keep_message(m, used_sensors, named_events):
            kept.append(m)
            scrub_message(m, ids, report)
        else:
            label = message_name(m.global_num)
            if m.global_num == EVENT:
                label = f"event({_event_name(m.value(field_num(EVENT, 'event') or 0))})"
            report.dropped[label] += 1

    records = [m for m in kept if m.global_num == RECORD]
    report.records_before = len(records)
    times = [m.value(253) or 0 for m in records]
    anchors = [m.value(253) or 0 for m in kept if m.global_num == EVENT]
    always = _extreme_records(records)
    updates = [m for m in kept if m.global_num == TANK_UPDATE]
    report.tank_updates_before = len(updates)
    update_keys = [(m.value(0) or 0, m.value(253) or 0) for m in updates]

    def build(n: int) -> Tuple[bytes, int]:
        keep = {id(records[i]) for i in records_to_keep(times, n, anchors, always)}
        keep_updates = {id(updates[i]) for i in tank_updates_to_keep(update_keys, n)}
        chosen = [m for m in kept
                  if (m.global_num != RECORD or id(m) in keep)
                  and (m.global_num != TANK_UPDATE or id(m) in keep_updates)]
        report.tank_updates_after = len(keep_updates)
        return build_fit(header, chosen), len(keep)

    if every is not None:
        n = max(1, every)
        output, left = build(n)
    else:
        n = 1
        output, left = build(n)
        while len(output) > max_bytes and n < MAX_EVERY:
            n += 1
            output, left = build(n)
        if len(output) > max_bytes:
            n = max(len(records), 1)
            output, left = build(n)
    report.every, report.records_after, report.output_size = n, left, len(output)

    _header, written = parse_fit(output)
    report.kept = Counter(message_name(m.global_num) for m in written)
    leaks = find_leaks(output, ids)
    if leaks:
        raise LeakError("source identifiers still in the output: " + "; ".join(leaks))
    problems = check_decodes(output)
    if problems:
        raise FitError("the output does not decode cleanly: " + "; ".join(problems))
    return output, report


def _extreme_records(records: Sequence[Message]) -> List[int]:
    """Indexes of the deepest, the coldest and the warmest record (the first of each)."""
    out: List[int] = []
    for name, pick in (("depth", max), ("temperature", min), ("temperature", max)):
        num = field_num(RECORD, name)
        values = [(m.value(num), -i) for i, m in enumerate(records)] if num is not None else []
        values = [v for v in values if v[0] is not None]
        if values:
            # the first record holding the extreme value: for max the largest -i, for min the smallest value then largest -i
            best = pick(v[0] for v in values)
            out.append(next(-i for value, i in values if value == best))
    return out


# --- what remains -------------------------------------------------------------

def _fit_time(value: Optional[int]) -> str:
    return "-" if value is None else (FIT_EPOCH + timedelta(seconds=value)).strftime("%Y-%m-%d %H:%M:%S")


def remaining(data: bytes) -> List[str]:
    """Every text, every serial or id-like value and every position in a FIT
    file, plus its times and device model: what the owner reads before a
    fixture is committed."""
    _header, messages = parse_fit(data)
    texts: Counter = Counter()
    id_like: Counter = Counter()
    unnamed: Counter = Counter()
    positions: Counter = Counter()
    models: Counter = Counter()
    counts: Counter = Counter()
    event_field = field_num(EVENT, "event") or 0
    data_field = field_num(EVENT, "data") or 3
    for m in messages:
        g = m.global_num
        name = message_name(g)
        counts[name] += 1
        for f in m.definition.fields:
            fname = field_name(g, f.num)
            label = f"{name}.{fname or f.num}"
            if f.kind == STRING:
                if m.text(f.num):
                    texts[f"{label} = {m.text(f.num)!r}"] += 1
                continue
            if m.is_blank(f.num):
                continue
            values = m.get(f.num) or []
            shown = values[0] if len(values) == 1 else values
            if fname is None:
                unnamed[f"{label} = {shown}"] += 1
            elif is_position(g, f.num):
                positions[f"{label} = {degrees(values[0]):.6f}"] += 1
            elif (f.kind in Z_TYPES or f.kind == BYTE or "serial" in fname or fname == "sensor"
                  or fname.endswith("_id") or fname == "ant_device_number"):
                id_like[f"{label} = {shown}"] += 1
            elif fname in ("manufacturer", "product", "software_version", "hardware_version"):
                models[f"{label} = {shown}"] += 1
            elif g == EVENT and f.num == data_field and _event_name(m.value(event_field)).startswith("tank_"):
                id_like[f"{name}({_event_name(m.value(event_field))}).data = {shown}"] += 1

    def section(title: str, items: Counter) -> List[str]:
        body = [f"  {k}  x{v}" for k, v in sorted(items.items())] or ["  (none)"]
        return [title] + body

    out = section("messages:", Counter({f"{k}": v for k, v in counts.items()}))
    out += section("texts:", texts)
    out += section("serial numbers and ids:", id_like)
    out += section("unnamed fields that still hold a value:", unnamed)
    out += section("positions (degrees):", positions)
    out += section("device model and versions (the product, not the owner's unit):", models)

    out.append("times and counters (kept as in the source):")
    activity = next((m for m in messages if m.global_num == 34), None)
    if activity is not None:
        utc, local = activity.value(253), activity.value(field_num(34, "local_timestamp") or 5)
        offset = "-" if utc is None or local is None else f"{(local - utc) / 3600:+.2f} h"
        out.append(f"  activity.timestamp = {_fit_time(utc)} UTC, local_timestamp = {_fit_time(local)} (offset {offset})")
    file_id = next((m for m in messages if m.global_num == FILE_ID), None)
    if file_id is not None:
        out.append(f"  file_id.time_created = {_fit_time(file_id.value(field_num(FILE_ID, 'time_created') or 4))} UTC")
    records = [m for m in messages if m.global_num == RECORD]
    if records:
        out.append(f"  records: {len(records)}, {_fit_time(records[0].value(253))} to "
                   f"{_fit_time(records[-1].value(253))} UTC")
    for m in messages:
        if m.global_num == 268:
            number = m.value(field_num(268, "dive_number") or 2)
            if number is not None:
                out.append(f"  dive_summary.dive_number = {number}")
    return out


def _short(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_short(v) for v in value) + "]"
    return str(value)


def describe(data: bytes, full_up_to: int = 3, distinct_up_to: int = 6) -> List[str]:
    """Every message type and every field in a FIT file with its values, as
    garmin-fit-sdk decodes them (unnamed fields show as numbers).

    A message type with at most ``full_up_to`` messages is listed message by
    message; a longer one field by field, with the number of messages holding
    the field and its range (numbers and times) or its distinct values (up to
    ``distinct_up_to`` of them)."""
    messages, errors = Decoder(Stream.from_byte_array(bytearray(data))).read(
        convert_datetimes_to_dates=True, merge_heart_rates=False)
    out: List[str] = [f"decode errors: {len(errors)}"]
    for key, items in messages.items():
        name = key[:-6] if key.endswith("_mesgs") else f"mesg_{key}"
        out.append(f"{name} x{len(items)}")
        if len(items) <= full_up_to:
            for i, item in enumerate(items):
                fields = ", ".join(f"{k}={_short(v)}" for k, v in item.items())
                out.append(f"  [{i}] {fields}")
            continue
        names: List[Any] = []
        for item in items:
            names.extend(k for k in item if k not in names)
        for fname in names:
            values = [item[fname] for item in items if fname in item and item[fname] is not None]
            if not values:
                continue
            plain = [v for v in values if not isinstance(v, (list, tuple))]
            distinct = []
            for v in plain:
                if v not in distinct:
                    distinct.append(v)
            numeric = plain and all(isinstance(v, (int, float, datetime)) and not isinstance(v, bool) for v in plain)
            if numeric and len(distinct) > distinct_up_to:
                shown = f"{_short(min(plain))} .. {_short(max(plain))} ({len(distinct)} distinct)"
            elif len(distinct) > distinct_up_to:
                shown = ", ".join(_short(v) for v in distinct[:distinct_up_to]) + f", ... ({len(distinct)} distinct)"
            elif distinct:
                shown = ", ".join(_short(v) for v in distinct)
            else:
                shown = _short(values[0]) + (" ..." if len(values) > 1 else "")
            out.append(f"  {fname}: {shown}  [{len(values)} of {len(items)}]")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Anonymise and thin a Garmin dive .fit into a test fixture.")
    parser.add_argument("source")
    parser.add_argument("output", nargs="?")
    parser.add_argument("--describe", action="store_true",
                        help="only list every message and field of SOURCE (e.g. a finished fixture); writes nothing")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                        help="thin the records until the file is at most this big (default %(default)s)")
    parser.add_argument("--every", type=int, default=None, help="keep every Nth record instead of fitting --max-bytes")
    args = parser.parse_args(argv)
    if args.describe:
        with open(args.source, "rb") as f:
            for line in describe(f.read()):
                print(line)
        return 0
    if not args.output:
        parser.error("the output file is required unless --describe is given")
    if os.path.abspath(args.source) == os.path.abspath(args.output):
        print("the output must not be the source file")
        return 2
    with open(args.source, "rb") as f:
        raw = f.read()
    try:
        output, report = anonymize(raw, max_bytes=args.max_bytes, every=args.every)
    except (FitError, LeakError) as e:
        print(f"error: {e}")
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "wb") as f:
        f.write(output)
    print(f"wrote {args.output}")
    for line in report.lines():
        print(line)
    print()
    print("What remains in the fixture - read this before committing it:")
    for line in remaining(output):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
