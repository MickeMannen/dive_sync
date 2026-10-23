"""UDDF 3.2 adapter (rework.md F2): read a hand-written export shaped like
Subsurface's, write a file from UnifiedDives, round-trip, and sync through
the engine. The carried fields of plans/convert.md I5 (per-sample channels,
events, the computer, gradient factors, weight, surface interval) are
written and read back, and a dive without them gives byte for byte the
file the writer produced before I5 (``tests/data/uddf/pre_i5_*.uddf``,
captured from the writer at 5bdfaa8)."""
import os
import re
from datetime import datetime

import pytest

from src.core.fields import common_default_links
from src.core.models import DiveEvent, GasMixture, SampleChannels, UnifiedDive, UnifiedSample
from src.core.services.subsurface import SubsurfaceAdapter
from src.core.services.uddf import (
    UddfAdapter,
    UddfDocument,
    _deco_model_of,
    events_by_sample,
    parse_uddf_utc,
    read_uddf,
    weight_kg,
)

from tests.test_link_engine import RecordingAdapter

DATA = os.path.join(os.path.dirname(__file__), "data", "uddf")

SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<uddf xmlns="http://www.streit.cc/uddf/3.2/" version="3.2.0">
  <generator><name>Subsurface</name><version>6.0</version></generator>
  <diver>
    <owner id="owner"><personal><firstname>Test</firstname></personal></owner>
    <buddy id="b1"><personal><firstname>Anna</firstname><lastname>Andersson</lastname></personal></buddy>
  </diver>
  <divesite>
    <site id="s1"><name>Zenobia</name><geography><latitude>34.887</latitude><longitude>33.657</longitude></geography></site>
  </divesite>
  <gasdefinitions>
    <mix id="air"><name>Air</name><o2>0.21</o2><he>0.0</he></mix>
    <mix id="ean32"><name>EAN32</name><o2>0.32</o2><he>0.0</he></mix>
  </gasdefinitions>
  <profiledata>
    <repetitiongroup id="rg1">
      <dive id="d1">
        <informationbeforedive>
          <link ref="s1"/><link ref="b1"/>
          <divenumber>148</divenumber>
          <datetime>2026-06-22T09:30:00</datetime>
        </informationbeforedive>
        <tankdata id="t1"><link ref="ean32"/><tankvolume>0.012</tankvolume><tankpressurebegin>21000000</tankpressurebegin><tankpressureend>6000000</tankpressureend></tankdata>
        <tankdata id="t2"><link ref="air"/><tankvolume>0.007</tankvolume></tankdata>
        <samples>
          <waypoint><depth>0.0</depth><divetime>0</divetime><temperature>297.15</temperature></waypoint>
          <waypoint><depth>31.4</depth><divetime>900</divetime><temperature>294.15</temperature></waypoint>
          <waypoint><depth>0.3</depth><divetime>3120</divetime></waypoint>
        </samples>
        <informationafterdive>
          <greatestdepth>31.4</greatestdepth>
          <averagedepth>18.2</averagedepth>
          <diveduration>3120</diveduration>
          <lowesttemperature>294.15</lowesttemperature>
          <visibility>25</visibility>
          <rating><ratingvalue>4</ratingvalue></rating>
          <notes><para>Great visibility.</para><para>Second line.</para></notes>
        </informationafterdive>
      </dive>
      <dive id="d2">
        <informationbeforedive><datetime>2026-06-23T10:00:00</datetime></informationbeforedive>
        <informationafterdive><greatestdepth>12</greatestdepth><diveduration>1800</diveduration></informationafterdive>
      </dive>
    </repetitiongroup>
  </profiledata>
