"""The Subsurface ``.ssrf`` writer ``src/core/convert/ssrf_writer.py``
(plans/convert.md I6) and its wiring in ``formats.py``.

A synthetic dive with every field the writer knows is pinned byte for byte
against ``tests/data/ssrf/synthetic.ssrf`` (invented data, written by this
writer and read by eye against Subsurface's own files); the anonymised FIT
fixtures of I2 are written and parsed when they are on disk. Nothing here
touches a live service or the owner's files.
"""
import os
from datetime import datetime
from xml.etree import ElementTree as ET

import pytest

from src.core.convert import formats
from src.core.convert.fit_reader import read_fit
from src.core.convert.formats import write_each, write_file
from src.core.convert.ssrf_writer import (
    EXTRA_BOTTOM_TIME,
    EXTRA_CNS_END,
    EXTRA_CNS_START,
    EXTRA_DECO_MODEL,
    EXTRA_DIVE_MODE,
    EXTRA_EXIT_GPS,
    EXTRA_FIRMWARE,
    EXTRA_SERIAL,
    EXTRA_WATER_TYPE,
    SsrfDocument,
    fmt_degrees,
    fmt_min,
    fmt_percent,
    hex_id,
    ssrf_drops,
    write_ssrf,
    xml_quote,
)
from src.core.models import DiveEvent, GasMixture, SampleChannels, UnifiedDive, UnifiedSample
from src.core.services.subsurface import MANUAL_DC_MODEL

DATA = os.path.join(os.path.dirname(__file__), "data")
FIT_DIR = os.path.join(DATA, "garmin_fit")
SINGLE_GAS = os.path.join(FIT_DIR, "single_gas.fit")
TWO_TANKS = os.path.join(FIT_DIR, "two_tanks.fit")
PINNED = os.path.join(DATA, "ssrf", "synthetic.ssrf")
fixtures = pytest.mark.skipif(not os.path.isfile(TWO_TANKS), reason="Garmin FIT fixtures not present")


def _ch(**kw):
    return SampleChannels(**kw)


def synthetic_dive() -> UnifiedDive:
    """An invented dive touching every field the writer knows: two tanks
    with a switch, every event kind, every sample channel, a named site
    with GPS, an exit position, a weight in pounds, Subsurface's own
    service fields and a foreign id. The texts hold the characters the
    writer must escape."""
    return UnifiedDive(
        date_time=datetime(2026, 3, 14, 9, 30, 5),
        date_time_utc=datetime(2026, 3, 14, 1, 30, 5),
        timezone="Etc/GMT-8",
        duration=48 * 60 + 20,
        max_depth=31.45,
        avg_depth=14.2,
        temp_min=24.0,
        temp_max=27.5,
        external_ids={"garmin": "123456789"},
        gas_mixtures=[
            GasMixture(oxygen=32.0, helium=0.0, start_pressure=201.3, end_pressure=62.0, tank_volume=11.1, tank_name="Tank 1"),
            GasMixture(oxygen=50.0, helium=0.0, start_pressure=180.0, end_pressure=150.5, tank_volume=7.0, tank_name="Deco <50>", tank_role="bailout"),
        ],
        location="Test Reef & Wall 'North'",
        notes="Line one <with> & \"quotes\"\nline two\twith a tab",
        dive_number=42,
        weight=6.6,
        weight_unit="pound",
        visibility=15.0,
        visibility_unit="meter",
        buddy="A. Buddy",
        lat=-10.123456,
        lng=-30.654321,
        samples=[
            UnifiedSample(depth=0.0, temp=27.5, time=0, pressure=201.3,
                          channels=_ch(pressures={0: 201.3, 1: 180.0}, ndl=5940, tts=60, deco_stop_depth=0.0, deco_stop_time=0, cns=0.0, ppo2=0.32, heart_rate=72)),
            UnifiedSample(depth=12.5, temp=27.5, time=120, pressure=190.0,
                          channels=_ch(pressures={0: 190.0, 1: 180.0}, ndl=1800, tts=180, deco_stop_depth=0.0, deco_stop_time=0, cns=1.0, ppo2=0.72, gas_time=3600, heart_rate=80)),
            UnifiedSample(depth=31.45, temp=24.0, time=600, pressure=150.0,
                          channels=_ch(pressures={0: 150.0, 1: 180.0}, ndl=0, tts=900, deco_stop_depth=6.0, deco_stop_time=180, cns=8.4, ppo2=1.33, gas_time=2400, heart_rate=88, gf99=35.0)),
            UnifiedSample(depth=21.0, temp=24.0, time=1200, pressure=110.0,
                          channels=_ch(pressures={0: 110.0, 1: 180.0}, ndl=0, tts=600, deco_stop_depth=6.0, deco_stop_time=120, cns=11.0, ppo2=0.99, gas_time=1800, heart_rate=84)),
            UnifiedSample(depth=6.0, temp=25.0, time=2400, pressure=80.0,
                          channels=_ch(pressures={0: 80.0, 1: 175.0}, ndl=0, tts=240, deco_stop_depth=6.0, deco_stop_time=60, cns=12.0, ppo2=0.8, gas_time=1500)),
            UnifiedSample(depth=6.0, temp=25.0, time=2600,
                          channels=_ch(pressures={1: 160.0}, ndl=0, tts=120, deco_stop_depth=6.0, deco_stop_time=0, cns=12.0, ppo2=0.8)),
            UnifiedSample(depth=3.0, temp=26.0, time=2800,
                          channels=_ch(pressures={1: 152.0}, ndl=5940, tts=30, deco_stop_depth=0.0, deco_stop_time=0, cns=12.0, ppo2=0.65)),
            UnifiedSample(depth=0.0, temp=26.0, time=2900, pressure=62.0),
        ],
        service_fields={"rating": 4, "visibility_stars": 3, "tags": ["reef", "deco"], "suit": "5mm wet", "divemaster": "D. Master"},
        events=[
            DiveEvent(time=0, type="gas_switch", tank=0, oxygen=32.0, helium=0.0),
            DiveEvent(time=610, type="alert", name="Ascent rate", value=3),
            DiveEvent(time=2500, type="gas_switch", tank=1),
            DiveEvent(time=1300, type="bookmark", name="Turtle"),
            DiveEvent(time=1500, type="mode_change", name="ccr"),
            DiveEvent(time=1600, type="setpoint_change", value=1.3),
            DiveEvent(time=2000, type="tank_pod_disconnected", tank=0),
        ],
        computer_vendor="Shearwater",
        computer_model="Perdix 2",
        computer_serial="1000000001",
        computer_firmware="93",
        gf_low=40,
        gf_high=85,
        deco_model="ZHL-16C",
        water_type="fresh",
        dive_mode="oc_multi_gas",
        exit_lat=-10.1240,
        exit_lng=-30.6540,
        surface_interval=90 * 60,
        bottom_time=40 * 60 + 10,
        cns_start=1.0,
        cns_end=12.0,
    )


