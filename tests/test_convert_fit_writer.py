"""The Garmin FIT writer ``src/core/convert/fit_writer.py`` (plans/convert.md I12).

Save as .fit for a dive that was not opened from a ``.fit`` encodes the
dive from scratch with Garmin's FIT SDK. Pinned here against a rich
`UnifiedDive` built in the test (samples with depth, temperature, two tank
pressures, NDL, CNS, PO2, heart rate; two gases with a switch; two tanks;
GF, water type and density; dive number, positions, zone, computer,
surface interval, bottom time, CNS) written and read back with
`fit_reader` field by field within the FIT's precision, a hand-logged
dive without samples, and every committed ``.ssrf`` and UDDF fixture.
Every file written is decoded by ``fitdecode`` with the CRC checked and
by Garmin's own decoder with no errors.
"""
import glob
import io
import os
import re
import struct
from datetime import datetime

import fitdecode
import pytest
from garmin_fit_sdk import Decoder, Stream

from src.core.convert import fit_writer as module
from src.core.convert import formats
from src.core.convert.fit_patch import fit_crc
from src.core.convert.fit_reader import read_fit
from src.core.convert.fit_writer import (
    APP_PRODUCT,
    FIT_DROPS_PREFIX,
    TANK_SENSOR_BASE,
    encode_fit,
    fit_drops,
    write_fit,
)
from src.core.models import (
    EVENT_ALERT,
    EVENT_BOOKMARK,
    EVENT_GAS_SWITCH,
    EVENT_MODE_CHANGE,
    EVENT_SETPOINT_CHANGE,
    DiveEvent,
    GasMixture,
    SampleChannels,
    UnifiedDive,
    UnifiedSample,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


# --- helpers -------------------------------------------------------------------

def _decode(data):
    """The file decoded by fitdecode (CRC checked) and by Garmin's SDK (no
    errors); returns (fitdecode data frames by name, the SDK's messages)."""
    assert data[8:12] == b".FIT" and struct.unpack_from("<H", data, len(data) - 2)[0] == fit_crc(data[:-2])
    frames = {}
    with fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.RAISE,
                             error_handling=fitdecode.ErrorHandling.RAISE) as reader:
        for frame in reader:
            if frame.frame_type == fitdecode.FIT_FRAME_DATA:
                frames.setdefault(frame.name if frame.global_mesg_num != 147 else 147, []).append(frame)
    messages, errors = Decoder(Stream.from_bytes_io(io.BytesIO(data))).read()
    assert errors == [] and messages
    return frames, messages


def _v(frame, name, raw=False):
    """A field's value, None when the message does not carry it."""
    return frame.get_value(name, fallback=None, raw_value=raw)


def _samples():
    out = []
    depths = [0.5, 10.2, 20.123, 20.5, 5.0, 0.3]
    for i, depth in enumerate(depths):
        out.append(UnifiedSample(
            depth=depth, temp=28.0 - i, time=i * 60, pressure=200.0 - i * 5,
            channels=SampleChannels(pressures={0: 200.0 - i * 5, 1: 180.0 - i * 2.25}, ndl=3000 - i * 100, tts=i * 30,
                                    deco_stop_depth=0.0 if i < 3 else 3.0, deco_stop_time=0 if i < 3 else 120,
                                    cns=1.0 + i, ppo2=0.5 + i / 10.0, heart_rate=70 + i, gas_time=1000 - i * 10)))
    return out


def rich_dive(**changes):
    """A two-tank, two-gas dive with every field a FIT holds (and some it does not)."""
    base = dict(
        date_time=datetime(2026, 3, 14, 9, 30, 5), date_time_utc=datetime(2026, 3, 14, 2, 30, 5), timezone="UTC+07:00",
        duration=300, max_depth=20.5, avg_depth=11.2, temp_min=23.0, temp_max=28.0, temp_avg=26.0,
        gas_mixtures=[GasMixture(oxygen=32.0, helium=0.0, start_pressure=201.3, end_pressure=62.0, tank_volume=11.1, tank_name="Tank 1"),
                      GasMixture(oxygen=50.0, helium=0.0, start_pressure=180.0, end_pressure=150.5, tank_volume=7.0, tank_name="Deco")],
        dive_number=42, lat=-10.123456, lng=-30.654321, exit_lat=-10.124, exit_lng=-30.654,
        samples=_samples(),
        events=[DiveEvent(time=0, type=EVENT_GAS_SWITCH, tank=0, oxygen=32.0, helium=0.0),
                DiveEvent(time=180, type=EVENT_GAS_SWITCH, tank=1, oxygen=50.0, helium=0.0),
                DiveEvent(time=240, type=EVENT_ALERT, name="safety_stop_started", value=22.0)],
        computer_vendor="Garmin", computer_model="Descent Mk3(i) 51mm", computer_serial="1000000001", computer_firmware="7.05",
        gf_low=40, gf_high=85, deco_model="ZHL-16C", water_type="salt", water_density=1025.0, dive_mode="oc_multi_gas",
        surface_interval=5400, bottom_time=2410, cns_start=1.0, cns_end=12.0, device_logged=True,
        sac=11.2, rmv=22.53, otu=19.0,
    )
    base.update(changes)
    return UnifiedDive(**base)


