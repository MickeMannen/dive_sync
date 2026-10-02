"""The Subsurface ``.ssrf`` reader ``src/core/convert/ssrf_reader.py``
(plans/convert.md I6b) and its wiring in ``formats.py``.

Round trips through the I6 writer (the synthetic dive of ``test_convert_ssrf``
and the anonymised FIT fixtures when they are on disk), the pinned
``synthetic.ssrf``, and ``tests/data/ssrf/handwritten.ssrf`` (invented data)
for what the writer never produces: trips, imperial units, several dive
computers, Subsurface's own extradata and legacy forms, sticky values. The
owner's local UWMedia exports are read only when present and never copied.
Nothing here touches a live service or the owner's files.
"""
import glob
import os
from datetime import datetime
from xml.etree import ElementTree as ET

import pytest

from src.core.convert import formats
from src.core.convert.fit_reader import read_fit
from src.core.convert.formats import UnsupportedFileError, read_file, read_files, write_file
from src.core.convert.ssrf_reader import (
    SsrfReadError,
    depth_m,
    parse_deco_model,
    parse_gps,
    parse_ssrf,
    percent,
    pressure_bar,
    quantity,
    read_ssrf,
    read_ssrf_string,
    seconds,
    split_model,
    temperature_c,
    volume_l,
    weight_kg,
)
from src.core.convert.ssrf_writer import SsrfDocument
from src.core.models import UnifiedDive
from tests.test_convert_ssrf import synthetic_dive

DATA = os.path.join(os.path.dirname(__file__), "data")
FIT_DIR = os.path.join(DATA, "garmin_fit")
SINGLE_GAS = os.path.join(FIT_DIR, "single_gas.fit")
TWO_TANKS = os.path.join(FIT_DIR, "two_tanks.fit")
PINNED = os.path.join(DATA, "ssrf", "synthetic.ssrf")
HANDWRITTEN = os.path.join(DATA, "ssrf", "handwritten.ssrf")
UWMEDIA_LOGS = "/Users/mikael/development/UWMedia/test_data/logs"   # the owner's local exports, never committed
fixtures = pytest.mark.skipif(not os.path.isfile(TWO_TANKS), reason="Garmin FIT fixtures not present")


def _write(dives, path) -> str:
    document = SsrfDocument()
    for dive in dives:
        document.add_dive(dive)
    document.save(path)
    return str(path)


def _channels(sample) -> dict:
    return sample.channels.model_dump() if sample.channels else {}


# --- value parsing --------------------------------------------------------------

def test_quantity_splits_number_and_unit():
    assert quantity("12.3 m") == (12.3, "m")
    assert quantity("192.98 bar") == (192.98, "bar")
    assert quantity("-1.5 C") == (-1.5, "c")
    assert quantity("60") == (60.0, "")
    assert quantity(None) == (None, "") and quantity("abc") == (None, "")


def test_metric_strings_are_read_as_they_are():
    assert depth_m("12.345 m") == 12.345 and depth_m("1500 mm") == 1.5
    assert temperature_c("29.0 C") == 29.0 and temperature_c("300.15 K") == 27.0
    assert pressure_bar("200.0 bar") == 200.0 and pressure_bar("1500 mbar") == 1.5
    assert volume_l("11.1 l", None) == 11.1 and volume_l("11100 ml", None) == 11.1
    assert weight_kg("2.994 kg") == 2.994 and weight_kg("500 g") == 0.5
    assert percent("32.0%") == 32.0 and percent("12%") == 12.0
    assert depth_m(None) is None and temperature_c("") is None and pressure_bar("x") is None


def test_imperial_strings_are_converted():
    assert depth_m("98.4 ft") == 29.992
    assert temperature_c("82.4 F") == 28.0
    assert pressure_bar("3000.0 psi") == 206.843
    assert weight_kg("6.0 lbs") == 2.722
    # 80 cuft at 3000 psi is an AL80, 11.1 l of water; without the working pressure the size is unknown
    assert volume_l("80.0 cuft", 206.843) == 11.097
    assert volume_l("80.0 cuft", None) is None


def test_seconds_follow_subsurface_durations():
    assert seconds("45:30 min") == 2730 and seconds("0:09 min") == 9 and seconds("99:00 min") == 5940
    assert seconds("60") == 3600, "a bare number is minutes, as in Subsurface"
    assert seconds(None) is None and seconds("  ") is None


def test_parse_gps_both_forms():
    assert parse_gps("-10.123456 -30.654321") == (-10.123456, -30.654321)
    assert parse_gps("4.121367, 118.632924") == (4.121367, 118.632924)
    assert parse_gps("") == (None, None) and parse_gps("north") == (None, None) and parse_gps(None) == (None, None)


