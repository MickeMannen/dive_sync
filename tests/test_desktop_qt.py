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


def test_dive_table_location_column_takes_the_leftover_width(qapp, scratch_data_dir, fake_keyring):
    """The view sizes the elastic column as width - fixedColumnsWidth(), so
    the row spans the window at any size instead of only near the one the
    fixed widths were picked for."""
    from desktop.controllers.dives import (ALL_COLUMNS, COLUMN_WIDTHS, DEFAULT_VISIBLE_COLUMNS,
                                           ELASTIC_MIN_WIDTH, DiveTableModel)
    model = DiveTableModel()
    assert model.isElasticColumn(DEFAULT_VISIBLE_COLUMNS.index("location"))
    assert not model.isElasticColumn(DEFAULT_VISIBLE_COLUMNS.index("date"))
    fixed = model.fixedColumnsWidth()
    assert fixed == sum(COLUMN_WIDTHS[k] for k in DEFAULT_VISIBLE_COLUMNS if k != "location")

    def location_width(row_budget):
        return max(model.elasticMinWidth(), row_budget - model.fixedColumnsWidth())

    # 838 is a row at the 1100 minimum window; 1338 at the 1600 cap. The
    # default columns fill each exactly, with nothing left over to scroll.
    assert fixed + location_width(838) == 838
    assert fixed + location_width(1338) == 1338
    # Squeezed past its minimum it stops shrinking and the table scrolls.
    assert location_width(200) == ELASTIC_MIN_WIDTH == model.elasticMinWidth()

    # Hiding a column hands its width to the location instead of leaving a gap.
    model.set_columns([k for k in DEFAULT_VISIBLE_COLUMNS if k != "buddy"])
    assert model.fixedColumnsWidth() == fixed - COLUMN_WIDTHS["buddy"]
    assert model.fixedColumnsWidth() + location_width(838) == 838
    # With every column on, the fixed part alone overflows the smallest window.
    model.set_columns([k for k, _, _ in ALL_COLUMNS])
    assert model.fixedColumnsWidth() > 838 - ELASTIC_MIN_WIDTH


def test_header_click_sorting_and_per_service_columns(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """Clicking a column heading calls toggleSort(key) with that column."""
    from desktop.controllers.dives import DivesController, ALL_COLUMNS
    from desktop import preferences
    from src.core import dive_cache
    rows = [{"date": "2026-06-22", "time": "10:00:00", "dive_number": 3, "location": "Blue Hole",
             "garmin_id": "g-1", "filename": "a.json"},
            {"date": "2026-06-20", "time": "09:00:00", "dive_number": 1, "location": "Aquarium",
             "garmin_id": "", "filename": "b.json"}]
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: rows)
    monkeypatch.setattr(dive_cache, "list_garmin_dives", lambda *a, **k: rows)

    c = DivesController("divelogs")
    c.load()
    # A column that is not the current one starts ascending...
    c.toggleSort("dive_number")
    assert c.sortKey == "dive_number" and c.sortAscending is True
    assert [c.model.row(i)["dive_number"] for i in range(2)] == [1, 3]
    # ...and clicking the same one again flips it.
    c.toggleSort("dive_number")
    assert c.sortAscending is False
    assert [c.model.row(i)["dive_number"] for i in range(2)] == [3, 1]
    # Dates are the exception: newest first is what you want from a dive log.
    c.toggleSort("date")
    assert c.sortKey == "date" and c.sortAscending is False
    assert [c.model.row(i)["date"] for i in range(2)] == ["2026-06-22", "2026-06-20"]
    assert preferences.get_sort("divelogs", "date") == ("date", False)
    c.toggleSort("")            # no column under the click: nothing happens
    assert c.sortKey == "date" and c.sortAscending is False

    # Divelogs is the only side that stores where a dive came from, and
    # Garmin the only one with a downloadable .fit, so each dialog offers
    # just its own extra.
    divelogs_keys = [col["key"] for col in c.allColumns]
    garmin_keys = [col["key"] for col in DivesController("garmin").allColumns]
    shared = [k for k, _, _ in ALL_COLUMNS]
    # Garmin also has the dive's title (activity name), placed in front of Location
    with_title = shared[:shared.index("location")] + ["activity_name"] + shared[shared.index("location"):]
    assert divelogs_keys == shared + ["garmin_id"] and garmin_keys == with_title + ["fit"]
    assert {"avg_depth", "sac", "tanks", "notes", "id"} <= set(shared)   # more than the original ten
    c.setVisibleColumns(["date", "garmin_id"])
    assert c.visibleColumns == ["date", "garmin_id"]
    assert c.model.data(c.model.index(0, 1)) == "g-1"
    # Ticked in any order, the columns still come out in catalogue order, so
    # switching one off and on again does not send it to the end of the table.
    c.setVisibleColumns(["location", "date", "dive_number"])
    assert c.visibleColumns == ["date", "dive_number", "location"]
    # A Garmin board asked for it falls back rather than showing a blank column.
    g = DivesController("garmin")
    g.setVisibleColumns(["garmin_id"])
    assert "garmin_id" not in g.visibleColumns