# --- the rich dive -------------------------------------------------------------

def test_rich_dive_round_trips_through_the_reader(tmp_path):
    """Every field the FIT holds comes back within its precision: depth to
    1 mm, pressure to 0.01 bar, temperature to 1 °C, positions to 1e-6
    degree, the zone as UTC+07:00; the samples' channels one by one."""
    dive = rich_dive()
    out = tmp_path / "rich.fit"
    assert write_fit(dive, out) == []
    back = read_fit(str(out))
    assert (back.date_time, back.date_time_utc, back.timezone) == (dive.date_time, dive.date_time_utc, "UTC+07:00")
    assert (back.duration, back.max_depth, back.avg_depth) == (300, 20.5, 11.2)
    assert (back.temp_min, back.temp_max, back.temp_avg) == (23.0, 28.0, 26.0)
    assert back.dive_number == 42 and back.device_logged is True
    for got, want in ((back.lat, -10.123456), (back.lng, -30.654321), (back.exit_lat, -10.124), (back.exit_lng, -30.654)):
        assert got == pytest.approx(want, abs=1e-6)
    assert [(g.oxygen, g.helium, g.start_pressure, g.end_pressure, g.tank_volume, g.tank_name, g.tank_role) for g in back.gas_mixtures] == [
        (32.0, 0.0, 201.3, 62.0, 11.1, "Tank 1", None), (50.0, 0.0, 180.0, 150.5, 7.0, "Deco", None)]
    assert (back.computer_vendor, back.computer_model, back.computer_serial, back.computer_firmware) == (
        "Garmin", "Descent Mk3(i) 51mm", "1000000001", "7.05")
    assert (back.gf_low, back.gf_high, back.deco_model, back.water_type, back.water_density) == (40, 85, "ZHL-16C", "salt", 1025.0)
    assert (back.dive_mode, back.surface_interval, back.bottom_time, back.cns_start, back.cns_end) == ("oc_multi_gas", 5400, 2410, 1.0, 12.0)
    assert (back.sac, back.rmv, back.otu) == (11.2, 22.53, 19.0)
    assert back.events == dive.events
    assert len(back.samples) == len(dive.samples)
    for got, want in zip(back.samples, dive.samples):
        assert (got.time, got.depth, got.temp, got.pressure) == (want.time, pytest.approx(want.depth, abs=0.001), want.temp, want.pressure)
        assert got.channels.pressures == {k: pytest.approx(v, abs=0.01) for k, v in want.channels.pressures.items()}
        for name in ("ndl", "tts", "deco_stop_time", "cns", "heart_rate", "gas_time"):
            assert getattr(got.channels, name) == getattr(want.channels, name), name
        assert got.channels.deco_stop_depth == pytest.approx(want.channels.deco_stop_depth, abs=0.001)
        assert got.channels.ppo2 == pytest.approx(want.channels.ppo2, abs=0.01)
        assert got.channels.setpoint is None and got.channels.gf99 is None
    assert back.location is None and back.buddy is None and back.notes is None and back.weight is None
    assert fit_drops(dive) == []


