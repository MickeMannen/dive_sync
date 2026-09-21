"""Track C phase 1: the link-driven matched-pair loop, match keys and the
generic source/target engine.

``test_link_loop_reproduces_legacy_loop`` is the "no behaviour change" proof:
it re-implements the pre-Track-C matched-pair loop verbatim as a reference and
checks the new engine, on its default board, makes identical writes across a
few hundred seeded field/direction combinations."""
import copy
import json
import os
import random
from datetime import datetime
from typing import List, Optional

import pytest

from src.core.adapter import BaseDiveAdapter
from src.core.config import ConfigManager, SettingsModel, SyncFilters
from src.core.fields import FieldLink, FieldSpec, are_gas_mixtures_different, default_field_links, legacy_field_links
from src.core.models import GasMixture, UnifiedDive, UnifiedSample
from src.core.sync_engine import SyncEngine
from src.core.services.divelogs import DivelogsAdapter
from src.core.services.garmin import GarminAdapter


# ---------------------------------------------------------------- harness

class RecordingAdapter(BaseDiveAdapter):
    """In-memory adapter: serves a fixed dive list, records writes."""
    service_id = ""
    display_name = ""
    _catalog: List[FieldSpec] = []

    def __init__(self, dives=None):
        self.dives = list(dives or [])
        self.updated = []
        self.added = []

    @classmethod
    def field_catalog(cls):
        return cls._catalog

    def login(self):
        return True

    def fetch_dives(self, date_from=None, date_to=None):
        return self.dives

    def add_dive(self, dive):
        self.added.append(dive)
        return f"new-{len(self.added)}"

    def update_dive(self, external_id, dive):
        self.updated.append((external_id, dive))
        return True

    def delete_dive(self, external_id):
        return True


class FakeGarmin(RecordingAdapter):
    service_id = "garmin"
    display_name = "Garmin Connect"
    _catalog = GarminAdapter.field_catalog()


class FakeDivelogs(RecordingAdapter):
    service_id = "divelogs"
    display_name = "Divelogs.org"
    _catalog = DivelogsAdapter.field_catalog()


def _settings_file(tmp_path, **overrides) -> str:
    overrides.setdefault("sync_filters", SyncFilters(only_new=False))
    settings = SettingsModel(api_cooldown_seconds=0.0, **overrides)
    path = str(tmp_path / "settings.json")
    ConfigManager.save_settings(settings, path)
    return path


def _engine(tmp_path, garmin_dives, divelogs_dives, **settings) -> SyncEngine:
    path = _settings_file(tmp_path, **settings)
    creds = str(tmp_path / "credentials.json")
    return SyncEngine(settings_path=path, credentials_path=creds,
                      source_adapter=FakeGarmin(garmin_dives), target_adapter=FakeDivelogs(divelogs_dives))


def _dive(**kw) -> UnifiedDive:
    base = dict(date_time=datetime(2026, 6, 22, 12, 0, 0), duration=3000, max_depth=20.0)
    base.update(kw)
    return UnifiedDive(**base)


def _pair(g_kw=None, d_kw=None):
    g = _dive(external_ids={"garmin": "1", "divelogs": "2"}, **(g_kw or {}))
    d = _dive(external_ids={"garmin": "1", "divelogs": "2"}, **(d_kw or {}))
    return g, d


# ---------------------------------------------------------------- legacy reference

