"""Subsurface git-storage parser/writer/adapter (rework.md F4) against the
real anonymised fixture in tests/data/subsurface_cloud."""
import os
import shutil
from datetime import datetime

import pytest

from src.core.models import GasMixture, UnifiedDive, UnifiedSample
from src.core.services.subsurface import (
    Cylinder,
    Dive,
    DiveComputer,
    Sample,
    SubsurfaceAdapter,
    SubsurfaceRepo,
    dive_to_unified,
    fmt_milli,
    parse_dive_dir_name,
    parse_sample_line,
    quote,
    read_logical_lines,
    render_cylinder,
    render_sample,
    split_line,
)
from src.core.site_matcher import distance_m, find_site

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "subsurface_cloud")
pytestmark = pytest.mark.skipif(not os.path.isdir(FIXTURE), reason="subsurface fixture missing")


@pytest.fixture
def repo_copy(tmp_path):
    dst = str(tmp_path / "repo")
    shutil.copytree(FIXTURE, dst)
    return dst


# ---------------------------------------------------------------- scalars and strings

def test_number_and_string_formats():
    assert fmt_milli(30.0) == "30.0" and fmt_milli(1.2) == "1.2" and fmt_milli(11.094) == "11.094"
    assert fmt_milli(206.843) == "206.843" and fmt_milli(-0.001) == "-0.001" and fmt_milli(0) == "0.0"
    assert quote('a "b" \\ c') == '"a \\"b\\" \\\\ c"'
    assert quote("line1\nline2") == '"line1\n\tline2"'
    assert quote("x\x01y") == '"x?y"'
    stripped, strings = split_line('keyvalue "Deco model" "Buhlmann ZHL-16C 40/85"')
    assert strings == ["Deco model", "Buhlmann ZHL-16C 40/85"] and stripped == 'keyvalue "" ""'
    stripped, strings = split_line('notes "a \\"q\\" b\n\tsecond"')
    assert strings == ['a "q" b\nsecond']
    assert read_logical_lines('notes "a\n\tb"\nbuddy "c"\n') == ['notes "a\n\tb"', 'buddy "c"']
    assert parse_dive_dir_name("29-Sat-09=56=11", 2026, 8) == datetime(2026, 8, 29, 9, 56, 11)
    assert parse_dive_dir_name("2025-12-31-Wed-23=59=59~abc1234", 2026, 1) == datetime(2025, 12, 31, 23, 59, 59)
    assert parse_dive_dir_name("29-Sat-09:56:11", 2026, 8) == datetime(2026, 8, 29, 9, 56, 11)  # old repos
    assert parse_dive_dir_name("29-trip-name", 2026, 8) is None


def test_sample_and_cylinder_round_trip():
    s = parse_sample_line("  0:15 1.753m 209.66bar:1 tts=0:00")
    assert (s.time_s, s.depth_m, s.pressures, s.raw_tail) == (15, 1.753, {1: 209.66}, "tts=0:00")
    s = parse_sample_line("  0:01 1.207m 31.0°C po2=0.7bar")
    assert (s.temp_c, s.raw_tail) == (31.0, "po2=0.7bar")
    assert render_sample(Sample(time_s=75, depth_m=12.5, temp_c=28.0, pressures={0: 200.0, 1: 190.5}, raw_tail="ndl=9:00")) == \
        "  1:15 12.5m 28.0°C 200.0bar:0 190.5bar:1 ndl=9:00"
    cyl = Cylinder(volume_l=11.094, workpressure_bar=206.843, description="AL80", o2=32.0, start_bar=200.0, end_bar=50.0, use="not used")
    assert render_cylinder(cyl) == 'cylinder vol=11.094l workpressure=206.843bar description="AL80" o2=32.0% start=200.0bar end=50.0bar use="not used"'
    assert render_cylinder(Cylinder(o2=None, he=None)) == "cylinder"  # air, nothing known


# ---------------------------------------------------------------- reading the fixture