def test_parse_deco_model_forms():
    assert parse_deco_model("ZHL-16C GF 40/85") == ("ZHL-16C", 40, 85)          # the I6 writer
    assert parse_deco_model("Buhlmann ZHL-16C 40/85") == ("ZHL-16C", 40, 85)    # Subsurface's Garmin import
    assert parse_deco_model("GF 45/75") == (None, 45, 75)                       # Subsurface's Shearwater import
    assert parse_deco_model("VPM-B +2") == ("VPM-B +2", None, None)
    assert parse_deco_model("ZHL-16B") == ("ZHL-16B", None, None)
    assert parse_deco_model("ZHL-16C GF ?/85") == ("ZHL-16C", None, 85)
    assert parse_deco_model(None) == (None, None, None) and parse_deco_model("  ") == (None, None, None)


def test_split_model():
    assert split_model("Shearwater Perdix 2") == ("Shearwater", "Perdix 2")
    assert split_model("Garmin Descent Mk2(i)/Mk3(i)(S)/G1/G2/X50i") == ("Garmin", "Descent Mk2(i)/Mk3(i)(S)/G1/G2/X50i")
    assert split_model("Heinrichs Weikamp OSTC 3") == ("Heinrichs Weikamp", "OSTC 3")
    assert split_model("OSTC") == (None, "OSTC")
    assert split_model("manually added dive") == (None, None)
    assert split_model("") == (None, None) and split_model(None) == (None, None)


# --- round trip through the I6 writer -------------------------------------------

def test_synthetic_round_trip(tmp_path):
    """Writer -> reader on the synthetic dive: everything comes back but
    what the file cannot hold (zone, UTC instant, temp max, the visibility
    distance; the weight comes back in kg and the provenance is the file's),
    what the writer rounds (CNS to whole percent), a sample's heart rate on
    a sample without one (sticky), the computer's PO2 once the dive switched
    to CCR (po2 is the setpoint on a rebreather) and the tank-pod event,
    which comes back as a named alert."""
    dive = synthetic_dive()
    back, warnings = read_ssrf(_write([dive], tmp_path / "synthetic.ssrf"))
    assert warnings == [] and len(back) == 1
    again = back[0]
    mine, theirs = dive.model_dump(exclude={"samples", "events"}), again.model_dump(exclude={"samples", "events"})
    differs = {k for k in mine if mine[k] != theirs[k]}
    assert differs == {"date_time_utc", "timezone", "temp_max", "visibility", "visibility_unit", "weight", "weight_unit", "device_logged"}
    assert theirs["date_time_utc"] is None and theirs["timezone"] is None and theirs["temp_max"] is None and theirs["visibility"] is None
    assert (again.weight, again.weight_unit) == (2.994, "kilogram")   # 6.6 lb written in kg
    assert again.device_logged is True                                  # a named computer with a profile
    assert (again.exit_lat, again.exit_lng) == (-10.124, -30.654)
    assert again.service_fields == dive.service_fields
    assert again.external_ids == {"garmin": "123456789"}
    assert [(e.time, e.type, e.name, e.tank, e.value) for e in again.events] == [
        (0, "gas_switch", None, 0, None), (610, "alert", "Ascent rate", None, 3.0), (1300, "bookmark", "Turtle", None, None),
        (1500, "mode_change", "ccr", None, None), (1600, "setpoint_change", None, None, 1.3),
        (2000, "alert", "tank pod disconnected", 0, None), (2500, "gas_switch", None, 1, None)]
    assert again.events[-1].model_dump() == {"time": 2500, "type": "gas_switch", "tank": 1, "oxygen": 50.0, "helium": 0.0}
    assert len(again.samples) == len(dive.samples)
    seen: dict = {}
    for s1, s2 in zip(dive.samples, again.samples):
        assert (s2.time, s2.depth, s2.temp, s2.pressure) == (s1.time, s1.depth, s1.temp, s1.pressure)
        c1, c2 = _channels(s1), _channels(s2)
        assert c2.get("pressures", {}) == (c1.get("pressures") or ({0: s1.pressure} if s1.pressure else {}))
        for key in ("ndl", "tts", "gas_time"):   # sticky: a sample without the value keeps the last one
            if c1.get(key) is not None:
                seen[key] = c1[key]
            assert c2.get(key) == seen.get(key), (s1.time, key)
        for key in ("deco_stop_depth", "deco_stop_time", "cns"):   # a 0 is in the file only after a non-zero value, then sticky
            if c1.get(key) or (c1.get(key) is not None and ("written " + key) in seen):
                seen["written " + key] = True
                seen[key] = round(c1[key]) if key == "cns" else c1[key]
            assert c2.get(key) == seen.get(key), (s1.time, key)
        if s1.time < 1500:
            assert c2.get("ppo2") == c1.get("ppo2") and c2.get("setpoint") is None
        elif c1.get("ppo2") is not None:
            assert c2.get("setpoint") == c1["ppo2"] and c2.get("ppo2") is None
    # heart rate: sticky from 2400 s on; GF99 was dropped by the writer
    assert [_channels(s).get("heart_rate") for s in again.samples] == [72, 80, 88, 84, 84, 84, 84, 84]
    assert all(_channels(s).get("gf99") is None for s in again.samples)
    # the last sample (written from UnifiedSample.pressure alone) gets its pressure back, plus the sticky values
    assert again.samples[-1].pressure == 62.0 and _channels(again.samples[-1])["pressures"] == {0: 62.0}