def legacy_matched_pair(g_dive: UnifiedDive, d_dive: UnifiedDive, direction: str, sync_gases: bool):
    """The matched-pair field loop as it was before Track C (verbatim logic).
    Returns (needs_garmin_update, needs_divelogs_update) and mutates the dives."""
    needs_garmin_update = False
    needs_divelogs_update = False

    if direction in ["to_divelogs", "bidirectional"]:
        has_diff = False
        g_buddy = "" if (g_dive.buddy is None or g_dive.buddy == "None") else g_dive.buddy.strip()
        d_buddy = "" if (d_dive.buddy is None or d_dive.buddy == "None") else d_dive.buddy.strip()
        if g_buddy != d_buddy:
            d_dive.buddy = g_dive.buddy
            has_diff = True
        g_notes = "" if (g_dive.notes is None or g_dive.notes == "None") else g_dive.notes.strip()
        d_notes = "" if (d_dive.notes is None or d_dive.notes == "None") else d_dive.notes.strip()
        if g_notes != d_notes:
            d_dive.notes = g_dive.notes
            has_diff = True
        if g_dive.weight != d_dive.weight or g_dive.weight_unit != d_dive.weight_unit:
            d_dive.weight = g_dive.weight
            d_dive.weight_unit = g_dive.weight_unit
            has_diff = True
        if g_dive.visibility != d_dive.visibility or g_dive.visibility_unit != d_dive.visibility_unit:
            d_dive.visibility = g_dive.visibility
            d_dive.visibility_unit = g_dive.visibility_unit
            has_diff = True
        if g_dive.lat != d_dive.lat or g_dive.lng != d_dive.lng:
            d_dive.lat = g_dive.lat
            d_dive.lng = g_dive.lng
            has_diff = True
        if g_dive.samples != d_dive.samples:
            d_dive.samples = g_dive.samples
            has_diff = True
        if sync_gases and are_gas_mixtures_different(g_dive.gas_mixtures, d_dive.gas_mixtures):
            d_dive.gas_mixtures = [
                GasMixture(oxygen=gm.oxygen, helium=gm.helium, start_pressure=gm.start_pressure,
                           end_pressure=gm.end_pressure, tank_volume=gm.tank_volume)
                for gm in g_dive.gas_mixtures
            ]
            has_diff = True
        if has_diff:
            needs_divelogs_update = True

    if direction in ["to_garmin", "bidirectional"]:
        has_diff = False
        g_buddy = "" if (g_dive.buddy is None or g_dive.buddy == "None") else g_dive.buddy.strip()
        d_buddy = "" if (d_dive.buddy is None or d_dive.buddy == "None") else d_dive.buddy.strip()
        if d_buddy != g_buddy:
            g_dive.buddy = d_dive.buddy
            has_diff = True
        g_notes = "" if (g_dive.notes is None or g_dive.notes == "None") else g_dive.notes.strip()
        d_notes = "" if (d_dive.notes is None or d_dive.notes == "None") else d_dive.notes.strip()
        if d_notes != g_notes:
            g_dive.notes = d_dive.notes
            has_diff = True
        if d_dive.weight != g_dive.weight or d_dive.weight_unit != g_dive.weight_unit:
            g_dive.weight = d_dive.weight
            g_dive.weight_unit = d_dive.weight_unit
            has_diff = True
        if d_dive.visibility != g_dive.visibility or d_dive.visibility_unit != g_dive.visibility_unit:
            g_dive.visibility = d_dive.visibility
            g_dive.visibility_unit = d_dive.visibility_unit
            has_diff = True
        if g_dive.lat is None or g_dive.lng is None:
            if d_dive.lat != g_dive.lat or d_dive.lng != g_dive.lng:
                g_dive.lat = d_dive.lat
                g_dive.lng = d_dive.lng
                has_diff = True
        if has_diff:
            needs_garmin_update = True

    return needs_garmin_update, needs_divelogs_update


TEXT_STATES = [None, "", "None", "Anna", "Bo", " Anna "]
WEIGHT_STATES = [(None, None), (5.0, "kilogram"), (6.0, "kilogram"), (5.0, "pound"), (0.0, None)]
VIS_STATES = [(None, None), (9.0, "meter"), (30.0, "foot"), (0.0, None)]
# A half coordinate (only one of lat/lng) is deliberately not in the grid: the
# old loop replaced such a Garmin value with an *empty* Divelogs pair under
# to_garmin, and the gps_fill link (prefer_non_empty) will not copy an empty
# source. That is the one known, accepted deviation from the old loop.
GPS_STATES = [(None, None), (1.0, 2.0), (3.0, 4.0)]
SAMPLE_STATES = [[], [UnifiedSample(depth=1.0, time=0)], [UnifiedSample(depth=2.0, time=0)]]
TANK_STATES = [
    [],
    [GasMixture(oxygen=21.0, start_pressure=200.0, tank_name="left")],
    [GasMixture(oxygen=32.0, start_pressure=200.0)],
    [GasMixture(oxygen=21.0, start_pressure=200.0), GasMixture(oxygen=50.0)],
]


def _random_state(rng):
    return dict(
        buddy=rng.choice(TEXT_STATES), notes=rng.choice(TEXT_STATES),
        weight=rng.choice(WEIGHT_STATES), visibility=rng.choice(VIS_STATES),
        gps=rng.choice(GPS_STATES), samples=rng.choice(SAMPLE_STATES), tanks=rng.choice(TANK_STATES),
    )


def _apply_state(dive: UnifiedDive, st):
    dive.buddy, dive.notes = st["buddy"], st["notes"]
    dive.weight, dive.weight_unit = st["weight"]
    dive.visibility, dive.visibility_unit = st["visibility"]
    dive.lat, dive.lng = st["gps"]
    dive.samples = copy.deepcopy(st["samples"])
    dive.gas_mixtures = copy.deepcopy(st["tanks"])


def _snapshot(dive: UnifiedDive):
    return dive.model_dump(mode="json")