def test_dives_controller_loads_cache_and_persists_prefs(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    from desktop.controllers.dives import DivesController
    from src.core import dive_cache
    rows = [{"date": "2026-06-22", "time": "10:00:00", "date_time": "2026-06-22 10:00:00", "dive_number": 1,
             "location": "Reef", "filename": "1.json", "buddy": "A"},
            {"date": "2026-06-23", "time": "11:00:00", "date_time": "2026-06-23 11:00:00", "dive_number": 2,
             "location": "Wreck", "location_name": "Wreck", "filename": "2.json", "buddy": ""}]
    monkeypatch.setattr(dive_cache, "list_garmin_dives", lambda *a, **k: rows)
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
    # Garmin: Save only stages the dive; Save all sends it
    assert c.saveStagesOnly and c.status.startswith("Staged.") and calls == {} and c.pendingCount == 1
    c.saveAll()
    assert wait_until(qapp, lambda: c.status == "Saved."), c.status
    assert calls["filename"] == "2.json" and calls["date_time"] == "2026-06-24 09:00:00"
    assert calls["duration"] == 45 and calls["max_depth"] == 18.5 and calls["dive_number"] == "7"
    assert calls["lat"] is None and calls["lng"] is None and calls["water_temp"] is None
    assert "tanks" not in calls  # garmin: tanksEditable is False, so it's never sent at all

    assert c.tanksEditable is False
    assert c.diveNumberEditable is True   # Garmin's dive number is ours to set


def test_dives_controller_save_passes_gps_water_temp_and_tanks(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    from desktop.controllers.dives import DivesController
    from src.core import dive_cache
    rows = [{"date": "2026-06-22", "time": "10:00:00", "date_time": "2026-06-22 10:00:00", "dive_number": 1,
             "location": "Reef", "filename": "1.json", "buddy": "A"}]
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: rows)
    calls = {}
    monkeypatch.setattr(dive_cache, "update_dive_fields", lambda service, filename, **kw: calls.update(service=service, filename=filename, **kw) or "/tmp/x.json")
    monkeypatch.setattr(dive_cache, "push_remote_update", lambda service, filepath: True)

    c = DivesController("divelogs")
    assert c.tanksEditable is True
    c.load()
    c.select(0)
    c.save({
        "dive_number": "1", "date": "2026-06-22", "time": "10:00:00", "duration": "45", "max_depth": "18.5",
        "location": "Reef", "notes": "", "weight": "", "visibility": "", "buddy": "A",
        "lat": "4.805935", "lng": "103.686585", "water_temp": "28.5",
        "tanks": [{"tank_name": "T1", "oxygen": "32", "helium": "0", "volume": "12", "start_pressure": "200", "end_pressure": "50"}],
    })
    assert wait_until(qapp, lambda: c.status == "Saved."), c.status
    assert calls["lat"] == 4.805935 and calls["lng"] == 103.686585 and calls["water_temp"] == 28.5
    assert calls["tanks"] == [{"tank_name": "T1", "oxygen": 32.0, "helium": 0.0, "volume": 12.0, "start_pressure": 200.0, "end_pressure": 50.0}]
    # Divelogs numbers its own dives from where they fall in date/time order,
    # so the form shows that field read-only and a save never sends one -
    # even when, as here, the payload carries it.
    assert c.diveNumberEditable is False
    assert "dive_number" not in calls


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
        scheduler.last_sync_results = {"Manual": {"matched_count": 1}}
    monkeypatch.setattr(scheduler, "run_sync_thread", fake_run)
    monkeypatch.setattr(scheduler, "is_sync_running", False)
    c = SyncController(log_queue)
    lines = []
    c.logLine.connect(lines.append)
    assert c.pairs[0]["id"] == "" and [d["value"] for d in c.directionsFor("")] == ["to_divelogs", "to_garmin"]
    c.runSync(True, "to_garmin", False, True, "")
    assert c.running is True
    assert wait_until(qapp, lambda: not c.running)
    assert c.status == "Dry run complete." and seen["dry_run"] is True and seen["custom"]["directionality"] == "to_garmin"
    assert any("hello from the sync" in l for l in lines)

    def failing(dry_run, custom_settings=None):
        scheduler.last_sync_results = {"Manual": {"error": "kaboom"}}
    monkeypatch.setattr(scheduler, "run_sync_thread", failing)
    c.runSync(False, "to_divelogs", True, True, "")
    assert wait_until(qapp, lambda: not c.running) and c.status == "Failed: kaboom"


def test_sync_controller_offers_every_configured_service(qapp, scratch_data_dir, fake_keyring, monkeypatch, submersion_enabled):
    """Submersion and Subsurface are selectable without a hand-written pair:
    the page offers each combination of the services that have credentials,
    and those travel to the scheduler as two specs rather than a pair id."""
    from desktop.controllers.sync import SyncController
    from desktop import credentials as creds_store, logging_bridge
    from src.core import scheduler
    from src.core.config import (CredentialsModel, DivelogsCredentials, GarminCredentials,
                                 SubmersionCredentials, SubsurfaceCredentials)
    creds_store.save_credentials_model(CredentialsModel(
        garmin=[GarminCredentials(username="g@x", password="pw")],
        divelogs=[DivelogsCredentials(username="d", password="pw")],
        subsurface=SubsurfaceCredentials(email="me@x.org", password="pw"),
        submersion=SubmersionCredentials(endpoint_url="s3.eu-central-003.backblazeb2.com", bucket="b",
                                         access_key_id="k", secret_access_key="s"),
    ))
    c = SyncController(logging_bridge.install())
    assert [s["id"] for s in c.services] == ["garmin", "divelogs", "submersion", "subsurface"]

    labels = {p["label"]: p for p in c.pairs}
    assert "Garmin Connect ↔ Divelogs.org" in labels
    assert "Garmin Connect ↔ Submersion" in labels
    assert "Garmin Connect ↔ Subsurface Cloud" in labels
    assert "Divelogs.org ↔ Submersion" in labels
    assert "Submersion ↔ Subsurface Cloud" in labels
    # Garmin/Divelogs keeps the empty id it has always had.
    assert labels["Garmin Connect ↔ Divelogs.org"]["id"] == ""
    assert [d["value"] for d in c.directionsFor("garmin~submersion")] == ["to_submersion", "to_garmin"]
    # Subsurface Cloud's spec differs from its service id.
    assert labels["Garmin Connect ↔ Subsurface Cloud"]["target"] == "subsurface-cloud"

    seen = {}
    monkeypatch.setattr(scheduler, "run_sync_thread",
                        lambda dry_run, custom_settings=None: seen.update(custom=custom_settings))
    monkeypatch.setattr(scheduler, "is_sync_running", False)
    c.runSync(True, "to_submersion", True, True, "garmin~submersion")
    assert wait_until(qapp, lambda: not c.running)
    assert seen["custom"]["source"] == "garmin" and seen["custom"]["target"] == "submersion"
    assert "pair" not in seen["custom"]         # no saved pair to look up

    # The built-in Garmin/Divelogs combination stays the plain default run.
    c.runSync(True, "to_divelogs", True, True, "")
    assert wait_until(qapp, lambda: not c.running)
    assert "source" not in seen["custom"] and "pair" not in seen["custom"]

    # Download with no service named covers every configured one.
    downloads = {}
    monkeypatch.setattr(scheduler, "run_download_thread",
                        lambda overwrite, base_dir=None, include_garmin=True, include_divelogs=True,
                        services=None: downloads.update(services=services, overwrite=overwrite))
    monkeypatch.setattr(scheduler, "is_download_running", False)
    c.download(False, "")
    assert wait_until(qapp, lambda: not c.running)
    assert downloads["services"] == ["garmin", "divelogs", "submersion", "subsurface"]
    c.download(True, "submersion")
    assert wait_until(qapp, lambda: not c.running)
    assert downloads["services"] == ["submersion"] and downloads["overwrite"] is True


# ---------------------------------------------------------------- mapping controller

def test_mapping_controller_split_gesture(qapp, scratch_data_dir, fake_keyring):
    """Dragging one sender text field onto a second receiver field offers a
    split; accepted, it lands on the default activity_name composite as
    reverse "auto" and shows in the Divelogs panel, where it runs."""
    from desktop.controllers.mapping import MappingController
    m = MappingController()
    asked = []
    m.askSplit.connect(lambda *args: asked.append(args))
    assert m.createRule("garmin.activityName", "divelogs", "divelogs.location")      # a plain rule: no question yet
    assert asked == []
    assert m.createRule("garmin.activityName", "divelogs", "divelogs.divesite")
    assert len(asked) == 1 and "Split Activity name into Location + Dive site" in asked[0][0]
    assert "Replaces the rule on Dive site" in asked[0][0]
    assert m.makeSplit(*asked[0][1:])
    garmin = next(p for p in m.receivers if p["receiver"] == "garmin")
    composite = next(r for r in garmin["rules"] if r["id"] == "activity_name")
    assert composite["reverse"] == "auto" and composite["reverse_conflict"] == "prefer_source"
    divelogs = next(p for p in m.receivers if p["receiver"] == "divelogs")
    assert not any(r["target"] in ("divelogs.location", "divelogs.divesite") for r in divelogs["rules"])
    location = next(f for f in divelogs["fields"] if f["key"] == "divelogs.location")
    assert location["split_id"] == "activity_name" and location["split_summary"].startswith("⇠ split of Activity name")
    assert [s["target"] for s in divelogs["splits"]] == ["garmin.activityName"]
    selected = m.selectedRule
    assert selected["split_active"] and selected["reverse_custom"] == ""
    assert selected["title"].startswith("Activity name → Location + Dive site")
    assert "Location = \"" in m.preview
    # unticking the split in the editor removes the reverse pattern again
    assert m.updateRule({"split": False, "reverse": "", "reverse_conflict": ""}) == ""
    assert m.selectedRule["reverse"] is None
    assert not next(p for p in m.receivers if p["receiver"] == "divelogs")["splits"]


def test_mapping_controller_board_operations(qapp, scratch_data_dir, fake_keyring):
    """rework.md G6: the desktop board is two receiver panels over the pair's rules."""
    from desktop.controllers.mapping import MappingController
    from src.core.config import ConfigManager
    m = MappingController()
    assert m.pairId == "garmin_divelogs" and m.sourceName == "Garmin Connect" and not m.dirty
    panels = m.receivers
    assert [p["receiver"] for p in panels] == ["divelogs", "garmin"]        # the run's receiver (to_divelogs) first
    assert panels[0]["active"] is True and panels[0]["sender"] == "garmin"
    assert {p["receiver"]: len(p["rules"]) for p in panels} == {"divelogs": 8, "garmin": 7}
    buddy = next(f for f in panels[0]["fields"] if f["key"] == "divelogs.buddy")
    assert buddy["linked"] and buddy["rule_id"] == "buddy" and buddy["rule_summary"] == "← Buddy [manual]"
    assert any(f["linked"] for f in panels[0]["sender_fields"])
    m.setViewDirection("to_garmin")
    assert [p["receiver"] for p in m.receivers] == ["garmin", "divelogs"]
    m.setViewDirection("")

    assert m.canDrop("garmin.buddy", "divelogs", "divelogs.max_depth").startswith("Cannot take")
    assert m.canDrop("divelogs.buddy", "divelogs", "divelogs.notes") == "Drag a field from the left-hand list onto a field on the right."
    assert m.canDrop("garmin.buddy", "divelogs", "divelogs.dive_number").startswith("This field is locked")
    asked = []
    m.askSplit.connect(lambda *args: asked.append(args))
    assert m.createRule("garmin.notes", "divelogs", "divelogs.divesite")          # Notes already feeds Notes: a split?
    assert asked and asked[0][1:] == ("garmin.notes", "divelogs", "divelogs.divesite")
    assert m.createPlainRule(*asked[0][1:])                                       # "No": composite onto the 'site' rule
    site = next(r for r in m.receivers[0]["rules"] if r["id"] == "site")
    assert site["source"] == ["garmin.locationName", "garmin.notes"] and "{garmin.notes}" in site["template"]
    assert m.selectedId == "site" and m.selectedReceiver == "divelogs" and m.preview and m.preview != "–" and m.dirty
    assert m.selectedRule["composite"] is True and m.selectedRule["text_target"] is True
    assert m.createRule("divelogs.temp_min", "garmin", "garmin.temp_min")          # a plain rule on the other receiver
    assert next(r for r in m.receivers[1]["rules"] if r["id"] == "temp_min")["conflict"] == "prefer_non_empty"
    # the composite now reads garmin.locationName, which Garmin's own 'site' rule fills from divelogs.divesite:
    # a loop the validator refuses on save until that rule goes
    assert "feeds back" in m.save()
    m.deleteRule("garmin", "site")
    m.selectRule("divelogs", "buddy")
    assert m.updateRule({"id": "buddy", "conflict": "target_wins", "separator": ", ", "template": "", "reverse": ""}) == ""
    assert next(f for f in m.receivers[0]["fields"] if f["key"] == "divelogs.buddy")["rule_noop"] is True
    assert m.updateRule({"id": "site"}) != ""      # duplicate id refused
    m.selectRule("divelogs", "site")
    assert m.updateRule({"reverse": "(?P<locationName>.+) / (?P<notes>.+)", "reverse_conflict": "source_wins"}) == ""
    assert m.selectedRule["reverse_conflict"] == "source_wins"
    assert m.addMatchKey("garmin.dive_number", "divelogs.dive_number") == ""
    assert m.addMatchKey("garmin.buddy", "divelogs.buddy") != ""
    assert m.matchKeys == [["garmin.dive_number", "divelogs.dive_number"]] and "dive_number" in m.matchKeyLabel
    asked = []
    m.askApplyToAll.connect(lambda: asked.append(True))
    assert m.save() == "" and not m.dirty and asked == [True]
    saved = ConfigManager.load_settings().default_pair()
    assert {r: len(v) for r, v in saved.rules.items()} == {"divelogs": 8, "garmin": 7}
    assert next(r for r in saved.rules["divelogs"] if r.id == "buddy").conflict == "target_wins"
    assert next(r for r in saved.rules["divelogs"] if r.id == "site").reverse_conflict == "source_wins"
    assert saved.match_keys == [["garmin.dive_number", "divelogs.dive_number"]]
    m.deleteRule("divelogs", "notes")
    assert m.dirty
    m.cancel()
    assert len(m.receivers[0]["rules"]) == 8 and not m.dirty
    m.removeMatchKey(0)
    assert m.matchKeys == [] and m.dirty
    m.resetToDefaults()
    assert len(m.receivers[1]["rules"]) == 7 and m.dirty
    m.savePairOptions("to_garmin", 30, True, True)
    assert m.pairDirection == "to_garmin" and m.pairGrace == 30
    assert m.pairPropagateDeletes is True and m.pairCreateOnGarmin is True
    assert [p["receiver"] for p in m.receivers] == ["garmin", "divelogs"]
    m.applyToAll()
    state = json.load(open(scratch_data_dir / "sync_state.json"))
    assert state["full_compare_once"] is True


def test_mapping_controller_click_to_connect(qapp, scratch_data_dir, fake_keyring):
    """Clicking a sender field then a receiver field makes the same rule a
    drag would (2026-09-23): with 36 Submersion fields a drag routinely spans
    more than the viewport and neither board scrolls mid-drag."""
    from desktop.controllers.mapping import MappingController
    m = MappingController()
    assert m.armedKey == ""

    m.armSource("divelogs", "garmin.temp_min")
    assert m.armedKey == "garmin.temp_min" and m.armedLabel == "Min temperature"
    assert "armed" in m.message
    sender = next(f for f in m.receivers[0]["sender_fields"] if f["key"] == "garmin.temp_min")
    assert sender["armed"] is True

    assert m.connectArmed("divelogs", "divelogs.temp_min") is True
    assert m.armedKey == ""
    rule = next(r for r in m.receivers[0]["rules"] if r["target"] == "divelogs.temp_min")
    assert rule["source"] == ["garmin.temp_min"] and m.selectedId == rule["id"]

    # clicking the same field twice disarms; Escape does too
    m.armSource("divelogs", "garmin.buddy")
    m.armSource("divelogs", "garmin.buddy")
    assert m.armedKey == "" and m.clearArmed() is False
    m.armSource("divelogs", "garmin.buddy")
    assert m.clearArmed() is True and m.armedKey == ""

    # a click on the wrong panel is ignored rather than making a nonsense rule
    m.armSource("divelogs", "garmin.notes")
    assert m.connectArmed("garmin", "garmin.activityName") is False
    assert m.armedKey == "garmin.notes"

    # a refused pairing reports why and leaves the board alone
    before = len(m.receivers[0]["rules"])
    m.armSource("divelogs", "garmin.buddy")
    assert m.connectArmed("divelogs", "divelogs.max_depth") is False
    assert "Cannot take" in m.message and len(m.receivers[0]["rules"]) == before

    # switching pair clears the arming
    m.armSource("divelogs", "garmin.buddy")
    m.selectPair("garmin_divelogs")
    assert m.armedKey == ""


# ---------------------------------------------------------------- settings controller

def test_settings_controller_saves_to_keychain_and_handles_profiles(qapp, scratch_data_dir, fake_keyring):
    from desktop.controllers.settings import SettingsController
    from src.core.config import ConfigManager
    import desktop.credentials as creds_store
    s = SettingsController()
    assert s.hasCredentials is False
    # Every card's status line says something from the start, so testing or
    # saving rewrites a line already on screen instead of adding one.
    assert s.garminStatus == s.divelogsStatus == s.subsurfaceStatus == "No account saved yet."
    assert s.submersionStatus == "Not configured yet."
    s.saveGarminAccounts([{"username": "g@x", "password": "pw", "token_dir": ""}])
    assert s.garminStatus == "Saved: g@x. Press Test to check the login."
    s.saveDivelogsAccounts([{"username": "d", "password": "pw2"}])
    s.save("me@x.org", "pw3", "s3", "https://s3.example.com", "eu-central-1", "my-bucket", "submersion-sync/", "keyid", "secret", False, "", "hunter2")
    assert s.hasCredentials and s.garminAccounts == [
        {"username": "g@x", "token_dir": creds_store.DEFAULT_GARMIN_TOKEN_DIR, "has_password": True}]
    assert s.divelogsAccounts == [{"username": "d", "has_password": True}]
    assert s.subsurfaceEmail == "me@x.org" and s.message == "Saved to keychain."
    assert s.submersionBucket == "my-bucket"
    assert json.loads(fake_keyring.store[("DiveSync", "submersion_secret")])["secret_access_key"] == "secret"
    assert creds_store.load_credentials_model().submersion.passphrase == "hunter2"
    # keeping a blank password/passphrase keeps the stored ones
    s.saveGarminAccounts([{"username": "g@x", "password": "", "token_dir": ""}])
    s.save("me@x.org", "", "s3", "https://s3.example.com", "eu-central-1", "my-bucket", "submersion-sync/", "keyid", "", False, "", "")
    assert creds_store.load_credentials_model().get_garmin_accounts()[0].password == "pw"
    assert json.loads(fake_keyring.store[("DiveSync", "submersion_secret")])["secret_access_key"] == "secret"
    assert creds_store.load_credentials_model().submersion.passphrase == "hunter2"
    # Each card saves on its own, the way the account lists always have.
    s.saveSubsurface("other@x.org", "")
    assert s.subsurfaceEmail == "other@x.org"
    assert creds_store.load_credentials_model().subsurface.password == "pw3"
    s.saveSubmersion("s3", "s3.eu-central-003.backblazeb2.com", "", "other-bucket", "", "keyid", "", False, "", "")
    assert s.submersionBucket == "other-bucket" and s.submersionEndpointUrl == "https://s3.eu-central-003.backblazeb2.com"
    assert creds_store.load_credentials_model().submersion.passphrase == "hunter2"
    assert s.submersionStatus == ("Saved: bucket 'other-bucket' at https://s3.eu-central-003.backblazeb2.com. "
                                  "Press Test to check it.")
    # Saving a store whose only 'override' repeats the endpoint's own region
    # leaves nothing overridden, so the Advanced section stays folded away.
    assert s.submersionRegionIsAuto is True
    s.saveSubmersion("s3", "s3.eu-central-003.backblazeb2.com", "eu-central-003", "other-bucket", "", "keyid", "",
                     False, "", "")
    assert s.submersionRegion == "" and s.submersionRegionIsAuto is True
    s.saveSubmersion("s3", "s3.eu-central-003.backblazeb2.com", "somewhere-else", "other-bucket", "", "keyid", "",
                     False, "", "")
    assert s.submersionRegion == "somewhere-else" and s.submersionRegionIsAuto is False
    s.saveSubmersion("s3", "s3.eu-central-003.backblazeb2.com", "", "other-bucket", "", "keyid", "", False, "", "")
    # A username saved with no password at all (nothing stored to fall back
    # on) is a half-saved account: the settings page has to be able to show
    # that, or it only surfaces as a login failure on the next sync.
    s.saveDivelogsAccounts([{"username": "d", "password": "pw2"}, {"username": "nopw", "password": ""}])
    assert s.divelogsAccounts == [{"username": "d", "has_password": True},
                                  {"username": "nopw", "has_password": False}]
    assert "No password stored for nopw" in s.divelogsStatus
    s.saveDivelogsAccounts([{"username": "d", "password": "pw2"}])

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
    monkeypatch.setattr(dive_cache, "list_garmin_dives", lambda *a, **k: [])
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: [])
    warnings = []
    controllers = desktop_app.build_controllers(logging_bridge.install())
    # Hooked through create_engine so loading main.qml is covered too; a
    # connect on the returned engine happens after the load and misses those.
    engine = desktop_app.create_engine(
        controllers, "Settings",
        on_warnings=lambda errs: warnings.extend(str(e.toString()) for e in errs))
    assert engine.rootObjects(), "main.qml did not load"
    root = engine.rootObjects()[0]
    wait(qapp, 300)
    sections = root.property("sections").toVariant()   # QJSValue -> list
    assert root.property("currentSection") == sections.index("Settings")   # Settings is the first-run landing page
    assert sections[-1] == "About"
    assert "Submersion Dives" not in sections and "Subsurface Dives" in sections   # Submersion sync is disabled
    # walk every section so each page instantiates
    for index in range(len(sections)):
        root.setProperty("currentSection", index)
        wait(qapp, 150)
    assert warnings == [], warnings
    # The window never opens larger than the screen it launches on, and never
    # shrinks below the size the pages are laid out for.
    available = desktop_app.available_screen_size()
    assert root.property("minimumWidth") == min(1100, available["width"])
    assert root.property("minimumHeight") == min(750, available["height"])
    assert root.property("width") >= root.property("minimumWidth")
    assert root.property("height") >= root.property("minimumHeight")
    if available["width"]:
        assert root.property("width") <= available["width"]
        assert root.property("height") <= available["height"]
    engine.deleteLater()
    wait(qapp, 50)