def _write(dives, path):
    document = SsrfDocument()
    for dive in dives:
        document.add_dive(dive)
    document.save(path)
    return open(path, "r", encoding="utf-8", newline="").read()


def _root(text: str) -> ET.Element:
    return ET.fromstring(text)


def _dive_elements(text: str):
    return _root(text).findall("dives/dive")


def _samples(dive_el):
    return dive_el.findall("divecomputer/sample")


# ---------------------------------------------------------------------------
# Value formats
# ---------------------------------------------------------------------------

def test_fmt_min_is_subsurfaces_duration():
    assert fmt_min(0) == "0:00 min"
    assert fmt_min(61) == "1:01 min"
    assert fmt_min(3065) == "51:05 min"
    assert fmt_min(-5) == "0:00 min"


def test_fmt_percent_one_decimal_from_permille():
    assert fmt_percent(32.0) == "32.0%"
    assert fmt_percent(18.5) == "18.5%"
    assert fmt_percent(21.04) == "21.0%"
    assert fmt_percent(100) == "100.0%"


def test_fmt_degrees_six_decimals_signed():
    assert fmt_degrees(-10.0) == "-10.000000"
    assert fmt_degrees(4.1198614) == "4.119861"
    assert fmt_degrees(0.0000004) == "0.000000"
    assert fmt_degrees(-0.0000006) == "-0.000001"


def test_hex_id_is_stable_and_never_shorter_than_eight_digits():
    assert hex_id("a", 1) == hex_id("a", 1)
    assert hex_id("a", 1) != hex_id("a", 2)
    for parts in (("x",), ("site", "", "", ""), ("device", "Garmin", "1")):
        value = hex_id(*parts)
        assert len(value) == 8 and int(value, 16) >= 0x10000000


def test_xml_quote_follows_subsurface():
    assert xml_quote("a < b > c & d") == "a &lt; b &gt; c &amp; d"
    assert xml_quote("it's \"q\"") == "it&apos;s &quot;q&quot;"
    assert xml_quote("it's \"q\"", attribute=False) == "it's \"q\""
    assert xml_quote("tab\tnl\ncr\r") == "tab\tnl\ncr\r"
    assert xml_quote("bell\x07 esc\x1b") == "bell? esc?"


# ---------------------------------------------------------------------------
# The synthetic dive
# ---------------------------------------------------------------------------

def test_synthetic_dive_is_pinned_byte_for_byte(tmp_path):
    text = _write([synthetic_dive()], tmp_path / "synthetic.ssrf")
    with open(PINNED, "r", encoding="utf-8", newline="") as f:
        expected = f.read()
    assert text == expected


