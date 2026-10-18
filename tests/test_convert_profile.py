"""The profile hover's core (plans/convert.md I19, src/core/convert/profile.py):
gas names, the active-gas timeline from the gas list and the switch events,
one sample's display-ready details in both unit systems, the gas-switch
markers and the deco ceiling series. Synthetic dives, plus the untracked
two-transmitter Garmin FIT when present."""
import os
from datetime import datetime

import pytest

from src.core.convert import profile
from src.core.models import DiveEvent, GasMixture, SampleChannels, UnifiedDive, UnifiedSample

DATA = os.path.join(os.path.dirname(__file__), "data", "garmin_fit")
TWO_TANKS = os.path.join(DATA, "two_tanks.fit")
fixtures = pytest.mark.skipif(not os.path.isfile(TWO_TANKS), reason="Garmin FIT fixtures not present")


def _sample(time, depth, temp=None, pressure=None, **channels):
    return UnifiedSample(depth=depth, temp=temp, time=time, pressure=pressure,
                         channels=SampleChannels(**channels) if channels else None)


def _dive(samples=(), gases=(), events=()):
    return UnifiedDive(date_time=datetime(2026, 5, 1, 9), duration=1800, max_depth=30.0, samples=list(samples),
                       gas_mixtures=list(gases), events=list(events))


def _deco_dive():
    """Two tanks (Air back gas named by the watch, EAN50 deco), a switch at
    20:00, deco from 15:00 to 25:00, every channel on one sample."""
    gases = [GasMixture(oxygen=21, helium=0, tank_name="Back gas"), GasMixture(oxygen=50, helium=0)]
    samples = [
        _sample(0, 0.0, 28.0),
        _sample(600, 30.0, 27.5, pressures={0: 180.0, 1: 200.0}, ndl=14 * 60, cns=12.0, ppo2=0.84, heart_rate=92, gas_time=91 * 60),
        _sample(900, 30.0, 27.0, pressures={0: 150.0}, ndl=0, tts=12 * 60, deco_stop_depth=6.0, deco_stop_time=3 * 60),
        _sample(1200, 6.0, 27.0, pressure=140.0, tts=5 * 60, deco_stop_depth=6.0, deco_stop_time=120, setpoint=1.3),
        _sample(1500, 3.0, 27.0, ndl=99 * 60, deco_stop_depth=0.0, deco_stop_time=0),
        _sample(1800, 0.0, 28.0),
    ]
    events = [DiveEvent(time=1200, type="gas_switch", tank=1)]
    return _dive(samples, gases, events)


# ---------------------------------------------------------------- gas names

def test_gas_name_covers_air_nitrox_oxygen_and_trimix():
    assert profile.gas_name(GasMixture(oxygen=21, helium=0)) == "Air"
    assert profile.gas_name(GasMixture(oxygen=20.9, helium=0)) == "Air"
    assert profile.gas_name(GasMixture(oxygen=32, helium=0)) == "EAN32"
    assert profile.gas_name(GasMixture(oxygen=100, helium=0)) == "O2"
    assert profile.gas_name(GasMixture(oxygen=21, helium=35)) == "21/35"
    assert profile.gas_name(GasMixture(oxygen=18, helium=45)) == "18/45"
    assert profile.gas_name(DiveEvent(time=0, type="gas_switch", oxygen=50)) == "EAN50"
    assert profile.gas_name(None) == "Air" and profile.gas_name(36.0, 0) == "EAN36" and profile.gas_name(10, 70) == "10/70"


# ---------------------------------------------------------------- the timeline

def test_timeline_of_a_single_gas_dive_is_the_starting_gas_alone():
    dive = _dive([_sample(0, 0.0), _sample(60, 10.0)], [GasMixture(oxygen=32)])
    assert profile.active_gas_timeline(dive) == [(0, 0)]
    assert profile.gas_switch_markers(dive) == []
    assert profile.active_gas_timeline(_dive([_sample(0, 0.0)])) == []          # no gases, no switches
    assert profile.active_gas_at([], 10) is None


def test_timeline_with_two_switches_in_time_order_and_a_switch_before_the_start():
    """Events arrive in any order; a switch at (or before) the first sample
    sets the starting gas instead of adding an entry; a switch to a mix that
    is in the list resolves to that tank; a repeated switch at the same
    second keeps the last."""
    gases = [GasMixture(oxygen=21, tank_name="Back"), GasMixture(oxygen=50, tank_name="Deco"), GasMixture(oxygen=100)]
    dive = _dive([_sample(0, 0.0), _sample(1800, 0.0)], gases, [
        DiveEvent(time=1500, type="gas_switch", oxygen=100, helium=0),
        DiveEvent(time=0, type="gas_switch", tank=1),
        DiveEvent(time=900, type="gas_switch", tank=0),
        DiveEvent(time=1500, type="gas_switch", tank=1),
        DiveEvent(time=300, type="alert", name="ndl_reached"),
    ])
    assert profile.active_gas_timeline(dive) == [(0, 1), (900, 0), (1500, 1)]
    assert profile.gas_switch_markers(dive) == [{"time": 900, "label": "Air"}, {"time": 1500, "label": "EAN50"}]
    # a dive whose first sample is not at 0: a switch before it sets the start too
    late = _dive([_sample(10, 1.0), _sample(100, 5.0)], gases, [DiveEvent(time=5, type="gas_switch", tank=2)])
    assert profile.active_gas_timeline(late) == [(10, 2)]