# ---------------------------------------------------------------- D7: quit cleanup

def test_cleanup_on_quit_syncs_garmin_token_and_clears_materialized_files(scratch_data_dir, fake_keyring, monkeypatch):
    """desktop/app.py::cleanup_on_quit is wired to QGuiApplication.aboutToQuit
    (see main()), which Qt fires on Cmd+Q / the Quit menu as well as a normal
    window close - unlike Toga, whose shell had no such hook at all. This
    exercises the cleanup logic itself (the same materialize/sync/clear
    primitives Track D's begin_operation/end_operation already covers);
    only the literal "choose Quit from the real macOS menu" step is still a
    manual, hands-on check (rework.md D7)."""
    import keyring
    from desktop import app as desktop_app
    from desktop import credentials as creds_store
    from src.core.config import CredentialsModel, GarminCredentials, CREDENTIALS_FILE
    from src.core.services.garmin import safe_token_filename

    creds_file = CREDENTIALS_FILE  # already pointed at scratch_data_dir by the fixture
    token_dir = str(scratch_data_dir / "tokens" / "garmin")
    creds_store.save_credentials_model(
        CredentialsModel(garmin=GarminCredentials(username="diver1", password="secret1", token_dir=token_dir))
    )
    keyring.set_password(creds_store.SERVICE_NAME, creds_store._garmin_token_key("diver1"), '{"cached": true}')

    # Simulate begin_operation() having materialized the token file, then
    # garth refreshing it during the (now-finished) sync.
    creds_store.materialize_local_cache()
    creds_store.materialize_garmin_token("diver1", token_dir)
    token_path = os.path.join(token_dir, safe_token_filename("diver1"))
    assert os.path.exists(creds_file) and os.path.exists(token_path)
    with open(token_path, "w") as f:
        f.write('{"cached": true, "refreshed": true}')

    desktop_app.cleanup_on_quit()

    assert not os.path.exists(creds_file), "materialized credentials.json must not outlive the app"
    assert not os.path.exists(token_path), "materialized Garmin token file must not outlive the app"
    assert (
        keyring.get_password(creds_store.SERVICE_NAME, creds_store._garmin_token_key("diver1"))
        == '{"cached": true, "refreshed": true}'
    ), "the refreshed token must be synced back to the keychain before the file is cleared"


