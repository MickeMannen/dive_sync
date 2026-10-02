"""UDDF 3.2 adapter (rework.md F2): read a hand-written export shaped like
Subsurface's, write a file from UnifiedDives, round-trip, and sync through
the engine. The carried fields of plans/convert.md I5 (per-sample channels,
events, the computer, gradient factors, weight, surface interval) are
written and read back, and a dive without them gives byte for byte the
file the writer produced before I5 (``tests/data/uddf/pre_i5_*.uddf``,
captured from the writer at 5bdfaa8)."""
import os
from datetime import datetime

import pytest

from src.core.models import DiveEvent, GasMixture, SampleChannels, UnifiedDive, UnifiedSample
from src.core.services.subsurface import SubsurfaceAdapter
from src.core.services.uddf import (
    UddfAdapter,
    UddfDocument,
    _deco_model_of,
    events_by_sample,
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
      <waypoint><calculatedpo2>23000</calculatedpo2><depth>1</depth><divetime>0</divetime><switchmix ref="OC1:21/00" />
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


class FakeSubsurface(RecordingAdapter):
    service_id = "subsurface"
    display_name = "Subsurface"
    _catalog = SubsurfaceAdapter.field_catalog()


def test_sync_from_subsurface_writes_the_pressure_profile(tmp_path):
    """A sample with a pressure and no channels (what the Shearwater,
    Subsurface and Submersion adapters produce) is written as the first
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
    pressure is written and read back unchanged."""
    from src.core.services.subsurface import SubsurfaceRepo, dive_to_unified
    repo = SubsurfaceRepo(SUBSURFACE_FIXTURE).load()
    dive = dive_to_unified(repo.find("2026/08/29-Sat-12=52=59/Dive-13"), repo.sites)
    with_pressure = [s for s in dive.samples if s.pressure is not None]
    assert len(with_pressure) > 100 and all(s.channels is None for s in dive.samples)
    document = UddfDocument()
    dive_id = document.write_dive(dive)
    document.save(str(tmp_path / "fixture.uddf"))
    text = open(tmp_path / "fixture.uddf", encoding="utf-8").read()
    assert text.count("<tankpressure ") == len(with_pressure) and text.count(f'ref="{dive_id}-tank0"') == len(with_pressure)
    back = read_uddf(str(tmp_path / "fixture.uddf"))[1][0]
    assert [(s.time, s.pressure) for s in back.samples] == [(s.time, s.pressure) for s in dive.samples]
    assert all(s.channels is None for s in back.samples)
