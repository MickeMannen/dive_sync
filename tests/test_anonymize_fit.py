"""Tests for the FIT fixture tool ``tests/tools/anonymize_fit.py`` (plans/convert.md I2).

The input is a small synthetic dive FIT built here byte by byte (made-up
serials, transmitter ids, names and positions), so no real file is needed.
garmin-fit-sdk decodes every file the tests write, which checks the tool's
CRCs and message layout independently of the tool's own parser.
"""
import struct

import pytest

garmin_fit_sdk = pytest.importorskip("garmin_fit_sdk", reason="garmin-fit-sdk is a dev-only dependency (requirements-dev.txt)")
from garmin_fit_sdk import Decoder, Stream  # noqa: E402

from tests.tools import anonymize_fit as tool

SERIAL = 3456789012
SENSOR_A, SENSOR_B, SENSOR_UNUSED = 3111111111, 2222222222, 2333333333
T0 = 1_100_000_000  # FIT seconds (2024-11-09)
START = (55.6, 12.6)
END = (55.601, 12.6025)
LAP_NE = (55.6012, 12.6031)
DURATION = 300

BASE = {"enum": (0x00, "B"), "sint8": (0x01, "b"), "uint8": (0x02, "B"), "uint16": (0x84, "H"),
        "sint32": (0x85, "i"), "uint32": (0x86, "I"), "uint32z": (0x8C, "I"), "uint16z": (0x8B, "H")}


def _msg(local, global_num, fields, big_endian=False):
    """A tool.Message from ``(field number, type, value)`` triples; a str
    value is a 16-byte string field."""
    defs, payload = [], bytearray()
    for num, kind, value in fields:
        if kind == "string":
            raw, base = value.encode().ljust(16, b"\0"), 0x07
        else:
            base, code = BASE[kind]
            raw = struct.pack((">" if big_endian else "<") + code, value)
        defs.append(tool.FieldDef(num, len(raw), base))
        payload += raw
    return tool.Message(tool.Definition(local, global_num, big_endian, tuple(defs)), payload)


def _semi(deg):
    return tool.semicircles(deg)


def _header():
    return bytes([14, 0x20]) + struct.pack("<H", 2195) + b"\0\0\0\0" + b".FIT" + b"\0\0"


