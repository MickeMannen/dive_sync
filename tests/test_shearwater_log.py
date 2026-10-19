"""The Shearwater native log decoder (rework.md Track H, H5).

The fixture dives are the owner's own Perdix 2 logs (``tests/data/shearwater/
dive_data.db``); the numbers pinned here were checked against the app's own
``calculated_values_from_samples`` and ``TankProfileData`` (rework.md
"Native log format"). Layouts the fixture lacks (imperial units, a gas
switch, a deco stop, transmitters 2-4, newer log versions, freedive) are
covered by synthetic 32-byte records built by ``_log`` below.
"""
import gzip
import json
import logging
import os
import sqlite3
import statistics
import struct

import pytest

from src.core.models import DiveEvent, GasMixture, SampleChannels
from src.core.services.shearwater_log import (
    RECORD, DecodedLog, ShearwaterLog, ShearwaterLogError, ShearwaterSample, carried_fields, check_against_header,
    decode_log, decode_profile, gas_index_of, mixes_breathed, parse_log, primary_transmitter, profile_events,
    profile_samples, psi2_to_bar, sample_channels, unified_dive_mode, unpack_blob,
)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "shearwater", "dive_data.db")
DIVE_452 = "881855471760887268"


def _fixture_rows():
    db = sqlite3.connect(FIXTURE)
    try:
        return db.execute("SELECT d.DiveNumber, d.DiveId, l.data_bytes_1, l.calculated_values_from_samples, d.TankProfileData "
                          "FROM log_data l JOIN dive_details d ON d.DiveId = l.log_id ORDER BY d.DiveNumber").fetchall()
    finally:
        db.close()


def _fixture_blob(number):
    return next(row[2] for row in _fixture_rows() if row[0] == str(number))


# ---------------------------------------------------------------- synthetic records

def _rec(kind, at=None):
    """A 32-byte record of type ``kind`` with ``at`` = {offset: bytes|int}."""
    rec = bytearray(RECORD)
    rec[0] = kind
    for offset, value in (at or {}).items():
        data = bytes([value]) if isinstance(value, int) else bytes(value)
        rec[offset:offset + len(data)] = data
    return bytes(rec)


def _be16(v):
    return struct.pack(">H", v)


def _sample(depth_dm, temp, *, kind=0x01, o2=21, he=0, status=0x10, stop=0, tts=0, ndl=99, p1=0xFFFF, p2=0xFFFF,
            ppo2=21, setpoint=70, gtr=0xFF, cns=0, gf99=255, cells=(0, 0, 0)):
    """A ``0x01`` sample; the offsets are libdivecomputer's + 1 (the type
    byte). ``cells`` are the three O2 cell bytes in mV (R18b)."""
    return _rec(kind, {1: _be16(depth_dm), 3: _be16(stop), 5: _be16(tts), 7: ppo2, 8: o2, 9: he, 10: ndl, 12: status,
                         13: cells[0], 15: cells[1], 16: cells[2],
                         14: struct.pack("b", temp), 19: setpoint, 20: _be16(p2), 22: gtr, 23: cns, 25: gf99, 28: _be16(p1)})


def _ext(p3=0xFFFF, p4=0xFFFF):
    """A ``0xE1`` extended sample: transmitters 3 and 4."""
    return _rec(0xE1, {1: _be16(p3), 3: _be16(p4)})


def _log(samples, *, log_version=17, units=0, mode=6, interval_ms=2000, dive_time=None, max_depth_dm=None,
         start=1760887268, o2=(21, 32, 0, 0, 0, 0, 0, 0, 0, 0), he=(0,) * 10, gas_state=0b11, ai_mode=5, with_opening_5=True,
         extra=(), cell_mask=0, cell_words=(0, 0, 0)):
    """A PNF log around ``samples`` with the header fields the decoder reads
    (rework.md "Native log format"); ``dive_time``/``max_depth_dm`` default to
    what the samples say so the sanity check passes. ``cell_mask`` and
    ``cell_words`` are opening record 3's O2 cell calibration block (R18b):
    the calibrated bits and the three words (bar per mV x 100000)."""
    samples = list(samples)
    interval_s = interval_ms / 1000
    if dive_time is None:
        under = [i + 1 for i, s in enumerate(samples) if s[0] in (0x01, 0x03) and struct.unpack_from(">H", s, 1)[0] > 0]
        dive_time = int(under[-1] * interval_s) if under else 0
    if max_depth_dm is None:
        max_depth_dm = max((struct.unpack_from(">H", s, 1)[0] for s in samples if s[0] in (0x01, 0x03)), default=0)
    end = start + dive_time
    records = [
        _rec(0x10, {4: 40, 5: 85, 8: units, 12: struct.pack(">I", start), 20: bytes(o2), 30: bytes(he[:2])}),
        _rec(0x11, {1: bytes(he[2:]), 16: _be16(1013)}),
        _rec(0x12),
        _rec(0x13, {3: _be16(1020), 6: cell_mask, 7: b"".join(_be16(w) for w in cell_words)}),
        _rec(0x14, {1: mode, 16: log_version, 17: _be16(gas_state), 28: ai_mode}),
    ]
    if with_opening_5:
        records.append(_rec(0x15, {23: _be16(interval_ms)}))
    records += samples
    records += list(extra)
    records += [
        _rec(0x20, {4: _be16(max_depth_dm), 6: bytes([(dive_time >> 16) & 0xFF, (dive_time >> 8) & 0xFF, dive_time & 0xFF]),
                      12: struct.pack(">I", end)}),
        _rec(0x21), _rec(0x22), _rec(0x23), _rec(0x24), _rec(0x25),
        bytes(RECORD),                                                # padding
        _rec(0xFF, {2: struct.pack(">I", 0xA5419AC1), 13: 11}),   # final: serial, model Perdix 2
    ]
    return b"".join(records)