def test_link_loop_reproduces_legacy_loop(tmp_path):
    rng = random.Random(20260921)
    cases = 0
    for _ in range(400):
        direction = rng.choice(["bidirectional", "to_divelogs", "to_garmin"])
        sync_gases = rng.random() > 0.2
        g_state, d_state = _random_state(rng), _random_state(rng)

        # Reference
        g_ref, d_ref = _pair()
        _apply_state(g_ref, g_state)
        _apply_state(d_ref, d_state)
        if not sync_gases:  # the old run_sync cleared gases on every dive before its loop
            g_ref.gas_mixtures, d_ref.gas_mixtures = [], []
        ref_g, ref_d = legacy_matched_pair(g_ref, d_ref, direction, sync_gases)

        # Engine on the legacy board (the shipped defaults changed on 2026-09-21)
        g_new, d_new = _pair()
        _apply_state(g_new, g_state)
        _apply_state(d_new, d_state)
        engine = _engine(tmp_path, [g_new], [d_new], directionality=direction, field_links=legacy_field_links(),
                         sync_filters=SyncFilters(only_new=False, sync_gases=sync_gases))
        results = engine.run_sync(dry_run=False)

        wrote_g = len(results["updated_on_garmin"]) == 1
        wrote_d = len(results["updated_on_divelogs"]) == 1
        ctx = f"direction={direction} gases={sync_gases} g={g_state} d={d_state}"
        assert wrote_g == ref_g, f"garmin update flag differs: {ctx}"
        assert wrote_d == ref_d, f"divelogs update flag differs: {ctx}"
        assert _snapshot(g_new) == _snapshot(g_ref), f"garmin dive differs: {ctx}"
        assert _snapshot(d_new) == _snapshot(d_ref), f"divelogs dive differs: {ctx}"
        cases += 1
    assert cases == 400


# ---------------------------------------------------------------- policies (C3 groundwork)

def _one_link(tmp_path, g_kw, d_kw, link: FieldLink, direction="bidirectional"):
    g, d = _pair(g_kw, d_kw)
    engine = _engine(tmp_path, [g], [d], directionality=direction, field_links=[link])
    results = engine.run_sync(dry_run=False)
    return g, d, results


def test_policy_source_wins_and_target_wins(tmp_path):
    g, d, res = _one_link(tmp_path, {"buddy": "A"}, {"buddy": "B"},
                          FieldLink(id="buddy", source=["garmin.buddy"], target="divelogs.buddy", conflict="source_wins"))
    assert (g.buddy, d.buddy) == ("A", "A") and len(res["updated_on_divelogs"]) == 1 and not res["updated_on_garmin"]

    g, d, res = _one_link(tmp_path, {"buddy": "A"}, {"buddy": "B"},
                          FieldLink(id="buddy", source=["garmin.buddy"], target="divelogs.buddy", conflict="target_wins"))
    assert (g.buddy, d.buddy) == ("B", "B") and len(res["updated_on_garmin"]) == 1 and not res["updated_on_divelogs"]


def test_policy_prefer_non_empty_fills_blanks_only(tmp_path):
    link = FieldLink(id="buddy", source=["garmin.buddy"], target="divelogs.buddy", conflict="prefer_non_empty")
    g, d, _ = _one_link(tmp_path, {"buddy": None}, {"buddy": "B"}, link)
    assert (g.buddy, d.buddy) == ("B", "B")
    g, d, _ = _one_link(tmp_path, {"buddy": "A"}, {"buddy": ""}, link)
    assert (g.buddy, d.buddy) == ("A", "A")
    g, d, res = _one_link(tmp_path, {"buddy": "A"}, {"buddy": "B"}, link)
    assert (g.buddy, d.buddy) == ("A", "B") and not res["updated_on_garmin"] and not res["updated_on_divelogs"]


def test_policy_manual_skips_real_conflicts(tmp_path):
    link = FieldLink(id="notes", source=["garmin.notes"], target="divelogs.notes", conflict="manual")
    g, d, res = _one_link(tmp_path, {"notes": "A"}, {"notes": "B"}, link)
    assert (g.notes, d.notes) == ("A", "B") and not res["updated_on_garmin"] and not res["updated_on_divelogs"]
    g, d, _ = _one_link(tmp_path, {"notes": "A"}, {"notes": None}, link)
    assert (g.notes, d.notes) == ("A", "A")