def test_pinned_synthetic_file_reads_like_the_writer_output(tmp_path):
    pinned, _ = read_ssrf(PINNED)
    written, _ = read_ssrf(_write([synthetic_dive()], tmp_path / "again.ssrf"))
    assert pinned[0].model_dump() == written[0].model_dump()


def _compare_fit_round_trip(fit_path, tmp_path):
    """FIT -> .ssrf -> reader, field by field; returns the dive and its copy."""
    dive = read_fit(fit_path)
    path = tmp_path / "fit.ssrf"
    assert write_file([dive], "ssrf", path) == []
    result = read_file(path)
    assert result.format_id == "ssrf" and result.warnings == [] and len(result.dives) == 1
    again = result.dives[0]
    mine, theirs = dive.model_dump(exclude={"samples", "events"}), again.model_dump(exclude={"samples", "events"})
    differs = {k for k in mine if mine[k] != theirs[k]}
    # legitimately different: the zone and the UTC instant (no Subsurface slot), temp max/avg (the
    # profile holds them), GPS rounded to six decimals, the Subsurface adapter's five service fields
    assert differs == {"date_time_utc", "timezone", "temp_max", "temp_avg", "lat", "lng", "exit_lat", "exit_lng", "service_fields"}
    assert theirs["timezone"] is None and theirs["date_time_utc"] is None
    for key in ("lat", "lng", "exit_lat", "exit_lng"):
        assert theirs[key] == pytest.approx(mine[key], abs=1e-6)
    assert again.service_fields == {"suit": None, "divemaster": None, "rating": None, "visibility_stars": None, "tags": []}
    # carried fields: the computer, the deco settings, water, exit, bottom time, CNS, mode, external ids
    for key in ("computer_vendor", "computer_model", "computer_serial", "computer_firmware", "gf_low", "gf_high", "deco_model",
                "water_type", "water_density", "dive_mode", "surface_interval", "bottom_time", "cns_start", "cns_end",
                "external_ids", "dive_number", "duration", "date_time", "max_depth", "avg_depth", "temp_min", "device_logged"):
        assert theirs[key] == mine[key], key
    assert [g.model_dump() for g in again.gas_mixtures] == [g.model_dump() for g in dive.gas_mixtures]
    # events: same times, tanks and names; the tank-pod events come back as alerts named by the writer
    assert len(again.events) == len(dive.events)
    for e1, e2 in zip(dive.events, again.events):
        assert (e2.time, e2.tank, e2.value) == (e1.time, e1.tank, e1.value)
        if e1.type.startswith("tank_pod"):
            assert (e2.type, e2.name) == ("alert", e1.type.replace("_", " "))
        else:
            assert (e2.type, e2.name, e2.oxygen, e2.helium) == (e1.type, e1.name, e1.oxygen, e1.helium)
    # samples: time, depth, temperature and every pressure identical; the channels too, except that the
    # file has no "0" (CNS 0 %, a 0 m next stop, a 0 s stop time: Subsurface writes none of them) and a
    # record without an NDL reading takes the previous one (sticky)
    assert len(again.samples) == len(dive.samples)
    sticky_ndl = 0
    for s1, s2 in zip(dive.samples, again.samples):
        assert (s2.time, s2.temp, s2.pressure) == (s1.time, s1.temp, s1.pressure)
        assert s2.depth == pytest.approx(s1.depth, abs=0.0006)
        c1, c2 = _channels(s1), _channels(s2)
        assert c2.get("pressures", {}) == c1.get("pressures", {})
        for key in ("tts", "ppo2", "gas_time", "heart_rate"):
            assert c2.get(key) == c1.get(key), (s1.time, key)
        assert c2.get("cns") == (c1.get("cns") or None), s1.time
        assert c2.get("deco_stop_depth") == (c1.get("deco_stop_depth") or None), s1.time
        assert c2.get("deco_stop_time") == (c1.get("deco_stop_time") or None), s1.time
        if c1.get("ndl") is not None:
            assert c2.get("ndl") == c1["ndl"], s1.time
        elif c2.get("ndl") is not None:
            sticky_ndl += 1
    return dive, again, sticky_ndl


