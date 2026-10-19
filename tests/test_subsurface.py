"""Subsurface git-storage parser/writer/adapter (rework.md F4) against the
real anonymised fixture in tests/data/subsurface_cloud."""
import os
import shutil
from datetime import datetime

import pytest

from src.core.models import GasMixture, SampleChannels, UnifiedDive, UnifiedSample
from src.core.services.subsurface import (
    Cylinder,
    Dive,
    DiveComputer,
    Sample,
    SubsurfaceAdapter,
    SubsurfaceRepo,
    apply_unified,
    dive_to_unified,
    fmt_milli,
    parse_dive_dir_name,
    parse_divecomputer_file as parse_divecomputer,
    parse_sample_line,
    quote,
    read_logical_lines,
    render_cylinder,
    render_dc_lines,
    render_sample,
    split_line,
    unified_samples,
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


def test_sample_pressure_round_trip(repo_copy):
    """A per-sample tank pressure (a transmitter's, rework.md H5) is written
    on sensor 0 and read back; a computer with several sensors gives the
    lowest-numbered one, the same on every sample."""
    dive = Dive(dir_path="x", when=datetime(2026, 1, 1))
    unified = UnifiedDive(date_time=datetime(2026, 1, 1), duration=20, max_depth=10.0,
                          samples=[UnifiedSample(depth=5.0, temp=20.0, time=10, pressure=200.0), UnifiedSample(depth=10.0, time=20)])
    apply_unified(dive, unified, SubsurfaceRepo(repo_copy), write_samples=True)
    assert [s.pressures for s in dive.computers[0].samples] == [{0: 200.0}, {}]
    assert render_dc_lines(dive.computers[0])[-2:] == ["  0:10 5.0m 20.0°C 200.0bar:0", "  0:20 10.0m"]
    assert [s.pressure for s in dive_to_unified(dive, {}).samples] == [200.0, None]
    dive.computers[0].samples = [Sample(time_s=1, depth_m=1.0, pressures={1: 150.0}),
                                 Sample(time_s=2, depth_m=2.0, pressures={0: 210.0, 1: 149.0})]
    assert [s.pressure for s in dive_to_unified(dive, {}).samples] == [None, 210.0]
    # the fixture's computer download keeps its pressures in the samples
    repo = SubsurfaceRepo(FIXTURE).load()
    u = dive_to_unified(repo.find("2026/08/29-Sat-12=52=59/Dive-13"), repo.sites)
    with_pressure = [s.pressure for s in u.samples if s.pressure is not None]
    assert with_pressure and with_pressure[0] == u.gas_mixtures[0].start_pressure


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
        service_fields={"rating": 4, "tags": ["wreck", "night"]}, suit="5 mm", air_temp=31.0, water_type="fresh",
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
    assert "airtemp 31.0°C" in text and "watersalinity 1000 g/l" in text                 # R10, in Subsurface's own spelling
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
    assert back.service_fields == {"divemaster": None, "rating": 4, "visibility_stars": None, "tags": ["wreck", "night"]}
    assert (back.suit, back.air_temp, back.water_type, back.water_density) == ("5 mm", 31.0, "fresh", None)

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
    settings = SettingsModel(directionality="to_subsurface", sync_filters=SyncFilters(only_new=False), field_links=links)
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


# ---------------------------------------------------------------- hand-logged dives: duration and depths

def _hand_logged_dive(repo, rel, dc_lines, dive_lines=("duration 41:00 min",)):
    path = os.path.join(repo, rel)
    os.makedirs(path)
    with open(os.path.join(path, "Dive-90"), "w") as f:
        f.write("\n".join(dive_lines) + "\n")
    with open(os.path.join(path, "Divecomputer"), "w") as f:
        f.write("\n".join(dc_lines) + "\n")
    return rel + "/Dive-90"


def _lines(repo, rel):
    with open(os.path.join(repo, os.path.dirname(rel), "Divecomputer")) as f:
        return [line.rstrip("\n") for line in f]


def test_hand_logged_dive_takes_new_duration_and_depths(repo_copy):
    """A dive dive_sync uploaded from a hand-logged Garmin dive (no profile,
    just duration and max depth on dive_sync's computer - the shape of the
    owner's real ones) takes edited duration and depths."""
    dive_id = _hand_logged_dive(repo_copy, "1993/10/10-Sun-11=30=00",
                                ['model "dive_sync"', "duration 41:00 min", "maxdepth 13.0m",
                                 'keyvalue "dive_sync:garmin" "24449947904"'])
    a = SubsurfaceAdapter(repo_copy)
    a.login()
    dive = next(d for d in a.fetch_dives() if d.external_ids["subsurface"] == dive_id)
    assert (dive.duration, dive.max_depth) == (41 * 60, 13.0)
    dive.duration, dive.max_depth, dive.avg_depth = 45 * 60, 14.5, 9.2
    assert a.update_dive(dive_id, dive)
    lines = _lines(repo_copy, dive_id)
    assert "duration 45:00 min" in lines and "maxdepth 14.5m" in lines and "meandepth 9.2m" in lines
    assert not any(line[:1] == " " or line[:1].isdigit() for line in lines)   # still no profile: Subsurface draws one
    assert 'keyvalue "dive_sync:garmin" "24449947904"' in lines
    again = next(d for d in SubsurfaceAdapter(repo_copy).fetch_dives() if d.external_ids["subsurface"] == dive_id) \
        if SubsurfaceAdapter(repo_copy).login() else None
    assert (again.duration, again.max_depth, again.avg_depth) == (45 * 60, 14.5, 9.2)


def test_subsurface_manual_dive_gets_its_drawn_profile_redrawn(repo_copy):
    dive_id = _hand_logged_dive(repo_copy, "1996/07/13-Sat-14=00=00",
                                ['model "manually added dive"', "duration 38:00 min", "maxdepth 15.0m",
                                 "salinity 1030g/l", "0:00 0.0m", "2:00 15.0m", "36:00 15.0m", "38:00 0.0m"])
    a = SubsurfaceAdapter(repo_copy)
    a.login()
    dive = next(d for d in a.fetch_dives() if d.external_ids["subsurface"] == dive_id)
    dive.duration, dive.max_depth, dive.avg_depth = 40 * 60, 12.0, 9.0
    assert a.update_dive(dive_id, dive)
    lines = _lines(repo_copy, dive_id)
    assert lines[:4] == ['model "manually added dive"', "duration 40:00 min", "maxdepth 12.0m", "meandepth 9.0m"]
    assert "salinity 1030g/l" in lines                               # other lines kept
    samples = [parse_sample_line(line) for line in lines if line[:1] == " " or line[:1].isdigit()]
    assert len(samples) == 4                                         # the old drawn profile is gone
    assert [s.time_s for s in samples] == [0, 600, 1800, 2400] and max(s.depth_m for s in samples) == 12.0


def test_recorded_profile_keeps_its_depths(repo_copy):
    a = SubsurfaceAdapter(repo_copy)
    a.login()
    dive = a.fetch_dives()[0]
    dive_id = dive.external_ids["subsurface"]
    before = [line for line in _lines(repo_copy, dive_id) if line.startswith(("maxdepth", "meandepth"))]
    dive.max_depth, dive.avg_depth = dive.max_depth + 5, 1.0
    assert a.update_dive(dive_id, dive)
    assert [line for line in _lines(repo_copy, dive_id) if line.startswith(("maxdepth", "meandepth"))] == before


def test_simple_profile_matches_duration_max_and_mean():
    from src.core.services.subsurface import simple_profile
    samples = simple_profile(2400, 12.0, 9.0)
    assert [(s.time_s, s.depth_m) for s in samples] == [(0, 0.0), (600, 12.0), (1800, 12.0), (2400, 0.0)]
    area = sum((b.time_s - a.time_s) * (a.depth_m + b.depth_m) / 2 for a, b in zip(samples, samples[1:]))
    assert abs(area / 2400 - 9.0) < 0.01                             # its mean depth is the one given
    assert simple_profile(0, 12.0) == [] and len(simple_profile(60, 30.0)) == 3   # slope clamps to half the dive


def test_zero_water_temperature_counts_as_not_recorded(repo_copy):
    """Garmin stores 0 °C on a hand-logged dive whose temperature was never
    entered, and earlier versions copied it into Subsurface: read as none,
    and cleared from the dive when dive_sync next writes it."""
    from src.core.models import recorded_water_temp
    assert recorded_water_temp(0) is None and recorded_water_temp("") is None and recorded_water_temp(28.5) == 28.5
    dive_id = _hand_logged_dive(repo_copy, "1993/10/10-Sun-11=30=00",
                                ['model "dive_sync"', "duration 41:00 min", "maxdepth 13.0m", "watertemp 0.0°C"],
                                dive_lines=("duration 41:00 min", "watertemp 0.0°C"))
    a = SubsurfaceAdapter(repo_copy)
    a.login()
    dive = next(d for d in a.fetch_dives() if d.external_ids["subsurface"] == dive_id)
    assert dive.temp_min is None
    dive.buddy = "Roger"
    assert a.update_dive(dive_id, dive)
    assert not any(line.startswith("watertemp") for line in _lines(repo_copy, dive_id))
    with open(os.path.join(repo_copy, dive_id)) as f:
        assert "watertemp" not in f.read()


def test_garmin_zero_temperature_is_not_a_reading():
    from src.core.services.garmin import GarminAdapter
    garmin = GarminAdapter.__new__(GarminAdapter)
    summary = {"activityId": 1, "startTimeLocal": "1993-10-10T11:30:00.0", "duration": 2460, "maxDepth": 13.0}
    hand_logged = garmin._map_to_unified(summary, {"summaryDTO": {"minTemperature": 0.0}})
    assert hand_logged.temp_min is None
    real = garmin._map_to_unified(summary, {"summaryDTO": {"minTemperature": 5.0}})
    assert real.temp_min == 5.0


# ---------------------------------------------------------------- renumbered logs (2026-09-25)

def _engine_to(repo, tmp_path, garmin_dives, **settings):
    from src.core.config import ConfigManager, SettingsModel, SyncFilters
    from src.core.sync_engine import SyncEngine
    from tests.test_link_engine import FakeGarmin
    path = str(tmp_path / "settings.json")
    ConfigManager.save_settings(SettingsModel(directionality="to_subsurface", sync_filters=SyncFilters(only_new=False),
                                              api_cooldown_seconds=0.0, **settings), path)
    return SyncEngine(settings_path=path, credentials_path=str(tmp_path / "c.json"),
                      source_adapter=FakeGarmin(garmin_dives), target_adapter=SubsurfaceAdapter(repo))


def test_same_start_time_beats_a_stale_dive_number(repo_copy, tmp_path):
    """The owner's case: Garmin renumbered its log (06-13 is now #4, 06-14 is
    #5) and replaced the activities, so no link holds. Subsurface still has
    06-14 as "#4". The 06-14 dive must pair with Garmin #5 by its start
    time, not with Garmin #4 by the number - that pairing uploaded the real
    dive again as a duplicate."""
    from tests.test_link_engine import _dive
    ss_0614 = _hand_logged_dive(repo_copy, "1992/06/14-Sun-10=30=00", ['model "dive_sync"', "duration 40:00 min", "maxdepth 9.0m"])
    ss_0613 = _hand_logged_dive(repo_copy, "1992/06/13-Sat-23=50=00", ['model "dive_sync"', "duration 40:00 min", "maxdepth 9.0m"])
    os.rename(os.path.join(repo_copy, ss_0614), os.path.join(repo_copy, os.path.dirname(ss_0614), "Dive-4"))
    os.rename(os.path.join(repo_copy, ss_0613), os.path.join(repo_copy, os.path.dirname(ss_0613), "Dive-3"))
    garmin = [_dive(date_time=datetime(1992, 6, 13, 23, 50), external_ids={"garmin": "new-4"}, dive_number=4),
              _dive(date_time=datetime(1992, 6, 14, 10, 30), external_ids={"garmin": "new-5"}, dive_number=5)]
    engine = _engine_to(repo_copy, tmp_path, garmin)
    # the dive list order that made the old matcher take the number first
    target = sorted(engine.target.fetch_dives(), key=lambda d: d.date_time, reverse=True)
    pairs, only_garmin, _ = engine.match_dives(garmin, target, {})
    by_garmin = {a.external_ids["garmin"]: b.date_time for a, b in pairs}
    assert by_garmin == {"new-4": datetime(1992, 6, 13, 23, 50), "new-5": datetime(1992, 6, 14, 10, 30)}
    assert only_garmin == []

    # and a real run renumbers both in Subsurface, uploading nothing
    res = engine.run_sync(dry_run=False)
    assert res["uploaded_to_subsurface"] == []
    assert os.path.exists(os.path.join(repo_copy, "1992/06/13-Sat-23=50=00/Dive-4"))
    assert os.path.exists(os.path.join(repo_copy, "1992/06/14-Sun-10=30=00/Dive-5"))
    assert not os.path.exists(os.path.join(repo_copy, "1992/06/14-Sun-10=30=00/Dive-4"))


def test_links_survive_a_renumbered_subsurface_dive(repo_copy, tmp_path):
    """A Subsurface id carries its Dive-N file name; renumbering changes it.
    Links compare on the directory, so the next run still recognises the
    pair - and a propagate-deletes run never reads the dive as gone."""
    from tests.test_link_engine import _dive
    garmin = [_dive(date_time=datetime(2025, 2, 8, 13, 51, 24), external_ids={"garmin": "g23"}, dive_number=23)]
    engine = _engine_to(repo_copy, tmp_path, garmin, propagate_deletes=True)
    engine.run_sync(dry_run=False)                                    # renumbers Dive-2 -> Dive-23
    assert os.path.exists(os.path.join(repo_copy, "2025/02/08-Sat-13=51=24/Dive-23"))
    assert engine.load_links() == {"g23": "2025/02/08-Sat-13=51=24"}

    garmin[0].dive_number = 24                                        # renumbered again on Garmin
    again = _engine_to(repo_copy, tmp_path, garmin, propagate_deletes=True)
    res = again.run_sync(dry_run=False)
    assert res["uploaded_to_subsurface"] == [] and res["deleted_on_subsurface"] == [] and res["deleted_on_garmin"] == []
    assert os.path.exists(os.path.join(repo_copy, "2025/02/08-Sat-13=51=24/Dive-24"))
    # a Garmin dive without a number leaves the Subsurface number alone (no update every run)
    garmin[0].dive_number = None
    res = _engine_to(repo_copy, tmp_path, garmin).run_sync(dry_run=False)
    assert res["updated_on_subsurface"] == [] and os.path.exists(os.path.join(repo_copy, "2025/02/08-Sat-13=51=24/Dive-24"))


def test_conflicts_survive_a_renumbered_subsurface_dive(repo_copy, tmp_path):
    """The owner's case: a run pushes Garmin's new dive number to Subsurface
    (Dive-2 becomes Dive-23) and in the same run records a manual conflict
    on another field, holding the id the dive had when it was read. Saving
    the pick must still find the dive by its directory, and a later run
    must replace the entry for the pair instead of stacking a second one."""
    from src.core.fields import FieldLink
    from tests.test_link_engine import _dive
    folder = "2025/02/08-Sat-13=51=24"
    with open(os.path.join(repo_copy, folder, "Dive-2"), "w") as f:
        f.write('duration 52:41 min\nbuddy "Bob"\n')
    links = [FieldLink(id="number", source=["garmin.dive_number"], target="subsurface.dive_number"),
             FieldLink(id="buddy", source=["garmin.buddy"], target="subsurface.buddy", conflict="manual")]
    garmin = [_dive(date_time=datetime(2025, 2, 8, 13, 51, 24), external_ids={"garmin": "g23"}, dive_number=23, buddy="Anna")]
    engine = _engine_to(repo_copy, tmp_path, garmin, field_links=links)
    engine.run_sync(dry_run=False)
    assert os.path.exists(os.path.join(repo_copy, folder, "Dive-23"))
    conflicts = engine.list_conflicts()
    assert [c.link_id for c in conflicts] == ["buddy"]
    assert conflicts[0].target_external_id == folder + "/Dive-2"           # the id at read time

    # the next run (Garmin renumbered again) replaces the pair's entry, it does not stack
    garmin[0].dive_number = 24
    again = _engine_to(repo_copy, tmp_path, garmin, field_links=links)
    again.run_sync(dry_run=False)
    assert os.path.exists(os.path.join(repo_copy, folder, "Dive-24"))
    conflicts = again.list_conflicts()
    assert [c.link_id for c in conflicts] == ["buddy"]

    # saving the pick finds the dive under its new file name
    saver = _engine_to(repo_copy, tmp_path, garmin, field_links=links)
    saver.resolve_conflict(conflicts[0].id, "source")
    assert saver.list_conflicts() == []
    with open(os.path.join(repo_copy, folder, "Dive-24")) as f:
        assert 'buddy "Anna"' in f.read()


# ---------------------------------------------------------------- R10: air temperature, suit, salinity

def test_air_temperature_suit_and_salinity_read_and_write(repo_copy):
    """R10: the dive's own ``airtemp`` and ``watersalinity`` win over the
    computer's ``airtemp`` / ``salinity``; the salinity becomes a water type
    (Subsurface's three values and Garmin's 1025) or, for another density,
    the density alone; the suit is the dive's own field; and the write
    puts a dive-level value only where the computer's differs, as
    Subsurface does with the water temperature."""
    from src.core.services.subsurface import parse_dive_file, parse_divecomputer_file, render_dive_lines
    dive = Dive(dir_path="x", when=datetime(2026, 1, 1))
    parse_dive_file(dive, 'duration 10:00 min\nwatersalinity 1030 g/l\nsuit "drysuit"\nairtemp 12.5°C\nwatertemp 8.0°C\n')
    dc = parse_divecomputer_file("Divecomputer", 'model "Perdix 2"\nairtemp 11.0°C\nsalinity 1020 g/l\n  0:00 0.0m\n')
    dive.computers = [dc]
    assert (dive.airtemp_c, dive.watersalinity_gl, dive.suit, dc.airtemp_c, dc.salinity_gl) == (12.5, 1030.0, "drysuit", 11.0, 1020.0)
    u = dive_to_unified(dive, {})
    assert (u.air_temp, u.suit, u.water_type, u.water_density) == (12.5, "drysuit", "salt", None)
    assert "suit" not in u.service_fields
    # the computer's values when the dive has none of its own
    bare = Dive(dir_path="y", when=datetime(2026, 1, 1)); bare.computers = [dc]
    u = dive_to_unified(bare, {})
    assert (u.air_temp, u.suit, u.water_type) == (11.0, None, "en13319")
    # Garmin's seawater and a density no type has
    other = Dive(dir_path="z", when=datetime(2026, 1, 1))
    parse_dive_file(other, "watersalinity 1025 g/l\n")
    assert (dive_to_unified(other, {}).water_type, dive_to_unified(other, {}).water_density) == ("salt", None)
    parse_dive_file(other, "watersalinity 1012 g/l\n")
    assert (dive_to_unified(other, {}).water_type, dive_to_unified(other, {}).water_density) == (None, 1012.0)
    assert dive_to_unified(Dive(dir_path="w", when=datetime(2026, 1, 1)), {}).water_type is None

    # writing: dive-level only where the computer's value differs
    repo = SubsurfaceRepo(repo_copy)
    target = Dive(dir_path="t", when=datetime(2026, 1, 1)); target.computers = [dc]
    apply_unified(target, UnifiedDive(date_time=datetime(2026, 1, 1), duration=100, max_depth=10.0,
                                      air_temp=11.0, water_type="en13319", suit="5 mm"), repo, write_samples=False)
    assert target.airtemp_c is None and target.watersalinity_gl is None and target.suit == "5 mm"
    apply_unified(target, UnifiedDive(date_time=datetime(2026, 1, 1), duration=100, max_depth=10.0,
                                      air_temp=14.0, water_type="salt"), repo, write_samples=False)
    assert target.airtemp_c == 14.0 and target.watersalinity_gl == 1030.0 and target.suit == "5 mm"   # None leaves the suit alone
    lines = render_dive_lines(target)
    assert "airtemp 14.0°C" in lines and "watersalinity 1030 g/l" in lines and 'suit "5 mm"' in lines
    assert lines.index("watersalinity 1030 g/l") < lines.index('suit "5 mm"') < lines.index("airtemp 14.0°C")   # Subsurface's order
    # a computer's own density is written as its own line; a 'custom' water type without one is nothing
    apply_unified(target, UnifiedDive(date_time=datetime(2026, 1, 1), duration=100, max_depth=10.0,
                                      water_type="custom", water_density=1012.0), repo, write_samples=False)
    assert target.watersalinity_gl == 1012.0
    apply_unified(target, UnifiedDive(date_time=datetime(2026, 1, 1), duration=100, max_depth=10.0, water_type="custom"), repo, write_samples=False)
    assert target.watersalinity_gl == 1012.0                       # nothing to write: left as it was
    assert "salinity 1020 g/l" in render_dc_lines(DiveComputer(model="dive_sync", salinity_gl=1020.0))
    # a dive cached before R10 still carries the suit as a service field
    old = UnifiedDive(date_time=datetime(2026, 1, 1), duration=100, max_depth=10.0, service_fields={"suit": "3 mm"})
    apply_unified(target, old, repo, write_samples=False)
    assert target.suit == "3 mm"
    # the catalogue names the three, so a rule can sync them
    specs = {f.name: f for f in SubsurfaceAdapter.field_catalog()}
    assert specs["suit"].unified == "suit" and specs["air_temp"].unit == "°C" and specs["water_type"].type == "text"


# ---------------------------------------------------------------- R8: the exit position

def test_exit_position_is_the_gps2_extradata(repo_copy, tmp_path):
    """plans/gap-roadmap.md R8: a drift dive's exit position is the ``GPS2``
    extradata of the dive computer, the key Subsurface's own Garmin import
    writes it under ("lat, lng", six decimals). Read from any computer that
    holds one, written on the computer that holds one (else the target
    computer) and refreshed in place on a computer read from disk; a dive
    without an exit position leaves what the log has."""
    from src.core.services.subsurface import (
        EXTRA_EXIT_GPS, exit_position_text, fmt_degrees, parse_divecomputer_file, parse_gps,
    )
    assert parse_gps("7.609369, 98.378959") == (7.609369, 98.378959) and parse_gps("7.609369 98.378959") == (7.609369, 98.378959)
    assert parse_gps("") == (None, None) and parse_gps("north") == (None, None)
    assert fmt_degrees(-10.0000564) == "-10.000056" and exit_position_text(7.61, 98.38) == "7.610000, 98.380000"
    assert exit_position_text(7.61, None) is None and EXTRA_EXIT_GPS == "GPS2"

    # reading: the Garmin import's GPS1/GPS2 pair on a real computer; GPS1 is not the site
    dc = parse_divecomputer_file("Divecomputer", 'model "Descent Mk3i"\nkeyvalue "GPS1" "7.609369, 98.378959"\n'
                                 'keyvalue "GPS2" "7.610000, 98.380000"\n  0:00 0.0m\n  0:10 5.0m\n')
    dive = Dive(dir_path="x", when=datetime(2026, 1, 1)); dive.computers = [dc]
    u = dive_to_unified(dive, {})
    assert (u.exit_lat, u.exit_lng) == (7.61, 98.38) and (u.lat, u.lng) == (None, None)
    assert dive_to_unified(Dive(dir_path="y", when=datetime(2026, 1, 1)), {}).exit_lat is None

    # writing on a computer read from disk: refreshed in place, nothing else moves
    repo = SubsurfaceRepo(repo_copy)
    apply_unified(dive, UnifiedDive(date_time=datetime(2026, 1, 1), duration=10, max_depth=5.0, exit_lat=7.62, exit_lng=98.39,
                                    external_ids={"garmin": "1"}), repo, write_samples=False)
    lines = render_dc_lines(dc)
    assert lines == ['model "Descent Mk3i"', 'keyvalue "GPS1" "7.609369, 98.378959"', 'keyvalue "GPS2" "7.620000, 98.390000"',
                     'keyvalue "dive_sync:garmin" "1"', "  0:00 0.0m", "  0:10 5.0m"]
    # no exit position in the sender: the log's own stays
    apply_unified(dive, UnifiedDive(date_time=datetime(2026, 1, 1), duration=10, max_depth=5.0, exit_lat=None), repo, write_samples=False)
    assert 'keyvalue "GPS2" "7.620000, 98.390000"' in render_dc_lines(dc)
    # a computer without one gets the line with the dive_sync ids, before its first sample
    bare = parse_divecomputer_file("Divecomputer", 'model "Perdix 2"\n  0:00 0.0m\n')
    other = Dive(dir_path="z", when=datetime(2026, 1, 1)); other.computers = [bare]
    apply_unified(other, UnifiedDive(date_time=datetime(2026, 1, 1), duration=10, max_depth=5.0, exit_lat=1.5, exit_lng=2.5), repo, write_samples=False)
    assert render_dc_lines(bare) == ['model "Perdix 2"', 'keyvalue "GPS2" "1.500000, 2.500000"', "  0:00 0.0m"]

    # our own computer: written with the new dive and read back by the adapter
    adapter = SubsurfaceAdapter(repo_copy)
    assert adapter.login()
    new_id = adapter.add_dive(UnifiedDive(date_time=datetime(2026, 9, 3, 10, 0), duration=1800, max_depth=12.0,
                                          lat=34.887, lng=33.657, exit_lat=34.890, exit_lng=33.660, location="Drift"))
    assert 'keyvalue "GPS2" "34.890000, 33.660000"' in open(os.path.join(repo_copy, new_id.rsplit("/", 1)[0], "Divecomputer")).read()
    back = next(d for d in adapter.fetch_dives() if d.external_ids["subsurface"] == new_id)
    assert (back.exit_lat, back.exit_lng) == (34.89, 33.66) and (back.lat, back.lng) == (34.887, 33.657)
    # the fixture's Garmin dives, imported by Subsurface itself, carry GPS1/GPS2: GPS2 is read as the exit, the site stays
    fixture = next(d for d in adapter.fetch_dives() if d.date_time == datetime(2025, 2, 8, 13, 51, 24))
    assert (fixture.exit_lat, fixture.exit_lng) == (7.606926, 98.378705)
    assert (fixture.lat, fixture.lng) != (fixture.exit_lat, fixture.exit_lng)
    specs = {f.name: f for f in SubsurfaceAdapter.field_catalog()}
    assert specs["exit_gps"].type == "gps" and specs["exit_gps"].unified == "exit_gps" and specs["exit_gps"].writable


def test_exit_position_syncs_only_through_a_drawn_rule(repo_copy, tmp_path):
    """No shipped board names the exit position, so the default pair does
    nothing with it; a rule garmin.exit_gps -> subsurface.exit_gps writes
    the GPS2 extradata once and a second run finds nothing to update."""
    from src.core.config import ConfigManager, SettingsModel, SyncFilters, SyncPairModel
    from src.core.fields import FieldLink, common_default_links
    from src.core.sync_engine import SyncEngine
    from tests.test_link_engine import FakeGarmin, _dive

    g = [_dive(date_time=datetime(2026, 8, 29, 12, 52, 59), external_ids={"garmin": "24438065346"}, exit_lat=7.611, exit_lng=98.381)]
    dc_path = os.path.join(repo_copy, "2026/08/29-Sat-12=52=59/Divecomputer")

    def run(links):
        # a pair's board lives on its sync_pairs entry (the top-level keys describe the garmin_divelogs pair only)
        pair = SyncPairModel(id="g2s", source="garmin", target=f"subsurface:{repo_copy}", directionality="to_subsurface",
                             field_links=links)
        settings = SettingsModel(sync_filters=SyncFilters(only_new=False), sync_pairs=[pair], api_cooldown_seconds=0.0)
        path = str(tmp_path / "settings.json")
        ConfigManager.save_settings(settings, path)
        engine = SyncEngine(settings_path=path, credentials_path=str(tmp_path / "c.json"),
                            source_adapter=FakeGarmin(g), target_adapter=SubsurfaceAdapter(repo_copy))
        return engine.run_sync(dry_run=False)

    default = common_default_links("garmin", "subsurface", match_on_dive_number=False)
    assert not any(link.target.endswith(".exit_gps") for link in default)
    res = run(default)
    assert res["matched_count"] == 1 and "GPS2" not in open(dc_path).read()      # the link write (dive_sync:garmin) only
    links = default + [FieldLink(id="exit", source=["garmin.exit_gps"], target="subsurface.exit_gps", direction="to_target",
                                 conflict="prefer_source")]
    res = run(links)
    assert len(res["updated_on_subsurface"]) == 1 and 'keyvalue "GPS2" "7.611000, 98.381000"' in open(dc_path).read()
    res = run(links)
    assert res["matched_count"] == 1 and res["updated_on_subsurface"] == [] and not res.get("conflicts")


# ---------------------------------------------------------------- R12: every sensor and channel per sample

def test_every_tank_pressure_and_channel_is_read_from_the_fixture():
    """plans/gap-roadmap.md R12: a sample's ``<bar>:<n>`` pressures land
    under tank ``n`` (the git format names the cylinder on the pressure
    itself), the other tokens Subsurface writes (ndl, tts, cns, po2,
    heartbeat) become channels, held until the next written value, while
    ``pressure`` stays the lowest tank's and the temperature is not held."""
    adapter = SubsurfaceAdapter(FIXTURE)
    assert adapter.login()
    dive = next(d for d in adapter.fetch_dives() if d.external_ids["subsurface"].startswith("2026/08/29-Sat-12=52=59"))
    by_time = {s.time: s for s in dive.samples}
    # "  0:18 1.318m 162.64bar:0 157.95bar:1": both tanks, pressure the first's
    s18 = by_time[18]
    assert s18.channels.pressures == {0: 162.64, 1: 157.95} and s18.pressure == 162.64
    # "  0:08 1.438m 160.69bar:1 tts=0:09": tank 1 only, so no pressure (tank 0 has readings elsewhere)
    s8 = by_time[8]
    assert s8.channels.pressures == {1: 160.69} and s8.pressure is None and s8.channels.tts == 9
    # sticky: the first sample's po2=0.7bar (an open-circuit dive: the computer's PO2) holds
    assert all(s.channels.ppo2 == 0.7 for s in dive.samples)
    assert all(s.channels.setpoint is None for s in dive.samples)
    # the temperature is not held: as before R12
    assert dive.samples[0].temp == 31.0 and any(s.temp is None for s in dive.samples)
    # the tanks' start and end still come from the pressures
    assert [g.start_pressure is not None for g in dive.gas_mixtures] == [True, True]
    hr = next(d for d in adapter.fetch_dives() if d.external_ids["subsurface"].startswith("2025/05/11-Sun-16=22=10"))
    assert hr.samples[0].channels.heart_rate == 111 and hr.samples[0].channels.cns == 1.0
    assert hr.samples[0].channels.tts == 10


def test_a_sample_with_only_the_first_tank_keeps_no_channels():
    """A computer whose samples hold depth, temperature and tank 0's
    pressure only reads exactly as before R12: ``channels`` None."""
    dc = parse_divecomputer("Divecomputer", 'model "X"\n  0:00 0.0m 25.0°C 200.0bar\n  0:10 5.0m 199.0bar:0\n')
    samples = unified_samples(dc, [GasMixture(oxygen=21.0)])
    assert [(s.time, s.depth, s.temp, s.pressure, s.channels) for s in samples] == \
        [(0, 0.0, 25.0, 200.0, None), (10, 5.0, None, 199.0, None)]


def test_rebreather_dctype_modechange_and_cells():
    """``po2`` is the setpoint while the computer is on a rebreather
    (``dctype CCR`` and its modechange events), the computer's PO2 after a
    bailout; ``sensor1``.. are the O2 cells; ``o2pressure=`` (an old CCR
    log) is the oxygen tank's pressure."""
    text = ('model "CCR"\ndctype CCR\n'
            'event 1:00 type=8 flags=0 divemode="OC" name="modechange"\n'
            '  0:00 1.0m 200.0bar:1 o2pressure=180.0bar po2=1.3bar sensor1=1.28bar sensor2=1.31bar ndl=99:00\n'
            '  0:30 10.0m stoptime=1:00 stopdepth=3.0m in_deco=1 cns=5% rbt=40:00 heartbeat=80\n'
            '  1:30 5.0m po2=0.9bar\n')
    dc = parse_divecomputer("Divecomputer", text)
    assert dc.dctype == "CCR" and dc.mode_changes == [(60, "OC")]
    tanks = [GasMixture(oxygen=10.0, helium=50.0, tank_role="diluent"), GasMixture(oxygen=21.0), GasMixture(oxygen=100.0, tank_role="oxygen")]
    first, second, third = unified_samples(dc, tanks)
    assert first.channels.setpoint == 1.3 and first.channels.ppo2 is None
    assert first.channels.o2_cells == {0: 1.28, 1: 1.31} and first.channels.pressures == {1: 200.0, 2: 180.0}
    assert first.channels.ndl == 5940
    assert (second.channels.deco_stop_time, second.channels.deco_stop_depth, second.channels.cns,
            second.channels.gas_time, second.channels.heart_rate) == (60, 3.0, 5.0, 2400, 80)
    assert second.channels.setpoint == 1.3 and second.channels.o2_cells == {0: 1.28, 1: 1.31}   # held
    assert third.channels.ppo2 == 0.9 and third.channels.setpoint is None                       # after the bailout


def _rich_dive(**kw) -> UnifiedDive:
    """A two-tank rebreather dive with every channel the git format holds."""
    def ch(**c):
        return SampleChannels(**c)
    samples = [
        UnifiedSample(time=0, depth=0.0, temp=28.0, pressure=200.0,
                      channels=ch(pressures={0: 200.0, 1: 190.0}, ndl=5940, tts=0, cns=1.0, setpoint=0.7,
                                  o2_cells={0: 0.69, 1: 0.71, 2: 0.7}, heart_rate=72)),
        UnifiedSample(time=60, depth=12.0, temp=27.5, pressure=190.0,
                      channels=ch(pressures={0: 190.0, 1: 189.5}, ndl=1200, tts=60, cns=2.0, setpoint=1.3,
                                  o2_cells={0: 1.28, 1: 1.31, 2: 1.3}, heart_rate=80)),
        UnifiedSample(time=120, depth=20.0, temp=27.0, pressure=None,
                      channels=ch(pressures={1: 189.0}, ndl=0, tts=300, deco_stop_depth=3.0, deco_stop_time=120,
                                  cns=2.0, setpoint=1.3, o2_cells={0: 1.28, 1: 1.31, 2: 1.3}, gas_time=1800, heart_rate=81)),
        UnifiedSample(time=180, depth=0.0, temp=27.0, pressure=180.0, channels=ch(pressures={0: 180.0})),
    ]
    base = dict(date_time=datetime(2026, 9, 5, 9, 0), duration=180, max_depth=20.0, dive_mode="ccr",
                # start and end as every reader of a per-tank log gives them (and Subsurface derives them back)
                gas_mixtures=[GasMixture(oxygen=21.0, tank_role="diluent", start_pressure=200.0, end_pressure=180.0),
                              GasMixture(oxygen=100.0, tank_role="oxygen", start_pressure=190.0, end_pressure=189.0)],
                samples=samples, external_ids={"uddf": "d1"})
    base.update(kw)
    return UnifiedDive(**base)


def test_a_new_dive_writes_every_tank_and_channel_and_reads_back(repo_copy):
    """A new dive's samples are written as Subsurface's git writer spells
    them - ``<bar>:<tank>`` per tank, then ``ndl=m:ss tts= in_deco=
    stoptime= stopdepth=…m cns=…% po2=…bar sensorN=…bar heartbeat=``, sticky
    values only when they change - with ``dctype CCR`` on the computer, and
    read back with the same channels."""
    adapter = SubsurfaceAdapter(repo_copy)
    assert adapter.login()
    dive = _rich_dive()
    new_id = adapter.add_dive(dive)
    text = open(os.path.join(repo_copy, new_id.rsplit("/", 1)[0], "Divecomputer"), encoding="utf-8").read()
    lines = text.splitlines()
    assert "dctype CCR" in lines
    assert "  0:00 0.0m 28.0°C 200.0bar:0 190.0bar:1 ndl=99:00 cns=1% po2=0.7bar sensor1=0.69bar sensor2=0.71bar sensor3=0.7bar heartbeat=72" in lines
    assert "  1:00 12.0m 27.5°C 190.0bar:0 189.5bar:1 ndl=20:00 tts=1:00 cns=2% po2=1.3bar sensor1=1.28bar sensor2=1.31bar sensor3=1.3bar heartbeat=80" in lines
    assert "  2:00 20.0m 27.0°C 189.0bar:1 ndl=0:00 tts=5:00 rbt=30:00 in_deco=1 stoptime=2:00 stopdepth=3.0m heartbeat=81" in lines
    assert "  3:00 0.0m 27.0°C 180.0bar:0" in lines
    back = next(d for d in SubsurfaceAdapter(repo_copy).fetch_dives() if d.external_ids["subsurface"] == new_id)
    assert [s.pressure for s in back.samples] == [200.0, 190.0, None, 180.0]
    for got, want in zip(back.samples, dive.samples):
        assert got.channels.pressures == want.channels.pressures
        assert got.channels.o2_cells == (want.channels.o2_cells or got.channels.o2_cells)   # the last sample holds them
        assert got.channels.setpoint == (want.channels.setpoint or 1.3)
        assert (got.depth, got.temp, got.time) == (want.depth, want.temp, want.time)
    from src.core.fields import are_samples_different
    assert not are_samples_different(dive.samples, back.samples)          # a sync sees the same profile
    third = back.samples[2].channels
    assert (third.ndl, third.tts, third.deco_stop_depth, third.deco_stop_time, third.gas_time, third.heart_rate) == \
        (0, 300, 3.0, 120, 1800, 81)
    # a dive whose samples carry only a pressure is written as before R12: tank 0
    plain = UnifiedDive(date_time=datetime(2026, 9, 6, 9, 0), duration=60, max_depth=5.0,
                        samples=[UnifiedSample(time=0, depth=0.0, pressure=200.0), UnifiedSample(time=60, depth=5.0)])
    plain_id = adapter.add_dive(plain)
    plain_lines = open(os.path.join(repo_copy, plain_id.rsplit("/", 1)[0], "Divecomputer"), encoding="utf-8").read().splitlines()
    assert "  0:00 0.0m 200.0bar:0" in plain_lines and "  1:00 5.0m" in plain_lines
    assert not any(line.startswith("dctype") for line in plain_lines)


def test_richer_samples_change_nothing_on_a_rerun(repo_copy, tmp_path):
    """R12 must not make a sync flag profiles: a rich dive uploaded to
    Subsurface, and Subsurface's fixture dives (now read with channels)
    uploaded to a UDDF file, are matched on the next run with nothing to
    update, no conflict, and the Subsurface files untouched."""
    from src.core.config import ConfigManager, SettingsModel, SyncFilters, SyncPairModel
    from src.core.fields import common_default_links
    from src.core.services.uddf import UddfAdapter
    from src.core.sync_engine import SyncEngine
    from tests.test_link_engine import RecordingAdapter

    class FakeUddf(RecordingAdapter):
        service_id = "uddf"
        display_name = "UDDF"
        _catalog = UddfAdapter.field_catalog()

    def snapshot():
        out = {}
        for root, _dirs, files in os.walk(repo_copy):
            if ".git" in root:
                continue
            for name in files:
                with open(os.path.join(root, name), "rb") as f:
                    out[os.path.join(root, name)] = f.read()
        return out

    # a rich dive into Subsurface: uploaded once, then nothing
    pair = SyncPairModel(id="u2s", source="uddf:x.uddf", target=f"subsurface:{repo_copy}", directionality="to_subsurface",
                         field_links=common_default_links("uddf", "subsurface"))
    spath = str(tmp_path / "s1.json")
    ConfigManager.save_settings(SettingsModel(sync_filters=SyncFilters(only_new=False), sync_pairs=[pair],
                                              api_cooldown_seconds=0.0), spath)
    source = [_rich_dive()]

    def run_in():
        return SyncEngine(settings_path=spath, credentials_path=str(tmp_path / "c.json"),
                          source_adapter=FakeUddf(source), target_adapter=SubsurfaceAdapter(repo_copy)).run_sync(dry_run=False)
    assert len(run_in()["uploaded_to_subsurface"]) == 1
    before = snapshot()
    again = run_in()
    assert again["uploaded_to_subsurface"] == [] and again.get("updated_on_subsurface", []) == [] and not again.get("conflicts")
    assert snapshot() == before

    # the fixture's dives out to UDDF: uploaded once with their channels, then nothing
    uddf_path = str(tmp_path / "out.uddf")
    # without the tanks link: the UDDF reader names a tank by its tankdata id, so a named Subsurface cylinder
    # differs on every run whatever the samples hold (older than R12, reported separately)
    pair = SyncPairModel(id="s2u", source=f"subsurface:{repo_copy}", target=f"uddf:{uddf_path}", directionality="to_uddf",
                         field_links=[link for link in common_default_links("subsurface", "uddf") if link.id != "tanks"])
    spath2 = str(tmp_path / "s2.json")
    ConfigManager.save_settings(SettingsModel(sync_filters=SyncFilters(only_new=False), sync_pairs=[pair],
                                              api_cooldown_seconds=0.0), spath2)

    def run_out():
        return SyncEngine(settings_path=spath2, credentials_path=str(tmp_path / "c.json"),
                          source_adapter=SubsurfaceAdapter(repo_copy), target_adapter=UddfAdapter(uddf_path)).run_sync(dry_run=False)
    first = run_out()
    assert len(first["uploaded_to_uddf"]) == len(SubsurfaceAdapter(repo_copy).fetch_dives())
    written = UddfAdapter(uddf_path).fetch_dives()
    assert any(s.channels and len(s.channels.pressures) == 2 for d in written for s in d.samples)   # both tanks went along
    uddf_before = open(uddf_path, encoding="utf-8").read()
    second = run_out()
    assert second["uploaded_to_uddf"] == [] and second.get("updated_on_uddf", []) == [] and not second.get("conflicts")
    assert open(uddf_path, encoding="utf-8").read() == uddf_before