def test_rich_dive_messages_and_both_decoders(tmp_path):
    """What the file holds, message by message, as fitdecode and Garmin's
    decoder see it: file_id, the creator and the two transmitters, the
    settings, two gases, the timer and the switches, a record and two
    tank readings per sample, two tank summaries and profiles, the dive
    summary, lap, session and activity with the local timestamp."""
    data = encode_fit(rich_dive())
    frames, messages = _decode(data)
    assert set(messages) >= {"file_id_mesgs", "device_info_mesgs", "dive_settings_mesgs", "dive_gas_mesgs", "event_mesgs",
                             "record_mesgs", "tank_update_mesgs", "tank_summary_mesgs", "dive_summary_mesgs", "lap_mesgs",
                             "session_mesgs", "activity_mesgs", "147"}
    file_id = frames["file_id"][0]
    assert (_v(file_id, "type"), _v(file_id, "manufacturer"), _v(file_id, "garmin_product", True),
            _v(file_id, "serial_number")) == ("activity", "garmin", 4223, 1000000001)
    assert _v(file_id, "time_created", True) == _v(frames["session"][0], "start_time", True)
    infos = frames["device_info"]
    assert [_v(i, "device_index", True) for i in infos] == [0, 1, 2]
    assert _v(infos[0], "software_version") == 7.05 and _v(infos[0], "source_type") == "local"
    assert [_v(i, "serial_number") for i in infos[1:]] == [TANK_SENSOR_BASE, TANK_SENSOR_BASE + 1]
    settings = frames["dive_settings"][0]
    assert (_v(settings, "model"), _v(settings, "gf_low"), _v(settings, "gf_high"), _v(settings, "water_type"),
            _v(settings, "water_density")) == ("zhl_16c", 40, 85, "salt", 1025.0)
    assert [(_v(g, "message_index"), _v(g, "oxygen_content"), _v(g, "helium_content"), _v(g, "status"))
            for g in frames["dive_gas"]] == [(0, 32, 0, "enabled"), (1, 50, 0, "enabled")]
    events = [(_v(e, "event"), _v(e, "event_type"), _v(e, "data", True)) for e in frames["event"]]
    assert events[0] == ("timer", "start", None) and events[-1] == ("timer", "stop_all", None)
    assert ("dive_gas_switched", "marker", 0) in events and ("dive_gas_switched", "marker", 1) in events
    assert ("dive_alert", "marker", 22) in events and _v(frames["event"][-2], "data") == "safety_stop_started"
    assert len(frames["record"]) == 6 and len(frames["tank_update"]) == 12
    record = frames["record"][3]
    assert (_v(record, "depth"), _v(record, "temperature"), _v(record, "ndl_time"), _v(record, "time_to_surface"),
            _v(record, "next_stop_depth"), _v(record, "next_stop_time"), _v(record, "cns_load"),
            _v(record, "po2"), _v(record, "heart_rate"), _v(record, "air_time_remaining")) == (
        20.5, 25, 2700, 90, 3.0, 120, 4, 0.8, 73, 970)
    assert [(_v(t, "sensor"), _v(t, "start_pressure"), _v(t, "end_pressure"), _v(t, "volume_used"))
            for t in frames["tank_summary"]] == [(TANK_SENSOR_BASE, 201.3, 62.0, 1546.23), (TANK_SENSOR_BASE + 1, 180.0, 150.5, 206.5)]
    profiles = frames[147]
    assert [(_v(p, 0), _v(p, 254), _v(p, 2), _v(p, 77)) for p in profiles] == [
        (TANK_SENSOR_BASE, 0, "Tank 1", 111), (TANK_SENSOR_BASE + 1, 1, "Deco", 70)]
    summary = frames["dive_summary"][0]
    assert (_v(summary, "dive_number"), _v(summary, "bottom_time"), _v(summary, "surface_interval"),
            _v(summary, "start_cns"), _v(summary, "end_cns"), _v(summary, "max_depth"), _v(summary, "avg_depth"),
            _v(summary, "reference_mesg")) == (42, 2410.0, 5400, 1, 12, 20.5, 11.2, "session")
    session, lap, activity = frames["session"][0], frames["lap"][0], frames["activity"][0]
    for m in (session, lap):
        assert (_v(m, "sport"), _v(m, "sub_sport"), _v(m, "total_timer_time"), _v(m, "total_elapsed_time")) == (
            "diving", "multi_gas_diving", 300.0, 300.0)
        assert (_v(m, "avg_temperature"), _v(m, "max_temperature"), _v(m, "min_temperature")) == (26, 28, 23)
        assert _v(m, "max_depth") == 20.5 and _v(m, "avg_depth") == 11.2
    assert _v(session, "start_position_lat", True) == int(round(-10.123456 * 2 ** 31 / 180))
    assert _v(session, "end_position_long", True) == int(round(-30.654 * 2 ** 31 / 180))
    start = _v(session, "start_time", True)
    assert (_v(activity, "timestamp", True), _v(activity, "local_timestamp", True)) == (start, start + 7 * 3600)
    assert _v(activity, "total_timer_time") == 300.0 and _v(activity, "num_sessions") == 1
    assert messages["session_mesgs"][0]["start_time"].strftime("%Y-%m-%d %H:%M:%S") == "2026-03-14 02:30:05"
    assert messages["record_mesgs"][2]["depth"] == 20.123


def test_drops_list_is_exact():
    """What a FIT cannot hold is named, in a fixed order, and only when the
    dive holds it; the written line is the registry's prefix plus the names."""
    dive = rich_dive(location="Reef", buddy="Kim", notes="n", weight=3.0, weight_unit="kilogram", visibility=10.0,
                     deco_model="VPM-B", computer_vendor="Shearwater", computer_model="Perdix 2", computer_serial="5555AAAA",
                     computer_firmware="v93", water_type="brackish", dive_mode="scr",
                     gas_mixtures=[GasMixture(oxygen=21.0, tank_role="backGas"), GasMixture(oxygen=50.0, tank_role="deco")],
                     samples=[UnifiedSample(depth=1.0, time=0, channels=SampleChannels(gf99=12.0, setpoint=1.3, o2_cells={0: 1.28, 1: 1.31}))],
                     events=[DiveEvent(time=0, type=EVENT_MODE_CHANGE, name="ccr"), DiveEvent(time=1, type=EVENT_SETPOINT_CHANGE, value=1.3),
                             DiveEvent(time=2, type=EVENT_BOOKMARK, name="Turtle"), DiveEvent(time=3, type=EVENT_GAS_SWITCH, oxygen=18.0, helium=45.0),
                             DiveEvent(time=4, type=EVENT_ALERT, name="Ascent rate", value=3.0), DiveEvent(time=5, type="tank_pod_connected", tank=0),
                             DiveEvent(time=6, type="odd_thing")],
                     external_ids={"garmin": "123"}, service_fields={"rating": 4, "tags": ["reef"], "suit": None, "divemaster": ""})
    assert fit_drops(dive) == [
        "site", "buddy", "notes", "weight", "visibility", "deco model", "computer maker", "computer model", "computer serial",
        "computer firmware", "water type", "dive mode", "tank role", "GF99 channel", "setpoint channel", "O2 cell readings",
        "mode change events", "setpoint change events", "bookmark events", "gas switch events without a gas",
        "alerts outside Garmin's list", "transmitter events without a tank", "odd thing events", "service ids", "rating", "tags"]
    assert fit_drops(rich_dive()) == []
    assert fit_drops(rich_dive(computer_vendor=None, computer_model=None)) == []
    assert fit_drops(rich_dive(computer_vendor="garmin", computer_model="descent x50i")) == []        # case-insensitive
    assert fit_drops(rich_dive(computer_vendor="Garmin", computer_model="Descent Mk9")) == ["computer model"]
    assert fit_drops(rich_dive(computer_serial="0")) == ["computer serial"]
    assert fit_drops(rich_dive(computer_firmware="700")) == ["computer firmware"]      # above uint16 hundredths
    assert fit_drops(rich_dive(deco_model="Bühlmann ZHL-16C")) == []
    assert fit_drops(rich_dive(dive_mode="ccr")) == [] and fit_drops(rich_dive(dive_mode="gauge")) == []
    assert fit_drops(rich_dive(events=[DiveEvent(time=0, type=EVENT_ALERT, name="48", value=48.0)])) == []   # the reader's own unnamed alert
    assert fit_drops(rich_dive(events=[DiveEvent(time=0, type=EVENT_ALERT, value=7.0)])) == ["alerts outside Garmin's list"]
    assert fit_drops(rich_dive(events=[DiveEvent(time=0, type="tank_pod_disconnected", tank=1)])) == []