@fixtures
def test_fit_two_tanks_round_trip(tmp_path):
    dive, again, sticky_ndl = _compare_fit_round_trip(TWO_TANKS, tmp_path)
    assert again.dive_number == 38 and len(again.samples) == 331 and sticky_ndl == 99
    assert sum(len(_channels(s).get("pressures", {})) for s in again.samples) == 494
    assert again.water_type == "salt" and again.water_density == 1025.0
    assert [e.type for e in again.events][:3] == ["gas_switch", "alert", "alert"]
    assert [e.name for e in again.events][:3] == [None, "tank pod connected", "tank pod connected"]


@fixtures
def test_fit_single_gas_round_trip(tmp_path):
    dive, again, sticky_ndl = _compare_fit_round_trip(SINGLE_GAS, tmp_path)
    assert again.dive_number == 28 and len(again.samples) == 386 and sticky_ndl == 132
    assert again.gas_mixtures[0].oxygen == 32.0 and again.events[0].oxygen == 32.0


@fixtures
def test_fit_fixtures_in_one_file_read_back_in_order(tmp_path):
    path = _write([read_fit(SINGLE_GAS), read_fit(TWO_TANKS)], tmp_path / "both.ssrf")
    dives, warnings = read_ssrf(path)
    assert warnings == [] and [d.dive_number for d in dives] == [28, 38]
    assert dives[0].computer_serial == dives[1].computer_serial == "1000000001"
    assert (dives[0].lat, dives[0].lng) == (-10.0, -30.0) and dives[0].location is None


# --- the hand-written file: what the writer never produces ----------------------

@pytest.fixture(scope="module")
def handwritten():
    return read_ssrf(HANDWRITTEN)


def test_handwritten_trips_flattened_and_warnings(handwritten):
    dives, warnings = handwritten
    assert [d.dive_number for d in dives] == [101, 102, 103, 104, None]
    assert warnings == [
        "handwritten.ssrf: dive 101: only the first dive computer (Garmin Descent Mk2(i)/Mk3(i)(S)/G1/G2/X50i) is read; 1 more not read: Shearwater Perdix",
        "handwritten.ssrf: dive 104: cylinder 1 is sized in cubic feet without a working pressure; its size is not read",
        "handwritten.ssrf: dive 105 has no date and is skipped",
    ]


def test_handwritten_subsurface_garmin_import(handwritten):
    """Dive 101: what Subsurface's own Garmin import writes - the family
    model name with the specific one in extradata, GPS1/GPS2, Subsurface's
    deco-model text, the dive's cns attribute, a legacy type-11 gas change
    with the mix in its value, cylinder pressures from the profile."""
    d = handwritten[0][0]
    assert (d.date_time, d.duration, d.max_depth, d.avg_depth, d.temp_min) == (datetime(2025, 7, 1, 8, 12, 30), 2530, 23.125, 11.267, 30.0)
    assert (d.location, d.lat, d.lng) == ("Invented Wall", 1.4, 2.4), "a site without GPS: the entry position comes from GPS1"
    assert (d.exit_lat, d.exit_lng) == (1.401, 2.401)
    assert (d.computer_vendor, d.computer_model, d.computer_serial, d.computer_firmware) == ("Garmin", "Descent X50i", "1000000001", "5.12")
    assert (d.deco_model, d.gf_low, d.gf_high) == ("ZHL-16C", 40, 85)
    assert (d.water_type, d.water_density, d.dive_mode, d.cns_end, d.cns_start, d.bottom_time) == ("salt", 1025.0, "oc_single_gas", 2.0, None, None)
    assert d.buddy == "B. Uddy" and d.notes == "Garmin import as Subsurface writes it" and d.device_logged is True
    assert d.external_ids == {} and d.surface_interval is None
    assert [g.model_dump() for g in d.gas_mixtures] == [
        {"oxygen": 32.0, "helium": 0.0, "start_pressure": 201.0, "end_pressure": 60.0, "tank_volume": 11.1, "tank_name": "Steel 12", "tank_role": None}]
    assert [e.model_dump() for e in d.events] == [
        {"time": 1, "type": "gas_switch", "tank": 0, "oxygen": 32.0, "helium": 0.0},
        {"time": 1585, "type": "alert", "name": "Ascent speed critical"},
        {"time": 2400, "type": "alert", "name": "Surface"}]