def test_synthetic_dive_structure(tmp_path):
    text = _write([synthetic_dive()], tmp_path / "s.ssrf")
    root = _root(text)
    assert root.tag == "divelog" and root.get("program") == "subsurface" and root.get("version") == "3"
    assert [c.tag for c in root] == ["settings", "divesites", "dives"]
    (device,) = root.findall("settings/divecomputerid")
    assert device.attrib == {"model": "Shearwater Perdix 2", "deviceid": device.get("deviceid"), "serial": "1000000001", "firmware": "93"}
    (site,) = root.findall("divesites/site")
    assert site.get("name") == "Test Reef & Wall 'North'"
    assert site.get("gps") == "-10.123456 -30.654321"
    (dive,) = _dive_elements(text)
    assert dive.get("number") == "42"
    assert dive.get("rating") == "4" and dive.get("visibility") == "3"
    assert dive.get("tags") == "reef, deco"
    assert dive.get("divesiteid") == site.get("uuid")
    assert (dive.get("date"), dive.get("time"), dive.get("duration")) == ("2026-03-14", "09:30:05", "48:20 min")
    assert [c.tag for c in dive] == ["divemaster", "buddy", "notes", "suit", "cylinder", "cylinder", "weightsystem", "divecomputer"]
    assert dive.find("notes").text == "Line one <with> & \"quotes\"\nline two\twith a tab"
    assert dive.find("buddy").text == "A. Buddy"
    cylinders = dive.findall("cylinder")
    assert cylinders[0].attrib == {"size": "11.1 l", "description": "Tank 1", "o2": "32.0%", "start": "201.3 bar", "end": "62.0 bar"}
    assert cylinders[1].attrib == {"size": "7.0 l", "description": "Deco <50>", "o2": "50.0%", "start": "180.0 bar", "end": "150.5 bar", "use": "bailout"}
    assert dive.find("weightsystem").attrib == {"weight": "2.994 kg", "description": "weight"}
    dc = dive.find("divecomputer")
    assert dc.get("model") == "Shearwater Perdix 2"
    assert dc.get("deviceid") == device.get("deviceid")
    assert len(dc.get("diveid")) == 8 and "dctype" not in dc.attrib
    assert dc.find("depth").attrib == {"max": "31.45 m", "mean": "14.2 m"}
    assert dc.find("temperature").attrib == {"water": "24.0 C"}
    assert dc.find("water").attrib == {"salinity": "1000 g/l"}
    assert dc.find("surfacetime").text == "90:00 min"
    extra = [(e.get("key"), e.get("value")) for e in dc.findall("extradata")]
    assert extra == [
        (EXTRA_DECO_MODEL, "ZHL-16C GF 40/85"), (EXTRA_SERIAL, "1000000001"), (EXTRA_FIRMWARE, "93"),
        (EXTRA_DIVE_MODE, "oc_multi_gas"), (EXTRA_EXIT_GPS, "-10.124000, -30.654000"),
        (EXTRA_BOTTOM_TIME, "40:10 min"), (EXTRA_CNS_START, "1.0%"), (EXTRA_CNS_END, "12.0%"),
        ("dive_sync:garmin", "123456789"),
    ]
    # the tail of the computer: extradata, then every event in time order, then the samples
    kinds = [c.tag for c in dc]
    assert kinds == ["depth", "temperature", "water", "surfacetime"] + ["extradata"] * 9 + ["event"] * 7 + ["sample"] * 8


def test_synthetic_dive_events(tmp_path):
    text = _write([synthetic_dive()], tmp_path / "s.ssrf")
    events = [e.attrib for e in _dive_elements(text)[0].findall("divecomputer/event")]
    assert events == [
        {"time": "0:00 min", "type": "25", "flags": "1", "name": "gaschange", "cylinder": "0", "o2": "32.0%"},
        {"time": "10:10 min", "type": "26", "value": "3", "name": "Ascent rate"},
        {"time": "21:40 min", "type": "8", "name": "Turtle"},
        {"time": "25:00 min", "type": "8", "divemode": "CCR", "name": "modechange"},
        {"time": "26:40 min", "type": "20", "value": "1300", "name": "SP change"},
        {"time": "33:20 min", "type": "26", "name": "tank pod disconnected", "cylinder": "0"},
        # the switch to tank 1 names no mix of its own: the cylinder's is written
        {"time": "41:40 min", "type": "25", "flags": "2", "name": "gaschange", "cylinder": "1", "o2": "50.0%"},
    ]