def test_link_direction_and_global_direction_gate_writes(tmp_path):
    # Link only writes towards Garmin; global direction forbids Garmin writes -> nothing
    link = FieldLink(id="buddy", source=["divelogs.buddy"], target="garmin.buddy", direction="to_target")
    g, d, res = _one_link(tmp_path, {"buddy": "A"}, {"buddy": "B"}, link, direction="to_divelogs")
    assert (g.buddy, d.buddy) == ("A", "B") and not res["updated_on_garmin"] and not res["updated_on_divelogs"]
    # Same link, global bidirectional: Divelogs is the origin regardless of policy name
    g, d, res = _one_link(tmp_path, {"buddy": "A"}, {"buddy": "B"}, link)
    assert (g.buddy, d.buddy) == ("B", "B") and len(res["updated_on_garmin"]) == 1
    # A one-way link with a fill-only policy leaves an existing value alone
    link = FieldLink(id="buddy", source=["divelogs.buddy"], target="garmin.buddy", direction="to_target", conflict="prefer_non_empty")
    g, d, _ = _one_link(tmp_path, {"buddy": "A"}, {"buddy": "B"}, link)
    assert (g.buddy, d.buddy) == ("A", "B")
    # 'off' never writes
    link = FieldLink(id="buddy", source=["garmin.buddy"], target="divelogs.buddy", direction="off")
    g, d, res = _one_link(tmp_path, {"buddy": "A"}, {"buddy": "B"}, link)
    assert (g.buddy, d.buddy) == ("A", "B")


def test_service_fields_sync_through_a_link(tmp_path):
    link = FieldLink(id="site", source=["garmin.activityName"], target="divelogs.divesite", direction="to_target")
    g, d, res = _one_link(tmp_path, {"service_fields": {"activityName": "Wreck"}},
                          {"service_fields": {"location": "Malmö", "divesite": "Old"}}, link)
    assert d.service_fields["divesite"] == "Wreck" and d.service_fields["location"] == "Malmö"
    assert len(res["updated_on_divelogs"]) == 1


def test_sync_gases_off_disables_tanks_links(tmp_path):
    link = FieldLink(id="tanks", source=["garmin.tanks"], target="divelogs.tanks", direction="to_target")
    g, d = _pair({"gas_mixtures": [GasMixture(oxygen=32.0)]}, {})
    engine = _engine(tmp_path, [g], [d], field_links=[link],
                     sync_filters=SyncFilters(only_new=False, sync_gases=False))
    res = engine.run_sync(dry_run=False)
    assert d.gas_mixtures == [] and not res["updated_on_divelogs"]


def test_invalid_links_are_skipped_not_fatal(tmp_path):
    links = [
        FieldLink(id="bad", source=["garmin.buddy"], target="divelogs.max_depth"),
        FieldLink(id="good", source=["garmin.notes"], target="divelogs.notes"),
    ]
    g, d = _pair({"buddy": "A", "notes": "N"}, {"buddy": "B", "notes": "M"})
    engine = _engine(tmp_path, [g], [d], field_links=links)
    engine.run_sync(dry_run=False)
    assert (d.buddy, d.notes) == ("B", "N")


def test_dry_run_reports_without_writing(tmp_path):
    g, d = _pair({"buddy": "A"}, {"buddy": None})  # a blank side is filled on the default board
    engine = _engine(tmp_path, [g], [d])
    res = engine.run_sync(dry_run=True)
    assert res["updated_on_divelogs"][0]["dry_run"] is True
    assert engine.target.updated == [] and not os.path.exists(engine.state_file)


def test_field_links_override_survives_settings_reload(tmp_path):
    g, d = _pair({"buddy": "A"}, {"buddy": "B"})
    engine = _engine(tmp_path, [g], [d])
    off = [FieldLink(id="buddy", source=["garmin.buddy"], target="divelogs.buddy", direction="off")]
    res = engine.run_sync(dry_run=False, field_links_override=off)
    assert (g.buddy, d.buddy) == ("A", "B") and not res["updated_on_divelogs"]


# ---------------------------------------------------------------- match keys (C19)

def test_match_keys_come_from_the_board(tmp_path):
    g = [_dive(date_time=datetime(2026, 1, 1, 8), dive_number=7, external_ids={"garmin": "1"})]
    d = [_dive(date_time=datetime(2026, 1, 1, 20), dive_number=7, external_ids={"divelogs": "2"})]

    # The Garmin/Divelogs default board has no match key (Divelogs numbers its own dives)
    engine = _engine(tmp_path, g, d)
    matched, ug, ud = engine.match_dives(g, d)
    assert matched == [] and len(ug) == 1 and len(ud) == 1  # ids differ, 12 h apart

    with_key = legacy_field_links()
    engine = _engine(tmp_path, g, d, field_links=with_key)
    matched, ug, ud = engine.match_dives(g, d)
    assert len(matched) == 1  # same number, same day

    # A datetime match key drawn from the Divelogs side works too
    key = FieldLink(id="t", source=["divelogs.date_time"], target="garmin.date_time", direction="off", match_order=1)
    d2 = [_dive(date_time=datetime(2026, 1, 1, 8), dive_number=99, external_ids={"divelogs": "2"})]
    engine = _engine(tmp_path, g, d2, field_links=[key], grace_window_minutes=0)
    matched, _, _ = engine.match_dives(g, d2)
    assert len(matched) == 1

    # Dive number zero is "unset"
    g0 = [_dive(date_time=datetime(2026, 1, 1, 8), dive_number=0, external_ids={"garmin": "1"})]
    d0 = [_dive(date_time=datetime(2026, 1, 1, 20), dive_number=0, external_ids={"divelogs": "2"})]
    engine = _engine(tmp_path, g0, d0, field_links=with_key)
    assert engine.match_dives(g0, d0)[0] == []


