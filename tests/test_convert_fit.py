"""The Garmin FIT reader ``src/core/convert/fit_reader.py`` (plans/convert.md I3).

Pinned against the two anonymised fixtures of I2 (``tests/data/garmin_fit``;
skipped while the owner has not committed them) and against small synthetic
FIT files built here byte by byte, which need no decoder library but
``fitdecode`` itself. ``two_tanks.fit`` is the same dive as the 09:56 one of
the ``subsurface_cloud`` fixture (``2026/08/29-Sat-09=56=11``), so a few
values are cross-checked against what Subsurface made of the same file.
"""
import io
import os
import struct
import zipfile
from datetime import datetime

import pytest

from src.core.convert.fit_reader import FitReadError, NotADiveError, activity_id_from_name, read_fit, zone_name
from src.core.models import EVENT_ALERT, EVENT_GAS_SWITCH, DiveEvent, UnifiedDive

DATA = os.path.join(os.path.dirname(__file__), "data", "garmin_fit")
SINGLE_GAS = os.path.join(DATA, "single_gas.fit")
TWO_TANKS = os.path.join(DATA, "two_tanks.fit")
fixtures = pytest.mark.skipif(not os.path.isfile(TWO_TANKS), reason="Garmin FIT fixtures not present")

SUBSURFACE_DIVE = os.path.join(os.path.dirname(__file__), "data", "subsurface_cloud", "2026", "08",
                               "29-Sat-09=56=11", "Divecomputer")


# --- a minimal FIT encoder for the synthetic cases --------------------------

_CRC_TABLE = (0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
              0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400)
# base type byte, struct code
TYPES = {"enum": (0x00, "B"), "uint8": (0x02, "B"), "sint8": (0x01, "b"), "uint16": (0x84, "H"),
         "sint32": (0x85, "i"), "uint32": (0x86, "I"), "uint32z": (0x8C, "I"), "float32": (0x88, "f")}
T0 = 1_100_000_000  # FIT seconds: 2024-11-08 11:33:20 UTC
# Global message numbers and field numbers (Garmin's FIT profile).
FILE_ID, SESSION, RECORD, EVENT, ACTIVITY, DIVE_GAS, DIVE_SUMMARY = 0, 18, 20, 21, 34, 259, 268


def _crc(data, crc=0):
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc


def _message(local, global_num, fields):
    """A definition plus one data message; ``fields`` are
    ``(field number, type name, value)`` triples."""
    definition = bytearray([0x40 | local, 0, 0]) + struct.pack("<H", global_num) + bytes([len(fields)])
    data = bytearray([local])
    for num, kind, value in fields:
        base, code = TYPES[kind]
        raw = struct.pack("<" + code, value)
        definition += bytes((num, len(raw), base))
        data += raw
    return bytes(definition + data)


def build_fit(messages):
    """A complete FIT file (14-byte header, both CRCs) holding ``messages``."""
    body = b"".join(messages)
    header = bytearray(struct.pack("<BBHI4s", 14, 0x10, 2140, len(body), b".FIT"))
    header += struct.pack("<H", _crc(bytes(header)))
    out = bytes(header) + body
    return out + struct.pack("<H", _crc(out))


def _file_id(file_type=4, product=4223, serial=777000111):
    return _message(0, FILE_ID, [(0, "enum", file_type), (1, "uint16", 1), (2, "uint16", product),
                                 (3, "uint32z", serial), (4, "uint32", T0)])


def _session(sport=53, sub_sport=53, timer=600.0, start_lat=None):
    fields = [(253, "uint32", T0 + 600), (2, "uint32", T0), (8, "uint32", int(timer * 1000)),
              (5, "enum", sport), (6, "enum", sub_sport)]
    if start_lat is not None:
        fields += [(3, "sint32", start_lat), (4, "sint32", start_lat * 2)]
    return _message(1, SESSION, fields)