</uddf>
"""


@pytest.fixture
def uddf_file(tmp_path):
    path = tmp_path / "export.uddf"
    path.write_text(SAMPLE, encoding="utf-8")
    return str(path)


def test_read_subsurface_style_export(uddf_file):
    _root, dives = read_uddf(uddf_file)
    assert len(dives) == 2
    d = dives[0]
    assert d.external_ids == {"uddf": "d1"} and d.date_time == datetime(2026, 6, 22, 9, 30)
    assert d.dive_number == 148 and d.duration == 3120 and d.max_depth == 31.4 and d.avg_depth == 18.2
    assert d.temp_min == 21.0 and d.location == "Zenobia" and (d.lat, d.lng) == (34.887, 33.657)
    assert d.buddy == "Anna Andersson" and d.notes == "Great visibility.\nSecond line."
    assert d.visibility == 25.0 and d.visibility_unit == "meter" and d.service_fields["rating"] == 4
    assert [(g.oxygen, g.tank_volume, g.start_pressure, g.end_pressure) for g in d.gas_mixtures] == \
        [(32.0, 12.0, 210.0, 60.0), (21.0, 7.0, None, None)]
    assert [(s.time, s.depth, s.temp) for s in d.samples] == [(0, 0.0, 24.0), (900, 31.4, 21.0), (3120, 0.3, None)]
    # minimal dive: id kept, derived fields default
    assert dives[1].external_ids["uddf"] == "d2" and dives[1].duration == 1800 and dives[1].max_depth == 12.0


def test_write_and_round_trip(tmp_path):
    path = str(tmp_path / "new.uddf")
    adapter = UddfAdapter(path)
    assert adapter.login() and adapter.fetch_dives() == []
    dive = UnifiedDive(
        date_time=datetime(2026, 9, 1, 10, 30), duration=2500, max_depth=21.5, avg_depth=12.0, temp_min=24.0,
        external_ids={"garmin": "555"}, dive_number=14, buddy="Bo Berg, Cid", notes="One\nTwo", location="Reef",
        lat=34.9, lng=33.7, visibility=30.0, visibility_unit="meter",
        gas_mixtures=[GasMixture(oxygen=32.0, start_pressure=210.0, end_pressure=60.0, tank_volume=12.0),
                      GasMixture(oxygen=21.0, helium=35.0, start_pressure=200.0, end_pressure=150.0, tank_volume=7.0)],
        samples=[UnifiedSample(depth=0.0, temp=25.0, time=0), UnifiedSample(depth=21.5, temp=24.0, time=600)],
        service_fields={"rating": 5},
    )
    new_id = adapter.add_dive(dive)
    assert new_id == "dive_sync-garmin-555" and os.path.exists(path)
    text = open(path, encoding="utf-8").read()
    assert text.startswith("<?xml") and 'xmlns="http://www.streit.cc/uddf/3.2/"' in text
    assert "<tankpressurebegin>21000000</tankpressurebegin>" in text and "<temperature>298.15</temperature>" in text
    assert "<o2>0.32</o2>" in text and "<he>0.35</he>" in text

    back = adapter.fetch_dives()
    assert len(back) == 1
    b = back[0]
    assert b.external_ids == {"uddf": new_id} and b.date_time == dive.date_time and b.duration == 2500
    assert b.max_depth == 21.5 and b.temp_min == 24.0 and b.location == "Reef" and (b.lat, b.lng) == (34.9, 33.7)
    assert b.buddy == "Bo Berg, Cid" and b.notes == "One\nTwo" and b.visibility == 30.0 and b.service_fields["rating"] == 5
    assert [(g.oxygen, g.helium, g.tank_volume, g.start_pressure) for g in b.gas_mixtures] == [(32.0, 0.0, 12.0, 210.0), (21.0, 35.0, 7.0, 200.0)]
    assert [(s.time, s.depth, s.temp) for s in b.samples] == [(0, 0.0, 25.0), (600, 21.5, 24.0)]

    # update keeps the id and position; sites and buddies are reused, not duplicated
    b.notes = "changed"
    assert adapter.update_dive(new_id, b)
    again = adapter.fetch_dives()[0]
    assert again.notes == "changed" and again.external_ids["uddf"] == new_id
    root, _ = read_uddf(path)
    assert len(root.findall("divesite/site")) == 1 and len(root.findall("diver/buddy")) == 2 and len(root.findall("gasdefinitions/mix")) == 2

    # a second dive without foreign ids gets a timestamp id; delete removes it
    second_id = adapter.add_dive(dive.model_copy(update={"external_ids": {}, "date_time": datetime(2026, 9, 2, 8, 0)}))
    assert second_id == "dive_sync-20260902T080000"
    assert len(adapter.fetch_dives()) == 2 and adapter.delete_dive(second_id) and len(adapter.fetch_dives()) == 1
    assert not adapter.delete_dive("nope") and not adapter.update_dive("nope", b)


def test_existing_export_is_preserved_when_adding(uddf_file):
    adapter = UddfAdapter(uddf_file)
    adapter.add_dive(UnifiedDive(date_time=datetime(2026, 7, 1, 8), duration=100, max_depth=5.0, location="Zenobia",
                                 lat=34.887, lng=33.657, buddy="Anna Andersson"))
    root, dives = read_uddf(uddf_file)
    assert len(dives) == 3 and dives[2].location == "Zenobia" and dives[2].buddy == "Anna Andersson"
    assert len(root.findall("divesite/site")) == 1 and len(root.findall("diver/buddy")) == 1   # matched, not duplicated
    assert root.find("generator/name").text == "Subsurface"                                    # untouched


def test_bad_file_fails_login(tmp_path):
    path = tmp_path / "bad.uddf"
    path.write_text("<uddf><broken", encoding="utf-8")
    assert UddfAdapter(str(path)).login() is False


def test_engine_pair_garmin_to_uddf(tmp_path):
    from src.core.config import ConfigManager, SettingsModel, SyncFilters
    from src.core.fields import common_default_links
    from src.core.sync_engine import SyncEngine
    from tests.test_link_engine import FakeGarmin, _dive

    settings = SettingsModel(sync_filters=SyncFilters(only_new=False), directionality="to_uddf",
                             field_links=common_default_links("garmin", "uddf"))
    spath = str(tmp_path / "settings.json")
    ConfigManager.save_settings(settings, spath)
    g = [_dive(external_ids={"garmin": "1"}, dive_number=3, buddy="Anna", location="Reef", lat=34.9, lng=33.7,
               gas_mixtures=[GasMixture(oxygen=32.0, start_pressure=200.0, tank_volume=11.1)])]
    uddf_path = str(tmp_path / "out.uddf")
    engine = SyncEngine(settings_path=spath, credentials_path=str(tmp_path / "c.json"),
                        source_adapter=FakeGarmin(g), target_adapter=UddfAdapter(uddf_path))
    res = engine.run_sync(dry_run=False)
    assert len(res["uploaded_to_uddf"]) == 1 and res["uploaded_to_uddf"][0]["new_id"] == "dive_sync-garmin-1"
    assert engine.load_links() == {"1": "dive_sync-garmin-1"}
    # second run pairs by the remembered id, no second upload
    engine2 = SyncEngine(settings_path=spath, credentials_path=str(tmp_path / "c.json"),
                         source_adapter=FakeGarmin(g), target_adapter=UddfAdapter(uddf_path))
    res = engine2.run_sync(dry_run=False)
    assert res["matched_count"] == 1 and res["uploaded_to_uddf"] == []


def test_two_dives_created_in_one_run(tmp_path):
    """save() used to namespace the in-memory tree through shared children,
    so the second add_dive of a run failed to find the repetition group."""
    adapter = UddfAdapter(str(tmp_path / "two.uddf"))
    assert adapter.login()
    first = adapter.add_dive(UnifiedDive(date_time=datetime(2026, 1, 1, 10), duration=1000, max_depth=10.0))
    second = adapter.add_dive(UnifiedDive(date_time=datetime(2026, 1, 2, 10), duration=1100, max_depth=12.0))
    assert first and second and first != second
    assert sorted(d.max_depth for d in UddfAdapter(adapter.path).fetch_dives()) == [10.0, 12.0]


# --- plans/convert.md I5: the carried fields -------------------------------------

def _pre_i5_dive():
    """Every field the writer knew before I5 (no weight, no carried fields):
    the dive ``tests/data/uddf/pre_i5_adapter.uddf`` was captured from."""
    return UnifiedDive(
        date_time=datetime(2026, 9, 1, 10, 30), date_time_utc=datetime(2026, 9, 1, 7, 30), timezone="Europe/Stockholm",
        duration=2500, max_depth=21.5, avg_depth=12.0, temp_min=24.0, temp_max=26.0, temp_avg=25.0,
        external_ids={"garmin": "555"}, dive_number=14, buddy="Bo Berg, Cid", notes="One\nTwo", location="Reef",
        lat=34.9, lng=33.7, visibility=30.0, visibility_unit="meter", device_logged=True,
        gas_mixtures=[GasMixture(oxygen=32.0, start_pressure=210.0, end_pressure=60.0, tank_volume=12.0, tank_name="T1", tank_role="backGas"),
                      GasMixture(oxygen=21.0, helium=35.0, start_pressure=200.0, end_pressure=150.0, tank_volume=7.0)],
        samples=[UnifiedSample(depth=0.0, temp=25.0, time=0), UnifiedSample(depth=21.5, temp=24.0, time=600)],
        service_fields={"rating": 5, "activityName": "x"},
    )


def test_output_without_carried_fields_is_byte_for_byte_as_before_i5(tmp_path):
    """The I5 extensions write nothing for a dive that has none of the new
    fields and no per-sample pressure: a sync's UDDF is the file it was
    (a sample's pressure is written since the owner's 2026-10-02 follow-up,
    pinned by test_sync_from_subsurface_writes_the_pressure_profile)."""
    path = str(tmp_path / "pinned.uddf")
    adapter = UddfAdapter(path)
    assert adapter.login()
    adapter.add_dive(_pre_i5_dive())
    adapter.add_dive(UnifiedDive(date_time=datetime(2026, 1, 2, 10), duration=1100, max_depth=12.0))
    expected = open(os.path.join(DATA, "pre_i5_adapter.uddf"), "rb").read()
    assert open(path, "rb").read() == expected


def test_sync_run_output_is_byte_for_byte_as_before_i5(tmp_path):
    """The same pin through the engine (FakeGarmin -> UddfAdapter), the
    path a scheduled sync takes."""
    from src.core.config import ConfigManager, SettingsModel, SyncFilters
    from src.core.fields import common_default_links
    from src.core.sync_engine import SyncEngine
    from tests.test_link_engine import FakeGarmin, _dive

    settings = SettingsModel(sync_filters=SyncFilters(only_new=False), directionality="to_uddf",
                             field_links=common_default_links("garmin", "uddf"))
    spath = str(tmp_path / "settings.json")
    ConfigManager.save_settings(settings, spath)
    g = [_dive(external_ids={"garmin": "1"}, dive_number=3, buddy="Anna", location="Reef", lat=34.9, lng=33.7, notes="hello",
               visibility=20.0, visibility_unit="meter", temp_min=22.0, avg_depth=10.0,
               gas_mixtures=[GasMixture(oxygen=32.0, start_pressure=200.0, end_pressure=50.0, tank_volume=11.1)],
               samples=[UnifiedSample(depth=0.0, time=0, temp=23.0), UnifiedSample(depth=18.0, time=300, temp=22.0)])]
    uddf_path = str(tmp_path / "out.uddf")
    engine = SyncEngine(settings_path=spath, credentials_path=str(tmp_path / "c.json"),
                        source_adapter=FakeGarmin(g), target_adapter=UddfAdapter(uddf_path))
    assert len(engine.run_sync(dry_run=False)["uploaded_to_uddf"]) == 1
    assert open(uddf_path, "rb").read() == open(os.path.join(DATA, "pre_i5_sync.uddf"), "rb").read()


def _rich_dive(**overrides):
    """A dive with every carried field the UDDF writer has a slot for, and
    a few it has none for (tts, the transmitter events, water, bottom time)."""
    base = dict(
        date_time=datetime(2026, 8, 29, 9, 56, 11), duration=2780, max_depth=11.92, temp_min=30.0, dive_number=38,
        weight=4.0, weight_unit="kilogram", surface_interval=497326, bottom_time=2599,
        computer_vendor="Garmin", computer_model="Descent X50i", computer_serial="1000000001", computer_firmware="7.05",
        gf_low=40, gf_high=85, deco_model="ZHL-16C", water_type="salt", water_density=1025.0, dive_mode="ccr",
        gas_mixtures=[GasMixture(oxygen=21.0, start_pressure=204.29, end_pressure=164.37, tank_volume=11.1),
                      GasMixture(oxygen=50.0, start_pressure=209.66, end_pressure=161.75, tank_volume=11.1)],
        samples=[
            UnifiedSample(depth=1.2, temp=31.0, time=0, pressure=204.29,
                          channels=SampleChannels(pressures={0: 204.29, 1: 209.66}, ndl=5940, cns=0.0, ppo2=0.24, heart_rate=72,
                                                  setpoint=0.7, gf99=0.0, gas_time=8762, tts=51, deco_stop_depth=0.0, deco_stop_time=0)),
            UnifiedSample(depth=11.9, temp=30.0, time=600, pressure=180.0,
                          channels=SampleChannels(pressures={0: 180.0}, deco_stop_depth=3.0, deco_stop_time=120, cns=12.5, ppo2=1.3, gf99=45.5)),
            UnifiedSample(depth=5.0, temp=30.0, time=1200),
            UnifiedSample(depth=0.1, temp=30.0, time=2720, channels=SampleChannels(pressures={1: 161.75}, ndl=99 * 60, heart_rate=80)),
        ],
        events=[
            DiveEvent(time=0, type="gas_switch", tank=0, oxygen=21.0, helium=0.0),
            DiveEvent(time=3, type="tank_pod_connected", tank=0),           # no UDDF slot
            DiveEvent(time=601, type="alert", name="ascent_critical", value=17.0),   # lands on the 600 s waypoint
            DiveEvent(time=600, type="gas_switch", oxygen=50.0, helium=0.0),  # by mix, no tank
            DiveEvent(time=1200, type="mode_change", name="oc_multi_gas"),
            DiveEvent(time=1205, type="setpoint_change", value=1.2),
            DiveEvent(time=2720, type="bookmark", name="Turtle"),
            DiveEvent(time=2721, type="alert", name="near_surface"),
        ],
    )
    base.update(overrides)
    return UnifiedDive(**base)


def test_carried_fields_are_written_in_uddf_units_and_read_back(tmp_path):
    path = str(tmp_path / "rich.uddf")
    document = UddfDocument()
    dive_id = document.write_dive(_rich_dive())
    document.save(path)
    text = open(path, encoding="utf-8").read()

    # the computer, once, under diver/owner/equipment; firmware in a note (UDDF has no element for it)
    assert text.count("<divecomputer ") == 1 and '<divecomputer id="dc-Garmin-1000000001">' in text
    assert "rebreather" not in text and "measuredpo2" not in text     # no cells on this dive: no rebreather equipment (R18)
    assert "<model>Descent X50i</model>" in text and "<serialnumber>1000000001</serialnumber>" in text
    assert "<manufacturer id=\"man-Garmin\">" in text and "<para>Firmware 7.05</para>" in text
    assert text.index("<owner") < text.index("<divesite")
    # the gradient factors as a root decomodel between gasdefinitions and profiledata, linked from the dive
    assert text.index("</gasdefinitions>") < text.index("<decomodel>") < text.index("<profiledata>")
    assert '<buehlmann id="buehlmann-ZHL-16C-gf40-85">' in text
    assert "<gradientfactorhigh>85</gradientfactorhigh>" in text and "<gradientfactorlow>40</gradientfactorlow>" in text
    assert '<link ref="buehlmann-ZHL-16C-gf40-85" />' in text
    # informationbeforedive: surface interval in seconds, the equipment used (computer link, lead in kg)
    assert "<surfaceintervalbeforedive>\n            <passedtime>497326</passedtime>" in text
    assert '<equipmentused>\n            <link ref="dc-Garmin-1000000001" />\n            <leadquantity>4</leadquantity>' in text
    # the first waypoint: channels in SI (Pa, s, %), both tanks' pressures by tankdata id, the mode, the switch
    first = text[text.index("<waypoint>"):text.index("</waypoint>")]
    assert "<calculatedpo2>24000</calculatedpo2>" in first and "<cns>0</cns>" in first
    assert '<divemode type="closedcircuit" />' in first and "<gradientfactor>0</gradientfactor>" in first
    assert "<heartbeat>72</heartbeat>" in first and "<nodecotime>5940</nodecotime>" in first
    assert "<remainingbottomtime>8762</remainingbottomtime>" in first and "<setpo2>70000</setpo2>" in first
    assert '<switchmix ref="mix-21-0" />' in first
    assert f'<tankpressure ref="{dive_id}-tank0">20429000</tankpressure>' in first
    assert f'<tankpressure ref="{dive_id}-tank1">20966000</tankpressure>' in first
    assert "decostop" not in first and "<alarm>" not in first      # a 0 m next stop is "no stop", not a stop
    # the second waypoint: the alert of the second after lands here (nearest earlier sample), a mandatory stop, the switch by mix
    second = text.split("<waypoint>")[2]
    assert "<alarm>ascent_critical</alarm>" in second and '<decostop kind="mandatory" decodepth="3" duration="120" />' in second
    assert '<switchmix ref="mix-50-0" />' in second and "<cns>12.5</cns>" in second and "<gradientfactor>45.5</gradientfactor>" in second
    # the third: a mode change and a setpoint change land on it; the fourth: a bookmark and the last alert
    third, fourth = text.split("<waypoint>")[3], text.split("<waypoint>")[4]
    assert '<divemode type="opencircuit" />' in third and "<setpo2>120000</setpo2>" in third
    assert "<setmarker>Turtle</setmarker>" in fourth and "<alarm>near_surface</alarm>" in fourth
    # what UDDF has no slot for is absent
    for word in ("tts", "tank_pod", "<bottomtime", "salt", "1025", "2599"):
        assert word not in text, word

    _root, back = read_uddf(path)
    b = back[0]
    assert (b.computer_vendor, b.computer_model, b.computer_serial, b.computer_firmware) == ("Garmin", "Descent X50i", "1000000001", "7.05")
    assert (b.gf_low, b.gf_high, b.deco_model) == (40, 85, "ZHL-16C")
    assert b.surface_interval == 497326 and b.weight == 4.0 and b.weight_unit == "kilogram" and b.dive_mode == "ccr"
    assert b.samples[0].pressure == 204.29 and b.samples[0].channels.pressures == {0: 204.29, 1: 209.66}
    assert b.samples[0].channels.model_dump() == {"pressures": {0: 204.29, 1: 209.66}, "ndl": 5940, "cns": 0.0, "ppo2": 0.24,
                                                   "setpoint": 0.7, "gf99": 0.0, "gas_time": 8762, "heart_rate": 72}
    assert b.samples[1].channels.model_dump() == {"pressures": {0: 180.0}, "deco_stop_depth": 3.0, "deco_stop_time": 120,
                                                   "cns": 12.5, "ppo2": 1.3, "gf99": 45.5}
    assert b.samples[1].pressure == 180.0 and b.samples[2].pressure is None
    assert b.samples[3].channels.pressures == {1: 161.75} and b.samples[3].pressure is None and b.samples[3].channels.heart_rate == 80
    # the events come back on their waypoint's second, the unwritable one is gone
    assert [e.model_dump() for e in b.events] == [
        {"time": 0, "type": "gas_switch", "tank": 0, "oxygen": 21.0, "helium": 0.0},
        {"time": 600, "type": "gas_switch", "tank": 1, "oxygen": 50.0, "helium": 0.0},   # a waypoint's switch is read before its alarm
        {"time": 600, "type": "alert", "name": "ascent_critical"},
        {"time": 1200, "type": "mode_change", "name": "oc_multi_gas"},
        {"time": 2720, "type": "alert", "name": "near_surface"},      # alarms before markers within a waypoint
        {"time": 2720, "type": "bookmark", "name": "Turtle"},
    ]
    # the setpoint change came back as the waypoint's setpoint channel
    assert b.samples[2].channels.setpoint == 1.2
    # water, bottom time and tts have no slot and stay unset
    assert b.water_type is None and b.bottom_time is None and all(s.channels is None or s.channels.tts is None for s in b.samples)


def test_setpoint_change_lands_as_setpo2_on_a_sample_without_channels(tmp_path):
    dive = _rich_dive(samples=[UnifiedSample(depth=1.0, time=0), UnifiedSample(depth=10.0, time=100)],
                      events=[DiveEvent(time=100, type="setpoint_change", value=1.2)])
    document = UddfDocument()
    document.write_dive(dive)
    document.save(str(tmp_path / "sp.uddf"))
    back = read_uddf(str(tmp_path / "sp.uddf"))[1][0]
    assert back.samples[1].channels.setpoint == 1.2 and back.samples[0].channels is None


def test_weight_in_pounds_is_written_in_kilograms(tmp_path):
    document = UddfDocument()
    document.write_dive(UnifiedDive(date_time=datetime(2026, 1, 1, 10), duration=100, max_depth=5.0, weight=10.0, weight_unit="pound"))
    document.save(str(tmp_path / "w.uddf"))
    text = open(tmp_path / "w.uddf", encoding="utf-8").read()
    assert "<leadquantity>4.536</leadquantity>" in text and "<equipmentused>" in text and "divecomputer" not in text
    back = read_uddf(str(tmp_path / "w.uddf"))[1][0]
    assert back.weight == 4.536 and back.weight_unit == "kilogram" and back.computer_model is None


def test_computer_and_decomodel_are_shared_between_dives_and_kept_on_update(tmp_path):
    path = str(tmp_path / "shared.uddf")
    adapter = UddfAdapter(path)
    assert adapter.login()
    first = adapter.add_dive(_rich_dive())
    adapter.add_dive(_rich_dive(date_time=datetime(2026, 8, 30, 9, 0), events=[], samples=[]))
    # a second computer (other serial) and other gradient factors get their own entries
    adapter.add_dive(_rich_dive(date_time=datetime(2026, 8, 31, 9, 0), computer_serial="2", gf_low=30, events=[], samples=[]))
    # no serial: matched by vendor and model
    adapter.add_dive(_rich_dive(date_time=datetime(2026, 9, 1, 9, 0), computer_serial=None, events=[], samples=[]))
    root, dives = read_uddf(path)
    assert len(dives) == 4
    assert [dc.get("id") for dc in root.findall("diver/owner/equipment/divecomputer")] == [
        "dc-Garmin-1000000001", "dc-Garmin-2", "dc-Garmin-Descent_X50i"]
    assert [m.get("id") for m in root.findall("decomodel/buehlmann")] == ["buehlmann-ZHL-16C-gf40-85", "buehlmann-ZHL-16C-gf30-85"]
    assert [d.computer_serial for d in dives] == ["1000000001", "1000000001", "2", None]
    assert [d.gf_low for d in dives] == [40, 40, 30, 40]
    # an update rewrites the dive but adds no second computer or model
    dives[0].notes = "changed"
    assert adapter.update_dive(first, dives[0])
    root, again = read_uddf(path)
    assert len(root.findall(".//divecomputer")) == 3 and len(root.findall("decomodel/buehlmann")) == 2
    assert again[0].notes == "changed" and again[0].computer_serial == "1000000001" and again[0].samples[0].channels.pressures == {0: 204.29, 1: 209.66}


def test_existing_owner_is_reused_for_the_computer(uddf_file):
    """The sample export has an owner already: the computer goes under it,
    and the Subsurface generator and buddies stay."""
    adapter = UddfAdapter(uddf_file)
    adapter.add_dive(_rich_dive(events=[], samples=[]))
    root, dives = read_uddf(uddf_file)
    assert len(root.findall("diver/owner")) == 1 and root.find("diver/owner").get("id") == "owner"
    assert root.find("diver/owner/equipment/divecomputer/model").text == "Descent X50i"
    assert dives[2].computer_model == "Descent X50i" and root.find("generator/name").text == "Subsurface"


SHEARWATER_STYLE = """<?xml version="1.0" encoding="utf-8"?>
<uddf xmlns="http://www.streit.cc/uddf/3.2/" version="3.2.3">
  <generator><name>Shearwater Cloud Desktop</name></generator>
  <diver><owner><equipment>
    <divecomputer id="Perdix 2_A0000001"><name>Perdix 2</name>
      <manufacturer id="Shearwater_Research_Inc"><name>Shearwater Research, Inc</name></manufacturer>
      <model>Perdix 2</model><serialnumber>A0000001</serialnumber>
      <notes><para>FirmV:100:</para></notes></divecomputer>
  </equipment></owner></diver>
  <gasdefinitions>
    <mix id="OC1:21/00"><name>OC1</name><o2>0.21</o2><he>0</he></mix>
    <mix id="OC2:50/00"><name>OC2</name><o2>0.5</o2><he>0</he></mix>
  </gasdefinitions>
  <decomodel><buehlmann id="zhl16c"><gradientfactorhigh>85</gradientfactorhigh><gradientfactorlow>40</gradientfactorlow></buehlmann></decomodel>
  <profiledata><repetitiongroup><dive id="1">
    <informationbeforedive>
      <link ref="zhl16c" /><divenumber>452</divenumber><datetime>2025-10-19T15:21:08Z</datetime>
      <surfaceintervalbeforedive><passedtime>10440</passedtime></surfaceintervalbeforedive>
      <equipmentused><link ref="Perdix 2_A0000001" /></equipmentused>
    </informationbeforedive>
    <tankdata id="T1"><link ref="OC1:21/00" /><tankpressurebegin>20256804</tankpressurebegin><tankpressureend>9000000</tankpressureend></tankdata>
    <tankdata id="T2"><link ref="OC2:50/00" /></tankdata>
    <samples>
      <waypoint><calculatedpo2>0.23</calculatedpo2><depth>1</depth><divetime>0</divetime><switchmix ref="OC1:21/00" />
        <tankpressure ref="T1">20256804</tankpressure><temperature>303.15</temperature><divemode type="opencircuit" /><gradientfactor>0</gradientfactor></waypoint>
      <waypoint><depth>20</depth><divetime>600</divetime><tankpressure ref="T1">15000000</tankpressure><tankpressure ref="T2">20000000</tankpressure>
        <temperature>303.15</temperature><divemode type="opencircuit" /><gradientfactor>30</gradientfactor><nodecotime>1200</nodecotime></waypoint>
      <waypoint><depth>6</depth><divetime>1500</divetime><switchmix ref="OC2:50/00" /><divemode type="opencircuit" />
        <decostop kind="mandatory" decodepth="6" duration="180" /><cns>8</cns></waypoint>
    </samples>
    <informationafterdive><greatestdepth>25.3</greatestdepth><diveduration>1788</diveduration></informationafterdive>
  </dive></repetitiongroup></profiledata>