def test_a_switch_to_an_unknown_gas_keeps_its_mix_name_and_a_nameless_one_is_ignored():
    """Pinned: a switch naming a mix the gas list lacks (a Shearwater that
    logs the mix, a tank index out of range) is kept under the mix's own
    name; a switch with neither a listed tank nor a mix is dropped."""
    dive = _dive([_sample(0, 0.0), _sample(1800, 0.0)], [GasMixture(oxygen=21)], [
        DiveEvent(time=600, type="gas_switch", oxygen=50, helium=0),
        DiveEvent(time=900, type="gas_switch", tank=7),
        DiveEvent(time=1200, type="gas_switch", tank=7, oxygen=18, helium=45),
    ])
    assert profile.active_gas_timeline(dive) == [(0, 0), (600, "EAN50"), (1200, "18/45")]
    assert profile.gas_switch_markers(dive) == [{"time": 600, "label": "EAN50"}, {"time": 1200, "label": "18/45"}]
    assert profile.gas_label(dive, "EAN50") == "EAN50"
    # no gas list at all: the switches alone make the timeline
    bare = _dive([_sample(0, 0.0)], [], [DiveEvent(time=0, type="gas_switch", oxygen=32), DiveEvent(time=60, type="gas_switch", oxygen=21)])
    assert profile.active_gas_timeline(bare) == [(0, "EAN32"), (60, "Air")]


def test_active_gas_at_the_boundaries():
    timeline = [(0, 0), (900, 1), (1500, "O2")]
    assert profile.active_gas_at(timeline, -5) == 0             # before the start: the starting gas
    assert profile.active_gas_at(timeline, 0) == 0
    assert profile.active_gas_at(timeline, 899) == 0
    assert profile.active_gas_at(timeline, 900) == 1            # a switch applies from its own second
    assert profile.active_gas_at(timeline, 1499) == 1
    assert profile.active_gas_at(timeline, 1500) == "O2"
    assert profile.active_gas_at(timeline, 10 ** 6) == "O2"


def test_two_tanks_of_the_same_mix_are_told_apart_by_tank_name():
    gases = [GasMixture(oxygen=21, tank_name="Left"), GasMixture(oxygen=21), GasMixture(oxygen=50)]
    dive = _dive([_sample(0, 0.0), _sample(600, 0.0)], gases, [DiveEvent(time=300, type="gas_switch", tank=1),
                                                               DiveEvent(time=400, type="gas_switch", tank=2)])
    assert profile.gas_switch_markers(dive) == [{"time": 300, "label": "Tank 2"}, {"time": 400, "label": "EAN50"}]
    assert profile.gas_label(dive, 0) == "Air (Left)" and profile.gas_label(dive, 1) == "Air (Tank 2)"
    assert profile.gas_label(dive, 2, with_tank=False) == "EAN50" and profile.gas_label(dive, None) == ""
    assert profile.tank_label(dive, 0) == "Left" and profile.tank_label(dive, 5) == "Tank 6"


# ---------------------------------------------------------------- sample details

def test_sample_details_metric_ndl_sample_with_every_channel():
    dive = _deco_dive()
    d = profile.sample_details(dive, 1)
    assert d == {
        "time": "10:00", "depth": "30.0 m", "temp": "27.5 °C", "ndl": "NDL 14 min", "gas": "Air (Back gas)",
        "tanks": ["Back gas 180 bar", "Tank 2 200 bar"], "cns": "CNS 12 %", "po2": "PO2 0.84 bar",
        "heart_rate": "HR 92 bpm", "gas_time": "Gas time 91 min",
        "lines": ["10:00", "30.0 m", "27.5 °C", "NDL 14 min", "Air (Back gas)", "Back gas 180 bar", "Tank 2 200 bar",
                  "CNS 12 %", "PO2 0.84 bar", "HR 92 bpm", "Gas time 91 min"],
    }
    assert "deco" not in d and "setpoint" not in d