def test_what_is_dropped_is_not_written_and_the_rest_is(tmp_path):
    """The dive of the drops test written and read back: a non-Garmin
    computer gives file_id's app product and no model, the odd mode and
    water type are left unset, the gas switch to an unlisted mix and the
    named alert are not in the file, the connect event names its transmitter."""
    dive = rich_dive(computer_vendor="Shearwater", computer_model="Perdix 2", computer_serial="5555AAAA", computer_firmware="v93",
                     deco_model="VPM-B", water_type="brackish", dive_mode="scr",
                     events=[DiveEvent(time=0, type=EVENT_GAS_SWITCH, oxygen=18.0, helium=45.0),
                             DiveEvent(time=1, type=EVENT_ALERT, name="Ascent rate", value=3.0),
                             DiveEvent(time=2, type=EVENT_GAS_SWITCH, oxygen=50.0, helium=0.0),        # by mix: the second entry
                             DiveEvent(time=5, type="tank_pod_connected", tank=1),
                             DiveEvent(time=9, type=EVENT_ALERT, name="48", value=48.0)])
    data = encode_fit(dive)
    frames, _ = _decode(data)
    file_id = frames["file_id"][0]
    assert _v(file_id, "garmin_product", True) == APP_PRODUCT == 65534 and _v(file_id, "serial_number") is None
    assert _v(frames["device_info"][0], "software_version") is None
    assert _v(frames["dive_settings"][0], "model") is None and _v(frames["dive_settings"][0], "water_type") is None
    assert _v(frames["session"][0], "sub_sport") is None and _v(frames["dive_settings"][0], "gf_low") == 40
    back = read_fit(data, name="x.fit")
    assert (back.computer_vendor, back.computer_model, back.computer_serial, back.computer_firmware) == ("Garmin", None, None, None)
    assert (back.deco_model, back.water_type, back.dive_mode, back.water_density) == (None, None, None, 1025.0)
    assert [e.model_dump() for e in back.events] == [
        {"time": 2, "type": EVENT_GAS_SWITCH, "tank": 1, "oxygen": 50.0, "helium": 0.0},
        {"time": 5, "type": "tank_pod_connected", "tank": 1},
        {"time": 9, "type": EVENT_ALERT, "name": "48", "value": 48.0}]


def test_values_the_dive_lacks_stay_missing(tmp_path):
    """No value is defaulted: a dive without gases, tanks, zone, positions,
    settings or summaries gives a file with none of them, which reads back
    with those fields unset (the local clock taken as UTC with no zone)."""
    dive = UnifiedDive(date_time=datetime(2026, 1, 2, 10, 0), duration=1100, max_depth=12.0,
                       samples=[UnifiedSample(depth=1.0, time=0), UnifiedSample(depth=12.0, time=600), UnifiedSample(depth=0.5, time=1100)])
    data = encode_fit(dive)
    frames, messages = _decode(data)
    assert "dive_gas" not in frames and "tank_summary" not in frames and "tank_update" not in frames and 147 not in frames
    assert "dive_settings" not in frames and len(frames["device_info"]) == 1
    assert _v(frames["activity"][0], "local_timestamp") is None
    assert _v(frames["session"][0], "start_position_lat") is None and _v(frames["session"][0], "sub_sport") is None
    summary = frames["dive_summary"][0]
    assert _v(summary, "dive_number") is None and _v(summary, "avg_depth") is None and _v(summary, "max_depth") == 12.0
    assert [_v(r, "temperature") for r in frames["record"]] == [None, None, None]
    back = read_fit(data, name="x.fit")
    assert (back.date_time, back.date_time_utc, back.timezone) == (datetime(2026, 1, 2, 10, 0), datetime(2026, 1, 2, 10, 0), None)
    assert back.gas_mixtures == [] and back.events == [] and back.dive_number is None and back.lat is None
    assert (back.gf_low, back.water_type, back.dive_mode, back.surface_interval, back.cns_end, back.temp_min) == (None,) * 6
    assert back.duration == 1100 and back.max_depth == 12.0 and back.avg_depth == pytest.approx(6.386, abs=0.001)   # the profile mean
    assert [s.depth for s in back.samples] == [1.0, 12.0, 0.5]


