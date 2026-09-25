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
                             import_profile, migrate_run_directions)
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
    assert SyncPairModel(id="p", directionality="to_submersion").directionality == "to_submersion"


def test_load_settings_migrates_once_and_saves_back(tmp_path, caplog):
    path = str(tmp_path / "settings.json")
    with open(path, "w") as f:
        json.dump({"directionality": "bidirectional", "field_links": [],
                   "sync_pairs": [{"id": "g2s", "source": "garmin", "target": "submersion",
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
