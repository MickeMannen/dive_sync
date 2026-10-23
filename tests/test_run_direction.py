"""rework.md G0: a sync run has exactly one receiver.

``bidirectional`` is no longer a run mode - a two-way sync is two directed
runs - so settings, pairs, cron jobs and profiles that still say it are
rewritten on the way in, the engine only ever writes one side (deletes
included), and the CLI refuses the old word with a pointer to the new way.
"""
import json
import logging
import os
import subprocess
import sys

import pytest

from src.core.config import (ConfigManager, CronJobModel, SettingsModel, SyncFilters, SyncPairModel,
                             drop_retired_services, import_profile, migrate_run_directions)
from tests.test_link_engine import FakeDivelogs, FakeGarmin, _dive, _engine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- migration

def test_migrate_run_directions_rewrites_every_holder():
    data = {
        "directionality": "bidirectional",
        "sync_pairs": [{"id": "g2s", "directionality": "bidirectional"}, {"id": "keep", "directionality": "to_source"}],
        "cron_jobs": [{"id": "nightly", "directionality": "bidirectional"},
                      {"id": "paired", "pair": "g2s", "directionality": "bidirectional"},
                      {"id": "fine", "directionality": "to_garmin"}],
    }
    changes = migrate_run_directions(data)
    assert data["directionality"] == "to_divelogs"                 # the implicit pair: Garmin sends
    assert data["sync_pairs"][0]["directionality"] == "to_target"  # a named pair: its source sends
    assert data["sync_pairs"][1]["directionality"] == "to_source"
    assert data["cron_jobs"][0]["directionality"] == "to_divelogs" # no pair = the implicit pair
    assert data["cron_jobs"][1]["directionality"] is None          # names a pair = that pair's direction
    assert data["cron_jobs"][2]["directionality"] == "to_garmin"
    assert len(changes) == 4 and all("bidirectional" in c for c in changes)
    assert migrate_run_directions(data) == []                      # idempotent


def test_models_never_hold_bidirectional():
    assert SettingsModel().directionality == "to_divelogs"
    assert SettingsModel(directionality="bidirectional").directionality == "to_divelogs"
    assert SyncPairModel(id="p").directionality == "to_target"
    assert SyncPairModel(id="p", directionality="bidirectional").directionality == "to_target"
    assert CronJobModel(id="j").directionality is None
    assert CronJobModel(id="j", directionality="bidirectional").directionality == "to_divelogs"
    assert CronJobModel(id="j", pair="p", directionality="bidirectional").directionality is None
    # anything already directed is left exactly as written
    assert SettingsModel(directionality="to_garmin").directionality == "to_garmin"
    assert SyncPairModel(id="p", directionality="to_subsurface").directionality == "to_subsurface"


def test_load_settings_migrates_once_and_saves_back(tmp_path, caplog):
    path = str(tmp_path / "settings.json")
    with open(path, "w") as f:
        json.dump({"directionality": "bidirectional", "field_links": [],
                   "sync_pairs": [{"id": "g2s", "source": "garmin", "target": "subsurface-cloud",
                                   "directionality": "bidirectional"}],
                   "cron_jobs": [{"id": "j", "pair": "g2s", "directionality": "bidirectional"}]}, f)

    with caplog.at_level(logging.WARNING, logger="dive_sync.config"):
        settings = ConfigManager.load_settings(path)
    assert settings.directionality == "to_divelogs"
    assert next(p for p in settings.sync_pairs if p.id == "g2s").directionality == "to_target"
    assert settings.cron_jobs[0].directionality is None
    warnings = [r for r in caplog.records if "bidirectional" in r.getMessage()]
    assert len(warnings) == 1 and "two directed runs" in warnings[0].getMessage()

    # written back, so the next load is silent
    on_disk = json.load(open(path))
    by_id = {p["id"]: p for p in on_disk["sync_pairs"]}
    assert "directionality" not in on_disk                                  # folded into the garmin_divelogs pair (G1)
    assert by_id["garmin_divelogs"]["directionality"] == "to_divelogs" and by_id["g2s"]["directionality"] == "to_target"
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="dive_sync.config"):
        ConfigManager.load_settings(path)
    assert not [r for r in caplog.records if "bidirectional" in r.getMessage()]