def test_hand_logged_dive_round_trips_its_summary(tmp_path):
    """A dive without samples: the summary, the tanks' pressures and the
    temperatures from the session; a zone given by name only; the gases
    without any tank data give no transmitter at all."""
    dive = UnifiedDive(date_time=datetime(2025, 7, 4, 16, 30), timezone="Europe/Stockholm", duration=1200, max_depth=5.0, avg_depth=3.0,
                       temp_min=18.0, temp_max=21.0, dive_number=7, device_logged=False,
                       gas_mixtures=[GasMixture(oxygen=21.0, start_pressure=200.0, end_pressure=120.0)],
                       lat=59.3, lng=18.1, water_type="fresh", surface_interval=86400, bottom_time=1150)
    out = tmp_path / "hand.fit"
    assert write_fit(dive, out) == []
    _decode(out.read_bytes())
    back = read_fit(str(out))
    assert (back.date_time, back.date_time_utc, back.timezone) == (datetime(2025, 7, 4, 16, 30), datetime(2025, 7, 4, 14, 30), "UTC+02:00")
    assert (back.duration, back.max_depth, back.avg_depth, back.temp_min, back.temp_max) == (1200, 5.0, 3.0, 18.0, 21.0)
    assert (back.dive_number, back.water_type, back.surface_interval, back.bottom_time) == (7, "fresh", 86400, 1150)
    assert back.lat == pytest.approx(59.3, abs=1e-6) and back.lng == pytest.approx(18.1, abs=1e-6)
    assert [(g.oxygen, g.start_pressure, g.end_pressure, g.tank_volume, g.tank_name) for g in back.gas_mixtures] == [(21.0, 200.0, 120.0, None, None)]
    assert back.samples == [] and back.events == [] and back.device_logged is True
    # gases alone: no tank message, the mixes read back without pressures
    plain = UnifiedDive(date_time=datetime(2025, 7, 4, 16, 30), duration=1200, max_depth=5.0,
                        gas_mixtures=[GasMixture(oxygen=32.0), GasMixture(oxygen=50.0)])
    frames, _ = _decode(encode_fit(plain))
    assert len(frames["dive_gas"]) == 2 and "tank_summary" not in frames and len(frames["device_info"]) == 1
    assert [(g.oxygen, g.start_pressure) for g in read_fit(encode_fit(plain), name="p.fit").gas_mixtures] == [(32.0, None), (50.0, None)]


def test_tanks_keep_their_index(tmp_path):
    """The reader pairs transmitter i with gas i, so every entry gets a
    transmitter once any has tank data - two tanks of the same mix stay
    two, an entry without pressures keeps its place - and a sample's lone
    ``pressure`` is the first tank's."""
    dive = UnifiedDive(date_time=datetime(2026, 8, 29, 9, 56, 11), duration=120, max_depth=10.0,
                       gas_mixtures=[GasMixture(oxygen=32.0), GasMixture(oxygen=21.0, start_pressure=204.29, end_pressure=164.37, tank_name="Tank 2"),
                                     GasMixture(oxygen=21.0, start_pressure=209.66, tank_volume=11.1)],
                       samples=[UnifiedSample(depth=1.0, time=0, pressure=100.0), UnifiedSample(depth=10.0, time=60, pressure=90.0)],
                       events=[DiveEvent(time=30, type=EVENT_GAS_SWITCH, tank=2)])
    back = read_fit(encode_fit(dive), name="t.fit")
    assert [(g.oxygen, g.start_pressure, g.end_pressure, g.tank_volume, g.tank_name) for g in back.gas_mixtures] == [
        (32.0, 100.0, 90.0, None, None), (21.0, 204.29, 164.37, None, "Tank 2"), (21.0, 209.66, None, 11.1, None)]   # the reader takes the first tank's first and last reading
    assert [s.channels.pressures for s in back.samples] == [{0: 100.0}, {0: 90.0}] and back.samples[1].pressure == 90.0
    assert back.events == [DiveEvent(time=30, type=EVENT_GAS_SWITCH, tank=2, oxygen=21.0, helium=0.0)]