def test_synthetic_dive_samples(tmp_path):
    text = _write([synthetic_dive()], tmp_path / "s.ssrf")
    samples = [s.attrib for s in _samples(_dive_elements(text)[0])]
    assert len(samples) == 8
    # every value the first sample has; the first NDL is always written, a TTS/CNS of 0 is not (Subsurface's start values)
    assert samples[0] == {"time": "0:00 min", "depth": "0.0 m", "temp": "27.5 C", "pressure0": "201.3 bar", "pressure1": "180.0 bar",
                          "ndl": "99:00 min", "tts": "1:00 min", "po2": "0.32 bar", "heartbeat": "72"}
    # unchanged temperature and pressures: temp left out, the pressure written each time it is read
    assert "temp" not in samples[1] and samples[1]["pressure1"] == "180.0 bar"
    assert samples[1]["rbt"] == "60:00 min" and samples[1]["cns"] == "1%"
    # into deco at the deepest point: in_deco flips, the stop appears, NDL goes to 0
    assert samples[2]["ndl"] == "0:00 min" and samples[2]["in_deco"] == "1"
    assert samples[2]["stoptime"] == "3:00 min" and samples[2]["stopdepth"] == "6.0 m" and samples[2]["cns"] == "8%"
    assert samples[2]["temp"] == "24.0 C" and samples[2]["po2"] == "1.33 bar"
    # still in deco: in_deco and stopdepth not repeated, stoptime changed
    assert "in_deco" not in samples[3] and "stopdepth" not in samples[3] and samples[3]["stoptime"] == "2:00 min"
    # a sample with per-tank channels but no tank-0 reading writes pressure1 only
    assert "pressure0" not in samples[5] and samples[5]["pressure1"] == "160.0 bar"
    assert samples[5]["stoptime"] == "0:00 min"
    # out of deco: in_deco back to 0, the stop depth to 0, the NDL written again
    assert samples[6]["in_deco"] == "0" and samples[6]["stopdepth"] == "0.0 m" and samples[6]["ndl"] == "99:00 min"
    # the last sample has no channels: its one pressure is pressure0, nothing else
    assert samples[7] == {"time": "48:20 min", "depth": "0.0 m", "pressure0": "62.0 bar"}


def test_synthetic_dive_drops(tmp_path):
    # its visibility distance comes with Subsurface's own stars, so only the GF99 channel is a loss
    assert write_ssrf([synthetic_dive()], tmp_path / "s.ssrf") == ["Subsurface: the GF99 channel is not written"]
    plain = synthetic_dive()
    plain.service_fields = {}
    assert write_ssrf([plain], tmp_path / "p.ssrf") == [
        "Subsurface: the visibility distance is not written (Subsurface rates visibility with stars)",
        "Subsurface: the GF99 channel is not written",
    ]


# ---------------------------------------------------------------------------
# Rules on their own
# ---------------------------------------------------------------------------

def _dive(**kw) -> UnifiedDive:
    base = dict(date_time=datetime(2026, 1, 2, 10, 0, 0), duration=1800, max_depth=18.0)
    base.update(kw)
    return UnifiedDive(**base)


def test_local_time_is_written_not_utc(tmp_path):
    dive = _dive(date_time=datetime(2026, 7, 1, 23, 59, 59), date_time_utc=datetime(2026, 7, 1, 15, 59, 59), timezone="Etc/GMT-8")
    (el,) = _dive_elements(_write([dive], tmp_path / "t.ssrf"))
    assert (el.get("date"), el.get("time")) == ("2026-07-01", "23:59:59")


def test_dive_without_computer_or_profile_is_manually_added(tmp_path):
    text = _write([_dive()], tmp_path / "m.ssrf")
    root = _root(text)
    assert root.findall("settings/divecomputerid") == [] and root.findall("divesites/site") == []
    (el,) = root.findall("dives/dive")
    assert "number" not in el.attrib and "divesiteid" not in el.attrib
    dc = el.find("divecomputer")
    assert dc.attrib == {"model": MANUAL_DC_MODEL}
    assert dc.find("depth").attrib == {"max": "18.0 m"}
    assert dc.find("temperature") is None and dc.find("water") is None and dc.find("surfacetime") is None
    assert "<divecomputer model='manually added dive'>\n  <depth max='18.0 m' />\n  </divecomputer>\n</dive>" in text


def test_recorded_profile_without_computer_has_no_model(tmp_path):
    dive = _dive(samples=[UnifiedSample(depth=0.0, time=0), UnifiedSample(depth=18.0, temp=22.0, time=60)])
    (el,) = _dive_elements(_write([dive], tmp_path / "p.ssrf"))
    assert el.find("divecomputer").attrib == {}
    assert [s.attrib for s in _samples(el)] == [{"time": "0:00 min", "depth": "0.0 m"}, {"time": "1:00 min", "depth": "18.0 m", "temp": "22.0 C"}]


def test_serial_without_model_still_gets_a_device(tmp_path):
    dive = _dive(computer_serial="77", samples=[UnifiedSample(depth=1.0, time=0)])
    root = _root(_write([dive], tmp_path / "d.ssrf"))
    (device,) = root.findall("settings/divecomputerid")
    assert device.attrib == {"deviceid": device.get("deviceid"), "serial": "77"}
    dc = root.find("dives/dive/divecomputer")
    assert dc.get("deviceid") == device.get("deviceid") and "model" not in dc.attrib