def _activity(offset_seconds=3600):
    return _message(2, ACTIVITY, [(253, "uint32", T0), (5, "uint32", T0 + offset_seconds), (0, "uint32", 600000)])


def _record(seconds, depth, temperature=None):
    fields = [(253, "uint32", T0 + seconds), (92, "uint32", int(depth * 1000))]
    if temperature is not None:
        fields.append((13, "sint8", temperature))
    return _message(3, RECORD, fields)


def _dive_gas(index, oxygen, helium=0, status=1):
    return _message(4, DIVE_GAS, [(254, "uint16", index), (0, "uint8", helium), (1, "uint8", oxygen), (2, "enum", status)])


def _gas_switch(seconds, gas_index):
    return _message(5, EVENT, [(253, "uint32", T0 + seconds), (0, "enum", 57), (1, "enum", 3), (3, "uint32", gas_index)])


def _dive_summary(number=12, max_depth=18.5):
    return _message(6, DIVE_SUMMARY, [(253, "uint32", T0 + 600), (10, "uint32", number), (3, "uint32", int(max_depth * 1000))])


def minimal_dive(**kwargs):
    """A single-gas dive with three samples, one gas and a gas switch."""
    return build_fit([_file_id(), _session(**kwargs), _activity(),
                      _record(0, 0.5, 24), _record(60, 18.0, 22), _record(120, 1.0, 23),
                      _dive_gas(0, 32), _gas_switch(0, 0), _dive_summary()])


# --- the fixtures -------------------------------------------------------------

@fixtures
def test_two_tanks_summary():
    """Dive 38 on 2026-08-29 at 09:56 local (UTC+7), air on two transmitters."""
    dive = read_fit(TWO_TANKS)
    assert isinstance(dive, UnifiedDive)
    assert dive.dive_number == 38
    assert dive.date_time == datetime(2026, 8, 29, 9, 56, 11)
    assert dive.date_time_utc == datetime(2026, 8, 29, 2, 56, 11)
    assert dive.timezone == "Etc/GMT-7"
    assert dive.duration == 2780
    assert dive.max_depth == pytest.approx(11.92)
    assert dive.avg_depth == pytest.approx(5.894)     # the watch's own (Connect's), not the profile mean 6.04
    assert (dive.temp_min, dive.temp_max, dive.temp_avg) == (30.0, 31.0, 30.0)
    assert dive.lat == pytest.approx(-10.0, abs=1e-6) and dive.lng == pytest.approx(-30.0, abs=1e-6)
    assert dive.exit_lat == pytest.approx(-10.000056, abs=1e-5) and dive.exit_lng == pytest.approx(-29.99979, abs=1e-5)
    assert dive.device_logged is True
    assert dive.external_ids == {} and dive.location is None and dive.buddy is None and dive.notes is None
    assert dive.weight is None and dive.visibility is None


@fixtures
def test_two_tanks_carried_fields():
    dive = read_fit(TWO_TANKS)
    assert (dive.computer_vendor, dive.computer_model) == ("Garmin", "Descent X50i")
    assert (dive.computer_serial, dive.computer_firmware) == ("1000000001", "7.05")
    assert (dive.gf_low, dive.gf_high, dive.deco_model) == (40, 85, "ZHL-16C")
    assert (dive.water_type, dive.water_density) == ("salt", 1025.0)
    assert dive.dive_mode == "oc_single_gas"
    assert dive.surface_interval == 497326 and dive.bottom_time == 2599
    assert (dive.cns_start, dive.cns_end) == (0.0, 0.0)


@fixtures
def test_two_tanks_gases_and_transmitters():
    """One gas, two transmitters: two tanks of air, each with its own start
    and end pressure, name and volume (an AL80)."""
    dive = read_fit(TWO_TANKS)
    tanks = [(g.oxygen, g.helium, g.start_pressure, g.end_pressure, g.tank_name, g.tank_volume) for g in dive.gas_mixtures]
    assert tanks == [(21.0, 0.0, 204.29, 164.37, "Tank 1", 11.1), (21.0, 0.0, 209.66, 161.75, "Tank 2", 11.1)]