def test_v1_profile_with_bidirectional_imports_as_a_directed_run():
    new, summary = import_profile({"dive_sync_profile": 1, "directionality": "bidirectional",
                                   "sync_pairs": [{"id": "p", "directionality": "bidirectional"}]}, SettingsModel())
    assert new.directionality == "to_divelogs"
    assert next(p for p in new.sync_pairs if p.id == "p").directionality == "to_target"


# ---------------------------------------------------------------- retired services (Submersion)
#
# plans/subsurface-submersion.md F18: Submersion support was removed. A file
# an older build wrote still loads; its Submersion pairs and the jobs that
# ran them are dropped (said once, saved back), its other Submersion keys
# ignored, the same way 'bidirectional' is migrated above.

def _old_submersion_settings():
    return {
        "settings_version": 2,
        "create_device_dives_on_submersion": False,
        "sync_pairs": [
            {"id": "garmin_divelogs", "source": "garmin", "target": "divelogs", "directionality": "to_divelogs",
             "create_device_dives_on_submersion": None},
            {"id": "garmin_submersion", "source": "garmin", "target": "submersion", "directionality": "to_target",
             "create_device_dives_on_submersion": True,
             "rules": {"submersion": [{"id": "notes", "target": "submersion.notes", "source": ["garmin.notes"]}]}},
            {"id": "sub_to_ss", "source": "Submersion", "target": "subsurface-cloud", "directionality": "to_source"},
            {"id": "g2s", "source": "garmin", "target": "subsurface-cloud", "directionality": "to_target"},
        ],
        "cron_jobs": [
            {"id": "nightly", "frequency": "daily", "hour": 3},
            {"id": "to-submersion", "pair": "garmin_submersion"},
            {"id": "by-specs", "source": "submersion", "target": "garmin", "directionality": "to_garmin"},
            {"id": "to-subsurface", "pair": "g2s"},
        ],
    }


def test_drop_retired_services_removes_submersion_pairs_and_their_jobs():
    data = _old_submersion_settings()
    changes = drop_retired_services(data)
    assert [p["id"] for p in data["sync_pairs"]] == ["garmin_divelogs", "g2s"]
    assert [j["id"] for j in data["cron_jobs"]] == ["nightly", "to-subsurface"]
    assert len(changes) == 4 and any("garmin_submersion" in c for c in changes)
    assert drop_retired_services(data) == []                       # idempotent
    assert drop_retired_services({}) == [] and drop_retired_services(None) == []


def test_load_settings_drops_submersion_once_and_saves_back(tmp_path, caplog):
    path = str(tmp_path / "settings.json")
    with open(path, "w") as f:
        json.dump(_old_submersion_settings(), f)
    with caplog.at_level(logging.WARNING, logger="dive_sync.config"):
        settings = ConfigManager.load_settings(path)
    assert [p.id for p in settings.sync_pairs] == ["garmin_divelogs", "g2s"]
    assert [j.id for j in settings.cron_jobs] == ["nightly", "to-subsurface"]
    assert not hasattr(settings, "create_device_dives_on_submersion")
    warnings = [r for r in caplog.records if "Submersion" in r.getMessage()]
    assert len(warnings) == 1 and "no longer supports" in warnings[0].getMessage()
    # written back without any of it, so the next load is silent
    with open(path) as f:
        assert "submersion" not in f.read().lower()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="dive_sync.config"):
        assert ConfigManager.load_settings(path) == settings
    assert not [r for r in caplog.records if "Submersion" in r.getMessage()]