def test_fixture_parses_to_unified():
    repo = SubsurfaceRepo(FIXTURE).load()
    assert len(repo.dives) == 8 and len(repo.sites) == 8
    two = repo.find("2026/08/29-Sat-12=52=59/Dive-13")
    assert two is not None and two.number == 13 and two.duration_s == 68 * 60 + 52
    assert [c.use for c in two.cylinders] == ["", "not used"]
    u = dive_to_unified(two, repo.sites)
    assert u.date_time == datetime(2026, 8, 29, 12, 52, 59) and u.duration == 4132
    assert u.max_depth == 13.82 and u.avg_depth == 8.545 and u.temp_min == 30.0
    assert u.external_ids == {"subsurface": "2026/08/29-Sat-12=52=59/Dive-13"}
    assert (u.lat, u.lng) == (7.609369, 98.378959) and u.location == "7.609369, 98.378959"
    assert len(u.gas_mixtures) == 2 and u.gas_mixtures[0].tank_volume == 11.094
    # pressures derived from the per-sensor sample data
    assert u.gas_mixtures[0].start_pressure > u.gas_mixtures[0].end_pressure > 0
    assert u.gas_mixtures[1].start_pressure > u.gas_mixtures[1].end_pressure > 0
    assert len(u.samples) > 3000 and u.samples[0].time == 1
    assert u.service_fields["tags"] == [] and u.service_fields["rating"] is None


# ---------------------------------------------------------------- writing

def test_add_update_delete_round_trip(repo_copy):
    adapter = SubsurfaceAdapter(repo_copy)
    assert adapter.login()
    before = len(adapter.fetch_dives())

    new = UnifiedDive(
        date_time=datetime(2026, 9, 1, 10, 30, 0), duration=2500, max_depth=21.5, avg_depth=12.0, temp_min=24.0,
        external_ids={"garmin": "555", "divelogs": "777"}, dive_number=14, buddy="Anna", notes='Line 1\nLine "2"',
        location="Zenobia", lat=34.887, lng=33.657, weight=6.0, weight_unit="kilogram",
        gas_mixtures=[GasMixture(oxygen=32.0, start_pressure=210.0, end_pressure=60.0, tank_volume=12.0, tank_name="D12"),
                      GasMixture(oxygen=50.0, start_pressure=200.0, end_pressure=150.0, tank_volume=7.0)],
        samples=[UnifiedSample(depth=0.0, temp=25.0, time=0), UnifiedSample(depth=21.5, temp=24.0, time=600), UnifiedSample(depth=0.2, time=2500)],
        service_fields={"suit": "5 mm", "rating": 4, "tags": ["wreck", "night"]},
    )
    new_id = adapter.add_dive(new)
    assert new_id == "2026/09/01-Tue-10=30=00/Dive-14"
    dive_dir = os.path.join(repo_copy, "2026", "09", "01-Tue-10=30=00")
    text = open(os.path.join(dive_dir, "Dive-14")).read()
    assert "duration 41:40 min" in text and 'buddy "Anna"' in text and 'notes "Line 1\n\tLine \\"2\\""' in text
    assert 'tags "wreck", "night"' in text and "rating 4" in text and 'suit "5 mm"' in text
    assert 'cylinder vol=12.0l description="D12" o2=32.0% start=210.0bar end=60.0bar' in text
    assert "cylinder vol=7.0l o2=50.0% start=200.0bar end=150.0bar" in text
    assert "weightsystem weight=6.0kg" in text and "watertemp 24.0°C" in text
    dc = open(os.path.join(dive_dir, "Divecomputer")).read()
    assert dc.startswith('model "dive_sync"\n') and "maxdepth 21.5m" in dc
    assert 'keyvalue "dive_sync:garmin" "555"' in dc and 'keyvalue "dive_sync:divelogs" "777"' in dc
    assert " 10:00 21.5m 24.0°C" in dc and " 41:40 0.2m" in dc
    # a new site was created and linked
    site_files = os.listdir(os.path.join(repo_copy, "01-Divesites"))
    assert len(site_files) == 9
    assert any("divesiteid" in line for line in text.splitlines())

    # read back
    dives = {d.external_ids["subsurface"]: d for d in adapter.fetch_dives()}
    assert len(dives) == before + 1
    back = dives[new_id]
    assert back.external_ids == {"subsurface": new_id, "garmin": "555", "divelogs": "777"}
    assert back.notes == 'Line 1\nLine "2"' and back.buddy == "Anna" and back.location == "Zenobia"
    assert (back.lat, back.lng) == (34.887, 33.657) and back.weight == 6.0
    assert [g.oxygen for g in back.gas_mixtures] == [32.0, 50.0] and back.gas_mixtures[0].tank_name == "D12"
    assert len(back.samples) == 3 and back.samples[1].depth == 21.5
    assert back.service_fields == {"suit": "5 mm", "divemaster": None, "rating": 4, "visibility_stars": None, "tags": ["wreck", "night"]}

    # same second again -> unique suffix, no clobbering
    second = adapter.add_dive(new.model_copy(update={"dive_number": 15}))
    assert second.startswith("2026/09/01-Tue-10=30=00~") and second.endswith("/Dive-15")

    # update an existing computer-downloaded dive: only owned fields change, samples untouched
    existing_id = "2026/08/29-Sat-12=52=59/Dive-13"
    existing = dives[existing_id]
    dc_before = open(os.path.join(repo_copy, "2026/08/29-Sat-12=52=59/Divecomputer")).read()
    existing.buddy = "Bo"
    existing.notes = "from garmin"
    existing.external_ids["garmin"] = "24438065346"
    assert adapter.update_dive(existing_id, existing)
    text = open(os.path.join(repo_copy, "2026/08/29-Sat-12=52=59/Dive-13")).read()
    assert 'buddy "Bo"' in text and 'notes "from garmin"' in text and 'divemaster ""' in text
    assert text.count("cylinder ") == 2 and 'use="not used"' in text  # cylinders preserved
    dc_after = open(os.path.join(repo_copy, "2026/08/29-Sat-12=52=59/Divecomputer")).read()
    assert 'keyvalue "dive_sync:garmin" "24438065346"' in dc_after
    assert dc_after.replace('keyvalue "dive_sync:garmin" "24438065346"\n', "") == dc_before
    again = {d.external_ids["subsurface"]: d for d in adapter.fetch_dives()}[existing_id]
    assert again.external_ids["garmin"] == "24438065346" and again.buddy == "Bo" and len(again.samples) > 3000

    # delete
    assert adapter.delete_dive(new_id) and not os.path.exists(dive_dir)
    assert not adapter.delete_dive(new_id)
    assert len(adapter.fetch_dives()) == before + 1  # the ~suffixed copy remains