def test_consumption_summary_and_diluent_role_round_trip():
    """The dive's SAC, RMV and OTU go into dive_summary (L/min in
    hundredths, OTU) with the pressure SAC Connect shows derived from the
    first tank's size, and come back through the reader; a dive without
    them writes none. A diluent tank's role is dive_gas.mode
    closed_circuit_diluent and comes back as the role; the other roles
    have no FIT slot and stay in the drops (R6)."""
    dive = rich_dive(gas_mixtures=[GasMixture(oxygen=21.0, helium=35.0, start_pressure=200.0, end_pressure=150.0, tank_volume=3.0, tank_role="diluent"),
                                   GasMixture(oxygen=100.0, start_pressure=200.0, end_pressure=120.0, tank_volume=3.0)],
                     dive_mode="ccr", sac=15.25, rmv=30.5, otu=42.0, events=[])
    assert fit_drops(dive) == []
    data = encode_fit(dive)
    frames, _ = _decode(data)
    summary = frames["dive_summary"][0]
    assert (_v(summary, "avg_volume_sac"), _v(summary, "avg_rmv"), _v(summary, "o2_toxicity")) == (15.25, 30.5, 42)
    assert _v(summary, "avg_pressure_sac") == pytest.approx(5.08)              # 15.25 L/min over a 3 L tank
    assert [_v(g, "mode") for g in frames["dive_gas"]] == ["closed_circuit_diluent", None]
    back = read_fit(data, name="ccr.fit")
    assert (back.sac, back.rmv, back.otu) == (15.25, 30.5, 42.0)
    assert [(g.oxygen, g.helium, g.tank_role) for g in back.gas_mixtures] == [(21.0, 35.0, "diluent"), (100.0, 0.0, None)]

    bare = rich_dive(sac=None, rmv=None, otu=None, gas_mixtures=[GasMixture(oxygen=32.0, tank_role="stage")], events=[])
    frames, _ = _decode(encode_fit(bare))
    summary = frames["dive_summary"][0]
    assert all(_v(summary, name) is None for name in ("avg_volume_sac", "avg_rmv", "o2_toxicity", "avg_pressure_sac"))
    assert fit_drops(bare) == ["tank role"]
    back = read_fit(encode_fit(bare), name="bare.fit")
    assert (back.sac, back.rmv, back.otu) == (None, None, None) and back.gas_mixtures[0].tank_role is None
    # a SAC without a tank size: the pressure SAC cannot be derived, the volume SAC is still written
    frames, _ = _decode(encode_fit(rich_dive(gas_mixtures=[GasMixture(oxygen=21.0)], events=[])))
    assert _v(frames["dive_summary"][0], "avg_pressure_sac") is None and _v(frames["dive_summary"][0], "avg_volume_sac") == 11.2


def test_precision_and_limits(tmp_path):
    """Values are rounded to the FIT's own precision (depth mm, pressure
    0.01 bar, volume 0.1 L, temperature 1 °C, CNS 1 %, PO2 0.01 bar capped
    at 2.54, uint8's 255 being "unset"); a half-hour zone (Asia/Kolkata) is written as its offset and
    read back as the fixed offset UTC+05:30 (R7); a serial too large for uint32z is left out."""
    dive = rich_dive(timezone="Asia/Kolkata", date_time_utc=datetime(2026, 3, 14, 4, 0, 5), computer_serial=str(2 ** 32),
                     gas_mixtures=[GasMixture(oxygen=31.96, start_pressure=201.3456, end_pressure=62.0049, tank_volume=11.123)],
                     samples=[UnifiedSample(depth=20.1234, temp=27.6, time=0, channels=SampleChannels(pressures={0: 150.556}, cns=12.4, ppo2=3.1)),
                              UnifiedSample(depth=20.1236, temp=27.4, time=1, channels=SampleChannels(pressures={0: 150.554}, cns=12.6, ppo2=1.004))],
                     events=[], cns_end=12.5)
    back = read_fit(encode_fit(dive), name="p.fit")
    assert (back.date_time, back.date_time_utc, back.timezone) == (datetime(2026, 3, 14, 9, 30, 5), datetime(2026, 3, 14, 4, 0, 5), "UTC+05:30")
    assert back.computer_serial is None and fit_drops(dive) == ["computer serial"]
    assert [(g.oxygen, g.start_pressure, g.end_pressure, g.tank_volume) for g in back.gas_mixtures] == [(32.0, 201.35, 62.0, 11.1)]
    assert [(s.depth, s.temp, s.channels.pressures[0], s.channels.cns, s.channels.ppo2) for s in back.samples] == [
        (20.123, 28.0, 150.56, 12.0, 2.54), (20.124, 27.0, 150.55, 13.0, 1.0)]
    assert back.cns_end == 13.0 or back.cns_end == 12.0    # a half rounds to the nearest even in Python


def test_write_fit_reports_the_drop_line_and_creates_the_folder(tmp_path):
    dive = rich_dive(location="Reef", buddy="Kim")
    out = tmp_path / "deep" / "er" / "d.fit"
    assert write_fit(dive, out) == [FIT_DROPS_PREFIX + "site, buddy"]
    assert FIT_DROPS_PREFIX == formats.FIT_DROPS_PREFIX
    assert out.read_bytes() == encode_fit(dive)
    assert read_fit(str(out)).dive_number == 42