def test_cylinder_air_nitrox_trimix_and_use(tmp_path):
    dive = _dive(gas_mixtures=[
        GasMixture(oxygen=21.0, helium=0.0),
        GasMixture(oxygen=20.9, helium=0.0, tank_volume=12.0, start_pressure=230.0),
        GasMixture(oxygen=18.0, helium=45.0, tank_role="diluent"),
        GasMixture(oxygen=100.0, tank_role="oxygen"),
        GasMixture(oxygen=21.0, helium=35.0, tank_role="not_used"),
        GasMixture(oxygen=32.0, tank_role="stage"),
    ])
    (el,) = _dive_elements(_write([dive], tmp_path / "c.ssrf"))
    assert [c.attrib for c in el.findall("cylinder")] == [
        {},
        {"size": "12.0 l", "start": "230.0 bar"},
        {"o2": "18.0%", "he": "45.0%", "use": "diluent"},
        {"o2": "100.0%", "use": "oxygen"},
        {"o2": "21.0%", "he": "35.0%", "use": "not used"},
        {"o2": "32.0%"},
    ]
    assert ssrf_drops([dive]) == ["Subsurface: the stage tank role is not written (Subsurface's cylinder use has no word for it)"]


def test_weight_in_kilograms_and_zero(tmp_path):
    (el,) = _dive_elements(_write([_dive(weight=6.0, weight_unit="kilogram")], tmp_path / "w.ssrf"))
    assert el.find("weightsystem").attrib == {"weight": "6.0 kg", "description": "weight"}
    (el,) = _dive_elements(_write([_dive(weight=0.0, weight_unit="kilogram")], tmp_path / "w0.ssrf"))
    assert el.find("weightsystem") is None


def test_channel_less_pressure_is_pressure0_and_zero_is_skipped(tmp_path):
    dive = _dive(samples=[
        UnifiedSample(depth=5.0, time=0, pressure=200.0),
        UnifiedSample(depth=5.0, time=10, pressure=0.0),
        UnifiedSample(depth=5.0, time=20, pressure=199.5, channels=SampleChannels(pressures={1: 180.0})),
    ])
    samples = [s.attrib for s in _samples(_dive_elements(_write([dive], tmp_path / "p.ssrf"))[0])]
    assert samples[0]["pressure0"] == "200.0 bar"
    assert "pressure0" not in samples[1]
    # channels with per-tank readings win over the single pressure
    assert "pressure0" not in samples[2] and samples[2]["pressure1"] == "180.0 bar"


def test_po2_is_the_setpoint_when_there_is_one(tmp_path):
    dive = _dive(dive_mode="ccr", samples=[
        UnifiedSample(depth=10.0, time=0, channels=SampleChannels(setpoint=0.7, ppo2=0.68)),
        UnifiedSample(depth=20.0, time=60, channels=SampleChannels(setpoint=0.7, ppo2=0.71)),
        UnifiedSample(depth=20.0, time=120, channels=SampleChannels(setpoint=1.3, ppo2=1.28)),
    ])
    text = _write([dive], tmp_path / "ccr.ssrf")
    (el,) = _dive_elements(text)
    assert el.find("divecomputer").get("dctype") == "CCR"
    assert [s.get("po2") for s in _samples(el)] == ["0.7 bar", None, "1.3 bar"]
    assert ssrf_drops([dive]) == ["Subsurface: the computer's PO2 is not written where the dive has a setpoint (Subsurface's po2 is the setpoint)"]


def test_po2_is_the_computers_ppo2_without_a_setpoint(tmp_path):
    dive = _dive(samples=[UnifiedSample(depth=10.0, time=0, channels=SampleChannels(ppo2=0.42)),
                          UnifiedSample(depth=10.0, time=10, channels=SampleChannels(ppo2=0.42)),
                          UnifiedSample(depth=12.0, time=20, channels=SampleChannels(ppo2=0.46))])
    (el,) = _dive_elements(_write([dive], tmp_path / "oc.ssrf"))
    assert [s.get("po2") for s in _samples(el)] == ["0.42 bar", None, "0.46 bar"]
    assert ssrf_drops([dive]) == []


def test_dctype_and_dive_mode_extradata(tmp_path):
    for mode, dctype, extra in (("ccr", "CCR", None), ("scr", "PSCR", None), ("apnea", "Freedive", None),
                                ("oc_single_gas", None, "oc_single_gas"), ("gauge", None, "gauge"), (None, None, None)):
        (el,) = _dive_elements(_write([_dive(dive_mode=mode)], tmp_path / "m.ssrf"))
        dc = el.find("divecomputer")
        assert dc.get("dctype") == dctype, mode
        modes = [e.get("value") for e in dc.findall("extradata") if e.get("key") == EXTRA_DIVE_MODE]
        assert modes == ([extra] if extra else []), mode