@fixtures
def test_two_tanks_samples_and_pressures():
    dive = read_fit(TWO_TANKS)
    assert len(dive.samples) == 331
    first, last = dive.samples[0], dive.samples[-1]
    assert (first.time, first.depth, first.temp) == (0, 1.207, 31.0)
    assert last.time == 2720 and last.depth < 0.1
    assert [s.time for s in dive.samples] == sorted(s.time for s in dive.samples)
    assert max(s.depth for s in dive.samples) == pytest.approx(dive.max_depth)
    # every reading of the two transmitters lands on a sample, keyed by the tank's index
    per_tank = {0: [], 1: []}
    for s in dive.samples:
        for tank, bar in (s.channels.pressures if s.channels else {}).items():
            per_tank[tank].append(bar)
        if s.channels and 0 in s.channels.pressures:
            assert s.pressure == s.channels.pressures[0]
        elif s.pressure is not None:
            pytest.fail("a sample's pressure must be the first tank's reading")
    assert len(per_tank[0]) > 200 and len(per_tank[1]) > 200
    assert per_tank[0][0] == 204.29 and per_tank[1][0] == 209.66   # = the start pressures
    assert per_tank[0][-1] == pytest.approx(164.37, abs=1.0) and per_tank[1][-1] == pytest.approx(161.75, abs=1.0)
    # the channels of one sample: ppo2 in bar, tts in seconds, cns in percent
    rich = next(s for s in dive.samples if s.channels and s.channels.tts)
    assert 0.2 < rich.channels.ppo2 < 0.5 and rich.channels.deco_stop_depth == 0.0 and rich.channels.cns == 0.0
    assert any(s.channels.gas_time for s in dive.samples if s.channels)
    assert all(s.channels.ndl is None or s.channels.ndl > 0 for s in dive.samples if s.channels)


@fixtures
def test_two_tanks_events():
    """The start-of-dive gas switch to tank 0 (air), Garmin's safety stop
    alerts, and the transmitters connecting and dropping off."""
    dive = read_fit(TWO_TANKS)
    assert dive.events[0].model_dump() == {"time": 0, "type": EVENT_GAS_SWITCH, "tank": 0, "oxygen": 21.0, "helium": 0.0}
    assert [e.time for e in dive.events] == sorted(e.time for e in dive.events)
    alerts = [(e.time, e.name) for e in dive.events if e.type == EVENT_ALERT]
    assert (1910, "safety_stop_started") in alerts and (2090, "safety_stop_complete") in alerts
    assert (2759, "near_surface") in alerts
    unknown = next(e for e in dive.events if e.type == EVENT_ALERT and e.name == "48")
    assert unknown.value == 48.0   # an alert fitdecode's profile does not name is kept by number
    assert {(e.type, e.tank) for e in dive.events if e.type.startswith("tank_pod")} == {
        ("tank_pod_connected", 0), ("tank_pod_connected", 1), ("tank_pod_disconnected", 1)}
    assert len(dive.events) == 12


@fixtures
@pytest.mark.skipif(not os.path.isfile(SUBSURFACE_DIVE), reason="subsurface fixture not present")
def test_two_tanks_agrees_with_subsurface_import():
    """Subsurface imported the same file: max depth, water temperature,
    salinity, deco model, firmware, the gas change at the start and the four
    kinds of alert agree."""
    dive = read_fit(TWO_TANKS)
    text = open(SUBSURFACE_DIVE, encoding="utf-8").read()
    assert f"maxdepth {dive.max_depth}m" in text
    assert f"watertemp {dive.temp_min}°C" in text
    assert f"salinity {int(dive.water_density)}g/l" in text
    assert f'"Buhlmann {dive.deco_model} {dive.gf_low}/{dive.gf_high}"' in text
    assert f'"FW Version" "{dive.computer_firmware}"' in text
    assert 'event 0:01 type=25 flags=1 name="gaschange" cylinder=0' in text and dive.events[0].tank == 0
    assert text.count('name="Alert timed out"') == sum(1 for e in dive.events if e.name == "alert_dismissed_by_timeout")
    logbook = open(os.path.join(os.path.dirname(SUBSURFACE_DIVE), "Dive-12"), encoding="utf-8").read()
    assert logbook.count("cylinder vol=11.094l") == len(dive.gas_mixtures)   # Subsurface's AL80 for the 11.1 L read here


