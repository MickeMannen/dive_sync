"""Join two or more Garmin ``.fit`` dive logs of one dive the computer
split (a short surface stop ended one log and started the next) into one
``.fit`` (plans/convert.md I16; the Dive edit page's Merge of I17).

A port of UWMedia's ``parsers/fit_merge.py`` (MIT, same owner; Log
Viewer's "Merge dives"), on the record walker and checksum of
`fit_patch` (itself the port of UWMedia's ``fit_time.py``). The two files
are never decoded and encoded again: every message is copied as the bytes
it is in its file, so the result stays as close to a native Descent file
as it can be. Nothing of Garmin's SDK is involved - UWMedia looked the
message and field numbers up in ``garmin_fit_sdk.Profile``; here they are
constants, checked against ``fitdecode``'s profile by the tests.

The merged file is the first log with the later logs' samples appended.
Every message of the first file is copied byte for byte (``file_id``,
``device_info``, settings, GPS, heart rate, Garmin's undocumented messages
included). From the second file come its ``record``, ``event``,
``tank_update``, ``gps_metadata``, ``hr`` and ``hrv`` messages and whatever
else it logged between its first and last sample; its gases, added to the
first file's ``dive_gas`` list when they are new (the gas-switch events
re-pointed at the merged list); its tank pods (``device_info``) and
``tank_summary`` messages for transmitters the first file has none for.
Its ``file_id``, settings and summary messages are dropped and the first
file's summaries are patched instead: ``session`` / ``lap`` / ``activity``
timer times, the ``session`` end position, ``dive_summary`` depths, bottom
time, O2 toxicity and end CNS / N2, ``tank_summary`` end pressures and
volume used. The timer stop at the end of the first log and the timer
start of the second are dropped so the join does not read as a pause.

Two ways to deal with the surface interval between the logs:

- keep the clock (``close_gap=False``): the later samples keep their real
  timestamps and the gap is filled with copies of the first log's last
  sample, one per logging interval (photos and videos still line up);
- close the gap (``close_gap=True``): every timestamp of the second log
  moves back so its first sample follows the first log's last one.

Three or more files are joined pairwise, in start order, into an
accumulator (file 1 + file 2, then that + file 3, ...), so the result is
one dive that starts when the first did and ends when the last did.

Garmin Connect identifies a dive by its start and the device's serial, so
it sees the merged file as the first dive: the original activities have to
be deleted there before the merged one is uploaded.
"""
from __future__ import annotations

import logging
import os
import struct
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from src.core.convert.fit_patch import (
    ACTIVITY,
    BASE_TYPES,
    DIVE_GAS,
    DIVE_SETTINGS,
    DIVE_SUMMARY,
    FILE_ID,
    FIT_EPOCH,
    LAP,
    MIN_DATE_TIME,
    RECORD,
    EVENT,
    SESSION,
    TANK_SUMMARY,
    TANK_UPDATE,
    TIMESTAMP_FIELD,
    FitPatchError,
    PathLike,
    _Definition,
    _walk,
    fit_bytes,
    recompute_crcs,
)
from src.core.convert.fit_reader import read_fit
from src.core.models import UnifiedDive

logger = logging.getLogger("dive_sync.convert.fit_merge")

# Dives further apart than this are two dives, not one that was split.
MAX_GAP_SECONDS = 2 * 3600

# Global message numbers beyond fit_patch's (Garmin's FIT profile; fitdecode.profile names them).
DEVICE_SETTINGS, USER_PROFILE, ZONES_TARGET, SPORT, TRAINING_SETTINGS = 2, 3, 7, 12, 13
DEVICE_INFO, FILE_CREATOR, HRV, HR, GPS_METADATA = 23, 49, 78, 132, 160
TIMESTAMP_CORRELATION, FIELD_DESCRIPTION, DEVELOPER_DATA_ID, TIME_IN_ZONE, DIVE_ALARM = 162, 206, 207, 216, 262

