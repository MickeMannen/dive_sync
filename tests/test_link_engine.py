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
from src.core.config import ConfigManager, SettingsModel, SyncFilters, SyncPairModel
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
        self.deleted = []

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
        self.deleted.append(external_id)
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
    # tank names are left out: the legacy loop dropped them, the rules carry
    # them across (see test_matched_dive_takes_the_garmin_tank_sensor_name)
    data = dive.model_dump(mode="json")
    for gas in data.get("gas_mixtures") or []:
        gas.pop("tank_name", None)
    return data


@pytest.mark.parametrize("direction", ["to_divelogs", "to_garmin"])
def test_link_loop_reproduces_legacy_loop(tmp_path, direction):
    """One directed half per run (rework.md G0): the old loop's
    'bidirectional' branch was its two directed branches applied in turn,
    which is exactly what two directed runs now do."""
    rng = random.Random(20260921)
    cases = 0
    for _ in range(200):
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
    assert cases == 200


# ---------------------------------------------------------------- policies (C3 groundwork)

def _one_link(tmp_path, g_kw, d_kw, link: FieldLink, direction="to_divelogs"):
    """One matched pair through one link. ``direction`` is the run's receiver
    (rework.md G0: a run writes one side) - pass ``to_garmin`` for a case
    that expects the Garmin side to change."""
    g, d = _pair(g_kw, d_kw)
    engine = _engine(tmp_path, [g], [d], directionality=direction, field_links=[link])
    results = engine.run_sync(dry_run=False)
    return g, d, results


def test_policy_source_wins_and_target_wins(tmp_path):
    g, d, res = _one_link(tmp_path, {"buddy": "A"}, {"buddy": "B"},
                          FieldLink(id="buddy", source=["garmin.buddy"], target="divelogs.buddy", conflict="source_wins"))
    assert (g.buddy, d.buddy) == ("A", "A") and len(res["updated_on_divelogs"]) == 1 and not res["updated_on_garmin"]

    g, d, res = _one_link(tmp_path, {"buddy": "A"}, {"buddy": "B"},
                          FieldLink(id="buddy", source=["garmin.buddy"], target="divelogs.buddy", conflict="target_wins"),
                          direction="to_garmin")
    assert (g.buddy, d.buddy) == ("B", "B") and len(res["updated_on_garmin"]) == 1 and not res["updated_on_divelogs"]


def test_policy_prefer_non_empty_fills_blanks_only(tmp_path):
    link = FieldLink(id="buddy", source=["garmin.buddy"], target="divelogs.buddy", conflict="prefer_non_empty")
    g, d, _ = _one_link(tmp_path, {"buddy": None}, {"buddy": "B"}, link, direction="to_garmin")
    assert (g.buddy, d.buddy) == ("B", "B")
    g, d, _ = _one_link(tmp_path, {"buddy": "A"}, {"buddy": ""}, link)
    assert (g.buddy, d.buddy) == ("A", "A")
    g, d, res = _one_link(tmp_path, {"buddy": "A"}, {"buddy": "B"}, link)
    assert (g.buddy, d.buddy) == ("A", "B") and not res["updated_on_garmin"] and not res["updated_on_divelogs"]