@fixtures
def test_single_gas_fixture():
    """Dive 28 on 2026-06-27 at 11:24 local (UTC+8), EAN32 on one transmitter."""
    dive = read_fit(SINGLE_GAS)
    assert dive.dive_number == 28
    assert dive.date_time == datetime(2026, 6, 27, 11, 24, 51)
    assert dive.date_time_utc == datetime(2026, 6, 27, 3, 24, 51)
    assert dive.timezone == "Etc/GMT-8"
    assert dive.duration == 3065 and dive.bottom_time == 2884 and dive.surface_interval == 4690
    assert dive.max_depth == pytest.approx(24.131)
    assert dive.avg_depth == pytest.approx(10.138)   # the watch's own; the profile mean is 10.31
    assert (dive.temp_min, dive.temp_max) == (29.0, 30.0)
    assert [(g.oxygen, g.helium, g.start_pressure, g.end_pressure, g.tank_name) for g in dive.gas_mixtures] == [
        (32.0, 0.0, 192.91, 95.76, "Tank 1")]
    assert len(dive.samples) == 386
    assert dive.samples[0].pressure == 192.91 and dive.samples[-1].pressure == pytest.approx(96.19)
    assert all(set(s.channels.pressures) <= {0} for s in dive.samples if s.channels)
    assert dive.events[0].model_dump() == {"time": 0, "type": EVENT_GAS_SWITCH, "tank": 0, "oxygen": 32.0, "helium": 0.0}
    assert (dive.cns_start, dive.cns_end) == (3.0, 10.0)
    assert dive.computer_firmware == "5.12" and dive.dive_mode == "oc_single_gas"
    assert dive.events[-1].type == "tank_pod_disconnected" and dive.events[-1].tank == 0


@fixtures
def test_fixture_round_trips_through_json():
    """What the reader fills survives the model's JSON (the caches' format)."""
    dive = read_fit(SINGLE_GAS)
    again = UnifiedDive.model_validate_json(dive.model_dump_json())
    assert again == dive


@fixtures
def test_bytes_and_connect_zip_read_the_same(tmp_path):
    """The reader takes a path, the bytes, or Connect's "export original"
    zip around the file; the cache's file name gives the activity id."""
    raw = open(SINGLE_GAS, "rb").read()
    from_path, from_bytes = read_fit(SINGLE_GAS), read_fit(raw)
    assert from_bytes == from_path
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("24449823352_ACTIVITY.fit", raw)
    zipped = tmp_path / "28_2026-06-27_1124_24449823352.zip"
    zipped.write_bytes(buffer.getvalue())
    from_zip = read_fit(str(zipped))
    assert from_zip.external_ids == {"garmin": "24449823352"}
    assert from_zip.model_copy(update={"external_ids": {}}) == from_path