def test_water_salinity_from_density_or_type(tmp_path):
    cases = (
        (dict(water_type="salt", water_density=1025.0), "1025", None),
        (dict(water_type="salt"), "1030", None),
        (dict(water_type="fresh"), "1000", None),
        (dict(water_type="en13319"), "1020", None),
        (dict(water_type="custom", water_density=1017.4), "1017", "custom"),
        (dict(water_type="custom"), None, "custom"),
        (dict(water_density=1030.0), "1030", None),
        ({}, None, None),
    )
    for fields, salinity, extra in cases:
        (el,) = _dive_elements(_write([_dive(**fields)], tmp_path / "w.ssrf"))
        dc = el.find("divecomputer")
        water = dc.find("water")
        assert (water.get("salinity") if water is not None else None) == (f"{salinity} g/l" if salinity else None), fields
        types = [e.get("value") for e in dc.findall("extradata") if e.get("key") == EXTRA_WATER_TYPE]
        assert types == ([extra] if extra else []), fields


def test_deco_model_text():
    assert SsrfDocument.deco_model_text(_dive(deco_model="ZHL-16C", gf_low=40, gf_high=85)) == "ZHL-16C GF 40/85"
    assert SsrfDocument.deco_model_text(_dive(gf_low=30, gf_high=70)) == "GF 30/70"
    assert SsrfDocument.deco_model_text(_dive(deco_model="VPM-B")) == "VPM-B"
    assert SsrfDocument.deco_model_text(_dive(gf_high=85)) == "GF ?/85"
    assert SsrfDocument.deco_model_text(_dive()) is None


def test_sites_are_shared_by_name_or_position(tmp_path):
    dives = [
        _dive(location="Reef", lat=1.0, lng=2.0),
        _dive(location="reef ", lat=1.5, lng=2.5),                     # same name, another position: the first site
        _dive(lat=1.0005, lng=2.0),                                     # no name, 55 m away: the first site
        _dive(location="Wall"),                                         # a name alone is a site
        _dive(lat=5.0, lng=6.0),                                        # a position alone is a site, unnamed
        _dive(),                                                        # nothing: no site
    ]
    root = _root(_write(dives, tmp_path / "sites.ssrf"))
    sites = root.findall("divesites/site")
    assert [(s.get("name"), s.get("gps")) for s in sites] == [("Reef", "1.000000 2.000000"), ("Wall", None), (None, "5.000000 6.000000")]
    ids = [d.get("divesiteid") for d in root.findall("dives/dive")]
    uuids = [s.get("uuid") for s in sites]
    assert ids == [uuids[0], uuids[0], uuids[0], uuids[1], uuids[2], None]
    assert len(set(uuids)) == 3 and all(len(u) == 8 for u in uuids)


def test_computers_are_one_per_model_and_serial(tmp_path):
    dives = [
        _dive(computer_vendor="Garmin", computer_model="Descent Mk3i", computer_serial="1", computer_firmware="5.12"),
        _dive(computer_vendor="Garmin", computer_model="Descent Mk3i", computer_serial="1", computer_firmware="5.12"),
        _dive(computer_vendor="Garmin", computer_model="Descent Mk3i", computer_serial="2"),
        _dive(computer_vendor="Shearwater", computer_model="Perdix", computer_serial="1"),
    ]
    root = _root(_write(dives, tmp_path / "dcs.ssrf"))
    devices = root.findall("settings/divecomputerid")
    assert [(d.get("model"), d.get("serial"), d.get("firmware")) for d in devices] == [
        ("Garmin Descent Mk3i", "1", "5.12"), ("Garmin Descent Mk3i", "2", None), ("Shearwater Perdix", "1", None)]
    device_ids = [d.get("deviceid") for d in devices]
    assert len(set(device_ids)) == 3
    dcs = root.findall("dives/dive/divecomputer")
    assert [dc.get("deviceid") for dc in dcs] == [device_ids[0], device_ids[0], device_ids[1], device_ids[2]]
    # the same dive twice gets the same diveid; a different one another
    assert dcs[0].get("diveid") == dcs[1].get("diveid") != dcs[2].get("diveid")


def test_texts_are_trimmed_and_blank_ones_left_out(tmp_path):
    dive = _dive(buddy="  Pat  ", notes="   ", location="  ", gas_mixtures=[GasMixture(tank_name=" ")])
    text = _write([dive], tmp_path / "t.ssrf")
    (el,) = _dive_elements(text)
    assert el.find("buddy").text == "Pat" and el.find("notes") is None
    assert "divesiteid" not in el.attrib and _root(text).findall("divesites/site") == []
    assert el.find("cylinder").attrib == {}