def test_cleanup_on_quit_still_clears_the_token_file_when_the_keychain_sync_fails(
    scratch_data_dir, fake_keyring, monkeypatch
):
    """Found by hand on 2026-09-22 (rework.md D7): a real macOS keychain
    permission re-prompt (a fresh ad-hoc-signed build re-authorizing) made
    sync_garmin_token_from_file's keyring.set_password raise, and because it
    shared a try block with the clear step right after it, the exception
    silently skipped clearing the file too - leaving a real plaintext
    Garmin token on disk with no error logged anywhere. A stale/unsynced
    token forcing a fresh login next time is an acceptable trade against
    that; the file must still be removed even when the sync fails."""
    from desktop import app as desktop_app
    from desktop import credentials as creds_store
    from src.core.config import CredentialsModel, GarminCredentials
    from src.core.services.garmin import safe_token_filename

    token_dir = str(scratch_data_dir / "tokens" / "garmin")
    creds_store.save_credentials_model(
        CredentialsModel(garmin=GarminCredentials(username="diver1", password="secret1", token_dir=token_dir))
    )
    creds_store.materialize_local_cache()
    os.makedirs(token_dir, exist_ok=True)
    token_path = os.path.join(token_dir, safe_token_filename("diver1"))
    with open(token_path, "w") as f:
        f.write('{"cached": true}')
    assert os.path.exists(token_path)

    monkeypatch.setattr(
        creds_store, "sync_garmin_token_from_file",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("keychain access denied")),
    )

    desktop_app.cleanup_on_quit()

    assert not os.path.exists(token_path), "the token file must be removed even when syncing it back to the keychain failed"