def test_handwritten_sticky_values(handwritten):
    """Dive 101's samples: temperature, NDL, TTS, PO2 and heart rate persist
    until the next written value; a pressure does not."""
    samples = handwritten[0][0].samples
    assert [s.model_dump() for s in samples] == [
        {"depth": 1.233, "temp": 30.0, "time": 1, "pressure": 201.0, "channels": {"pressures": {0: 201.0}, "ppo2": 0.7, "heart_rate": 70}},
        {"depth": 5.0, "temp": 30.0, "time": 10, "pressure": None, "channels": {"ndl": 5940, "tts": 9, "ppo2": 0.7, "heart_rate": 75}},
        {"depth": 10.0, "temp": 30.0, "time": 20, "pressure": 195.0, "channels": {"pressures": {0: 195.0}, "ndl": 2400, "tts": 9, "ppo2": 0.7, "heart_rate": 75}},
        {"depth": 15.0, "temp": 29.0, "time": 30, "pressure": None, "channels": {"ndl": 2400, "tts": 9, "ppo2": 1.0, "heart_rate": 75}},
        {"depth": 0.0, "temp": 29.0, "time": 2530, "pressure": 60.0, "channels": {"pressures": {0: 60.0}, "ndl": 2400, "tts": 0, "ppo2": 1.0, "heart_rate": 75}},
    ]


def test_handwritten_imperial_dive(handwritten):
    """Dive 102: feet, Fahrenheit, psi, cubic feet with a working pressure,
    pounds; the legacy ``pressure`` attribute; the Subsurface adapter's
    service fields; a sample whose only extra is the tank pressure."""
    d = handwritten[0][1]
    assert (d.max_depth, d.avg_depth, d.temp_min) == (29.992, 18.288, 28.0)
    assert (d.weight, d.weight_unit) == (3.629, "kilogram")   # 6 + 2 lb
    assert [g.model_dump() for g in d.gas_mixtures] == [
        {"oxygen": 32.0, "helium": 0.0, "start_pressure": 199.948, "end_pressure": 48.263, "tank_volume": 11.097, "tank_name": "AL80", "tank_role": None}]
    assert d.service_fields == {"suit": "3mm shorty", "divemaster": "D. Master", "rating": 3, "visibility_stars": 4, "tags": ["training", "wreck"]}
    assert (d.location, d.lat, d.lng) == ("Imperial Pinnacle", 1.5, 2.5)
    assert (d.computer_vendor, d.computer_model, d.computer_serial, d.computer_firmware) == ("Suunto", "EON Steel", "EON-000001", "2.5.4")
    assert d.samples[0].model_dump() == {"depth": 0.0, "temp": 30.0, "time": 0, "pressure": 199.948}, "pressure set, channels None"
    assert d.samples[1].model_dump() == {"depth": 9.997, "temp": 30.0, "time": 60, "pressure": 193.053, "channels": {"pressures": {0: 193.053}, "ndl": 3600}}
    assert d.samples[2].depth == 29.992 and d.samples[2].temp == 28.0 and d.samples[2].channels.ndl == 720
    assert d.events == [] and d.dive_mode == "oc_single_gas" and d.water_type is None and d.water_density is None


