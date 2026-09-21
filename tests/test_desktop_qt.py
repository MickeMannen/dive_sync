"""Qt desktop app (rework.md Track D): controllers and the QML front end,
run offscreen. Never touches the real keychain or the real app-data dir."""
import json
import os
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication, QEventLoop, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from tests.test_desktop_credentials import fake_keyring  # noqa: E402,F401  (fixture)


@pytest.fixture(scope="session")
def qapp():
    app = QGuiApplication.instance() or QGuiApplication([])
    yield app


@pytest.fixture
def scratch_data_dir(tmp_path, monkeypatch):
    import desktop.paths as paths
    import desktop.preferences as prefs
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(paths, "data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(prefs, "PREFS_FILE", str(tmp_path / "desktop_prefs.json"))
    import src.core.config as config
    settings_file, creds_file = str(tmp_path / "settings.json"), str(tmp_path / "credentials.json")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config, "CREDENTIALS_FILE", creds_file)
    # the ConfigManager defaults were bound at import time; point them at the scratch dir too
    load_s, save_s = config.ConfigManager.load_settings, config.ConfigManager.save_settings
    load_c, save_c = config.ConfigManager.load_credentials, config.ConfigManager.save_credentials
    monkeypatch.setattr(config.ConfigManager, "load_settings", staticmethod(lambda path=settings_file: load_s(path)))
    monkeypatch.setattr(config.ConfigManager, "save_settings", staticmethod(lambda settings, path=settings_file: save_s(settings, path)))
    monkeypatch.setattr(config.ConfigManager, "load_credentials", staticmethod(lambda path=creds_file: load_c(path)))
    monkeypatch.setattr(config.ConfigManager, "save_credentials", staticmethod(lambda creds, path=creds_file: save_c(creds, path)))
    return tmp_path