</uddf>
"""


def _ccr_dive() -> UnifiedDive:
    """A synthetic rebreather dive with three O2 cells (plans/gap-roadmap.md
    R18): every cell on the first sample, one cell missing on the second,
    none on the third."""
    return UnifiedDive(
        date_time=datetime(2026, 6, 1, 10, 0, 0), duration=1800, max_depth=30.0, dive_mode="ccr",
        gas_mixtures=[GasMixture(oxygen=21.0, helium=0.0, tank_role="diluent", start_pressure=200.0),
                      GasMixture(oxygen=100.0, helium=0.0, tank_role="oxygen", start_pressure=180.0)],
        samples=[
            UnifiedSample(depth=0.0, temp=25.0, time=0,
                          channels=SampleChannels(ppo2=0.70, setpoint=0.7, o2_cells={0: 0.69, 1: 0.71, 2: 0.70}, heart_rate=70, ndl=5940)),
            UnifiedSample(depth=30.0, temp=24.0, time=600,
                          channels=SampleChannels(ppo2=1.21, setpoint=1.2, o2_cells={0: 1.22, 2: 1.19})),
            UnifiedSample(depth=0.0, temp=25.0, time=1800, channels=SampleChannels(ppo2=0.7, setpoint=0.7)),
        ],
    )


def test_o2_cells_are_written_as_measuredpo2_per_o2sensor_and_read_back(tmp_path):
    """UDDF's rebreather cells: one ``rebreather`` with an ``o2sensor`` per
    cell under the owner's equipment, linked from the dive's equipmentused,
    and on each waypoint one ``measuredpo2`` (Pa) per cell referring to its
    sensor, between heartbeat and nodecotime; reading back gives the same
    cells by the sensors' order in the file."""
    path = str(tmp_path / "ccr.uddf")
    document = UddfDocument()
    document.write_dive(_ccr_dive())
    document.save(path)
    text = open(path, encoding="utf-8").read()
    equipment = text[text.index("<equipment>"):text.index("</equipment>")]
    assert equipment.count("<rebreather ") == 1 and '<rebreather id="rebreather-dive_sync">' in equipment
    assert [f'<o2sensor id="o2sensor-{n}">' in equipment for n in (1, 2, 3)] == [True, True, True] and "o2sensor-4" not in text
    assert "<name>Cell 1</name>" in equipment and text.index("<owner") < text.index("<divesite")
    assert '<equipmentused>\n            <link ref="rebreather-dive_sync" />' in text
    first, second, third = text.split("<waypoint>")[1:4]
    assert '<measuredpo2 ref="o2sensor-1">69000</measuredpo2>' in first
    assert '<measuredpo2 ref="o2sensor-2">71000</measuredpo2>' in first
    assert '<measuredpo2 ref="o2sensor-3">70000</measuredpo2>' in first
    assert first.index("<heartbeat>") < first.index("<measuredpo2") < first.index("<nodecotime>")
    assert "<calculatedpo2>70000</calculatedpo2>" in first and "<setpo2>70000</setpo2>" in first
    assert '<measuredpo2 ref="o2sensor-1">122000</measuredpo2>' in second and '<measuredpo2 ref="o2sensor-3">119000</measuredpo2>' in second
    assert "o2sensor-2" not in second and "measuredpo2" not in third

    _root, back = read_uddf(path)
    cells = [s.channels.o2_cells if s.channels else {} for s in back[0].samples]
    assert cells == [{0: 0.69, 1: 0.71, 2: 0.7}, {0: 1.22, 2: 1.19}, {}]
    assert [s.channels.ppo2 for s in back[0].samples] == [0.7, 1.21, 0.7] and back[0].samples[1].channels.setpoint == 1.2

    # a second dive in the same file shares the rebreather and its sensors
    document = UddfDocument(_root)
    document.write_dive(_ccr_dive(), "second")
    document.save(path)
    text = open(path, encoding="utf-8").read()
    assert text.count("<rebreather ") == 1 and text.count("<o2sensor ") == 3 and text.count("<measuredpo2 ") == 10