def test_sample_details_in_deco_shows_the_stop_and_tts_instead_of_ndl():
    dive = _deco_dive()
    d = profile.sample_details(dive, 2)
    assert d["deco"] == "Deco: stop 6 m for 3 min, TTS 12 min" and "ndl" not in d
    assert d["tanks"] == ["Back gas 150 bar"] and d["gas"] == "Air (Back gas)"
    # the sample at the switch: the new gas, the single `pressure` field as tank 1, the setpoint
    d = profile.sample_details(dive, 3)
    assert d["gas"] == "EAN50 (Tank 2)" and d["tanks"] == ["Back gas 140 bar"] and d["setpoint"] == "Setpoint 1.30 bar"
    assert d["deco"] == "Deco: stop 6 m for 2 min, TTS 5 min"
    # out of deco again: NDL back, no deco line; a stop of 0 is "no stop"
    d = profile.sample_details(dive, 4)
    assert d["ndl"] == "NDL 99 min" and "deco" not in d
    # a bare sample: time, depth, temperature and the gas only
    assert profile.sample_details(dive, 0) == {"time": "0:00", "depth": "0.0 m", "temp": "28.0 °C", "gas": "Air (Back gas)",
                                               "lines": ["0:00", "0.0 m", "28.0 °C", "Air (Back gas)"]}
    assert profile.sample_details(dive, 6) == {} and profile.sample_details(dive, -1) == {}


def test_sample_details_imperial_converts_depth_temperature_and_pressures():
    dive = _deco_dive()
    d = profile.sample_details(dive, 1, "imperial")
    assert d["depth"] == "98 ft" and d["temp"] == "82 °F"
    assert d["tanks"] == ["Back gas 2611 psi", "Tank 2 2901 psi"]
    assert d["ndl"] == "NDL 14 min" and d["cns"] == "CNS 12 %"
    d = profile.sample_details(dive, 2, "imperial")
    assert d["deco"] == "Deco: stop 20 ft for 3 min, TTS 12 min"
    # a half-metre stop keeps its decimal in metric
    half = _dive([_sample(0, 10.0, deco_stop_depth=4.5, deco_stop_time=60)])
    assert profile.sample_details(half, 0)["deco"] == "Deco: stop 4.5 m for 1 min"
    assert profile.sample_details(half, 0, "imperial")["deco"] == "Deco: stop 15 ft for 1 min"


def test_sample_details_without_a_gas_list_and_with_no_channels():
    """A dive from a service cache (depth/temp/time only): no gas line, no
    tanks; a sample without a temperature has no temp line either."""
    dive = profile.dive_from_samples([{"time": 0, "depth": 0.0, "temp": 20.0}, {"time": 3700, "depth": 12.34},
                                      {"time": 10, "depth": None, "temp": 1.0}])
    assert len(dive.samples) == 2 and not dive.gas_mixtures and not dive.events
    assert profile.sample_details(dive, 0) == {"time": "0:00", "depth": "0.0 m", "temp": "20.0 °C", "lines": ["0:00", "0.0 m", "20.0 °C"]}
    assert profile.sample_details(dive, 1) == {"time": "1:01:40", "depth": "12.3 m", "lines": ["1:01:40", "12.3 m"]}
    assert profile.clock_text(59) == "0:59" and profile.clock_text(3600) == "1:00:00" and profile.clock_text(7265) == "2:01:05"
    assert profile.detail_lines({"time": "1:00", "tanks": [], "depth": "", "gas": None}) == ["1:00"]
    assert profile.gas_switch_markers(dive) == [] and profile.ceiling_series(dive) == []


# ---------------------------------------------------------------- markers and ceiling

def test_markers_and_ceiling_from_a_synthetic_dive():
    dive = _deco_dive()
    assert profile.gas_switch_markers(dive) == [{"time": 1200, "label": "EAN50"}]
    assert profile.ceiling_series(dive) == [{"time": 900, "depth": 6.0}, {"time": 1200, "depth": 6.0}, {"time": 1500, "depth": 0.0}]
    # a dive whose computer logs the stop channel but never a stop: no band
    no_deco = _dive([_sample(0, 0.0, deco_stop_depth=0.0), _sample(60, 10.0, deco_stop_depth=0.0)])
    assert profile.ceiling_series(no_deco) == []
    assert profile.ceiling_series(_dive([_sample(0, 0.0), _sample(60, 10.0)])) == []


@fixtures
def test_two_tanks_fit_gives_tank_pressures_gas_and_no_band():
    """The two-transmitter Garmin FIT: its one switch at 0 s sets the start
    (no marker), both tank pressures show by the watch's tank names, the
    NDL, PO2 and gas time lines are there, and a no-deco dive has no
    ceiling."""
    from src.core.convert.formats import read_files
    dive = read_files([TWO_TANKS]).dives[0]
    assert profile.active_gas_timeline(dive) == [(0, 0)]
    assert profile.gas_switch_markers(dive) == [] and profile.ceiling_series(dive) == []
    d = profile.sample_details(dive, 2)
    assert d["time"] == "0:12" and d["depth"] == "1.7 m" and d["temp"] == "30.0 °C"
    assert d["tanks"] == ["Tank 1 204 bar", "Tank 2 210 bar"] and d["gas"] == "Air (Tank 1)"
    assert d["po2"] == "PO2 0.25 bar" and d["cns"] == "CNS 0 %" and "ndl" not in d and "deco" not in d
    d = profile.sample_details(dive, 200, "imperial")
    assert d["ndl"] == "NDL 127 min" and d["gas_time"] == "Gas time 91 min" and d["tanks"] == ["Tank 2 2605 psi"]
    assert d["depth"] == "38 ft" and d["temp"] == "86 °F"