def test_policy_prefer_source_mirrors_when_source_has_data(tmp_path):
    """prefer_source (added 2026-09-22 for the default tanks link): unlike
    prefer_non_empty, a non-empty source always overwrites a non-empty
    target (fixes the real bug where a target's stale tank data survived
    forever); unlike source_wins, an empty source never blanks a target
    that already has real data."""
    link = FieldLink(id="buddy", source=["garmin.buddy"], target="divelogs.buddy", conflict="prefer_source")
    # source has data, target has different data -> source overwrites (the fix)
    g, d, res = _one_link(tmp_path, {"buddy": "A"}, {"buddy": "B"}, link)
    assert (g.buddy, d.buddy) == ("A", "A") and len(res["updated_on_divelogs"]) == 1
    # source empty, target has data -> target kept; on a run towards Garmin the source is filled from it
    g, d, res = _one_link(tmp_path, {"buddy": None}, {"buddy": "B"}, link, direction="to_garmin")
    assert (g.buddy, d.buddy) == ("B", "B") and len(res["updated_on_garmin"]) == 1 and not res["updated_on_divelogs"]
    # both empty -> no-op
    g, d, res = _one_link(tmp_path, {"buddy": None}, {"buddy": None}, link)
    assert not res["updated_on_garmin"] and not res["updated_on_divelogs"]

    # one-way (to_target) case, as the default tanks link actually uses:
    # source non-empty always mirrors, even onto a non-empty target
    one_way = FieldLink(id="tanks", source=["garmin.tanks"], target="divelogs.tanks", direction="to_target",
                        conflict="prefer_source")
    g, d = _pair({"gas_mixtures": [GasMixture(oxygen=32.0, start_pressure=200.0)]},
                {"gas_mixtures": [GasMixture(oxygen=21.0, start_pressure=150.0)]})
    engine = _engine(tmp_path, [g], [d], field_links=[one_way])
    res = engine.run_sync(dry_run=False)
    assert [t.oxygen for t in d.gas_mixtures] == [32.0] and len(res["updated_on_divelogs"]) == 1
    # source empty (manual Garmin dive, no gas API data) -> target's real tanks are never wiped
    g2, d2 = _pair({"gas_mixtures": []}, {"gas_mixtures": [GasMixture(oxygen=21.0, start_pressure=150.0)]})
    engine2 = _engine(tmp_path, [g2], [d2], field_links=[one_way])
    res2 = engine2.run_sync(dry_run=False)
    assert [t.oxygen for t in d2.gas_mixtures] == [21.0] and not res2["updated_on_divelogs"]


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
    # Same link on a run towards Garmin: Divelogs is the origin regardless of policy name
    g, d, res = _one_link(tmp_path, {"buddy": "A"}, {"buddy": "B"}, link, direction="to_garmin")
    assert (g.buddy, d.buddy) == ("B", "B") and len(res["updated_on_garmin"]) == 1
    # A one-way link with a fill-only policy leaves an existing value alone
    link = FieldLink(id="buddy", source=["divelogs.buddy"], target="garmin.buddy", direction="to_target", conflict="prefer_non_empty")
    g, d, _ = _one_link(tmp_path, {"buddy": "A"}, {"buddy": "B"}, link, direction="to_garmin")
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


def test_create_on_garmin_off_by_default_skips_new_garmin_dives(tmp_path):
    """rework.md C16: a dive found only on Divelogs (or any other source) is
    not created on Garmin unless create_on_garmin is on for the pair."""
    d = [_dive(external_ids={"divelogs": "2"}, buddy="A")]
    engine = _engine(tmp_path, [], d, directionality="to_garmin")  # create_on_garmin defaults to off

    res = engine.run_sync(dry_run=False)

    assert res["uploaded_to_garmin"] == []
    assert engine.source.added == []
    assert res["skipped"] == [{"reason": "create_on_garmin_off", "time": str(d[0].date_time)}]


def test_create_on_garmin_on_allows_new_garmin_dives(tmp_path):
    d = [_dive(external_ids={"divelogs": "2"}, buddy="A")]
    engine = _engine(tmp_path, [], d, create_on_garmin=True, directionality="to_garmin")

    res = engine.run_sync(dry_run=False)

    assert len(res["uploaded_to_garmin"]) == 1
    assert len(engine.source.added) == 1


def test_create_on_garmin_does_not_affect_uploads_to_divelogs(tmp_path):
    """The switch is Garmin-specific: a dive found only on Garmin still
    uploads to Divelogs with create_on_garmin left off."""
    g = [_dive(external_ids={"garmin": "1"}, buddy="A")]
    engine = _engine(tmp_path, g, [])  # create_on_garmin defaults to off

    res = engine.run_sync(dry_run=False)

    assert len(res["uploaded_to_divelogs"]) == 1
    assert res["skipped"] == []


def test_create_on_garmin_does_not_affect_matched_dive_updates(tmp_path):
    """Only new-dive creation is gated - an already-matched pair's field
    links still apply with create_on_garmin off."""
    g = [_dive(external_ids={"garmin": "1"}, buddy=None)]
    d = [_dive(external_ids={"divelogs": "1"}, buddy="A")]
    engine = _engine(tmp_path, g, d, directionality="to_garmin")  # create_on_garmin defaults to off

    res = engine.run_sync(dry_run=False)

    assert res["skipped"] == []
    assert len(res["updated_on_garmin"]) == 1
    assert engine.source.updated[0][1].buddy == "A"