def wait(app, ms=50):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_until(app, predicate, timeout_ms=5000):
    for _ in range(timeout_ms // 20):
        if predicate():
            return True
        wait(app, 20)
    return predicate()


# ---------------------------------------------------------------- table model

def test_dive_table_model_columns_and_sorting(qapp, scratch_data_dir, fake_keyring):
    from desktop.controllers.dives import ALL_COLUMNS, DEFAULT_VISIBLE_COLUMNS, DiveTableModel, sort_key
    model = DiveTableModel()
    model.set_rows([{"date": "2026-06-22", "dive_number": 10, "max_depth": "12 m", "filename": "a.json"},
                    {"date": "2026-06-23", "dive_number": 2, "max_depth": "3.5 m", "filename": "b.json"},
                    {"date": "2026-06-21", "dive_number": None, "max_depth": "", "filename": "c.json"}])
    assert model.rowCount() == 3 and model.columnCount() == len(DEFAULT_VISIBLE_COLUMNS)
    assert model.headerData(2, Qt.Horizontal) == "Dive #" and model.columnKey(2) == "dive_number"
    model.sort_rows("dive_number", True)
    assert [r["filename"] for r in (model.row(i) for i in range(3))] == ["b.json", "a.json", "c.json"]  # blanks last
    model.sort_rows("max_depth", False)
    assert model.row(0)["filename"] == "a.json"
    model.set_columns(["date", "buddy", "nope"])
    assert model.columns == ["date", "buddy"] and model.columnCount() == 2
    assert model.data(model.index(0, 0)) == "2026-06-22"
    assert sort_key("12 kg", True) == (0, 12.0) and sort_key(None, False) == (1, "")


def test_dives_controller_loads_cache_and_persists_prefs(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    from desktop.controllers.dives import DivesController
    from src.core import dive_cache
    rows = [{"date": "2026-06-22", "time": "10:00:00", "date_time": "2026-06-22 10:00:00", "dive_number": 1,
             "location": "Reef", "filename": "1.json", "buddy": "A"},
            {"date": "2026-06-23", "time": "11:00:00", "date_time": "2026-06-23 11:00:00", "dive_number": 2,
             "location": "Wreck", "filename": "2.json", "buddy": ""}]
    monkeypatch.setattr(dive_cache, "list_garmin_dives", lambda: rows)
    c = DivesController("garmin")
    c.load()
    assert c.model.rowCount() == 2 and c.selected == {}
    c.select(1)
    assert c.selected["location"] == "Wreck"
    c.setSort("date", False)
    assert c.model.row(0)["filename"] == "2.json" and c.sortAscending is False
    c.setVisibleColumns(["date", "location"])
    assert c.visibleColumns == ["date", "location"]
    # persisted
    c2 = DivesController("garmin")
    assert c2.visibleColumns == ["date", "location"] and c2.sortKey == "date" and c2.sortAscending is False

    # save goes through dive_cache with parsed fields, in a worker thread
    calls = {}
    monkeypatch.setattr(dive_cache, "update_dive_fields", lambda service, filename, **kw: calls.update(service=service, filename=filename, **kw) or "/tmp/x.json")
    monkeypatch.setattr(dive_cache, "push_remote_update", lambda service, filepath: True)
    c.select(0)
    c.save({"dive_number": "7", "date": "2026-06-24", "time": "09:00:00", "duration": "45", "max_depth": "18.5",
            "location": "New", "notes": "n", "weight": "6 kg", "visibility": "10 m", "buddy": "B"})
    assert wait_until(qapp, lambda: c.status == "Saved."), c.status
    assert calls["filename"] == "2.json" and calls["date_time"] == "2026-06-24 09:00:00"
    assert calls["duration"] == 45 and calls["max_depth"] == 18.5 and calls["dive_number"] == "7"


# ---------------------------------------------------------------- sync controller

def test_sync_controller_runs_scheduler_off_thread(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    from desktop.controllers.sync import SyncController
    from desktop import logging_bridge
    from src.core import scheduler
    import logging
    log_queue = logging_bridge.install()
    seen = {}

    def fake_run(dry_run, custom_settings=None):
        seen.update(dry_run=dry_run, custom=custom_settings)
        logging.getLogger("dive_sync.test").info("hello from the sync")
        scheduler.last_sync_results = {"matched_count": 1}
    monkeypatch.setattr(scheduler, "run_sync_thread", fake_run)
    monkeypatch.setattr(scheduler, "is_sync_running", False)
    c = SyncController(log_queue)
    lines = []
    c.logLine.connect(lines.append)
    assert c.pairs[0]["id"] == "" and c.directionsFor("")[1]["value"] == "to_divelogs"
    c.runSync(True, "to_garmin", False, True, False, "")
    assert c.running is True
    assert wait_until(qapp, lambda: not c.running)
    assert c.status == "Dry run complete." and seen["dry_run"] is True and seen["custom"]["directionality"] == "to_garmin"
    assert any("hello from the sync" in l for l in lines)

    def failing(dry_run, custom_settings=None):
        scheduler.last_sync_results = {"error": "kaboom"}
    monkeypatch.setattr(scheduler, "run_sync_thread", failing)
    c.runSync(False, "bidirectional", True, True, False, "")
    assert wait_until(qapp, lambda: not c.running) and c.status == "Failed: kaboom"


# ---------------------------------------------------------------- mapping controller

def test_mapping_controller_board_operations(qapp, scratch_data_dir, fake_keyring):
    from desktop.controllers.mapping import MappingController
    from src.core.config import ConfigManager
    m = MappingController()
    assert m.pairId == "default" and m.sourceName == "Garmin Connect" and len(m.links) == 9 and not m.dirty
    assert len(m.sourceFields) == 18 and any(f["linked"] for f in m.sourceFields)
    assert m.canLink("garmin.buddy", "divelogs.max_depth").startswith("Cannot link")
    assert m.canLink("garmin.buddy", "garmin.notes") == "Link a field with one on the other side."
    assert m.createLink("garmin.notes", "divelogs.divesite")          # composite onto the 'site' link
    site = next(l for l in m.links if l["id"] == "site")
    assert site["source"] == ["garmin.locationName", "garmin.notes"] and "{garmin.notes}" in site["template"]
    assert m.selectedId == "site" and m.preview and m.preview != "–" and m.dirty
    assert m.createLink("garmin.temp_min", "divelogs.temp_min")
    assert next(l for l in m.links if l["id"] == "temp_min")["direction"] == "to_target"
    m.selectLink("buddy")
    assert {d["value"] for d in m.allowedDirections} == {"bidirectional", "to_target", "to_source", "off"}
    assert m.updateLink({"id": "buddy", "direction": "to_target", "conflict": "manual", "match_order": "", "separator": ", ", "template": ""}) == ""
    assert m.updateLink({"id": "site"}) != ""      # duplicate id refused
    asked = []
    m.askApplyToAll.connect(lambda: asked.append(True))
    assert m.save() == "" and not m.dirty and asked == [True]
    saved = ConfigManager.load_settings()
    assert len(saved.field_links) == 10 and next(l for l in saved.field_links if l.id == "buddy").conflict == "manual"
    m.deleteLink("notes")
    assert m.dirty
    m.cancel()
    assert len(m.links) == 10 and not m.dirty
    m.resetToDefaults()
    assert len(m.links) == 9 and m.dirty
    m.savePairOptions("to_divelogs", 30)
    assert m.pairDirection == "to_divelogs" and m.pairGrace == 30
    m.applyToAll()
    state = json.load(open(scratch_data_dir / "sync_state.json"))
    assert state["full_compare_once"] is True


# ---------------------------------------------------------------- settings controller

def test_settings_controller_saves_to_keychain_and_handles_profiles(qapp, scratch_data_dir, fake_keyring):
    from desktop.controllers.settings import SettingsController
    from src.core.config import ConfigManager
    s = SettingsController()
    assert s.hasCredentials is False
    s.save("g@x", "pw", "", "d", "pw2", "me@x.org", "pw3",
           "s3", "https://s3.example.com", "eu-central-1", "my-bucket", "submersion-sync/", "keyid", "secret", False, "")
    assert s.hasCredentials and s.garminUsername == "g@x" and s.subsurfaceEmail == "me@x.org" and s.message == "Saved to keychain."
    assert fake_keyring.store[("DiveSync", "divelogs_password")] == "pw2"
    assert s.submersionBucket == "my-bucket" and fake_keyring.store[("DiveSync", "submersion_secret_access_key")] == "secret"
    # keeping a blank password keeps the stored one
    s.save("g@x", "", "", "d", "", "me@x.org", "",
           "s3", "https://s3.example.com", "eu-central-1", "my-bucket", "submersion-sync/", "keyid", "", False, "")
    assert fake_keyring.store[("DiveSync", "garmin_password")] == "pw"
    assert fake_keyring.store[("DiveSync", "submersion_secret_access_key")] == "secret"

    path = str(scratch_data_dir / "profile.json")
    assert s.exportProfile(path).startswith("Profile written")
    data = json.load(open(path))
    data["grace_window_minutes"] = 42
    json.dump(data, open(path, "w"))
    assert s.checkProfile("file://" + path).startswith("Review")
    assert "grace_window_minutes: 15 -> 42" in s.profileSummary
    assert s.applyProfile() == "Profile applied."
    assert ConfigManager.load_settings().grace_window_minutes == 42
    assert s.applyProfile() == "Check a profile first."
    assert s.checkProfile(str(scratch_data_dir / "missing.json")).startswith("Profile cannot be imported")


# ---------------------------------------------------------------- QML loads

def test_qml_front_end_loads_without_warnings(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    from desktop import app as desktop_app
    from desktop import logging_bridge
    from src.core import dive_cache
    monkeypatch.setattr(dive_cache, "list_garmin_dives", lambda: [])
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda: [])
    warnings = []
    controllers = desktop_app.build_controllers(logging_bridge.install())
    engine = desktop_app.create_engine(controllers, "Settings")
    engine.warnings.connect(lambda errs: warnings.extend(str(e.toString()) for e in errs))
    assert engine.rootObjects(), "main.qml did not load"
    root = engine.rootObjects()[0]
    wait(qapp, 300)
    assert root.property("currentSection") == 5   # Settings is the first-run landing page
    # walk every section so each page instantiates
    for index in range(6):
        root.setProperty("currentSection", index)
        wait(qapp, 150)
    assert warnings == [], warnings
    engine.deleteLater()
    wait(qapp, 50)