@pytest.mark.parametrize("path", sorted(glob.glob(os.path.join(FIXTURES, "ssrf", "*.ssrf")) + glob.glob(os.path.join(FIXTURES, "uddf", "*.uddf"))))
def test_committed_fixtures_written_as_fit_read_back(tmp_path, path):
    """Every dive of the committed .ssrf and UDDF fixtures encodes to a file
    both decoders take, and reads back with its key values: start, number,
    duration, depths, gases and tank pressures, positions, GF, mode."""
    result = formats.read_file(path)
    assert result.dives
    for i, dive in enumerate(result.dives):
        out = tmp_path / f"{i}.fit"
        lines = write_fit(dive, out)
        assert lines == ([FIT_DROPS_PREFIX + ", ".join(fit_drops(dive))] if fit_drops(dive) else [])
        _decode(out.read_bytes())
        back = read_fit(str(out))
        assert (back.date_time, back.dive_number, back.duration, back.max_depth) == (dive.date_time, dive.dive_number, dive.duration, dive.max_depth)
        if dive.avg_depth is not None:
            assert back.avg_depth == pytest.approx(dive.avg_depth, abs=0.001)
        assert [(g.oxygen, g.helium) for g in back.gas_mixtures] == [(g.oxygen, g.helium) for g in dive.gas_mixtures]
        for got, want in zip(back.gas_mixtures, dive.gas_mixtures):
            for name in ("start_pressure", "end_pressure"):
                assert getattr(got, name) == (pytest.approx(getattr(want, name), abs=0.01) if getattr(want, name) is not None else None)
            assert got.tank_volume == (pytest.approx(want.tank_volume, abs=0.05) if want.tank_volume is not None else None)
        for name in ("lat", "lng", "exit_lat", "exit_lng"):
            assert getattr(back, name) == (pytest.approx(getattr(dive, name), abs=1e-6) if getattr(dive, name) is not None else None)
        assert (back.gf_low, back.gf_high, back.surface_interval, back.bottom_time) == (dive.gf_low, dive.gf_high, dive.surface_interval, dive.bottom_time)
        assert back.dive_mode == (dive.dive_mode if dive.dive_mode in module.SUB_SPORTS else None)
        assert len(back.samples) == len([s for s in dive.samples if s.time is not None])
        for got, want in zip(back.samples, dive.samples):
            assert (got.time, got.depth) == (want.time, pytest.approx(want.depth, abs=0.001))
            assert got.temp == (float(round(want.temp)) if want.temp is not None else None)
        assert back.location is None and back.buddy is None and back.notes is None


def test_synthetic_ssrf_values(tmp_path):
    """The synthetic Subsurface fixture, spot-checked: a Shearwater dive
    comes out as a Garmin file with its settings, tanks, channels and the
    events a FIT has, and the drops name the rest."""
    dive = formats.read_file(os.path.join(FIXTURES, "ssrf", "synthetic.ssrf")).dives[0]
    assert fit_drops(dive) == ["site", "buddy", "notes", "weight", "suit", "computer maker", "computer model", "tank role", "setpoint channel",
                               "alerts outside Garmin's list", "bookmark events", "mode change events", "setpoint change events",
                               "service ids", "divemaster", "rating", "visibility stars", "tags"]
    back = read_fit(encode_fit(dive), name="s.fit")
    assert (back.date_time, back.dive_number, back.duration, back.max_depth, back.avg_depth) == (datetime(2026, 3, 14, 9, 30, 5), 42, 2900, 31.45, 14.2)
    assert (back.computer_vendor, back.computer_model, back.computer_serial, back.computer_firmware) == ("Garmin", None, "1000000001", "93.00")
    assert (back.gf_low, back.gf_high, back.deco_model, back.water_type, back.water_density, back.dive_mode) == (40, 85, "ZHL-16C", "fresh", 1000.0, "oc_multi_gas")
    assert (back.surface_interval, back.bottom_time, back.cns_start, back.cns_end) == (5400, 2410, 1.0, 12.0)
    assert [(g.oxygen, g.start_pressure, g.end_pressure, g.tank_volume, g.tank_name) for g in back.gas_mixtures] == [
        (32.0, 201.3, 62.0, 11.1, "Tank 1"), (50.0, 180.0, 150.5, 7.0, "Deco <50>")]
    assert [(e.time, e.type, e.tank) for e in back.events] == [(0, EVENT_GAS_SWITCH, 0), (2500, EVENT_GAS_SWITCH, 1)]
    assert len(back.samples) == len(dive.samples) and any(s.channels.pressures.get(1) for s in back.samples)


def test_only_the_writer_imports_the_sdk():
    """Shipped code imports garmin_fit_sdk in fit_writer.py alone: the
    reader stays on fitdecode, the patcher and the merge are byte-level."""
    pattern = re.compile(r"^\s*(import|from)\s+garmin_fit_sdk", re.MULTILINE)
    importers = []
    for folder in (SRC, os.path.join(os.path.dirname(SRC), "desktop")):
        for root, _dirs, files in os.walk(folder):
            for name in files:
                if name.endswith(".py") and pattern.search(open(os.path.join(root, name), encoding="utf-8").read()):
                    importers.append(os.path.relpath(os.path.join(root, name), os.path.dirname(SRC)))
    assert importers == [os.path.join("src", "core", "convert", "fit_writer.py")]