def test_pre_sync_backup_snapshots_what_was_fetched(tmp_path):
    """rework.md C14: a JSON snapshot of the fetched dives per side, written
    before any write, next to the state file."""
    g, d = _pair({"buddy": "A"}, {"buddy": "A"})
    engine = _engine(tmp_path, [g], [d])
    engine.run_sync(dry_run=False)

    backups_root = os.path.join(os.path.dirname(engine.state_file), "backups")
    runs = os.listdir(backups_root)
    assert len(runs) == 1
    with open(os.path.join(backups_root, runs[0], "garmin.json")) as f:
        garmin_backup = json.load(f)
    with open(os.path.join(backups_root, runs[0], "divelogs.json")) as f:
        divelogs_backup = json.load(f)
    assert len(garmin_backup) == 1 and garmin_backup[0]["buddy"] == "A"
    assert len(divelogs_backup) == 1


def test_pre_sync_backup_skipped_on_dry_run(tmp_path):
    g, d = _pair({"buddy": "A"}, {"buddy": "A"})
    engine = _engine(tmp_path, [g], [d])
    engine.run_sync(dry_run=True)

    backups_root = os.path.join(os.path.dirname(engine.state_file), "backups")
    assert not os.path.exists(backups_root)


def test_pre_sync_backup_prunes_to_the_retention_count(tmp_path):
    g, d = _pair({"buddy": "A"}, {"buddy": "A"})
    engine = _engine(tmp_path, [g], [d], backup_retention_count=2)
    for _ in range(4):
        engine.run_sync(dry_run=False)

    backups_root = os.path.join(os.path.dirname(engine.state_file), "backups")
    assert len(os.listdir(backups_root)) == 2


def test_pre_sync_backup_failure_does_not_abort_the_sync(tmp_path, monkeypatch):
    """The try/except inside _write_pre_sync_backup itself must swallow a
    real write failure (disk full, permissions, ...) rather than the sync
    it's meant to protect failing along with it."""
    g, d = _pair({"buddy": "A"}, {"buddy": "A"})
    engine = _engine(tmp_path, [g], [d])
    import src.core.sync_engine as sync_engine_module
    monkeypatch.setattr(sync_engine_module.os, "makedirs", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))

    res = engine.run_sync(dry_run=False)

    assert res["matched_count"] == 1


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


def test_grace_window_override_changes_matching_outcome(tmp_path):
    """rework.md C15 "still open" item: a per-pair grace_window_minutes
    override (SyncPairModel -> pairs.py::engine_for -> run_sync's
    grace_window_override, exercised structurally in test_pairs.py) must
    actually change tier-3 matching, not just get threaded through as an
    inert number. Two dives 20 minutes apart: a 10-minute window misses,
    a 30-minute window catches them - proven via the same run_sync()
    keyword argument a real pair's override arrives through, not by
    presetting settings.grace_window_minutes directly."""
    g = [_dive(date_time=datetime(2026, 6, 22, 12, 0), external_ids={"garmin": "1"})]
    d = [_dive(date_time=datetime(2026, 6, 22, 12, 20), external_ids={"divelogs": "2"})]

    engine = _engine(tmp_path, g, d, grace_window_minutes=60)  # base default irrelevant; override wins
    narrow = engine.run_sync(dry_run=True, grace_window_override=10)
    assert narrow["matched_count"] == 0

    engine2 = _engine(tmp_path, g, d, grace_window_minutes=60)
    wide = engine2.run_sync(dry_run=True, grace_window_override=30)
    assert wide["matched_count"] == 1


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
    engine = _engine(tmp_path, g, d, create_on_garmin=True)
    assert engine.source_id == "garmin" and engine.target_id == "divelogs"
    assert engine.garmin is engine.source and engine.divelogs is engine.target
    res = engine.run_sync(dry_run=False)
    assert res["source"] == "garmin" and res["target"] == "divelogs"
    up = res["uploaded_to_divelogs"][0]
    assert up["garmin_id"] == "1" and up["source_id"] == "1" and up["new_divelogs_id"] == "new-1" and up["new_id"] == "new-1"
    assert res["uploaded_to_garmin"] == []       # one receiver per run (rework.md G0)
    # the other direction is its own run
    back = tmp_path / "back"
    back.mkdir()
    engine = _engine(back, g, d, create_on_garmin=True, directionality="to_garmin")
    res = engine.run_sync(dry_run=False)
    up = res["uploaded_to_garmin"][0]
    assert up["divelogs_id"] == "2" and up["new_garmin_id"] == "new-1"
    assert res["uploaded_to_divelogs"] == []


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
    # rework.md G1: a pair's board and direction live on its sync_pairs entry
    # (the top-level keys describe the garmin_divelogs pair only)
    pair = SyncPairModel(id="d2u", source="divelogs", target="uddf:x.uddf", directionality="to_uddf", field_links=links)
    path = _settings_file(tmp_path, sync_pairs=[pair])
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
    engine = _engine(tmp_path, [g], [d], field_links=[SITE_LINK], directionality="to_garmin")
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