def make_dive(records=DURATION, with_positions=True, user_name="Jane Diver"):
    """A synthetic two-transmitter dive with a gas switch, as a FIT file."""
    m = [_msg(0, 0, [(3, "uint32z", SERIAL), (4, "uint32", T0), (1, "uint16", 1), (2, "uint16", 4518), (0, "enum", 4)]),
         _msg(1, 3, [(0, "string", user_name)]),  # user_profile.friendly_name
         _msg(2, 23, [(253, "uint32", T0), (3, "uint32z", SERIAL), (4, "uint16", 4518), (0, "uint8", 0)]),
         _msg(3, 23, [(253, "uint32", T0), (24, "uint32z", SENSOR_A), (4, "uint16", 4442), (0, "uint8", 2),
                      (21, "uint16z", 4321)]),  # ant_device_number
         _msg(3, 23, [(253, "uint32", T0), (24, "uint32z", SENSOR_B), (4, "uint16", 4442), (0, "uint8", 3),
                      (21, "uint16z", 4322)]),
         _msg(4, 147, [(0, "uint32z", SENSOR_A), (2, "string", "Micke tank A"), (91, "string", "Micke tank A"),
                       (77, "uint16", 111), (5, "uint8", 9)]),  # field 5: unnamed, cut
         _msg(4, 147, [(0, "uint32z", SENSOR_B), (2, "string", "Micke tank B"), (91, "string", "Micke tank B"),
                       (77, "uint16", 111), (5, "uint8", 9)]),
         _msg(4, 147, [(0, "uint32z", SENSOR_UNUSED), (2, "string", "Spare pod"), (91, "string", "Spare pod"),
                       (77, "uint16", 111), (5, "uint8", 9)]),
         _msg(5, 259, [(254, "uint16", 0), (1, "uint8", 21), (0, "uint8", 0)]),
         _msg(5, 259, [(254, "uint16", 1), (1, "uint8", 50), (0, "uint8", 0)]),
         _msg(6, 21, [(253, "uint32", T0), (0, "enum", 0), (1, "enum", 0), (3, "uint32", 0)]),  # timer start
         _msg(6, 21, [(253, "uint32", T0), (0, "enum", 57), (1, "enum", 3), (3, "uint32", 0)]),  # gas switched
         _msg(6, 21, [(253, "uint32", T0), (0, "enum", 81), (1, "enum", 3), (3, "uint32", SENSOR_A)]),
         _msg(6, 21, [(253, "uint32", T0), (0, "enum", 81), (1, "enum", 3), (3, "uint32", SENSOR_B)]),
         _msg(6, 21, [(253, "uint32", T0), (0, "enum", 250), (1, "enum", 3), (3, "uint32", 1)])]  # unnamed event
    tanks = _msg(8, 319, [(253, "uint32", 0), (0, "uint32z", 0), (1, "uint16", 0)])
    for i in range(records):
        t = T0 + i
        depth = int(20000 * (1 - abs(i - records / 2) / (records / 2)))  # mm, deepest in the middle
        temp = 25 - depth // 4000
        m.append(_msg(7, 20, [(253, "uint32", t), (92, "uint32", depth), (13, "sint8", temp), (200, "uint8", 7)]))
        if i == 101:
            m.append(_msg(6, 21, [(253, "uint32", t), (0, "enum", 57), (1, "enum", 3), (3, "uint32", 1)]))
        if i % 5 == 0:
            for sensor, start in ((SENSOR_A, 20000), (SENSOR_B, 19000)):
                tanks = _msg(8, 319, [(253, "uint32", t), (0, "uint32z", sensor), (1, "uint16", start - 10 * i)])
                m.append(tanks)
    end = T0 + records - 1
    m += [_msg(9, 323, [(253, "uint32", end), (0, "uint32z", SENSOR_A), (1, "uint16", 20000), (2, "uint16", 17000)]),
          _msg(9, 323, [(253, "uint32", end), (0, "uint32z", SENSOR_B), (1, "uint16", 19000), (2, "uint16", 16000)])]
    pos = [(3, "sint32", _semi(START[0])), (4, "sint32", _semi(START[1])),
           (5, "sint32", _semi(END[0])), (6, "sint32", _semi(END[1])),
           (27, "sint32", _semi(LAP_NE[0])), (28, "sint32", _semi(LAP_NE[1]))] if with_positions else []
    # The lap comes first and starts somewhere else: the session's start position must still be the origin.
    lap_pos = [(3, "sint32", _semi(START[0] + 0.01)), (4, "sint32", _semi(START[1] + 0.01))] + pos[2:]
    m.append(_msg(10, 19, [(253, "uint32", end), (2, "uint32", T0)] + (lap_pos if with_positions else [])))
    session_pos = [(3, "sint32", _semi(START[0])), (4, "sint32", _semi(START[1])),
                   (38, "sint32", _semi(END[0])), (39, "sint32", _semi(END[1])),
                   (29, "sint32", _semi(LAP_NE[0])), (30, "sint32", _semi(LAP_NE[1]))] if with_positions else []
    m.append(_msg(11, 18, [(253, "uint32", end), (2, "uint32", T0), (5, "enum", 53), (6, "enum", 53)] + session_pos))
    m.append(_msg(12, 268, [(253, "uint32", end), (10, "uint32", 42), (3, "uint32", 20000)], big_endian=True))
    m.append(_msg(13, 34, [(253, "uint32", end), (5, "uint32", end + 7200), (1, "uint16", 1)]))
    return tool.build_fit(_header(), m)


def _decode(data):
    stream = Stream.from_byte_array(bytearray(data))
    assert Decoder(stream).check_integrity()
    messages, errors = Decoder(Stream.from_byte_array(bytearray(data))).read()
    assert errors == []
    return messages


# --- the file format -----------------------------------------------------------

def test_fit_crc_matches_the_crc16_check_value():
    assert tool.fit_crc(b"123456789") == 0xBB3D
    assert tool.fit_crc(b"6789", tool.fit_crc(b"12345")) == 0xBB3D


def test_synthetic_input_is_a_valid_fit():
    messages = _decode(make_dive())
    assert messages["user_profile_mesgs"][0]["friendly_name"] == "Jane Diver"
    assert len(messages["record_mesgs"]) == DURATION


def test_parse_and_build_round_trip_byte_for_byte():
    raw = make_dive(records=40)
    header, messages = tool.parse_fit(raw)
    assert tool.build_fit(header, messages) == raw
    assert sum(1 for m in messages if m.global_num == 20) == 40
    summary = next(m for m in messages if m.global_num == 268)
    assert summary.definition.big_endian and summary.value(10) == 42