# Second-file messages that describe the file or the dive as a whole: the first file's are kept instead.
SKIPPED_FROM_SECOND = frozenset({
    FILE_ID, FILE_CREATOR, ACTIVITY, SESSION, LAP, DIVE_SUMMARY, TIME_IN_ZONE, TIMESTAMP_CORRELATION,
    DEVICE_SETTINGS, USER_PROFILE, SPORT, TRAINING_SETTINGS, ZONES_TARGET, DIVE_SETTINGS, DIVE_ALARM,
})
# Second-file messages that are samples, wherever they sit in the file. Anything else unknown is
# kept only when logged between its first and last record, so Garmin's undocumented per-sample
# messages come along while its settings-like ones stay behind.
STREAM_FROM_SECOND = frozenset({RECORD, EVENT, TANK_UPDATE, GPS_METADATA, HR, HRV})

# Field numbers.
MESSAGE_INDEX = 254
F_FILE_TYPE, F_FILE_SERIAL = 0, 3                       # file_id.type, file_id.serial_number
FILE_TYPE_ACTIVITY = 4
F_SPORT, F_SUB_SPORT = 5, 6                             # session.sport, session.sub_sport
SPORT_DIVING = 53
F_EVENT, F_EVENT_TYPE, F_EVENT_DATA = 0, 1, 3           # event.event, event.event_type, event.data
EVENT_TIMER, EVENT_GAS_SWITCHED = 0, 57
EVENT_TYPE_START, EVENT_TYPE_STOP, EVENT_TYPE_MARKER, EVENT_TYPE_STOP_ALL = 0, 1, 3, 4
EVENT_TYPE_STOPS = frozenset({EVENT_TYPE_STOP, EVENT_TYPE_STOP_ALL})
F_GAS_HE, F_GAS_O2, F_GAS_MODE = 0, 1, 3                # dive_gas.helium_content, oxygen_content, mode
F_SERIAL = 3                                            # device_info.serial_number
F_SENSOR, F_END_PRESSURE, F_VOLUME_USED = 0, 2, 3       # tank_summary.sensor, end_pressure, volume_used
F_DEPTH = 92                                            # record.depth
F_START_TIME, F_ELAPSED, F_TIMER = 2, 7, 8              # session.start_time, total_elapsed_time, total_timer_time
F_END_LAT, F_END_LONG = 38, 39                          # session.end_position_lat, end_position_long
F_ACTIVITY_TIMER = 0                                    # activity.total_timer_time
F_AVG_DEPTH, F_MAX_DEPTH, F_BOTTOM_TIME = 2, 3, 11      # dive_summary
F_END_CNS, F_END_N2, F_O2_TOX = 6, 8, 9                 # dive_summary
F_GPS_UTC = 6                                           # gps_metadata.utc_timestamp
# Other date_time fields of the second file that move with its timestamps.
DATE_TIME_FIELDS = frozenset({(GPS_METADATA, F_GPS_UTC)})

# The integer base types, by the low five bits of the base type byte (the
# high bit only says the type is wider than a byte): struct code, size, "not set".
_INT_SPECS: Dict[int, Tuple[str, int, int]] = {
    base & 0x1F: (code, size, invalid) for base, (code, size, invalid) in BASE_TYPES.items()
    if invalid is not None and code != "s" and base != 0x0D
}


class MergeError(ValueError):
    """The files cannot be merged; the message says why, for the page."""


@dataclass
class MergeResult:
    """What `merge_fits` did: where it wrote, how many logs went in, the
    surface intervals between them as logged (``gap_seconds``, from each
    log's last sample to the next one's first, summed), the seconds taken
    out of the timeline (``closed_seconds``: the gaps less one logging
    interval each, so the kept-clock span minus this is the closed span; 0
    when the clock is kept), the samples added to bridge the gaps
    (``filled_samples``, 0 when they were closed), the gases and tank
    summaries the later logs brought that the first had not, and warnings
    worth showing (a differing logging interval, a gas switch recorded at
    a join)."""
    path: str
    files: int
    gap_seconds: int = 0
    closed_seconds: int = 0
    filled_samples: int = 0
    gases_added: int = 0
    tanks_added: int = 0
    warnings: List[str] = field(default_factory=list)