def test_match_key_hit_needs_start_times_within_a_day(tmp_path):
    """Two logs numbered independently share numbers across decades; a number
    hit only counts when the dives start within 24 h of each other, and it
    must not steal the true timestamp partner (live baseline, 2026-09-21)."""
    g = [_dive(date_time=datetime(2026, 5, 3, 22), dive_number=16, external_ids={"garmin": "16"}),
         _dive(date_time=datetime(2026, 2, 16, 10, 7), dive_number=20, external_ids={"garmin": "20"})]
    d = [_dive(date_time=datetime(2026, 2, 16, 10, 7), dive_number=16, external_ids={"divelogs": "a"})]
    engine = _engine(tmp_path, g, d, field_links=legacy_field_links())
    matched, ug, ud = engine.match_dives(g, d)
    assert len(matched) == 1
    assert matched[0][0].external_ids["garmin"] == "20"  # the timestamp twin, not the number twin
    assert [x.external_ids["garmin"] for x in ug] == ["16"]


def test_matching_uses_utc_when_both_sides_have_it(tmp_path):
    # Local times 8 h apart (different zones), same instant
    g = [_dive(date_time=datetime(2026, 6, 1, 20), date_time_utc=datetime(2026, 6, 1, 12), external_ids={"garmin": "1"})]
    d = [_dive(date_time=datetime(2026, 6, 1, 12), date_time_utc=datetime(2026, 6, 1, 12), external_ids={"divelogs": "2"})]
    engine = _engine(tmp_path, g, d)
    assert len(engine.match_dives(g, d)[0]) == 1
    # Without UTC on one side the naive local times decide (8 h apart: no match)
    d[0].date_time_utc = None
    assert engine.match_dives(g, d)[0] == []


# ---------------------------------------------------------------- generic pair (F1)

def test_engine_results_and_aliases_follow_service_ids(tmp_path):
    g = [_dive(external_ids={"garmin": "1"}, buddy="A")]
    d = [_dive(date_time=datetime(2026, 7, 1, 12), external_ids={"divelogs": "2"})]
    engine = _engine(tmp_path, g, d)
    assert engine.source_id == "garmin" and engine.target_id == "divelogs"
    assert engine.garmin is engine.source and engine.divelogs is engine.target
    res = engine.run_sync(dry_run=False)
    assert res["source"] == "garmin" and res["target"] == "divelogs"
    up = res["uploaded_to_divelogs"][0]
    assert up["garmin_id"] == "1" and up["source_id"] == "1" and up["new_divelogs_id"] == "new-1" and up["new_id"] == "new-1"
    up = res["uploaded_to_garmin"][0]
    assert up["divelogs_id"] == "2" and up["new_garmin_id"] == "new-1"


def test_engine_with_a_non_garmin_pair(tmp_path):
    class FakeUddf(RecordingAdapter):
        service_id = "uddf"
        display_name = "UDDF file"
        stores_external_ids = True
        _catalog = [
            FieldSpec(key="uddf.buddy", label="Buddy", type="text", unified="buddy"),
            FieldSpec(key="uddf.dive_number", label="Dive number", type="number", unified="dive_number"),
        ]

    links = [
        FieldLink(id="buddy", source=["divelogs.buddy"], target="uddf.buddy"),
        FieldLink(id="num", source=["divelogs.dive_number"], target="uddf.dive_number", direction="off", match_order=1),
    ]
    path = _settings_file(tmp_path, field_links=links, directionality="to_uddf")
    a = [_dive(external_ids={"divelogs": "10"}, dive_number=3, buddy="Anna"),
         _dive(date_time=datetime(2026, 8, 1), external_ids={"divelogs": "11"}, dive_number=4)]
    # same number and same day: the match key counts (a hit more than a day apart would not)
    b = [_dive(date_time=datetime(2026, 6, 22, 20), external_ids={"uddf": "x"}, dive_number=3, buddy=None)]
    engine = SyncEngine(settings_path=path, credentials_path=str(tmp_path / "c.json"),
                        source_adapter=FakeDivelogs(a), target_adapter=FakeUddf(b))
    assert engine.state_file.endswith("sync_state_divelogs_uddf.json")
    with pytest.raises(AttributeError):
        engine.garmin
    res = engine.run_sync(dry_run=False)
    assert res["matched_count"] == 1
    assert b[0].buddy == "Anna" and b[0].external_ids["divelogs"] == "10"
    assert len(res["updated_on_uddf"]) == 1 and res["updated_on_uddf"][0]["linked_divelogs"] == "10"
    assert len(res["uploaded_to_uddf"]) == 1 and res["uploaded_to_divelogs"] == []
    assert res["updated_on_divelogs"] == []  # to_uddf: divelogs is never written, not even the back-link