def _blob(raw):
    return struct.pack("<I", len(raw)) + gzip.compress(raw)


# ---------------------------------------------------------------- the fixture

def test_every_fixture_dive_decodes_and_agrees_with_the_app():
    rows = _fixture_rows()
    assert len(rows) == 27
    for number, dive_id, blob, calculated, _ in rows:
        log = parse_log(unpack_blob(blob))
        samples = profile_samples(log)                      # the sanity check passes on every dive
        calc = json.loads(calculated)
        assert log.units == "metric" and log.dive_mode_name == "OC rec" and log.log_version in (16, 17), number
        assert len(samples) == len(log.samples) > 200
        assert [s.time for s in samples] == [(i + 1) * log.interval_ms // 1000 for i in range(len(samples))]
        # the app averages the under-water samples; the temperature extremes are exact
        under = [s.depth for s in samples if s.depth > 0]
        assert round(statistics.mean(under), 3) == round(calc["AverageDepth"], 3), number
        assert (min(s.temp for s in samples), max(s.temp for s in samples)) == (calc["MinTemp"], calc["MaxTemp"]), number
        assert abs(max(under) - log.max_depth) <= 0.2
        assert samples[-1].depth == 0.0                     # the 60 s surface tail is kept


@pytest.mark.parametrize("number, interval_ms, count, dive_time, max_depth, first, last, temps, avg", [
    (452, 2000, 927, 1788, 25.3, (2, 1.0, 30.0), (1854, 0.0, 30.0), (29.0, 30.0), 15.696),
    (433, 5000, 496, 2414, 14.7, (5, 1.6, 17.0), (2480, 0.0, 16.0), (15.0, 17.0), 8.168),
    (427, 10000, 270, 2631, 15.1, (10, 1.2, 29.0), (2700, 0.0, 28.0), (27.0, 29.0), 7.188),
])
def test_pinned_dives(number, interval_ms, count, dive_time, max_depth, first, last, temps, avg):
    log = parse_log(unpack_blob(_fixture_blob(number)))
    assert (log.interval_ms, log.dive_time, log.max_depth) == (interval_ms, dive_time, max_depth)
    samples = profile_samples(log)
    assert len(samples) == count
    assert (samples[0].time, samples[0].depth, samples[0].temp) == first
    assert (samples[-1].time, samples[-1].depth, samples[-1].temp) == last
    assert max(s.depth for s in samples) == max_depth
    assert (min(s.temp for s in samples), max(s.temp for s in samples)) == temps
    assert round(statistics.mean(s.depth for s in samples if s.depth > 0), 3) == avg
    assert (log.gf_low, log.gf_high, log.model, log.serial) == (40, 85, 11, 0xA5419AC1)
    assert log.gas_switches == [(samples[0].time, 21, 0)]         # one gas, air, from the first sample on


def test_dive_452_header_channels_and_tank_pressure():
    log = parse_log(unpack_blob(_fixture_blob(452)))
    assert (log.log_version, log.start_ticks, log.end_ticks) == (17, 1760887268, 1760889051)   # = data_bytes_3's StartTime/EndTime
    assert log.o2[:2] == [21, 0] and log.gas_enabled[0] and log.ai_mode == 5
    assert (log.surface_pressure_mbar, log.density) == (1006, 1020)
    assert log.info_events == [(11, 1760887268, 452, 0)]                   # event 11 carries the dive number
    first = log.samples[0]
    assert first == ShearwaterSample(time=2, depth=1.0, temp=30.0, ppo2=0.23, o2=21, he=0, ccr=False, deco_stop_depth=0.0,
                                     ndl_or_stop_min=0, tts_min=1, cns=0.0, setpoint=0.7, gf99=0, gas_time_min=None,
                                     pressures={0: 202.57})
    # transmitter 1: 2938 psi at the first sample = TankProfileData's start pressure, 1632 psi at the dive time = its end
    samples = profile_samples(log)
    assert primary_transmitter(log) == 0
    assert samples[0].pressure == 202.57 == round(2938 / 14.5037738, 2)
    assert next(s.pressure for s in samples if s.time == 1788) == 112.52 == round(1632 / 14.5037738, 2)
    assert samples[-1].pressure == 111.28 < 112.52                           # the log's last value, after surfacing
    assert all(s.pressure is not None for s in samples)
    # the channels (R5): the same reading keyed by its tank, the minutes as seconds, the CNS fraction as a percentage
    assert samples[0].channels == SampleChannels(pressures={0: 202.57}, ndl=0, tts=60, deco_stop_depth=0.0, cns=0.0, ppo2=0.23, gf99=0.0)
    at_600 = next(s for s in samples if s.time == 600)
    assert at_600.channels == SampleChannels(pressures={0: 169.75}, ndl=1440, tts=360, deco_stop_depth=0.0, cns=1.0, ppo2=0.65, gas_time=1920)
    assert [s.time for s in samples if s.channels.gf99 is None][:3] == [12, 14, 16]    # 255 = the computer had none
    assert max(s.channels.cns for s in samples) == 3.0 and max(s.channels.gf99 or 0 for s in samples) == 51.0
    assert profile_events(log, [GasMixture(oxygen=21.0)]) == []                # one gas, air, from the first sample on
    assert carried_fields(log) == {"computer_vendor": "Shearwater", "computer_model": "Perdix 2", "computer_serial": "A5419AC1",
                                   "gf_low": 40, "gf_high": 85, "dive_mode": "oc_single_gas",
                                   "water_type": "en13319", "water_density": 1020.0}


def test_dive_427_without_air_integration_has_no_pressures():
    log = parse_log(unpack_blob(_fixture_blob(427)))
    assert log.ai_mode == 0 and primary_transmitter(log) is None
    assert all(s.pressures == {} for s in log.samples)
    assert all(s.pressure is None and s.channels.pressures == {} for s in profile_samples(log))


def test_dive_433_extended_records_carry_transmitter_4():
    """Log version 16 with an ``0xE1`` record after every sample: transmitter
    4 (a second paired transmitter) reports from 200 s on, transmitter 1 all
    along; ``pressure`` carries transmitter 1, the lowest index, and the
    channels both - keyed by tank when the adapter says which tank T4 is
    (the dive's gas_mixtures is [T1, T4], so T4 is tank 1)."""
    log = parse_log(unpack_blob(_fixture_blob(433)))
    assert len(log.samples) == 496 and all(set(s.pressures) <= {0, 3} for s in log.samples)
    assert all(0 in s.pressures for s in log.samples)
    fourth = [s for s in log.samples if 3 in s.pressures]
    assert len(fourth) == 208 and fourth[0].time == 200 and fourth[0].pressures == {0: 179.13, 3: 162.03}
    assert primary_transmitter(log) == 0 and profile_samples(log)[39].pressure == 179.13
    assert profile_samples(log)[39].channels.pressures == {0: 179.13, 3: 162.03}              # no map: slot indices
    mapped = profile_samples(log, tank_of_transmitter={0: 0, 3: 1})
    assert mapped[39].pressure == 179.13 and mapped[39].channels.pressures == {0: 179.13, 1: 162.03}
    assert mapped[0].channels.pressures == {0: 186.43}
    assert sum(1 for s in mapped if len(s.channels.pressures) == 2) == 208


def test_decode_profile_is_the_whole_pipeline():
    assert len(decode_profile(_fixture_blob(452))) == 927
    decoded = decode_log(_fixture_blob(433), {0: 0, 3: 1}, [GasMixture(oxygen=21.0, tank_name="T1"), GasMixture(oxygen=21.0, tank_name="T4")])
    assert isinstance(decoded, DecodedLog) and len(decoded.samples) == 496 and decoded.events == []
    assert decoded.samples[39].channels.pressures == {0: 179.13, 1: 162.03}
    assert decoded.fields["dive_mode"] == "oc_single_gas" and decoded.fields["computer_serial"] == "A5419AC1"
    assert DecodedLog() == DecodedLog(samples=[], events=[], fields={})


# ---------------------------------------------------------------- synthetic records

def test_imperial_log_is_converted():
    raw = _log([_sample(100, 50), _sample(330, 41), _sample(0, 59)], units=1, max_depth_dm=330, dive_time=4)
    log = parse_log(raw)
    assert log.units == "imperial" and log.max_depth == round(33.0 * 0.3048, 2) == 10.06
    samples = profile_samples(log)
    assert [(s.time, s.depth, s.temp) for s in samples] == [(2, 3.05, 10.0), (4, 10.06, 5.0), (6, 0.0, 15.0)]


def test_negative_temperature_bytes():
    """A negative byte is offset by 102 and clamped to 0 (libdivecomputer's fix)."""
    log = parse_log(_log([_sample(50, -100), _sample(50, -105), _sample(50, 3)]))
    assert [s.temp for s in log.samples] == [0.0, -3.0, 3.0]


def test_gas_switch_and_deco_stop():
    raw = _log([
        _sample(100, 20, o2=21),
        _sample(300, 20, o2=21, ndl=12, tts=2),
        _sample(300, 20, o2=21, stop=6, ndl=3, tts=12, gf99=88, gtr=45, cns=12),   # in deco: stop at 6 m for 3 min
        _sample(60, 20, o2=50, stop=0, ndl=99),                                       # switched to 50 %
        _sample(60, 20, o2=50),
        _sample(0, 20, o2=50),
    ])
    log = parse_log(raw)
    assert log.gas_switches == [(2, 21, 0), (8, 50, 0)]
    deco = log.samples[2]
    assert (deco.deco_stop_depth, deco.ndl_or_stop_min, deco.tts_min, deco.gf99, deco.gas_time_min, deco.cns) == (6.0, 3, 12, 88, 45, 0.12)
    assert (log.samples[1].deco_stop_depth, log.samples[1].ndl_or_stop_min) == (0.0, 12)
    assert [s.o2 for s in log.samples] == [21, 21, 21, 50, 50, 50]
    assert (log.dive_time, log.max_depth) == (10, 30.0)
    samples = profile_samples(log)
    assert len(samples) == 6
    # the channels (R5): one byte is the NDL *or* the stop time, told apart by the stop depth
    assert samples[1].channels == SampleChannels(ndl=720, tts=120, deco_stop_depth=0.0, cns=0.0, ppo2=0.21)
    assert samples[2].channels == SampleChannels(tts=720, deco_stop_depth=6.0, deco_stop_time=180, cns=12.0, ppo2=0.21,
                                                 gf99=88.0, gas_time=2700)
    assert samples[3].channels.ndl == 5940 and samples[3].channels.deco_stop_time is None      # 99 = the cap, as logged
    # the switch to 50 % is an event naming the tank that holds the mix; the starting gas (tank 0) is none
    gases = [GasMixture(oxygen=21.0), GasMixture(oxygen=50.0)]
    assert profile_events(log, gases) == [DiveEvent(time=8, type="gas_switch", tank=1, oxygen=50.0, helium=0.0)]
    assert profile_events(log) == [DiveEvent(time=8, type="gas_switch", oxygen=50.0, helium=0.0)]           # no tanks known
    assert profile_events(log, [GasMixture(oxygen=21.0)]) == [DiveEvent(time=8, type="gas_switch", oxygen=50.0, helium=0.0)]
    assert mixes_breathed(log) == [(21, 0), (50, 0)] and unified_dive_mode(log) == "oc_multi_gas"
    assert decode_log(_blob(raw), None, gases).events == profile_events(log, gases)


def test_ccr_status_and_setpoint():
    log = parse_log(_log([_sample(200, 20, status=0x00, setpoint=130, ppo2=125), _sample(200, 20, status=0x08)], mode=0))
    assert (log.samples[0].ccr, log.samples[0].setpoint, log.samples[0].ppo2) == (True, 1.3, 1.25)
    assert log.samples[1].ccr is True                                       # SC bit set, OC bit clear
    samples = profile_samples(log)
    assert (samples[0].channels.setpoint, samples[0].channels.ppo2) == (1.3, 1.25)   # a closed-circuit sample carries its setpoint
    assert unified_dive_mode(log) == "ccr" and carried_fields(log)["dive_mode"] == "ccr"
    # an open-circuit sample does not: the byte is always written (0.7 on the fixture's OC dives) but means nothing there
    oc = parse_log(_log([_sample(200, 20, status=0x10, setpoint=70, ppo2=42)]))
    assert oc.samples[0].setpoint == 0.7 and profile_samples(oc)[0].channels.setpoint is None
    assert profile_samples(oc)[0].channels.ppo2 == 0.42


# ---------------------------------------------------------------- O2 cells (R18b)

def test_cells_of_a_closed_circuit_sample_are_calibrated_mv():
    """Opening record 3's mask says which cells count and its words scale
    them: cell n's PO2 = its mV x word / 100000, in bar, only on a
    closed-circuit sample whose PPO2 the computer measured itself; the raw
    mV of every cell is kept beside it."""
    log = parse_log(_log([_sample(200, 20, status=0x00, ppo2=88, setpoint=130, cells=(42, 43, 42)),
                          _sample(200, 20, status=0x08, ppo2=88, setpoint=130, cells=(50, 0, 60))],
                         mode=0, cell_mask=0x07, cell_words=(2000, 2200, 2050)))
    assert log.cell_calibrated == (True, True, True) and log.cell_calibration == (0.02, 0.022, 0.0205)
    assert log.cells_at_default is False and log.cell_factors == {0: 0.02, 1: 0.022, 2: 0.0205}
    first, second = log.samples
    assert first.cell_mv == {0: 42, 1: 43, 2: 42} and first.cells == {0: 0.84, 1: 0.946, 2: 0.861}
    assert second.ccr is True and second.cells == {0: 1.0, 1: 0.0, 2: 1.23}   # a semi-closed sample too; 0 mV carried as 0.0, as libdivecomputer does
    channels = sample_channels(first)
    assert channels.o2_cells == {0: 0.84, 1: 0.946, 2: 0.861} and channels.ppo2 == 0.88 and channels.setpoint == 1.3
    samples = profile_samples(log)
    assert [s.channels.o2_cells for s in samples] == [{0: 0.84, 1: 0.946, 2: 0.861}, {0: 1.0, 1: 0.0, 2: 1.23}]
    # the whole pipeline, as the adapter calls it
    decoded = decode_log(_blob(_log([_sample(200, 20, status=0x00, cells=(42, 43, 42))], mode=0, cell_mask=0x07, cell_words=(2000, 2200, 2050))))
    assert decoded.samples[0].channels.o2_cells == {0: 0.84, 1: 0.946, 2: 0.861}
    assert json.loads(decoded.samples[0].model_dump_json())["channels"]["o2_cells"] == {"0": 0.84, "1": 0.946, "2": 0.861}


def test_only_the_calibrated_cells_are_reported():
    """A cell whose mask bit is clear is not reported (its mV stays in
    ``cell_mv``); mask 0x05 gives cells 1 and 3."""
    log = parse_log(_log([_sample(200, 20, status=0x00, cells=(42, 43, 42))], mode=0, cell_mask=0x05, cell_words=(2000, 2200, 2050)))
    assert log.cell_calibrated == (True, False, True) and log.cell_factors == {0: 0.02, 2: 0.0205}
    assert log.samples[0].cells == {0: 0.84, 2: 0.861} and log.samples[0].cell_mv == {0: 42, 1: 43, 2: 42}
    assert sample_channels(log.samples[0]).o2_cells == {0: 0.84, 2: 0.861}
    # no mask bit at all (an open-circuit computer, the fixture's): nothing, whatever the bytes hold
    none = parse_log(_log([_sample(200, 20, status=0x00, cells=(42, 43, 42))], mode=0))
    assert none.cell_factors == {} and none.samples[0].cells == {} and none.samples[0].cell_mv == {0: 42, 1: 43, 2: 42}


def test_open_circuit_and_external_ppo2_samples_have_no_cells():
    """The cell bytes mean nothing on an open-circuit sample (a bailout) or
    when the PPO2 came from an external monitor (status bit 0x02): neither
    the mV nor a PO2 is read there, while the closed-circuit samples around
    them keep theirs."""
    log = parse_log(_log([_sample(200, 20, status=0x00, cells=(42, 43, 42)),
                          _sample(200, 20, status=0x10, cells=(42, 43, 42)),      # bailout to open circuit
                          _sample(200, 20, status=0x02, cells=(42, 43, 42)),      # closed circuit, external PPO2
                          _sample(200, 20, status=0x00, cells=(45, 44, 45))],
                         mode=0, cell_mask=0x07, cell_words=(2000, 2000, 2000)))
    assert [s.cells for s in log.samples] == [{0: 0.84, 1: 0.86, 2: 0.84}, {}, {}, {0: 0.9, 1: 0.88, 2: 0.9}]
    assert [s.cell_mv for s in log.samples] == [{0: 42, 1: 43, 2: 42}, {}, {}, {0: 45, 1: 44, 2: 45}]
    assert [s.ccr for s in log.samples] == [True, False, True, True]
    assert [s.channels.o2_cells for s in profile_samples(log)] == [{0: 0.84, 1: 0.86, 2: 0.84}, {}, {}, {0: 0.9, 1: 0.88, 2: 0.9}]


def test_cells_at_the_factory_default_are_left_out(caplog):
    """libdivecomputer's rule: when every calibrated cell still holds the
    factory default word (2100) the sensors are treated as uncalibrated and
    no per-cell PO2 is reported (a Petrel 3 writes the default for every
    cell); the computer's own PPO2 stays. One cell off the default, or an
    uncalibrated cell at it, keeps the rule off."""
    with caplog.at_level(logging.INFO, logger="src.core.services.shearwater_log"):
        log = parse_log(_log([_sample(200, 20, status=0x00, ppo2=85, cells=(42, 43, 42))], mode=0,
                             cell_mask=0x07, cell_words=(2100, 2100, 2100)))
    assert log.cells_at_default is True and log.cell_calibrated == (True, True, True) and log.cell_factors == {}
    assert log.samples[0].cells == {} and log.samples[0].cell_mv == {0: 42, 1: 43, 2: 42} and log.samples[0].ppo2 == 0.85
    assert profile_samples(log)[0].channels.o2_cells == {} and profile_samples(log)[0].channels.ppo2 == 0.85
    assert "factory default calibration (2100)" in caplog.text
    # two calibrated cells at the default (the third uncalibrated): still all default -> left out
    assert parse_log(_log([_sample(200, 20, status=0x00)], mode=0, cell_mask=0x03, cell_words=(2100, 2100, 1900))).cell_factors == {}
    # one calibrated cell off the default: all three count
    trusted = parse_log(_log([_sample(200, 20, status=0x00, cells=(42, 43, 42))], mode=0, cell_mask=0x07, cell_words=(2100, 2100, 1900)))
    assert trusted.cells_at_default is False and trusted.samples[0].cells == {0: 0.882, 1: 0.903, 2: 0.798}


def test_the_fixture_dives_have_no_cells():
    """The owner's Perdix 2 is an open-circuit computer: its header marks no
    cell calibrated and every sample is open circuit, so no fixture dive
    gains a cell reading (nothing changes for a dive cached before R18b)."""
    for number in (452, 433, 427):
        log = parse_log(unpack_blob(_fixture_blob(number)))
        assert log.cell_calibrated == (False, False, False) and log.cell_factors == {}
        assert not any(s.cells or s.cell_mv for s in log.samples)
        assert not any(s.channels.o2_cells for s in profile_samples(log))


def test_bailout_is_a_mode_change_event():
    """A rebreather diver going open circuit (and back) with the same mix:
    no gas switch, a mode_change each way; with another mix, both."""
    log = parse_log(_log([_sample(200, 20, status=0x00, o2=21), _sample(200, 20, status=0x10, o2=21),
                          _sample(100, 20, status=0x00, o2=21), _sample(100, 20, status=0x10, o2=50)], mode=0))
    assert profile_events(log, [GasMixture(oxygen=21.0), GasMixture(oxygen=50.0)]) == [
        DiveEvent(time=4, type="mode_change", name="oc_multi_gas"),
        DiveEvent(time=6, type="mode_change", name="ccr"),
        DiveEvent(time=8, type="gas_switch", tank=1, oxygen=50.0, helium=0.0),
        DiveEvent(time=8, type="mode_change", name="oc_multi_gas"),
    ]
    assert unified_dive_mode(log) == "ccr"
    single = parse_log(_log([_sample(200, 20, status=0x00), _sample(200, 20, status=0x10)], mode=4))
    assert profile_events(single) == [DiveEvent(time=4, type="mode_change", name="oc_single_gas")]
    assert unified_dive_mode(single) == "scr"
    back = parse_log(_log([_sample(200, 20, status=0x10), _sample(200, 20, status=0x08)], mode=4))
    assert profile_events(back) == [DiveEvent(time=4, type="mode_change", name="scr")]


def test_starting_gas_is_an_event_only_when_it_is_not_the_first_tank():
    log = parse_log(_log([_sample(100, 20, o2=32), _sample(0, 20, o2=32)]))
    assert profile_events(log, [GasMixture(oxygen=21.0), GasMixture(oxygen=32.0)]) == \
        [DiveEvent(time=2, type="gas_switch", tank=1, oxygen=32.0, helium=0.0)]
    assert profile_events(log, [GasMixture(oxygen=32.0), GasMixture(oxygen=21.0)]) == []
    assert profile_events(log, [GasMixture(oxygen=21.0)]) == [DiveEvent(time=2, type="gas_switch", oxygen=32.0, helium=0.0)]
    assert profile_events(log, []) == []                                   # no tanks known: the first mix is the dive's
    assert gas_index_of(32.0, 0.0, [GasMixture(oxygen=21.0), GasMixture(oxygen=32.4)]) == 1
    assert gas_index_of(18.0, 45.0, [GasMixture(oxygen=18.0, helium=45.0)]) == 0 and gas_index_of(18.0, 45.0, []) is None


def test_sample_channels_units_and_transmitter_map():
    raw = ShearwaterSample(time=10, depth=20.0, temp=20.0, ppo2=0.63, o2=21, he=0, ccr=False, deco_stop_depth=0.0,
                           ndl_or_stop_min=25, tts_min=4, cns=0.125, setpoint=0.7, gf99=37, gas_time_min=31,
                           pressures={0: 150.0, 3: 120.0, 1: 90.0})
    assert sample_channels(raw) == SampleChannels(pressures={0: 150.0, 1: 90.0, 3: 120.0}, ndl=1500, tts=240, deco_stop_depth=0.0,
                                                  cns=12.5, ppo2=0.63, gf99=37.0, gas_time=1860)
    # a map keys the readings by tank; a transmitter the map does not name keeps its slot index unless a named one holds it
    assert sample_channels(raw, {0: 0, 3: 1}).pressures == {0: 150.0, 1: 120.0}           # T2 (slot 1) is unnamed and loses tank 1 to T4
    assert sample_channels(raw, {0: 0}).pressures == {0: 150.0, 1: 90.0, 3: 120.0}
    assert sample_channels(raw, {0: 1, 1: 0, 3: 2}).pressures == {1: 150.0, 0: 90.0, 2: 120.0}
    assert sample_channels(raw, {}).pressures == {0: 150.0, 1: 90.0, 3: 120.0}
    deco = ShearwaterSample(time=10, depth=20.0, temp=20.0, ppo2=1.2, o2=50, he=0, ccr=True, deco_stop_depth=3.0,
                            ndl_or_stop_min=2, tts_min=9, cns=0.3, setpoint=1.3, gf99=None, gas_time_min=None, pressures={})
    assert sample_channels(deco) == SampleChannels(tts=540, deco_stop_depth=3.0, deco_stop_time=120, cns=30.0, ppo2=1.2, setpoint=1.3)
    assert sample_channels(deco).model_dump() == {"tts": 540, "deco_stop_depth": 3.0, "deco_stop_time": 120, "cns": 30.0,
                                                  "ppo2": 1.2, "setpoint": 1.3}


def test_carried_fields_from_the_header():
    log = parse_log(_log([_sample(100, 20), _sample(0, 20)], mode=2))
    assert carried_fields(log)["dive_mode"] == "gauge"
    assert unified_dive_mode(parse_log(_log([_sample(100, 20)], mode=3))) is None          # PPO2 monitor mode
    assert unified_dive_mode(parse_log(_log([_sample(100, 20)], mode=7))) == "apnea"
    assert unified_dive_mode(parse_log(_log([_sample(100, 20)], mode=5))) == "ccr"
    assert unified_dive_mode(parse_log(_log([_sample(100, 20)], mode=12))) == "oc_single_gas"
    assert unified_dive_mode(parse_log(_log([_sample(100, 20)], log_version=7))) is None    # no mode byte before version 8
    assert "dive_mode" not in carried_fields(parse_log(_log([_sample(100, 20)], log_version=7)))
    # the water setting: the three salinities, else custom; the model byte by libdivecomputer's table
    raw = _log([_sample(100, 20)])
    records = [raw[i:i + RECORD] for i in range(0, len(raw), RECORD)]
    fresh = b"".join(_rec(0x13, {3: _be16(1000)}) if r[0] == 0x13 else r for r in records)
    assert {k: carried_fields(parse_log(fresh))[k] for k in ("water_type", "water_density")} == {"water_type": "fresh", "water_density": 1000.0}
    salt = b"".join(_rec(0x13, {3: _be16(1030)}) if r[0] == 0x13 else r for r in records)
    assert carried_fields(parse_log(salt))["water_type"] == "salt"
    odd = b"".join(_rec(0x13, {3: _be16(1012)}) if r[0] == 0x13 else r for r in records)
    assert carried_fields(parse_log(odd))["water_type"] == "custom"
    none = b"".join(_rec(0x13) if r[0] == 0x13 else r for r in records)
    assert "water_type" not in carried_fields(parse_log(none)) and "water_density" not in carried_fields(parse_log(none))
    teric = b"".join(_rec(0xFF, {2: struct.pack(">I", 0x0000ABCD), 13: 8}) if r[0] == 0xFF else r for r in records)
    fields = carried_fields(parse_log(teric))
    assert (fields["computer_model"], fields["computer_serial"]) == ("Teric", "0000ABCD")
    unknown = b"".join(_rec(0xFF, {2: struct.pack(">I", 1), 13: 99}) if r[0] == 0xFF else r for r in records)
    fields = carried_fields(parse_log(unknown))
    assert "computer_model" not in fields and fields["computer_serial"] == "00000001" and fields["computer_vendor"] == "Shearwater"
    without_final = b"".join(r for r in records if r[0] != 0xFF)
    fields = carried_fields(parse_log(without_final))
    assert "computer_model" not in fields and "computer_serial" not in fields and fields["gf_low"] == 40


def test_transmitter_words_and_the_primary_transmitter():
    assert psi2_to_bar(0xFFFF) is None and psi2_to_bar(0xFFFE) is None and psi2_to_bar(0xFFFC) is None
    assert psi2_to_bar(0) is None and psi2_to_bar(0x2000) is None            # battery bits but no reading
    assert psi2_to_bar(0x1000 | 1469) == psi2_to_bar(1469) == 202.57         # 1469 x 2 psi, battery nibble ignored
    # transmitter 1 off, transmitter 2 reading: the profile carries T2
    log = parse_log(_log([_sample(100, 20, p1=0xFFFF, p2=1000), _sample(100, 20, p1=0xFFFF, p2=990), _sample(0, 20, p2=0xFFFD)]))
    assert [s.pressures for s in log.samples] == [{1: 137.9}, {1: 136.52}, {}]
    assert primary_transmitter(log) == 1
    assert [s.pressure for s in profile_samples(log)] == [137.9, 136.52, None]
    # both on: T1 is the primary, T2 stays parsed
    log = parse_log(_log([_sample(100, 20, p1=1400, p2=1000), _sample(0, 20, p1=1390, p2=990)]))
    assert log.samples[0].pressures == {0: 193.05, 1: 137.9}
    assert [s.pressure for s in profile_samples(log)] == [193.05, 191.67]


def test_extended_record_attaches_transmitters_3_and_4_to_its_sample():
    samples = [_sample(100, 20), _ext(p3=1200, p4=0xFFFF), _sample(0, 20), _ext(p3=1190, p4=800)]
    log = parse_log(_log(samples, log_version=13))
    assert [s.pressures for s in log.samples] == [{2: 165.47}, {2: 164.1, 3: 110.32}]
    assert primary_transmitter(log) == 2 and [s.pressure for s in profile_samples(log)] == [165.47, 164.1]
    old = parse_log(_log(samples, log_version=12))                           # before version 13: no such record
    assert all(s.pressures == {} for s in old.samples)


def test_pressures_need_log_version_7():
    log = parse_log(_log([_sample(100, 20, p1=1400)], log_version=6))
    assert log.samples[0].pressures == {} and log.ai_mode == 0


def test_avelo_sample_reads_like_a_dive_sample_with_one_transmitter():
    log = parse_log(_log([_sample(100, 20, kind=0x03, p1=1400, p2=1000), _sample(0, 20, kind=0x03)], mode=12))
    assert log.dive_mode_name == "Avelo" and log.samples[0].pressures == {0: 193.05} and log.samples[0].ccr is False


def test_older_log_defaults_and_missing_opening_5():
    raw = _log([_sample(100, 20), _sample(0, 20)], log_version=8, with_opening_5=False)
    log = parse_log(raw)
    assert log.interval_ms == 10000 and [s.time for s in log.samples] == [10, 20]
    assert parse_log(_log([_sample(100, 20)], log_version=7)).dive_mode is None    # the mode byte came with version 8


def test_newer_log_version_is_decoded_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        log = parse_log(_log([_sample(100, 20), _sample(0, 20)], log_version=18))
    assert log.log_version == 18 and len(profile_samples(log)) == 2
    assert [r for r in caplog.records if "log version 18 is newer" in r.getMessage()]
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        parse_log(_log([_sample(100, 20)], log_version=17))
    assert not caplog.records


def test_info_events_and_padding_are_read_past():
    extra = [_rec(0x30, {1: 11, 4: struct.pack(">I", 1760887268), 8: struct.pack(">I", 452)}), _rec(0x51), _rec(0x70), _rec(0xA1)]
    log = parse_log(_log([_sample(100, 20), _sample(0, 20)], extra=extra))
    assert log.info_events == [(11, 1760887268, 452, 0)] and len(log.samples) == 2


# ---------------------------------------------------------------- the sanity check

def test_sanity_check_drops_samples_that_disagree_with_the_header():
    samples = [_sample(100, 20), _sample(250, 20), _sample(0, 20)]
    good = parse_log(_log(samples))
    check_against_header(good)
    assert len(profile_samples(good)) == 3
    deeper = parse_log(_log(samples, max_depth_dm=380))            # header 38 m, samples 25 m
    with pytest.raises(ShearwaterLogError, match="samples reach 25.0 m but the header says 38.0 m"):
        profile_samples(deeper)
    assert len(profile_samples(deeper, check=False)) == 3
    assert len(profile_samples(parse_log(_log(samples, max_depth_dm=258)))) == 3      # within 1 m: the header keeps the peak
    longer = parse_log(_log(samples, dive_time=4 + 6 + 30 + 1))     # last under-water sample at 4 s, header 41 s
    with pytest.raises(ShearwaterLogError, match="last under-water sample at 4 s"):
        profile_samples(longer)
    assert len(profile_samples(parse_log(_log(samples, dive_time=4 + 6 + 30)))) == 3   # 3 intervals + 30 s is fine


# ---------------------------------------------------------------- error paths: every one ends in no samples

def test_blob_errors():
    with pytest.raises(ShearwaterLogError, match="empty"):
        unpack_blob(None)
    with pytest.raises(ShearwaterLogError, match="empty"):
        unpack_blob(b"\x01\x02")
    with pytest.raises(ShearwaterLogError, match="not a gzip stream"):
        unpack_blob(struct.pack("<I", 32) + b"not gzip at all")
    raw = _log([_sample(100, 20)])
    with pytest.raises(ShearwaterLogError, match="declares 12 bytes but holds"):
        unpack_blob(struct.pack("<I", 12) + gzip.compress(raw))
    assert unpack_blob(_blob(raw)) == raw
    truncated = _blob(raw)[:-10]
    with pytest.raises(ShearwaterLogError):
        unpack_blob(truncated)


def test_log_structure_errors():
    with pytest.raises(ShearwaterLogError, match="empty"):
        parse_log(b"")
    with pytest.raises(ShearwaterLogError, match="legacy Predator"):
        parse_log(b"\xff\xff" + bytes(254))
    with pytest.raises(ShearwaterLogError, match="not a multiple"):
        parse_log(_log([_sample(100, 20)]) + b"\x00")
    raw = _log([_sample(100, 20)])
    without_opening_3 = b"".join(r for r in (raw[i:i + RECORD] for i in range(0, len(raw), RECORD)) if r[0] != 0x13)
    with pytest.raises(ShearwaterLogError, match="opening 3"):
        parse_log(without_opening_3)
    without_closing_2 = b"".join(r for r in (raw[i:i + RECORD] for i in range(0, len(raw), RECORD)) if r[0] != 0x22)
    with pytest.raises(ShearwaterLogError, match="closing 2"):
        parse_log(without_closing_2)
    with pytest.raises(ShearwaterLogError, match="zero sample interval"):
        parse_log(_log([_sample(100, 20)], interval_ms=0))


def test_freedive_and_empty_logs_give_no_profile():
    free = parse_log(_log([_rec(0x02, {1: _be16(1100), 3: _be16(250)})], mode=7, dive_time=30, max_depth_dm=90))
    assert free.is_freedive and free.samples == [] and free.freedive_records == 1
    with pytest.raises(ShearwaterLogError, match="freedive"):
        profile_samples(free)
    with pytest.raises(ShearwaterLogError, match="no dive samples"):
        profile_samples(parse_log(_log([], dive_time=0, max_depth_dm=0)))
    with pytest.raises(ShearwaterLogError, match="no dive samples"):
        profile_samples(parse_log(_log([], dive_time=0, max_depth_dm=0)), check=False)
    # a freedive log tagged only by its records (mode byte not freedive) still gives nothing
    tagged = parse_log(_log([_rec(0x02, {1: _be16(1100)})], mode=6, dive_time=0, max_depth_dm=0))
    assert tagged.is_freedive


def test_decode_profile_raises_only_shearwater_log_error():
    for blob in (None, b"", b"\x00\x00\x00", struct.pack("<I", 0) + gzip.compress(b""), _blob(b"\xff\xff" + bytes(30))):
        with pytest.raises(ShearwaterLogError):
            decode_profile(blob)


# --- check.md B16: a rebreather dive's tanks from the transmitter names --------

def _ccr_log(mode: int = 0, cc_gases=((21, 0, True),), loop=((18, 45),)) -> "ShearwaterLog":
    """A header-and-samples log built in memory (no bytes): ``cc_gases`` fill
    the closed-circuit slots 5.., ``loop`` the mixes of the samples."""
    from src.core.services.shearwater_log import ShearwaterLog, ShearwaterSample
    o2, he, enabled = [0] * 10, [0] * 10, [False] * 10
    o2[0], enabled[0] = 99, True                              # an open-circuit oxygen, not a diluent
    for i, (a, b, on) in enumerate(cc_gases):
        o2[5 + i], he[5 + i], enabled[5 + i] = a, b, on
    log = ShearwaterLog(log_version=17, units="metric", dive_mode=mode, interval_ms=10000, dive_time=60, max_depth=30.0,
                        start_ticks=0, end_ticks=60, gf_low=40, gf_high=70, o2=o2, he=he, gas_enabled=enabled,
                        surface_pressure_mbar=1013, density=1025, ai_mode=0, model=10, serial=1)
    for t, (a, b) in enumerate(loop):
        log.samples.append(ShearwaterSample(time=10 * (t + 1), depth=10.0, temp=20.0, ppo2=1.2, o2=a, he=b, ccr=mode != 1,
                                            deco_stop_depth=0.0, ndl_or_stop_min=10, tts_min=1, cns=0.0, setpoint=1.2,
                                            gf99=None, gas_time_min=None))
    return log


def test_rebreather_gases_take_roles_and_mixes_from_the_transmitter_names():
    from src.core.services.shearwater_log import rebreather_gases
    gases = [GasMixture(tank_name="O2", start_pressure=200.0), GasMixture(tank_name="D1"), GasMixture(tank_name="T3")]
    out = rebreather_gases(_ccr_log(), gases)
    assert [(g.tank_name, g.oxygen, g.helium, g.tank_role, g.start_pressure) for g in out] == [
        ("O2", 100.0, 0.0, "oxygen", 200.0), ("D1", 18.0, 45.0, "diluent", None), ("T3", 21.0, 0.0, None, None)]
    # two diluent transmitters: the loop's mix first, then the header's other enabled CC gas; DIL spelled out
    out = rebreather_gases(_ccr_log(cc_gases=((21, 0, True), (10, 70, True), (12, 60, False))),
                           [GasMixture(tank_name="dil"), GasMixture(tank_name="D2")])
    assert [(g.oxygen, g.helium, g.tank_role) for g in out] == [(18.0, 45.0, "diluent"), (21.0, 0.0, "diluent")]
    # a mix set in the app wins over the log; semi-closed counts as a rebreather
    out = rebreather_gases(_ccr_log(mode=4), [GasMixture(tank_name="D1", oxygen=32.0), GasMixture(tank_name="O2", oxygen=99.0)])
    assert [(g.oxygen, g.tank_role) for g in out] == [(32.0, "diluent"), (99.0, "oxygen")]
    # no loop samples: the header's CC gas
    out = rebreather_gases(_ccr_log(loop=()), [GasMixture(tank_name="D1")])
    assert (out[0].oxygen, out[0].helium) == (21.0, 0.0) and out[0].tank_role == "diluent"


def test_open_circuit_dives_keep_their_tanks():
    from src.core.services.shearwater_log import rebreather_gases
    gases = [GasMixture(tank_name="O2"), GasMixture(tank_name="D1")]
    assert rebreather_gases(_ccr_log(mode=1), gases) is None          # OC tec
    assert rebreather_gases(_ccr_log(), [GasMixture(tank_name="T1")]) is None   # nothing named