@pytest.mark.parametrize("mutate, message", [
    (lambda raw: b"XXXX" + raw[4:], "not a FIT file"),
    (lambda raw: raw[:-3] + raw[-2:], "size"),
    (lambda raw: raw[:-2] + bytes([raw[-2] ^ 1, raw[-1]]), "CRC"),
])
def test_parse_refuses_broken_files(mutate, message):
    with pytest.raises(tool.FitError, match=message):
        tool.parse_fit(mutate(make_dive(records=10)))


def test_parse_refuses_compressed_timestamp_headers():
    header = bytearray(_header())
    body = bytes([0x40, 0, 0]) + struct.pack("<H", 20) + bytes([1, 92, 4, 0x86]) + bytes([0x80]) + b"\0" * 4
    struct.pack_into("<I", header, 4, len(body))
    raw = bytes(header) + body
    raw += struct.pack("<H", tool.fit_crc(raw))
    with pytest.raises(tool.FitError, match="compressed"):
        tool.parse_fit(raw)


def test_parse_cuts_developer_fields():
    header = bytearray(_header())
    # definition with one normal field (depth) and one 2-byte developer field
    body = bytes([0x60, 0, 0]) + struct.pack("<H", 20) + bytes([1, 92, 4, 0x86]) + bytes([1, 0, 2, 0])
    body += bytes([0x00]) + struct.pack("<I", 1234) + b"\xAA\xBB"
    struct.pack_into("<I", header, 4, len(body))
    raw = bytes(header) + body
    raw += struct.pack("<H", tool.fit_crc(raw))
    _header_out, messages = tool.parse_fit(raw)
    assert len(messages) == 1
    assert messages[0].definition.dev_size == 2
    assert messages[0].value(92) == 1234 and bytes(messages[0].payload) == struct.pack("<I", 1234)
    rebuilt = tool.build_fit(bytes(header), messages)
    assert b"\xAA\xBB" not in rebuilt[14:-2]
    assert _decode(rebuilt)["record_mesgs"][0]["depth"] == 1.234


def test_message_blank_cut_and_text():
    m = _msg(0, 147, [(0, "uint32z", 5), (2, "string", "abc"), (77, "uint16", 111)])
    assert m.text(2) == "abc" and m.value(77) == 111
    m.set_text(2, "x" * 40)
    assert m.text(2) == "x" * 15  # cut to fit, NUL-terminated
    m.blank(77)
    assert m.value(77) is None and m.is_blank(77)
    m.cut({2})
    assert m.get(2) is None and m.value(0) == 5 and len(m.payload) == 4 + 2


# --- thinning ------------------------------------------------------------------

def test_records_to_keep_every_nth_ends_extremes_and_anchors():
    times = list(range(100, 200))
    keep = tool.records_to_keep(times, 10, anchors=[155, 300, 50], always=[33])
    assert keep[:2] == [0, 10] and keep[-1] == 99
    assert 33 in keep and 55 in keep  # always, and the anchor's own second
    assert set(range(0, 100, 10)) <= set(keep)
    assert len(keep) == 10 + 1 + 1 + 1  # every 10th, the last, index 33, the anchor at 155
    assert tool.records_to_keep([], 5) == []


def test_records_to_keep_anchor_between_records_takes_the_nearest_earlier_on_a_tie():
    times = [0, 10, 20, 30]
    assert 1 in tool.records_to_keep(times, 100, anchors=[14])
    assert 2 in tool.records_to_keep(times, 100, anchors=[16])
    assert 1 in tool.records_to_keep(times, 100, anchors=[15])


def test_tank_updates_to_keep_per_sensor_interval_first_and_last():
    updates = [(1, 0), (2, 0), (1, 5), (2, 5), (1, 10), (2, 10), (1, 12), (2, 13)]
    keep = tool.tank_updates_to_keep(updates, 10)
    assert keep == [0, 1, 4, 5, 6, 7]
    assert tool.tank_updates_to_keep(updates, 1) == list(range(len(updates)))


# --- anonymising ---------------------------------------------------------------

@pytest.fixture(scope="module")
def anonymised():
    return tool.anonymize(make_dive(), max_bytes=10 ** 6)


