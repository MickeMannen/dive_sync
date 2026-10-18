"""The Garmin FIT merge ``src/core/convert/fit_merge.py`` (plans/convert.md I16).

A dive the computer split into two or more logs is joined into one ``.fit``
at the record level. Pinned here on synthetic logs built with the encoder
of ``test_convert_fit`` (a pair and a chain of three, merged with the clock
kept and with the gaps closed), on the anonymised fixtures of I2 (two dives
two months apart, which the gap rule refuses) and, read-only, on the
owner's real split dive of 2026-07-25: three Descent X50i logs in
``test_data/fit`` (gitignored; skipped when absent). Every merged file is
read back with `fit_reader` as one dive, its CRC checked, decoded by
``fitdecode`` and - when ``garmin_fit_sdk`` is installed - by
Garmin's own decoder, and walked independently: every message of the first
log reappears in order with its bytes unchanged outside the patched summary
fields.
"""
import os
import struct
from datetime import datetime, timedelta

import pytest

from src.core.convert import fit_merge as module
from src.core.convert.fit_merge import (MAX_GAP_SECONDS, MergeError, MergeResult, can_merge, merge_fits, merge_problem,
                                        merged_file_name, missing_serials, serial_choices)
from src.core.convert.fit_reader import read_fit
from src.core.models import UnifiedDive
from tests.test_convert_fit import ACTIVITY, DIVE_GAS, DIVE_SUMMARY, EVENT, FILE_ID, RECORD, SESSION, T0, _message, build_fit
from tests.test_convert_fit_patch import OWNER_FITS, SINGLE_GAS, TWO_TANKS, _assert_well_formed

DEVICE_INFO, TANK_UPDATE, TANK_SUMMARY = 23, 319, 323
SERIAL = 777000111
# The fields the join patches in the first log's summaries.
PATCHED = {(SESSION, 7), (SESSION, 8), (SESSION, 38), (SESSION, 39), (19, 7), (19, 8), (ACTIVITY, 0),
           (DIVE_SUMMARY, 2), (DIVE_SUMMARY, 3), (DIVE_SUMMARY, 6), (DIVE_SUMMARY, 8), (DIVE_SUMMARY, 9), (DIVE_SUMMARY, 11),
           (TANK_SUMMARY, 2), (TANK_SUMMARY, 3)}
# The fields a chosen serial is written into (I20).
SERIAL_FIELDS = {(FILE_ID, 3), (DEVICE_INFO, 3)}
NO_SERIAL_WARNING = "No log carries a dive computer serial; Garmin Connect may not accept the merged file"
fixtures = pytest.mark.skipif(not os.path.isfile(TWO_TANKS), reason="Garmin FIT fixtures not present")
owner = pytest.mark.skipif(len(OWNER_FITS) != 3, reason="the owner's three test_data/fit dives are not present")


# --- synthetic logs ------------------------------------------------------------------