def _definition(mesg_num: int, fields: List[Tuple[int, int, int]]) -> _Definition:
    """A little-endian definition message for ``fields`` ((number, size, base type))."""
    raw = bytes([0x40, 0, 0]) + struct.pack("<H", mesg_num) + bytes([len(fields)])
    for num, size, base in fields:
        raw += bytes([num, size, base])
    return _Definition(mesg_num, "<", list(fields), 0, raw)


# An event message of the merge's own: timestamp, event, event_type, data.
_EVENT_DEFINITION = _definition(EVENT, [(TIMESTAMP_FIELD, 4, 0x86), (F_EVENT, 1, 0x00), (F_EVENT_TYPE, 1, 0x00),
                                        (F_EVENT_DATA, 4, 0x86)])


class _Msg:
    """One data message: its definition and its field bytes (without the
    header byte), with the timestamp it carries or inherits."""
    __slots__ = ("definition", "body", "compressed", "header", "timestamp")

    def __init__(self, definition: _Definition, body: bytearray, compressed: bool, header: int, timestamp: Optional[int]):
        self.definition = definition
        self.body = body
        self.compressed = compressed
        self.header = header
        self.timestamp = timestamp  # FIT date_time (seconds since the FIT epoch), None when it has none

    @property
    def mesg_num(self) -> int:
        return self.definition.mesg_num

    def _locate(self, num: int) -> Optional[Tuple[int, int, int]]:
        offset = 0
        for field_num, size, base in self.definition.fields:
            if field_num == num:
                return offset, size, base
            offset += size
        return None

    def get(self, num: int) -> Optional[int]:
        """An integer field's value; None when the message lacks it or it is unset."""
        found = self._locate(num)
        if found is None:
            return None
        offset, size, base = found
        spec = _INT_SPECS.get(base & 0x1F)
        if spec is None or spec[1] != size:
            return None
        value = struct.unpack_from(self.definition.endian + spec[0], self.body, offset)[0]
        return None if value == spec[2] else value

    def set(self, num: int, value: Union[int, float]) -> bool:
        """Set an integer field, clamped to the type's range; False when the
        message lacks the field."""
        found = self._locate(num)
        if found is None:
            return False
        offset, size, base = found
        spec = _INT_SPECS.get(base & 0x1F)
        if spec is None or spec[1] != size:
            return False
        code, _size, invalid = spec
        low = -(1 << (8 * size - 1)) if code.islower() else 0
        high = (1 << (8 * size - 1)) - 1 if code.islower() else (1 << (8 * size)) - 1
        value = max(low, min(high, int(round(value))))
        if value == invalid and invalid != 0:
            value -= 1  # the type's top value means "not set"
        struct.pack_into(self.definition.endian + code, self.body, offset, value)
        return True

    def copy(self) -> "_Msg":
        return _Msg(self.definition, bytearray(self.body), self.compressed, self.header, self.timestamp)


def _read_messages(data: bytes) -> List[_Msg]:
    """Every data message of a FIT file, through `fit_patch`'s walker."""
    buffer = bytearray(data)
    messages: List[_Msg] = []
    last: List[Optional[int]] = [None]

    def on_message(definition: _Definition, start: int, header: int, compressed: bool) -> None:
        body = bytearray(buffer[start:start + definition.size])
        msg = _Msg(definition, body, compressed, buffer[header], None)
        if compressed:
            if last[0] is not None:
                ts = (last[0] & ~0x1F) | (buffer[header] & 0x1F)
                msg.timestamp = ts + 0x20 if ts < last[0] else ts
        else:
            ts = msg.get(TIMESTAMP_FIELD)
            if ts is not None and ts >= MIN_DATE_TIME:
                msg.timestamp = ts
        if msg.timestamp is not None:
            last[0] = msg.timestamp
        messages.append(msg)

    _walk(buffer, on_message)
    return messages