def test_dives_inside_trip_directories(repo_copy):
    trip = os.path.join(repo_copy, "2026", "09", "05-Red Sea trip")
    os.makedirs(os.path.join(trip, "05-Sat-08=00=00"))
    open(os.path.join(trip, "00-Trip"), "w").write('date 2026-09-05\ntime 08:00:00\nlocation "Red Sea"\nnotes ""\n')
    open(os.path.join(trip, "05-Sat-08=00=00", "Dive-20"), "w").write('duration 50:00 min\nbuddy "T"\nnotes ""\n')
    open(os.path.join(trip, "05-Sat-08=00=00", "Divecomputer"), "w").write('model "X"\nmaxdepth 18.0m\n  0:00 0.0m\n 25:00 18.0m\n 50:00 0.0m\n')
    dives = SubsurfaceAdapter(repo_copy).fetch_dives()
    d = next(x for x in dives if x.dive_number == 20)
    assert d.date_time == datetime(2026, 9, 5, 8, 0, 0) and d.max_depth == 18.0 and d.duration == 3000
    assert d.external_ids["subsurface"] == "2026/09/05-Red Sea trip/05-Sat-08=00=00/Dive-20"


# ---------------------------------------------------------------- site matcher

def test_site_matcher():
    sites = [("Zenobia", (34.887, 33.657)), ("Reef", (34.900, 33.700)), ("Unknown", (None, None))]
    get_name, get_gps = (lambda s: s[0]), (lambda s: s[1])
    assert find_site(sites, "zenobia ", None, None, get_name, get_gps)[0] == "Zenobia"
    assert find_site(sites, "Other", 34.8871, 33.6571, get_name, get_gps)[0] == "Zenobia"   # ~15 m away
    assert find_site(sites, "Other", 34.95, 33.7, get_name, get_gps) is None                 # too far
    assert find_site(sites, None, None, None, get_name, get_gps) is None
    assert 14 < distance_m(34.887, 33.657, 34.8871, 33.6571) < 16


# ---------------------------------------------------------------- through the engine