def test_engine_refuses_same_service_twice(tmp_path):
    with pytest.raises(ValueError, match="itself"):
        SyncEngine(settings_path=str(tmp_path / "s.json"), credentials_path=str(tmp_path / "c.json"),
                   source_adapter=FakeGarmin(), target_adapter=FakeGarmin())


# ---------------------------------------------------------------- phase 2: templates, uploads, conflicts, test mapping

from src.core.conflicts import ConflictStore  # noqa: E402

SITE_LINK = FieldLink(id="site_to_garmin", source=["divelogs.location", "divelogs.divesite", "divelogs.dive_number"],
                      target="garmin.activityName", direction="to_target",
                      template="{divelogs.location}, {divelogs.divesite} #{dive_number:03d}")


def test_composite_link_on_matched_pair(tmp_path):
    g, d = _pair({"service_fields": {"activityName": "Old name"}},
                 {"dive_number": 7, "service_fields": {"location": "Larnaca", "divesite": "Zenobia"}})
    engine = _engine(tmp_path, [g], [d], field_links=[SITE_LINK])
    res = engine.run_sync(dry_run=False)
    assert g.service_fields["activityName"] == "Larnaca, Zenobia #007"
    assert len(res["updated_on_garmin"]) == 1 and not res["updated_on_divelogs"]
    # equal after the first run -> nothing on the second
    res = engine.run_sync(dry_run=False)
    assert not res["updated_on_garmin"]
    # global direction forbids Garmin writes -> composite does nothing
    g.service_fields["activityName"] = "Old name"
    engine = _engine(tmp_path, [g], [d], field_links=[SITE_LINK], directionality="to_divelogs")
    res = engine.run_sync(dry_run=False)
    assert g.service_fields["activityName"] == "Old name" and not res["updated_on_garmin"]


def test_upload_renders_composite_and_service_links(tmp_path):
    d = _dive(external_ids={"divelogs": "2"}, dive_number=7, location="Larnaca, Zenobia",
              service_fields={"location": "Larnaca", "divesite": "Zenobia"},
              gas_mixtures=[GasMixture(oxygen=32.0, tank_name="left")])
    board = [l for l in default_field_links() if l.id != "activity_name"] + [SITE_LINK]
    engine = _engine(tmp_path, [], [d], field_links=board)
    engine.run_sync(dry_run=False)
    added = engine.source.added[0]
    assert added.service_fields["activityName"] == "Larnaca, Zenobia #007"
    assert added.gas_mixtures[0].tank_name == "left"      # identity links leave tanks alone
    assert added is not d and d.service_fields.get("activityName") is None  # the original is untouched

    # a plain service-field link is applied too; on the default board nothing changes
    g = _dive(external_ids={"garmin": "1"}, service_fields={"activityName": "Wreck", "locationName": None})
    link = FieldLink(id="site", source=["garmin.activityName"], target="divelogs.divesite", direction="to_target")
    engine = _engine(tmp_path, [g], [], field_links=[link])
    engine.run_sync(dry_run=False)
    assert engine.target.added[0].service_fields["divesite"] == "Wreck"
    engine = _engine(tmp_path, [g], [])
    engine.run_sync(dry_run=False)
    # default board: divesite follows locationName (empty here); the writer then falls back to the site name
    assert engine.target.added[0].service_fields.get("divesite") is None