class _Writer:
    """Writes data messages, emitting a definition whenever a message's
    definition is not in one of the 16 local message slots (0-3 for
    compressed-timestamp messages, which can only address those)."""

    def __init__(self) -> None:
        self.out = bytearray()
        self._slots: List[Optional[bytes]] = [None] * 16
        self._used = [0] * 16
        self._tick = 0
        self.developer_fields = False

    def _slot(self, definition: _Definition, compressed: bool) -> int:
        content = definition.raw[1:]
        limit = 4 if compressed else 16
        for i in range(limit):
            if self._slots[i] == content:
                break
        else:
            i = min(range(limit), key=lambda k: self._used[k])
            dev_flag = definition.raw[0] & 0x20
            self.developer_fields = self.developer_fields or bool(dev_flag)
            self.out.append(0x40 | dev_flag | i)
            self.out += content
            self._slots[i] = content
        self._tick += 1
        self._used[i] = self._tick
        return i

    def write(self, msg: _Msg) -> None:
        slot = self._slot(msg.definition, msg.compressed)
        if msg.compressed:
            self.out.append(0x80 | (slot << 5) | (msg.header & 0x1F))
        else:
            self.out.append(slot)
        self.out += msg.body


def _records(messages: Iterable[_Msg]) -> List[_Msg]:
    return [m for m in messages if m.mesg_num == RECORD and m.timestamp is not None]


def _interval(records: Sequence[_Msg]) -> int:
    """The logging interval: the most common gap between samples."""
    diffs = Counter(b.timestamp - a.timestamp for a, b in zip(records, records[1:]) if b.timestamp > a.timestamp)
    return diffs.most_common(1)[0][0] if diffs else 1


def _gas_key(msg: _Msg) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    return msg.get(F_GAS_O2), msg.get(F_GAS_HE), msg.get(F_GAS_MODE)


def _first(messages: Sequence[_Msg], mesg_num: int) -> Optional[_Msg]:
    return next((m for m in messages if m.mesg_num == mesg_num), None)


def _shift(msg: _Msg, delta: int) -> None:
    """Moves the message's timestamp fields by ``delta`` seconds (in place)."""
    if delta == 0:
        return
    if msg.compressed:
        if msg.timestamp is not None:
            msg.header = (msg.header & 0xE0) | ((msg.timestamp + delta) & 0x1F)
    else:
        ts = msg.get(TIMESTAMP_FIELD)
        if ts is not None and ts >= MIN_DATE_TIME:
            msg.set(TIMESTAMP_FIELD, ts + delta)
    for mesg_num, num in DATE_TIME_FIELDS:
        if msg.mesg_num == mesg_num:
            value = msg.get(num)
            if value is not None and value >= MIN_DATE_TIME:
                msg.set(num, value + delta)
    if msg.timestamp is not None:
        msg.timestamp += delta


def _is_timer(msg: _Msg, event_types: Iterable[int]) -> bool:
    return msg.mesg_num == EVENT and msg.get(F_EVENT) == EVENT_TIMER and msg.get(F_EVENT_TYPE) in event_types


def _fit_datetime(seconds: float) -> datetime:
    return (FIT_EPOCH + timedelta(seconds=seconds)).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# One log
# ---------------------------------------------------------------------------

class _Log:
    """One Garmin dive log as messages, with what the checks look at."""

    def __init__(self, data: bytes, name: str):
        self.name = name
        self.header = bytearray(data[:data[0]]) if data else bytearray()
        try:
            self.messages = _read_messages(data)
        except FitPatchError as e:
            raise MergeError(f"{name} is not a FIT file ({e})") from e
        self.records = _records(self.messages)
        self.session = _first(self.messages, SESSION)
        file_id = _first(self.messages, FILE_ID)
        file_type = file_id.get(F_FILE_TYPE) if file_id is not None else None
        sport = self.session.get(F_SPORT) if self.session is not None else None
        if self.session is None or (file_type is not None and file_type != FILE_TYPE_ACTIVITY) or sport != SPORT_DIVING:
            raise MergeError(f"{name} is not a Garmin dive log")
        if not self.records:
            raise MergeError(f"{name} has no samples")
        self.serial: Optional[int] = file_id.get(F_FILE_SERIAL) if file_id is not None else None
        start = self.session.get(F_START_TIME)
        self.start: int = start if start is not None and start >= MIN_DATE_TIME else self.records[0].timestamp
        timer = self.session.get(F_TIMER)
        self.end: float = self.start + timer / 1000.0 if timer is not None else float(self.records[-1].timestamp)

    @property
    def interval(self) -> int:
        return _interval(self.records)