SITE_REVERSIBLE_LINK = FieldLink(id="site_to_garmin", source=["divelogs.location", "divelogs.divesite"],
                                 target="garmin.activityName", direction="bidirectional", conflict="target_wins",
                                 template="{divelogs.divesite} ({divelogs.location})",
                                 reverse=r"(?P<divesite>.+) \((?P<location>.+)\)")


def test_composite_reverse_parses_edited_target_back_into_sources(tmp_path):
    g, d = _pair({"service_fields": {"activityName": "Zenobia (Larnaca)"}},
                 {"service_fields": {"location": "Larnaca", "divesite": "Zenobia"}})
    engine = _engine(tmp_path, [g], [d], field_links=[SITE_REVERSIBLE_LINK])
    res = engine.run_sync(dry_run=False)
    assert not res["updated_on_divelogs"] and not res["updated_on_garmin"]  # already in agreement

    # the diver renames the Garmin activity to something the pattern still describes
    g.service_fields["activityName"] = "Wreck Alpha (Malmo)"
    res = engine.run_sync(dry_run=False)
    assert d.service_fields["location"] == "Malmo" and d.service_fields["divesite"] == "Wreck Alpha"
    assert res["updated_on_divelogs"]

    # a rename that no longer fits the pattern can't be guessed at -> left alone
    g.service_fields["activityName"] = "totally different"
    res = engine.run_sync(dry_run=False)
    assert d.service_fields["location"] == "Malmo" and d.service_fields["divesite"] == "Wreck Alpha"
    assert not res["updated_on_divelogs"]


def test_upload_renders_composite_and_service_links(tmp_path):
    d = _dive(external_ids={"divelogs": "2"}, dive_number=7, location="Larnaca, Zenobia",
              service_fields={"location": "Larnaca", "divesite": "Zenobia"},
              gas_mixtures=[GasMixture(oxygen=32.0, tank_name="left")])
    board = [l for l in default_field_links() if l.id != "activity_name"] + [SITE_LINK]
    engine = _engine(tmp_path, [], [d], field_links=board, create_on_garmin=True, directionality="to_garmin")
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


def test_deletion_propagates_on_full_sync_when_enabled(tmp_path):
    """rework.md C13: a dive gone from one side on a full sync is deleted on
    the other when propagate_deletes is on for the pair."""
    g = [_dive(external_ids={"garmin": "1"}, buddy="A")]
    d = [_dive(external_ids={"divelogs": "2"}, buddy="A")]
    engine = _engine(tmp_path, g, d)
    engine.run_sync(dry_run=False)
    assert engine.load_links() == {"1": "2"}

    # Next full sync: Garmin no longer has dive "1" (deleted there)
    engine2 = _engine(tmp_path, [], d, propagate_deletes=True)
    res = engine2.run_sync(dry_run=False)

    assert engine2.target.deleted == ["2"]
    assert res["deleted_on_divelogs"] == [{"id": "2", "gone_from": "garmin"}]
    assert engine2.load_links() == {}


def test_deletion_only_logged_when_propagate_deletes_off(tmp_path):
    """When off, the deleted-from-garmin dive is untouched on Divelogs; the
    stale link stays (a run towards Garmin would then re-create it there,
    see the receiver test below)."""
    g = [_dive(external_ids={"garmin": "1"}, buddy="A")]
    d = [_dive(external_ids={"divelogs": "2"}, buddy="A")]
    engine = _engine(tmp_path, g, d)
    engine.run_sync(dry_run=False)

    engine2 = _engine(tmp_path, [], d)  # propagate_deletes defaults to off
    res = engine2.run_sync(dry_run=False)

    assert engine2.target.deleted == []
    assert res["deleted_on_divelogs"] == []
    assert engine2.load_links() == {"1": "2"}  # stale link kept