def test_manual_conflicts_are_recorded_refreshed_and_not_written_on_dry_run(tmp_path):
    link = FieldLink(id="notes", source=["garmin.notes"], target="divelogs.notes", conflict="manual")
    g, d = _pair({"notes": "A"}, {"notes": "B"})
    engine = _engine(tmp_path, [g], [d], field_links=[link])

    res = engine.run_sync(dry_run=True)
    assert len(res["conflicts"]) == 1 and not os.path.exists(engine.conflicts_file)

    res = engine.run_sync(dry_run=False)
    stored = ConflictStore(engine.conflicts_file).load()
    assert len(stored) == 1 and stored[0].link_id == "notes"
    assert stored[0].source_value == "A" and stored[0].target_value == "B"
    assert stored[0].dive_ids == {"garmin": "1", "divelogs": "2"}
    assert res["conflicts"][0]["id"] == stored[0].id
    assert engine.list_conflicts()[0].id == stored[0].id

    # conflict gone on the next run -> entry dropped; a pair outside the run is kept
    other = stored[0].model_copy(update={"id": "other", "dive_ids": {"garmin": "9", "divelogs": "8"}})
    ConflictStore(engine.conflicts_file).save(stored + [other])
    d.notes = "A"
    engine.run_sync(dry_run=False)
    assert [c.id for c in engine.list_conflicts()] == ["other"]


def test_resolve_conflict_writes_the_chosen_side(tmp_path):
    link = FieldLink(id="notes", source=["garmin.notes"], target="divelogs.notes", conflict="manual")
    g, d = _pair({"notes": "A"}, {"notes": "B"})
    engine = _engine(tmp_path, [g], [d], field_links=[link])
    engine.run_sync(dry_run=False)
    conflict = engine.list_conflicts()[0]

    resolved = engine.resolve_conflict(conflict.id[:6], "source")
    assert resolved.id == conflict.id
    ext_id, dive = engine.target.updated[-1]
    assert ext_id == "2" and dive.notes == "A" and engine.list_conflicts() == []

    # the other way round, with a weight tuple and a wrong id
    link = FieldLink(id="weight", source=["garmin.weight"], target="divelogs.weight", conflict="manual")
    g, d = _pair({"weight": 5.0, "weight_unit": "kilogram"}, {"weight": 6.0, "weight_unit": "kilogram"})
    engine = _engine(tmp_path, [g], [d], field_links=[link])
    engine.run_sync(dry_run=False)
    conflict = engine.list_conflicts()[0]
    engine.resolve_conflict(conflict.id, "target")
    ext_id, dive = engine.source.updated[-1]
    assert ext_id == "1" and (dive.weight, dive.weight_unit) == (6.0, "kilogram")
    with pytest.raises(ValueError, match="No conflict"):
        engine.resolve_conflict("nope", "source")
    with pytest.raises(ValueError, match="winner"):
        engine.resolve_conflict("nope", "left")


def test_test_mapping_is_read_only(tmp_path):
    g = [_dive(external_ids={"garmin": "1"}, buddy="A", dive_number=1)]
    d = [_dive(external_ids={"divelogs": "2"}, buddy="B", dive_number=1),
         _dive(date_time=datetime(2026, 8, 1), external_ids={"divelogs": "3"}, dive_number=9)]
    engine = _engine(tmp_path, g, d)
    candidate = [FieldLink(id="buddy", source=["garmin.buddy"], target="divelogs.buddy", conflict="manual"),
                 FieldLink(id="dive_number", source=["garmin.dive_number"], target="divelogs.dive_number",
                           direction="off", match_order=1)]
    out = engine.test_mapping(candidate, limit=10)
    assert out["ok"] and out["matched"] == 1 and out["fetched"] == {"garmin": 1, "divelogs": 2}
    assert out["unmatched"]["divelogs"] == ["2026-08-01 00:00:00"]
    row = out["rows"][0]
    assert row["link"] == "buddy" and row["result"] == "conflict" and row["conflict"] is True
    assert (row["source_value"], row["target_value"]) == ("A", "B")
    # nothing written, nothing persisted, saved board untouched
    assert engine.target.updated == [] and g[0].buddy == "A" and d[0].buddy == "B"
    assert not os.path.exists(engine.state_file) and not os.path.exists(engine.conflicts_file)
    assert engine.settings.field_links == default_field_links()

    out = engine.test_mapping([FieldLink(id="bad", source=["garmin.buddy"], target="divelogs.max_depth")])
    assert not out["ok"] and out["problems"]


def test_fetch_recent_dives_default_and_garmin_override(monkeypatch):
    fake = FakeDivelogs([_dive(date_time=datetime(2026, m, 1), external_ids={"divelogs": str(m)}) for m in (3, 1, 2)])
    assert [d.external_ids["divelogs"] for d in fake.fetch_recent_dives(2)] == ["3", "2"]

    g = GarminAdapter("dummy", "dummy")
    g.logged_in = True
    g.cooldown_seconds = 0
    listed = [{"activityId": str(i), "activityType": {"typeKey": "diving"}, "startTimeLocal": f"2026-0{i}-01 10:00:00"} for i in (1, 3, 2)]
    fetched = []

    class Client:
        def get_activities(self, start, limit, activitytype=None):
            return listed if start == 0 else []
        def connectapi(self, url, params=None):
            if "tanksensor" in url:
                return None
            fetched.append(url)
            return {"activityId": url.rsplit("/", 1)[1], "summaryDTO": {"startTimeLocal": "2026-01-01 10:00:00", "duration": 1, "maxDepth": 1}}
        def get_activity_details(self, activity_id):
            return None
    g.client = Client()
    dives = g.fetch_recent_dives(2)
    assert [d.external_ids["garmin"] for d in dives] == ["3", "2"]
    assert len(fetched) == 2  # details only for the newest two