def test_anonymize_output_decodes_and_keeps_the_dive(anonymised):
    output, report = anonymised
    messages = _decode(output)
    assert report.records_before == DURATION == report.records_after  # under the size limit: nothing thinned
    assert [g["oxygen_content"] for g in messages["dive_gas_mesgs"]] == [21, 50]
    switches = [e for e in messages["event_mesgs"] if e["event"] == "dive_gas_switched"]
    assert [e["data"] for e in switches] == [0, 1]
    assert messages["dive_summary_mesgs"][0]["dive_number"] == 42
    assert max(r["depth"] for r in messages["record_mesgs"]) == 20.0
    assert messages["activity_mesgs"][0]["local_timestamp"] == T0 + DURATION - 1 + 7200


def test_anonymize_drops_personal_and_unknown_messages(anonymised):
    output, report = anonymised
    messages = _decode(output)
    assert "user_profile_mesgs" not in messages
    assert report.dropped["user_profile"] == 1
    assert report.dropped["event(event_250)"] == 1
    assert len(messages["147"]) == 2  # the unused transmitter's profile is gone
    assert b"Jane" not in output and b"Micke" not in output and b"Spare" not in output


def test_anonymize_replaces_serials_and_transmitter_ids_consistently(anonymised):
    output, _report = anonymised
    messages = _decode(output)
    a, b = tool.PLACEHOLDER_SENSOR_BASE, tool.PLACEHOLDER_SENSOR_BASE + 1
    assert messages["file_id_mesgs"][0]["serial_number"] == tool.PLACEHOLDER_SERIAL_BASE
    assert messages["device_info_mesgs"][0]["serial_number"] == tool.PLACEHOLDER_SERIAL_BASE
    assert [d.get(24) for d in messages["device_info_mesgs"][1:]] == [a, b]
    assert all("ant_device_number" not in d for d in messages["device_info_mesgs"])
    assert {t["sensor"] for t in messages["tank_update_mesgs"]} == {a, b}
    assert [t["sensor"] for t in messages["tank_summary_mesgs"]] == [a, b]
    assert [(p[0], p[2], p[91]) for p in messages["147"]] == [(a, "Tank 1", "Tank 1"), (b, "Tank 2", "Tank 2")]
    pods = [e["data"] for e in messages["event_mesgs"] if e["event"] == "tank_pod_connected"]
    assert pods == [a, b]
    for value in (SERIAL, SENSOR_A, SENSOR_B, SENSOR_UNUSED):
        assert struct.pack("<I", value) not in output


def test_anonymize_moves_positions_but_keeps_their_offsets(anonymised):
    output, report = anonymised
    session = _decode(output)["session_mesgs"][0]

    def deg(v):
        return tool.degrees(v)
    assert deg(session["start_position_lat"]) == pytest.approx(tool.FIXED_LAT_DEG, abs=1e-6)
    assert deg(session["start_position_long"]) == pytest.approx(tool.FIXED_LONG_DEG, abs=1e-6)
    assert deg(session["end_position_lat"]) == pytest.approx(tool.FIXED_LAT_DEG + END[0] - START[0], abs=1e-6)
    assert deg(session["end_position_long"]) == pytest.approx(tool.FIXED_LONG_DEG + END[1] - START[1], abs=1e-6)
    assert deg(session["nec_lat"]) == pytest.approx(tool.FIXED_LAT_DEG + LAP_NE[0] - START[0], abs=1e-6)
    assert report.changed["position"] == 12
    for value in (_semi(START[0]), _semi(START[1]), _semi(END[0]), _semi(END[1])):
        assert struct.pack("<i", value) not in output


def test_anonymize_cuts_unnamed_fields(anonymised):
    output, report = anonymised
    messages = _decode(output)
    assert all(200 not in r for r in messages["record_mesgs"])
    assert all(5 not in p for p in messages["147"])
    assert report.cut_unnamed[("record", 200)] == DURATION


def test_anonymize_without_positions_adds_none():
    output, report = tool.anonymize(make_dive(records=30, with_positions=False), max_bytes=10 ** 6)
    session = _decode(output)["session_mesgs"][0]
    assert "start_position_lat" not in session and report.changed["position"] == 0