FOREIGN_CELLS = """<?xml version="1.0" encoding="utf-8"?>
<uddf xmlns="http://www.streit.cc/uddf/3.2/" version="3.2.1">
  <generator><name>other</name></generator>
  <diver><owner id="me"><equipment>
    <rebreather id="unit"><name>Unit</name><o2sensor id="cell_c" /><o2sensor id="cell_a" /></rebreather>
  </equipment></owner></diver>
  <profiledata><repetitiongroup><dive id="1">
    <informationbeforedive><datetime>2026-06-01T10:00:00</datetime></informationbeforedive>
    <samples>
      <waypoint><depth>0</depth><divetime>0</divetime><measuredpo2 ref="cell_a">7.0e4</measuredpo2><measuredpo2 ref="cell_c">7.1e4</measuredpo2></waypoint>
      <waypoint><depth>20</depth><divetime>60</divetime><measuredpo2 ref="cell_b">1.2e5</measuredpo2><measuredpo2 ref="cell_a">1.21e5</measuredpo2></waypoint>
    </samples>
    <informationafterdive><greatestdepth>20</greatestdepth><diveduration>60</diveduration></informationafterdive>
  </dive></repetitiongroup></profiledata>
</uddf>
"""


def test_measuredpo2_refs_follow_the_files_o2sensor_order_and_an_undeclared_one_gets_the_next_cell(tmp_path):
    path = tmp_path / "foreign.uddf"
    path.write_text(FOREIGN_CELLS, encoding="utf-8")
    _root, dives = read_uddf(str(path))
    cells = [s.channels.o2_cells for s in dives[0].samples]
    # cell_c is the file's first sensor (index 0), cell_a its second (1); cell_b, declared nowhere, becomes cell 2
    assert cells == [{0: 0.71, 1: 0.7}, {1: 1.21, 2: 1.2}]


def test_read_shearwater_style_export(tmp_path):
    """The dialect Shearwater Cloud writes (plans/convert.md Findings 6):
    tankpressure by tankdata id, switchmix, divemode, gradientfactor, the
    computer under the owner, a decomodel named zhl16c."""
    path = tmp_path / "shearwater.uddf"
    path.write_text(SHEARWATER_STYLE, encoding="utf-8")
    d = read_uddf(str(path))[1][0]
    assert (d.computer_vendor, d.computer_model, d.computer_serial, d.computer_firmware) == ("Shearwater Research, Inc", "Perdix 2", "A0000001", None)
    assert (d.gf_low, d.gf_high, d.deco_model) == (40, 85, "ZHL-16C") and d.surface_interval == 10440
    assert d.dive_mode == "oc_multi_gas"          # two mixes
    assert [s.channels.model_dump() if s.channels else None for s in d.samples] == [
        {"pressures": {0: 202.57}, "ppo2": 0.23, "gf99": 0.0},
        {"pressures": {0: 150.0, 1: 200.0}, "ndl": 1200, "gf99": 30.0},
        {"deco_stop_depth": 6.0, "deco_stop_time": 180, "cns": 8.0},
    ]
    assert [s.pressure for s in d.samples] == [202.57, 150.0, None]
    assert [e.model_dump() for e in d.events] == [
        {"time": 0, "type": "gas_switch", "tank": 0, "oxygen": 21.0, "helium": 0.0},
        {"time": 1500, "type": "gas_switch", "tank": 1, "oxygen": 50.0, "helium": 0.0},
    ]