def test_deletion_follows_the_direction(tmp_path):
    """rework.md G0: deletes only ever happen on the run's receiver. A dive
    gone from the sender is removed on the receiver; a dive gone from the
    receiver is left alone by this run (the opposite run would remove it
    from the sender) and the sender's copy falls through to the normal
    unmatched-dive upload, exactly as with propagate_deletes off."""
    g = [_dive(external_ids={"garmin": "1"}, buddy="A")]
    d = [_dive(external_ids={"divelogs": "2"}, buddy="A")]
    engine = _engine(tmp_path, g, d)
    engine.run_sync(dry_run=False)

    # Run towards Divelogs, dive gone from Divelogs (the receiver): nothing deleted on Garmin
    engine2 = _engine(tmp_path, g, [], propagate_deletes=True)
    res = engine2.run_sync(dry_run=False)
    assert engine2.source.deleted == [] and engine2.target.deleted == []
    assert res["deleted_on_garmin"] == [] and res["deleted_on_divelogs"] == []
    assert len(res["uploaded_to_divelogs"]) == 1          # the surviving Garmin dive is re-sent
    assert engine2.load_links() == {"1": "new-1"}

    # Run towards Garmin, same situation: Divelogs is now the sender, so Garmin's copy goes
    engine3 = _engine(tmp_path, g, [], propagate_deletes=True, directionality="to_garmin")
    res = engine3.run_sync(dry_run=False)
    assert engine3.source.deleted == ["1"] and engine3.target.deleted == []
    assert res["deleted_on_garmin"] == [{"id": "1", "gone_from": "divelogs"}]
    assert engine3.load_links() == {}

    # Run towards Garmin, dive gone from Garmin (the receiver): left alone, re-created from Divelogs
    engine4 = _engine(tmp_path, g, d)
    engine4.run_sync(dry_run=False)
    engine5 = _engine(tmp_path, [], d, propagate_deletes=True, create_on_garmin=True, directionality="to_garmin")
    res = engine5.run_sync(dry_run=False)
    assert engine5.source.deleted == [] and engine5.target.deleted == []
    assert res["deleted_on_divelogs"] == []
    assert len(res["uploaded_to_garmin"]) == 1
    assert engine5.load_links() == {"1": "2", "new-1": "2"}  # stale link kept, plus the fresh re-upload


def test_deletion_not_checked_on_incremental_sync(tmp_path):
    """An incremental fetch's date window cannot tell 'deleted' apart from
    'outside this run', so it must not be treated as either even with
    propagate_deletes on - same natural re-upload as the off case."""
    g = [_dive(external_ids={"garmin": "1"}, buddy="A")]
    d = [_dive(external_ids={"divelogs": "2"}, buddy="A")]
    engine = _engine(tmp_path, g, d)
    engine.run_sync(dry_run=False)

    engine2 = _engine(tmp_path, [], d, propagate_deletes=True, create_on_garmin=True, directionality="to_garmin",
                      sync_filters=SyncFilters(only_new=True))
    res = engine2.run_sync(dry_run=False)

    assert engine2.target.deleted == []
    assert res["deleted_on_divelogs"] == []
    assert len(res["uploaded_to_garmin"]) == 1
    assert engine2.load_links() == {"1": "2", "new-1": "2"}


def test_deletion_dry_run_reports_without_deleting(tmp_path):
    g = [_dive(external_ids={"garmin": "1"}, buddy="A")]
    d = [_dive(external_ids={"divelogs": "2"}, buddy="A")]
    engine = _engine(tmp_path, g, d)
    engine.run_sync(dry_run=False)

    engine2 = _engine(tmp_path, [], d, propagate_deletes=True)
    res = engine2.run_sync(dry_run=True)

    assert engine2.target.deleted == []
    assert res["deleted_on_divelogs"] == [{"id": "2", "gone_from": "garmin", "dry_run": True}]
    assert engine2.load_links() == {"1": "2"}  # dry run touches no state


def test_deletion_gone_on_both_sides_just_drops_the_stale_link(tmp_path):
    g = [_dive(external_ids={"garmin": "1"}, buddy="A")]
    d = [_dive(external_ids={"divelogs": "2"}, buddy="A")]
    engine = _engine(tmp_path, g, d)
    engine.run_sync(dry_run=False)

    engine2 = _engine(tmp_path, [], [], propagate_deletes=True)
    res = engine2.run_sync(dry_run=False)

    assert engine2.source.deleted == [] and engine2.target.deleted == []
    assert res["deleted_on_divelogs"] == [] and res["deleted_on_garmin"] == []
    assert engine2.load_links() == {}


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