@fixtures
def test_connect_named_files_give_the_activity_id(tmp_path):
    """The files as Connect exports them (I9 follow-up): ``<id>.zip`` holding
    ``<id>_ACTIVITY.fit``, the member alone, or the zip renamed - the id is
    taken from whichever name holds it. A file renamed by its dive number
    (``515.fit``) holds no id."""
    raw = open(SINGLE_GAS, "rb").read()
    member = tmp_path / "22569827629_ACTIVITY.fit"
    member.write_bytes(raw)
    assert read_fit(str(member)).external_ids == {"garmin": "22569827629"}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("22569827629_ACTIVITY.fit", raw)
    connect = tmp_path / "22569827629.zip"
    connect.write_bytes(buffer.getvalue())
    assert read_fit(str(connect)).external_ids == {"garmin": "22569827629"}
    # the zip renamed: the member's name still carries the id; the bytes alone too
    renamed = tmp_path / "phuket day 2.zip"
    renamed.write_bytes(buffer.getvalue())
    assert read_fit(str(renamed)).external_ids == {"garmin": "22569827629"}
    assert read_fit(buffer.getvalue()).external_ids == {"garmin": "22569827629"}
    # a short all-digit name is a dive number, not an id
    short = tmp_path / "515.fit"
    short.write_bytes(raw)
    assert read_fit(str(short)).external_ids == {}


@pytest.mark.parametrize("names, expected", [
    (("38_2026-08-29_095611_24449823373.fit",), "24449823373"),
    (("22569827629.zip",), "22569827629"),
    (("22569827629_ACTIVITY.fit",), "22569827629"),
    (("22569827629_activity.FIT",), "22569827629"),
    (("/Users/me/DivingMedia/20260829_Phuket/logs/22569827629.zip",), "22569827629"),
    (("phuket.zip", "22569827629_ACTIVITY.fit"), "22569827629"),
    (("28_2026-06-27_1124_24449823352.zip", "22569827629_ACTIVITY.fit"), "24449823352"),   # the first name that has one
    (("515.fit",), None),
    (("20260829_Phuket.zip",), None),       # a date, not an id
    (("dive_22569827629.fit",), None),
    ((None, "", "two_tanks.fit"), None),
    ((), None),
])
def test_activity_id_from_name(names, expected):
    assert activity_id_from_name(*names) == expected


# --- synthetic files ----------------------------------------------------------

def test_minimal_dive_fills_what_is_there_and_leaves_the_rest_unset():
    dive = read_fit(minimal_dive())
    assert dive.date_time_utc == datetime(2024, 11, 8, 11, 33, 20)
    assert dive.date_time == datetime(2024, 11, 8, 12, 33, 20) and dive.timezone == "Etc/GMT-1"
    assert dive.duration == 600 and dive.dive_number == 12 and dive.max_depth == 18.5
    assert [(s.time, s.depth, s.temp) for s in dive.samples] == [(0, 0.5, 24.0), (60, 18.0, 22.0), (120, 1.0, 23.0)]
    assert dive.avg_depth == pytest.approx(((0.5 + 18.0) / 2 * 60 + (18.0 + 1.0) / 2 * 60) / 120, abs=1e-3)
    assert (dive.temp_min, dive.temp_max, dive.temp_avg) == (22.0, 24.0, None)
    assert [(g.oxygen, g.helium, g.start_pressure, g.tank_name) for g in dive.gas_mixtures] == [(32.0, 0.0, None, None)]
    assert all(s.pressure is None for s in dive.samples)
    assert dive.events == [DiveEvent(time=0, type=EVENT_GAS_SWITCH, tank=0, oxygen=32.0, helium=0.0)]
    assert dive.dive_mode == "oc_single_gas"
    assert (dive.computer_vendor, dive.computer_model, dive.computer_serial) == ("Garmin", "Descent Mk3(i) 51mm", "777000111")
    for name in ("computer_firmware", "gf_low", "gf_high", "deco_model", "water_type", "water_density", "exit_lat",
                 "exit_lng", "surface_interval", "bottom_time", "cns_start", "cns_end", "lat", "lng", "location"):
        assert getattr(dive, name) is None, name
    assert dive.external_ids == {} and dive.service_fields == {}


