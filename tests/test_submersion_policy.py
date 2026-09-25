"""Garmin -> Submersion is metadata only (rework.md F17).

Submersion builds a dive's depth profile, cylinders, tank-pressure series and
gas switches from the ``.fit`` file the diver imports in the app itself. These
tests pin the two consequences for dive_sync: the shipped board carries no
``samples`` / ``tanks`` link (and a hand-written one is refused), and a dive a
dive computer recorded is left for that import to create, while a hand-logged
dive - which has no file to import - is created as before.
"""
from datetime import datetime

from src.core.config import ConfigManager, SettingsModel, SyncFilters, SyncPairModel
from src.core.fields import FieldLink, build_catalog
from src.core.models import UnifiedSample
from src.core.pairs import default_links_for
from src.core.services.divelogs import DivelogsAdapter
from src.core.services.garmin import GarminAdapter
from src.core.services.submersion.adapter import SubmersionAdapter
from src.core.sync_engine import SyncEngine
from src.core.templates import validate_links
from tests.test_link_engine import FakeGarmin, RecordingAdapter, _dive


class FakeSubmersion(RecordingAdapter):
    service_id = "submersion"
    display_name = "Submersion"
    stores_external_ids = True
    _catalog = SubmersionAdapter.field_catalog()


def _engine(tmp_path, garmin_dives, submersion_dives=(), pairs=None, **settings) -> SyncEngine:
    settings.setdefault("sync_filters", SyncFilters(only_new=False))
    if pairs is not None:
        settings["sync_pairs"] = pairs
    path = str(tmp_path / "settings.json")
    ConfigManager.save_settings(SettingsModel(api_cooldown_seconds=0.0, **settings), path)
    return SyncEngine(settings_path=path, credentials_path=str(tmp_path / "credentials.json"),
                      source_adapter=FakeGarmin(list(garmin_dives)),
                      target_adapter=FakeSubmersion(list(submersion_dives)))


def _garmin_dives():
    """One dive a Descent recorded, one entered by hand on Garmin Connect."""
    recorded = _dive(date_time=datetime(2026, 6, 22, 12, 0), external_ids={"garmin": "1"},
                     dive_number=30, device_logged=True)
    hand = _dive(date_time=datetime(2026, 6, 23, 12, 0), external_ids={"garmin": "2"},
                 dive_number=31, device_logged=False)
    return recorded, hand


# ---------------------------------------------------------------- the board

def test_the_shipped_board_leaves_the_profile_and_cylinders_alone():
    ids = {l.id: l for l in default_links_for("garmin", "submersion")}
    assert "samples" not in ids and "tanks" not in ids
    # the two fields the generic board cannot guess: Garmin's location name is
    # the dive site, its activity name is the dive's own name
    assert ids["site"].source == ["garmin.locationName"] and ids["site"].target == "submersion.location"
    assert ids["dive_name"].source == ["garmin.activityName"] and ids["dive_name"].target == "submersion.dive_name"
    # the soft fields are still there, and the dive number is still a match key
    assert {"buddy", "notes", "weight", "visibility", "gps"} <= set(ids)
    assert ids["dive_number"].match_order == 1
    # Submersion on the sending side keeps the same pairing, mirrored
    mirrored = {l.id: l for l in default_links_for("submersion", "garmin")}
    assert mirrored["site"].source == ["submersion.location"] and mirrored["site"].target == "garmin.locationName"
    # every other pair is unaffected
    assert "samples" in {l.id for l in default_links_for("garmin", "divelogs")}


def test_a_rule_writing_submersion_samples_or_tanks_is_refused():
    catalog = build_catalog(GarminAdapter.field_catalog(), SubmersionAdapter.field_catalog())
    problems = validate_links([
        FieldLink(id="samples", source=["garmin.samples"], target="submersion.samples", direction="to_target"),
        FieldLink(id="tanks", source=["garmin.tanks"], target="submersion.tanks", direction="to_target"),
    ], catalog)
    assert len(problems) == 2 and all("cannot be written" in p for p in problems)
    # reading them is still fine - a Submersion dive can feed another service
    to_divelogs = build_catalog(SubmersionAdapter.field_catalog(), DivelogsAdapter.field_catalog())
    assert validate_links([FieldLink(id="samples", source=["submersion.samples"], target="divelogs.samples",
                                     direction="to_target")], to_divelogs) == []


# ---------------------------------------------------------------- new dives

def test_a_recorded_dive_waits_for_the_diver_s_own_fit_import(tmp_path):
    engine = _engine(tmp_path, _garmin_dives())
    result = engine.run_sync()
    uploaded = result["uploaded_to_submersion"]
    assert [e["source_id"] for e in uploaded] == ["2"], "only the hand-logged dive should be created"
    assert [(s["reason"], s["time"]) for s in result["skipped"]] == \
        [("awaits_manual_fit_import", str(datetime(2026, 6, 22, 12, 0)))]
    assert len(engine.target.added) == 1 and engine.target.added[0].external_ids["garmin"] == "2"


def test_the_setting_lets_recorded_dives_through(tmp_path):
    engine = _engine(tmp_path, _garmin_dives(), create_device_dives_on_submersion=True)
    result = engine.run_sync()
    assert sorted(e["source_id"] for e in result["uploaded_to_submersion"]) == ["1", "2"]
    assert result["skipped"] == []


def test_the_pair_can_override_the_setting(tmp_path):
    pair = SyncPairModel(id="garmin_submersion", source="garmin", target="submersion",
                         directionality="to_submersion", create_device_dives_on_submersion=True)
    engine = _engine(tmp_path, _garmin_dives(), pairs=[pair])
    assert sorted(e["source_id"] for e in engine.run_sync()["uploaded_to_submersion"]) == ["1", "2"]


def test_a_dive_with_a_profile_counts_as_recorded_without_the_flag(tmp_path):
    """A sender that does not say (no ``device_logged``) is judged by whether
    the dive carries a profile at all."""
    silent = _dive(date_time=datetime(2026, 6, 24, 12, 0), external_ids={"garmin": "3"},
                   samples=[UnifiedSample(depth=5.0, time=0), UnifiedSample(depth=18.0, time=300)])
    engine = _engine(tmp_path, [silent])
    result = engine.run_sync()
    assert result["uploaded_to_submersion"] == []
    assert [s["reason"] for s in result["skipped"]] == ["awaits_manual_fit_import"]


def test_a_matched_dive_is_updated_whatever_the_setting(tmp_path):
    """The hold is on *creating* dives only: a dive that already exists in
    Submersion gets its soft fields on every run."""
    recorded, _hand = _garmin_dives()
    recorded.buddy = "Anna"
    existing = _dive(date_time=recorded.date_time, dive_number=30,
                     external_ids={"submersion": "sub-1", "garmin": "1"})
    engine = _engine(tmp_path, [recorded], [existing])
    result = engine.run_sync()
    assert result["matched_count"] == 1 and result["skipped"] == []
    assert [external_id for external_id, _ in engine.target.updated] == ["sub-1"]
    assert engine.target.updated[0][1].buddy == "Anna"