def test_read_other_dialects(tmp_path):
    """ATMOS writes divemode as text and equipmentused after the dive;
    Oceanic+ writes leadquantity; an unknown tankpressure ref on a one-tank
    dive is that tank's; a mode change in the waypoints is an event."""
    text = SAMPLE.replace(
        '<tankdata id="t2"><link ref="air"/><tankvolume>0.007</tankvolume></tankdata>', "").replace(
        "<waypoint><depth>0.0</depth><divetime>0</divetime><temperature>297.15</temperature></waypoint>",
        '<waypoint><depth>0.0</depth><divetime>0</divetime><divemode>closedcircuit</divemode><tankpressure>20000000</tankpressure></waypoint>').replace(
        "<waypoint><depth>31.4</depth><divetime>900</divetime><temperature>294.15</temperature></waypoint>",
        '<waypoint><depth>31.4</depth><divetime>900</divetime><divemode type="opencircuit"/><alarm>deco</alarm></waypoint>').replace(
        "<datetime>2026-06-22T09:30:00</datetime>",
        "<datetime>2026-06-22T09:30:00</datetime><equipmentused><leadquantity>6.5</leadquantity></equipmentused>").replace(
        "<visibility>25</visibility>",
        '<visibility>25</visibility><equipmentused><link ref="dc"/></equipmentused>').replace(
        '<owner id="owner"><personal><firstname>Test</firstname></personal></owner>',
        '<owner id="owner"><equipment><divecomputer id="dc"><name>Mission One</name><serialnumber>M1</serialnumber></divecomputer></equipment></owner>')
    path = tmp_path / "other.uddf"
    path.write_text(text, encoding="utf-8")
    d = read_uddf(str(path))[1][0]
    assert d.weight == 6.5 and d.weight_unit == "kilogram"
    assert (d.computer_vendor, d.computer_model, d.computer_serial) == (None, "Mission One", "M1")
    assert d.dive_mode == "ccr" and d.samples[0].pressure == 200.0 and d.samples[0].channels is None   # a lone tank-0 pressure is just .pressure
    assert [e.model_dump() for e in d.events] == [
        {"time": 900, "type": "alert", "name": "deco"}, {"time": 900, "type": "mode_change", "name": "oc_single_gas"}]
    assert d.gf_low is None and d.deco_model is None and d.surface_interval is None


def test_helpers():
    assert weight_kg(10.0, "pound") == pytest.approx(4.5359237) and weight_kg(10.0, "lbs") == pytest.approx(4.5359237)
    assert weight_kg(4.0, "kilogram") == 4.0 and weight_kg(4.0, None) == 4.0
    assert _deco_model_of("buehlmann-ZHL-16C-gf40-85") == "ZHL-16C" and _deco_model_of("zhl16c") == "ZHL-16C"
    assert _deco_model_of("zhl_16b") == "ZHL-16B" and _deco_model_of("vpm-b") is None
    samples = [UnifiedSample(depth=0.0, time=0), UnifiedSample(depth=5.0, time=10), UnifiedSample(depth=6.0), UnifiedSample(depth=7.0, time=30)]
    events = [DiveEvent(time=0, type="gas_switch", tank=0), DiveEvent(time=12, type="alert", name="x"),
              DiveEvent(time=29, type="bookmark"), DiveEvent(time=30, type="alert", name="y"), DiveEvent(time=99, type="alert", name="late"),
              DiveEvent(time=5, type="tank_pod_connected", tank=0)]
    by_sample = events_by_sample(samples, events)
    assert {i: [e.name or e.type for e in evs] for i, evs in by_sample.items()} == {
        0: ["gas_switch"], 2: ["x", "bookmark"], 3: ["y", "late"]}  # the untimed sample counts at the previous second (10 s); the pod event is dropped
    assert events_by_sample([], events) == {}
    assert events_by_sample(samples, [DiveEvent(time=-5, type="alert", name="early")]) == {0: [DiveEvent(time=-5, type="alert", name="early")]}


def test_parse_uddf_utc_takes_a_non_zero_offset_only():
    """plans/gap-roadmap.md R11: a datetime with an offset gives the UTC
    instant; a Z or +00:00 does not (the Shearwater app writes its local
    clock with a Z), nor does a datetime without one."""
    assert parse_uddf_utc("2026-06-22T09:30:00+08:00") == datetime(2026, 6, 22, 1, 30)
    assert parse_uddf_utc("2026-06-22T09:30:00-0330") == datetime(2026, 6, 22, 13, 0)
    assert parse_uddf_utc("2026-06-22T09:30+05:45") == datetime(2026, 6, 22, 3, 45)
    for text in ("2026-06-22T09:30:00", "2026-06-22T09:30:00Z", "2026-06-22T09:30:00+00:00", "2026-06-22", "", "garbage+08:00"):
        assert parse_uddf_utc(text) is None, text


def test_offset_in_the_datetime_gives_the_utc_instant_and_keeps_the_local_clock(tmp_path):
    path = tmp_path / "offset.uddf"
    path.write_text(SAMPLE.replace("<datetime>2026-06-22T09:30:00</datetime>",
                                   "<datetime>2026-06-22T09:30:00+08:00</datetime>"), encoding="utf-8")
    dives = read_uddf(str(path))[1]
    assert dives[0].date_time == datetime(2026, 6, 22, 9, 30) and dives[0].date_time_utc == datetime(2026, 6, 22, 1, 30)
    assert dives[0].timezone is None                       # no zone name: a Garmin upload keeps its own zone
    assert dives[1].date_time_utc is None                  # no offset written
    # the Shearwater app's Z is its local clock: no UTC instant
    sw = tmp_path / "shearwater.uddf"
    sw.write_text(SHEARWATER_STYLE, encoding="utf-8")
    d = read_uddf(str(sw))[1][0]
    assert d.date_time == datetime(2025, 10, 19, 15, 21, 8) and d.date_time_utc is None
    # writing is unchanged: a dive added to the file is written without an offset
    UddfAdapter(str(path)).add_dive(_rich_dive(events=[], samples=[]))
    assert re.search(r"<datetime>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d</datetime>", path.read_text(encoding="utf-8").split("</dive>")[-2])


class FakeSubsurface(RecordingAdapter):
    service_id = "subsurface"
    display_name = "Subsurface"
    _catalog = SubsurfaceAdapter.field_catalog()


def test_sync_from_subsurface_writes_the_pressure_profile(tmp_path):
    """A sample with a pressure and no channels (what the Shearwater and
    Subsurface adapters produce) is written as the first
    tank's ``tankpressure`` (owner, 2026-10-02): the new output of a sync
    is pinned in ``tests/data/uddf/subsurface_sync.uddf``, and it reads
    back as the source produced it (pressure set, no channels), so a
    second run sees no change."""
    from src.core.config import ConfigManager, SettingsModel, SyncFilters
    from src.core.fields import common_default_links
    from src.core.sync_engine import SyncEngine

    settings = SettingsModel(sync_filters=SyncFilters(only_new=False), directionality="to_uddf",
                             field_links=common_default_links("subsurface", "uddf"))
    spath = str(tmp_path / "settings.json")
    ConfigManager.save_settings(settings, spath)
    source = [UnifiedDive(
        date_time=datetime(2026, 8, 29, 12, 52, 59), duration=4132, max_depth=13.82, avg_depth=8.545, temp_min=30.0,
        external_ids={"subsurface": "2026/08/29-Sat-12=52=59/Dive-13"}, dive_number=13, location="Reef", lat=7.6, lng=98.4,
        gas_mixtures=[GasMixture(oxygen=21.0, start_pressure=204.0, end_pressure=160.0, tank_volume=11.094),
                      GasMixture(oxygen=21.0, start_pressure=210.0, end_pressure=165.0, tank_volume=11.094, tank_role="not_used")],
        samples=[UnifiedSample(depth=1.2, temp=31.0, time=1, pressure=204.0), UnifiedSample(depth=8.0, temp=30.0, time=600, pressure=190.5),
                 UnifiedSample(depth=13.82, temp=30.0, time=1200), UnifiedSample(depth=0.3, temp=31.0, time=4132, pressure=160.0)],
    )]
    uddf_path = str(tmp_path / "out.uddf")
    engine = SyncEngine(settings_path=spath, credentials_path=str(tmp_path / "c.json"),
                        source_adapter=FakeSubsurface(source), target_adapter=UddfAdapter(uddf_path))
    assert len(engine.run_sync(dry_run=False)["uploaded_to_uddf"]) == 1
    text = open(uddf_path, encoding="utf-8").read()
    assert text.count("<tankpressure ") == 3 and text.count('<tankpressure ref="dive_sync-subsurface-2026/08/29-Sat-12=52=59/Dive-13-tank0">') == 3
    assert "<tankpressure ref=\"dive_sync-subsurface-2026/08/29-Sat-12=52=59/Dive-13-tank0\">19050000</tankpressure>" in text
    assert open(uddf_path, "rb").read() == open(os.path.join(DATA, "subsurface_sync.uddf"), "rb").read()
    back = UddfAdapter(uddf_path).fetch_dives()[0]
    assert [(s.time, s.pressure, s.channels) for s in back.samples] == [(1, 204.0, None), (600, 190.5, None), (1200, None, None), (4132, 160.0, None)]
    # the second run pairs the dive and finds nothing to update
    again = SyncEngine(settings_path=spath, credentials_path=str(tmp_path / "c.json"),
                       source_adapter=FakeSubsurface(source), target_adapter=UddfAdapter(uddf_path)).run_sync(dry_run=False)
    assert again["matched_count"] == 1 and again["uploaded_to_uddf"] == [] and again.get("updated_uddf", []) == []
    # no tank: nothing to refer the pressure to, so none is written
    document = UddfDocument()
    document.write_dive(UnifiedDive(date_time=datetime(2026, 1, 1), duration=10, max_depth=1.0, samples=[UnifiedSample(depth=1.0, time=0, pressure=100.0)]))
    document.save(str(tmp_path / "notank.uddf"))
    assert "tankpressure" not in open(tmp_path / "notank.uddf", encoding="utf-8").read()