def test_events_without_known_kind_or_name(tmp_path):
    dive = _dive(gas_mixtures=[GasMixture(oxygen=21.0), GasMixture(oxygen=36.0)], events=[
        DiveEvent(time=5, type="gas_switch", oxygen=50.0, helium=10.0),        # a mix without a tank: no flags, no cylinder
        DiveEvent(time=10, type="gas_switch", tank=0),                          # air: no mix attributes
        DiveEvent(time=20, type="gas_switch", tank=1),                          # the cylinder's mix
        DiveEvent(time=30, type="alert"),                                        # no name: the kind
        DiveEvent(time=40, type="something_else", name="Odd & Co"),
        DiveEvent(time=50, type="mode_change", name="oc_single_gas"),
        DiveEvent(time=60, type="setpoint_change"),
        DiveEvent(time=70, type="bookmark"),
    ])
    events = [e.attrib for e in _dive_elements(_write([dive], tmp_path / "e.ssrf"))[0].findall("divecomputer/event")]
    assert events == [
        {"time": "0:05 min", "type": "25", "name": "gaschange", "o2": "50.0%", "he": "10.0%"},
        {"time": "0:10 min", "type": "25", "flags": "1", "name": "gaschange", "cylinder": "0"},
        {"time": "0:20 min", "type": "25", "flags": "2", "name": "gaschange", "cylinder": "1", "o2": "36.0%"},
        {"time": "0:30 min", "type": "26", "name": "alert"},
        {"time": "0:40 min", "type": "26", "name": "Odd & Co"},
        {"time": "0:50 min", "type": "8", "divemode": "OC", "name": "modechange"},
        {"time": "1:00 min", "type": "20", "name": "SP change"},
        {"time": "1:10 min", "type": "8", "name": "bookmark"},
    ]


def test_heartbeat_written_on_every_sample_with_one(tmp_path):
    dive = _dive(samples=[UnifiedSample(depth=1.0, time=0, channels=SampleChannels(heart_rate=70)),
                          UnifiedSample(depth=2.0, time=10, channels=SampleChannels(heart_rate=70)),
                          UnifiedSample(depth=3.0, time=20, channels=SampleChannels(heart_rate=0))])
    (el,) = _dive_elements(_write([dive], tmp_path / "h.ssrf"))
    assert [s.get("heartbeat") for s in _samples(el)] == ["70", "70", None]


def test_sample_time_none_and_negative_depth(tmp_path):
    dive = _dive(samples=[UnifiedSample(depth=-0.001), UnifiedSample(depth=12.3456, time=65)])
    (el,) = _dive_elements(_write([dive], tmp_path / "n.ssrf"))
    assert [s.attrib for s in _samples(el)] == [{"time": "0:00 min", "depth": "-0.001 m"}, {"time": "1:05 min", "depth": "12.346 m"}]


def test_drops_are_empty_for_a_plain_dive_and_name_everything_once():
    assert ssrf_drops([_dive(), _dive(gas_mixtures=[GasMixture(tank_role="diluent")])]) == []
    dives = [_dive(visibility=10.0, visibility_unit="meter"), _dive(visibility=5.0, visibility_unit="meter", service_fields={"visibility_stars": 2}),
             _dive(gas_mixtures=[GasMixture(tank_role="stage"), GasMixture(tank_role="deco"), GasMixture(tank_role="deco")])]
    assert ssrf_drops(dives) == [
        "Subsurface: the visibility distance is not written (Subsurface rates visibility with stars)",
        "Subsurface: the deco, stage tank roles are not written (Subsurface's cylinder use has no word for them)",
    ]
    # a visibility that comes with Subsurface's own stars is not a loss
    assert ssrf_drops([dives[1]]) == []


# ---------------------------------------------------------------------------
# formats.py wiring
# ---------------------------------------------------------------------------

def test_write_file_ssrf_returns_drops_and_creates_folders(tmp_path):
    path = tmp_path / "out" / "deeper" / "log.ssrf"
    assert write_file([synthetic_dive()], "ssrf", path) == ssrf_drops([synthetic_dive()])
    assert path.is_file() and _root(path.read_text(encoding="utf-8")).tag == "divelog"
    assert formats._ssrf_drops is ssrf_drops


def test_write_each_one_ssrf_per_dive(tmp_path):
    dives = [synthetic_dive(), _dive(dive_number=43, date_time=datetime(2026, 3, 14, 14, 0, 0))]
    result = write_each(dives, "ssrf", tmp_path)
    assert [os.path.basename(p) for p in result.paths] == ["2026-03-14 0930 dive 42.ssrf", "2026-03-14 1400 dive 43.ssrf"]
    for path, number in zip(result.paths, ("42", "43")):
        (el,) = _dive_elements(open(path, encoding="utf-8").read())
        assert el.get("number") == number
    assert result.warnings == ssrf_drops([dives[0]])


# ---------------------------------------------------------------------------
# The FIT fixtures
# ---------------------------------------------------------------------------

