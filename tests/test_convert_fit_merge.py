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
                                        merged_file_name)
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
fixtures = pytest.mark.skipif(not os.path.isfile(TWO_TANKS), reason="Garmin FIT fixtures not present")
owner = pytest.mark.skipif(len(OWNER_FITS) != 3, reason="the owner's three test_data/fit dives are not present")


# --- synthetic logs ------------------------------------------------------------------

def _log(start, seconds, depths, gases=((32, 0),), number=12, serial=SERIAL, tank=None, switch_to=0, timer=None,
         timer_events=True, gas_switch=True, end_cns=5, bottom=None):
    """One Garmin dive log: file_id, session, activity, the gases, a timer
    start and a gas switch at ``start``, samples at ``seconds`` (offsets
    from ``start``, ``depths`` in m), a tank's readings and summary when
    ``tank`` is (sensor, start bar, end bar), a timer stop and a
    dive_summary. The session timer ends at the last sample unless ``timer``
    says otherwise."""
    timer = seconds[-1] if timer is None else timer
    bottom = timer if bottom is None else bottom
    messages = [
        _message(0, FILE_ID, [(0, "enum", 4), (1, "uint16", 1), (2, "uint16", 4223), (3, "uint32z", serial), (4, "uint32", start)]),
        _message(1, SESSION, [(253, "uint32", start), (2, "uint32", start), (7, "uint32", timer * 1000), (8, "uint32", timer * 1000),
                              (5, "enum", 53), (6, "enum", 53), (38, "sint32", start // 2), (39, "sint32", -(start // 4))]),
        _message(2, ACTIVITY, [(253, "uint32", start), (5, "uint32", start + 3600), (0, "uint32", timer * 1000)]),
        _message(3, DEVICE_INFO, [(253, "uint32", start), (3, "uint32z", serial), (4, "uint16", 4223)]),
    ]
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


def _assert_first_log_kept(first, merged):
    """Every message of the first log but its timer stop reappears in the
    merged file, in order, byte for byte outside the patched summary
    fields; the only messages in between are the gases and tank pods the
    later logs brought."""
    expected = [(m, b) for m, b in _messages(first) if not _is_timer_stop(m, b)]
    i = 0
    for mesg, body in _messages(merged):
        if i == len(expected):
            break
        want_mesg, want = expected[i]
        if mesg == want_mesg and body.keys() == want.keys() and all(
                body[num] == want[num] for num in want if (mesg, num) not in PATCHED):
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
    assert merge_problem([first, dive(508, t + timedelta(seconds=666), 100, serial=None)]) == "dive 508 has no dive computer serial"
    assert merge_problem([dive(1, t, 10, serial=None), first]) == "dive 1 has no dive computer serial"
    # dives without a UTC clock are placed by their wall clock
    naive = [UnifiedDive(date_time=t + timedelta(seconds=s), duration=100, max_depth=5.0, computer_serial="3504700399") for s in (0, 50)]
    assert merge_problem(naive) == "dive 2 starts before dive 1 ends; the logs overlap"
    naive[1].date_time += timedelta(seconds=60)
    assert can_merge(naive)


def test_merged_file_name():
    assert merged_file_name("/dives/507_2026-07-25_114300_23725272949.fit") == "507_2026-07-25_114300_23725272949 (merged).fit"
    assert merged_file_name("23725272949_ACTIVITY.zip") == "23725272949_ACTIVITY (merged).fit"


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