SUBSURFACE_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "subsurface_cloud")


@pytest.mark.skipif(not os.path.isdir(SUBSURFACE_FIXTURE), reason="subsurface fixture missing")
def test_subsurface_fixture_dive_keeps_its_pressure_profile_through_uddf(tmp_path):
    """The real two-sensor Subsurface dive: every sample's (first sensor)
    pressure is written and read back unchanged, and since R12 the second
    sensor's and the other channels go along."""
    from src.core.services.subsurface import SubsurfaceRepo, dive_to_unified
    repo = SubsurfaceRepo(SUBSURFACE_FIXTURE).load()
    dive = dive_to_unified(repo.find("2026/08/29-Sat-12=52=59/Dive-13"), repo.sites)
    with_pressure = [s for s in dive.samples if s.pressure is not None]
    both = [s for s in dive.samples if s.channels is not None and len(s.channels.pressures) == 2]
    tank1 = [s for s in dive.samples if s.channels is not None and 1 in s.channels.pressures]
    assert len(with_pressure) > 100 and len(both) > 100
    document = UddfDocument()
    dive_id = document.write_dive(dive)
    document.save(str(tmp_path / "fixture.uddf"))
    text = open(tmp_path / "fixture.uddf", encoding="utf-8").read()
    assert text.count(f'ref="{dive_id}-tank0"') == len(with_pressure) and text.count(f'ref="{dive_id}-tank1"') == len(tank1)
    back = read_uddf(str(tmp_path / "fixture.uddf"))[1][0]
    assert [(s.time, s.pressure) for s in back.samples] == [(s.time, s.pressure) for s in dive.samples]
    assert [s.channels.pressures for s in back.samples if s.channels] == [s.channels.pressures for s in dive.samples if s.channels]


def test_air_temperature_and_suit_round_trip(tmp_path):
    """R10: the air temperature is ``informationbeforedive/airtemperature``
    in kelvin; the suit is a ``suit`` under the owner's equipment, named
    once and linked from the dive's ``equipmentused``; both read back, and
    a file written without them is unchanged. No shipped rule syncs them."""
    from src.core.fields import common_default_links
    path = str(tmp_path / "r10.uddf")
    adapter = UddfAdapter(path)
    assert adapter.login()
    dive = UnifiedDive(date_time=datetime(2026, 9, 1, 10, 30), duration=2500, max_depth=21.5, air_temp=31.5, suit="5 mm wetsuit",
                       gas_mixtures=[GasMixture(oxygen=21.0)])
    adapter.add_dive(dive)
    adapter.add_dive(UnifiedDive(date_time=datetime(2026, 9, 2, 10, 30), duration=2500, max_depth=21.5, suit="5 mm wetsuit"))
    adapter.add_dive(UnifiedDive(date_time=datetime(2026, 9, 3, 10, 30), duration=2500, max_depth=21.5, suit="drysuit", weight=4.0, weight_unit="kilogram"))
    text = open(path, encoding="utf-8").read()
    assert "<airtemperature>304.65</airtemperature>" in text and text.count("<airtemperature>") == 1
    assert text.count("<suit ") == 2 and '<suit id="suit-5_mm_wetsuit">\n          <name>5 mm wetsuit</name>' in text   # named once, reused
    assert text.count('<link ref="suit-5_mm_wetsuit"') == 2 and text.count('<link ref="suit-drysuit"') == 1
    back = adapter.fetch_dives()
    assert [(d.air_temp, d.suit) for d in back] == [(31.5, "5 mm wetsuit"), (None, "5 mm wetsuit"), (None, "drysuit")]
    assert back[2].weight == 4.0                                    # the equipmentused carries the lead too
    # the catalogue names both (so a rule can sync them); the shipped defaults do not
    specs = {f.name: f for f in UddfAdapter.field_catalog()}
    assert specs["air_temp"].unified == "air_temp" and specs["air_temp"].unit == "°C" and specs["suit"].unified == "suit"
    for link in common_default_links("subsurface", "uddf"):
        assert not link.target.endswith((".air_temp", ".suit", ".water_type")), link.target
    # a suit under a buddy's equipment, or no name: still read / still nothing
    other = tmp_path / "other.uddf"
    other.write_text("""<uddf version="3.2.0"><diver><owner id="o"><equipment><suit id="s1"><name>Shorty</name></suit></equipment></owner></diver>
<profiledata><repetitiongroup><dive id="a"><informationbeforedive><datetime>2026-06-23T10:00:00</datetime>
<airtemperature>293.15</airtemperature><equipmentused><link ref="s1"/><link ref="nothing"/></equipmentused></informationbeforedive>
<informationafterdive><greatestdepth>12</greatestdepth><diveduration>1800</diveduration></informationafterdive></dive>
<dive id="b"><informationbeforedive><datetime>2026-06-24T10:00:00</datetime></informationbeforedive>
<informationafterdive><greatestdepth>12</greatestdepth><diveduration>1800</diveduration></informationafterdive></dive>
</repetitiongroup></profiledata></uddf>""", encoding="utf-8")
    _root, dives = read_uddf(str(other))
    assert [(d.air_temp, d.suit) for d in dives] == [(20.0, "Shorty"), (None, None)]


def test_sync_rule_carries_air_temperature_and_suit_to_uddf(tmp_path):
    """R10 end to end: a rule on ``uddf.air_temp`` / ``uddf.suit`` /
    ``uddf.water_type`` (not shipped; drawn on the board) copies them on
    an upload and on a later change, like any other field."""
    from src.core.config import ConfigManager, SettingsModel, SyncFilters, SyncPairModel
    from src.core.fields import FieldLink, common_default_links
    from src.core.sync_engine import SyncEngine
    links = common_default_links("subsurface", "uddf") + [
        FieldLink(id="air_temp", source=["subsurface.air_temp"], target="uddf.air_temp", conflict="prefer_source"),
        FieldLink(id="suit", source=["subsurface.suit"], target="uddf.suit", conflict="prefer_source"),
    ]
    # a pair's board lives on its sync_pairs entry (the top-level keys describe the garmin_divelogs pair only)
    pair = SyncPairModel(id="s2u", source="subsurface:x", target="uddf:x.uddf", directionality="to_uddf", field_links=links)
    settings = SettingsModel(sync_filters=SyncFilters(only_new=False), sync_pairs=[pair])
    spath = str(tmp_path / "settings.json")
    ConfigManager.save_settings(settings, spath)
    source = [UnifiedDive(date_time=datetime(2026, 8, 29, 12, 52, 59), duration=4132, max_depth=13.82,
                          external_ids={"subsurface": "2026/08/29-Sat-12=52=59/Dive-13"}, air_temp=30.0, suit="3 mm")]
    uddf_path = str(tmp_path / "out.uddf")

    def run():
        return SyncEngine(settings_path=spath, credentials_path=str(tmp_path / "c.json"),
                          source_adapter=FakeSubsurface(source), target_adapter=UddfAdapter(uddf_path)).run_sync(dry_run=False)
    assert len(run()["uploaded_to_uddf"]) == 1
    back = UddfAdapter(uddf_path).fetch_dives()[0]
    assert (back.air_temp, back.suit) == (30.0, "3 mm")
    again = run()
    assert again["matched_count"] == 1 and again["uploaded_to_uddf"] == [] and again.get("updated_on_uddf", []) == []
    source[0].air_temp, source[0].suit = 28.0, "5 mm"
    assert len(run()["updated_on_uddf"]) == 1
    back = UddfAdapter(uddf_path).fetch_dives()[0]
    assert (back.air_temp, back.suit) == (28.0, "5 mm")