def test_known_pairs_are_remembered_and_used_for_matching(tmp_path):
    """Garmin and Divelogs cannot store each other's id: pairs live in the
    sync state, uploads are remembered, and no update is issued just to
    'link' a dive (the first real run on the test accounts rewrote every
    matched Divelogs dive for that reason, 2026-09-21)."""
    g = [_dive(external_ids={"garmin": "1"}, buddy="A"),
         _dive(date_time=datetime(2026, 7, 1, 12), external_ids={"garmin": "9"}, buddy="B")]
    d = [_dive(external_ids={"divelogs": "2"}, buddy="A")]
    engine = _engine(tmp_path, g, d)
    res = engine.run_sync(dry_run=False)
    assert res["updated_on_divelogs"] == [] and res["updated_on_garmin"] == []  # nothing to write
    assert engine.load_links() == {"1": "2", "9": "new-1"}  # matched pair + upload

    # Next run: the uploaded twin carries a different local time (say the
    # service shifted it), yet it is paired by id and not uploaded again
    d2 = [_dive(external_ids={"divelogs": "2"}, buddy="A"),
          _dive(date_time=datetime(2026, 7, 1, 20), external_ids={"divelogs": "new-1"}, buddy="B")]
    engine2 = _engine(tmp_path, g, d2)
    res = engine2.run_sync(dry_run=False)
    assert res["matched_count"] == 2 and res["uploaded_to_divelogs"] == [] and res["uploaded_to_garmin"] == []

    # A dry run neither records pairs nor touches the state file
    engine3 = _engine(tmp_path, [_dive(external_ids={"garmin": "5"})], [_dive(external_ids={"divelogs": "6"})])
    os.remove(engine3.state_file)
    engine3.run_sync(dry_run=True)
    assert not os.path.exists(engine3.state_file)

    # A service that can store ids still gets the link written through update_dive
    class LinkingDivelogs(FakeDivelogs):
        stores_external_ids = True
    path = _settings_file(tmp_path)
    g = [_dive(external_ids={"garmin": "1"})]
    d = [_dive(external_ids={"divelogs": "2"})]
    engine4 = SyncEngine(settings_path=path, credentials_path=str(tmp_path / "c.json"),
                         source_adapter=FakeGarmin(g), target_adapter=LinkingDivelogs(d))
    res = engine4.run_sync(dry_run=False)
    assert len(res["updated_on_divelogs"]) == 1 and engine4.target.updated[0][1].external_ids["garmin"] == "1"
    assert res["updated_on_garmin"] == []


def test_state_file_without_links_still_loads(tmp_path):
    engine = _engine(tmp_path, [], [])
    with open(engine.state_file, "w") as f:
        json.dump({"last_sync_time": "2026-09-01T10:00:00"}, f)
    assert engine.load_last_sync_time() == datetime(2026, 9, 1, 10) and engine.load_links() == {}
    engine.save_state(links={"a": "b"})
    assert engine.load_last_sync_time() == datetime(2026, 9, 1, 10) and engine.load_links() == {"a": "b"}


def test_full_compare_flag_is_honoured_once(tmp_path):
    """The 'apply the changed board to all matched dives' prompt sets a
    one-shot flag: the next run ignores the incremental window, then clears it."""
    from src.core.config import SyncFilters
    g = [_dive(external_ids={"garmin": "1"}, buddy="A")]
    d = [_dive(external_ids={"divelogs": "2"}, buddy=None)]
    engine = _engine(tmp_path, g, d, sync_filters=SyncFilters(only_new=True))
    engine.save_state(dt=datetime(2030, 1, 1))          # a last-sync far in the future: incremental would fetch nothing
    engine.request_full_compare(True)
    assert engine.load_state()["full_compare_once"] is True
    res = engine.run_sync(dry_run=False)
    assert engine.settings.sync_filters.only_new is False and len(res["updated_on_divelogs"]) == 1
    assert "full_compare_once" not in engine.load_state()
    # a dry run does not consume the flag
    engine.request_full_compare(True)
    engine.run_sync(dry_run=True)
    assert engine.load_state()["full_compare_once"] is True
    engine.request_full_compare(False)
    assert "full_compare_once" not in engine.load_state()