def _log(start, seconds, depths, gases=((32, 0),), number=12, serial=SERIAL, tank=None, switch_to=0, timer=None,
         timer_events=True, gas_switch=True, end_cns=5, bottom=None, file_serial=None, creator_serial=None,
         serial_field=True, file_id=True):
    """One Garmin dive log: file_id, session, activity, the creator
    device_info (device_index 0), the gases, a timer start and a gas
    switch at ``start``, samples at ``seconds`` (offsets from ``start``,
    ``depths`` in m), a tank's readings and summary when ``tank`` is
    (sensor, start bar, end bar), a timer stop and a dive_summary. The
    session timer ends at the last sample unless ``timer`` says otherwise.
    ``serial`` goes into file_id and the creator device_info (0 leaves it
    unset in both: a log without a serial); ``file_serial`` /
    ``creator_serial`` set one of them apart; ``serial_field=False`` writes
    a file_id without the serial field at all; ``file_id=False`` leaves the
    file_id message out."""
    timer = seconds[-1] if timer is None else timer
    bottom = timer if bottom is None else bottom
    file_serial = serial if file_serial is None else file_serial
    creator_serial = serial if creator_serial is None else creator_serial
    id_fields = [(0, "enum", 4), (1, "uint16", 1), (2, "uint16", 4223), (3, "uint32z", file_serial), (4, "uint32", start)]
    if not serial_field:
        id_fields = [f for f in id_fields if f[0] != 3]
    messages = [
        _message(1, SESSION, [(253, "uint32", start), (2, "uint32", start), (7, "uint32", timer * 1000), (8, "uint32", timer * 1000),
                              (5, "enum", 53), (6, "enum", 53), (38, "sint32", start // 2), (39, "sint32", -(start // 4))]),
        _message(2, ACTIVITY, [(253, "uint32", start), (5, "uint32", start + 3600), (0, "uint32", timer * 1000)]),
        _message(3, DEVICE_INFO, [(253, "uint32", start), (0, "uint8", 0), (3, "uint32z", creator_serial), (4, "uint16", 4223)]),
    ]
    if file_id:
        messages.insert(0, _message(0, FILE_ID, id_fields))
    for i, (o2, he) in enumerate(gases):
        messages.append(_message(4, DIVE_GAS, [(254, "uint16", i), (0, "uint8", he), (1, "uint8", o2), (2, "enum", 1), (3, "enum", 0)]))
    if timer_events:
        messages.append(_message(5, EVENT, [(253, "uint32", start), (0, "enum", 0), (1, "enum", 0), (3, "uint32", 0)]))
    if gas_switch:
        messages.append(_message(5, EVENT, [(253, "uint32", start), (0, "enum", 57), (1, "enum", 3), (3, "uint32", switch_to)]))
    for i, (s, depth) in enumerate(zip(seconds, depths)):
        messages.append(_message(6, RECORD, [(253, "uint32", start + s), (92, "uint32", int(depth * 1000)), (13, "sint8", 24)]))
        if tank is not None:
            sensor, p0, p1 = tank
            pressure = p0 + (p1 - p0) * i / max(1, len(seconds) - 1)
            messages.append(_message(7, TANK_UPDATE, [(253, "uint32", start + s), (0, "uint32z", sensor), (1, "uint16", int(pressure * 100))]))
    if timer_events:
        messages.append(_message(5, EVENT, [(253, "uint32", start + timer), (0, "enum", 0), (1, "enum", 4), (3, "uint32", 0)]))
    if tank is not None:
        sensor, p0, p1 = tank
        messages.append(_message(8, DEVICE_INFO, [(253, "uint32", start), (3, "uint32z", sensor), (4, "uint16", 3000)]))
        messages.append(_message(9, TANK_SUMMARY, [(253, "uint32", start), (0, "uint32z", sensor), (1, "uint16", p0 * 100),
                                                   (2, "uint16", p1 * 100), (3, "uint32", (p0 - p1) * 100)]))
    messages.append(_message(10, DIVE_SUMMARY, [(253, "uint32", start), (10, "uint32", number), (3, "uint32", int(max(depths) * 1000)),
                                                (2, "uint32", int(sum(depths) / len(depths) * 1000)), (11, "uint32", bottom * 1000),
                                                (6, "uint8", end_cns), (8, "uint16", 40), (9, "uint16", 3)]))
    return build_fit(messages)


# A: 0-120 s on 32 %, tank 1001 200 -> 150 bar. B: 180 s later, 300-420 s, on a new gas (50 %), tank 1002 180 -> 100 bar.
A_SECONDS, A_DEPTHS = (0, 60, 120), (0.5, 18.0, 1.0)
B_START, B_SECONDS, B_DEPTHS = T0 + 300, (0, 60, 120), (2.0, 25.0, 1.0)


def _a(**kw):
    return _log(T0, A_SECONDS, A_DEPTHS, tank=(1001, 200, 150), **kw)


def _b(**kw):
    kw.setdefault("gases", ((50, 0),))
    kw.setdefault("tank", (1002, 180, 100))
    return _log(B_START, B_SECONDS, B_DEPTHS, number=13, end_cns=9, **kw)


def _write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def _messages(data):
    """An independent walk: every data message as (message number,
    {field number: bytes}), in file order (`_walk_spans` of the patch
    tests, per message instead of per field)."""
    out = []
    pos = 0
    while pos < len(data):
        header_size = data[pos]
        end = pos + header_size + struct.unpack_from("<I", data, pos + 4)[0]
        definitions, p = {}, pos + header_size
        while p < end:
            header = data[p]
            if header & 0x80:
                local = (header >> 5) & 0x03
            elif header & 0x40:
                local = header & 0x0F
                endian = ">" if data[p + 2] == 1 else "<"
                mesg = struct.unpack_from(endian + "H", data, p + 3)[0]
                count = data[p + 5]
                fields = [(data[p + 6 + 3 * i], data[p + 7 + 3 * i]) for i in range(count)]
                q = p + 6 + 3 * count
                dev = 0
                if header & 0x20:
                    n = data[q]
                    dev = sum(data[q + 2 + 3 * i] for i in range(n))
                    q += 1 + 3 * n
                definitions[local] = (mesg, fields, dev)
                p = q
                continue
            else:
                local = header & 0x0F
            mesg, fields, dev = definitions[local]
            offset, body = p + 1, {}
            for num, size in fields:
                body[num] = bytes(data[offset:offset + size])
                offset += size
            out.append((mesg, body))
            p = offset + dev
        pos = end + 2
    return out


def _is_timer_stop(mesg, body):
    return mesg == EVENT and body.get(0) == b"\x00" and body.get(1) in (b"\x01", b"\x04")


def _assert_first_log_kept(first, merged, patched=PATCHED):
    """Every message of the first log but its timer stop reappears in the
    merged file, in order, byte for byte outside the ``patched`` fields
    (a file_id may have gained the serial field, I20); the only messages
    in between are the gases and tank pods the later logs brought."""
    expected = [(m, b) for m, b in _messages(first) if not _is_timer_stop(m, b)]
    i = 0
    for mesg, body in _messages(merged):
        if i == len(expected):
            break
        want_mesg, want = expected[i]
        same_fields = body.keys() == want.keys() or (mesg == FILE_ID and want.keys() <= body.keys())
        if mesg == want_mesg and same_fields and all(
                body[num] == want[num] for num in want if (mesg, num) not in patched):
            i += 1
        else:
            assert mesg in (DIVE_GAS, DEVICE_INFO), (i, mesg)
    assert i == len(expected), (i, len(expected))


# --- the constants -------------------------------------------------------------------

def test_constants_are_the_profiles():
    """The message and field numbers and the enums UWMedia took from
    garmin_fit_sdk's Profile are fitdecode's profile's."""
    from fitdecode import profile
    messages = {module.DEVICE_SETTINGS: "device_settings", module.USER_PROFILE: "user_profile", module.ZONES_TARGET: "zones_target",
                module.SPORT: "sport", module.TRAINING_SETTINGS: "training_settings", module.DEVICE_INFO: "device_info",
                module.FILE_CREATOR: "file_creator", module.HRV: "hrv", module.HR: "hr", module.GPS_METADATA: "gps_metadata",
                module.TIMESTAMP_CORRELATION: "timestamp_correlation", module.FIELD_DESCRIPTION: "field_description",
                module.DEVELOPER_DATA_ID: "developer_data_id", module.TIME_IN_ZONE: "time_in_zone", module.DIVE_ALARM: "dive_alarm",
                module.FILE_ID: "file_id", module.SESSION: "session", module.LAP: "lap", module.RECORD: "record", module.EVENT: "event",
                module.ACTIVITY: "activity", module.DIVE_SETTINGS: "dive_settings", module.DIVE_GAS: "dive_gas",
                module.DIVE_SUMMARY: "dive_summary", module.TANK_UPDATE: "tank_update", module.TANK_SUMMARY: "tank_summary"}
    for num, name in messages.items():
        assert profile.MESSAGE_TYPES[num].name == name, (num, name)
    fields = {(module.FILE_ID, module.F_FILE_TYPE): "type", (module.FILE_ID, module.F_FILE_SERIAL): "serial_number",
              (module.SESSION, module.F_SPORT): "sport", (module.SESSION, module.F_SUB_SPORT): "sub_sport",
              (module.EVENT, module.F_EVENT): "event", (module.EVENT, module.F_EVENT_TYPE): "event_type", (module.EVENT, module.F_EVENT_DATA): "data",
              (module.DIVE_GAS, module.F_GAS_HE): "helium_content", (module.DIVE_GAS, module.F_GAS_O2): "oxygen_content",
              (module.DIVE_GAS, module.F_GAS_MODE): "mode", (module.DIVE_GAS, module.MESSAGE_INDEX): "message_index",
              (module.DEVICE_INFO, module.F_SERIAL): "serial_number", (module.TANK_SUMMARY, module.F_SENSOR): "sensor",
              (module.DEVICE_INFO, module.F_DEVICE_INDEX): "device_index", (module.DEVICE_INFO, module.F_INFO_MANUFACTURER): "manufacturer",
              (module.DEVICE_INFO, module.F_INFO_PRODUCT): "product", (module.FILE_ID, module.F_FILE_MANUFACTURER): "manufacturer",
              (module.FILE_ID, module.F_FILE_PRODUCT): "product",
              (module.TANK_SUMMARY, module.F_END_PRESSURE): "end_pressure", (module.TANK_SUMMARY, module.F_VOLUME_USED): "volume_used",
              (module.RECORD, module.F_DEPTH): "depth", (module.SESSION, module.F_START_TIME): "start_time",
              (module.SESSION, module.F_ELAPSED): "total_elapsed_time", (module.SESSION, module.F_TIMER): "total_timer_time",
              (module.SESSION, module.F_END_LAT): "end_position_lat", (module.SESSION, module.F_END_LONG): "end_position_long",
              (module.ACTIVITY, module.F_ACTIVITY_TIMER): "total_timer_time", (module.DIVE_SUMMARY, module.F_AVG_DEPTH): "avg_depth",
              (module.DIVE_SUMMARY, module.F_MAX_DEPTH): "max_depth", (module.DIVE_SUMMARY, module.F_BOTTOM_TIME): "bottom_time",
              (module.DIVE_SUMMARY, module.F_END_CNS): "end_cns", (module.DIVE_SUMMARY, module.F_END_N2): "end_n2",
              (module.DIVE_SUMMARY, module.F_O2_TOX): "o2_toxicity", (module.GPS_METADATA, module.F_GPS_UTC): "utc_timestamp",
              (module.RECORD, module.TIMESTAMP_FIELD): "timestamp"}
    for (mesg, num), name in fields.items():
        assert profile.MESSAGE_TYPES[mesg].fields[num].name == name, (mesg, num, name)
    # the lap shares the session's timer fields (the join patches both alike)
    assert profile.MESSAGE_TYPES[module.LAP].fields[module.F_TIMER].name == "total_timer_time"
    assert profile.MESSAGE_TYPES[module.LAP].fields[module.F_ELAPSED].name == "total_elapsed_time"
    enums = profile.FIELD_TYPES
    assert enums["event"].enum[module.EVENT_TIMER] == "timer" and enums["event"].enum[module.EVENT_GAS_SWITCHED] == "dive_gas_switched"
    assert {enums["event_type"].enum[v] for v in module.EVENT_TYPE_STOPS} == {"stop", "stop_all"}
    assert enums["event_type"].enum[module.EVENT_TYPE_START] == "start" and enums["event_type"].enum[module.EVENT_TYPE_MARKER] == "marker"
    assert enums["sport"].enum[module.SPORT_DIVING] == "diving" and enums["file"].enum[module.FILE_TYPE_ACTIVITY] == "activity"
    assert enums["device_index"].enum[module.DEVICE_INDEX_CREATOR] == "creator"
    assert profile.MESSAGE_TYPES[module.FILE_ID].fields[module.F_FILE_SERIAL].type.name == "uint32z" and module.BASE_UINT32Z == 0x8C
    assert module.MAX_GAP_SECONDS == 2 * 3600
    # the integer base types the messages are read through: every FIT integer type, no float or string
    assert set(module._INT_SPECS) == {0, 1, 2, 3, 4, 5, 6, 10, 11, 12, 14, 15, 16}
    assert module._INT_SPECS[6] == ("I", 4, 0xFFFFFFFF) and module._INT_SPECS[12] == ("I", 4, 0)


def test_nothing_of_garmins_sdk_ships():
    """The module names the SDK only in its credit line, never imports it."""
    import re
    assert not re.search(r"^\s*(import|from)\s+garmin_fit_sdk", open(module.__file__).read(), re.MULTILINE)
    assert re.search(r"^\s*(import|from)\s+garmin_fit_sdk", "from garmin_fit_sdk import Profile", re.MULTILINE)


# --- the synthetic pair -------------------------------------------------------------------

def _check_pair(a, b, out, close_gap):
    first, second, merged = read_fit(a), read_fit(b), read_fit(out)
    assert merged.date_time_utc == first.date_time_utc and merged.computer_serial == first.computer_serial == str(SERIAL)
    assert merged.dive_number == first.dive_number == 12
    assert merged.max_depth == 25.0 == max(first.max_depth, second.max_depth)
    assert [(g.oxygen, g.start_pressure, g.end_pressure) for g in merged.gas_mixtures] == [(32.0, 200.0, 150.0), (50.0, 180.0, 100.0)]
    assert merged.cns_end == second.cns_end == 9.0 and merged.bottom_time == first.bottom_time + second.bottom_time == 240
    assert merged.exit_lat == pytest.approx(second.exit_lat) and merged.exit_lng == pytest.approx(second.exit_lng)
    if close_gap:
        assert merged.duration == 300 == first.duration + 60 + second.duration    # the second log follows the first's last sample
        assert [s.time for s in merged.samples] == [0, 60, 120, 180, 240, 300]
        assert [s.depth for s in merged.samples] == list(A_DEPTHS) + list(B_DEPTHS)
    else:
        assert merged.duration == 420 == (second.date_time_utc + timedelta(seconds=second.duration) - first.date_time_utc).total_seconds()
        assert [s.time for s in merged.samples] == [0, 60, 120, 180, 240, 300, 360, 420]
        assert [s.depth for s in merged.samples] == list(A_DEPTHS) + [1.0, 1.0] + list(B_DEPTHS)
    assert merged.avg_depth == pytest.approx(sum(s.depth for s in merged.samples) / len(merged.samples), abs=1e-3)
    # the tank readings sit on the samples of both halves; the gas switch of the second log points at the merged gas list
    assert [s.pressure for s in merged.samples][:3] == [200.0, 175.0, 150.0] and merged.samples[-1].channels.pressures == {1: 100.0}
    switches = [e for e in merged.events if e.type == "gas_switch"]
    assert [(e.time, e.tank, e.oxygen) for e in switches] == [(0, 0, 32.0), (180 if close_gap else 300, 1, 50.0)]
    _assert_well_formed(out)
    _assert_first_log_kept(open(a, "rb").read(), open(out, "rb").read())
    return merged


@pytest.mark.parametrize("close_gap", [False, True], ids=["keep", "close"])
def test_synthetic_pair(tmp_path, close_gap):
    """A (0-120 s) and B (300-420 s): one dive from A's start to B's end
    with the gap filled from A's last sample (keep), or 300 s long with
    B moved back onto A (close); gases, tanks, depths, CNS and bottom
    time merged; A's bytes kept; both decoders happy."""
    a, b = _write(tmp_path, "a.fit", _a()), _write(tmp_path, "b.fit", _b())
    out = tmp_path / "a (merged).fit"
    result = merge_fits([a, b], out, close_gap)
    assert isinstance(result, MergeResult) and result.path == str(out) and result.files == 2
    assert result.gap_seconds == 180 and result.gases_added == 1 and result.tanks_added == 1 and result.warnings == []
    if close_gap:
        assert result.closed_seconds == 120 and result.filled_samples == 0
    else:
        assert result.closed_seconds == 0 and result.filled_samples == 2
    _check_pair(a, b, str(out), close_gap)
    assert open(a, "rb").read() == _a() and open(b, "rb").read() == _b()           # the inputs are never written


def test_synthetic_pair_same_tank_and_gas(tmp_path):
    """B on A's gas and transmitter: nothing added, A's tank summary gets
    B's end pressure and the volumes used add up; a differing logging
    interval is a warning, not a refusal."""
    a = _write(tmp_path, "a.fit", _a())
    b = _write(tmp_path, "b.fit", _log(B_START, (0, 30, 60, 90, 120), (2.0, 10.0, 25.0, 10.0, 1.0), number=13, tank=(1001, 150, 80)))
    out = tmp_path / "m.fit"
    result = merge_fits([a, b], out, close_gap=False)
    assert result.gases_added == 0 and result.tanks_added == 0 and result.filled_samples == 2
    assert result.warnings == ["b.fit logs a sample every 30 s where a.fit logs every 60 s"]
    merged = read_fit(str(out))
    assert [(g.oxygen, g.start_pressure, g.end_pressure) for g in merged.gas_mixtures] == [(32.0, 200.0, 80.0)]
    assert len(merged.samples) == 3 + 2 + 5 and merged.max_depth == 25.0
    assert [e.time for e in merged.events if e.type == "gas_switch"] == [0, 300]
    messages = _assert_well_formed(out)
    if messages is not None:
        assert messages["tank_summary_mesgs"][0]["volume_used"] == pytest.approx(50 + 70)
        assert messages["session_mesgs"][0]["total_timer_time"] == 420
        timers = [m["event_type"] for m in messages["event_mesgs"] if m["event"] == "timer"]
        assert timers == ["start", "stop_all"]                      # A's stop and B's start are gone


def test_synthetic_gas_switch_recorded_at_the_join(tmp_path):
    """A second log that starts on another gas without a switch event of
    its own (Garmin logs one at the start of every dive; another writer may
    not): the merge records one at the join and says so. On the same gas
    nothing is recorded."""
    a = _write(tmp_path, "a.fit", _a())
    b = _write(tmp_path, "b.fit", _b(gas_switch=False))
    out = tmp_path / "m.fit"
    result = merge_fits([a, b], out, close_gap=True)
    assert result.gases_added == 1
    assert result.warnings == ["b.fit starts on another gas than a.fit ended on; a gas switch was recorded at the join"]
    merged = read_fit(str(out))
    assert [(e.time, e.tank, e.oxygen) for e in merged.events if e.type == "gas_switch"] == [(0, 0, 32.0), (180, 1, 50.0)]
    _assert_well_formed(out)
    same = _write(tmp_path, "b2.fit", _b(gases=((32, 0),), gas_switch=False))
    result = merge_fits([a, same], tmp_path / "m2.fit", close_gap=True)
    assert result.warnings == [] and result.gases_added == 0
    assert [e.time for e in read_fit(str(tmp_path / "m2.fit")).events if e.type == "gas_switch"] == [0]


# --- a chain of three -------------------------------------------------------------------

C_START = B_START + 120 + 90     # 90 s after B's last sample


def _c():
    return _log(C_START, (0, 60, 120, 180), (3.0, 12.0, 30.0, 0.8), number=14, tank=(1001, 150, 80), end_cns=15)


@pytest.mark.parametrize("close_gap", [False, True], ids=["keep", "close"])
def test_synthetic_chain_of_three_equals_pairwise(tmp_path, close_gap):
    """A + B + C in one call is byte for byte what merging A + B and then
    that file + C gives; the result spans the three, with C's tank (A's
    transmitter again) closing A's summary and C's gas list adding nothing."""
    a, b, c = _write(tmp_path, "a.fit", _a()), _write(tmp_path, "b.fit", _b()), _write(tmp_path, "c.fit", _c())
    chain = tmp_path / "chain.fit"
    result = merge_fits([a, b, c], chain, close_gap)
    ab = tmp_path / "ab.fit"
    first = merge_fits([a, b], ab, close_gap)
    second = merge_fits([str(ab), c], tmp_path / "abc.fit", close_gap)
    assert chain.read_bytes() == (tmp_path / "abc.fit").read_bytes()
    assert result.files == 3 and result.gap_seconds == 180 + 90 and first.gap_seconds == 180
    # the file ab.fit has no memory of its closed gap, so by hand the second join sees that too
    assert second.gap_seconds == 90 + (120 if close_gap else 0)
    assert result.gases_added == 1 and result.tanks_added == 1
    if close_gap:
        assert result.closed_seconds == 120 + 30 and result.filled_samples == 0
    else:
        assert result.closed_seconds == 0 and result.filled_samples == 2 + 1
    merged = read_fit(str(chain))
    c_dive = read_fit(c)
    assert merged.date_time_utc == read_fit(a).date_time_utc
    if close_gap:
        assert merged.duration == 120 + 60 + 120 + 60 + 180 == 540
        assert len(merged.samples) == 3 + 3 + 4
    else:
        assert merged.duration == (c_dive.date_time_utc + timedelta(seconds=c_dive.duration) - merged.date_time_utc).total_seconds() == 690
        assert len(merged.samples) == 3 + 2 + 3 + 1 + 4
    assert merged.max_depth == 30.0 and merged.cns_end == 15.0 and merged.bottom_time == 120 + 120 + 180
    assert [(g.oxygen, g.start_pressure, g.end_pressure) for g in merged.gas_mixtures] == [(32.0, 200.0, 80.0), (50.0, 180.0, 100.0)]
    assert [(e.tank, e.oxygen) for e in merged.events if e.type == "gas_switch"] == [(0, 32.0), (1, 50.0), (0, 32.0)]
    _assert_well_formed(chain)
    _assert_first_log_kept(open(a, "rb").read(), chain.read_bytes())


def test_chain_gaps_are_checked_as_logged(tmp_path):
    """Two gaps of 1.5 h: within the rule each, so the chain merges with
    the gaps closed too (the accumulator's moved-back tail must not make
    the second gap look like 3 h)."""
    gap = int(MAX_GAP_SECONDS * 0.75)
    a = _write(tmp_path, "a.fit", _a())
    b = _write(tmp_path, "b.fit", _log(T0 + 120 + gap, (0, 60, 120), (2.0, 20.0, 1.0), number=13))
    c = _write(tmp_path, "c.fit", _log(T0 + 120 + gap + 120 + gap, (0, 60, 120), (2.0, 15.0, 1.0), number=14))
    result = merge_fits([a, b, c], tmp_path / "m.fit", close_gap=True)
    assert result.gap_seconds == 2 * gap and result.closed_seconds == 2 * (gap - 60)
    assert read_fit(str(tmp_path / "m.fit")).duration == 120 + 60 + 120 + 60 + 120
    assert can_merge([a, b, c])
    result = merge_fits([a, b, c], tmp_path / "k.fit", close_gap=False)
    assert result.filled_samples == 2 * (gap // 60 - 1) and read_fit(str(tmp_path / "k.fit")).duration == 120 + gap + 120 + gap + 120


# --- refusals ------------------------------------------------------------------------------

def test_refusals(tmp_path):
    a = _write(tmp_path, "a.fit", _a())
    b = _write(tmp_path, "b.fit", _b())
    out = tmp_path / "out.fit"

    def refused(paths, text, **kw):
        with pytest.raises(MergeError, match=text):
            merge_fits(paths, out, kw.get("close_gap", False))
        assert not out.exists()

    refused([a], "two or more")
    refused([b, a], "a.fit starts before b.fit; merge the logs in start order")
    refused([a, a], "a.fit starts before a.fit ends; the logs overlap")
    overlapping = _write(tmp_path, "o.fit", _log(T0 + 60, (0, 60, 120), (1.0, 2.0, 1.0)))
    refused([a, overlapping], "o.fit starts before a.fit ends; the logs overlap")
    far = _write(tmp_path, "far.fit", _log(T0 + 120 + MAX_GAP_SECONDS + 1, (0, 60), (1.0, 2.0)))
    refused([a, far], r"a.fit and far.fit are 2.0 hours apart; only dives within 2 hours of each other can be merged")
    other = _write(tmp_path, "other.fit", _b(serial=SERIAL + 1))
    refused([a, other], f"other.fit and a.fit come from different dive computers \\(serial {SERIAL + 1}, serial {SERIAL}\\)")
    run = _write(tmp_path, "run.fit", _log(B_START, (0, 60), (0.0, 0.0)).replace(bytes([53, 53]), bytes([1, 1]), 1))
    refused([a, run], "run.fit is not a Garmin dive log")
    text = _write(tmp_path, "notes.txt", b"not a fit file at all, just text" * 4)
    refused([a, text], "notes.txt is not a FIT file")
    refused([a, str(tmp_path / "missing.fit")], "missing.fit cannot be read")
    with pytest.raises(MergeError, match="cannot replace one of the logs"):
        merge_fits([a, b], a, False)
    assert open(a, "rb").read() == _a()
    # the same checks through merge_problem on paths
    assert merge_problem([a]) == "two or more dives are needed"
    assert merge_problem([b, a]) == "a.fit starts before b.fit; merge the logs in start order"
    assert merge_problem([a, other]).startswith("other.fit and a.fit come from different dive computers")
    assert merge_problem([a, far]).startswith("a.fit and far.fit are 2.0 hours apart")
    assert merge_problem([a, text]).startswith("notes.txt is not a Garmin dive log")
    assert merge_problem([a, run]).startswith("run.fit is not a Garmin dive log")
    assert merge_problem([a, b]) is None and can_merge([a, b]) and not can_merge([b, a])


def test_can_merge_on_dives():
    """What enables the page's Merge button: the dives as fit_reader read
    them, by serial, UTC start and duration, in the order given."""
    def dive(number, start, duration, serial="3504700399"):
        return UnifiedDive(date_time=start + timedelta(hours=2), date_time_utc=start, duration=duration, max_depth=10.0,
                           dive_number=number, computer_serial=serial)
    t = datetime(2026, 7, 25, 9, 43, 31)
    first, second, third = dive(507, t, 655), dive(508, t + timedelta(seconds=666), 1045), dive(509, t + timedelta(seconds=1862), 1606)
    assert can_merge([first, second, third]) and merge_problem([first, second]) is None
    assert can_merge([first, third])                    # 509 starts 552 s after 507 ends
    assert merge_problem([first]) == "two or more dives are needed"
    assert merge_problem([second, first]) == "dive 507 starts before dive 508; merge the logs in start order"
    assert merge_problem([first, dive(508, t + timedelta(seconds=600), 100)]) == "dive 508 starts before dive 507 ends; the logs overlap"
    assert merge_problem([first, dive(None, t + timedelta(seconds=600), 100)]) == "dive 2 starts before dive 507 ends; the logs overlap"
    late = dive(510, t + timedelta(seconds=655 + MAX_GAP_SECONDS + 1), 100)
    assert merge_problem([first, late]) == "dive 507 and dive 510 are 2.0 hours apart; only dives within 2 hours of each other can be merged"
    assert merge_problem([first, dive(508, t + timedelta(seconds=666), 100, serial="1")]) == (
        "dive 508 and dive 507 come from different dive computers (serial 1, serial 3504700399)")
    # a dive without a serial merges (the dialog asks which serial the merged file gets, I20); two serials do not
    without = dive(508, t + timedelta(seconds=666), 100, serial=None)
    assert merge_problem([first, without]) is None and can_merge([dive(1, t - timedelta(seconds=100), 10, serial=None), first])
    assert missing_serials([first, without]) == ["dive 508"] and serial_choices([first, without]) == ["3504700399"]
    assert missing_serials([dive(None, t - timedelta(seconds=100), 10, serial=""), first, without]) == ["dive 1", "dive 508"]
    assert serial_choices([dive(1, t - timedelta(seconds=100), 10, serial=None), first, dive(509, t + timedelta(seconds=1862), 100)]) == ["3504700399"]
    assert merge_problem([dive(1, t - timedelta(seconds=100), 10, serial=None), first, dive(509, t + timedelta(seconds=1862), 100, serial="1")]) == (
        "dive 509 and dive 507 come from different dive computers (serial 1, serial 3504700399)")
    both = [dive(1, t - timedelta(seconds=100), 10, serial=None), without]
    assert merge_problem(both) is None and serial_choices(both) == [] and missing_serials(both) == ["dive 1", "dive 508"]
    assert missing_serials([first]) == [] and serial_choices([first]) == ["3504700399"]
    # dives without a UTC clock are placed by their wall clock
    naive = [UnifiedDive(date_time=t + timedelta(seconds=s), duration=100, max_depth=5.0, computer_serial="3504700399") for s in (0, 50)]
    assert merge_problem(naive) == "dive 2 starts before dive 1 ends; the logs overlap"
    naive[1].date_time += timedelta(seconds=60)
    assert can_merge(naive)


def test_merged_file_name():
    assert merged_file_name("/dives/507_2026-07-25_114300_23725272949.fit") == "507_2026-07-25_114300_23725272949 (merged).fit"
    assert merged_file_name("23725272949_ACTIVITY.zip") == "23725272949_ACTIVITY (merged).fit"


# --- the dive computer serial (I20) ---------------------------------------------------------

def _sdk_serials(messages):
    """(file_id serial, creator device_info serial) as Garmin's decoder reads them; None without the SDK."""
    if messages is None:
        return None
    creators = [m for m in messages["device_info_mesgs"] if m.get("device_index") in ("creator", 0)]
    return messages["file_id_mesgs"][0].get("serial_number"), creators[0].get("serial_number") if creators else None


def test_serial_falls_back_to_the_creator_device_info(tmp_path):
    """A log whose file_id has no serial but whose creator device_info
    (device_index 0) has one: the reader and the merge take that one, so
    the pair merges with nothing asked and nothing written (the first
    log's bytes stay as they were); a file whose only device_info is a
    tank pod has no serial."""
    a = _write(tmp_path, "a.fit", _a(file_serial=0))
    b = _write(tmp_path, "b.fit", _b())
    assert read_fit(a).computer_serial == str(SERIAL) and module._load(a).serial == SERIAL
    assert merge_problem([a, b]) is None and missing_serials([a, b]) == [] and serial_choices([a, b]) == [str(SERIAL)]
    out = tmp_path / "m.fit"
    result = merge_fits([a, b], out, False)
    assert result.serial == str(SERIAL) and result.warnings == []
    assert read_fit(str(out)).computer_serial == str(SERIAL)
    _assert_first_log_kept(open(a, "rb").read(), out.read_bytes())          # file_id's unset serial is left as it is
    assert _messages(out.read_bytes())[0][1][3] == b"\0\0\0\0"
    pod_only = _write(tmp_path, "pod.fit", _log(T0, A_SECONDS, A_DEPTHS, serial=0, tank=(1001, 200, 150)))
    assert read_fit(pod_only).computer_serial is None and module._load(pod_only).serial is None
    assert missing_serials([pod_only, b]) == ["pod.fit"]


def test_missing_serial_is_asked_not_refused(tmp_path):
    """A log without a serial anywhere merges: `merge_problem` is None,
    `missing_serials` names it and `serial_choices` lists what the dialog
    offers; two different serials are still refused with the old text,
    with or without a serial-less log between them."""
    a, b = _write(tmp_path, "a.fit", _a()), _write(tmp_path, "b.fit", _b(serial=0))
    assert read_fit(b).computer_serial is None
    assert merge_problem([a, b]) is None and can_merge([a, b])
    assert missing_serials([a, b]) == ["b.fit"] and serial_choices([a, b]) == [str(SERIAL)]
    none_a = _write(tmp_path, "n.fit", _a(serial=0))
    assert merge_problem([none_a, b]) is None and serial_choices([none_a, b]) == [] and missing_serials([none_a, b]) == ["n.fit", "b.fit"]
    other = _write(tmp_path, "other.fit", _b(serial=SERIAL + 1))
    assert merge_problem([a, other]) == f"other.fit and a.fit come from different dive computers (serial {SERIAL + 1}, serial {SERIAL})"
    c = _write(tmp_path, "c.fit", _c())
    assert merge_problem([none_a, other, c]) == f"c.fit and other.fit come from different dive computers (serial {SERIAL}, serial {SERIAL + 1})"
    with pytest.raises(MergeError, match="different dive computers"):
        merge_fits([none_a, other, c], tmp_path / "x.fit", False)
    assert not (tmp_path / "x.fit").exists()
    assert serial_choices([none_a, other, c]) == [str(SERIAL + 1), str(SERIAL)]
    # an unreadable path is merge_problem's to report, not a missing serial
    text = _write(tmp_path, "notes.txt", b"not a fit file at all, just text" * 4)
    assert missing_serials([a, text]) == [] and serial_choices([a, text]) == [str(SERIAL)]


@pytest.mark.parametrize("how", ["chosen", "chosen_int", "taken"], ids=["chosen", "int", "default"])
def test_merge_writes_the_serial_into_the_first_log(tmp_path, how):
    """The first log has no serial, the second has: the serial given to
    `merge_fits` (as text or number) - or, with none given, the second
    log's - is written into the first log's file_id and creator
    device_info in place; the output reads back with it under fit_reader
    and Garmin's decoder, its CRC holds, and the first log's bytes are
    unchanged outside those two fields and the patched summaries."""
    a, b = _write(tmp_path, "a.fit", _a(serial=0)), _write(tmp_path, "b.fit", _b())
    assert read_fit(a).computer_serial is None and read_fit(b).computer_serial == str(SERIAL)
    out = tmp_path / "m.fit"
    serial = {"chosen": str(SERIAL), "chosen_int": SERIAL, "taken": None}[how]
    result = merge_fits([a, b], out, False, serial=serial)
    assert result.serial == str(SERIAL) and result.files == 2
    if how == "taken":
        assert result.warnings == [f"a.fit has no dive computer serial; the merged file takes serial {SERIAL} from b.fit"]
    else:
        assert result.warnings == [f"a.fit has no dive computer serial; the merged file carries serial {SERIAL}"]
    merged = read_fit(str(out))
    assert merged.computer_serial == str(SERIAL) and merged.duration == 420 and len(merged.samples) == 8
    messages = _assert_well_formed(out)
    assert _sdk_serials(messages) in (None, (SERIAL, SERIAL))
    walked = _messages(out.read_bytes())
    assert walked[0][0] == FILE_ID and walked[0][1][3] == struct.pack("<I", SERIAL)
    creator = next(b for m, b in walked if m == DEVICE_INFO and b.get(0) == b"\0")
    assert creator[3] == struct.pack("<I", SERIAL)
    _assert_first_log_kept(open(a, "rb").read(), out.read_bytes(), PATCHED | SERIAL_FIELDS)
    with pytest.raises(AssertionError):
        _assert_first_log_kept(open(a, "rb").read(), out.read_bytes())          # the serial fields did change
    assert open(a, "rb").read() == _a(serial=0)
    # the same bytes whichever way the serial came
    merge_fits([a, b], tmp_path / "again.fit", False, serial=str(SERIAL))
    assert (tmp_path / "again.fit").read_bytes() == out.read_bytes()


def test_merge_serial_refusals(tmp_path):
    """No serial at all, a serial that is no number, or one the first log
    does not carry: refused before anything is written; a blank serial is
    none given; the first log's own serial is accepted."""
    a, b = _write(tmp_path, "a.fit", _a()), _write(tmp_path, "b.fit", _b(serial=0))
    out = tmp_path / "m.fit"
    for bad in ("abc", "0", "-1", str(0xFFFFFFFF + 1), "12 34"):
        with pytest.raises(MergeError, match="is not a dive computer serial"):
            merge_fits([a, b], out, False, serial=bad)
    with pytest.raises(MergeError, match=f"a.fit carries serial {SERIAL}; the merged file cannot be given serial 42"):
        merge_fits([a, b], out, False, serial="42")
    assert not out.exists()
    result = merge_fits([a, b], out, False, serial="")
    assert result.serial == str(SERIAL) and result.warnings == [] and read_fit(str(out)).computer_serial == str(SERIAL)
    result = merge_fits([a, b], tmp_path / "same.fit", False, serial=str(SERIAL))
    assert result.serial == str(SERIAL) and result.warnings == [] and (tmp_path / "same.fit").read_bytes() == out.read_bytes()
    _assert_first_log_kept(open(a, "rb").read(), out.read_bytes())


def test_merge_adds_the_serial_field_when_file_id_lacks_it(tmp_path):
    """A first log whose file_id has no serial field at all: the field is
    appended to the message (a definition of its own, the other fields'
    bytes untouched) and both decoders read it; without a file_id message
    the serial cannot be written and the result says so."""
    a, b = _write(tmp_path, "a.fit", _a(serial_field=False)), _write(tmp_path, "b.fit", _b())
    assert _messages(open(a, "rb").read())[0][1].keys() == {0, 1, 2, 4} and read_fit(a).computer_serial == str(SERIAL)  # the creator's
    a0 = _write(tmp_path, "a0.fit", _a(serial_field=False, creator_serial=0))
    assert read_fit(a0).computer_serial is None
    out = tmp_path / "m.fit"
    result = merge_fits([a0, b], out, False, serial=str(SERIAL))
    assert result.serial == str(SERIAL) and result.warnings == [f"a0.fit has no dive computer serial; the merged file carries serial {SERIAL}"]
    assert read_fit(str(out)).computer_serial == str(SERIAL)
    assert _sdk_serials(_assert_well_formed(out)) in (None, (SERIAL, SERIAL))
    first_id, merged_id = _messages(open(a0, "rb").read())[0][1], _messages(out.read_bytes())[0][1]
    assert list(merged_id) == [0, 1, 2, 4, 3] and merged_id[3] == struct.pack("<I", SERIAL)
    assert {k: v for k, v in merged_id.items() if k != 3} == first_id
    _assert_first_log_kept(open(a0, "rb").read(), out.read_bytes(), PATCHED | SERIAL_FIELDS)
    no_id = _write(tmp_path, "noid.fit", _a(file_id=False, creator_serial=0))
    result = merge_fits([no_id, b], tmp_path / "n.fit", False, serial=str(SERIAL))
    assert result.serial is None and result.warnings == ["noid.fit has no file_id message; the dive computer serial could not be written"]
    walked = _messages((tmp_path / "n.fit").read_bytes())
    assert FILE_ID not in {m for m, _ in walked}
    # b's creator device_info (a serial noid.fit's creator lacks) came along as a pod, so the reader's fallback still finds the serial
    assert read_fit(str(tmp_path / "n.fit")).computer_serial == str(SERIAL)
    assert [b[3] for m, b in walked if m == DEVICE_INFO and b.get(0) == b"\0"] == [b"\0\0\0\0", struct.pack("<I", SERIAL)]


@pytest.mark.parametrize("close_gap", [False, True], ids=["keep", "close"])
def test_merge_without_any_serial(tmp_path, close_gap):
    """No log carries a serial: the merge goes ahead without one and the
    result warns that Garmin Connect may not take the file; given a
    serial, it is written."""
    a, b = _write(tmp_path, "a.fit", _a(serial=0)), _write(tmp_path, "b.fit", _b(serial=0))
    assert merge_problem([a, b]) is None and serial_choices([a, b]) == [] and missing_serials([a, b]) == ["a.fit", "b.fit"]
    out = tmp_path / "m.fit"
    result = merge_fits([a, b], out, close_gap)
    assert result.serial is None and result.warnings == [NO_SERIAL_WARNING]
    merged = read_fit(str(out))
    assert merged.computer_serial is None and merged.duration == (300 if close_gap else 420)
    assert _sdk_serials(_assert_well_formed(out)) in (None, (None, None))
    _assert_first_log_kept(open(a, "rb").read(), out.read_bytes())
    result = merge_fits([a, b], tmp_path / "s.fit", close_gap, serial="4242")
    assert result.serial == "4242" and result.warnings == ["a.fit has no dive computer serial; the merged file carries serial 4242"]
    assert read_fit(str(tmp_path / "s.fit")).computer_serial == "4242"


# --- the fixtures ----------------------------------------------------------------------------

@fixtures
def test_two_fixture_dives_are_refused_by_the_gap_rule(tmp_path):
    """single_gas (June) and two_tanks (August) share the anonymised
    serial but are two months apart: two dives, not one split."""
    dives = sorted((read_fit(p) for p in (SINGLE_GAS, TWO_TANKS)), key=lambda d: d.date_time_utc)
    assert dives[0].computer_serial == dives[1].computer_serial
    problem = merge_problem(dives)
    assert problem and "hours apart" in problem and not can_merge(dives)
    with pytest.raises(MergeError, match="hours apart"):
        merge_fits([SINGLE_GAS, TWO_TANKS], tmp_path / "m.fit", False)
    assert not (tmp_path / "m.fit").exists()
    assert merge_problem([TWO_TANKS, SINGLE_GAS]) == "single_gas.fit starts before two_tanks.fit; merge the logs in start order"


@owner
@pytest.mark.parametrize("close_gap", [False, True], ids=["keep", "close"])
def test_owner_split_dive(tmp_path, close_gap):
    """The owner's real split dive of 2026-07-25 (three Descent X50i logs,
    read-only): merged into a tmp path both ways, read back as one dive
    from the first start to the last end with the gaps filled (keep) or
    the samples contiguous (close), both decoders happy, gases and tank
    sensible, the originals' bytes untouched."""
    originals = [open(p, "rb").read() for p in OWNER_FITS]
    dives = [read_fit(p) for p in OWNER_FITS]
    assert len({d.computer_serial for d in dives}) == 1 and can_merge(dives) and can_merge(OWNER_FITS)
    out = tmp_path / merged_file_name(OWNER_FITS[0])
    result = merge_fits(OWNER_FITS, out, close_gap)
    assert [open(p, "rb").read() for p in OWNER_FITS] == originals
    assert result.files == 3 and result.warnings == [] and result.gases_added == 0 and result.tanks_added == 0
    merged = read_fit(str(out))
    first, last = dives[0], dives[-1]
    # the gaps as logged: from each log's last sample to the next one's first
    ends = [d.date_time_utc + timedelta(seconds=d.samples[-1].time) for d in dives]
    gaps = [int((b.date_time_utc - a_end).total_seconds()) for a_end, b in zip(ends, dives[1:])]
    assert result.gap_seconds == sum(gaps) and all(0 < g <= MAX_GAP_SECONDS for g in gaps)
    assert merged.date_time_utc == first.date_time_utc and merged.date_time == first.date_time and merged.timezone == first.timezone
    assert merged.dive_number == first.dive_number and merged.computer_serial == first.computer_serial
    assert merged.max_depth == max(d.max_depth for d in dives)
    assert merged.cns_end == last.cns_end and merged.bottom_time == pytest.approx(sum(d.bottom_time for d in dives), abs=1)
    kept_span = int((last.date_time_utc + timedelta(seconds=last.duration) - first.date_time_utc).total_seconds())
    samples = sum(len(d.samples) for d in dives)
    if close_gap:
        # each later log follows the previous one's last sample (one logging interval on), so the time a
        # timer ran on past its last sample (508: 117 s at the surface) is closed with the gap
        assert result.closed_seconds == sum(gaps) - 2 and result.filled_samples == 0
        assert merged.duration == sum(d.samples[-1].time + 1 for d in dives[:-1]) + last.duration == kept_span - result.closed_seconds
        assert len(merged.samples) == samples
    else:
        assert result.closed_seconds == 0 and result.filled_samples == sum(gaps) - 2
        assert merged.duration == kept_span
        assert len(merged.samples) == samples + result.filled_samples
    assert merged.samples[-1].time == merged.duration - (last.duration - last.samples[-1].time)
    assert [s.depth for s in merged.samples[:len(first.samples)]] == [s.depth for s in first.samples]
    assert [s.depth for s in merged.samples[-len(last.samples):]] == [s.depth for s in last.samples]
    assert len(merged.gas_mixtures) == 1
    gas = merged.gas_mixtures[0]
    assert gas.oxygen == first.gas_mixtures[0].oxygen and gas.start_pressure == first.gas_mixtures[0].start_pressure
    assert gas.end_pressure == last.gas_mixtures[0].end_pressure and gas.tank_volume == first.gas_mixtures[0].tank_volume
    assert [e for e in merged.events if e.type == "gas_switch"][0] == first.events[0]
    assert len(merged.events) == sum(len(d.events) for d in dives)
    messages = _assert_well_formed(out)
    if messages is not None:
        assert len(messages["session_mesgs"]) == 1 and len(messages["file_id_mesgs"]) == 1 and len(messages["activity_mesgs"]) == 1
        assert messages["session_mesgs"][0]["total_timer_time"] == pytest.approx(merged.duration, abs=1)
        assert [m["event_type"] for m in messages["event_mesgs"] if m["event"] == "timer"] == ["start", "stop_all"]
        assert len(messages["record_mesgs"]) == len(merged.samples)
    _assert_first_log_kept(originals[0], out.read_bytes())