def test_handwritten_ccr_dive(handwritten):
    """Dive 103: dctype CCR, cylinder uses, the sensor remap and the legacy
    o2pressure, po2 as the setpoint with dc_supplied_ppo2 as the PO2, the
    O2 sensors ignored, modechange / SP change / bookmark / unknown events,
    the dive's watersalinity, a blank dive_sync id skipped."""
    d = handwritten[0][2]
    assert d.dive_mode == "ccr" and (d.water_type, d.water_density) == ("salt", 1030.0)
    assert (d.deco_model, d.gf_low, d.gf_high) == (None, 45, 75)
    assert d.external_ids == {"garmin": "424242"}
    assert (d.computer_vendor, d.computer_model, d.computer_serial, d.computer_firmware) == ("Shearwater", "Petrel 2", "5555AAAA", "71")
    assert [(g.tank_name, g.tank_role, g.oxygen, g.start_pressure, g.end_pressure) for g in d.gas_mixtures] == [
        ("Diluent", "diluent", 21.0, 200.0, None),          # its line's start; pressure0 is remapped away from it
        ("Oxygen", "oxygen", 100.0, 180.0, 150.0),          # o2pressure, the oxygen cylinder's, first and last
        ("Bailout", "bailout", 50.0, 150.0, 110.0),         # pressure0 with sensor0='2'
        ("Spare", "not_used", 21.0, None, None)]
    assert [e.model_dump() for e in d.events] == [
        {"time": 0, "type": "gas_switch", "tank": 0, "oxygen": 21.0, "helium": 0.0},
        {"time": 1800, "type": "mode_change", "name": "oc_multi_gas"},
        {"time": 1810, "type": "gas_switch", "tank": 2, "oxygen": 50.0, "helium": 0.0},
        {"time": 2700, "type": "mode_change", "name": "ccr"},
        {"time": 2760, "type": "setpoint_change", "value": 1.3},
        {"time": 3000, "type": "bookmark"},
        {"time": 3300, "type": "alert", "name": "ascent"},
        {"time": 3360, "type": "alert", "value": 7.0}]
    s = d.samples
    assert [x.pressure for x in s] == [180.0, 170.0, 165.0, None, None, 150.0], "the lowest-numbered tank with readings"
    assert _channels(s[0]) == {"pressures": {1: 180.0, 2: 150.0}, "ppo2": 0.7, "setpoint": 0.7}
    assert _channels(s[1]) == {"pressures": {1: 170.0, 2: 148.0}, "deco_stop_depth": 6.0, "deco_stop_time": 180, "cns": 5.0, "ppo2": 0.7, "setpoint": 1.2}
    # on bailout (OC) the computer's po2 is still its setpoint: it reports its PO2 separately
    assert _channels(s[2]) == {"pressures": {1: 165.0, 2: 140.0}, "deco_stop_depth": 6.0, "deco_stop_time": 180, "cns": 5.0, "ppo2": 0.7, "setpoint": 0.9}
    assert _channels(s[4]) == {"deco_stop_depth": 0.0, "deco_stop_time": 0, "cns": 9.0, "ppo2": 0.7, "setpoint": 1.3}
    assert all(x.temp == 14.0 for x in s)


def test_handwritten_legacy_dive_without_divecomputer(handwritten):
    """Dive 104: the pre-divecomputer layout (samples, depth and temperature
    under the dive, a <location> element with gps), no duration attribute."""
    d = handwritten[0][3]
    assert (d.location, d.lat, d.lng) == ("Old Reef", 1.6, 2.6)
    assert (d.duration, d.max_depth, d.avg_depth, d.temp_min) == (1500, 12.0, 8.0, 20.0)
    assert [(s.time, s.depth, s.temp) for s in d.samples] == [(0, 0.0, None), (300, 12.0, None), (1500, 0.0, None)]
    assert d.gas_mixtures[0].tank_volume is None and d.gas_mixtures[0].tank_name == "rental"
    assert (d.computer_vendor, d.computer_model, d.device_logged) == (None, None, None)


def test_handwritten_hand_logged_dive(handwritten):
    d = handwritten[0][4]
    assert d.dive_number is None and d.notes == "Logged by hand" and d.device_logged is False
    assert (d.duration, d.max_depth, d.avg_depth, d.samples, d.events) == (1200, 5.0, 3.0, [], [])
    assert (d.computer_vendor, d.computer_model, d.computer_serial) == (None, None, None)


# --- edge cases on small strings -------------------------------------------------

def test_not_a_divelog_and_empty_log():
    with pytest.raises(SsrfReadError, match=r"x.xml: not a Subsurface divelog \(root element <uddf>\)"):
        read_ssrf_string("<uddf/>", "x.xml")
    assert read_ssrf_string("<divelog program='subsurface' version='3'><dives/></divelog>") == ([], [])
    assert read_ssrf_string("<divelog/>") == ([], [])


def test_namespace_is_stripped_and_time_without_seconds():
    text = ("<divelog xmlns='urn:x' program='subsurface' version='3'><dives>"
            "<dive number='7' date='2026-01-02' time='10:30'><divecomputer model='manually added dive'><depth max='9.0 m'/></divecomputer></dive>"
            "</dives></divelog>")
    dives, warnings = read_ssrf_string(text)
    assert warnings == [] and dives[0].date_time == datetime(2026, 1, 2, 10, 30) and dives[0].max_depth == 9.0


def test_dive_label_for_warnings_without_number():
    text = ("<divelog><dives><dive date='2026-01-02' time='10:30:00' duration='10:00 min'>"
            "<divecomputer model='A B'/><divecomputer model='C D'/></dive>"
            "<dive><notes>no date at all</notes></dive></dives></divelog>")
    dives, warnings = read_ssrf_string(text, "log.ssrf")
    assert len(dives) == 1 and warnings == [
        "log.ssrf: dive of 2026-01-02 10:30: only the first dive computer (A B) is read; 1 more not read: C D",
        "log.ssrf: dive #2 has no date and is skipped"]