@dataclass
class _Entry:
    """What the chain check needs of a log or a dive."""
    name: str
    serial: Optional[str]
    start: datetime
    end: datetime


def _chain_problem(entries: Sequence[_Entry]) -> Optional[str]:
    """Why ``entries`` (in the order given) cannot be merged, None when they can."""
    if len(entries) < 2:
        return "two or more dives are needed"
    first = entries[0]
    if first.serial is None:
        return f"{first.name} has no dive computer serial"
    for a, b in zip(entries, entries[1:]):
        if b.serial is None:
            return f"{b.name} has no dive computer serial"
        if b.serial != first.serial:
            return f"{b.name} and {first.name} come from different dive computers (serial {b.serial}, serial {first.serial})"
        if b.start < a.start:
            return f"{b.name} starts before {a.name}; merge the logs in start order"
        gap = (b.start - a.end).total_seconds()
        if gap <= 0:
            return f"{b.name} starts before {a.name} ends; the logs overlap"
        if gap > MAX_GAP_SECONDS:
            return (f"{a.name} and {b.name} are {gap / 3600:.1f} hours apart; only dives within "
                    f"{MAX_GAP_SECONDS // 3600} hours of each other can be merged")
    return None


def _entry_of_log(log: _Log) -> _Entry:
    return _Entry(log.name, str(log.serial) if log.serial else None, _fit_datetime(log.start), _fit_datetime(log.end))


def _entry_of_dive(dive: UnifiedDive, name: str) -> _Entry:
    start = dive.date_time_utc if dive.date_time_utc is not None else dive.date_time
    return _Entry(name, dive.computer_serial or None, start, start + timedelta(seconds=dive.duration or 0))


def merge_problem(items: Sequence[Union[UnifiedDive, PathLike]]) -> Optional[str]:
    """Why ``items`` cannot be merged in the order given, None when they can:
    two or more, the same dive computer serial, in start order, no overlap,
    each starting within `MAX_GAP_SECONDS` of the previous one's end. An
    item is a `UnifiedDive` as `fit_reader` read it (``computer_serial``,
    ``date_time_utc``, ``duration``; no second read for the page) or a
    path, which is read. The timing is the dives' (start and duration);
    `merge_fits` joins at the samples, which can differ by the seconds the
    timer ran on after the last sample."""
    entries: List[_Entry] = []
    for i, item in enumerate(items):
        if isinstance(item, UnifiedDive):
            entries.append(_entry_of_dive(item, f"dive {i + 1}" if item.dive_number is None else f"dive {item.dive_number}"))
        else:
            path = os.fspath(item)
            try:
                dive = read_fit(path)
            except Exception as e:
                return f"{os.path.basename(path)} is not a Garmin dive log ({e})"
            entries.append(_entry_of_dive(dive, os.path.basename(path)))
    return _chain_problem(entries)


def can_merge(items: Sequence[Union[UnifiedDive, PathLike]]) -> bool:
    """True when `merge_problem` finds nothing: what enables the Merge button."""
    return merge_problem(items) is None


def merged_file_name(first: PathLike) -> str:
    """The suggested name of the merged file: ``<first file name> (merged).fit``."""
    stem = os.path.splitext(os.path.basename(os.fspath(first)))[0]
    return f"{stem} (merged).fit"


# ---------------------------------------------------------------------------
# The join
# ---------------------------------------------------------------------------

@dataclass
class _Join:
    gap: int            # from the first log's last sample to the second's first, as logged
    delta: int          # applied to the second log's timestamps (0 when the clock is kept)
    closed: int         # seconds taken out of the timeline (0 when the clock is kept)
    filled: int
    gases_added: int
    tanks_added: int
    warnings: List[str]