def test_exit_position_round_trips_in_applicationdata(tmp_path):
    """plans/gap-roadmap.md R8: UDDF 3.2 has no element for a second
    position, so the exit position goes into the dive's ``applicationdata``
    - the container the spec provides for program-specific data - as
    ``dive_sync/exitposition/latitude|longitude`` in decimal degrees, placed
    after ``informationbeforedive`` as the spec's examples do, and is read
    back from there; a dive without one writes no applicationdata."""
    path = str(tmp_path / "exit.uddf")
    adapter = UddfAdapter(path)
    assert adapter.login()
    adapter.add_dive(UnifiedDive(date_time=datetime(2026, 9, 1, 10, 30), duration=2500, max_depth=21.5, location="Drift wall",
                                 lat=7.609369, lng=98.378959, exit_lat=7.6123456, exit_lng=98.3812344))
    adapter.add_dive(UnifiedDive(date_time=datetime(2026, 9, 2, 10, 30), duration=2500, max_depth=21.5, exit_lat=1.0))
    text = open(path, encoding="utf-8").read()
    assert text.count("<applicationdata>") == 1
    flat = re.sub(r">\s+<", "><", text)
    assert ("</informationbeforedive><applicationdata><dive_sync><exitposition>"
            "<latitude>7.612346</latitude><longitude>98.381234</longitude></exitposition></dive_sync></applicationdata>") in flat
    assert text.index("<applicationdata>") < text.index("<informationafterdive>")
    back = adapter.fetch_dives()
    assert [(d.exit_lat, d.exit_lng) for d in back] == [(7.612346, 98.381234), (None, None)]
    assert back[0].lat == pytest.approx(7.609369, abs=1e-3) and back[0].lng == pytest.approx(98.378959, abs=1e-3)  # the site, as before R8
    # an update keeps it; clearing it removes the element
    adapter.update_dive(back[0].external_ids["uddf"], back[0])
    assert open(path, encoding="utf-8").read().count("<exitposition>") == 1
    back[0].exit_lat = back[0].exit_lng = None
    adapter.update_dive(back[0].external_ids["uddf"], back[0])
    assert "applicationdata" not in open(path, encoding="utf-8").read()
    # the catalogue names it as one gps field; no shipped board draws a rule for it
    specs = {f.name: f for f in UddfAdapter.field_catalog()}
    assert specs["exit_gps"].type == "gps" and specs["exit_gps"].unified == "exit_gps"
    for link in common_default_links("subsurface", "uddf") + common_default_links("garmin", "uddf"):
        assert not link.target.endswith(".exit_gps"), link.target
    # another program's applicationdata is not mistaken for ours
    other = tmp_path / "other.uddf"
    other.write_text("""<uddf version="3.2.0"><profiledata><repetitiongroup><dive id="d1">
      <informationbeforedive><datetime>2026-01-01T10:00:00</datetime></informationbeforedive>
      <applicationdata><hargikas><ambient>25.0</ambient></hargikas></applicationdata>
      <informationafterdive><greatestdepth>10</greatestdepth><diveduration>600</diveduration></informationafterdive>
    </dive></repetitiongroup></profiledata></uddf>""", encoding="utf-8")
    assert read_uddf(str(other))[1][0].exit_lat is None


# --- check.md B12/B13: the Shearwater app's own UDDF dialect ------------------
# Synthetic, written for these tests: the app's generator name, PO2s in bar,
# tankdata without ids referred to as T<n>, unused 0/0 slots, a pressure of 0
# for "unknown" and a transmitter status word exported as ~564 bar.
SHEARWATER_APP_DIALECT = """<?xml version="1.0" encoding="utf-8"?>
<uddf xmlns="http://www.streit.cc/uddf/3.2/" version="3.2.3">
  <generator><name>Shearwater Cloud Desktop</name><type>logbook</type></generator>
  <gasdefinitions><mix id="CC1:21/00"><name>CC1</name><o2>0.21</o2><he>0</he></mix></gasdefinitions>
  <profiledata><repetitiongroup><dive id="77">
    <informationbeforedive><divenumber>12</divenumber><datetime>2026-03-01T10:00:00Z</datetime></informationbeforedive>
    <tankdata><tankpressurebegin>20000000</tankpressurebegin><tankpressureend>12000000</tankpressureend></tankdata>
    <tankdata><tankpressurebegin>0</tankpressurebegin><tankpressureend>0</tankpressureend></tankdata>
    <tankdata><tankpressurebegin>0</tankpressurebegin><tankpressureend>0</tankpressureend></tankdata>
    <tankdata><tankpressurebegin>0</tankpressurebegin><tankpressureend>5000000</tankpressureend></tankdata>
    <tankdata />
    <samples>
      <waypoint><calculatedpo2>0.7</calculatedpo2><depth>0</depth><divetime>0</divetime><switchmix ref="CC1:21/00" />
        <tankpressure ref="T1">20000000</tankpressure><tankpressure ref="T4">56454000</tankpressure>
        <divemode type="closedcircuit" /><setpo2>0.7</setpo2></waypoint>
      <waypoint><calculatedpo2>1.29</calculatedpo2><depth>30</depth><divetime>600</divetime>
        <tankpressure ref="T1">16000000</tankpressure><tankpressure ref="T4">7000000</tankpressure>
        <divemode type="closedcircuit" /><setpo2>1.3</setpo2></waypoint>
      <waypoint><depth>0</depth><divetime>1200</divetime>
        <tankpressure ref="T1">12000000</tankpressure><tankpressure ref="T4">5000000</tankpressure></waypoint>
    </samples>
    <informationafterdive><greatestdepth>30</greatestdepth><diveduration>1200</diveduration></informationafterdive>
  </dive></repetitiongroup></profiledata>
</uddf>
"""


def test_shearwater_app_file_reads_po2_as_bar_and_tanks_by_position(tmp_path):
    """B12: the app's PO2s are bar, keyed on its generator name. B13: T<n>
    is the n-th tankdata, the unused slots are left out (T4 kept although
    T2 and T3 are dropped), a 0 pressure is unknown and the status word is
    no reading."""
    path = tmp_path / "app.uddf"
    path.write_text(SHEARWATER_APP_DIALECT, encoding="utf-8")
    d = read_uddf(str(path))[1][0]
    assert [(g.tank_name, g.start_pressure, g.end_pressure) for g in d.gas_mixtures] == [("T1", 200.0, 120.0), ("T4", None, 50.0)]
    assert [s.channels.pressures for s in d.samples] == [{0: 200.0}, {0: 160.0, 1: 70.0}, {0: 120.0, 1: 50.0}]
    assert [(s.channels.ppo2, s.channels.setpoint) for s in d.samples] == [(0.7, 0.7), (1.29, 1.3), (None, None)]
    assert d.dive_mode == "ccr"


def test_po2_is_pascal_in_any_other_file(tmp_path):
    """The standard stays: the same values under another generator are pascal."""
    text = SHEARWATER_APP_DIALECT.replace("Shearwater Cloud Desktop", "Some Logbook").replace(
        "<calculatedpo2>1.29</calculatedpo2>", "<calculatedpo2>129000</calculatedpo2>").replace(
        "<setpo2>1.3</setpo2>", "<setpo2>130000</setpo2>")
    path = tmp_path / "other.uddf"
    path.write_text(text, encoding="utf-8")
    d = read_uddf(str(path))[1][0]
    assert (d.samples[1].channels.ppo2, d.samples[1].channels.setpoint) == (1.29, 1.3)
    # outside the app's files a 0 pressure is 0 bar, and the ~564 bar reading is kept
    assert d.gas_mixtures[1].start_pressure == 0.0 and d.samples[0].channels.pressures[1] == pytest.approx(564.54)


def test_a_tankdata_id_wins_over_the_position():
    """A ref that is a tankdata's id is that tank, even when it reads like T<n>."""
    from xml.etree import ElementTree as ET
    from src.core.services.uddf import _read_tanks
    dive = ET.fromstring('<dive><tankdata id="T2"><link ref="m"/></tankdata><tankdata id="T1"><link ref="m"/></tankdata>'
                         '<samples><waypoint><tankpressure ref="T1">1e7</tankpressure></waypoint></samples></dive>')
    tanks, refs, _mix = _read_tanks(dive, {"m": (32.0, 0.0)})
    assert refs == {"T1": 1, "T2": 0} and [t.tank_name for t in tanks] == ["T2", "T1"]


def test_writing_into_a_shearwater_app_file_converts_it_to_pascal(tmp_path):
    """DiveSync never writes bar: a dive added to the app's file turns the
    whole document standard (PO2s in pascal, the 0 'unknown' pressures
    gone, the status words dropped, DiveSync as the generator), and the app's dive reads back the same."""
    path = tmp_path / "app.uddf"
    path.write_text(SHEARWATER_APP_DIALECT, encoding="utf-8")
    before = read_uddf(str(path))[1][0]
    adapter = UddfAdapter(str(path))
    assert adapter.login()
    adapter.add_dive(UnifiedDive(date_time=datetime(2026, 3, 2, 10, 0), duration=600, max_depth=10.0))
    text = path.read_text(encoding="utf-8")
    assert "<calculatedpo2>129000</calculatedpo2>" in text and "<setpo2>70000</setpo2>" in text
    assert "Shearwater Cloud Desktop" not in text and "<tankpressurebegin>0</tankpressurebegin>" not in text
    after = read_uddf(str(path))[1][0]
    assert after.gas_mixtures == before.gas_mixtures
    assert [s.channels for s in after.samples] == [s.channels for s in before.samples]


MISC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test_data", "misc_test_files")
SW_CCR_UDDF = os.path.join(MISC, "ccr_dive_uddf.uddf")
SW_PERDIX_UDDF = os.path.join(MISC, "SwedenDivePerdix2 2025-6-22 11-8-32.uddf")
SW_DB = os.path.join(os.path.dirname(__file__), "data", "shearwater", "dive_data.db")


@pytest.mark.skipif(not os.path.isfile(SW_CCR_UDDF), reason="the owner's test_data/misc_test_files are not present")
def test_owner_shearwater_ccr_uddf_has_two_tanks_with_pressures_and_po2_in_bar():
    d = read_uddf(SW_CCR_UDDF)[1][0]
    assert [g.tank_name for g in d.gas_mixtures] == ["T1", "T2"]
    assert all(len(s.channels.pressures) == 2 for s in d.samples[:20])
    po2 = [s.channels.ppo2 for s in d.samples if s.channels and s.channels.ppo2 is not None]
    assert po2 and 0.4 <= min(po2) and max(po2) <= 2.0