def test_gas_change_without_cylinder_matches_the_mix_and_oc_mode_by_mixes():
    text = ("<divelog><dives><dive date='2026-01-02' time='10:30:00' duration='10:00 min'>"
            "<cylinder size='11.1 l' /><cylinder size='7.0 l' o2='50.0%' />"
            "<divecomputer model='Test Unit'>"
            "<event time='1:00 min' type='25' name='gaschange' o2='50.0%' />"
            "<event time='2:00 min' type='25' flags='9' name='gaschange' o2='18.0%' he='45.0%' />"
            "<event time='3:00 min' type='8' divemode='OC' name='modechange' />"
            "<sample time='0:00 min' depth='0.0 m' /></divecomputer></dive></dives></divelog>")
    dives, _ = read_ssrf_string(text)
    d = dives[0]
    assert d.dive_mode == "oc_multi_gas"
    assert [e.model_dump() for e in d.events] == [
        {"time": 60, "type": "gas_switch", "tank": 1, "oxygen": 50.0, "helium": 0.0},
        {"time": 120, "type": "gas_switch", "oxygen": 18.0, "helium": 45.0},   # flags name a cylinder the dive has not
        {"time": 180, "type": "mode_change", "name": "oc_multi_gas"}]


def test_po2_is_the_computers_po2_on_an_open_circuit_dive_and_the_setpoint_on_ccr():
    base = ("<divelog><dives><dive date='2026-01-02' time='10:30:00' duration='10:00 min'><cylinder size='11.1 l' />"
            "<divecomputer model='Test Unit'{dctype}><sample time='0:00 min' depth='0.0 m' po2='0.21 bar' />"
            "<sample time='1:00 min' depth='10.0 m' /></divecomputer></dive></dives></divelog>")
    oc = read_ssrf_string(base.format(dctype=""))[0][0]
    assert [_channels(s) for s in oc.samples] == [{"ppo2": 0.21}, {"ppo2": 0.21}] and oc.dive_mode == "oc_single_gas"
    ccr = read_ssrf_string(base.format(dctype=" dctype='CCR'"))[0][0]
    assert [_channels(s) for s in ccr.samples] == [{"setpoint": 0.21}, {"setpoint": 0.21}] and ccr.dive_mode == "ccr"
    assert read_ssrf_string(base.format(dctype=" dctype='PSCR'"))[0][0].dive_mode == "scr"
    assert read_ssrf_string(base.format(dctype=" dctype='Freedive'"))[0][0].dive_mode == "apnea"


def test_in_deco_without_stopdepth_and_zero_pressure_is_no_reading():
    text = ("<divelog><dives><dive date='2026-01-02' time='10:30:00'><cylinder size='11.1 l' />"
            "<divecomputer model='Test Unit'>"
            "<sample time='0:00 min' depth='0.0 m' pressure0='0.0 bar' in_deco='0' />"
            "<sample time='1:00 min' depth='20.0 m' in_deco='1' stoptime='2:00 min' />"
            "<sample time='2:00 min' depth='20.0 m' pressure0='150.0 bar' />"
            "</divecomputer></dive></dives></divelog>")
    d = read_ssrf_string(text)[0][0]
    assert [s.model_dump() for s in d.samples] == [
        {"depth": 0.0, "temp": None, "time": 0, "pressure": None, "channels": {"deco_stop_depth": 0.0}},
        {"depth": 20.0, "temp": None, "time": 60, "pressure": None, "channels": {"deco_stop_time": 120}},   # in deco, depth unknown
        {"depth": 20.0, "temp": None, "time": 120, "pressure": 150.0, "channels": {"pressures": {0: 150.0}, "deco_stop_time": 120}}]
    assert d.duration == 120 and d.max_depth == 20.0 and d.gas_mixtures[0].start_pressure == 150.0


def test_writer_extradata_keys_read_back():
    text = ("<divelog><dives><dive date='2026-01-02' time='10:30:00' duration='30:00 min'>"
            "<divecomputer model='Test Unit'>"
            "<extradata key='Dive mode' value='gauge' /><extradata key='Water type' value='custom' />"
            "<extradata key='Bottom time' value='25:30 min' /><extradata key='CNS start' value='3.0%' /><extradata key='CNS end' value='10.5%' />"
            "<extradata key='dive_sync:divelogs' value='987' /><extradata key='dive_sync:' value='x' />"
            "<water salinity='1017 g/l' /><surfacetime>78:10 min</surfacetime>"
            "</divecomputer></dive></dives></divelog>")
    d = read_ssrf_string(text)[0][0]
    assert (d.dive_mode, d.water_type, d.water_density, d.bottom_time, d.cns_start, d.cns_end) == ("gauge", "custom", 1017.0, 1530, 3.0, 10.5)
    assert d.external_ids == {"divelogs": "987"} and d.surface_interval == 4690
    assert (d.computer_vendor, d.computer_model, d.computer_serial, d.computer_firmware) == ("Test", "Unit", None, None)