def test_engine_pair_with_subsurface_target(repo_copy, tmp_path):
    from src.core.config import ConfigManager, SettingsModel, SyncFilters
    from src.core.fields import FieldLink, common_default_links
    from src.core.sync_engine import SyncEngine
    from tests.test_link_engine import FakeGarmin, _dive

    links = common_default_links("garmin", "subsurface", match_on_dive_number=False)
    settings = SettingsModel(sync_filters=SyncFilters(only_new=False), field_links=links)
    path = str(tmp_path / "settings.json")
    ConfigManager.save_settings(settings, path)
    g = [_dive(date_time=datetime(2026, 8, 29, 12, 52, 59), external_ids={"garmin": "24438065346"}, buddy="Anna"),
         _dive(date_time=datetime(2026, 9, 2, 9, 0, 0), external_ids={"garmin": "9"}, notes="new one", location="Reef",
               lat=34.900, lng=33.700)]
    engine = SyncEngine(settings_path=path, credentials_path=str(tmp_path / "c.json"),
                        source_adapter=FakeGarmin(g), target_adapter=SubsurfaceAdapter(repo_copy))
    res = engine.run_sync(dry_run=False)
    assert res["matched_count"] == 1 and len(res["uploaded_to_subsurface"]) == 1
    # the matched dive got the buddy filled and the Garmin id written into the repo (stores_external_ids)
    assert len(res["updated_on_subsurface"]) == 1
    dc = open(os.path.join(repo_copy, "2026/08/29-Sat-12=52=59/Divecomputer")).read()
    assert 'keyvalue "dive_sync:garmin" "24438065346"' in dc
    assert 'buddy "Anna"' in open(os.path.join(repo_copy, "2026/08/29-Sat-12=52=59/Dive-13")).read()
    # second run: tier 1 by stored id, nothing to do
    engine2 = SyncEngine(settings_path=path, credentials_path=str(tmp_path / "c.json"),
                         source_adapter=FakeGarmin(g), target_adapter=SubsurfaceAdapter(repo_copy))
    res = engine2.run_sync(dry_run=False)
    assert res["matched_count"] == 2 and res["uploaded_to_subsurface"] == [] and res["updated_on_subsurface"] == []


# ---------------------------------------------------------------- E5: tank role mapping

def test_cylinderuse_maps_to_and_from_tank_role(repo_copy):
    """Subsurface's cylinderuse is a narrower axis (diluent/oxygen/bailout/
    not-used) than a general multi-tank role; unmappable roles (backGas,
    stage, sidemountLeft, ...) are honestly dropped, not invented."""
    from src.core.services.subsurface import USE_TO_TANK_ROLE, Cylinder, apply_unified
    from src.core.models import GasMixture

    assert USE_TO_TANK_ROLE == {"diluent": "diluent", "oxygen": "oxygen", "bailout": "bailout", "not used": "not_used"}

    repo = SubsurfaceRepo(repo_copy).load()
    two = repo.find("2026/08/29-Sat-12=52=59/Dive-13")
    two.cylinders[1].use = "not used"
    u = dive_to_unified(two, repo.sites)
    assert u.gas_mixtures[0].tank_role is None and u.gas_mixtures[1].tank_role == "not_used"

    dive = Dive(dir_path="x", when=datetime(2026, 1, 1))
    unified = UnifiedDive(date_time=datetime(2026, 1, 1), duration=100, max_depth=10.0, gas_mixtures=[
        GasMixture(oxygen=32.0, tank_volume=11.0, tank_role="diluent"),
        GasMixture(oxygen=100.0, tank_volume=3.0, tank_role="oxygen"),
        GasMixture(oxygen=21.0, tank_volume=11.0, tank_role="backGas"),   # no Subsurface equivalent
    ])
    apply_unified(dive, unified, SubsurfaceRepo(repo_copy), write_samples=True)
    assert [c.use for c in dive.cylinders] == ["diluent", "oxygen", ""]

    # an existing cylinder's use is kept when the incoming role can't be mapped
    dive2 = Dive(dir_path="y", when=datetime(2026, 1, 1))
    dive2.cylinders = [Cylinder(use="not used")]
    apply_unified(dive2, UnifiedDive(date_time=datetime(2026, 1, 1), duration=100, max_depth=10.0,
                                    gas_mixtures=[GasMixture(oxygen=21.0, tank_role="stage")]),
                 SubsurfaceRepo(repo_copy), write_samples=True)
    assert dive2.cylinders[0].use == "not used"


def test_render_cylinder_writes_bailout_and_not_used():
    assert 'use="bailout"' in render_cylinder(Cylinder(o2=21.0, use="bailout"))
    assert 'use="not used"' in render_cylinder(Cylinder(use="not used"))