def test_dives_controller_fit_marks_and_unchanged_coordinates(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    from desktop.controllers.dives import DivesController
    from src.core import dive_cache
    rows = [{"filename": "1.json", "fit_file": "1.fit", "manual": False},
            {"filename": "2.json", "fit_file": "", "manual": False},
            {"filename": "3.json", "fit_file": "", "manual": True}]
    monkeypatch.setattr(dive_cache, "list_dives", lambda service: [dict(r) for r in rows])
    c = DivesController("garmin")
    assert [r["fit"] for r in c._list_dives()] == ["✓", "✗", "M"]      # M: a hand-logged dive
    assert c.splitSiteNames and not DivesController("divelogs").splitSiteNames
    # the form shows 6 decimals; saving that text back leaves the coordinate alone
    assert DivesController._changed_coord("4.7948", 4.794800067320466) is None
    assert DivesController._changed_coord("4.8", 4.794800067320466) == 4.8
    assert DivesController._changed_coord("", 4.79) is None and DivesController._changed_coord("1.5", None) == 1.5


def test_dives_controller_stages_edits_and_saves_them_together(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """Edits wait in the controller until Save (one dive) or Save all; a dive
    whose upload fails stays pending, and Undo drops a dive's edits."""
    from desktop.controllers.dives import DivesController
    from src.core import dive_cache
    rows = [{"date": "2026-06-22", "time": "10:00:00", "date_time": "2026-06-22 10:00:00", "dive_number": 1,
             "location": "Reef", "activity_name": "Reef", "location_name": "", "filename": "1.json", "buddy": "A"},
            {"date": "2026-06-23", "time": "11:00:00", "date_time": "2026-06-23 11:00:00", "dive_number": 2,
             "location": "Wreck", "activity_name": "Wreck", "location_name": "", "filename": "2.json", "buddy": ""}]
    monkeypatch.setattr(dive_cache, "list_garmin_dives", lambda *a, **k: [dict(r) for r in rows])
    written, pushed = [], []
    monkeypatch.setattr(dive_cache, "update_dive_fields", lambda service, filename, **kw: written.append((filename, kw)) or filename)
    monkeypatch.setattr(dive_cache, "push_remote_update", lambda service, filepath: pushed.append(filepath) or filepath != "2.json")
    form = lambda **kw: dict({"date": "2026-06-22", "time": "10:00:00", "duration": "", "max_depth": "", "notes": "",
                              "weight": "", "visibility": "", "buddy": "", "lat": "", "lng": "", "water_temp": ""}, **kw)
    c = DivesController("garmin")
    c.load()
    c.stage("1.json", form(activity_name="Gozo, Blue Hole", location_name="Blue Hole", buddy="Anna"))
    c.stage("2.json", form(activity_name="Wreck", buddy="Bo"))
    c.stage("missing.json", form())                                   # not listed: ignored
    assert c.pendingCount == 2 and sorted(c.pendingFiles) == ["1.json", "2.json"] and written == []
    c.select(c.rowOf("1.json"))
    assert c.selected["pending"] is True and c.selected["buddy"] == "Anna"
    # Garmin: Location is the location name, the title is its own column
    assert c.selected["location"] == "Blue Hole" and c.selected["activity_name"] == "Gozo, Blue Hole"
    cell = lambda col: c.model.data(c.model.index(c.rowOf("1.json"), c.model.columns.index(col)))
    c.setVisibleColumns(["date", "activity_name", "location"])
    assert (cell("activity_name"), cell("location")) == ("Gozo, Blue Hole", "Blue Hole")

    c.saveAll()
    assert wait_until(qapp, lambda: not c.busy and pushed), c.status
    assert [f for f, _ in written] == ["1.json", "2.json"] and written[0][1]["location_name"] == "Blue Hole"
    assert c.pendingFiles == ["2.json"] and "1 of 2" in c.status        # the failed upload stays pending
    assert c.selected["filename"] == "1.json"                         # the selection survives the reload

    c.discard("2.json")
    assert c.pendingCount == 0
    c.saveDive("1.json")
    assert c.status == "Nothing to save for this dive."


def test_dive_table_shows_staged_values(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """A dive with unsaved edits shows them in the table, formatted like the
    stored values; Undo brings the stored ones back."""
    from PySide6.QtCore import Qt
    from desktop.controllers.dives import DivesController
    from src.core import dive_cache
    rows = [{"date": "2026-06-22", "time": "10:00:00", "date_time": "2026-06-22 10:00:00", "dive_number": 1,
             "location": "Reef", "activity_name": "Reef", "location_name": "", "filename": "1.json", "buddy": "A",
             "water_temp": "", "tanks": ""}]
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: [dict(r) for r in rows])
    c = DivesController("divelogs")
    c.setVisibleColumns(["date", "location", "buddy", "water_temp", "tanks"])
    c.load()
    cell = lambda col: c.model.data(c.model.index(0, c.visibleColumns.index(col)), Qt.DisplayRole)
    c.stage("1.json", {"date": "2026-06-22", "location": "Gozo, Blue Hole", "buddy": "Anna", "water_temp": "18",
                       "tanks": [{"tank_name": "Micke01", "oxygen": "32", "helium": "0", "volume": "12",
                                  "start_pressure": "200", "end_pressure": "50"}]})
    assert cell("location") == "Gozo, Blue Hole" and cell("buddy") == "Anna"
    assert cell("water_temp") == dive_cache._format_water_temp(18.0, 18.0, 18.0)
    assert cell("tanks").startswith("Micke01: 32% O2")
    c.discard("1.json")
    assert cell("location") == "Reef" and cell("buddy") == "A"


def test_quitting_after_discarding_unsaved_dives_is_not_asked_again(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """Discard in the unsaved-dives dialog quits, and quitting closes the
    window again: that second close must go through, not reopen the dialog."""
    from PySide6.QtCore import QObject
    from desktop import app as desktop_app
    from desktop import logging_bridge
    from src.core import dive_cache
    rows = [{"date": "2026-06-22", "time": "10:00:00", "date_time": "2026-06-22 10:00:00", "filename": "1.json"}]
    monkeypatch.setattr(dive_cache, "list_dives", lambda service, *a, **k: [dict(r) for r in rows] if service == "garmin" else [])
    monkeypatch.setattr(dive_cache, "get_samples", lambda *a, **k: [])
    controllers = desktop_app.build_controllers(logging_bridge.install())
    engine = desktop_app.create_engine(controllers, "Garmin Dives")
    root = engine.rootObjects()[0]
    root.show()
    wait(qapp, 200)
    controllers["garminDives"].stage("1.json", {"buddy": "x"})
    root.close()
    wait(qapp, 200)
    assert root.isVisible() and root.findChild(QObject, "leaveDivesDialog").property("opened")
    root.setProperty("discardConfirmed", True)      # what the dialog's Discard sets before quitting
    root.close()
    wait(qapp, 200)
    assert not root.isVisible()


def test_sync_controller_source_and_target(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """The Sync page picks a Source and a Target: the run writes the target,
    and the pair behind the two is found whichever way round it was defined."""
    from desktop.controllers.sync import SyncController
    from desktop import credentials as creds_store, logging_bridge
    from src.core import scheduler
    from src.core.config import (ConfigManager, CredentialsModel, DivelogsCredentials, GarminCredentials,
                                 SettingsModel, SubsurfaceCredentials, SyncPairModel)
    creds_store.save_credentials_model(CredentialsModel(
        garmin=[GarminCredentials(username="g@x", password="pw")],
        divelogs=[DivelogsCredentials(username="d", password="pw")],
        subsurface=SubsurfaceCredentials(email="me@x.org", password="pw"),
    ))
    settings = SettingsModel()
    settings.sync_pairs.append(SyncPairModel(id="d2u", source="divelogs", target="uddf:export.uddf"))
    ConfigManager.save_settings(settings)
    c = SyncController(logging_bridge.install())
    specs = [e["spec"] for e in c.endpoints]
    assert specs == ["garmin", "divelogs", "subsurface-cloud", "uddf:export.uddf"]
    assert [e["spec"] for e in c.targetsFor("garmin")] == ["divelogs", "subsurface-cloud", "uddf:export.uddf"]
    assert "subsurface-cloud" not in [e["spec"] for e in c.targetsFor("subsurface-cloud")]

    seen = {}
    monkeypatch.setattr(scheduler, "run_sync_thread", lambda dry_run, custom_settings=None: seen.update(custom=custom_settings))
    monkeypatch.setattr(scheduler, "is_sync_running", False)
    run = lambda s, t: (c.runSyncBetween(True, s, t, True, True), wait_until(qapp, lambda: not c.running))
    run("garmin", "divelogs")                           # the classic default run
    assert seen["custom"]["directionality"] == "to_divelogs" and "source" not in seen["custom"]
    run("divelogs", "garmin")                           # same pair, the other way
    assert seen["custom"]["directionality"] == "to_garmin"
    run("subsurface-cloud", "garmin")                   # the combination is defined garmin -> subsurface
    assert seen["custom"]["directionality"] == "to_garmin"
    assert (seen["custom"]["source"], seen["custom"]["target"]) == ("garmin", "subsurface-cloud")
    run("uddf:export.uddf", "divelogs")                 # a saved pair goes by its id
    assert seen["custom"]["pair"] == "d2u" and seen["custom"]["directionality"] == "to_divelogs"
    seen.clear()
    c.runSyncBetween(True, "garmin", "garmin", True, True)
    assert seen == {} and c.status == "Pick two different services to sync."


def test_mapping_controller_source_and_target_view(qapp, scratch_data_dir, fake_keyring):
    """The Mapping page picks Source and Target: one panel, the rules for
    writing the target; the saved direction for scheduled runs stays put."""
    from desktop.controllers.mapping import MappingController
    from src.core.config import ConfigManager, SettingsModel, SyncPairModel
    settings = SettingsModel()
    settings.sync_pairs.append(SyncPairModel(id="d2u", source="divelogs", target="uddf:export.uddf"))
    ConfigManager.save_settings(settings)
    m = MappingController()
    assert [e["id"] for e in m.endpoints] == ["garmin", "divelogs", "uddf"]
    assert [e["id"] for e in m.targetsFor("divelogs")] == ["garmin", "uddf"]
    assert [e["id"] for e in m.targetsFor("garmin")] == ["divelogs"]
    assert (m.viewSource, m.viewTarget) == ("garmin", "divelogs")      # the saved to_divelogs
    assert [p["receiver"] for p in m.panels] == ["divelogs"] and m.panels[0]["sender"] == "garmin"

    assert m.selectView("divelogs", "garmin")                          # swap: the other direction
    assert [p["receiver"] for p in m.panels] == ["garmin"] and m.savedDirection == "to_divelogs"
    assert m.selectView("uddf", "divelogs") and m.pairId == "d2u"      # the pair is found either way round
    assert [p["receiver"] for p in m.panels] == ["divelogs"] and m.savedDirection == "to_uddf"
    assert not m.selectView("garmin", "uddf") and m.pairId == "d2u"    # no board joins those two
    m.savePairOptions("to_uddf", 10, False, False)
    assert m.viewTarget == "divelogs"                                   # saving options does not move the view


def test_mapping_board_for_configured_services_without_a_saved_pair(qapp, scratch_data_dir, fake_keyring):
    """Subsurface Cloud set up but no pair saved for it: the board still
    offers Garmin/Divelogs -> Subsurface with the shipped defaults, and saving
    adds that pair to settings (the pair the Sync page then runs)."""
    from desktop.controllers.mapping import MappingController
    from desktop import credentials as creds_store
    from src.core.config import (ConfigManager, CredentialsModel, DivelogsCredentials, GarminCredentials,
                                 SubsurfaceCredentials)
    creds_store.save_credentials_model(CredentialsModel(
        garmin=[GarminCredentials(username="g@x", password="pw")],
        divelogs=[DivelogsCredentials(username="d", password="pw")],
        subsurface=SubsurfaceCredentials(email="me@x.org", password="pw")))
    m = MappingController()
    assert [e["id"] for e in m.endpoints] == ["garmin", "divelogs", "subsurface"]
    assert [e["id"] for e in m.targetsFor("subsurface")] == ["garmin", "divelogs"]
    assert m.selectView("garmin", "subsurface") and m.pairId == "garmin_subsurface"
    assert m.panels[0]["receiver"] == "subsurface" and m.panels[0]["rules"]          # the shipped defaults
    assert not any(p.id == "garmin_subsurface" for p in ConfigManager.load_settings().sync_pairs)

    assert m.save() == ""
    saved = next(p for p in ConfigManager.load_settings().sync_pairs if p.id == "garmin_subsurface")
    assert (saved.source, saved.target, saved.directionality) == ("garmin", "subsurface-cloud", "to_subsurface")
    assert saved.rules and not m.dirty
    m.reload()
    assert [p["id"] for p in m.pairs].count("garmin_subsurface") == 1        # now listed once, as a saved pair


def test_dives_controller_stages_deletions_until_save_all(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """Delete only marks a dive; Save all changes deletes it online first and
    then from the cache. A failed online delete keeps it marked and cached."""
    from desktop.controllers.dives import DivesController
    from src.core import dive_cache
    rows = [{"date": "2026-06-22", "time": "10:00:00", "date_time": "2026-06-22 10:00:00", "filename": "a.json", "location": "Reef"},
            {"date": "2026-06-23", "time": "10:00:00", "date_time": "2026-06-23 10:00:00", "filename": "b.json", "location": "Wreck"}]
    listed = [dict(r) for r in rows]
    monkeypatch.setattr(dive_cache, "list_dives", lambda service: [dict(r) for r in listed])
    calls = []
    remote_ok = {"value": False}
    monkeypatch.setattr(dive_cache, "dive_external_id", lambda service, filename: (f"/x/{filename}", "id-" + filename))
    monkeypatch.setattr(dive_cache, "push_remote_delete",
                        lambda service, external_id, filepath=None: calls.append(("remote", external_id)) or remote_ok["value"])
    def local_delete(service, filename):
        calls.append(("local", filename))
        listed[:] = [r for r in listed if r["filename"] != filename]
        return f"/x/{filename}", "id-" + filename
    monkeypatch.setattr(dive_cache, "delete_dive_local", local_delete)

    c = DivesController("subsurface")
    c.load()
    c.select(c.rowOf("a.json"))
    c.deleteSelected()
    assert calls == [] and c.deletingFiles == ["a.json"] and c.pendingCount == 1
    assert "Save all changes deletes it" in c.status
    c.discard("a.json")                                              # Undo unmarks it
    assert c.deletingFiles == [] and c.pendingCount == 0

    c.select(c.rowOf("a.json"))
    c.deleteSelected()
    c.saveAll()                                                      # the online delete fails
    assert wait_until(qapp, lambda: not c.busy and calls)
    assert calls == [("remote", "id-a.json")] and c.deletingFiles == ["a.json"]   # still cached and marked
    remote_ok["value"] = True
    calls.clear()
    c.saveAll()
    assert wait_until(qapp, lambda: not c.busy and len(calls) == 2)
    assert calls == [("remote", "id-a.json"), ("local", "a.json")]
    assert c.deletingFiles == [] and c.pendingCount == 0 and c.rowOf("a.json") == -1 and c.status == "Deleted."


def test_dives_controller_counts_its_dives(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    from desktop.controllers.dives import DivesController
    from src.core import dive_cache
    listed = [{"date": "2026-06-22", "time": "10:00:00", "date_time": "2026-06-22 10:00:00", "filename": f"{i}.json"}
              for i in range(3)]
    monkeypatch.setattr(dive_cache, "list_dives", lambda service: [dict(r) for r in listed])
    c = DivesController("subsurface")
    counts = []
    c.diveCountChanged.connect(lambda: counts.append(c.diveCount))
    c.load()
    assert c.diveCount == 3 and counts == [3]
    listed.pop()
    c.load()                                  # e.g. after a refresh or a saved deletion
    assert c.diveCount == 2 and counts == [3, 2]


def test_sync_page_never_deletes_unless_mirroring(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    from desktop.controllers.sync import SyncController
    from desktop import credentials as creds_store, logging_bridge
    from src.core import scheduler
    from src.core.config import CredentialsModel, DivelogsCredentials, GarminCredentials
    creds_store.save_credentials_model(CredentialsModel(
        garmin=[GarminCredentials(username="g@x", password="pw")],
        divelogs=[DivelogsCredentials(username="d", password="pw")]))
    c = SyncController(logging_bridge.install())
    seen = {}
    monkeypatch.setattr(scheduler, "run_sync_thread", lambda dry_run, custom_settings=None: seen.update(custom=custom_settings))
    monkeypatch.setattr(scheduler, "is_sync_running", False)
    c.runSyncBetween(True, "garmin", "divelogs", True, True)
    assert wait_until(qapp, lambda: not c.running)
    assert seen["custom"]["propagate_deletes"] is False and "mirror" not in seen["custom"]
    c.runSyncBetween(True, "garmin", "divelogs", True, True, "", "", True, True)
    assert wait_until(qapp, lambda: not c.running)
    assert seen["custom"]["mirror"] is True and seen["custom"]["only_new"] is False


def test_conflicts_controller_lists_every_pair(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """The Conflicts page shows the conflicts of every pair, grouped, with
    field labels and readable values, and resolves through the right pair."""
    from desktop.controllers.conflicts import ConflictsController, brief
    from desktop import credentials as creds_store
    from src.core.config import CredentialsModel, DivelogsCredentials, GarminCredentials, SubsurfaceCredentials
    from src.core.conflicts import Conflict, ConflictStore
    from src.core.pairs import engine_for
    from src.core.sync_engine import SyncEngine
    creds_store.save_credentials_model(CredentialsModel(
        garmin=[GarminCredentials(username="g@x", password="pw")], divelogs=[DivelogsCredentials(username="d", password="pw")],
        subsurface=SubsurfaceCredentials(email="me@x.org", password="pw")))

    def record(engine, link, src_key, tgt_key, src_val, tgt_val):
        ConflictStore(engine.conflicts_file).save([Conflict(
            id=Conflict.make_id(link, engine.source_id, engine.target_id, "1", "2"), link_id=link,
            source_service=engine.source_id, target_service=engine.target_id, source_external_id="1",
            target_external_id="2", source_key=src_key, target_key=tgt_key, field_type="text",
            dive_ids={engine.source_id: "1", engine.target_id: "2"}, source_value=src_val, target_value=tgt_val,
            dive_time="2026-06-27 09:20:00")])

    class Pair:                                   # what record() needs of an engine
        def __init__(self, source_id, target_id, conflicts_file):
            self.source_id, self.target_id, self.conflicts_file = source_id, target_id, conflicts_file
    import os
    from src.core import config
    base = os.path.dirname(config.SETTINGS_FILE)
    record(Pair("garmin", "divelogs", os.path.join(base, "conflicts.json")), "buddy", "garmin.buddy", "divelogs.buddy", "Anna", "Bob")
    record(Pair("garmin", "subsurface", os.path.join(base, "conflicts_garmin_subsurface.json")),
           "gps", "garmin.gps", "subsurface.gps", [4.7948, 103.683518], [4.805835, 103.686585])
    c = ConflictsController()
    c.load()
    assert [g["label"] for g in c.groups] == ["Garmin Connect ↔ Divelogs.org", "Garmin Connect ↔ Subsurface Cloud"]
    assert c.count == 2
    buddy = c.groups[0]["conflicts"][0]
    assert (buddy["target_label"], buddy["source_text"], buddy["target_text"]) == ("Buddy", "Anna", "Bob")
    assert (buddy["source_name"], buddy["target_name"]) == ("Garmin Connect", "Divelogs.org")
    gps = c.groups[1]["conflicts"][0]
    assert gps["pair_id"] == "garmin_subsurface" and gps["source_text"] == "4.7948, 103.684"
    assert brief(None) == "(empty)" and brief([{"o2": 21}]) == "1 item(s)" and brief(18.2) == "18.2"


def test_about_info(qapp, scratch_data_dir, fake_keyring):
    import os
    from desktop.controllers.about import AboutController
    from src.core import about
    a = AboutController()
    assert a.version == about.app_version() and a.version not in ("", "local-dev")
    assert a.licenseName == "MIT" and "Permission is hereby granted" in a.licenseText
    assert a.projectUrl.startswith("https://") and any(c["name"] == "Python" for c in a.components)
    # the license text the bundle carries is the one in LICENSE
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "LICENSE"), encoding="utf-8") as f:
        assert " ".join(f.read().split()) == " ".join(about._MIT_TEXT.split())
    os.environ["APP_VERSION"] = "v9.9.9"
    try:
        assert about.app_version() == "9.9.9"                      # the Docker image's build version wins
    finally:
        del os.environ["APP_VERSION"]


def test_subsurface_dives_page_specifics(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """Subsurface: the location column is the dive site, there is no
    visibility in metres, and only a hand-logged dive's depths are editable."""
    from PySide6.QtCore import Qt
    from desktop.controllers.dives import DivesController
    from src.core import dive_cache
    rows = [{"date": "2026-06-22", "time": "10:00:00", "date_time": "2026-06-22 10:00:00", "filename": "a.json",
             "location": "Blue Hole", "has_profile": True},
            {"date": "1993-10-10", "time": "11:30:00", "date_time": "1993-10-10 11:30:00", "filename": "b.json",
             "location": "Kullen", "has_profile": False}]
    monkeypatch.setattr(dive_cache, "list_dives", lambda service: [dict(r) for r in rows])
    s = DivesController("subsurface")
    s.load()
    assert "visibility" not in [c["key"] for c in s.allColumns] and "visibility" not in s.visibleColumns
    assert next(c["label"] for c in s.allColumns if c["key"] == "location") == "Dive site"
    assert s.model.headerData(s.visibleColumns.index("location"), Qt.Horizontal) == "Dive site"
    assert s.profileLocksDepths and not s.hasVisibility
    g = DivesController("garmin")
    assert g.hasVisibility and not g.profileLocksDepths
    assert next(c["label"] for c in DivesController("divelogs").allColumns if c["key"] == "location") == "Location, dive site"