def test_no_local_timestamp_means_no_zone_and_local_equals_utc():
    data = build_fit([_file_id(), _session(), _record(0, 1.0), _record(10, 2.0)])
    dive = read_fit(data)
    assert dive.timezone is None and dive.date_time == dive.date_time_utc == datetime(2024, 11, 8, 11, 33, 20)
    assert dive.dive_number is None and dive.gas_mixtures == [] and dive.events == []
    assert dive.max_depth == 2.0 and dive.avg_depth == 1.5   # from the samples when there is no summary
    assert dive.temp_min is None and dive.dive_mode == "oc_single_gas"


def test_dive_modes_and_disabled_gases():
    dive = read_fit(build_fit([_file_id(), _session(sub_sport=54), _record(0, 1.0),
                               _dive_gas(0, 21, status=0), _dive_gas(1, 32), _dive_gas(2, 50), _gas_switch(0, 2)]))
    assert dive.dive_mode == "oc_multi_gas"
    assert [(g.oxygen, g.start_pressure) for g in dive.gas_mixtures] == [(32.0, None), (50.0, None)]
    assert dive.events[0].tank == 1 and dive.events[0].oxygen == 50.0   # the switch names the gas's message index
    assert read_fit(build_fit([_file_id(), _session(sub_sport=55), _record(0, 1.0)])).dive_mode == "gauge"
    assert read_fit(build_fit([_file_id(), _session(sub_sport=63), _record(0, 1.0)])).dive_mode == "ccr"
    assert read_fit(build_fit([_file_id(), _session(sub_sport=0), _record(0, 1.0)])).dive_mode is None


def test_position_from_the_session():
    dive = read_fit(build_fit([_file_id(), _session(start_lat=2 ** 31 // 4), _record(0, 1.0)]))
    assert dive.lat == pytest.approx(45.0, abs=1e-6) and dive.lng == pytest.approx(90.0, abs=1e-6)
    assert dive.exit_lat is None


@pytest.mark.parametrize("sport, sub_sport, words", [
    (1, 0, "'running'"), (53, 56, "apnea"), (53, 57, "apnea"),
])
def test_other_activities_are_refused(sport, sub_sport, words):
    with pytest.raises(NotADiveError) as info:
        read_fit(build_fit([_file_id(), _session(sport=sport, sub_sport=sub_sport), _record(0, 1.0)]))
    assert words in str(info.value)


def test_a_settings_file_is_refused():
    with pytest.raises(NotADiveError, match="'settings' file"):
        read_fit(build_fit([_file_id(file_type=2), _session()]))


def test_a_file_without_session_is_refused():
    with pytest.raises(FitReadError, match="no session"):
        read_fit(build_fit([_file_id(), _record(0, 1.0)]))


@pytest.mark.parametrize("data", [
    b"", b"not a fit file at all", b"\x0e\x10\x5c\x08\x00\x00\x00\x00.FIT\x00\x00",
    minimal_dive()[:-40], minimal_dive()[:-1] + b"\x00",
])
def test_corrupt_files_raise_a_clear_error(data):
    with pytest.raises(FitReadError, match="not a FIT file"):
        read_fit(data)


def test_a_zip_without_fit_and_a_missing_file(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w"):
        pass
    with pytest.raises(FitReadError):
        read_fit(buffer.getvalue())
    with pytest.raises(FileNotFoundError):
        read_fit(str(tmp_path / "missing.fit"))


@pytest.mark.parametrize("offset, name", [
    (0, "Etc/UTC"), (3600, "Etc/GMT-1"), (8 * 3600, "Etc/GMT-8"), (-5 * 3600, "Etc/GMT+5"), (14 * 3600, "Etc/GMT-14"),
    (5 * 3600 + 1800, None), (15 * 3600, None), (-13 * 3600, None),
])
def test_zone_name(offset, name):
    assert zone_name(offset) == name
    if name:
        from zoneinfo import ZoneInfo

        from datetime import timedelta
        assert datetime(2026, 1, 1, tzinfo=ZoneInfo(name)).utcoffset() == timedelta(seconds=offset)