@pytest.mark.skipif(not os.path.isfile(SW_PERDIX_UDDF), reason="the owner's test_data/misc_test_files are not present")
def test_owner_shearwater_perdix_uddf_agrees_with_the_database_read():
    """The app's UDDF of dive 433 against DiveSync's read of the same dive
    from the app's database: the same two tanks and, sample for sample, the
    same pressures (the database samples are one 5 s interval later, the
    UDDF rounds to 0.1 bar)."""
    from src.core.services.shearwater import ShearwaterAdapter
    d = read_uddf(SW_PERDIX_UDDF)[1][0]
    assert [(g.tank_name, g.start_pressure, g.end_pressure) for g in d.gas_mixtures] == [("T1", 186.43, 89.22), ("T4", None, 49.37)]
    po2 = [s.channels.ppo2 for s in d.samples if s.channels and s.channels.ppo2 is not None]
    assert po2 and 0.15 <= min(po2) and max(po2) <= 0.6
    db = next(x for x in ShearwaterAdapter(SW_DB).fetch_dives() if x.dive_number == 433)
    assert [(g.tank_name, g.start_pressure, g.end_pressure) for g in db.gas_mixtures] == \
        [(g.tank_name, g.start_pressure, g.end_pressure) for g in d.gas_mixtures]
    by_time = {s.time: (s.channels.pressures if s.channels else {}) for s in db.samples}
    compared = 0
    for s in d.samples:
        theirs = by_time.get(s.time + 5)
        if theirs is None:
            continue
        ours = s.channels.pressures if s.channels else {}
        assert ours.keys() == theirs.keys(), s.time
        assert all(abs(ours[k] - theirs[k]) <= 0.1 for k in ours), s.time
        compared += 1
    assert compared > 400


# --- check.md B14/B15: a gas list from the mixes, APD's cells ----------------
# Synthetic, written for these tests: a rebreather dive in a file without any
# tankdata, a switch on the loop, a bailout to open circuit and back.
MIXES_ONLY_CCR = """<?xml version="1.0" encoding="UTF-8"?>
<uddf xmlns="http://www.streit.cc/uddf/3.2/" version="3.2.0">
  <generator><name>Hand made</name></generator>
  <gasdefinitions>
    <mix id="dil1"><name>Dil 21</name><o2>0.21</o2><he>0</he></mix>
    <mix id="dil2"><name>Dil 15/50</name><o2>0.15</o2><he>0.50</he></mix>
    <mix id="deco"><name>Nx 40</name><o2>0.40</o2></mix>
  </gasdefinitions>
  <profiledata><repetitiongroup><dive id="r1">
    <informationbeforedive><datetime>2026-04-01T08:00:00</datetime></informationbeforedive>
    <samples>
      <waypoint><depth>0</depth><divetime>0</divetime><divemode type="closedcircuit"/></waypoint>
      <waypoint><depth>40</depth><divetime>300</divetime><switchmix ref="dil2"/></waypoint>
      <waypoint><depth>20</depth><divetime>900</divetime><divemode type="opencircuit"/></waypoint>
      <waypoint><depth>15</depth><divetime>960</divetime><switchmix ref="deco"/></waypoint>
      <waypoint><depth>6</depth><divetime>1300</divetime><divemode type="closedcircuit"/><switchmix ref="dil1"/></waypoint>
      <waypoint><depth>0</depth><divetime>1500</divetime></waypoint>
    </samples>
    <informationafterdive><greatestdepth>40</greatestdepth><diveduration>1500</diveduration></informationafterdive>
  </dive></repetitiongroup></profiledata>
</uddf>
"""


def test_mixes_become_the_gas_list_when_the_file_has_no_tankdata(tmp_path):
    """B14: one tank per mix in file order, diluents on a rebreather dive,
    a bailout tank for a mix switched to on open circuit; the switches name
    the tank for the circuit they were made on."""
    path = tmp_path / "ccr.uddf"
    path.write_text(MIXES_ONLY_CCR, encoding="utf-8")
    d = read_uddf(str(path))[1][0]
    assert [(g.oxygen, g.helium, g.tank_role) for g in d.gas_mixtures] == [
        (21.0, 0.0, "diluent"), (15.0, 50.0, "diluent"), (40.0, 0.0, "diluent"), (40.0, 0.0, "bailout")]
    assert [(e.time, e.tank) for e in d.events if e.type == "gas_switch"] == [(300, 1), (960, 3), (1300, 0)]
    assert d.dive_mode == "ccr"
    # an open-circuit file: the mixes without a role, every switch on its mix's tank
    oc = MIXES_ONLY_CCR.replace('type="closedcircuit"', 'type="opencircuit"')
    path.write_text(oc, encoding="utf-8")
    d = read_uddf(str(path))[1][0]
    assert [g.tank_role for g in d.gas_mixtures] == [None, None, None]
    assert [e.tank for e in d.events if e.type == "gas_switch"] == [1, 2, 0]


def test_mixes_are_not_tanks_where_the_file_has_tankdata_or_is_our_own(tmp_path):
    """A dive without tankdata in a file whose other dives have some keeps
    no gases (the mixes are those tanks'), and so does DiveSync's own file."""
    path = tmp_path / "mixed.uddf"
    path.write_text(SAMPLE, encoding="utf-8")
    assert read_uddf(str(path))[1][1].gas_mixtures == []
    own = MIXES_ONLY_CCR.replace("<name>Hand made</name>", "<name>dive_sync</name>")
    path.write_text(own, encoding="utf-8")
    assert read_uddf(str(path))[1][0].gas_mixtures == []


def test_apd_ppo2_refs_are_the_cells_of_the_first_controller(tmp_path):
    """B15: APD writes each cell as ``ppo2`` with an o2sensor ref, the second
    controller's view of the same three cells as sensors 4-6; the cells are
    the first controller's (as Subsurface keeps them). Another file's ppo2
    refs are cells too, all of them."""
    text = """<uddf version="3.3.0"><generator><name>AP DiveSight</name></generator>
      <diver><owner><equipment><divecomputer id="INSPIRATION"/><rebreather id="rb">
        <o2sensor id="a1"/><o2sensor id="a2"/><o2sensor id="a3"/><o2sensor id="b1"/><o2sensor id="b2"/><o2sensor id="b3"/>
      </rebreather></equipment></owner></diver>
      <profiledata><repetitiongroup><dive id="d">
        <informationbeforedive><datetime>2026-01-01T10:00:00</datetime></informationbeforedive>
        <samples><waypoint><depth>10</depth><divetime>60</divetime><divemode type="closedcircuit"/><setpo2>130000</setpo2>
          <ppo2 ref="a1">128000</ppo2><ppo2 ref="a2">131000</ppo2><ppo2 ref="a3">130000</ppo2>
          <ppo2 ref="b1">127000</ppo2><ppo2 ref="b2">131000</ppo2><ppo2 ref="b3">130000</ppo2></waypoint></samples>
        <informationafterdive><greatestdepth>10</greatestdepth><diveduration>60</diveduration></informationafterdive>
      </dive></repetitiongroup></profiledata></uddf>"""
    path = tmp_path / "apd.uddf"
    path.write_text(text, encoding="utf-8")
    ch = read_uddf(str(path))[1][0].samples[0].channels
    assert ch.o2_cells == {0: 1.28, 1: 1.31, 2: 1.3} and ch.setpoint == 1.3
    path.write_text(text.replace("AP DiveSight", "Other").replace('id="INSPIRATION"', 'id="X"'), encoding="utf-8")
    assert read_uddf(str(path))[1][0].samples[0].channels.o2_cells == {0: 1.28, 1: 1.31, 2: 1.3, 3: 1.27, 4: 1.31, 5: 1.3}


SUBSURFACE_CCR = [os.path.join(MISC, f) for f in ("test-uddf-ccr-3.1.uddf", "test-uddf-ccr-3.2.uddf")]
APD_UDDF = os.path.join(MISC, "test-apd-inspiration.uddf")


@pytest.mark.skipif(not all(os.path.isfile(p) for p in SUBSURFACE_CCR), reason="the owner's test_data/misc_test_files are not present")
@pytest.mark.parametrize("path", SUBSURFACE_CCR)
def test_subsurface_ccr_fixture_gas_list_like_its_reference(path):
    """Subsurface's test-uddf-ccr-reference.xml lists the three mixes as
    diluents and an EAN50 bailout (plus an 18/45 bailout DiveSync does not
    make: that mix is never switched to on open circuit)."""
    d = read_uddf(path)[1][0]
    assert [(g.oxygen, g.helium, g.tank_role) for g in d.gas_mixtures] == [
        (21.0, 0.0, "diluent"), (18.0, 45.0, "diluent"), (50.0, 0.0, "diluent"), (50.0, 0.0, "bailout")]
    assert [(e.time, e.tank) for e in d.events if e.type == "gas_switch"] == [(60, 1), (120, 3)]


@pytest.mark.skipif(not os.path.isfile(APD_UDDF), reason="the owner's test_data/misc_test_files are not present")
def test_apd_fixture_cells_and_gases_like_its_reference():
    d = next(x for x in read_uddf(APD_UDDF)[1] if x.samples)
    assert [g.tank_role for g in d.gas_mixtures] == ["diluent"] * 6 + ["bailout"]
    assert d.samples[0].channels.o2_cells == {0: 0.35, 1: 0.35, 2: 0.35} and d.samples[0].channels.setpoint == 0.7
    assert max(i for s in d.samples if s.channels for i in s.channels.o2_cells) == 2
