"""UDDF 3.2 adapter (rework.md F2): read a hand-written export shaped like
Subsurface's, write a file from UnifiedDives, round-trip, and sync through
the engine."""
import os
from datetime import datetime

import pytest

from src.core.models import GasMixture, UnifiedDive, UnifiedSample
from src.core.services.uddf import UddfAdapter, UddfDocument, read_uddf

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