@fixtures
def test_fit_fixtures_in_one_file(tmp_path):
    single, two = read_fit(SINGLE_GAS), read_fit(TWO_TANKS)
    text = _write([single, two], tmp_path / "garmin.ssrf")
    root = _root(text)
    # one watch for both dives, one made-up site (both entries are at 10 S 30 W)
    (device,) = root.findall("settings/divecomputerid")
    assert device.attrib == {"model": "Garmin Descent X50i", "deviceid": device.get("deviceid"), "serial": "1000000001", "firmware": "5.12"}
    (site,) = root.findall("divesites/site")
    assert site.get("gps") == "-10.000000 -30.000000" and "name" not in site.attrib
    first, second = root.findall("dives/dive")
    assert (first.get("number"), first.get("date"), first.get("time"), first.get("duration")) == ("28", "2026-06-27", "11:24:51", "51:05 min")
    assert (second.get("number"), second.get("date"), second.get("time"), second.get("duration")) == ("38", "2026-08-29", "09:56:11", "46:20 min")
    assert first.get("divesiteid") == second.get("divesiteid") == site.get("uuid")
    assert [c.attrib for c in first.findall("cylinder")] == [
        {"size": "11.1 l", "description": "Tank 1", "o2": "32.0%", "start": "192.91 bar", "end": "95.76 bar"}]
    assert [c.attrib for c in second.findall("cylinder")] == [
        {"size": "11.1 l", "description": "Tank 1", "start": "204.29 bar", "end": "164.37 bar"},
        {"size": "11.1 l", "description": "Tank 2", "start": "209.66 bar", "end": "161.75 bar"}]
    dc1, dc2 = first.find("divecomputer"), second.find("divecomputer")
    assert dc1.get("deviceid") == dc2.get("deviceid") == device.get("deviceid") and dc1.get("diveid") != dc2.get("diveid")
    assert dc1.find("depth").attrib == {"max": "24.131 m", "mean": "10.138 m"}
    assert dc1.find("temperature").attrib == {"water": "29.0 C"}
    assert dc1.find("water").attrib == {"salinity": "1025 g/l"}
    assert dc1.find("surfacetime").text == "78:10 min"
    extra = dict((e.get("key"), e.get("value")) for e in dc1.findall("extradata"))
    assert extra[EXTRA_DECO_MODEL] == "ZHL-16C GF 40/85" and extra[EXTRA_FIRMWARE] == "5.12" and extra[EXTRA_SERIAL] == "1000000001"
    assert extra[EXTRA_BOTTOM_TIME] == "48:04 min" and extra[EXTRA_CNS_START] == "3.0%" and extra[EXTRA_CNS_END] == "10.0%"
    assert extra[EXTRA_EXIT_GPS].startswith("-10.000725, -29.998301")
    # events: the start-of-dive gas switch, the alerts, the transmitter events
    events1 = dc1.findall("event")
    assert len(events1) == len(single.events) == 17
    assert events1[0].attrib == {"time": "0:00 min", "type": "25", "flags": "1", "name": "gaschange", "cylinder": "0", "o2": "32.0%"}
    assert sum(1 for e in events1 if e.get("type") == "26") == 16
    assert [e.get("name") for e in dc2.findall("event")][:3] == ["gaschange", "tank pod connected", "tank pod connected"]
    assert [e.get("cylinder") for e in dc2.findall("event")][:3] == ["0", "0", "1"]
    # samples: one per record; pressureN per transmitter
    s1, s2 = dc1.findall("sample"), dc2.findall("sample")
    assert len(s1) == len(single.samples) == 386 and len(s2) == len(two.samples) == 331
    assert sum(1 for s in s1 if "pressure0" in s.attrib) == 272 and not any("pressure1" in s.attrib for s in s1)
    assert sum(1 for s in s2 if "pressure0" in s.attrib) == 246 and sum(1 for s in s2 if "pressure1" in s.attrib) == 248
    assert s1[0].attrib == {"time": "0:00 min", "depth": "1.416 m", "temp": "30.0 C", "pressure0": "192.91 bar", "po2": "0.37 bar"}
    assert s2[0].attrib == {"time": "0:00 min", "depth": "1.207 m", "temp": "31.0 C", "po2": "0.24 bar"}
    assert all(s.get("po2") is None or s.get("po2").endswith(" bar") for s in s1)
    assert any("rbt" in s.attrib for s in s1) and any("cns" in s.attrib for s in s1) and any("ndl" in s.attrib for s in s1)
    assert not any("in_deco" in s.attrib or "stoptime" in s.attrib for s in s1), "a no-deco dive never enters deco"
    assert ssrf_drops([single, two]) == []


@fixtures
def test_fit_fixtures_one_file_each(tmp_path):
    result = write_each([read_fit(SINGLE_GAS), read_fit(TWO_TANKS)], "ssrf", tmp_path)
    assert [os.path.basename(p) for p in result.paths] == ["2026-06-27 1124 dive 28.ssrf", "2026-08-29 0956 dive 38.ssrf"]
    assert result.warnings == []
    for path in result.paths:
        root = _root(open(path, encoding="utf-8").read())
        assert len(root.findall("dives/dive")) == 1 and len(root.findall("settings/divecomputerid")) == 1
    # the FIT's cache file name gives the Garmin id, written for a later sync to recognise the dive
    with open(SINGLE_GAS, "rb") as f:
        named = read_fit(f.read(), name="2026-06-27_1124_dive-28_24449823352.fit")   # the cache's file name
    assert "<extradata key='dive_sync:garmin' value='24449823352' />" in _write([named], tmp_path / "named.ssrf")