def test_air_temperature_and_suit_are_named_among_the_drops():
    """R10: no FIT message holds an air temperature or a suit."""
    base = rich_dive()
    assert "air temperature" not in fit_drops(base) and "suit" not in fit_drops(base)
    dropped = fit_drops(rich_dive(air_temp=28.0, suit="5 mm"))
    assert dropped.index("air temperature") < dropped.index("suit")
    assert "suit" not in fit_drops(rich_dive(suit="  "))


# --- check.md B17: a rebreather's setpoints ------------------------------------

def _ccr_dive(**extra):
    samples = [UnifiedSample(depth=d, time=t, channels=SampleChannels(ppo2=p)) for t, d, p in
               ((0, 1.0, 0.7), (60, 10.0, 1.3), (600, 30.0, 1.3), (1200, 5.0, 1.29), (1500, 2.0, 0.7))]
    events = [DiveEvent(time=60, type=EVENT_SETPOINT_CHANGE, name="setpoint_switch_auto_high", value=1.3),
              DiveEvent(time=1500, type=EVENT_SETPOINT_CHANGE, name="setpoint_switch_manual_low", value=0.7)]
    for s, sp in zip(samples, (0.7, 1.3, 1.3, 1.3, 0.7)):
        s.channels.setpoint = sp
    return UnifiedDive(date_time=datetime(2026, 5, 1, 9, 0), date_time_utc=datetime(2026, 5, 1, 1, 0), timezone="UTC+08:00",
                       duration=1500, max_depth=30.0, dive_mode="ccr",
                       gas_mixtures=[GasMixture(oxygen=21.0, tank_role="diluent")], samples=samples, events=events, **extra)


def test_setpoint_switches_round_trip_as_garmin_alerts_and_dive_settings():
    """Garmin's setpoint switch alerts are the dive's setpoint_change events
    (value from dive_settings' low/high setpoint), and the per-sample
    setpoint follows them: low until the first switch to high. The writer
    encodes both back - the alerts, ccr_low/high_setpoint and the switch
    modes - so nothing about them is dropped."""
    dive = _ccr_dive()
    assert fit_drops(dive) == []
    assert module.setpoint_settings(dive) == {"ccr_high_setpoint": 1.3, "ccr_high_setpoint_switch_mode": "automatic",
                                              "ccr_low_setpoint": 0.7, "ccr_low_setpoint_switch_mode": "manual"}
    data = encode_fit(dive)
    frames, _sdk = _decode(data)
    settings = frames["dive_settings"][0]
    assert (settings.get_value("ccr_low_setpoint"), settings.get_value("ccr_high_setpoint")) == (0.7, 1.3)
    assert settings.get_value("ccr_low_setpoint_switch_mode") == "manual"
    back = read_fit(data)
    assert [(e.time, e.name, e.value) for e in back.events if e.type == EVENT_SETPOINT_CHANGE] == [
        (60, "setpoint_switch_auto_high", 1.3), (1500, "setpoint_switch_manual_low", 0.7)]
    assert [s.channels.setpoint for s in back.samples] == [0.7, 1.3, 1.3, 1.3, 0.7]


def test_a_setpoint_the_switches_do_not_give_is_dropped():
    """A setpoint channel other than what the switches give (another
    computer's), or a setpoint change without a Garmin name, has no place
    in a .fit and is named."""
    dive = _ccr_dive()
    dive.samples[2].channels.setpoint = 1.2
    assert fit_drops(dive) == ["setpoint channel"]
    dive = _ccr_dive()
    dive.events.append(DiveEvent(time=700, type=EVENT_SETPOINT_CHANGE, value=1.2))
    assert "setpoint change events" in fit_drops(dive)
    plain = rich_dive()
    assert module.setpoint_settings(plain) == {}


GARMIN_CCR = [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test_data", "misc_test_files", f)
              for f in ("garmin_2023-08-17-08-51-59.fit", "garmin_2023-10-21-12-13-38.fit")]


@pytest.mark.skipif(not all(os.path.isfile(p) for p in GARMIN_CCR), reason="the owner's test_data/misc_test_files are not present")
@pytest.mark.parametrize("path", GARMIN_CCR)
def test_owner_garmin_ccr_dive_has_its_setpoints(path):
    """A Descent CCR dive: switches to 1.3 and back to 0.7, every sample with
    a setpoint, and a .fit written from it keeps them."""
    dive = read_fit(path)
    switches = [(e.name, e.value) for e in dive.events if e.type == EVENT_SETPOINT_CHANGE]
    assert switches == [("setpoint_switch_auto_high", 1.3), ("setpoint_switch_auto_low", 0.7)]
    assert {s.channels.setpoint for s in dive.samples} == {0.7, 1.3} and dive.samples[0].channels.setpoint == 0.7
    dive.external_ids = {}
    assert "setpoint channel" not in fit_drops(dive)
    back = read_fit(encode_fit(dive))
    assert [s.channels.setpoint for s in back.samples] == [s.channels.setpoint for s in dive.samples]