def test_anonymize_thins_to_the_size_limit_and_keeps_the_mandatory_records():
    raw = make_dive()
    output, report = tool.anonymize(raw, max_bytes=2500)
    assert len(output) <= 2500 and report.every > 1
    messages = _decode(output)
    records = messages["record_mesgs"]
    assert len(records) == report.records_after < DURATION
    times = [int(r["timestamp"].timestamp()) for r in records]
    first = int(messages["file_id_mesgs"][0]["time_created"].timestamp())
    assert times[0] == first and times[-1] == first + DURATION - 1
    assert max(r["depth"] for r in records) == 20.0  # the deepest record stays
    assert first + 101 in times  # the record of the mid-dive gas switch
    updates = messages["tank_update_mesgs"]
    assert report.tank_updates_after == len(updates) < report.tank_updates_before
    for sensor in {u["sensor"] for u in updates}:
        own = [u for u in updates if u["sensor"] == sensor]
        assert int(own[0]["timestamp"].timestamp()) == first  # start pressure
        assert int(own[-1]["timestamp"].timestamp()) == first + 295  # the last update


def test_anonymize_every_overrides_the_size_limit():
    _output, report = tool.anonymize(make_dive(), every=50)
    assert report.every == 50 and report.records_after < 20


def test_anonymize_aborts_when_an_identifier_survives(monkeypatch):
    monkeypatch.setattr(tool, "scrub_message", lambda m, ids, report: None)
    with pytest.raises(tool.LeakError, match="serial number"):
        tool.anonymize(make_dive(records=20))


def test_collect_identifiers_session_start_wins_over_an_earlier_lap():
    _header_out, messages = tool.parse_fit(make_dive(records=5))
    ids = tool.collect_identifiers(messages)
    assert ids.origin == (_semi(START[0]), _semi(START[1]))
    assert ids.serials == [SERIAL]
    assert ids.sensors[:2] == [SENSOR_A, SENSOR_B] and SENSOR_UNUSED in ids.sensors
    assert "Jane Diver" in ids.texts
    assert ids.moved(_semi(START[0]), is_long=False) == _semi(tool.FIXED_LAT_DEG)


def test_find_leaks_names_the_kind_and_offset_not_the_value():
    ids = tool.Identifiers(serials=[SERIAL], texts=["Secret Reef"])
    data = b"..." + struct.pack("<I", SERIAL) + b"Secret Reef"
    leaks = tool.find_leaks(data, ids)
    assert leaks == ["serial number at byte 3", "text at byte 7"]
    assert tool.find_leaks(b"clean", ids) == []


# --- listings and the command line ---------------------------------------------

def test_remaining_lists_ids_positions_and_times(anonymised):
    output, _report = anonymised
    text = "\n".join(tool.remaining(output))
    assert "file_id.serial_number = 1000000001" in text
    assert "tank_summary.sensor = 2000000002" in text
    assert "session.start_position_lat = -10.000000" in text
    assert "sensor_profile(147).name = 'Tank 1'" in text
    assert "dive_summary.dive_number = 42" in text
    assert "(offset +2.00 h)" in text


def test_describe_lists_every_message_and_field(anonymised):
    output, _report = anonymised
    lines = tool.describe(output)
    text = "\n".join(lines)
    assert lines[0] == "decode errors: 0"
    assert f"record x{DURATION}" in text and "  depth: 0 .. 20 " in text
    assert "dive_gas x2" in text and "oxygen_content=50" in text
    assert "mesg_147 x2" in text and "2=Tank 1" in text


def test_main_writes_the_fixture_and_the_listing(tmp_path, capsys):
    source = tmp_path / "in.fit"
    source.write_bytes(make_dive(records=60))
    out = tmp_path / "sub" / "out.fit"
    assert tool.main([str(source), str(out), "--every", "5"]) == 0
    printed = capsys.readouterr().out
    assert "What remains in the fixture" in printed and "records: 60 ->" in printed
    _decode(out.read_bytes())
    assert source.read_bytes() == make_dive(records=60)  # the source is only read

    assert tool.main([str(out), "--describe"]) == 0
    assert "file_id x1" in capsys.readouterr().out


def test_main_refuses_overwriting_the_source_and_bad_input(tmp_path, capsys):
    source = tmp_path / "in.fit"
    source.write_bytes(make_dive(records=10))
    assert tool.main([str(source), str(source)]) == 2
    bad = tmp_path / "bad.fit"
    bad.write_bytes(b"not a fit file at all")
    assert tool.main([str(bad), str(tmp_path / "o.fit")]) == 1
    assert not (tmp_path / "o.fit").exists()
    assert "error: not a FIT file" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        tool.main([str(source)])