def test_parse_ssrf_on_an_element():
    root = ET.fromstring("<divelog><dives><dive number='3' date='2026-01-02' time='10:30:00' duration='1:00 min'/></dives></divelog>")
    dives, warnings = parse_ssrf(root)
    assert [d.dive_number for d in dives] == [3] and warnings == [] and dives[0].samples == [] and dives[0].max_depth == 0.0


# --- the registry ----------------------------------------------------------------

def test_read_file_ssrf_through_the_registry(tmp_path):
    result = read_file(HANDWRITTEN)
    assert result.format_id == "ssrf" and [d.dive_number for d in result.dives] == [101, 102, 103, 104, None]
    assert len(result.warnings) == 3 and result.files == [(HANDWRITTEN, "ssrf")]
    as_xml = tmp_path / "export.xml"
    as_xml.write_bytes(open(HANDWRITTEN, "rb").read())
    assert read_file(as_xml).format_id == "ssrf"
    empty = tmp_path / "empty.ssrf"
    empty.write_text("<divelog program='subsurface' version='3'><dives/></divelog>")
    assert read_file(empty).warnings == ["empty.ssrf: the file holds no dive"]


def test_read_files_warns_for_an_ssrf_that_is_no_divelog(tmp_path):
    bad = tmp_path / "bad.ssrf"
    bad.write_text("<uddf xmlns='http://www.streit.cc/uddf/3.2/'/>")
    with pytest.raises(SsrfReadError):
        read_file(bad)
    result = read_files([HANDWRITTEN, bad])
    assert len(result.dives) == 5 and result.warnings[-1] == "bad.ssrf: not a Subsurface divelog (root element <uddf>)"
    worse = tmp_path / "worse.ssrf"
    worse.write_text("<gpx/>")
    with pytest.raises(UnsupportedFileError, match="none of the files could be read: bad.ssrf: not a Subsurface divelog"):
        read_files([bad, worse])


def test_registry_says_ssrf_is_read_and_written():
    ssrf = formats.get_format("ssrf")
    assert (ssrf.can_read, ssrf.can_write) == (True, True)
    assert [f.id for f in formats.readable_formats()] == ["fit", "uddf", "ssrf"]
    assert formats.open_name_filters()[0] == "Dive files (*.fit *.zip *.uddf *.xml *.ssrf)"
    assert "Subsurface (*.ssrf *.xml)" in formats.open_name_filters()


# --- the owner's local exports (read-only, skipped when absent) -------------------

def _uwmedia_files():
    return sorted(glob.glob(os.path.join(UWMEDIA_LOGS, "ssrf", "*.ssrf"))
                  + glob.glob(os.path.join(UWMEDIA_LOGS, "submersion_dives", "*.ssrf.xml")))


@pytest.mark.skipif(not _uwmedia_files(), reason="UWMedia's local .ssrf exports not present")
def test_uwmedia_local_exports_read_without_warnings():
    """Subsurface's own export of Garmin dives and Submersion's .ssrf.xml
    exports (an OC deco dive, two CCR dives): each holds one dive with a
    profile, a computer and a deco model; the CCR ones read po2 as the
    setpoint. Nothing from these files is written or asserted verbatim."""
    for path in _uwmedia_files():
        result = read_file(path)
        assert result.format_id == "ssrf" and result.warnings == [], path
        (dive,) = result.dives
        assert isinstance(dive, UnifiedDive) and dive.dive_number and dive.duration > 0 and dive.max_depth > 0
        assert len(dive.samples) > 100 and all(s.time is not None for s in dive.samples)
        assert dive.gas_mixtures and dive.computer_model and dive.gf_low and dive.gf_high
        assert dive.events and dive.events[0].type == "gas_switch" and dive.events[0].tank is not None
        if dive.dive_mode == "ccr":
            assert any(s.channels and s.channels.setpoint for s in dive.samples)
            assert not any(s.channels and s.channels.ppo2 and s.channels.setpoint is None for s in dive.samples)
        else:
            assert dive.dive_mode in ("oc_single_gas", "oc_multi_gas")
            assert not any(s.channels and s.channels.setpoint for s in dive.samples)
        assert dive.water_density in (1000.0, 1020.0, 1025.0, 1030.0) and dive.water_type is not None