def test_submersion_pairs_are_dropped_from_a_profile_or_payload_too():
    """Not only a file: the model drops them wherever settings are built
    (a profile import, a web settings post)."""
    settings = SettingsModel.model_validate(_old_submersion_settings())
    assert [p.id for p in settings.sync_pairs] == ["garmin_divelogs", "g2s"]
    profile = dict(_old_submersion_settings(), dive_sync_profile=2)
    profile.pop("settings_version")
    new, summary = import_profile(profile, SettingsModel())
    assert [p.id for p in new.sync_pairs] == ["garmin_divelogs", "g2s"]
    assert [j.id for j in new.cron_jobs] == ["nightly", "to-subsurface"]
    assert summary.ignored_keys == ["create_device_dives_on_submersion"]


# ---------------------------------------------------------------- engine

def test_writable_sides_is_exactly_one_side(tmp_path, caplog):
    engine = _engine(tmp_path, [], [])
    assert engine.writable_sides() == {"divelogs"} and engine.receiver_id() == "divelogs"
    engine.direction = "to_garmin"
    assert engine.writable_sides() == {"garmin"} and engine.receiver_id() == "garmin"
    engine.direction = "to_source"
    assert engine.writable_sides() == {"garmin"}
    engine.direction = "to_target"
    assert engine.writable_sides() == {"divelogs"}
    with caplog.at_level(logging.WARNING):
        engine.direction = "bidirectional"   # only reachable by poking the engine directly
        assert engine.writable_sides() == set() and engine.receiver_id() is None
    assert any("no longer a run mode" in r.getMessage() for r in caplog.records)


def test_a_run_writes_one_side_only(tmp_path):
    """Both sides have something the other lacks; one run fills one side."""
    g = [_dive(external_ids={"garmin": "1"}, buddy="Anna", notes=None)]
    d = [_dive(external_ids={"divelogs": "2"}, buddy=None, notes="deep")]
    engine = _engine(tmp_path, g, d)
    res = engine.run_sync(dry_run=False)
    assert res["directionality"] == "to_divelogs"
    assert len(res["updated_on_divelogs"]) == 1 and res["updated_on_garmin"] == []
    assert (d[0].buddy, g[0].notes) == ("Anna", None)

    res = engine.run_sync(dry_run=False, direction_override="to_garmin")
    assert len(res["updated_on_garmin"]) == 1 and res["updated_on_divelogs"] == []
    assert g[0].notes == "deep"


def test_ad_hoc_pair_defaults_to_writing_its_target(tmp_path, monkeypatch):
    """`--source garmin --target uddf:x` with no saved pair for those two
    services runs a transient pair that writes its target; the
    garmin_divelogs pair's direction never leaks into it (rework.md G1). A
    saved pair with the same two services is picked up instead."""
    from src.core.config import CredentialsModel, GarminCredentials
    from src.core.pairs import engine_for
    settings_path = str(tmp_path / "settings.json")
    creds_path = str(tmp_path / "credentials.json")
    ConfigManager.save_settings(SettingsModel(sync_filters=SyncFilters(only_new=False), directionality="to_garmin"),
                                settings_path)
    ConfigManager.save_credentials(CredentialsModel(garmin=[GarminCredentials(
        username="u", password="p", token_dir=str(tmp_path / "tokens"))]), creds_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    engine = engine_for("garmin", "uddf:out.uddf", settings_path=settings_path, credentials_path=creds_path)
    assert engine.direction == "to_target" and engine.pair.id == "garmin_uddf" and engine.pair_id is None
    ConfigManager.save_settings(SettingsModel(sync_pairs=[SyncPairModel(
        id="files", source="garmin", target="uddf:other.uddf", directionality="to_garmin")]), settings_path)
    engine = engine_for("garmin", "uddf:out.uddf", settings_path=settings_path, credentials_path=creds_path)
    assert engine.direction == "to_garmin" and engine.pair.id == "files"


# ---------------------------------------------------------------- CLI

def test_cli_refuses_bidirectional(tmp_path):
    env = dict(os.environ, DATA_DIR=str(tmp_path), PYTHONPATH=ROOT)
    res = subprocess.run([sys.executable, os.path.join(ROOT, "sync.py"), "--direction", "bidirectional", "--dry-run"],
                         cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert res.returncode == 2
    assert "no longer a run mode" in res.stderr and "--direction to_garmin" in res.stderr