def _join(first: _Log, second: _Log, close_gap: bool, shift: int = 0) -> Tuple[bytes, _Join]:
    """The bytes of ``first`` with ``second`` appended (the port of UWMedia's
    ``merge_fit_dives``); ``first``'s messages are patched in place.
    ``shift`` is how far ``first``'s tail has already been moved back when
    it is the accumulator of a closed-gap chain (zero or negative), so the
    gap is checked and reported as the logs recorded it."""
    msgs1, msgs2 = first.messages, second.messages
    records1, records2 = first.records, second.records
    last1, first2 = records1[-1].timestamp, records2[0].timestamp
    gap = first2 - last1 + shift      # as logged: the accumulator's last sample sits ``shift`` s before its real time
    if gap <= 0:
        raise MergeError(f"{second.name} starts before {first.name} ends; the logs overlap")
    if gap > MAX_GAP_SECONDS:
        raise MergeError(f"{first.name} and {second.name} are {gap / 3600:.1f} hours apart; only dives within "
                         f"{MAX_GAP_SECONDS // 3600} hours of each other can be merged")
    warnings: List[str] = []
    interval, interval2 = _interval(records1), _interval(records2)
    if interval2 != interval:
        warnings.append(f"{second.name} logs a sample every {interval2} s where {first.name} logs every {interval} s")
    delta = (last1 + interval) - first2 if close_gap else 0

    # Gases: the first file's list, plus the second's new ones.
    gases1 = [m for m in msgs1 if m.mesg_num == DIVE_GAS]
    gas_index = {_gas_key(m): m.get(MESSAGE_INDEX) for m in reversed(gases1)}
    next_index = max([m.get(MESSAGE_INDEX) or 0 for m in gases1], default=-1) + 1
    gas_map: Dict[int, int] = {}
    new_gases: List[_Msg] = []
    for gas in (m for m in msgs2 if m.mesg_num == DIVE_GAS):
        old = gas.get(MESSAGE_INDEX)
        if old is None:
            continue
        key = _gas_key(gas)
        if key not in gas_index:
            added = gas.copy()
            added.set(MESSAGE_INDEX, next_index)
            gas_index[key] = next_index
            new_gases.append(added)
            next_index += 1
        gas_map[old] = gas_index[key]

    # Tank pods and tank summaries the first file does not have.
    serials1 = {m.get(F_SERIAL) for m in msgs1 if m.mesg_num == DEVICE_INFO} - {None}
    new_pods = [m for m in msgs2 if m.mesg_num == DEVICE_INFO and m.get(F_SERIAL) not in serials1 | {None}]
    sensors1 = {m.get(F_SENSOR) for m in msgs1 if m.mesg_num == TANK_SUMMARY}
    summaries2 = {m.get(F_SENSOR): m for m in msgs2 if m.mesg_num == TANK_SUMMARY}
    tanks_added = sum(1 for sensor in summaries2 if sensor not in sensors1)

    # The second file's samples, in its order.
    rec_positions = [i for i, m in enumerate(msgs2) if m.mesg_num == RECORD]
    span = range(rec_positions[0], rec_positions[-1] + 1)
    stream2: List[_Msg] = []
    for i, msg in enumerate(msgs2):
        num = msg.mesg_num
        if num in SKIPPED_FROM_SECOND or num in (DIVE_GAS, DEVICE_INFO):
            continue
        if num == TANK_SUMMARY and msg.get(F_SENSOR) in sensors1:
            continue
        if num not in STREAM_FROM_SECOND and num not in (TANK_SUMMARY, DEVELOPER_DATA_ID, FIELD_DESCRIPTION) and i not in span:
            continue
        if _is_timer(msg, {EVENT_TYPE_START}) and (msg.timestamp is None or msg.timestamp <= first2):
            continue  # the second log's timer start: the merged dive's timer is already running
        msg = msg.copy()
        if num == EVENT and msg.get(F_EVENT) == EVENT_GAS_SWITCHED:
            data = msg.get(F_EVENT_DATA)
            if data in gas_map:
                msg.set(F_EVENT_DATA, gas_map[data])
        _shift(msg, delta)
        stream2.append(msg)
    # A dive that starts on another gas than the first one ended on, without
    # a switch event of its own to say so (Garmin logs one at the start of
    # every dive; other writers may not): record the switch at the join.
    switches1 = [m.get(F_EVENT_DATA) for m in msgs1 if m.mesg_num == EVENT and m.get(F_EVENT) == EVENT_GAS_SWITCHED]
    gas_before = switches1[-1] if switches1 else 0
    switched_at_start = any(m.mesg_num == EVENT and m.get(F_EVENT) == EVENT_GAS_SWITCHED
                            and m.timestamp is not None and m.timestamp <= first2 for m in msgs2)
    gas_after = gas_map.get(0, gas_before)
    if not switched_at_start and gas_after != gas_before:
        switch = _Msg(_EVENT_DEFINITION, bytearray(_EVENT_DEFINITION.size), False, 0, first2 + delta)
        switch.set(TIMESTAMP_FIELD, first2 + delta)
        switch.set(F_EVENT, EVENT_GAS_SWITCHED)
        switch.set(F_EVENT_TYPE, EVENT_TYPE_MARKER)
        switch.set(F_EVENT_DATA, gas_after)
        stream2.insert(0, switch)
        warnings.append(f"{second.name} starts on another gas than {first.name} ended on; a gas switch was recorded at the join")

    additions = list(new_gases)  # the second file's new gases and tank pods, in the first file's preamble
    for pod in new_pods:
        pod = pod.copy()
        _shift(pod, delta)
        additions.append(pod)

    # Bridge the gap with the first dive's last sample.
    filler: List[_Msg] = []
    if not close_gap:
        template = next((m for m in reversed(records1) if not m.compressed), None)
        if template is None:
            raise MergeError(f"{first.name} logs its samples without full timestamps; the gap cannot be filled")
        ts = last1 + interval
        while ts < first2:
            sample = template.copy()
            sample.set(TIMESTAMP_FIELD, ts)
            sample.timestamp = ts
            filler.append(sample)
            ts += interval

    # The first file's summaries now describe the merged dive.
    session1, session2 = first.session, second.session
    start1 = (session1.get(F_START_TIME) if session1 else None) or records1[0].timestamp
    start2 = (session2.get(F_START_TIME) if session2 else None) or first2
    timer2 = session2.get(F_TIMER) if session2 else None
    end2 = start2 + delta + (timer2 / 1000 if timer2 is not None else records2[-1].timestamp - start2)
    total_ms = int(round((end2 - start1) * 1000))
    depths = [m.get(F_DEPTH) for m in records1 + filler + records2]
    depths = [d for d in depths if d is not None]
    summary2 = _first(msgs2, DIVE_SUMMARY)
    for msg in msgs1:
        if msg.mesg_num in (SESSION, LAP):
            msg.set(F_ELAPSED, total_ms)
            msg.set(F_TIMER, total_ms)
            if msg.mesg_num == SESSION and session2 is not None:
                for num in (F_END_LAT, F_END_LONG):
                    value = session2.get(num)
                    if value is not None:
                        msg.set(num, value)
        elif msg.mesg_num == ACTIVITY:
            msg.set(F_ACTIVITY_TIMER, total_ms)
        elif msg.mesg_num == DIVE_SUMMARY:
            if depths:
                msg.set(F_AVG_DEPTH, sum(depths) / len(depths))
                msg.set(F_MAX_DEPTH, max(depths))
            if summary2 is not None:
                for num in (F_BOTTOM_TIME, F_O2_TOX):
                    a, b = msg.get(num), summary2.get(num)
                    if a is not None and b is not None:
                        msg.set(num, a + b)
                for num in (F_END_CNS, F_END_N2):
                    value = summary2.get(num)
                    if value is not None:
                        msg.set(num, value)
        elif msg.mesg_num == TANK_SUMMARY and msg.get(F_SENSOR) in summaries2:
            other = summaries2[msg.get(F_SENSOR)]
            end = other.get(F_END_PRESSURE)
            if end is not None:
                msg.set(F_END_PRESSURE, end)
            a, b = msg.get(F_VOLUME_USED), other.get(F_VOLUME_USED)
            if a is not None and b is not None:
                msg.set(F_VOLUME_USED, a + b)

    writer = _Writer()
    # Where the second file's gases and pods go: after the first file's last
    # gas, or before its first sample when it lists none.
    insert_after = max((i for i, m in enumerate(msgs1) if m.mesg_num == DIVE_GAS), default=None)
    first_record = next(i for i, m in enumerate(msgs1) if m.mesg_num == RECORD)
    for i, msg in enumerate(msgs1):
        if insert_after is None and i == first_record:
            for added in additions:
                writer.write(added)
        if _is_timer(msg, EVENT_TYPE_STOPS) and (msg.timestamp is None or msg.timestamp >= last1):
            continue  # the first log's timer stop: the dive goes on
        writer.write(msg)
        if insert_after is not None and i == insert_after:
            for added in additions:
                writer.write(added)
    for msg in filler + stream2:
        writer.write(msg)

    header = bytearray(first.header)
    struct.pack_into("<I", header, 4, len(writer.out))
    if writer.developer_fields and header[1] < 0x20:
        header[1] = 0x20  # developer fields need protocol 2.0
    data = bytearray(header) + writer.out + b"\0\0"
    recompute_crcs(data)
    return bytes(data), _Join(gap, delta, gap - interval if close_gap else 0, len(filler), len(new_gases), tanks_added, warnings)