def test_matched_dive_takes_the_garmin_tank_sensor_name(tmp_path):
    """A Garmin tank with a transmitter carries its name (tankSensors[].name)
    to Divelogs on a matched dive; a Garmin tank without one keeps the name
    Divelogs already has."""
    from src.core.services.garmin import GarminAdapter
    garmin = GarminAdapter.__new__(GarminAdapter)
    summary = {"activityId": 1, "startTimeLocal": "2026-06-27T11:24:00.0", "duration": 3000, "maxDepth": 20.0}
    details = {"activityId": 1, "diveInfo": {"diveGases": [{"gasIndex": 0, "oxygenContent": 32, "heliumContent": 0,
                                                             "tankStartingPressure": 193.0, "tankEndingPressure": 96.0}]}}
    sensor = {"tankSensors": [{"tankIndex": 0, "name": "Micke01", "pressureUnit": "BAR",
                               "startingPressure": 192.91, "endingPressure": 95.76}]}
    mapped = garmin._map_to_unified(summary, details, None, sensor)
    assert [g.tank_name for g in mapped.gas_mixtures] == ["Micke01"]

    tank = mapped.gas_mixtures[0]
    g, d = _pair({"gas_mixtures": [tank]},
                 {"gas_mixtures": [tank.model_copy(update={"tank_name": None})]})
    res = _engine(tmp_path, [g], [d]).run_sync(dry_run=False)
    assert d.gas_mixtures[0].tank_name == "Micke01" and len(res["updated_on_divelogs"]) == 1
    assert not _engine(tmp_path, [g], [d]).run_sync(dry_run=False)["updated_on_divelogs"]   # settled

    # no transmitter on Garmin: the name on Divelogs is neither a difference nor blanked
    unnamed = tank.model_copy(update={"tank_name": None})
    g, d = _pair({"gas_mixtures": [unnamed]}, {"gas_mixtures": [tank.model_copy(update={"tank_name": "Main"})]})
    assert not _engine(tmp_path, [g], [d]).run_sync(dry_run=False)["updated_on_divelogs"]
    g.gas_mixtures[0] = unnamed.model_copy(update={"end_pressure": 50.0})
    _engine(tmp_path, [g], [d]).run_sync(dry_run=False)
    assert d.gas_mixtures[0].end_pressure == 50.0 and d.gas_mixtures[0].tank_name == "Main"


def test_mirror_makes_the_receiver_a_copy_of_the_sender(tmp_path):
    """Mirror: every dive compared (even with only_new on), missing dives
    created, fields forced to the sender's value, and receiver dives the
    sender does not have deleted. Without it nothing is deleted."""
    from src.core.fields import SyncRule
    from src.core.config import SyncPairModel
    rules = {"divelogs": [SyncRule(id="buddy", target="divelogs.buddy", source=["garmin.buddy"], conflict="prefer_non_empty"),
                          SyncRule(id="notes", target="divelogs.notes", source=["garmin.notes"], conflict="target_wins")]}
    pair = SyncPairModel(id="garmin_divelogs", source="garmin", target="divelogs", directionality="to_divelogs", rules=rules)

    def run(mirror, dry_run=False):
        g = [_dive(external_ids={"garmin": "1"}, buddy="Anna", notes="from garmin"),
             _dive(date_time=datetime(2026, 7, 1, 9), external_ids={"garmin": "2"})]
        d = [_dive(external_ids={"divelogs": "10"}, buddy="Bob", notes="mine"),
             _dive(date_time=datetime(2025, 1, 1, 9), external_ids={"divelogs": "11"})]
        engine = _engine(tmp_path, g, d, sync_pairs=[pair], sync_filters=SyncFilters(only_new=True))
        return engine, d, engine.run_sync(dry_run=dry_run, mirror_override=mirror)

    engine, d, res = run(mirror=False)
    assert engine.target.deleted == [] and res["deleted_on_divelogs"] == []
    assert d[0].buddy == "Bob"                                     # prefer_non_empty keeps the receiver's value

    engine, d, res = run(mirror=True, dry_run=True)
    assert engine.target.deleted == [] and res["deleted_on_divelogs"][0]["dry_run"] is True

    engine, d, res = run(mirror=True)
    assert engine.target.deleted == ["11"]                         # Divelogs-only dive removed
    assert [e["id"] for e in res["deleted_on_divelogs"]] == ["11"]
    assert d[0].buddy == "Anna" and d[0].notes == "mine"           # forced; "never overwrite" left alone
    assert len(engine.target.added) == 1                           # the Garmin-only dive created
    assert engine.source.deleted == []                             # the sender is never touched