def _load(path: str) -> _Log:
    name = os.path.basename(path)
    try:
        data, member = fit_bytes(path)
    except FitPatchError as e:
        raise MergeError(f"{name} is not a FIT file ({e})") from e
    except OSError as e:
        raise MergeError(f"{name} cannot be read ({e.strerror or e})") from e
    return _Log(data, name)


def merge_fits(paths: Sequence[PathLike], dest: PathLike, close_gap: bool) -> MergeResult:
    """Write the Garmin dive logs ``paths`` (two or more, in start order; a
    ``.fit`` or a Connect zip holding one) as one dive to ``dest``, keeping
    the clock and filling the gaps (``close_gap=False``) or moving each
    later log back onto the previous one (``close_gap=True``); see the
    module docstring. Raises `MergeError` when a file is not a Garmin dive
    log, the serials differ, the logs are out of order or overlap, or a
    gap is longer than `MAX_GAP_SECONDS`. The inputs are never written."""
    paths = [os.fspath(p) for p in paths]
    dest = os.fspath(dest)
    if len(paths) < 2:
        raise MergeError("two or more logs are needed")
    if any(os.path.abspath(dest) == os.path.abspath(p) for p in paths):
        raise MergeError("the merged file cannot replace one of the logs; choose another name")
    logs = [_load(p) for p in paths]
    problem = _chain_problem([_entry_of_log(log) for log in logs])
    if problem:
        raise MergeError(problem)
    result = MergeResult(path=dest, files=len(paths))
    acc, shift = logs[0], 0
    for nxt in logs[1:]:
        data, join = _join(acc, nxt, close_gap, shift)
        shift += join.delta
        result.gap_seconds += join.gap
        result.closed_seconds += join.closed
        result.filled_samples += join.filled
        result.gases_added += join.gases_added
        result.tanks_added += join.tanks_added
        result.warnings += join.warnings
        acc = _Log(data, acc.name)
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)
    logger.info("Merged %d logs into %s (%s): gap %d s, closed %d s, %d filler sample(s), %d gas(es) and %d tank(s) added%s",
                len(paths), dest, "gap closed" if close_gap else "clock kept", result.gap_seconds, result.closed_seconds,
                result.filled_samples, result.gases_added, result.tanks_added,
                "; " + "; ".join(result.warnings) if result.warnings else "")
    return result
