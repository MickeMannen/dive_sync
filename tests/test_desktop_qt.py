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
from PySide6.QtQuick import QQuickItem  # noqa: E402  (imported before any QML object exists, so wrappers get the item type)

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


def test_sorting_by_date_orders_one_days_dives_by_time(qapp):
    from desktop.controllers.dives import DiveTableModel
    model = DiveTableModel(service="divelogs")
    model.set_rows([{"date": "2026-06-22", "time": "09:15:00", "filename": "morning.json"},
                    {"date": "2026-06-23", "time": "08:00:00", "filename": "next-day.json"},
                    {"date": "2026-06-22", "time": "14:40:00", "filename": "afternoon.json"},
                    {"date": "", "time": "10:00:00", "filename": "undated.json"},
                    {"date": "2026-06-22", "time": "11:05:00", "filename": "midday.json"}])

    def order():
        return [model.row(i)["filename"] for i in range(model.rowCount())]

    model.sort_rows("date", True)
    assert order() == ["morning.json", "midday.json", "afternoon.json", "next-day.json", "undated.json"]
    model.sort_rows("date", False)
    assert order() == ["next-day.json", "afternoon.json", "midday.json", "morning.json", "undated.json"]
    # a dive arriving during a refresh goes in at its time, not just its day
    model.merge_rows([{"id": "n", "date": "2026-06-22", "time": "12:30:00", "filename": "lunch.json"}], "date", False)
    assert order()[:4] == ["next-day.json", "afternoon.json", "lunch.json", "midday.json"]


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
    assert divelogs_keys == shared + ["garmin_id"] and garmin_keys == with_title + ["fit", "device"]
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
    monkeypatch.setattr(dive_cache, "push_remote_update", lambda service, filepath, *a, **k: True)
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


def test_dive_table_merge_rows_inserts_in_sort_order_without_a_reset(qapp):
    from desktop.controllers.dives import DiveTableModel
    model = DiveTableModel(service="garmin")
    model.set_rows([{"id": "1", "date": "2026-06-23", "filename": "a.json"},
                    {"id": "2", "date": "2026-06-21", "filename": "b.json"}])
    resets, inserts = [], []
    model.modelReset.connect(lambda: resets.append(1))
    model.rowsInserted.connect(lambda parent, first, last: inserts.append(first))
    model.merge_rows([{"id": "3", "date": "2026-06-22", "filename": "c.json"},
                      {"id": "4", "date": "", "filename": "d.json"}], "date", False)
    assert [model.row(i)["filename"] for i in range(4)] == ["a.json", "c.json", "b.json", "d.json"]
    # a dive already listed - here under an older file name - is replaced, not doubled
    model.merge_rows([{"id": "2", "date": "2026-06-24", "filename": "b2.json"}], "date", False)
    assert [model.row(i)["filename"] for i in range(4)] == ["b2.json", "a.json", "c.json", "d.json"]
    assert resets == [] and inserts == [1, 3, 0]


def test_dives_refresh_lists_garmin_dives_as_they_arrive(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    import threading
    from desktop.controllers.dives import DivesController
    from src.core import scheduler
    garmin_dir = scratch_data_dir / "garmin" / "default" / "data"
    garmin_dir.mkdir(parents=True)
    halfway, finish = threading.Event(), threading.Event()

    def download(*args, **kwargs):
        for n in (1, 2):
            dive = {"summary": {"activityId": str(n), "startTimeLocal": f"2026-06-2{n} 10:00:00"}, "details": {}}
            (garmin_dir / f"{n}.json").write_text(json.dumps(dive))
            if n == 1:
                halfway.set()
                finish.wait(5)
        scheduler.last_download_results = {"success": True}

    monkeypatch.setattr(scheduler, "run_download_thread", download)
    c = DivesController("garmin")
    c._live_timer.setInterval(20)
    c.refresh()
    assert halfway.wait(5)
    assert wait_until(qapp, lambda: c.diveCount == 1) and c.busy     # listed before the refresh is done
    finish.set()
    assert wait_until(qapp, lambda: not c.busy) and c.diveCount == 2 and c.listStatus == "Refreshed."


def test_dives_refresh_stop_button(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    import threading
    from desktop.controllers.dives import DivesController
    from src.core import progress
    from src.core.sync_engine import SyncEngine
    started = threading.Event()

    def download(self, mock_data_dir, overwrite, include_garmin, include_divelogs):
        for n in range(1, 500):
            progress.report(n, 500, f"dive {n}", "garmin")
            started.set()
            threading.Event().wait(0.01)
        return True

    monkeypatch.setattr(SyncEngine, "download_and_save_raw_data", download)
    c = DivesController("garmin")
    assert not c.stoppable
    c.refresh()
    assert started.wait(5) and wait_until(qapp, lambda: c.stoppable)
    c.stop()
    assert c.listStatus == "Stopping after the current dive…"
    assert wait_until(qapp, lambda: not c.busy)
    assert c.listStatus == "Stopped. The dives downloaded so far are listed." and not c.stoppable
    assert not progress.stop_requested()


def test_dives_controller_save_passes_gps_water_temp_and_tanks(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    from desktop.controllers.dives import DivesController
    from src.core import dive_cache
    rows = [{"date": "2026-06-22", "time": "10:00:00", "date_time": "2026-06-22 10:00:00", "dive_number": 1,
             "location": "Reef", "filename": "1.json", "buddy": "A"}]
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: rows)
    calls = {}
    monkeypatch.setattr(dive_cache, "update_dive_fields", lambda service, filename, **kw: calls.update(service=service, filename=filename, **kw) or "/tmp/x.json")
    monkeypatch.setattr(dive_cache, "push_remote_update", lambda service, filepath, *a, **k: True)

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
                        services=None, refresh_fits=False, accounts=None:
                        downloads.update(services=services, overwrite=overwrite, accounts=accounts))
    monkeypatch.setattr(scheduler, "is_download_running", False)
    c.download(False, "")
    assert wait_until(qapp, lambda: not c.running)
    assert downloads["services"] == ["garmin", "divelogs", "submersion", "subsurface"]
    c.download(True, "submersion")
    assert wait_until(qapp, lambda: not c.running)
    assert downloads["services"] == ["submersion"] and downloads["overwrite"] is True
    # the page's ticks: a list, kept in the page's order, unknown ids dropped
    c.download(False, ["subsurface", "garmin", "uddf"])
    assert wait_until(qapp, lambda: not c.running)
    assert downloads["services"] == ["garmin", "subsurface"]
    # from QML the array arrives as a QJSValue (the page's onClicked)
    from PySide6.QtQml import QJSEngine
    js = QJSEngine()
    c.download(False, js.toScriptValue(["divelogs"]))
    assert wait_until(qapp, lambda: not c.running)
    assert downloads["services"] == ["divelogs"]
    downloads.clear()
    c.download(False, [])
    assert not c.running and downloads == {} and "Tick at least one" in c.status


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
    # the board is shared by every account combination; the desktop keeps
    # each combination's state apart (rework.md E19)
    state_files = sorted(p.name for p in (scratch_data_dir / "sync").glob("sync_state*.json"))
    assert state_files and all(json.load(open(scratch_data_dir / "sync" / n))["full_compare_once"] is True for n in state_files)


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
        {"username": "g@x", "token_dir": "", "has_password": True}]
    assert s.divelogsAccounts == [{"username": "d", "has_password": True}]
    assert s.subsurfaceAccounts == [{"username": "me@x.org", "has_password": True}] and s.message == "Saved to keychain."
    assert s.submersionBucket == "my-bucket"
    assert json.loads(fake_keyring.store[(creds_store.SERVICE_NAME, "submersion_secret")])["secret_access_key"] == "secret"
    assert creds_store.load_credentials_model().submersion.passphrase == "hunter2"
    # keeping a blank password/passphrase keeps the stored ones
    s.saveGarminAccounts([{"username": "g@x", "password": "", "token_dir": ""}])
    s.save("me@x.org", "", "s3", "https://s3.example.com", "eu-central-1", "my-bucket", "submersion-sync/", "keyid", "", False, "", "")
    assert creds_store.load_credentials_model().get_garmin_accounts()[0].password == "pw"
    assert json.loads(fake_keyring.store[(creds_store.SERVICE_NAME, "submersion_secret")])["secret_access_key"] == "secret"
    assert creds_store.load_credentials_model().submersion.passphrase == "hunter2"
    # Subsurface Cloud holds several accounts too (rework.md E19): a saved
    # list replaces the stored one, a blank password keeps the stored one.
    s.saveSubsurface("other@x.org", "")
    assert s.subsurfaceAccounts == [{"username": "me@x.org", "has_password": True},
                                    {"username": "other@x.org", "has_password": False}]
    assert s.subsurfaceStatus == "No password stored for other@x.org - enter it and save."
    s.saveSubsurfaceAccounts([{"username": "other@x.org", "password": "pw4"}, {"username": "me@x.org", "password": ""}])
    stored = {a.email: a.password for a in creds_store.load_credentials_model().get_subsurface_accounts()}
    assert stored == {"other@x.org": "pw4", "me@x.org": "pw3"}
    assert s.subsurfaceStatus == "2 accounts saved. Press Test to check a login."
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


# ---------------------------------------------------------------- Track J: units

def test_units_controller_defaults_to_metric_persists_and_emits_once(qapp, scratch_data_dir, fake_keyring):
    """The ``units`` context property (plans/units.md J2): metric until the
    diver picks imperial on the Settings page, saved to desktop_prefs.json,
    one ``unitsChanged`` per actual change, unknown strings are metric."""
    from desktop import preferences
    from desktop.controllers.units import UnitsController
    emitted = []
    c = UnitsController()
    c.unitsChanged.connect(lambda: emitted.append(c.units))
    assert c.units == "metric" and c.isImperial is False
    assert [s["key"] for s in c.systems] == ["metric", "imperial"]
    assert [s["label"] for s in c.systems] == ["Metric (m, bar, °C, L, kg)", "Imperial (ft, psi, °F, cuft, lb)"]
    c.setUnits("metric")                      # already metric: nothing happens
    assert emitted == []
    c.setUnits("imperial")
    assert c.units == "imperial" and c.isImperial is True and emitted == ["imperial"]
    assert preferences.get_units() == "imperial"
    assert json.load(open(scratch_data_dir / "desktop_prefs.json"))["units"] == "imperial"
    c.setUnits("imperial")                    # same again: no second signal
    assert emitted == ["imperial"]
    c.setUnits("cubits")                      # unknown: metric, and that is a change
    assert c.units == "metric" and emitted == ["imperial", "metric"]
    assert preferences.get_units() == "metric"
    # a new controller starts from what the previous one saved
    preferences.set_units("imperial")
    assert UnitsController().units == "imperial"


def test_units_controller_slots_are_the_core_module_for_the_current_system(qapp, scratch_data_dir, fake_keyring):
    """label / decimals / convert / convertBack / format / formatWithLabel
    return exactly what src/core/units.py gives for the controller's system:
    nothing is converted or rounded in the desktop layer."""
    from desktop.controllers.units import UnitsController
    from src.core import units
    c = UnitsController()
    for system in ("metric", "imperial"):
        c.setUnits(system)
        for kind in units.KINDS:
            assert c.label(kind) == units.unit_label(kind, system)
            assert c.decimals(kind) == units.decimals(kind, system)
            assert c.convert(kind, 10.0) == units.convert(kind, 10.0, system)
            assert c.convertBack(kind, 10.0) == units.convert_back(kind, 10.0, system)
            assert c.convertBack(kind, c.convert(kind, 12.34)) == pytest.approx(12.34)
            assert c.format(kind, 12.34) == units.format_value(kind, 12.34, system)
            assert c.formatWithLabel(kind, 12.34) == units.format_with_label(kind, 12.34, system)
            assert c.convert(kind, None) is None and c.convertBack(kind, None) is None
            assert c.format(kind, None) == "" and c.formatWithLabel(kind, None) == ""
    assert c.label("depth") == "ft" and c.format("depth", 10.0) == "33" and c.formatWithLabel("depth", 10.0) == "33 ft"
    assert c.format("temperature", 20.0) == "68" and c.label("nope") == ""
    # the labels as one map, for QML bindings that follow unitsChanged (J3)
    assert c.labels == {"depth": "ft", "pressure": "psi", "temperature": "°F", "volume": "cuft", "weight": "lb", "sac": "cuft/min"}
    c.setUnits("metric")
    assert c.label("depth") == "m" and c.format("depth", 10.0) == "10.0" and c.format("pressure", 200.4) == "200"
    assert c.labels == {"depth": "m", "pressure": "bar", "temperature": "°C", "volume": "L", "weight": "kg", "sac": "L/min"}
    # the scales map for a QML component that draws SI numbers itself (J4,
    # ProfileChart.qml): factor, label and decimals per kind, from the core
    for system in ("metric", "imperial"):
        c.setUnits(system)
        scales = c.scales
        assert set(scales) == set(units.KINDS) - {"temperature"}
        for kind, scale in scales.items():
            assert scale == {"factor": units.convert(kind, 1.0, system), "label": units.unit_label(kind, system),
                             "decimals": units.decimals(kind, system)}
    assert c.scales["depth"] == {"factor": units.FT_PER_M, "label": "ft", "decimals": 0}
    c.setUnits("metric")
    assert c.scales["depth"] == {"factor": 1.0, "label": "m", "decimals": 1}


def test_settings_page_units_card_switches_the_system_and_follows_it(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """The Units card on the Settings page: two radio buttons with the
    systems' texts, metric ticked by default; a click applies at once (the
    controller and the prefs file change, no Save), and a change made from
    the controller moves the tick; the page loads without warnings."""
    from PySide6.QtCore import QMetaObject, QObject
    from desktop import app as desktop_app
    from desktop import logging_bridge
    from desktop import preferences
    from src.core import dive_cache
    monkeypatch.setattr(dive_cache, "list_garmin_dives", lambda *a, **k: [])
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: [])
    warnings = []
    controllers = desktop_app.build_controllers(logging_bridge.install())
    c = controllers["units"]
    engine = desktop_app.create_engine(controllers, "Settings",
                                       on_warnings=lambda errs: warnings.extend(str(e.toString()) for e in errs))
    root = engine.rootObjects()[0]
    wait(qapp, 300)
    card = root.findChild(QObject, "unitsCard")
    assert card is not None and card.property("title") == "Units"
    metric, imperial = card.findChild(QObject, "units-metric"), card.findChild(QObject, "units-imperial")
    assert metric.property("text") == "Metric (m, bar, °C, L, kg)"
    assert imperial.property("text") == "Imperial (ft, psi, °F, cuft, lb)"
    assert metric.property("checked") is True and imperial.property("checked") is False
    QMetaObject.invokeMethod(imperial, "click")
    wait(qapp, 100)
    assert c.units == "imperial" and preferences.get_units() == "imperial"
    assert imperial.property("checked") is True and metric.property("checked") is False
    c.setUnits("metric")                      # changed elsewhere: the card follows
    wait(qapp, 100)
    assert metric.property("checked") is True and imperial.property("checked") is False
    assert warnings == [], warnings
    engine.deleteLater()
    wait(qapp, 50)


# ---------------------------------------------------------------- J3: the dives pages follow the units setting

def _unit_rows():
    """Two dives as dive_cache lists them (SI; weight and visibility in the
    dive's own unit): 30.2 m / 20 °C / 4 kg / 10 m / a 12 L tank 200->50 bar,
    and a shallower one in pounds and feet, to sort against."""
    from src.core import dive_cache
    tanks = [{"tank_name": "T1", "oxygen": 21.0, "helium": 0.0, "volume": 12.0, "start_pressure": 200.0, "end_pressure": 50.0}]
    return [
        {"date": "2026-06-22", "time": "10:00:00", "date_time": "2026-06-22 10:00:00", "dive_number": 1, "duration": 45,
         "location": "Reef", "filename": "1.json", "buddy": "A", "max_depth": "30.20", "avg_depth": "18.50",
         "sac": dive_cache._format_sac(tanks, 18.5, 2700), "water_temp": dive_cache._format_water_temp(19.0, 21.0, 20.0),
         "water_temps": [19.0, 21.0, 20.0], "water_temp_value": 20.0, "tanks": dive_cache._format_tanks(tanks),
         "tanks_detail": tanks, "weight": "4 kg", "visibility": "10 m", "lat": None, "lng": None, "notes": ""},
        {"date": "2026-06-23", "time": "11:00:00", "date_time": "2026-06-23 11:00:00", "dive_number": 2, "duration": 30,
         "location": "Wreck", "filename": "2.json", "buddy": "", "max_depth": 9.5, "avg_depth": "", "sac": "",
         "water_temp": "", "water_temps": [None, None, None], "water_temp_value": None, "tanks": "", "tanks_detail": [],
         "weight": "10 lbs", "visibility": "30 ft", "lat": None, "lng": None, "notes": ""},
    ]


def test_dives_page_shows_the_chosen_units_and_follows_a_switch(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """Under metric every cell, heading and form value is exactly what the
    cache lists (the regression guard); under imperial every depth,
    temperature, SAC, tank size and pressure, weight and visibility is
    converted and rounded like the dive computer, a dive's own pounds /
    feet are converted to the metric system too, and a switch at runtime
    updates the open table and the selected dive at once. The sort runs on
    the stored values in both systems."""
    from PySide6.QtCore import Qt
    from desktop.controllers.dives import DivesController
    from desktop.controllers.units import UnitsController
    from src.core import dive_cache
    rows = _unit_rows()
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: [dict(r) for r in rows])
    monkeypatch.setattr(dive_cache, "get_samples", lambda *a, **k: [])
    units = UnitsController()
    c = DivesController("divelogs", units=units)
    c.setVisibleColumns(["date", "max_depth", "avg_depth", "water_temp", "weight", "visibility", "sac", "tanks"])
    c.load()
    c.setSort("max_depth", True)
    cell = lambda name, col: c.model.data(c.model.index(c.rowOf(name), c.visibleColumns.index(col)), Qt.DisplayRole)
    header = lambda col: c.model.headerData(c.visibleColumns.index(col), Qt.Horizontal)
    metric_cells = {(f, col): cell(f, col) for f in ("1.json", "2.json") for col in c.visibleColumns}
    # metric: the stored strings, untouched
    assert metric_cells[("1.json", "max_depth")] == "30.20" and metric_cells[("2.json", "max_depth")] == "9.5"
    assert metric_cells[("1.json", "water_temp")] == "19-21°C (avg 20°C)" and metric_cells[("1.json", "sac")] == "14.0"
    assert metric_cells[("1.json", "tanks")] == "T1: 21% O2, 12L, 200->50 bar"
    assert metric_cells[("1.json", "weight")] == "4 kg" and metric_cells[("1.json", "visibility")] == "10 m"
    assert metric_cells[("2.json", "weight")] == "4.5 kg" and metric_cells[("2.json", "visibility")] == "9.1 m"   # pounds and feet, in metric
    assert header("sac") == "SAC (L/min)" and header("max_depth") == "Max Depth"
    assert next(col["label"] for col in c.allColumns if col["key"] == "sac") == "SAC (L/min)"
    c.select(c.rowOf("1.json"))
    sel = c.selected
    assert sel["max_depth"] == "30.20" and sel["avg_depth"] == "18.50" and sel["water_temp_value"] == 20.0
    assert sel["weight"] == "4 kg" and sel["visibility"] == "10 m" and sel["tanks_detail"] == rows[0]["tanks_detail"]
    assert [c.model.row(i)["filename"] for i in range(2)] == ["2.json", "1.json"]

    changed, selected = [], []
    c.model.dataChanged.connect(lambda *a: changed.append(a))
    c.selectedChanged.connect(lambda: selected.append(c.selected["max_depth"]))
    units.setUnits("imperial")
    assert changed and selected == ["99"]                     # the open page updated at once
    assert cell("1.json", "max_depth") == "99" and cell("1.json", "avg_depth") == "61"
    assert cell("2.json", "max_depth") == "31"
    assert cell("1.json", "water_temp") == "66-70°F (avg 68°F)" and cell("2.json", "water_temp") == ""
    assert cell("1.json", "sac") == "0.5"                     # 14.0 L/min
    assert cell("1.json", "tanks") == "T1: 21% O2, 0.4cuft, 2901->725 psi"
    assert cell("1.json", "weight") == "8.8 lb" and cell("1.json", "visibility") == "33 ft"
    assert cell("2.json", "weight") == "10 lbs" and cell("2.json", "visibility") == "30 ft"    # already imperial: as stored
    assert header("sac") == "SAC (cuft/min)" and header("max_depth") == "Max Depth"
    assert next(col["label"] for col in c.allColumns if col["key"] == "sac") == "SAC (cuft/min)"
    sel = c.selected
    assert sel["filename"] == "1.json" and sel["max_depth"] == "99" and sel["avg_depth"] == "61"
    assert sel["water_temp_value"] == "68" and sel["water_temp"] == "66-70°F (avg 68°F)"
    assert sel["weight"] == "8.8 lb" and sel["visibility"] == "33 ft" and sel["sac"] == "0.5"
    assert sel["tanks_detail"] == [{"tank_name": "T1", "oxygen": 21.0, "helium": 0.0, "volume": "0.4",
                                    "start_pressure": "2901", "end_pressure": "725"}]
    assert rows[0]["tanks_detail"][0]["volume"] == 12.0                     # the stored row is untouched
    assert c.model.row(c.rowOf("1.json"))["max_depth"] == "30.20"
    # the sort is on the stored values: 9.5 m before 30.2 m whichever system shows
    c.setSort("max_depth", False)
    assert [c.model.row(i)["filename"] for i in range(2)] == ["1.json", "2.json"]
    c.setSort("max_depth", True)
    assert [c.model.row(i)["filename"] for i in range(2)] == ["2.json", "1.json"]
    c.select(c.rowOf("1.json"))

    units.setUnits("metric")                                  # and back: the old strings, exactly
    assert {(f, col): cell(f, col) for f in ("1.json", "2.json") for col in c.visibleColumns} == metric_cells
    assert c.selected["max_depth"] == "30.20" and c.selected["water_temp_value"] == 20.0
    assert c.selected["tanks_detail"] == rows[0]["tanks_detail"]
    # a controller without a UnitsController is metric
    assert DivesController("divelogs")._system() == "metric"


def test_dives_page_edits_in_imperial_reach_the_save_as_si(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """Typed values are brought back to SI at full precision before the
    existing save path; a field left as the form showed it keeps the stored
    value (the rounded display never drifts the data); weight and
    visibility keep the dive's own unit field; the staged overlay and the
    form show the edit in the chosen system."""
    from PySide6.QtCore import Qt
    from desktop.controllers.dives import DivesController
    from desktop.controllers.units import UnitsController
    from src.core import dive_cache
    rows = _unit_rows()
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: [dict(r) for r in rows])
    monkeypatch.setattr(dive_cache, "get_samples", lambda *a, **k: [])
    calls = {}
    monkeypatch.setattr(dive_cache, "update_dive_fields", lambda service, filename, **kw: calls.update(filename=filename, **kw) or "/tmp/x.json")
    monkeypatch.setattr(dive_cache, "push_remote_update", lambda service, filepath, *a, **k: True)
    units = UnitsController()
    units.setUnits("imperial")
    c = DivesController("divelogs", units=units)
    c.setVisibleColumns(["date", "max_depth", "water_temp", "weight", "visibility", "tanks"])
    c.load()
    c.select(c.rowOf("1.json"))
    shown = c.selected
    assert shown["max_depth"] == "99" and shown["water_temp_value"] == "68" and shown["weight"] == "8.8 lb"
    form = lambda **kw: dict({"date": "2026-06-22", "time": "10:00:00", "duration": "45", "max_depth": shown["max_depth"],
                              "location": "Reef", "notes": "", "weight": shown["weight"], "visibility": shown["visibility"],
                              "buddy": "A", "lat": "", "lng": "", "water_temp": shown["water_temp_value"],
                              "tanks": [dict(shown["tanks_detail"][0])]}, **kw)
    cell = lambda col: c.model.data(c.model.index(c.rowOf("1.json"), c.visibleColumns.index(col)), Qt.DisplayRole)

    # nothing touched: every stored value goes back as it was (no drift)
    c.save(form())
    assert wait_until(qapp, lambda: c.status == "Saved."), c.status
    assert calls["max_depth"] == 30.2 and calls["water_temp"] == 20.0
    assert calls["weight"] == "4 kg" and calls["visibility"] == "10 m"
    assert calls["tanks"] == [{"tank_name": "T1", "oxygen": 21.0, "helium": 0.0, "volume": 12.0, "start_pressure": 200.0, "end_pressure": 50.0}]

    # edited: 100 ft, 50 °F, 3000 psi, 0.5 cuft, 10 lb, 20 ft - converted back at full precision
    calls.clear()
    c.select(c.rowOf("1.json"))
    tank = dict(shown["tanks_detail"][0], start_pressure="3000", volume="0.5")
    c.stage("1.json", form(max_depth="100", water_temp="50", weight="10", visibility="20", tanks=[tank]))
    assert cell("max_depth") == "100" and cell("water_temp") == "50-50°F (avg 50°F)"     # the overlay, in the shown system
    assert cell("weight") == "10.0 lb" and cell("visibility") == "20 ft"
    assert c._pending["1.json"]["visibility"].endswith(" m")             # the dive keeps its metres
    assert cell("tanks") == "T1: 21% O2, 0.5cuft, 3000->725 psi"
    c.select(c.rowOf("1.json"))                                  # the form echoes what was typed
    assert c.selected["pending"] is True and c.selected["max_depth"] == "100" and c.selected["weight"] == "10"
    c.saveDive("1.json")
    assert wait_until(qapp, lambda: c.status == "Saved."), c.status
    assert calls["max_depth"] == pytest.approx(30.48) and calls["water_temp"] == pytest.approx(10.0)
    assert calls["weight"].endswith(" kg") and float(calls["weight"].split()[0]) == pytest.approx(4.5359237)
    assert calls["visibility"].endswith(" m") and float(calls["visibility"].split()[0]) == pytest.approx(6.096)
    assert calls["tanks"][0]["start_pressure"] == pytest.approx(206.843, abs=1e-3) and calls["tanks"][0]["volume"] == pytest.approx(14.158, abs=0.01)
    assert calls["tanks"][0]["end_pressure"] == 50.0 and calls["tanks"][0]["tank_name"] == "T1"

    # a weight typed with the shown unit ("9 lb") is the same edit; one typed
    # in the other system ("3 kg") is sent as typed, as before; with nothing
    # stored, a bare number gets the shown unit so the adapter keeps it
    calls.clear()
    c.select(c.rowOf("1.json"))
    c.stage("1.json", form(weight="9 lb", visibility="4 m"))
    assert c._pending["1.json"]["visibility"] == "4 m"
    assert float(c._pending["1.json"]["weight"].split()[0]) == pytest.approx(4.0823313) and c._pending["1.json"]["weight"].endswith(" kg")
    c.discard("1.json")
    c.select(c.rowOf("2.json"))
    c.stage("2.json", {"weight": "", "visibility": "25"})
    assert c._pending["2.json"] == {"weight": "", "visibility": "25 ft"}
    c.discard("2.json")
    c.select(c.rowOf("2.json"))
    c.stage("2.json", {"weight": "12", "visibility": "30 ft"})            # the dive's own pounds and feet
    assert c._pending["2.json"] == {"weight": "12 lbs", "visibility": "30 ft"}

    # metric: everything passes through exactly as typed, as before
    calls.clear()
    units.setUnits("metric")
    c.discard("2.json")
    c.select(c.rowOf("1.json"))
    c.save(form(max_depth="31.5", water_temp="21", weight="6 kg", visibility="12", tanks=[{"tank_name": "T1", "oxygen": "21", "helium": "0", "volume": "12", "start_pressure": "210", "end_pressure": "50"}]))
    assert wait_until(qapp, lambda: c.status == "Saved."), c.status
    assert calls["max_depth"] == 31.5 and calls["water_temp"] == 21.0 and calls["weight"] == "6 kg" and calls["visibility"] == "12 m"
    assert calls["tanks"] == [{"tank_name": "T1", "oxygen": 21.0, "helium": 0.0, "volume": 12.0, "start_pressure": 210.0, "end_pressure": 50.0}]
    assert c._pending == {} and c._typed == {}


def test_dives_page_staged_edits_survive_a_units_switch(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """Edits staged in one system are kept as SI: a switch re-expresses the
    form's echo and the table's overlay in the new system, and the save
    sends the value typed, converted once."""
    from PySide6.QtCore import Qt
    from desktop.controllers.dives import DivesController
    from desktop.controllers.units import UnitsController
    from src.core import dive_cache
    rows = _unit_rows()
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: [dict(r) for r in rows])
    monkeypatch.setattr(dive_cache, "get_samples", lambda *a, **k: [])
    calls = {}
    monkeypatch.setattr(dive_cache, "update_dive_fields", lambda service, filename, **kw: calls.update(filename=filename, **kw) or "/tmp/x.json")
    monkeypatch.setattr(dive_cache, "push_remote_update", lambda service, filepath, *a, **k: True)
    units = UnitsController()
    units.setUnits("imperial")
    c = DivesController("divelogs", units=units)
    c.setVisibleColumns(["date", "max_depth", "weight", "tanks"])
    c.load()
    c.select(c.rowOf("1.json"))
    cell = lambda col: c.model.data(c.model.index(c.rowOf("1.json"), c.visibleColumns.index(col)), Qt.DisplayRole)
    c.stage("1.json", {"max_depth": "100", "water_temp": "68", "weight": "10", "visibility": "33 ft",
                       "tanks": [dict(c.selected["tanks_detail"][0], start_pressure="3000")]})
    si = dict(c._pending["1.json"])
    units.setUnits("metric")
    assert c._pending["1.json"] == si                                   # SI, untouched by the switch
    assert c.selected["pending"] is True and c.selected["max_depth"] == "30.48" and c.selected["water_temp_value"] == "20"
    assert c.selected["weight"].endswith(" kg") and c.selected["visibility"] == "10 m"
    assert float(c.selected["tanks_detail"][0]["start_pressure"]) == pytest.approx(206.8427, abs=1e-3)
    assert c.selected["tanks_detail"][0]["volume"] == "12"               # untouched: the stored 12 L, echoed as text
    assert cell("max_depth") == "30.48" and cell("tanks") == "T1: 21% O2, 12L, 206.843->50 bar"
    units.setUnits("imperial")
    assert c.selected["max_depth"] == "100" and c.selected["tanks_detail"][0]["start_pressure"] == "3000"
    assert cell("max_depth") == "100" and cell("weight") == "10.0 lb"
    c.saveDive("1.json")
    assert wait_until(qapp, lambda: c.status == "Saved."), c.status
    assert calls["max_depth"] == pytest.approx(30.48) and calls["water_temp"] == 20.0
    assert calls["tanks"][0]["start_pressure"] == pytest.approx(206.843, abs=1e-3) and calls["visibility"] == "10 m"


def test_dives_page_labels_follow_the_units_setting(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """The form's labels name the shown unit ("Max depth (ft)", "Water temp
    (°F)", "Weight (lb)", the tank table's "Volume (cuft)" / "Start (psi)")
    and change with the Settings page's switch; the page loads without
    warnings in both systems."""
    from PySide6.QtCore import QObject
    from desktop import app as desktop_app
    from desktop import logging_bridge
    from src.core import dive_cache
    rows = _unit_rows()
    monkeypatch.setattr(dive_cache, "list_garmin_dives", lambda *a, **k: [])
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: [dict(r) for r in rows])
    monkeypatch.setattr(dive_cache, "get_samples", lambda *a, **k: [])
    controllers = desktop_app.build_controllers(logging_bridge.install())
    units, c = controllers["units"], controllers["divelogsDives"]
    warnings = []
    engine = desktop_app.create_engine(controllers, "Divelogs Dives",
                                       on_warnings=lambda errs: warnings.extend(str(e.toString()) for e in errs))
    root = engine.rootObjects()[0]
    wait(qapp, 300)
    page = root.findChild(QObject, "divesPage-Divelogs.org")
    depth, temp, weight = (page.findChild(QObject, n) for n in ("field-max-depth", "field-water-temp", "field-weight"))
    headings = page.findChild(QObject, "tankHeadings")
    texts = lambda: [child.property("text") for child in headings.children() if child.property("text") is not None]
    assert depth.property("label") == "Max depth (m)" and temp.property("label") == "Water temp (°C)"
    assert weight.property("label") == "Weight (kg)"
    assert texts() == ["Name", "O2 %", "He %", "Volume (L)", "Start (bar)", "End (bar)"]
    c.select(c.rowOf("1.json"))
    wait(qapp, 100)
    assert depth.property("text") == "30.20" and temp.property("text") == "20" and weight.property("text") == "4 kg"
    units.setUnits("imperial")
    wait(qapp, 100)
    assert depth.property("label") == "Max depth (ft)" and temp.property("label") == "Water temp (°F)"
    assert weight.property("label") == "Weight (lb)"
    assert texts() == ["Name", "O2 %", "He %", "Volume (cuft)", "Start (psi)", "End (psi)"]
    assert depth.property("text") == "99" and temp.property("text") == "68" and weight.property("text") == "8.8 lb"
    assert c.pendingFiles == []                                 # re-showing the dive is not an edit
    units.setUnits("metric")
    wait(qapp, 100)
    assert depth.property("label") == "Max depth (m)" and depth.property("text") == "30.20"
    assert warnings == [], warnings
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
    monkeypatch.setattr(dive_cache, "list_dives", lambda service, *a, **k: [dict(r) for r in rows])
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
    monkeypatch.setattr(dive_cache, "push_remote_update", lambda service, filepath, *a, **k: pushed.append(filepath) or filepath != "2.json")
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
    monkeypatch.setattr(dive_cache, "list_dives", lambda service, *a, **k: [dict(r) for r in listed])
    calls = []
    remote_ok = {"value": False}
    monkeypatch.setattr(dive_cache, "dive_external_id", lambda service, filename, *a, **k: (f"/x/{filename}", "id-" + filename))
    monkeypatch.setattr(dive_cache, "push_remote_delete",
                        lambda service, external_id, username=None, filepath=None, adapter=None: calls.append(("remote", external_id)) or remote_ok["value"])
    def local_delete(service, filename, *a):
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
    monkeypatch.setattr(dive_cache, "list_dives", lambda service, *a, **k: [dict(r) for r in listed])
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
    base = os.path.join(os.path.dirname(config.SETTINGS_FILE), "sync")
    # kept per account combination (rework.md E19)
    record(Pair("garmin", "divelogs", os.path.join(base, "conflicts_g@x_d.json")), "buddy", "garmin.buddy", "divelogs.buddy", "Anna", "Bob")
    record(Pair("garmin", "subsurface", os.path.join(base, "conflicts_garmin-g@x_subsurface-me@x.org.json")),
           "gps", "garmin.gps", "subsurface.gps", [4.7948, 103.683518], [4.805835, 103.686585])
    c = ConflictsController()
    c.load()
    assert [g["label"] for g in c.groups] == ["Garmin Connect (g@x) ↔ Divelogs.org (d)",
                                              "Garmin Connect (g@x) ↔ Subsurface Cloud (me@x.org)"]
    assert c.count == 2
    buddy = c.groups[0]["conflicts"][0]
    assert (buddy["target_label"], buddy["source_text"], buddy["target_text"]) == ("Buddy", "Anna", "Bob")
    assert (buddy["source_name"], buddy["target_name"]) == ("Garmin Connect", "Divelogs.org")
    gps = c.groups[1]["conflicts"][0]
    assert gps["pair_id"] == "garmin_subsurface::g@x::me@x.org" and gps["source_text"] == "4.7948, 103.684"
    assert brief(None) == "(empty)" and brief([{"o2": 21}]) == "1 item(s)" and brief(18.2) == "18.2"

    # Picks are staged (the same side again undoes), grouped per receiving
    # service for the Save buttons, and written per service: one engine per
    # pair, every pick attempted, failed picks kept staged.
    staged_signals = []
    c.stagedChanged.connect(lambda: staged_signals.append(c.stagedCount))
    c.stage(buddy["pair_id"], buddy["id"], "source")           # Garmin's "Anna" wins: Divelogs gets updated
    c.stage(gps["pair_id"], gps["id"], "target")               # Subsurface's GPS wins: Garmin gets updated
    assert c.stagedCount == 2 and c.staged == {buddy["id"]: "source", gps["id"]: "target"}
    assert c.pendingServices == [{"service": "divelogs", "name": "Divelogs.org", "count": 1},
                                 {"service": "garmin", "name": "Garmin Connect", "count": 1}]
    c.stage(gps["pair_id"], gps["id"], "target")               # same side again: undo
    assert c.staged == {buddy["id"]: "source"} and c.pendingServices[0]["service"] == "divelogs"
    c.unstage(gps["id"])                                       # nothing staged: no signal
    c.stage(gps["pair_id"], gps["id"], "target")
    c.stage(gps["pair_id"], gps["id"], "source")               # picking the other side replaces the pick
    assert c.staged[gps["id"]] == "source" and c.pendingServices[1] == {"service": "subsurface", "name": "Subsurface Cloud", "count": 1}
    assert staged_signals[-1] == 2

    # Clear all (owner request 2026-09-30): every file emptied, picks dropped, nothing written
    c.clearAll()
    assert c.count == 0 and c.staged == {} and c.message == "Cleared 2 waiting conflict(s)."
    assert ConflictStore(os.path.join(base, "conflicts_g@x_d.json")).load() == []
    c.clearAll()
    assert c.message == "No conflicts waiting."
    # re-record for the rest of the test
    record(Pair("garmin", "divelogs", os.path.join(base, "conflicts_g@x_d.json")), "buddy", "garmin.buddy", "divelogs.buddy", "Anna", "Bob")
    record(Pair("garmin", "subsurface", os.path.join(base, "conflicts_garmin-g@x_subsurface-me@x.org.json")),
           "gps", "garmin.gps", "subsurface.gps", [4.7948, 103.683518], [4.805835, 103.686585])
    c.load()
    c.stage(buddy["pair_id"], buddy["id"], "source")
    c.stage(gps["pair_id"], gps["id"], "source")
    c.stage(gps["pair_id"], "ghost", "target")                 # unknown conflict: ignored
    assert c.stagedCount == 2
    c.discard()
    assert c.stagedCount == 0 and c.pendingServices == []
    c.stage(buddy["pair_id"], buddy["id"], "source")
    c.stage(gps["pair_id"], gps["id"], "target")

    calls, engines = [], {}

    class FakeEngine:
        def __init__(self, pair_id, conflicts_file):
            self.pair_id, self.conflicts_file = pair_id, conflicts_file

        def resolve_conflict(self, conflict_id, winner):
            calls.append((self.pair_id, conflict_id, winner))
            if conflict_id == gps["id"]:
                raise RuntimeError("garmin refused the update of dive 1")
            store = ConflictStore(self.conflicts_file)
            found = store.get(conflict_id)
            store.remove(conflict_id)
            return found
    files = {buddy["pair_id"]: os.path.join(base, "conflicts_g@x_d.json"),
             gps["pair_id"]: os.path.join(base, "conflicts_garmin-g@x_subsurface-me@x.org.json")}
    monkeypatch.setattr(c, "_engine", lambda pair_id: engines.setdefault(pair_id, FakeEngine(pair_id, files[pair_id])))
    c.save("divelogs")                                         # only the picks Divelogs receives
    assert c.busy and c.message == "Updating Divelogs.org with 1 change…"
    c.stage(buddy["pair_id"], buddy["id"], "target")          # ignored while saving
    assert wait_until(qapp, lambda: not c.busy)
    assert calls == [(buddy["pair_id"], buddy["id"], "source")]
    assert c.count == 1 and c.staged == {gps["id"]: "target"}
    assert c.message == "Divelogs.org: saved 1 of 1."
    c.save("garmin")                                           # the service refuses: the pick stays staged
    assert wait_until(qapp, lambda: not c.busy)
    assert calls[-1] == (gps["pair_id"], gps["id"], "target") and set(engines) == {buddy["pair_id"], gps["pair_id"]}
    assert c.staged == {gps["id"]: "target"} and c.count == 1
    assert c.message.startswith("Garmin Connect: saved 0 of 1. 1 failed and is still staged: garmin refused")
    c.save("divelogs")                                         # nothing for this service: a no-op
    assert not c.busy
    c.save()                                                   # no argument: everything staged
    assert c.busy and wait_until(qapp, lambda: not c.busy) and calls[-1][1] == gps["id"]


def test_conflicts_page_renders_staged_picks(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """The QML page instantiates its conflict delegates, and staging a pick
    (Keep this / Undo) re-renders them without warnings."""
    import os
    from desktop import app as desktop_app
    from desktop import credentials as creds_store
    from desktop import logging_bridge
    from src.core import config, dive_cache
    from src.core.config import CredentialsModel, DivelogsCredentials, GarminCredentials
    from src.core.conflicts import Conflict, ConflictStore
    monkeypatch.setattr(dive_cache, "list_garmin_dives", lambda *a, **k: [])
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: [])
    creds_store.save_credentials_model(CredentialsModel(
        garmin=[GarminCredentials(username="g@x", password="pw")], divelogs=[DivelogsCredentials(username="d", password="pw")]))
    path = os.path.join(os.path.dirname(config.SETTINGS_FILE), "sync", "conflicts_g@x_d.json")
    ConflictStore(path).save([Conflict(
        id=Conflict.make_id("buddy", "garmin", "divelogs", str(i), str(i + 100)), link_id="buddy",
        source_service="garmin", target_service="divelogs", source_external_id=str(i), target_external_id=str(i + 100),
        source_key="garmin.buddy", target_key="divelogs.buddy", field_type="text",
        dive_ids={"garmin": str(i), "divelogs": str(i + 100)}, source_value=f"Anna {i}", target_value=f"Bob {i}",
        dive_time=f"2026-06-{10 + i:02d} 09:20:00") for i in range(3)])
    warnings = []
    controllers = desktop_app.build_controllers(logging_bridge.install())
    engine = desktop_app.create_engine(controllers, "Conflicts",
                                       on_warnings=lambda errs: warnings.extend(str(e.toString()) for e in errs))
    root = engine.rootObjects()[0]
    wait(qapp, 300)
    c = controllers["conflictsController"]
    assert c.count == 3
    first = c.groups[0]["conflicts"][0]
    c.stage(first["pair_id"], first["id"], "target")
    wait(qapp, 150)
    assert c.stagedCount == 1
    c.unstage(first["id"])
    wait(qapp, 150)
    assert c.stagedCount == 0
    assert warnings == [], warnings
    engine.deleteLater()
    wait(qapp, 50)


def test_conflicts_page_follows_the_units_setting(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """plans/units.md J5: a conflict over a quantity shows both sides in the
    chosen units (found from the field catalogue: its unit, or the own-unit
    weight / visibility it is), rounded like the other pages; text fields and
    metric values shown in metric are exactly today's strings. The saved
    decision is a side, so nothing converts back."""
    import os
    from desktop.controllers.conflicts import ConflictsController, quantity_kind, quantity_text
    from desktop.controllers.units import UnitsController
    from desktop import credentials as creds_store
    from src.core import config
    from src.core.config import CredentialsModel, DivelogsCredentials, GarminCredentials
    from src.core.conflicts import Conflict, ConflictStore
    from src.core.pairs import field_catalog_of
    creds_store.save_credentials_model(CredentialsModel(
        garmin=[GarminCredentials(username="g@x", password="pw")], divelogs=[DivelogsCredentials(username="d", password="pw")]))

    def conflict(link, name, src_val, tgt_val, field_type="number", target=None):
        return Conflict(id=Conflict.make_id(link, "garmin", "divelogs", "1", "2"), link_id=link,
                        source_service="garmin", target_service="divelogs", source_external_id="1", target_external_id="2",
                        source_key=f"garmin.{name}", target_key=f"divelogs.{target or name}",
                        source_type=field_type, field_type=field_type,
                        dive_ids={"garmin": "1", "divelogs": "2"}, source_value=src_val, target_value=tgt_val,
                        dive_time="2026-06-27 09:20:00")
    path = os.path.join(os.path.dirname(config.SETTINGS_FILE), "sync", "conflicts_g@x_d.json")
    ConflictStore(path).save([conflict("depth", "max_depth", 30.2, 30.5),
                              conflict("temp", "temp_min", 24.5, 26.0),
                              conflict("weight", "weight", [4.0, "kilogram"], [10.0, "pound"]),
                              conflict("vis", "visibility", [25.0, "meter"], [80.0, "foot"]),
                              conflict("time", "duration", 2700, 2760),
                              conflict("buddy", "buddy", "Anna", "Bob", field_type="text")])

    # the kinds come from the catalogue: the unit it names, else the own-unit quantity
    specs = {f.key: f for f in field_catalog_of("garmin") + field_catalog_of("divelogs")}
    assert quantity_kind(specs["garmin.max_depth"]) == "depth" and quantity_kind(specs["divelogs.temp_min"]) == "temperature"
    assert quantity_kind(specs["garmin.weight"]) == "weight" and quantity_kind(specs["divelogs.visibility"]) == "depth"
    assert quantity_kind(specs["garmin.duration"]) == "" and quantity_kind(specs["garmin.buddy"]) == "" and quantity_kind(None) == ""
    assert quantity_text([4.0, "kilogram"], "", "imperial") == "4.0, kilogram"    # no kind: brief, whatever the system
    assert quantity_text("n/a", "depth", "imperial") == "n/a" and quantity_text(None, "depth", "imperial") == "(empty)"

    units = UnitsController()
    assert units.units == "metric"
    c = ConflictsController(units=units)
    emitted = []
    c.conflictsChanged.connect(lambda: emitted.append(1))
    c.load()
    texts = lambda: [(r["source_text"], r["target_text"]) for r in c.conflicts]
    kinds = [(r["source_kind"], r["target_kind"]) for r in c.conflicts]
    assert kinds == [("depth", "depth"), ("temperature", "temperature"), ("weight", "weight"), ("depth", "depth"), ("", ""), ("", "")]
    # metric: every metric value exactly as before Track J; a weight or
    # visibility the dive holds in pounds / feet converts to metric (as on the Dives pages)
    assert texts() == [("30.2", "30.5"), ("24.5", "26"), ("4.0, kilogram", "4.5 kg"), ("25.0, meter", "24.4 m"),
                       ("2700", "2760"), ("Anna", "Bob")]
    stored = ConflictStore(path).load()

    units.setUnits("imperial")                                  # a runtime switch re-emits the rows, converted
    assert len(emitted) == 2
    assert texts() == [("99 ft", "100 ft"), ("76 °F", "79 °F"), ("8.8 lb", "10.0 lb"), ("82 ft", "80 ft"),
                       ("2700", "2760"), ("Anna", "Bob")]
    assert [r["source_value"] for r in c.conflicts] == [30.2, 24.5, [4.0, "kilogram"], [25.0, "meter"], 2700, "Anna"]
    assert ConflictStore(path).load() == stored                  # the file is untouched

    # picking a side under imperial saves the side, not a value
    calls = []

    class FakeEngine:
        def resolve_conflict(self, conflict_id, winner):
            calls.append((conflict_id, winner))
            store = ConflictStore(path)
            found = store.get(conflict_id)
            store.remove(conflict_id)
            return found
    monkeypatch.setattr(c, "_engine", lambda pair_id: FakeEngine())
    depth, temp = c.conflicts[0], c.conflicts[1]
    c.stage(depth["pair_id"], depth["id"], "source")
    c.stage(temp["pair_id"], temp["id"], "target")
    c.save()
    assert wait_until(qapp, lambda: not c.busy)
    assert calls == [(depth["id"], "source"), (temp["id"], "target")]
    assert c.count == 4 and texts()[0] == ("8.8 lb", "10.0 lb")      # reloaded, still imperial
    assert [x.source_value for x in ConflictStore(path).load()][0] == [4.0, "kilogram"]

    units.setUnits("metric")
    assert texts()[0] == ("4.0, kilogram", "4.5 kg")
    # a controller without a UnitsController is metric
    plain = ConflictsController()
    plain.load()
    assert [(r["source_text"], r["target_text"]) for r in plain.conflicts][0] == ("4.0, kilogram", "4.5 kg")


def test_conflicts_page_renders_under_imperial(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """The QML page shows the converted values and follows a switch made on
    the Settings page while it is open, without warnings."""
    import os
    from desktop import app as desktop_app
    from desktop import credentials as creds_store
    from desktop import logging_bridge, preferences
    from src.core import config, dive_cache
    from src.core.config import CredentialsModel, DivelogsCredentials, GarminCredentials
    from src.core.conflicts import Conflict, ConflictStore
    monkeypatch.setattr(dive_cache, "list_garmin_dives", lambda *a, **k: [])
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: [])
    creds_store.save_credentials_model(CredentialsModel(
        garmin=[GarminCredentials(username="g@x", password="pw")], divelogs=[DivelogsCredentials(username="d", password="pw")]))
    path = os.path.join(os.path.dirname(config.SETTINGS_FILE), "sync", "conflicts_g@x_d.json")
    ConflictStore(path).save([Conflict(
        id=Conflict.make_id("depth", "garmin", "divelogs", "1", "2"), link_id="depth",
        source_service="garmin", target_service="divelogs", source_external_id="1", target_external_id="2",
        source_key="garmin.max_depth", target_key="divelogs.max_depth", source_type="number", field_type="number",
        dive_ids={"garmin": "1", "divelogs": "2"}, source_value=30.2, target_value=30.5, dive_time="2026-06-27 09:20:00")])
    preferences.set_units("imperial")
    warnings = []
    controllers = desktop_app.build_controllers(logging_bridge.install())
    engine = desktop_app.create_engine(controllers, "Conflicts",
                                       on_warnings=lambda errs: warnings.extend(str(e.toString()) for e in errs))
    root = engine.rootObjects()[0]
    wait(qapp, 300)
    c = controllers["conflictsController"]
    assert c.count == 1 and (c.conflicts[0]["source_text"], c.conflicts[0]["target_text"]) == ("99 ft", "100 ft")
    controllers["units"].setUnits("metric")
    wait(qapp, 150)
    assert (c.conflicts[0]["source_text"], c.conflicts[0]["target_text"]) == ("30.2", "30.5")
    assert warnings == [], warnings
    engine.deleteLater()
    wait(qapp, 50)


def test_mapping_page_under_imperial(qapp, scratch_data_dir, fake_keyring):
    """plans/units.md J5: the Mapping page shows no quantity - fields, rules
    and the catalogue's unit next to a field name (the unit the services
    exchange) - so nothing on it converts; it loads without warnings under
    imperial, and a template preview keeps its SI number, because the
    preview is the text the rule writes to the service."""
    from desktop import app as desktop_app
    from desktop import logging_bridge, preferences
    from desktop.controllers.mapping import MappingController
    preferences.set_units("imperial")
    m = MappingController()
    assert m.createPlainRule("garmin.max_depth", "divelogs", "divelogs.notes")   # notes + max depth: a composite
    m.previewTemplate("{garmin.notes} / max {garmin.max_depth} m")
    assert m.preview == "Great visibility on the wreck. / max 31.4 m" and m.previewProblems == ""
    warnings = []
    controllers = desktop_app.build_controllers(logging_bridge.install())
    assert controllers["units"].units == "imperial"
    engine = desktop_app.create_engine(controllers, "Mapping",
                                       on_warnings=lambda errs: warnings.extend(str(e.toString()) for e in errs))
    root = engine.rootObjects()[0]
    wait(qapp, 300)
    assert controllers["mappingController"].panels
    assert warnings == [], warnings
    engine.deleteLater()
    wait(qapp, 50)


def test_about_info(qapp, scratch_data_dir, fake_keyring):
    import os
    from desktop.controllers.about import AboutController
    from src.core import about
    a = AboutController()
    assert a.version == about.app_version() and a.version not in ("", "local-dev")
    assert a.licenseName == "MIT" and "Permission is hereby granted" in a.licenseText
    assert a.projectUrl.startswith("https://") and any(c["name"] == "Python" for c in a.components)
    assert any(c["name"] == "Garmin FIT SDK" for c in a.components)               # shipped since I12
    assert [r["name"] for r in a.references] == ["libdivecomputer", "Garmin FIT SDK"]   # the "With thanks to" card
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
    monkeypatch.setattr(dive_cache, "list_dives", lambda service, *a, **k: [dict(r) for r in rows])
    s = DivesController("subsurface")
    s.load()
    assert "visibility" not in [c["key"] for c in s.allColumns] and "visibility" not in s.visibleColumns
    assert next(c["label"] for c in s.allColumns if c["key"] == "location") == "Dive site"
    assert s.model.headerData(s.visibleColumns.index("location"), Qt.Horizontal) == "Dive site"
    assert s.profileLocksDepths and not s.hasVisibility
    g = DivesController("garmin")
    assert g.hasVisibility and not g.profileLocksDepths
    assert next(c["label"] for c in DivesController("divelogs").allColumns if c["key"] == "location") == "Location, dive site"


def test_tooltips_use_the_themed_tip():
    """The attached ToolTip takes the platform style (faint on macOS) and
    shows instantly over its neighbours; every tooltip goes through Tip.qml."""
    import glob
    import re
    qml_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "desktop", "qml")
    offenders = [f"{os.path.basename(p)}:{n}" for p in glob.glob(os.path.join(qml_dir, "*.qml"))
                 for n, line in enumerate(open(p, encoding="utf-8"), 1) if re.search(r"\bToolTip\.\w+\s*:", line)]
    assert offenders == []


def test_save_all_reports_progress_and_updates_each_dive_as_it_is_saved(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """Save all names the dive it is on and how far it has got, and each dive
    loses its unsaved mark as soon as its own upload is done - not only when
    the whole batch is. A dive edited again while it uploads stays pending."""
    import threading
    from desktop.controllers.dives import DivesController
    from src.core import dive_cache
    listed = [{"date": "2026-06-22", "time": "10:00:00", "date_time": "2026-06-22 10:00:00", "filename": f"{n}.json",
               "location": f"Site {n}", "buddy": ""} for n in (1, 2, 3)]
    monkeypatch.setattr(dive_cache, "list_dives", lambda service, *a, **k: [dict(r) for r in listed])
    gates = {f"{n}.json": threading.Event() for n in (1, 2, 3)}

    def update(service, filename, **kw):
        next(r for r in listed if r["filename"] == filename)["buddy"] = kw["buddy"]
        return filename
    monkeypatch.setattr(dive_cache, "update_dive_fields", update)
    monkeypatch.setattr(dive_cache, "push_remote_update", lambda service, filepath, *a, **k: gates[filepath].wait(5))
    c = DivesController("subsurface")
    c.setVisibleColumns(["date", "location", "buddy"])
    c.load()
    form = lambda buddy: {"date": "2026-06-22", "time": "10:00:00", "location": "x", "buddy": buddy}
    for n in (1, 2, 3):
        c.stage(f"{n}.json", dict(form(f"B{n}"), location=f"Site {n}"))
    buddy = lambda f: c.model.data(c.model.index(c.rowOf(f), c.visibleColumns.index("buddy")))

    c.saveAll()
    assert wait_until(qapp, lambda: c.savingFile == "1.json")
    assert c.progressText == "Saving 2026-06-22 Site 1 (1/3)" and c.progressFraction == 0 and c.stoppable
    gates["1.json"].set()
    assert wait_until(qapp, lambda: c.savingFile == "2.json")
    assert sorted(c.pendingFiles) == ["2.json", "3.json"] and c.busy        # 1 is done, the rest still going
    assert buddy("1.json") == "B1" and c.progressText.endswith("(2/3)")
    c.stage("2.json", dict(form("newer"), location="Site 2"))                # edited again while it uploads
    gates["2.json"].set()
    gates["3.json"].set()
    assert wait_until(qapp, lambda: not c.busy)
    assert c.pendingFiles == ["2.json"] and c.savingFile == "" and c.progressFraction == -1
    assert c.status == "Saved 3 dives."


def test_editing_a_dive_in_the_table(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """A double-clicked cell edits the dive in place: Enter / Tab stages the
    value without a Save and the form below shows it too; Esc drops it."""
    from PySide6.QtCore import QMetaObject, QObject, Q_ARG
    from desktop import app as desktop_app
    from desktop import logging_bridge
    from src.core import dive_cache
    rows = [{"date": "2026-06-22", "time": "10:00:00", "date_time": "2026-06-22 10:00:00", "dive_number": 1,
             "activity_name": "Reef", "location_name": "Gozo", "filename": "1.json", "buddy": "A", "weight": "4"},
            {"date": "2026-06-23", "time": "11:00:00", "date_time": "2026-06-23 11:00:00", "dive_number": 2,
             "activity_name": "Wreck", "location_name": "", "filename": "2.json", "buddy": "", "weight": ""}]
    monkeypatch.setattr(dive_cache, "list_garmin_dives", lambda *a, **k: [dict(r) for r in rows])
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: [])
    monkeypatch.setattr(dive_cache, "get_samples", lambda *a, **k: [])
    controllers = desktop_app.build_controllers(logging_bridge.install())
    c = controllers["garminDives"]
    c.setVisibleColumns(["date", "dive_number", "buddy", "weight", "avg_depth"])
    warnings = []
    engine = desktop_app.create_engine(controllers, "Garmin Dives",
                                       on_warnings=lambda errs: warnings.extend(str(e.toString()) for e in errs))
    root = engine.rootObjects()[0]
    wait(qapp, 300)
    page = root.findChild(QObject, "divesPage-Garmin Connect")
    editor = root.findChild(QObject, "cellEditor")
    buddy_field = root.findChild(QObject, "field-buddy")
    call = lambda name, *args: QMetaObject.invokeMethod(page, name, *[Q_ARG("QVariant", a) for a in args])
    row, col = c.rowOf("1.json"), c.visibleColumns.index("buddy")

    call("startCellEdit", row, col)
    wait(qapp)
    assert editor.property("visible") and editor.property("text") == "A"
    assert c.selected["filename"] == "1.json"                                # double-click selects the dive
    editor.setProperty("text", "Anna")
    call("moveCellEdit", 1)                                                  # Tab: stage, go on to Weight
    wait(qapp)
    assert c.pendingFiles == ["1.json"] and c.selected["buddy"] == "Anna"
    assert buddy_field.property("text") == "Anna"                           # the form shows the edit too
    assert c.model.data(c.model.index(row, col)) == "Anna"
    assert editor.property("visible") and editor.property("text") == "4"
    editor.setProperty("text", "6")
    call("moveCellEdit", 1)                                                  # Avg depth is derived: no next cell
    wait(qapp)
    assert not editor.property("visible") and c.selected["weight"] == "6" and c.selected["buddy"] == "Anna"

    call("startCellEdit", c.rowOf("2.json"), col)
    wait(qapp)
    editor.setProperty("text", "dropped")
    call("cancelCellEdit")                                                   # Esc
    wait(qapp)
    assert not editor.property("visible") and c.pendingFiles == ["1.json"]
    call("startCellEdit", c.rowOf("2.json"), c.visibleColumns.index("avg_depth"))
    wait(qapp)
    assert not editor.property("visible")                                    # not an editable column
    assert warnings == [], warnings
    engine.deleteLater()
    wait(qapp, 50)


def test_mapping_save_drops_conflicts_the_board_no_longer_raises(qapp, scratch_data_dir, fake_keyring):
    """Owner request 2026-09-27: giving a rule a policy other than manual
    clears the conflicts it queued, without waiting for a run."""
    import os
    from desktop.controllers.mapping import MappingController
    from src.core import layout
    from src.core.conflicts import Conflict, ConflictStore

    def conflict(link, source_key, target_key):
        return Conflict(id=Conflict.make_id(link, "garmin", "divelogs", "1", "2"), link_id=link, source_service="garmin",
                        target_service="divelogs", source_external_id="1", target_external_id="2", source_key=source_key,
                        target_key=target_key, field_type="text", dive_ids={"garmin": "1", "divelogs": "2"},
                        source_value="A", target_value="B")
    store = ConflictStore(os.path.join(layout.sync_dir(str(scratch_data_dir)), "conflicts_u_v.json"))
    store.save([conflict("buddy", "garmin.buddy", "divelogs.buddy"), conflict("notes", "garmin.notes", "divelogs.notes")])

    m = MappingController()
    m.selectRule("divelogs", "buddy")
    assert m.updateRule({"conflict": "source_wins"}) == ""
    assert m.save() == ""
    assert "1 waiting conflict" in m.message and "dropped" in m.message
    assert [c.link_id for c in store.load()] == ["notes"]         # notes' policy did not change

    assert m.save() == "" and m.message == "Saved."               # nothing more to drop


def test_settings_controller_shearwater_accounts(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """The Shearwater card: databases are desktop preferences (not keychain
    items), Detect lists every account the app has, and the Sync page
    offers the picked account like the other services'."""
    import shutil
    from desktop import accounts, logging_bridge
    from desktop.controllers.settings import SettingsController
    from desktop.controllers.sync import SyncController
    from tests.test_shearwater import FIXTURE, _fake_install
    c = SettingsController()
    assert c.shearwaterAccounts == [] and "not installed" in c.shearwaterStatus
    assert c.detectShearwater() == []
    users = _fake_install(scratch_data_dir, monkeypatch, ["live@x", "test@x"], active="test@x")
    live, test = str(users / "live@x" / "dive_data.db"), str(users / "test@x" / "dive_data.db")
    assert c.detectShearwater() == [{"account": "test@x", "database": test}, {"account": "live@x", "database": live}]
    assert "Found 2" in c.message
    assert "using the app's active account on this computer, test@x" in c.shearwaterStatus   # nothing saved yet
    copy = str(scratch_data_dir / "copy.db")
    shutil.copy(FIXTURE, copy)
    c.saveShearwaterAccounts([{"account": "", "database": test}, {"account": "Copy", "database": copy},
                              {"account": "", "database": str(scratch_data_dir / "missing.db")}, {"account": "x", "database": ""}])
    folder = scratch_data_dir.name                                             # a file outside the app is named after its folder
    assert [(r["account"], r["exists"]) for r in c.shearwaterAccounts] == [("test@x", True), ("Copy", True), (folder, False)]
    assert c.shearwaterStatus == f"2 account(s) ready; missing: {folder}."
    c.saveGarminAccounts([{"username": "g@x", "password": "p"}])
    sync = SyncController(logging_bridge.install())
    assert sync.shearwaterAccounts == ["test@x", "Copy"]
    assert {"spec": "shearwater", "id": "shearwater", "label": "Shearwater app"} in sync.endpoints
    sync.setSelectedAccount("shearwater", "Copy")
    assert accounts.sync_selection()["shearwater"] == "Copy"
    assert accounts.engine_kwargs()["shearwater_account"] == "Copy"
    # the owner's bug (2026-09-29): Garmin -> Shearwater had no pair on the page,
    # so Sync now answered "Pick two different services" instead of running
    from src.core import scheduler
    seen = {}
    monkeypatch.setattr(scheduler, "run_sync_thread", lambda dry_run, custom=None, **kw: seen.update(custom=custom))
    monkeypatch.setattr(scheduler, "is_sync_running", False)
    assert any({p["source"], p["target"]} == {"garmin", "shearwater"} and p["builtin"] for p in sync.pairs)
    sync.runSyncBetween(False, "garmin", "shearwater", True, True, "", "", True, False)
    assert wait_until(qapp, lambda: not sync.running)
    assert (seen["custom"]["source"], seen["custom"]["target"], seen["custom"]["directionality"]) == ("garmin", "shearwater", "to_shearwater")
    assert seen["custom"]["shearwater_account"] == "Copy" and seen["custom"]["account_scoped"] is True
    c.saveShearwaterAccounts([])
    assert c.shearwaterAccounts == [] and "using the app's active account" in c.shearwaterStatus


def test_mapping_controller_take_part_of_a_value(qapp, scratch_data_dir, fake_keyring):
    """The rule editor's 'use only part of the value' (2026-09-30)."""
    from desktop.controllers.mapping import MappingController
    m = MappingController()
    assert m.createRule("garmin.activityName", "divelogs", "divelogs.location")
    rule = m.selectedRule
    assert rule["take_possible"] is True and rule["take"] == ""
    assert (rule["take_mode"], rule["take_separator"]) == ("whole", ",")
    m.previewTakeChoice("before", ",", "")
    assert "no such part" in m.preview and m.previewProblems == ""            # the example title has no comma
    m.previewTake("no group")
    assert "needs a group" in m.previewProblems
    assert m.updateRule({"take": r"^(\w+)", "template": "{garmin.activityName}"}) != ""      # not both
    # the plain choice "before a separator" is stored as the pattern behind it
    assert m.updateRule({"take_mode": "before", "take_separator": " of "}) == ""
    assert m.selectedRule["take_mode"] == "before" and m.selectedRule["take_separator"] == " of "
    assert m.preview.startswith('"Wreck of the Zenobia" becomes "Wreck"')
    rules = m._as_models()
    assert rules["divelogs"][-1].take == m.selectedRule["take"] and rules["divelogs"][-1].take.startswith("^(.+?)")
    assert m.updateRule({"take_mode": "custom", "take": r"^(\w+)"}) == "" and m.selectedRule["take_mode"] == "custom"
    assert m.updateRule({"take_mode": "whole"}) == "" and m.selectedRule["take"] == "" and m.preview == "(plain copy)"


def test_mapping_board_of_a_shearwater_pair_saves(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """The owner's report (2026-09-30): the Garmin <-> Shearwater board could
    never be saved (its shipped rules failed validation), so the page kept
    saying 'unsaved changes' and the app asked about them on quit."""
    import shutil
    from desktop.controllers.mapping import MappingController
    from desktop.controllers.settings import SettingsController
    from tests.test_shearwater import FIXTURE
    settings = SettingsController()
    settings.saveGarminAccounts([{"username": "g@x", "password": "p"}])
    db = str(scratch_data_dir / "dive_data.db")
    shutil.copy(FIXTURE, db)
    settings.saveShearwaterAccounts([{"account": "test", "database": db}])
    m = MappingController()
    m.selectPair("garmin_shearwater")
    assert m.pairId == "garmin_shearwater" and not m.dirty
    assert m.createRule("garmin.activityName", "shearwater", "shearwater.other1")   # a field with no rule yet (notes would offer a split)
    assert m.dirty
    assert m.save() == "" and m.message.startswith("Saved") and not m.dirty
    # a board that cannot be saved says so up front
    m2 = MappingController()
    m2.selectPair("garmin_shearwater")
    m2._rules_of("shearwater").append({"id": "bad", "target": "shearwater.tanks", "source": ["garmin.tanks"], "conflict": "source_wins",
                                       "template": None, "reverse": None, "reverse_conflict": None, "separator": ", ", "when": None, "take": None})
    assert m2.save() != "" and m2.message.startswith("Not saved") and "cannot be written" in m2.message


# ---------------------------------------------------------------- Track I: Convert page

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
FIT_FIXTURES = os.path.join(FIXTURES, "garmin_fit")


def _file_url(path):
    from PySide6.QtCore import QUrl
    return QUrl.fromLocalFile(str(path)).toString()


def _save_button(page, format_id):
    """The Save as button of ``format_id`` on the Convert page. A Repeater's
    delegates are visual children of its parent item, not QObject children,
    so findChild does not see them."""
    repeater = page.findChild(QQuickItem, "saveTargets")
    return next(item for item in repeater.parentItem().childItems() if item.objectName() == f"saveTarget-{format_id}")


def _loaded_convert(qapp, *paths):
    """A ConvertController showing the dives of ``paths`` (read on its worker)."""
    from desktop.controllers.convert import ConvertController
    c = ConvertController()
    c.openFiles([_file_url(p) for p in paths])
    assert c.busy
    assert wait_until(qapp, lambda: not c.busy)
    return c


def test_convert_controller_opens_files_and_selects(qapp, scratch_data_dir, fake_keyring):
    """Several files at once (Q6, Q9), the dives listed in file order, one
    selected to start with; plain click, Ctrl-click and Shift-click as in a
    file manager; the detail pane shows the clicked dive."""
    from desktop.controllers import convert as module
    c = _loaded_convert(qapp, os.path.join(FIXTURES, "ssrf", "handwritten.ssrf"), os.path.join(FIXTURES, "uddf", "subsurface_sync.uddf"))
    rows = c.dives
    assert c.diveCount == len(rows) > 2 and c.files == ["handwritten.ssrf", "subsurface_sync.uddf"]
    assert [r["index"] for r in rows] == list(range(len(rows)))
    assert all(r["date"] and r["time"] and r["max_depth"].endswith(" m") and r["duration"].endswith(" min") for r in rows if r["date"] != "")
    assert c.message.startswith(f"{len(rows)} dives from 2 files.")
    assert isinstance(c.warnings, list)                       # the readers' lines, shown on the page
    # a fresh read selects the first dive and shows it
    assert c.selection == [0] and c.currentRow == 0 and c.selected["title"].startswith(rows[0]["date"])
    sel = c.selected
    assert {"tanks", "samples", "channels", "events", "computer", "max_depth", "duration"} <= set(sel)
    assert sel["sample_count"] == len(sel["samples"]) and all({"time", "depth", "temp"} <= set(s) for s in sel["samples"][:3])
    # plain click: that dive alone; Ctrl-click adds and removes; Shift-click a range
    c.clickRow(2, False, False)
    assert c.selection == [2] and c.currentRow == 2 and c.selectedCount == 1
    c.clickRow(0, True, False)
    assert c.selection == [0, 2] and c.currentRow == 0
    c.clickRow(2, True, False)
    assert c.selection == [0]
    c.clickRow(0, False, False)
    c.clickRow(3, False, True)
    assert c.selection == [0, 1, 2, 3] and c.currentRow == 3
    c.selectAll()
    assert c.selectedCount == c.diveCount
    c.clickRow(99, False, False)                               # off the list: ignored
    assert c.selectedCount == c.diveCount
    # the targets and the dialog filters come from the registry
    assert [t["id"] for t in c.targets] == ["uddf", "ssrf", "fit"] and c.targets[1]["extension"] == ".ssrf"
    assert c.openNameFilters[0].startswith("Dive files (") and c.saveNameFilters("uddf") == ["UDDF (*.uddf)"]
    assert c.saveNameFilters("fit") == ["Garmin FIT (*.fit)"] and c.fitSaveTip == "" and c.fitEncodes is True   # Subsurface dives: written from scratch (I12)
    assert "site" in c.fitDrops and "buddy" in c.fitDrops
    # row and detail formatting helpers
    assert module.mix_name(21.0, 0) == "air" and module.mix_name(32.0, 0) == "EAN32" and module.mix_name(18.0, 45.0) == "TMX 18/45"
    assert module.local_path("file:///tmp/a%20b.fit") == "/tmp/a b.fit" and module.local_path("/x/y") == "/x/y"
    c.clear()
    assert c.diveCount == 0 and c.selected == {} and c.message == ""


def test_convert_controller_reads_a_garmin_fit(qapp, scratch_data_dir, fake_keyring):
    """The two-transmitter FIT: tanks, both pressure channels, the events and
    the computer reach the detail pane (the fixtures are untracked: skipped
    when absent)."""
    path = os.path.join(FIT_FIXTURES, "two_tanks.fit")
    if not os.path.exists(path):
        pytest.skip("FIT fixture not present")
    c = _loaded_convert(qapp, path)
    assert c.diveCount == 1 and c.files == ["two_tanks.fit"] and c.warnings == []
    sel = c.selected
    assert sel["source"] == "two_tanks.fit" and sel["external_ids"] == ""   # a cache file name would give the activity id
    assert sel["computer"].startswith("Garmin Descent") and "serial 1000000001" in sel["computer"]
    assert len(sel["tanks"]) == 2 and sel["tanks"][0]["mix"] == "air" and sel["tanks"][1]["index"] == 2
    assert sel["channels"][0] == "tank pressure (tank 1, tank 2)" and "NDL" in sel["channels"]
    assert sel["event_count"] == len(sel["events"]) > 0 and sel["gf"] == "40/85"
    assert c.dives[0]["dive_number"] != "" and c.suggestedFileName("uddf").endswith(f" dive {c.dives[0]['dive_number']}.uddf")


def test_convert_controller_fills_dives_from_the_garmin_cache(qapp, scratch_data_dir, fake_keyring):
    """I9 (decision Q7): a dive whose Garmin activity id is in an account's
    cache gets site, buddy, notes, weight, visibility and tank volumes from
    it, only where the file has none; the detail pane says what came from
    where; the checkbox (on by default, remembered in desktop_prefs.json)
    re-applies to the dives already loaded without re-reading the files;
    dives without a match or an id pass through; a broken cache entry is a
    log line, not a failed open."""
    from desktop import preferences
    from desktop.controllers.convert import ConvertController
    from src.core import garmin_files, layout
    from src.core.convert.formats import ReadResult
    from src.core.models import GasMixture, UnifiedDive
    from tests.test_convert_enrich import cache_payload
    folder = layout.dives_dir("garmin", "me@example.org", str(scratch_data_dir))
    garmin_files.write_cached(folder, cache_payload(24449823373, tank_sizes=(12.0, 11.0)))
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "9_2026-01-01_100000_555.json"), "w") as f:
        f.write("{broken")
    fit = UnifiedDive(date_time=datetime(2026, 8, 29, 9, 56, 11), duration=2780, max_depth=11.92, dive_number=38,
                      external_ids={"garmin": "24449823373"}, notes="From the watch",
                      gas_mixtures=[GasMixture(oxygen=21.0, start_pressure=200.0, end_pressure=60.0, tank_name="Tank 1"),
                                    GasMixture(oxygen=21.0, tank_volume=15.0, tank_name="Tank 2")])
    broken = UnifiedDive(date_time=datetime(2026, 1, 1, 10), duration=600, max_depth=5.0, external_ids={"garmin": "555"})
    plain = UnifiedDive(date_time=datetime(2026, 2, 1, 10), duration=600, max_depth=5.0, location="From the UDDF")
    result = ReadResult(dives=[fit, broken, plain], files=[("/x/a.fit", "fit"), ("/x/b.fit", "fit"), ("/x/c.uddf", "uddf")])

    c = ConvertController()
    assert c.enrichFromCache is True and preferences.get_convert_enrich() is True
    c._apply_read(result)
    assert c.enrichedCount == 1 and c.message == "3 dives from 3 files. 1 filled from the Garmin cache."
    sel = c.selected
    assert sel["location"] == "House Reef" and sel["buddy"] == "Kim" and sel["notes"] == "From the watch"   # the file's notes stay
    assert sel["weight"] == "4 kg" and sel["visibility"] == "15 m"
    assert [t["volume"] for t in sel["tanks"]] == ["12 l", "15 l"] and sel["tanks"][0]["start_pressure"] == "200 bar"
    assert sel["enriched"] == "site, buddy, weight, visibility, tank volume from the Garmin cache (me@example.org)"
    assert c.dives[0]["location"] == "House Reef"
    c.clickRow(1, False, False)
    assert c.selected["enriched"] == "" and c.selected["location"] == ""          # the broken entry: nothing filled
    c.clickRow(2, False, False)
    assert c.selected["enriched"] == "" and c.selected["location"] == "From the UDDF"
    # the dives as read are untouched; the enriched ones are derived from them
    assert fit.location is None and fit.gas_mixtures[0].tank_volume is None
    # untick: the file alone, the selection kept, the choice remembered
    c.clickRow(0, True, False)
    c.setEnrichFromCache(False)
    assert c.enrichFromCache is False and preferences.get_convert_enrich() is False
    assert c.selection == [0, 2] and c.currentRow == 0
    assert c.enrichedCount == 0 and c.message == "3 dives from 3 files."
    assert c.selected["location"] == "" and c.selected["buddy"] == "" and c.selected["enriched"] == ""
    assert [t["volume"] for t in c.selected["tanks"]] == ["", "15 l"] and c.dives[0]["location"] == ""
    c.setEnrichFromCache(False)                                                   # no change: nothing happens
    c.setEnrichFromCache(True)
    assert c.selected["location"] == "House Reef" and c.enrichedCount == 1 and c.message.endswith("1 filled from the Garmin cache.")
    # a new controller starts from the remembered choice
    c.setEnrichFromCache(False)
    assert ConvertController().enrichFromCache is False
    # the worker path reads the cache too, and a file without Garmin ids is unchanged;
    # Open adds to the list (I9b), so the filled dive is still there
    c.setEnrichFromCache(True)
    c.openFiles([_file_url(os.path.join(FIXTURES, "ssrf", "handwritten.ssrf"))])
    assert wait_until(qapp, lambda: not c.busy)
    assert c.diveCount == 8 and c.enrichedCount == 1
    assert c.message == f"5 dives from 1 file added; 8 in the list. {len(c.warnings)} warnings. 1 filled from the Garmin cache."
    assert c.selection == [3] and c.currentRow == 3 and c.selected["enriched"] == ""
    c.clear()
    assert c.diveCount == 0 and c.enrichedCount == 0 and c.message == ""
    c.openFiles([_file_url(os.path.join(FIXTURES, "ssrf", "handwritten.ssrf"))])
    assert wait_until(qapp, lambda: not c.busy)
    assert c.diveCount == 5 and c.enrichedCount == 0 and "Garmin cache" not in c.message


def test_convert_controller_fills_by_start_time_and_prefers_the_selected_account(qapp, scratch_data_dir, fake_keyring):
    """The I9 follow-up: a file whose name holds no activity id (a renamed
    export, a file in the diver's own folder) is matched to the cached dive
    with the same start; the pane says so. With two accounts holding the
    dive, the Garmin dives page's account comes first and an exact id match
    anywhere beats it."""
    from desktop import accounts, preferences
    from desktop.controllers import convert as module
    from desktop.controllers.convert import ConvertController
    from src.core import garmin_files, layout
    from src.core.convert.formats import ReadResult
    from src.core.models import GasMixture, UnifiedDive
    from tests.test_convert_enrich import cache_payload
    gmt = "2026-08-29 02:56:11"
    garmin_files.write_cached(layout.dives_dir("garmin", "live@example.org", str(scratch_data_dir)),
                              cache_payload(18314668175, location="Live copy", gmt=gmt))
    garmin_files.write_cached(layout.dives_dir("garmin", "test@example.org", str(scratch_data_dir)),
                              cache_payload(24449823373, location="Test copy", buddy="", gmt=gmt))
    renamed = UnifiedDive(date_time=datetime(2026, 8, 29, 9, 56, 11), date_time_utc=datetime(2026, 8, 29, 2, 56, 11),
                          duration=2780, max_depth=11.92, dive_number=38, gas_mixtures=[GasMixture(oxygen=21.0)])
    # no account configured: name order
    assert module.preferred_garmin_account() == ""
    c = ConvertController()
    c._apply_read(ReadResult(dives=[renamed], files=[("/x/515.fit", "fit")], sources=["/x/515.fit"]))
    assert c.enrichedCount == 1 and c.message == "1 dive from 1 file. 1 filled from the Garmin cache."
    assert c.selected["location"] == "Live copy" and c.selected["external_ids"] == ""
    assert c.selected["enriched"] == "site, buddy, notes, weight, visibility, tank volume from the Garmin cache (live@example.org, matched by start time)"
    # the Garmin dives page's pick comes first
    from src.core.config import CredentialsModel, GarminCredentials
    from desktop import credentials as creds_store
    creds_store.save_credentials_model(CredentialsModel(garmin=[
        GarminCredentials(username="live@example.org", password="p"), GarminCredentials(username="test@example.org", password="p")]))
    assert module.preferred_garmin_account() == "live@example.org"
    accounts.select("dives", "garmin", "test@example.org")
    assert module.preferred_garmin_account() == "test@example.org"
    c.clear()
    c._apply_read(ReadResult(dives=[renamed], files=[("/x/515.fit", "fit")], sources=["/x/515.fit"]))
    assert c.selected["location"] == "Test copy"
    assert c.selected["enriched"] == "site, notes, weight, visibility, tank volume from the Garmin cache (test@example.org, matched by start time)"
    # an exact id match in the other account wins over the preferred one's time match
    exported = renamed.model_copy(update={"external_ids": {"garmin": "18314668175"}})
    c.clear()
    c._apply_read(ReadResult(dives=[exported], files=[("/x/18314668175.zip", "fit")], sources=["/x/18314668175.zip"]))
    assert c.selected["location"] == "Live copy" and c.selected["external_ids"] == "garmin 18314668175"
    assert c.selected["enriched"].endswith("from the Garmin cache (live@example.org)")
    # the pick is the remembered one only while the account exists; the Sync page's is the fallback
    accounts.select("dives", "garmin", "gone@example.org")
    accounts.select("sync", "garmin", "test@example.org")
    assert module.preferred_garmin_account() == "live@example.org"       # dives page: first configured
    creds_store.save_credentials_model(CredentialsModel(garmin=[GarminCredentials(username="test@example.org", password="p")]))
    assert module.preferred_garmin_account() == "test@example.org"
    assert preferences.get_selected_account("dives", "garmin") == "gone@example.org"


def test_convert_controller_appends_removes_and_clears(qapp, scratch_data_dir, fake_keyring, tmp_path):
    """I9b: Open adds to the list, skipping a dive already in it (same file,
    or same start and number); Remove takes the selected dives off (one or
    several), selects the row that moved into the first one's place, keeps
    the details pane, the enrichment and the files in step; Clear empties
    the list. Nothing on disk is touched."""
    import shutil
    from desktop.controllers.convert import ConvertController
    from src.core import garmin_files, layout
    from src.core.convert import formats
    from tests.test_convert_enrich import cache_payload
    handwritten = os.path.join(FIXTURES, "ssrf", "handwritten.ssrf")
    synthetic = os.path.join(FIXTURES, "ssrf", "synthetic.ssrf")
    uddf = os.path.join(FIXTURES, "uddf", "subsurface_sync.uddf")
    copy = tmp_path / "copy.ssrf"                        # the same dives under another name
    shutil.copy(handwritten, copy)
    c = _loaded_convert(qapp, handwritten)
    read = formats.read_file(handwritten)
    n, w = len(read.dives), f" {len(read.warnings)} warnings." if read.warnings else ""
    assert c.diveCount == n and c.files == ["handwritten.ssrf"] and c.message == f"{n} dives from 1 file.{w}"
    # the same file again: nothing added; a copy of it: the dives are the same ones
    c.openFiles([_file_url(handwritten)])
    assert wait_until(qapp, lambda: not c.busy)
    assert c.diveCount == n and c.message == f"0 dives from 1 file added; {n} in the list. {n} already loaded.{w}"
    c.openFiles([_file_url(copy)])
    assert wait_until(qapp, lambda: not c.busy)
    assert c.diveCount == n and c.files == ["handwritten.ssrf"] and f"{n} already loaded." in c.message
    # another file adds its dives after the loaded ones and selects the first new one
    c.clickRow(1, False, False)
    c.openFiles([_file_url(synthetic), _file_url(uddf)])
    assert wait_until(qapp, lambda: not c.busy)
    more = formats.read_files([synthetic, uddf])
    added = len(more.dives)
    assert c.diveCount == n + added and c.files == ["handwritten.ssrf", "synthetic.ssrf", "subsurface_sync.uddf"]
    assert c.message == f"{added} dives from 2 files added; {n + added} in the list."
    assert c.selection == [n] and c.currentRow == n and c.dives[n]["source"] == "synthetic.ssrf"
    assert [r["source"] for r in c.dives][:n] == ["handwritten.ssrf"] * n
    # remove one: the next row takes its place and is selected; the pane follows
    c.clickRow(0, False, False)
    second = c.dives[1]
    c.removeSelected()
    assert c.diveCount == n + added - 1 and c.dives[0]["date"] == second["date"] and c.dives[0]["index"] == 0
    assert c.selection == [0] and c.currentRow == 0 and c.selected["title"].startswith(second["date"])
    assert c.message == f"Removed 1 dive; {n + added - 1} in the list."
    # remove several, including the last row: the selection lands on the new last row
    last = c.diveCount - 1
    c.clickRow(last, False, False)
    c.clickRow(2, True, False)
    c.removeSelected()
    assert c.diveCount == n + added - 3 and c.selection == [2] and c.currentRow == 2
    assert c.message == f"Removed 2 dives; {n + added - 3} in the list."
    # a file none of whose dives is left is forgotten, so it can be opened again
    c.selectAll()
    c.clickRow(0, True, False)                           # keep the first (a handwritten dive)
    c.removeSelected()
    assert c.diveCount == 1 and c.files == ["handwritten.ssrf"] and c.dives[0]["source"] == "handwritten.ssrf"
    c.openFiles([_file_url(synthetic)])
    assert wait_until(qapp, lambda: not c.busy)
    assert c.diveCount == 1 + len(formats.read_file(synthetic).dives) and c.files == ["handwritten.ssrf", "synthetic.ssrf"]
    # nothing selected: nothing happens; the files are still on disk
    c._selection = []
    c.removeSelected()
    assert c.diveCount > 1 and os.path.exists(handwritten) and os.path.exists(copy)
    # two files of the same name in different folders are two files: removing one's dives forgets that one only
    c.clear()
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir(); b_dir.mkdir()
    shutil.copy(handwritten, a_dir / "log.ssrf")
    shutil.copy(synthetic, b_dir / "log.ssrf")
    c.openFiles([_file_url(a_dir / "log.ssrf"), _file_url(b_dir / "log.ssrf")])
    assert wait_until(qapp, lambda: not c.busy)
    assert c.files == ["log.ssrf", "log.ssrf"] and c.diveCount == n + len(formats.read_file(synthetic).dives)
    c.clickRow(0, False, False)
    c.clickRow(n - 1, False, True)
    c.removeSelected()
    assert c._files == [str(b_dir / "log.ssrf")] and all(r["source"] == "log.ssrf" for r in c.dives)
    c.openFiles([_file_url(a_dir / "log.ssrf")])
    assert wait_until(qapp, lambda: not c.busy)
    assert c.diveCount == n + len(formats.read_file(synthetic).dives) and c.message.startswith(f"{n} dives from 1 file added;")
    # the enrichment follows the list: the filled dive keeps its fill after a remove above it
    folder = layout.dives_dir("garmin", "me@example.org", str(scratch_data_dir))
    garmin_files.write_cached(folder, cache_payload(24449823373, location="House Reef"))
    from src.core.convert.formats import ReadResult
    from src.core.models import GasMixture, UnifiedDive
    fit = UnifiedDive(date_time=datetime(2026, 8, 29, 9, 56, 11), duration=2780, max_depth=11.92, dive_number=38,
                      external_ids={"garmin": "24449823373"}, gas_mixtures=[GasMixture(oxygen=21.0)])
    c._apply_read(ReadResult(dives=[fit], files=[("/x/a.fit", "fit")], sources=["/x/a.fit"]), append=True)
    row = c.diveCount - 1
    assert c.enrichedCount == 1 and c.dives[row]["location"] == "House Reef" and c.message.endswith("1 filled from the Garmin cache.")
    c.clickRow(0, False, False)
    c.removeSelected()
    assert c.enrichedCount == 1 and c.dives[row - 1]["location"] == "House Reef" and c.message.endswith("1 filled from the Garmin cache.")
    c.clickRow(row - 1, False, False)
    assert c.selected["enriched"].startswith("site, buddy") and fit.location is None
    # removing everything: an empty list, an empty pane
    c.selectAll()
    c.removeSelected()
    assert c.diveCount == 0 and c.selected == {} and c.selection == [] and c.files == [] and c.message == f"Removed {row} dives."
    assert c.enrichedCount == 0
    # clear
    c._apply_read(ReadResult(dives=[fit], files=[("/x/a.fit", "fit")], sources=["/x/a.fit"]), append=True)
    assert c.diveCount == 1 and c.selection == [0]
    c.clear()
    assert c.diveCount == 0 and c.files == [] and c.selected == {} and c.message == "" and c._dives_as_read == [] and c._cached == {}


def test_convert_controller_enriches_the_fit_fixture(qapp, scratch_data_dir, fake_keyring, tmp_path):
    """The two-transmitter FIT under the name Connect's cache gives it (the
    activity id is the last part), with a cache entry for that id: the dive
    gets its site, buddy, notes, weight and visibility; the tank volumes the
    watch wrote (the transmitter profiles) stay the file's, as do the
    profile and the pressures (skipped while the fixture is absent)."""
    import shutil
    from src.core import garmin_files, layout
    from tests.test_convert_enrich import cache_payload
    source = os.path.join(FIT_FIXTURES, "two_tanks.fit")
    if not os.path.exists(source):
        pytest.skip("FIT fixture not present")
    path = tmp_path / "38_2026-08-29_095611_24449823373.fit"
    shutil.copy(source, path)
    folder = layout.dives_dir("garmin", "me@example.org", str(scratch_data_dir))
    garmin_files.write_cached(folder, cache_payload(24449823373, location="House Reef", buddy="Kim", tank_sizes=(12.0, 12.0)))
    c = _loaded_convert(qapp, path)
    assert c.diveCount == 1 and c.enrichedCount == 1 and c.warnings == []
    sel = c.selected
    assert sel["external_ids"] == "garmin 24449823373" and sel["location"] == "House Reef" and sel["buddy"] == "Kim"
    assert sel["enriched"] == "site, buddy, notes, weight, visibility from the Garmin cache (me@example.org)"
    assert [t["volume"] for t in sel["tanks"]] == ["11.1 l", "11.1 l"] and sel["tanks"][0]["name"] == "Tank 1"   # the file's, not the cache's 12
    assert sel["channels"][0] == "tank pressure (tank 1, tank 2)" and sel["gf"] == "40/85" and sel["sample_count"] > 100
    assert c.dives[0]["location"] == "House Reef" and c.suggestedFileName("uddf").endswith(" dive 38.uddf")
    c.setEnrichFromCache(False)
    assert c.selected["location"] == "" and [t["volume"] for t in c.selected["tanks"]] == ["11.1 l", "11.1 l"]


def test_convert_controller_opens_the_garmin_fit_cache(qapp, scratch_data_dir, fake_keyring, tmp_path):
    """I9c, Open Cached: the folder is the preferred Garmin account's FIT
    cache under DATA_DIR (`layout.garmin_fit_dir`), as a file URL; the button
    is off without an account, without the folder and without a .fit file
    in it, with a tip saying why (the Garmin page's Download dives fills
    it); `refreshCache` picks up a download made meanwhile; opening from
    the cache adds the dives like Open but leaves the remembered last folder
    alone, so a Save after it does not land in the data folder."""
    import shutil
    from PySide6.QtCore import QUrl
    from desktop import accounts, preferences
    from desktop import credentials as creds_store
    from desktop.controllers import convert as module
    from desktop.controllers.convert import ConvertController
    from src.core import layout
    from src.core.config import CredentialsModel, GarminCredentials
    # no account: nothing to open
    assert module.garmin_cache_folder("") == "" and module.has_fit_files("") is False
    c = ConvertController()
    changes = []
    c.cacheChanged.connect(lambda: changes.append(1))
    assert c.cacheAccount == "" and c.cacheFolder == "" and c.cacheAvailable is False
    assert c.cacheTip.startswith("No Garmin account is set up.") and "Download dives" in c.cacheTip
    # an account, no cache folder yet: off, the tip names the account and the Garmin page
    creds_store.save_credentials_model(CredentialsModel(garmin=[
        GarminCredentials(username="live@example.org", password="p"), GarminCredentials(username="test@example.org", password="p")]))
    accounts.select("dives", "garmin", "test@example.org")
    c.refreshCache()
    assert changes == [1]
    folder = layout.garmin_fit_dir("test@example.org", str(scratch_data_dir))
    assert folder.startswith(str(scratch_data_dir)) and folder.endswith(os.path.join("garmin", "test@example.org", "fit"))
    assert module.garmin_cache_folder("test@example.org") == folder
    assert c.cacheAccount == "test@example.org" and QUrl(c.cacheFolder).toLocalFile() == folder
    assert not os.path.exists(folder) and c.cacheAvailable is False      # nothing is created
    assert c.cacheTip == ("Nothing cached yet for test@example.org: the Garmin page's Download dives fetches the .fit files "
                          "from Garmin Connect, and they are kept here.")
    # the folder without a .fit file is still off; one .fit turns it on
    os.makedirs(folder)
    with open(os.path.join(folder, "notes.txt"), "w") as f:
        f.write("x")
    assert c.cacheAvailable is False
    source = os.path.join(FIT_FIXTURES, "two_tanks.fit")
    cached_fit = os.path.join(folder, "38_2026-08-29_095611_24449823373.FIT")
    if os.path.exists(source):
        shutil.copy(source, cached_fit)
    else:
        with open(cached_fit, "wb") as f:
            f.write(b"not a fit")                 # enough for the button; the read is not what this checks
    c.refreshCache()
    assert c.cacheAvailable is True and module.has_fit_files(folder) is True
    assert c.cacheTip == ("Opens the .fit files this app downloaded from Garmin Connect (the Garmin page's dives) "
                          "for test@example.org. The dives are added to the list.")
    # the other account's cache is another folder
    accounts.select("dives", "garmin", "live@example.org")
    c.refreshCache()
    assert QUrl(c.cacheFolder).toLocalFile() == layout.garmin_fit_dir("live@example.org", str(scratch_data_dir))
    assert c.cacheAvailable is False
    accounts.select("dives", "garmin", "test@example.org")
    c.refreshCache()
    # opening from the cache: the dives are added, the last folder is not the cache
    own = tmp_path / "own"
    own.mkdir()
    preferences.set_convert_folder(str(own))
    cached_ssrf = os.path.join(folder, "copy.ssrf")      # the dialog's filters are the Open dialog's: any format opens
    shutil.copy(os.path.join(FIXTURES, "ssrf", "handwritten.ssrf"), cached_ssrf)
    c.openCachedFiles([_file_url(cached_ssrf)])
    assert c.busy
    assert wait_until(qapp, lambda: not c.busy)
    assert c.diveCount == 5 and c.files == ["copy.ssrf"] and c.message.startswith("5 dives from 1 file.")
    assert preferences.get_convert_folder("") == str(own) and QUrl(c.dialogFolder).toLocalFile() == str(own)
    assert c.suggestedFileUrl("uddf").startswith(QUrl.fromLocalFile(str(own)).toString())
    # Open (the plain one) still remembers; a cancelled cache dialog does nothing
    c.openFiles([_file_url(os.path.join(FIXTURES, "ssrf", "synthetic.ssrf"))])
    assert wait_until(qapp, lambda: not c.busy)
    assert preferences.get_convert_folder("") == os.path.join(FIXTURES, "ssrf")
    c.openCachedFiles([])
    assert not c.busy and preferences.get_convert_folder("") == os.path.join(FIXTURES, "ssrf")
    # a cached FIT read from there is filled from the same account's cache (I9) like any other
    if os.path.exists(source):
        from src.core import garmin_files
        from tests.test_convert_enrich import cache_payload
        garmin_files.write_cached(layout.dives_dir("garmin", "test@example.org", str(scratch_data_dir)),
                                  cache_payload(24449823373, location="House Reef"))
        c.clear()
        c.openCachedFiles([_file_url(cached_fit)])
        assert wait_until(qapp, lambda: not c.busy)
        assert c.diveCount == 1 and c.selected["location"] == "House Reef"
        assert preferences.get_convert_folder("") == os.path.join(FIXTURES, "ssrf")


def test_convert_controller_opens_a_folder(qapp, scratch_data_dir, fake_keyring, tmp_path):
    """I13, Open folder…: every dive file directly in the folder, by
    extension (case-insensitive), in name order, read the way Open files…
    reads several files - a dive already listed is skipped and counted, a
    .xml that is no dive file is named in the warnings; hidden files and
    subfolders are left alone; an empty folder is one plain message; the
    folder becomes the remembered dialog folder either way; a cancelled
    dialog, a missing folder and a busy controller do nothing."""
    import shutil
    from PySide6.QtCore import QUrl
    from desktop import preferences
    from desktop.controllers.convert import ConvertController
    from src.core.convert import formats
    from tests.test_convert_fit import minimal_dive
    handwritten = os.path.join(FIXTURES, "ssrf", "handwritten.ssrf")
    folder = tmp_path / "logs"
    folder.mkdir()
    shutil.copy(handwritten, folder / "handwritten.ssrf")
    shutil.copy(os.path.join(FIXTURES, "uddf", "subsurface_sync.uddf"), folder / "subsurface_sync.uddf")
    (folder / "DIVE.FIT").write_bytes(minimal_dive())                                  # upper case is taken
    (folder / "bad.xml").write_text("<notes/>", encoding="utf-8")                        # named in the warnings
    (folder / "notes.txt").write_text("not a dive")                                      # not a dive extension
    shutil.copy(os.path.join(FIXTURES, "ssrf", "synthetic.ssrf"), folder / ".hidden.ssrf")   # dot-file: skipped
    (folder / "more").mkdir()
    shutil.copy(os.path.join(FIXTURES, "ssrf", "synthetic.ssrf"), folder / "more" / "deeper.ssrf")   # subfolder: not searched
    read = formats.read_file(handwritten)
    n_hand, n_uddf = len(read.dives), len(formats.read_file(os.path.join(FIXTURES, "uddf", "subsurface_sync.uddf")).dives)
    w = len(read.warnings) + 1                                               # the reader's own lines, plus bad.xml
    # the handwritten dives are already in the list from that very file: skipped and counted
    c = _loaded_convert(qapp, str(folder / "handwritten.ssrf"))
    assert c.diveCount == n_hand and preferences.get_convert_folder("") == str(folder)
    preferences.set_convert_folder(str(tmp_path))
    c.openFolder(QUrl.fromLocalFile(str(folder)).toString())
    assert c.busy and c.message.startswith("Reading 4 files…")           # DIVE.FIT, bad.xml, handwritten, subsurface_sync
    assert wait_until(qapp, lambda: not c.busy)
    assert c.diveCount == n_hand + n_uddf + 1
    assert c.files == ["handwritten.ssrf", "DIVE.FIT", "subsurface_sync.uddf"]     # name order, the listed file kept first
    assert c.message == (f"{n_uddf + 1} dives from 3 files added; {c.diveCount} in the list. {n_hand} already loaded. {w} warnings.")
    assert len(c.warnings) == w and [x for x in c.warnings if x.startswith("bad.xml: ")]
    assert not any("hidden" in x or "deeper" in x or "notes" in x for x in c.warnings)
    assert c.selection == [n_hand]                                          # the first dive added
    assert not any(s in ("deeper.ssrf", ".hidden.ssrf") for s in c._sources)
    assert preferences.get_convert_folder("") == str(folder) and QUrl(c.dialogFolder).toLocalFile() == str(folder)
    # the same folder again: everything is already loaded
    c.openFolder(QUrl.fromLocalFile(str(folder)).toString())
    assert wait_until(qapp, lambda: not c.busy)
    assert c.message == f"0 dives from 3 files added; {n_hand + n_uddf + 1} in the list. {n_hand + n_uddf + 1} already loaded. {w} warnings."
    # an empty folder: one message, nothing read, the folder still remembered
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / ".DS_Store").write_bytes(b"\0")
    (empty / "readme.txt").write_text("x")
    c.openFolder(QUrl.fromLocalFile(str(empty)).toString())
    assert not c.busy and c.message == f"No dive files in {empty}" and c.diveCount == n_hand + n_uddf + 1
    assert preferences.get_convert_folder("") == str(empty)
    # a folder whose only dive file is unreadable reads like one bad file through Open files…
    lone = tmp_path / "lone"
    lone.mkdir()
    (lone / "nothing.xml").write_text("<notes/>", encoding="utf-8")
    c.openFolder(QUrl.fromLocalFile(str(lone)).toString())
    assert wait_until(qapp, lambda: not c.busy)
    assert c.message.startswith("Could not read:") and "nothing.xml" in c.message and c.diveCount == n_hand + n_uddf + 1
    # cancelled dialog, a path that is no folder, and a plain path instead of a URL
    c.openFolder("")
    assert not c.busy and c.message.startswith("Could not read:")
    c.openFolder(QUrl.fromLocalFile(str(tmp_path / "missing")).toString())
    assert not c.busy and c.message == f"Not a folder: {tmp_path / 'missing'}"
    c.openFolder(QUrl.fromLocalFile(str(folder / "notes.txt")).toString())
    assert c.message == f"Not a folder: {folder / 'notes.txt'}"
    c.clear()
    c.openFolder(str(folder))
    assert c.busy
    c.openFolder(str(folder))                                               # busy: ignored
    assert wait_until(qapp, lambda: not c.busy)
    assert c.diveCount == n_hand + n_uddf + 1 and c.message == f"{c.diveCount} dives from 3 files. {w} warnings."


def test_convert_controller_reports_unreadable_files(qapp, scratch_data_dir, fake_keyring, tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("not a dive")
    from desktop.controllers.convert import ConvertController
    c = ConvertController()
    c.openFiles([_file_url(bad)])
    assert wait_until(qapp, lambda: not c.busy)
    assert c.diveCount == 0 and c.message.startswith("Could not read:") and "notes.txt" in c.message
    c.openFiles([])                                            # cancelled dialog: nothing happens
    assert not c.busy
    # one bad file among good ones is a warning, not a failure
    c = _loaded_convert(qapp, os.path.join(FIXTURES, "ssrf", "synthetic.ssrf"), bad)
    assert c.diveCount >= 1 and c.files == ["synthetic.ssrf"] and any("notes.txt" in w for w in c.warnings)
    assert c.message.endswith("1 warning.")


def test_convert_controller_saves_one_dive_or_one_file_per_dive(qapp, scratch_data_dir, fake_keyring, tmp_path, monkeypatch):
    """One dive selected: the Save-as dialog's suggested name is I4's
    ``<date> <time> dive <n>.<ext>``; several: one file per dive in the
    folder picked (Q8). The dialogs start in Documents and then in the last
    folder used, remembered in desktop_prefs.json (Q11)."""
    from PySide6.QtCore import QUrl
    from desktop import preferences
    from desktop.controllers import convert as module
    from src.core.convert import formats
    monkeypatch.setattr(module, "documents_folder", lambda: str(tmp_path / "Documents"))
    assert module.ConvertController().dialogFolder == QUrl.fromLocalFile(str(tmp_path / "Documents")).toString()
    c = _loaded_convert(qapp, os.path.join(FIXTURES, "ssrf", "handwritten.ssrf"))
    dives = c._dives
    # opening files makes their folder the next dialog's
    assert c.dialogFolder == QUrl.fromLocalFile(os.path.join(FIXTURES, "ssrf")).toString()
    # one dive: the suggested name and URL, then the write
    c.clickRow(1, False, False)
    name = c.suggestedFileName("uddf")
    assert name == formats.output_file_name(dives[1], "uddf") and name.endswith(".uddf")
    assert c.suggestedFileUrl("uddf") == QUrl.fromLocalFile(os.path.join(FIXTURES, "ssrf", name)).toString()
    out = tmp_path / "exports"
    out.mkdir()
    target = out / name
    c.saveAs("uddf", _file_url(target))
    assert target.exists() and c.message == f"Wrote {target}"
    assert c.saveWarnings == formats.describe_drops([dives[1]], "uddf") == c.targetDrops("uddf")
    # the folder is remembered for the next dialog
    assert preferences.get_convert_folder("x") == str(out) and c.dialogFolder == QUrl.fromLocalFile(str(out)).toString()
    assert c.suggestedFileUrl("ssrf").startswith(QUrl.fromLocalFile(str(out)).toString())
    # several dives: a file per dive into the folder, named like the single one
    c.selectAll()
    assert c.suggestedFileName("ssrf") == "" and c.suggestedFileUrl("ssrf") == ""
    c.saveAs("ssrf", _file_url(out / "ignored.ssrf"))         # the page opens a folder dialog instead
    assert "pick a folder" in c.message and not (out / "ignored.ssrf").exists()
    batch = tmp_path / "batch"
    c.saveEach("ssrf", _file_url(batch))
    written = sorted(os.listdir(batch))
    assert written == sorted(os.path.basename(p) for p in formats.output_paths(dives, "ssrf", batch, overwrite=True))
    assert c.message == f"Wrote {len(dives)} files to {batch}" and preferences.get_convert_folder("x") == str(batch)
    assert all(n.endswith(".ssrf") for n in written) and any(" dive " in n for n in written)
    # writing again never overwrites: the new files get a (2) suffix
    c.saveEach("ssrf", _file_url(batch))
    assert len(os.listdir(batch)) == 2 * len(dives) and any("(2).ssrf" in n for n in os.listdir(batch))
    # a write that fails is reported, not raised
    c.saveEach("ssrf", _file_url(batch / "a-file-not-a-folder.ssrf"))
    (batch / "blocked").write_text("x")
    c.saveEach("ssrf", _file_url(batch / "blocked"))
    assert c.message.startswith("Could not write into")


@pytest.mark.parametrize("ssi_on", [False, True])
def test_convert_page_renders_dives_and_the_sidebar_order(qapp, scratch_data_dir, fake_keyring, monkeypatch, ssi_on):
    """The Dive edit section ("Convert" until I14) sits between Conflicts and Settings (Q10); the page
    instantiates its list, detail pane and chart without a QML warning while
    dives are shown and the selection changes. The SSI parts (the Send to SSI
    button, its plan area, the MySSI card in Settings) follow the
    `ssi.UPLOAD_ENABLED` switch: hidden while it is off (the shipped state
    since 2026-10-02), shown when it is on."""
    from PySide6.QtCore import QMetaObject, QObject
    from desktop import app as desktop_app
    from desktop import logging_bridge
    from desktop import preferences
    from src.core import dive_cache
    from src.core.convert import formats
    from src.core.services import ssi
    monkeypatch.setattr(ssi, "UPLOAD_ENABLED", ssi_on)
    monkeypatch.setattr(dive_cache, "list_garmin_dives", lambda *a, **k: [])
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: [])
    warnings = []
    controllers = desktop_app.build_controllers(logging_bridge.install())
    c = controllers["convertController"]
    c._apply_read(formats.read_files([os.path.join(FIXTURES, "ssrf", "handwritten.ssrf"), os.path.join(FIXTURES, "ssrf", "synthetic.ssrf")]))
    engine = desktop_app.create_engine(controllers, "Dive edit",
                                       on_warnings=lambda errs: warnings.extend(str(e.toString()) for e in errs))
    root = engine.rootObjects()[0]
    wait(qapp, 300)
    sections = root.property("sections").toVariant()
    assert "Convert" not in sections                                               # renamed to Dive edit (I14)
    assert sections.index("Dive edit") == sections.index("Conflicts") + 1 == sections.index("Settings") - 1
    assert root.property("currentSection") == sections.index("Dive edit")
    page = root.findChild(QObject, "convertPage")
    assert page is not None and page.findChild(QObject, "convertDiveList") is not None
    assert page.findChild(QObject, "saveTargets").property("count") == 3     # Save as UDDF…, Save as Subsurface…, Save as .fit…
    fit_button = _save_button(page, "fit")
    assert fit_button.property("text") == "Save as .fit…" and fit_button.property("enabled") is True   # .ssrf dives: written from scratch (I12)
    assert _save_button(page, "uddf").property("text") == "Save as UDDF…" and _save_button(page, "uddf").property("enabled") is True
    assert page.findChild(QObject, "fitConnectNote").property("visible") is True
    assert page.findChild(QObject, "fitDropsNote").property("visible") is True
    # the Garmin-cache checkbox (I9): on by default, toggling it reaches the controller and the prefs file
    box = page.findChild(QObject, "enrichCheckBox")
    assert box is not None and box.property("checked") is True and c.enrichFromCache is True
    assert page.findChild(QObject, "enrichedLine").property("visible") is False   # Subsurface dives: nothing filled
    QMetaObject.invokeMethod(box, "click")
    wait(qapp, 100)
    assert c.enrichFromCache is False and preferences.get_convert_enrich() is False
    QMetaObject.invokeMethod(box, "click")
    wait(qapp, 100)
    assert c.enrichFromCache is True and box.property("checked") is True
    # Open Cached (I9c): off without a Garmin account, the tip says so; a cached .fit
    # of a configured account turns it on when the page is shown again
    # Open folder… (I13) sits between Open files… and Open Cached, on while the controller is idle
    buttons = [b for b in page.findChildren(QObject) if b.objectName() in ("openFilesButton", "openFolderButton", "openCachedButton")]
    assert [b.objectName() for b in buttons] == ["openFilesButton", "openFolderButton", "openCachedButton"]
    open_folder = page.findChild(QObject, "openFolderButton")
    assert open_folder.property("text") == "Open folder…" and open_folder.property("enabled") is True
    c._set_busy(True)
    wait(qapp, 50)
    assert open_folder.property("enabled") is False
    c._set_busy(False)
    wait(qapp, 50)
    assert open_folder.property("enabled") is True
    cached = page.findChild(QObject, "openCachedButton")
    assert cached is not None and cached.property("text") == "Open Cached" and cached.property("enabled") is False
    assert c.cacheAvailable is False and "Download dives" in c.cacheTip
    from desktop import accounts
    from desktop import credentials as creds_store
    from src.core import layout
    from src.core.config import CredentialsModel, GarminCredentials
    creds_store.save_credentials_model(CredentialsModel(garmin=[GarminCredentials(username="me@example.org", password="p")]))
    accounts.select("dives", "garmin", "me@example.org")
    fit_folder = layout.garmin_fit_dir("me@example.org", str(scratch_data_dir))
    os.makedirs(fit_folder)
    with open(os.path.join(fit_folder, "1_2026-01-01_100000_1.fit"), "wb") as f:
        f.write(b"x")
    assert cached.property("enabled") is False                                    # not re-read until the page is shown
    root.setProperty("currentSection", sections.index("Settings"))
    wait(qapp, 150)
    root.setProperty("currentSection", sections.index("Dive edit"))
    wait(qapp, 150)
    assert cached.property("enabled") is True and c.cacheAvailable is True
    assert c.cacheTip.startswith("Opens the .fit files this app downloaded from Garmin Connect") and "me@example.org" in c.cacheTip
    assert c.ssiEnabled is ssi_on
    assert page.findChild(QObject, "sendToSsiButton").property("visible") is ssi_on
    assert page.findChild(QObject, "ssiSection").property("visible") is False     # nothing planned yet, either way
    c.clickRow(1, False, False)
    wait(qapp, 150)
    c.selectAll()
    wait(qapp, 150)
    c._set_ssi_message("would show the plan")
    wait(qapp, 100)
    assert page.findChild(QObject, "ssiSection").property("visible") is ssi_on   # the SSI area only when on
    # the list's own buttons (I9b): Remove with a selection, Clear while dives are listed, Delete on the list
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    remove, clear_button = page.findChild(QObject, "removeButton"), page.findChild(QObject, "clearButton")
    assert remove.property("visible") is True and remove.property("enabled") is True and clear_button.property("visible") is True
    assert page.findChild(QObject, "selectAllButton").property("visible") is True
    total = c.diveCount
    c.clickRow(0, False, False)
    wait(qapp, 100)
    QMetaObject.invokeMethod(remove, "click")
    wait(qapp, 150)
    assert c.diveCount == total - 1 and c.selection == [0] and c.message.startswith("Removed 1 dive;")
    dive_list = page.findChild(QObject, "convertDiveList")
    QMetaObject.invokeMethod(dive_list, "forceActiveFocus")
    wait(qapp, 100)
    QTest.keyClick(root, Qt.Key.Key_Delete)
    wait(qapp, 150)
    assert c.diveCount == total - 2
    QTest.keyClick(root, Qt.Key.Key_Backspace)
    wait(qapp, 150)
    assert c.diveCount == total - 3
    c._selection = []
    c.selectionChanged.emit()
    wait(qapp, 100)
    assert remove.property("enabled") is False
    QMetaObject.invokeMethod(clear_button, "click")
    wait(qapp, 150)
    assert c.diveCount == 0 and remove.property("visible") is False and clear_button.property("visible") is False
    c._apply_read(formats.read_files([os.path.join(FIXTURES, "ssrf", "handwritten.ssrf")]))
    wait(qapp, 150)
    c.clear()
    wait(qapp, 150)
    # the Settings page has the MySSI card only when the switch is on
    root.setProperty("currentSection", sections.index("Settings"))
    wait(qapp, 200)
    assert controllers["settingsController"].ssiEnabled is ssi_on
    assert root.findChild(QObject, "ssiCard").property("visible") is ssi_on
    assert warnings == [], warnings
    engine.deleteLater()
    wait(qapp, 50)


def test_profile_chart_is_one_component_shared_by_both_pages(qapp):
    """DivesPage draws its depth profile through ProfileChart.qml (the Canvas
    was taken out of it for the Convert page); the component paints a sample
    list and hides itself for fewer than two samples."""
    from PySide6.QtCore import QUrl
    from PySide6.QtQml import QQmlComponent, QQmlEngine
    from desktop.app import QML_DIR
    dives_page = open(os.path.join(QML_DIR, "DivesPage.qml"), encoding="utf-8").read()
    convert_page = open(os.path.join(QML_DIR, "ConvertPage.qml"), encoding="utf-8").read()
    assert "ProfileChart {" in dives_page and "ProfileChart {" in convert_page
    assert "Canvas {" not in dives_page and "Canvas {" not in convert_page
    assert "ProfileChart 1.0 ProfileChart.qml" in open(os.path.join(QML_DIR, "qmldir"), encoding="utf-8").read()
    engine = QQmlEngine()
    engine.addImportPath(QML_DIR)
    component = QQmlComponent(engine, QUrl.fromLocalFile(os.path.join(QML_DIR, "ProfileChart.qml")))
    assert component.status() == QQmlComponent.Status.Ready, [str(e.toString()) for e in component.errors()]
    chart = component.create()
    # hasProfile drives visible (an item outside a window reports its effective visibility, always false here)
    assert chart is not None and chart.property("hasProfile") is False
    chart.setProperty("samples", [{"time": 0, "depth": 0.0}, {"time": 60, "depth": 12.5}, {"time": 120, "depth": 0.0}])
    wait(qapp, 50)
    assert chart.property("hasProfile") is True
    chart.setProperty("samples", [{"time": 0, "depth": 0.0}])
    assert chart.property("hasProfile") is False
    chart.deleteLater()
    engine.deleteLater()
    wait(qapp, 30)


# ---------------------------------------------------------------- J4: the Convert page and the profile chart follow the units setting

def _units_dives():
    """Two dives as a reader gives them (SI): one with every unit-bearing
    field and a profile, one whose weight and visibility the service holds
    in pounds / feet."""
    from src.core.models import GasMixture, UnifiedDive, UnifiedSample
    si = UnifiedDive(date_time=datetime(2026, 3, 1, 10), duration=2700, max_depth=18.5, avg_depth=12.0, dive_number=7,
                     temp_min=28.0, temp_max=29.5, weight=4.0, weight_unit="kilogram", visibility=15.0, visibility_unit="meter",
                     gas_mixtures=[GasMixture(oxygen=32.0, tank_volume=12.0, start_pressure=200.0, end_pressure=50.0, tank_name="S80")],
                     samples=[UnifiedSample(time=0, depth=0.0, temp=29.0), UnifiedSample(time=600, depth=18.5, temp=28.0),
                              UnifiedSample(time=2700, depth=0.0, temp=29.0)])
    own = UnifiedDive(date_time=datetime(2026, 3, 2, 10), duration=1800, max_depth=10.0, dive_number=8,
                      weight=2.0, weight_unit="pound", visibility=30.0, visibility_unit="feet")
    return [si, own]


def test_convert_page_shows_the_chosen_units_and_follows_a_switch(qapp, scratch_data_dir, fake_keyring):
    """The Convert page's list and detail pane (plans/units.md J4): metric
    shows every value exactly as before (trimmed decimals, "12 l"), imperial
    converts and rounds like the dive computer (whole ft, psi, °F; cuft and
    lb with one decimal), a dive's own pounds / feet are shown in the chosen
    system too, the profile samples stay metres, a switch at runtime rebuilds
    rows and selection at once, and the dives themselves never change."""
    from desktop.controllers import convert as module
    from desktop.controllers.convert import ConvertController
    from desktop.controllers.units import UnitsController
    from src.core.convert.formats import ReadResult
    units = UnitsController()
    c = ConvertController(units=units)
    emitted = []
    c.divesChanged.connect(lambda: emitted.append("dives"))
    c.selectionChanged.connect(lambda: emitted.append("selection"))
    c._apply_read(ReadResult(dives=_units_dives(), files=[("/x/a.uddf", "uddf"), ("/x/b.uddf", "uddf")]))
    tank = lambda: [(t["volume"], t["start_pressure"], t["end_pressure"]) for t in c.selected["tanks"]]
    detail = lambda: {k: c.selected[k] for k in ("max_depth", "avg_depth", "temp_min", "temp_max", "weight", "visibility")}
    # metric: the strings as before Track J
    assert [r["max_depth"] for r in c.dives] == ["18.5 m", "10 m"]
    metric = detail()
    assert metric == {"max_depth": "18.5 m", "avg_depth": "12 m", "temp_min": "28 °C", "temp_max": "29.5 °C",
                      "weight": "4 kg", "visibility": "15 m"}
    assert tank() == [("12 l", "200 bar", "50 bar")]
    assert [s["depth"] for s in c.selected["samples"]] == [0.0, 18.5, 0.0]
    c.clickRow(1, False, False)
    assert c.selected["weight"] == "0.9 kg" and c.selected["visibility"] == "9.1 m"     # the dive's pounds / feet, in metric
    # the switch: rows and selection are re-emitted once each, the values follow
    emitted.clear()
    units.setUnits("imperial")
    assert emitted == ["dives", "selection"]
    assert [r["max_depth"] for r in c.dives] == ["61 ft", "33 ft"]
    assert c.selected["weight"] == "2 lb" and c.selected["visibility"] == "30 ft"       # exactly as the service holds them
    c.clickRow(0, False, False)
    assert detail() == {"max_depth": "61 ft", "avg_depth": "39 ft", "temp_min": "82 °F", "temp_max": "85 °F",
                        "weight": "8.8 lb", "visibility": "49 ft"}
    assert tank() == [("0.4 cuft", "2901 psi", "725 psi")]
    assert [s["depth"] for s in c.selected["samples"]] == [0.0, 18.5, 0.0]              # the chart's samples stay SI
    assert c._dives[0].max_depth == 18.5 and c._dives[0].gas_mixtures[0].start_pressure == 200.0
    # and back: the old strings, exactly
    units.setUnits("metric")
    assert detail() == metric and tank() == [("12 l", "200 bar", "50 bar")] and [r["max_depth"] for r in c.dives] == ["18.5 m", "10 m"]
    # a controller without a UnitsController is metric
    plain = ConvertController()
    plain._apply_read(ReadResult(dives=_units_dives(), files=[("/x/a.uddf", "uddf"), ("/x/b.uddf", "uddf")]))
    assert plain.dives[0]["max_depth"] == "18.5 m" and plain.selected["weight"] == "4 kg"
    # the helper: None is blank in both systems, a dive's own imperial value is shown as held
    assert module._quantity("depth", None, "imperial") == "" and module._quantity("pressure", 200.0, "imperial") == "2901 psi"
    assert module._quantity("weight", 2.0, "imperial", "imperial") == "2 lb" and module._quantity("weight", 2.0, "metric", "imperial") == "0.9 kg"
    assert module._own_system("pound", "pound") == "imperial" and module._own_system(None, "pound") == "metric"


def test_convert_page_shows_the_fixtures_in_both_systems(qapp, scratch_data_dir, fake_keyring):
    """The files the readers give, under imperial and back: the Subsurface
    fixture always, the two-transmitter FIT when it is present. Metric is
    the pre-J4 text of the existing convert tests."""
    from desktop.controllers.convert import ConvertController
    from desktop.controllers.units import UnitsController
    units = UnitsController()
    units.setUnits("imperial")
    c = ConvertController(units=units)
    c.openFiles([_file_url(os.path.join(FIXTURES, "ssrf", "handwritten.ssrf"))])
    assert wait_until(qapp, lambda: not c.busy)
    sel = c.selected
    assert c.dives[0]["max_depth"] == "76 ft" and sel["max_depth"] == "76 ft" and sel["avg_depth"] == "37 ft"
    assert sel["temp_min"] == "86 °F" and (sel["tanks"][0]["volume"], sel["tanks"][0]["start_pressure"], sel["tanks"][0]["end_pressure"]) == ("0.4 cuft", "2915 psi", "870 psi")
    units.setUnits("metric")
    sel = c.selected
    assert c.dives[0]["max_depth"] == "23.1 m" and sel["max_depth"] == "23.1 m" and sel["avg_depth"] == "11.3 m"
    assert sel["temp_min"] == "30 °C" and (sel["tanks"][0]["volume"], sel["tanks"][0]["start_pressure"], sel["tanks"][0]["end_pressure"]) == ("11.1 l", "201 bar", "60 bar")
    path = os.path.join(FIT_FIXTURES, "two_tanks.fit")
    if not os.path.exists(path):
        pytest.skip("FIT fixture not present")
    c.clear()
    units.setUnits("imperial")
    c.openFiles([_file_url(path)])
    assert wait_until(qapp, lambda: not c.busy)
    sel = c.selected
    assert c.dives[0]["max_depth"] == "39 ft" and sel["max_depth"] == "39 ft" and sel["avg_depth"] == "19 ft"
    assert sel["temp_min"] == "86 °F" and sel["temp_max"] == "88 °F"
    assert [(t["volume"], t["start_pressure"], t["end_pressure"]) for t in sel["tanks"]] == [
        ("0.4 cuft", "2963 psi", "2384 psi"), ("0.4 cuft", "3041 psi", "2346 psi")]
    assert sel["channels"][0] == "tank pressure (tank 1, tank 2)" and sel["sample_count"] > 100
    units.setUnits("metric")
    sel = c.selected
    assert c.dives[0]["max_depth"] == "11.9 m" and sel["temp_min"] == "30 °C" and sel["temp_max"] == "31 °C"
    assert [(t["volume"], t["start_pressure"], t["end_pressure"]) for t in sel["tanks"]] == [
        ("11.1 l", "204 bar", "164 bar"), ("11.1 l", "210 bar", "162 bar")]


def test_convert_page_saves_si_whichever_units_are_shown(qapp, scratch_data_dir, fake_keyring, tmp_path):
    """Save as UDDF / Subsurface under imperial: the writers get the SI dives,
    so the files read back in metres and bar at the reader's precision."""
    from desktop.controllers.convert import ConvertController
    from desktop.controllers.units import UnitsController
    from src.core.convert import formats
    units = UnitsController()
    units.setUnits("imperial")
    c = ConvertController(units=units)
    c.openFiles([_file_url(os.path.join(FIXTURES, "ssrf", "handwritten.ssrf"))])
    assert wait_until(qapp, lambda: not c.busy)
    c.clickRow(1, False, False)
    dive = c._dives[1]
    assert c.selected["max_depth"] == "98 ft" and c.selected["tanks"][0]["start_pressure"] == "2900 psi"
    target = tmp_path / "one.uddf"
    c.saveAs("uddf", _file_url(target))
    assert c.message == f"Wrote {target}"
    back = formats.read_files([str(target)]).dives
    assert len(back) == 1 and back[0].max_depth == pytest.approx(dive.max_depth, abs=0.01) == pytest.approx(29.99, abs=0.01)
    assert back[0].gas_mixtures[0].start_pressure == pytest.approx(dive.gas_mixtures[0].start_pressure, abs=0.1)
    assert back[0].temp_min == pytest.approx(28.0) and back[0].weight == pytest.approx(dive.weight, abs=0.01)
    folder = tmp_path / "each"
    c.selectAll()
    c.saveEach("ssrf", _file_url(folder))
    assert c.message.startswith("Wrote ")
    again = formats.read_files([os.path.join(folder, n) for n in sorted(os.listdir(folder))]).dives
    assert [d.max_depth for d in again] == pytest.approx([d.max_depth for d in c._dives], abs=0.01)
    assert [g.start_pressure for d in again for g in d.gas_mixtures] == pytest.approx(
        [g.start_pressure for d in c._dives for g in d.gas_mixtures], abs=0.1)


def test_profile_chart_axis_follows_the_units_setting(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """ProfileChart.qml labels its depth axis through ``depthScale``: metres
    by default (the pre-J4 text, "0 m" / "12.5 m"), feet with the display
    rule's rounding when the pages bind ``units.scales.depth``; the samples
    it is given stay metres. On the Convert page the chart follows the
    Settings switch at once, with no QML warnings."""
    from PySide6.QtCore import QObject, QUrl
    from PySide6.QtQml import QQmlComponent, QQmlEngine
    from desktop import app as desktop_app
    from desktop import logging_bridge
    from desktop.app import QML_DIR
    from desktop.controllers.units import UnitsController
    from src.core import dive_cache
    from src.core.convert.formats import ReadResult
    engine = QQmlEngine()
    engine.addImportPath(QML_DIR)
    component = QQmlComponent(engine, QUrl.fromLocalFile(os.path.join(QML_DIR, "ProfileChart.qml")))
    assert component.status() == QQmlComponent.Status.Ready, [str(e.toString()) for e in component.errors()]
    chart = component.create()
    chart.setProperty("samples", [{"time": 0, "depth": 0.0}, {"time": 60, "depth": 12.5}, {"time": 120, "depth": 0.0}])
    wait(qapp, 30)
    assert chart.property("maxDepth") == 12.5
    assert chart.property("depthAxisTop") == "0 m" and chart.property("depthAxisBottom") == "12.5 m"
    units = UnitsController()
    units.setUnits("imperial")
    chart.setProperty("depthScale", units.scales["depth"])
    wait(qapp, 30)
    assert chart.property("depthAxisTop") == "0 ft" and chart.property("depthAxisBottom") == "41 ft"
    assert chart.property("maxDepth") == 12.5                                  # the samples are still metres
    units.setUnits("metric")
    chart.setProperty("depthScale", units.scales["depth"])
    wait(qapp, 30)
    assert chart.property("depthAxisBottom") == "12.5 m"
    chart.deleteLater()
    engine.deleteLater()
    wait(qapp, 30)
    # the Convert page's chart, bound to the app's units
    monkeypatch.setattr(dive_cache, "list_garmin_dives", lambda *a, **k: [])
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: [])
    controllers = desktop_app.build_controllers(logging_bridge.install())
    units, c = controllers["units"], controllers["convertController"]
    warnings = []
    engine = desktop_app.create_engine(controllers, "Dive edit", on_warnings=lambda errs: warnings.extend(str(e.toString()) for e in errs))
    root = engine.rootObjects()[0]
    wait(qapp, 300)
    c._apply_read(ReadResult(dives=_units_dives(), files=[("/x/a.uddf", "uddf"), ("/x/b.uddf", "uddf")]))
    wait(qapp, 100)
    chart = root.findChild(QObject, "convertPage").findChild(QObject, "profileChart")
    assert chart.property("hasProfile") is True and chart.property("depthAxisBottom") == "18.5 m"
    units.setUnits("imperial")
    wait(qapp, 100)
    assert chart.property("depthAxisBottom") == "61 ft" and chart.property("depthScale")["label"] == "ft"
    assert c.dives[0]["max_depth"] == "61 ft"
    units.setUnits("metric")
    wait(qapp, 100)
    assert chart.property("depthAxisBottom") == "18.5 m"
    assert warnings == [], warnings
    engine.deleteLater()
    wait(qapp, 50)


# ---------------------------------------------------------------- I14: the Dive edit page's editor

def _form(c, **changes):
    """The selected dive's form as the page sends it back, with ``changes`` typed in."""
    form = {k: (list(dict(t) for t in v) if k == "tanks" else v) for k, v in c.selected["form"].items()}
    form.update(changes)
    return form


def test_dive_edit_controller_edits_a_hand_logged_dive(qapp, scratch_data_dir, fake_keyring, tmp_path):
    """An edit on the card changes the dive in the list at once (the row and
    `selected`), keeps the dive before the first edit for I15's Revert,
    refuses a bad value with a message and nothing changed, locks the three
    profile fields on a recorded dive, follows Remove and Clear, and
    survives the Garmin-cache checkbox (plans/convert.md I14)."""
    from desktop.controllers.convert import ConvertController, form_choices, form_values
    from src.core.convert import formats
    from src.core.models import KNOWN_DIVE_MODES, KNOWN_WATER_TYPES
    c = ConvertController()
    c._apply_read(formats.read_files([os.path.join(FIXTURES, "ssrf", "handwritten.ssrf")]), {})
    edits = []
    c.editChanged.connect(lambda: edits.append(c.editError))
    # the hand-logged dive: nothing locked, every field in the form
    c.clickRow(4, False, False)
    sel = c.selected
    assert sel["locked"] is False and sel["locked_fields"] == [] and sel["form"]["max_depth"] == "5" and sel["form"]["tanks"] == []
    assert sel["form"]["date"] == "2025-07-04" and sel["form"]["time"] == "16:30:00" and sel["form"]["utc_offset"] == ""
    assert sel["form"]["duration"] == "20" and sel["form"]["notes"] == "Logged by hand" and sel["form"]["dive_mode"] == "oc_single_gas"
    assert sel["choices"] == {"dive_mode": [""] + list(KNOWN_DIVE_MODES), "water_type": [""] + list(KNOWN_WATER_TYPES)}
    assert set(sel["form"]) == {"date", "time", "utc_offset", "duration", "surface_interval", "bottom_time", "max_depth", "avg_depth",
                                "temp_min", "temp_max", "temp_avg", "dive_number", "water_density", "gf_low", "gf_high", "cns_start",
                                "cns_end", "lat", "lng", "exit_lat", "exit_lng", "weight", "visibility", "location", "buddy", "notes",
                                "computer_vendor", "computer_model", "computer_serial", "computer_firmware", "deco_model",
                                "dive_mode", "water_type", "tanks"}
    before = c._dives[4]
    assert c.edit(4, _form(c, max_depth="7.5", buddy="Kim", location="Jetty", dive_number="105", water_type="fresh", duration="25",
                           weight="2", visibility="8", gf_low="30", gf_high="70", computer_vendor="Shearwater")) == ""
    assert c.dives[4]["max_depth"] == "7.5 m" and c.dives[4]["location"] == "Jetty" and c.dives[4]["dive_number"] == 105
    assert c.dives[4]["duration"] == "25 min"
    sel = c.selected
    assert sel["max_depth"] == "7.5 m" and sel["buddy"] == "Kim" and sel["water_type"] == "fresh" and sel["gf"] == "30/70"
    assert sel["form"]["max_depth"] == "7.5" and sel["form"]["weight"] == "2" and sel["form"]["visibility"] == "8"
    dive = c._dives[4]
    assert dive.max_depth == 7.5 and dive.duration == 1500 and dive.weight == 2.0 and dive.weight_unit == "kilogram"
    assert dive.visibility == 8.0 and dive.visibility_unit == "meter" and dive.computer_vendor == "Shearwater"
    assert c.originalOf(4) is before and before.max_depth == 5.0 and c.originalOf(3) is None and c.editedRows == [4]
    assert edits == [""]                                         # one editChanged per edit, no error
    # the form sent back unchanged is no edit
    assert c.edit(4, _form(c)) == "" and c._dives[4] is dive and edits == [""]
    # a second edit builds on the first; the original stays the dive as first shown
    assert c.edit(4, _form(c, notes="two\nlines", tanks=[{"tank_name": "S80", "oxygen": "32", "helium": "", "volume": "11.1",
                                                           "start_pressure": "200", "end_pressure": "50", "role": "backGas"}])) == ""
    assert c._dives[4].notes == "two\nlines" and c._dives[4].max_depth == 7.5 and c.originalOf(4) is before
    tank = c._dives[4].gas_mixtures[0]
    assert (tank.tank_name, tank.oxygen, tank.helium, tank.tank_volume, tank.start_pressure, tank.end_pressure, tank.tank_role) == (
        "S80", 32.0, 0.0, 11.1, 200.0, 50.0, "backGas")
    assert c.selected["tanks"][0]["mix"] == "EAN32" and c.selected["form"]["tanks"][0]["start_pressure"] == "200"
    # a bad value: the message, nothing changed, cleared by the next good edit
    assert c.edit(4, _form(c, max_depth="deep")) == "Max depth: not a number: 'deep'"
    assert c.editError == "Max depth: not a number: 'deep'" and c._dives[4].max_depth == 7.5
    assert c.edit(4, _form(c, dive_mode="freedive")).startswith("Dive mode: 'freedive' is not one of")
    assert c.edit(4, _form(c, duration="long")) == "Duration: not a number of minutes: 'long'"
    assert c.edit(4, _form(c, utc_offset="Bangkok")).startswith("Time zone: use a UTC offset")
    assert c.edit(4, _form(c, max_depth="8")) == "" and c.editError == "" and c._dives[4].max_depth == 8.0
    assert c.edit(99, _form(c)) == ""                            # off the list: ignored
    # a recorded dive: the three profile fields locked, the rest editable
    c.clickRow(0, False, False)
    sel = c.selected
    assert sel["locked"] is True and sel["locked_fields"] == ["duration", "max_depth", "avg_depth"]
    assert c.edit(0, _form(c, max_depth="20")) == "max depth: set by the dive profile, not editable"
    assert c._dives[0].max_depth == 23.125 and c.editedRows == [4]
    assert c.edit(0, _form(c, temp_min="29.5", buddy="Sam", bottom_time="40")) == ""
    assert c._dives[0].temp_min == 29.5 and c._dives[0].buddy == "Sam" and c._dives[0].bottom_time == 2400
    assert c._dives[0].max_depth == 23.125 and len(c._dives[0].samples) == 5 and c.editedRows == [0, 4]
    # the helpers
    assert form_values(c._dives[0])["temp_min"] == "29.5" and form_choices(c._dives[0])["water_type"][2] == "salt"
    # the checkbox re-derives the list: the edits stay
    c.setEnrichFromCache(False)
    assert c._dives[4].max_depth == 8.0 and c._dives[0].buddy == "Sam"
    c.setEnrichFromCache(True)
    assert c._dives[4].max_depth == 8.0
    # Remove moves the edited rows down with their dives; Clear forgets them
    c.clickRow(1, False, False)
    c.removeSelected()
    assert c.editedRows == [0, 3] and c._dives[3].max_depth == 8.0 and c.originalOf(3) is before and c.originalOf(4) is None
    c.clickRow(0, False, False)
    c.removeSelected()
    assert c.editedRows == [2] and c.originalOf(0) is None and c._dives[2].max_depth == 8.0
    c.clear()
    assert c.editedRows == [] and c._originals == {} and c.editError == ""


def test_dive_edit_under_imperial_saves_si_and_never_drifts(qapp, scratch_data_dir, fake_keyring):
    """Under imperial the form shows the J3 rounding, a typed value comes
    back as SI at full precision (100 ft -> 30.48 m), a field left as shown
    keeps the stored value exactly, a weight or visibility keeps the dive's
    own unit, the tank table converts per cell, and a units switch re-shows
    the dive without counting as an edit."""
    from desktop.controllers import convert as module
    from desktop.controllers.convert import ConvertController, si_changes
    from desktop.controllers.units import UnitsController
    from src.core.convert import formats
    units = UnitsController()
    units.setUnits("imperial")
    c = ConvertController(units=units)
    c._apply_read(formats.read_files([os.path.join(FIXTURES, "ssrf", "handwritten.ssrf")]), {})
    c.clickRow(1, False, False)
    stored = c._dives[1]
    form = c.selected["form"]
    assert form["max_depth"] == "98" and form["avg_depth"] == "60" and form["temp_min"] == "82" and form["weight"] == "8.0"
    assert form["tanks"] == [{"tank_name": "AL80", "oxygen": "32", "helium": "0", "volume": "0.4", "start_pressure": "2900",
                              "end_pressure": "700", "role": ""}]
    # the form sent back as shown: nothing changes (no drift from the rounded display)
    assert si_changes(stored, form, "imperial") == {} and c.edit(1, _form(c)) == "" and c._dives[1] is stored
    # one cell of the tank table and the weight: converted back, the rest of the dive exactly as it was
    c.clickRow(4, False, False)
    assert c.selected["form"]["max_depth"] == "16"
    assert c.edit(4, _form(c, max_depth="100", weight="10", visibility="20", temp_min="50")) == ""
    dive = c._dives[4]
    assert dive.max_depth == pytest.approx(30.48) and dive.weight == pytest.approx(4.5359237) and dive.weight_unit == "kilogram"
    assert dive.visibility == pytest.approx(6.096) and dive.visibility_unit == "meter" and dive.temp_min == pytest.approx(10.0)
    assert dive.avg_depth == 3.0 and dive.duration == 1200 and dive.notes == "Logged by hand"
    assert c.selected["form"]["max_depth"] == "100" and c.selected["form"]["weight"] == "10.0" and c.dives[4]["max_depth"] == "100 ft"
    untouched = {k: v for k, v in dive.model_dump().items() if k not in ("max_depth", "weight", "weight_unit", "visibility", "visibility_unit", "temp_min")}
    assert untouched == {k: v for k, v in c.originalOf(4).model_dump().items() if k in untouched}
    c.clickRow(1, False, False)
    tank = dict(c.selected["form"]["tanks"][0], start_pressure="3000", volume="0.5")
    assert c.edit(1, _form(c, tanks=[tank])) == ""
    gm = c._dives[1].gas_mixtures[0]
    assert gm.start_pressure == pytest.approx(206.843, abs=1e-3) and gm.tank_volume == pytest.approx(14.158, abs=0.01)
    assert gm.end_pressure == stored.gas_mixtures[0].end_pressure == 48.263 and gm.tank_name == "AL80" and gm.oxygen == 32.0
    assert c._dives[1].weight == stored.weight == 3.629                 # the weight field untouched: as stored
    # a dive the service holds in pounds / feet: typed in the shown system, kept in its own
    from src.core.convert.formats import ReadResult
    own = ConvertController(units=units)
    own._apply_read(ReadResult(dives=_units_dives(), files=[("/x/a.uddf", "uddf"), ("/x/b.uddf", "uddf")]), {})
    own.clickRow(1, False, False)
    assert own.selected["form"]["weight"] == "2" and own.selected["form"]["visibility"] == "30"
    assert own.edit(1, _form(own, weight="3", visibility="")) == ""
    assert (own._dives[1].weight, own._dives[1].weight_unit, own._dives[1].visibility, own._dives[1].visibility_unit) == (3.0, "pound", None, None)
    units.setUnits("metric")
    assert own.selected["form"]["weight"] == "1.4"                     # 3 lb shown in kilograms
    assert own.edit(1, _form(own, weight="2")) == "" and own._dives[1].weight == pytest.approx(4.4092452) and own._dives[1].weight_unit == "pound"
    # the switch: the form is re-expressed, the stored values untouched, the rows re-emitted
    emitted = []
    c.divesChanged.connect(lambda: emitted.append("dives"))
    c.selectionChanged.connect(lambda: emitted.append("selection"))
    units.setUnits("imperial")
    units.setUnits("metric")
    assert emitted == ["dives", "selection"] * 2
    assert c.selected["form"]["tanks"][0]["start_pressure"] == "206.842718" and c.selected["form"]["tanks"][0]["end_pressure"] == "48.263"
    assert c._dives[1].gas_mixtures[0].start_pressure == pytest.approx(206.843, abs=1e-3)
    assert c.edit(1, _form(c)) == "" and c.editedRows == [1, 4]       # the re-shown form is no edit
    c.clickRow(4, False, False)
    assert c.selected["form"]["max_depth"] == "30.48" and c.selected["form"]["weight"] == "4.535924"
    assert c.edit(4, _form(c)) == "" and c._dives[4].max_depth == pytest.approx(30.48)
    assert module._minutes_text(2780) == "46.33" and module._minutes_text(None) == ""
    assert module._form_number("depth", 4.535923700100354, "metric") == "4.535924" and module._form_number("depth", 30.48, "imperial") == "100"


def test_dive_edit_shifts_the_profile_with_the_start_time(qapp, scratch_data_dir, fake_keyring):
    """A date, time or time zone edit moves the whole dive: every sample and
    event keeps its offset and so moves by the same delta as the start, the
    duration stays, the UTC start follows, and the list row shows the new
    time (UWMedia's Adjust time)."""
    from datetime import timedelta
    from desktop.controllers.convert import ConvertController
    from src.core.convert import formats
    c = ConvertController()
    c._apply_read(formats.read_files([os.path.join(FIXTURES, "ssrf", "handwritten.ssrf")]), {})
    c.clickRow(0, False, False)
    before = c._dives[0]
    offsets = [s["time"] for s in c.selected["samples"]]
    instants = [before.date_time + timedelta(seconds=t) for t in offsets]
    events = [before.date_time + timedelta(seconds=e.time) for e in before.events]
    assert len(offsets) == 5 and len(events) == 3 and before.date_time_utc is None and c.selected["form"]["utc_offset"] == ""
    assert c.edit(0, _form(c, date="2025-07-02", time="09:42:30", utc_offset="+07:00")) == ""
    dive = c._dives[0]
    delta = timedelta(days=1, hours=1, minutes=30)
    assert dive.date_time == datetime(2025, 7, 2, 9, 42, 30) and dive.timezone == "Etc/GMT-7"
    assert dive.date_time_utc == datetime(2025, 7, 2, 2, 42, 30) and dive.duration == before.duration
    assert [s["time"] for s in c.selected["samples"]] == offsets                       # the chart's offsets: unchanged
    assert [dive.date_time + timedelta(seconds=t) for t in offsets] == [t + delta for t in instants]
    assert [dive.date_time + timedelta(seconds=e.time) for e in dive.events] == [t + delta for t in events]
    assert c.dives[0]["date"] == "2025-07-02" and c.dives[0]["time"] == "09:42" and c.selected["title"].startswith("2025-07-02 09:42")
    assert {k: c.selected["form"][k] for k in ("date", "time", "utc_offset")} == {"date": "2025-07-02", "time": "09:42:30", "utc_offset": "+07:00"}
    assert c.selected["date_time"] == "2025-07-02 09:42:30" and c.selected["timezone"] == "Etc/GMT-7"
    # the zone alone: the wall clock stays, the UTC instant moves
    assert c.edit(0, _form(c, utc_offset="+08:00")) == ""
    assert c._dives[0].date_time == datetime(2025, 7, 2, 9, 42, 30) and c._dives[0].date_time_utc == datetime(2025, 7, 2, 1, 42, 30)
    assert c.selected["form"]["utc_offset"] == "+08:00"
    # the time without seconds; the original is the dive as first shown
    assert c.edit(0, _form(c, time="10:00")) == "" and c._dives[0].date_time == datetime(2025, 7, 2, 10, 0, 0)
    assert c.originalOf(0) is before and before.date_time == datetime(2025, 7, 1, 8, 12, 30)


def test_dive_edit_save_as_writes_the_edited_dive(qapp, scratch_data_dir, fake_keyring, tmp_path):
    """Save as UDDF / Subsurface after an edit writes the edited dive, one
    file or one per dive; the files read back with the edited values and
    the untouched ones; nothing is written to the file opened."""
    from desktop.controllers.convert import ConvertController
    from src.core.convert import formats
    source = os.path.join(FIXTURES, "ssrf", "handwritten.ssrf")
    original_bytes = open(source, "rb").read()
    c = ConvertController()
    c.openFiles([_file_url(source)])
    assert wait_until(qapp, lambda: not c.busy)
    c.clickRow(4, False, False)
    assert c.edit(4, _form(c, max_depth="9.5", buddy="Kim", location="Jetty", date="2025-07-05", time="07:00",
                           tanks=[{"tank_name": "S80", "oxygen": "32", "helium": "0", "volume": "11.1", "start_pressure": "200", "end_pressure": "50", "role": ""}])) == ""
    target = tmp_path / "edited.uddf"
    assert c.suggestedFileName("uddf") == "2025-07-05 0700.uddf"            # the edited start names the file (no dive number)
    c.saveAs("uddf", _file_url(target))
    assert c.message == f"Wrote {target}"
    back = formats.read_files([str(target)]).dives
    assert len(back) == 1 and back[0].max_depth == pytest.approx(9.5) and back[0].buddy == "Kim" and back[0].location == "Jetty"
    assert back[0].date_time == datetime(2025, 7, 5, 7, 0) and back[0].duration == 1200 and back[0].notes == "Logged by hand"
    assert back[0].gas_mixtures[0].oxygen == 32.0 and back[0].gas_mixtures[0].start_pressure == pytest.approx(200.0)
    c.clickRow(0, False, False)
    assert c.edit(0, _form(c, buddy="Sam", temp_min="29")) == ""
    folder = tmp_path / "each"
    c.selectAll()
    c.saveEach("ssrf", _file_url(folder))
    assert c.message.startswith("Wrote 5 files")
    again = formats.read_files([os.path.join(folder, n) for n in sorted(os.listdir(folder))]).dives
    by_number = {d.dive_number: d for d in again}
    assert by_number[101].buddy == "Sam" and by_number[101].temp_min == pytest.approx(29.0) and by_number[101].max_depth == pytest.approx(23.125, abs=0.01)
    edited = next(d for d in again if d.location == "Jetty")
    assert edited.max_depth == pytest.approx(9.5) and edited.buddy == "Kim" and len(edited.gas_mixtures) == 1
    assert open(source, "rb").read() == original_bytes


def test_dive_edit_page_editor(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """The page from main.qml under "Dive edit": the editor's fields show
    the selected dive, a typed value committed with editingFinished reaches
    the dive, the three profile fields are read-only on a recorded dive
    and editable on a hand-logged one, a choice and a tank row commit too,
    an error shows on the card, a units switch is no edit, several dives
    selected show the count instead of the editor, and no QML warning."""
    from PySide6.QtCore import QMetaObject, QObject
    from desktop import app as desktop_app
    from desktop import logging_bridge
    from src.core import dive_cache
    from src.core.convert import formats
    monkeypatch.setattr(dive_cache, "list_garmin_dives", lambda *a, **k: [])
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: [])
    warnings = []
    controllers = desktop_app.build_controllers(logging_bridge.install())
    c, units = controllers["convertController"], controllers["units"]
    c._apply_read(formats.read_files([os.path.join(FIXTURES, "ssrf", "handwritten.ssrf")]), {})
    engine = desktop_app.create_engine(controllers, "Dive edit", on_warnings=lambda errs: warnings.extend(str(e.toString()) for e in errs))
    root = engine.rootObjects()[0]
    wait(qapp, 300)
    assert root.property("sections").toVariant()[6] == "Dive edit"
    page = root.findChild(QObject, "convertPage")
    field = lambda name: page.findChild(QObject, name)
    commit = lambda name, text: (field(name).setProperty("text", text), QMetaObject.invokeMethod(field(name), "editingFinished"), wait(qapp, 100))
    # the recorded dive selected on open: the profile fields locked, the rest editable, the values shown
    depth, duration, avg = field("edit-max-depth"), field("edit-duration"), field("edit-avg-depth")
    assert depth.property("readOnly") is True and duration.property("readOnly") is True and avg.property("readOnly") is True
    assert depth.property("label") == "Max depth (from the dive profile)" and depth.property("text") == "23.125"
    assert field("edit-date").property("text") == "2025-07-01" and field("edit-time").property("text") == "08:12:30"
    assert field("edit-location").property("text") == "Invented Wall" and field("edit-buddy").property("readOnly") is False
    assert field("edit-dive-mode").property("currentText") == "oc_single_gas" and field("edit-water-type").property("currentText") == "salt"
    assert page.findChild(QObject, "tankRows").property("count") == 1 and field("edit-notes") is not None
    assert field("editError").property("visible") is False
    # a committed field reaches the dive; the title and the row follow
    commit("edit-buddy", "Sam")
    assert c._dives[0].buddy == "Sam" and c.editedRows == [0]
    commit("edit-time", "09:00")
    assert c._dives[0].date_time == datetime(2025, 7, 1, 9, 0) and c.dives[0]["time"] == "09:00"
    assert field("edit-time").property("text") == "09:00:00"                 # the controller's echo
    # a bad value: the error on the card, cleared by the next good one
    commit("edit-temp-min", "warm")
    assert field("editError").property("visible") is True and "Water temp (min): not a number" in field("editError").property("text")
    commit("edit-temp-min", "29")
    assert field("editError").property("visible") is False and c._dives[0].temp_min == 29.0
    # the hand-logged dive: editable depths, a typed depth lands in the row
    c.clickRow(4, False, False)
    wait(qapp, 150)
    assert depth.property("readOnly") is False and depth.property("label") == "Max depth (m)" and depth.property("text") == "5"
    assert page.findChild(QObject, "tankRows").property("count") == 0
    commit("edit-max-depth", "7.5")
    assert c._dives[4].max_depth == 7.5 and c.dives[4]["max_depth"] == "7.5 m"
    # a choice: pick the second word and commit
    mode = field("edit-dive-mode")
    mode.setProperty("currentIndex", 4)                                       # ccr
    QMetaObject.invokeMethod(mode, "picked")
    wait(qapp, 100)
    assert c._dives[4].dive_mode == "ccr" and mode.property("currentText") == "ccr"
    # + Add tank adds a row of air and commits it
    QMetaObject.invokeMethod(page.findChild(QObject, "addTankButton"), "click")
    wait(qapp, 150)
    assert len(c._dives[4].gas_mixtures) == 1 and c._dives[4].gas_mixtures[0].oxygen == 21.0
    assert page.findChild(QObject, "tankRows").property("count") == 1
    # units: labels and values follow, nothing is edited by the switch
    edited = list(c.editedRows)
    units.setUnits("imperial")
    wait(qapp, 150)
    assert depth.property("label") == "Max depth (ft)" and depth.property("text") == "25" and field("edit-weight").property("label") == "Weight (lb)"
    headings = page.findChild(QObject, "tankHeadings")
    assert [ch.property("text") for ch in headings.children() if ch.property("text") is not None] == [
        "Name", "O2 %", "He %", "Volume (cuft)", "Start (psi)", "End (psi)", "Role"]
    QMetaObject.invokeMethod(depth, "editingFinished")
    wait(qapp, 100)
    assert c.editedRows == edited and c._dives[4].max_depth == 7.5
    commit("edit-max-depth", "100")
    assert c._dives[4].max_depth == pytest.approx(30.48) and depth.property("text") == "100"
    units.setUnits("metric")
    wait(qapp, 150)
    assert depth.property("text") == "30.48"
    # several selected: the count, no editor; one again: the editor
    title = page.findChild(QObject, "detailTitle")
    c.selectAll()
    wait(qapp, 150)
    assert title.property("text") == "5 dives selected" and page.property("editing") is False
    c.clickRow(4, False, False)
    wait(qapp, 150)
    assert page.property("editing") is True and title.property("text").startswith("2025-07-04 16:30")
    assert warnings == [], warnings
    engine.deleteLater()
    wait(qapp, 50)


def test_dive_edit_on_a_garmin_fit(qapp, scratch_data_dir, fake_keyring):
    """The two-transmitter FIT (untracked fixture, skipped when absent): the
    zone shows as its UTC offset, the profile fields are locked, a zone
    change moves the UTC start and not the wall clock, the tanks edit per
    cell, and Save as .fit (I14b) patches the dive's own file into a new
    one that reads back with the edits: the suggested name beside the
    original, the opened file refused as the target and never changed,
    the not-in-a-FIT list before the save."""
    from PySide6.QtCore import QUrl
    from desktop.controllers.convert import ConvertController
    from src.core.convert import formats
    path = os.path.join(FIT_FIXTURES, "two_tanks.fit")
    if not os.path.exists(path):
        pytest.skip("FIT fixture not present")
    original_bytes = open(path, "rb").read()
    c = ConvertController()
    c.openFiles([_file_url(path)])
    assert wait_until(qapp, lambda: not c.busy)
    sel = c.selected
    assert sel["locked"] is True and sel["form"]["utc_offset"] == "+07:00" and sel["form"]["time"] == "09:56:11"
    assert sel["form"]["dive_number"] == "38" and sel["form"]["surface_interval"] == "8288.77" and sel["form"]["gf_low"] == "40"
    assert sel["form"]["computer_model"] == "Descent X50i" and sel["form"]["water_density"] == "1025" and sel["form"]["cns_end"] == "0"
    assert [t["start_pressure"] for t in sel["form"]["tanks"]] == ["204.29", "209.66"]       # metric: as stored
    before = c._dives[0]
    assert c.edit(0, _form(c, utc_offset="+08:00", dive_number="39", tanks=[dict(sel["form"]["tanks"][0], end_pressure="160"), sel["form"]["tanks"][1]])) == ""
    dive = c._dives[0]
    assert dive.date_time == before.date_time and dive.date_time_utc == datetime(2026, 8, 29, 1, 56, 11) and dive.timezone == "Etc/GMT-8"
    assert dive.dive_number == 39 and dive.gas_mixtures[0].end_pressure == 160.0 and dive.gas_mixtures[1] == before.gas_mixtures[1]
    assert len(dive.samples) == len(before.samples) and dive.samples[-1].time == before.samples[-1].time and dive.events == before.events
    # Save as .fit: offered for this dive, named beside the original, the drops list of what no FIT holds
    assert [t["id"] for t in c.targets] == ["uddf", "ssrf", "fit"] and c.targets[2]["title"] == "Save as .fit…" and c.targets[2]["patch"] is True
    assert c.fitSaveTip == "" and c.fitDrops == []
    assert c.suggestedFileName("fit") == "two_tanks (edited).fit"
    assert c.suggestedFileUrl("fit") == QUrl.fromLocalFile(os.path.join(FIT_FIXTURES, "two_tanks (edited).fit")).toString()
    assert c.edit(0, _form(c, buddy="Kim", location="Jetty", weight="4")) == ""
    assert c.fitDrops == ["site", "buddy", "weight"] and c.targetDrops("fit") == [formats.FIT_DROPS_PREFIX + "site, buddy, weight"]
    # the opened file is refused as the target; a new name is patched and reads back with the edits
    c.saveAs("fit", _file_url(path))
    assert "is the file the dive was opened from" in c.message and open(path, "rb").read() == original_bytes
    target = scratch_data_dir / "two_tanks (edited).fit"
    c.saveAs("fit", _file_url(target))
    assert c.message == f"Wrote {target}" and c.saveWarnings == [formats.FIT_DROPS_PREFIX + "site, buddy, weight"]
    back = formats.read_file(target).dives[0]
    assert back.dive_number == 39 and back.gas_mixtures[0].end_pressure == 160.0 and back.timezone == "Etc/GMT-8"
    assert back.date_time == before.date_time and back.date_time_utc == datetime(2026, 8, 29, 1, 56, 11) and back.buddy is None
    assert back.samples == before.samples and len(back.events) == len(before.events)
    assert open(path, "rb").read() == original_bytes
    # one file per dive into a folder, named after the source; dives from an .ssrf beside it turn the target off (mixed)
    folder = scratch_data_dir / "each"
    c.saveEach("fit", _file_url(folder))
    assert c.message == f"Wrote 1 file to {folder}" and os.listdir(folder) == ["two_tanks (edited).fit"]
    c.openFiles([_file_url(os.path.join(FIXTURES, "ssrf", "handwritten.ssrf"))])
    assert wait_until(qapp, lambda: not c.busy)
    c.selectAll()
    assert c.fitSaveTip == "Save as .fit patches a dive opened from a .fit file and writes any other dive from scratch: select one kind at a time"
    assert c.fitDrops == [] and c.fitEncodes is False
    assert c.suggestedFileName("fit") == ""
    c.saveEach("fit", _file_url(scratch_data_dir / "mixed"))
    assert c.message == c.fitSaveTip and not (scratch_data_dir / "mixed").exists()
    c.clickRow(0, False, False)
    assert c.fitSaveTip == "" and c.fitDrops == ["site", "buddy", "weight"] and c.fitEncodes is False
    # the .ssrf dives alone: written from scratch (I12), one file per dive with the date names
    c.clickRow(1, False, False)
    c.clickRow(c.diveCount - 1, False, True)
    assert c.fitSaveTip == "" and c.fitEncodes is True and "site" in c.fitDrops
    c.saveEach("fit", _file_url(scratch_data_dir / "encoded"))
    assert c.message == f"Wrote {c.diveCount - 1} files to {scratch_data_dir / 'encoded'}"
    names = sorted(os.listdir(scratch_data_dir / "encoded"))
    assert names[0] == "2025-07-01 0812 dive 101.fit" and all(n.endswith(".fit") and "(edited)" not in n for n in names)
    assert formats.read_file(scratch_data_dir / "encoded" / names[0]).dives[0].dive_number == 101


def test_dive_edit_page_save_as_fit(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """The page with the FIT fixture (skipped when absent): the Save as .fit
    button is on for a dive from a .fit and off, with the tip, when the
    selection holds a dive from another format; the "Not in a .fit" line
    appears once a field no FIT holds is edited; the Connect re-upload
    note sits under the buttons; no QML warning."""
    from PySide6.QtCore import QMetaObject, QObject
    from desktop import app as desktop_app
    from desktop import logging_bridge
    from src.core import dive_cache
    from src.core.convert import formats
    path = os.path.join(FIT_FIXTURES, "two_tanks.fit")
    if not os.path.exists(path):
        pytest.skip("FIT fixture not present")
    monkeypatch.setattr(dive_cache, "list_garmin_dives", lambda *a, **k: [])
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: [])
    warnings = []
    controllers = desktop_app.build_controllers(logging_bridge.install())
    c = controllers["convertController"]
    c._apply_read(formats.read_files([path, os.path.join(FIXTURES, "ssrf", "handwritten.ssrf")]), {})
    engine = desktop_app.create_engine(controllers, "Dive edit", on_warnings=lambda errs: warnings.extend(str(e.toString()) for e in errs))
    root = engine.rootObjects()[0]
    wait(qapp, 300)
    page = root.findChild(QObject, "convertPage")
    button = _save_button(page, "fit")
    drops, note = (page.findChild(QObject, n) for n in ("fitDropsNote", "fitConnectNote"))
    assert button.property("enabled") is True and button.property("text") == "Save as .fit…"
    assert drops.property("visible") is False and note.property("visible") is True
    assert note.property("text").startswith("Garmin Connect knows a dive by its start time and device")
    # an edit of a field no FIT holds: the line names it
    field = page.findChild(QObject, "edit-buddy")
    field.setProperty("text", "Kim")
    QMetaObject.invokeMethod(field, "editingFinished")
    wait(qapp, 150)
    assert drops.property("visible") is True and drops.property("text") == "Not in a .fit, save as UDDF or Subsurface for these: buddy"
    # a mixed selection: the button off with the tip, the notes gone
    c.selectAll()
    wait(qapp, 150)
    assert button.property("enabled") is False and button.property("blocked") == c.fitSaveTip != ""
    assert drops.property("visible") is False and note.property("visible") is False
    c.clickRow(0, False, False)
    wait(qapp, 150)
    assert button.property("enabled") is True and drops.property("visible") is True
    # the .ssrf dive alone: the button on (written from scratch), its own drops line and the Connect note
    c.clickRow(1, False, False)
    wait(qapp, 150)
    assert button.property("enabled") is True and c.fitEncodes is True and c.fitSaveTip == ""
    assert drops.property("visible") is True and drops.property("text").startswith("Not in a .fit, save as UDDF or Subsurface for these: site, buddy, notes")
    assert note.property("visible") is True
    assert warnings == [], warnings
    engine.deleteLater()
    wait(qapp, 50)


def test_dive_edit_save_as_fit_from_an_ssrf(qapp, scratch_data_dir, fake_keyring, tmp_path):
    """Save as .fit on a dive that did not come from a .fit (I12): the
    target is on with the "written from scratch" state, the suggested name
    is the date one in the dialogs' folder, the drops are what no FIT holds
    of the dive, the file written reads back through the FIT reader with
    the edits, and the unsaved-edit mark goes; the source is untouched."""
    from PySide6.QtCore import QUrl
    from desktop import preferences
    from src.core.convert import formats
    from src.core.convert.fit_reader import read_fit
    path = os.path.join(FIXTURES, "ssrf", "synthetic.ssrf")
    original_bytes = open(path, "rb").read()
    c = _loaded_convert(qapp, path)
    assert c.selection == [0] and c.diveCount == 1
    assert [t["id"] for t in c.targets] == ["uddf", "ssrf", "fit"] and c.targets[2]["patch"] is True and c.targets[2]["title"] == "Save as .fit…"
    assert c.fitSaveTip == "" and c.fitEncodes is True
    assert c.fitDrops == ["site", "buddy", "notes", "weight", "computer maker", "computer model", "tank role", "setpoint channel",
                          "alerts outside Garmin's list", "bookmark events", "mode change events", "setpoint change events",
                          "service ids", "suit", "divemaster", "rating", "visibility stars", "tags"]
    assert c.targetDrops("fit") == [formats.FIT_DROPS_PREFIX + ", ".join(c.fitDrops)]
    assert c.suggestedFileName("fit") == "2026-03-14 0930 dive 42.fit"
    folder = preferences.get_convert_folder(tmp_path)
    assert c.suggestedFileUrl("fit") == QUrl.fromLocalFile(os.path.join(folder, "2026-03-14 0930 dive 42.fit")).toString()
    assert c.edit(0, _form(c, dive_number="43", gf_high="90")) == "" and c.editedRows == [0]
    target = scratch_data_dir / "out" / "dive 42.fit"
    c.saveAs("fit", _file_url(target))
    assert c.message == f"Wrote {target}" and c.saveWarnings == c.targetDrops("fit") and c.editedRows == []
    back = read_fit(str(target))
    assert (back.dive_number, back.gf_high, back.gf_low, back.duration, back.max_depth) == (43, 90, 40, 2900, 31.45)
    assert back.date_time == datetime(2026, 3, 14, 9, 30, 5) and back.computer_serial == "1000000001" and back.buddy is None
    assert [(g.oxygen, g.start_pressure, g.tank_name) for g in back.gas_mixtures] == [(32.0, 201.3, "Tank 1"), (50.0, 180.0, "Deco <50>")]
    assert len(back.samples) == len(c._dives[0].samples) and formats.read_file(target).format_id == "fit"
    assert open(path, "rb").read() == original_bytes and preferences.get_convert_folder("") == str(target.parent)
    # the written .fit opened again is a .fit dive: Save as .fit now patches it
    c.openFiles([_file_url(target)])
    assert wait_until(qapp, lambda: not c.busy and c.diveCount == 2)
    c.clickRow(1, False, False)
    assert c.fitSaveTip == "" and c.fitEncodes is False and c.fitDrops == [] and c.suggestedFileName("fit") == "dive 42 (edited).fit"
    c.selectAll()
    assert c.fitSaveTip.startswith("Save as .fit patches a dive opened from a .fit file") and c.fitEncodes is False


def test_dive_edit_marks_revert_and_save_clear_the_marks(qapp, scratch_data_dir, fake_keyring, tmp_path):
    """Unsaved edits (plans/convert.md I15): an edit marks its row
    (`editedRows`, `hasEdits`, `revertCount` for the selection); Revert
    puts the dive as first shown back and drops the mark; Revert N on a
    mixed selection reverts only the edited ones; Save as (one file or one
    per dive) clears the marks of the dives written and makes the saved
    dive what Revert goes back to; a failed write keeps the mark; opening
    another file keeps the marks; Remove and Clear take them away."""
    from desktop.controllers.convert import ConvertController
    from src.core.convert import formats
    c = ConvertController()
    c._apply_read(formats.read_files([os.path.join(FIXTURES, "ssrf", "handwritten.ssrf")]), {})
    marks = []
    c.marksChanged.connect(lambda: marks.append(c.revertCount))
    assert c.hasEdits is False and c.editedRows == [] and c.revertCount == 0
    # an edit marks the row; the selection's count follows
    c.clickRow(4, False, False)
    before = c._dives[4]
    assert c.edit(4, _form(c, buddy="Kim", max_depth="7.5")) == ""
    assert c.hasEdits is True and c.editedRows == [4] and c.revertCount == 1 and marks[-1] == 1
    c.clickRow(0, False, False)
    assert c.revertCount == 0
    # Revert: the dive as first shown, no mark, the row and the pane follow
    c.clickRow(4, False, False)
    c.revert(4)
    assert c.hasEdits is False and c.editedRows == [] and c.originalOf(4) is None and c.revertCount == 0
    assert c._dives[4].model_dump() == before.model_dump() and c.dives[4]["max_depth"] == "5 m" and c.selected["buddy"] == ""
    c.revert(4)                                                         # nothing to revert: no change, no emit
    c.revert(99)
    assert c.editedRows == []
    # Revert N on a mixed selection: rows 0 and 4 edited, 2 not; the untouched one stays
    assert c.edit(4, _form(c, buddy="Kim")) == ""
    c.clickRow(0, False, False)
    assert c.edit(0, _form(c, buddy="Sam")) == ""
    c.clickRow(2, True, False)
    c.clickRow(4, True, False)
    assert sorted(c.selection) == [0, 2, 4] and c.revertCount == 2 and c.editedRows == [0, 4]
    c.revertSelected()
    assert c.editedRows == [] and c.hasEdits is False and c._dives[0].buddy != "Sam" and c._dives[4].buddy != "Kim"
    # Save as UDDF clears the mark of the dive written; the saved dive is the new baseline
    c.clickRow(4, False, False)
    assert c.edit(4, _form(c, buddy="Kim", location="Jetty")) == ""
    edited = c._dives[4]
    assert c.editedRows == [4]
    (tmp_path / "a file").write_text("not a folder")
    c.saveAs("uddf", _file_url(tmp_path / "a file" / "x.uddf"))        # a write that fails keeps the mark
    assert c.message.startswith("Could not write x.uddf") and c.editedRows == [4] and c.originalOf(4).buddy != "Kim"
    c.saveAs("uddf", _file_url(tmp_path / "saved.uddf"))
    assert c.message == f"Wrote {tmp_path / 'saved.uddf'}" and c.editedRows == [] and c.hasEdits is False
    assert c.originalOf(4) is edited and c._dives[4] is edited and c.selected["buddy"] == "Kim"
    # an edit after the save marks again; Revert goes back to what was saved, not to the file
    assert c.edit(4, _form(c, buddy="Lee")) == "" and c.editedRows == [4]
    c.revert(4)
    assert c.editedRows == [] and c._dives[4] is edited and c._dives[4].buddy == "Kim" and c.originalOf(4) is edited
    # the checkbox re-derives the list: the saved dive stays
    c.setEnrichFromCache(False)
    assert c._dives[4].buddy == "Kim"
    c.setEnrichFromCache(True)
    # one file per dive clears every mark among the dives written
    c.clickRow(0, False, False)
    assert c.edit(0, _form(c, buddy="Sam")) == ""
    c.clickRow(1, False, False)
    assert c.edit(1, _form(c, buddy="Ann")) == ""
    assert c.editedRows == [0, 1]
    c.clickRow(0, False, False)
    c.clickRow(1, True, False)
    c.clickRow(2, True, False)
    c.saveEach("ssrf", _file_url(tmp_path / "each"))
    assert c.message.startswith("Wrote 3 files") and c.editedRows == [] and c.hasEdits is False
    assert c.originalOf(0).buddy == "Sam" and c.originalOf(1).buddy == "Ann" and c.originalOf(2) is None
    # Revert all (main.qml's Discard): every mark goes at once
    c.clickRow(3, False, False)
    assert c.edit(3, _form(c, buddy="Bo")) == ""
    c.clickRow(4, False, False)
    assert c.edit(4, _form(c, buddy="Cy")) == "" and c.editedRows == [3, 4]
    c.revertAll()
    assert c.editedRows == [] and c._dives[3].buddy != "Bo" and c._dives[4].buddy == "Kim"
    # opening another file keeps the marks (the rows are appended)
    assert c.edit(4, _form(c, buddy="Cy")) == "" and c.editedRows == [4]
    c.openFiles([_file_url(os.path.join(FIXTURES, "uddf", "pre_i5_adapter.uddf"))])
    assert wait_until(qapp, lambda: not c.busy)
    assert c.diveCount == 7 and c.editedRows == [4] and c.hasEdits is True and c._dives[4].buddy == "Cy"
    # Remove takes the mark with the row (the page asked first); Clear forgets everything
    c.clickRow(4, False, False)
    assert c.revertCount == 1
    c.removeSelected()
    assert c.editedRows == [] and c.hasEdits is False and c.diveCount == 6
    c.clickRow(0, False, False)
    assert c.edit(0, _form(c, buddy="Zed")) == "" and c.hasEdits is True
    c.clear()
    assert c.hasEdits is False and c.editedRows == [] and c._edited == {} and c._originals == {}


def test_dive_edit_page_marks_revert_and_asks_before_discarding(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """The page (I15): an edited dive shows the dot in its row and "edited"
    on the card, Revert is on only for an edited dive and puts it back,
    "Revert N" for several selected; Remove and Clear open the discard
    question when an edited dive would go - Keep leaves everything, Discard
    does it and `hasEdits` follows; no QML warning."""
    from PySide6.QtCore import QMetaObject, QObject
    from desktop import app as desktop_app
    from desktop import logging_bridge
    from src.core import dive_cache
    from src.core.convert import formats
    monkeypatch.setattr(dive_cache, "list_garmin_dives", lambda *a, **k: [])
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: [])
    warnings = []
    controllers = desktop_app.build_controllers(logging_bridge.install())
    c = controllers["convertController"]
    c._apply_read(formats.read_files([os.path.join(FIXTURES, "ssrf", "handwritten.ssrf")]), {})
    engine = desktop_app.create_engine(controllers, "Dive edit", on_warnings=lambda errs: warnings.extend(str(e.toString()) for e in errs))
    root = engine.rootObjects()[0]
    root.show()                                                           # the list's rows exist only in a shown window
    wait(qapp, 300)
    page = root.findChild(QObject, "convertPage")
    find = lambda name: page.findChild(QObject, name)
    click = lambda name: (QMetaObject.invokeMethod(find(name), "click"), wait(qapp, 150))
    commit = lambda name, text: (find(name).setProperty("text", text), QMetaObject.invokeMethod(find(name), "editingFinished"), wait(qapp, 100))
    # the list's rows are delegate items (no QObject parent, so not findChild's): walk the view's items
    def find_item(item, name):
        for child in item.childItems():
            if child.objectName() == name:
                return child
            found = find_item(child, name)
            if found is not None:
                return found
        return None
    dot = lambda i: find_item(find("convertDiveList").property("contentItem"), f"editMark-{i}").property("text")
    revert, mark, dialog = find("revertButton"), find("editedMark"), find("discardEditsDialog")
    assert revert.property("enabled") is False and revert.property("text") == "Revert" and mark.property("visible") is False
    assert dot(0) == "" and dialog.property("opened") is False
    # an edit: the dot on the row, "edited" on the card, Revert on
    commit("edit-buddy", "Sam")
    assert c.editedRows == [0] and dot(0) == "●" and dot(1) == ""
    assert mark.property("visible") is True and mark.property("text") == "● edited" and revert.property("enabled") is True
    # Revert: back as read, the marks gone, the field shows the stored value again
    click("revertButton")
    assert c.editedRows == [] and dot(0) == "" and mark.property("visible") is False
    assert revert.property("enabled") is False and find("edit-buddy").property("text") == c._dives[0].buddy
    # Revert N: two edited among three selected
    commit("edit-buddy", "Sam")
    c.clickRow(4, False, False)
    wait(qapp, 150)
    commit("edit-buddy", "Kim")
    c.clickRow(0, False, False); c.clickRow(2, True, False); c.clickRow(4, True, False)
    wait(qapp, 150)
    assert revert.property("text") == "Revert 2" and revert.property("enabled") is True and mark.property("visible") is False
    click("revertButton")
    assert c.editedRows == [] and revert.property("text") == "Revert" and revert.property("enabled") is False
    # Remove with an edited dive selected: the question; Keep leaves it, Discard removes it
    c.clickRow(4, False, False)
    wait(qapp, 150)
    commit("edit-buddy", "Kim")
    assert c.hasEdits is True
    click("removeButton")
    assert dialog.property("opened") is True and dialog.property("count") == 1 and dialog.property("action") == "remove"
    click("keepEditsButton")
    assert dialog.property("opened") is False and c.diveCount == 5 and c.hasEdits is True and c._dives[4].buddy == "Kim"
    click("removeButton")
    assert dialog.property("opened") is True
    click("discardEditsButton")
    assert dialog.property("opened") is False and c.diveCount == 4 and c.hasEdits is False and c.editedRows == []
    # Remove of an unedited dive does not ask
    c.clickRow(3, False, False)
    wait(qapp, 100)
    click("removeButton")
    assert dialog.property("opened") is False and c.diveCount == 3
    # Clear: the question counts every edited dive in the list, selected or not
    commit("edit-buddy", "Ann")
    c.clickRow(0, False, False)
    wait(qapp, 150)
    commit("edit-buddy", "Bo")
    c.clickRow(1, False, False)
    wait(qapp, 150)
    assert c.editedRows == [0, 2] and c.revertCount == 0
    click("clearButton")
    assert dialog.property("opened") is True and dialog.property("count") == 2 and dialog.property("action") == "clear"
    click("keepEditsButton")
    assert c.diveCount == 3 and c.hasEdits is True
    click("clearButton")
    click("discardEditsButton")
    assert c.diveCount == 0 and c.hasEdits is False and dialog.property("opened") is False
    # an empty list: Clear just clears, no question
    click("clearButton")
    assert dialog.property("opened") is False
    assert warnings == [], warnings
    root.close()
    engine.deleteLater()
    wait(qapp, 50)


def test_closing_the_app_asks_about_unsaved_dive_edits(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """main.qml's closing chain (I15): with a dive edited on the Dive edit
    page and not saved, closing opens "Unsaved dive edits"; its Discard
    reverts the edits and closes again (the unsaved-dives question still
    follows when there is one, as after the mapping dialog); with no
    edits the window closes without asking."""
    from PySide6.QtCore import QMetaObject, QObject
    from desktop import app as desktop_app
    from desktop import logging_bridge
    from src.core import dive_cache
    from src.core.convert import formats
    monkeypatch.setattr(dive_cache, "list_dives", lambda *a, **k: [])
    monkeypatch.setattr(dive_cache, "get_samples", lambda *a, **k: [])
    controllers = desktop_app.build_controllers(logging_bridge.install())
    c = controllers["convertController"]
    c._apply_read(formats.read_files([os.path.join(FIXTURES, "ssrf", "handwritten.ssrf")]), {})
    engine = desktop_app.create_engine(controllers, "Dive edit")
    root = engine.rootObjects()[0]
    root.show()
    wait(qapp, 200)
    dialog = root.findChild(QObject, "leaveEditsDialog")
    c.clickRow(4, False, False)
    assert c.edit(4, _form(c, buddy="Kim")) == "" and c.hasEdits is True
    root.close()
    wait(qapp, 200)
    assert root.isVisible() and dialog.property("opened") is True
    assert root.findChild(QObject, "leaveDivesDialog").property("opened") is False
    # Discard: the edits go, and the window closes without a second question
    QMetaObject.invokeMethod(dialog, "discarded")
    wait(qapp, 200)
    assert c.hasEdits is False and c._dives[4].buddy != "Kim" and not root.isVisible()
    # with nothing edited the window closes at once
    root.show()
    wait(qapp, 100)
    root.close()
    wait(qapp, 200)
    assert not root.isVisible() and dialog.property("opened") is False
    engine.deleteLater()
    wait(qapp, 50)


def _split_dive_folder(tmp_path):
    """A folder with a synthetic split dive for the Merge tests (I17): the
    merge tests' three chained logs a, b, c (`tests/test_convert_fit_merge`:
    one computer, 180 s and 90 s between the samples), a fourth log d of
    the same computer three hours after c, and a Subsurface file last in
    name order. Returns (folder, {name: bytes})."""
    import shutil
    from tests.test_convert_fit_merge import C_START, _a, _b, _c, _log
    folder = tmp_path / "split"
    folder.mkdir()
    files = {"a.fit": _a(), "b.fit": _b(), "c.fit": _c(),
             "d.fit": _log(C_START + 180 + 3 * 3600, (0, 60, 120), (1.0, 10.0, 1.0), number=15)}
    for name, data in files.items():
        (folder / name).write_bytes(data)
    shutil.copy(os.path.join(FIXTURES, "ssrf", "handwritten.ssrf"), folder / "z.ssrf")
    files["z.ssrf"] = (folder / "z.ssrf").read_bytes()
    return folder, files


def _chain_rows(c):
    return [r["row"] for r in c.mergeChain]


def test_dive_edit_merge_chain_and_merge(qapp, scratch_data_dir, fake_keyring, tmp_path):
    """Merge (I17, reworked in I17b) through the controller: `mergeChain`
    is the selected dives in start order whatever the click order,
    `mergeProblem` is why they cannot be merged (fewer than two, a
    Subsurface dive named, the rule fit_merge.merge_problem's: same
    computer, within 2 hours, no overlap) and "" when they can;
    `mergeProblemOf` re-checks the dialog's ticked rows; an unsaved edit
    flags the row; `merge` refuses a target equal to a source, and
    otherwise writes the file on the worker, adds the merged dive as a new
    row with the merged file as its source and selects it - start and end
    as merge_fits gives - with the originals' bytes and rows untouched; a
    middle row left out drops the chain's tail; a MergeError shows under
    the list and adds nothing."""
    from PySide6.QtCore import QUrl
    from desktop import preferences
    from desktop.controllers.convert import ConvertController
    from src.core.convert import fit_merge
    from src.core.convert.fit_reader import read_fit
    folder, files = _split_dive_folder(tmp_path)
    too_few = "two or more dives are needed"
    c = ConvertController()
    assert c.mergeChain == [] and c.mergeProblem == too_few and c.mergeProblemOf([]) == too_few
    c.openFolder(_file_url(folder))
    assert wait_until(qapp, lambda: not c.busy)
    assert [s for s in c._sources[:5]] == ["a.fit", "b.fit", "c.fit", "d.fit", "z.ssrf"] and c.diveCount == 9
    # one dive selected: one row, nothing to merge yet, no file to suggest
    assert c.currentRow == 0 and c.selection == [0] and c.mergeError == ""
    assert _chain_rows(c) == [0] and c.mergeProblem == too_few and c.mergeFileUrl() == ""
    # a, b, c selected: the rows in start order with what the dialog shows, no problem
    c.clickRow(1, True, False)
    c.clickRow(2, True, False)
    chain = c.mergeChain
    assert _chain_rows(c) == [0, 1, 2] and c.mergeProblem == ""
    assert [r["source"] for r in chain] == ["a.fit", "b.fit", "c.fit"] and [r["dive_number"] for r in chain] == [12, 13, 14]
    assert chain[0]["date"] == "2024-11-08" and chain[0]["time"] == "12:33:20" and chain[0]["duration"] == "2 min"
    assert [r["edited"] for r in chain] == [False, False, False]
    assert c.mergeFileUrl() == QUrl.fromLocalFile(str(folder / "a (merged).fit")).toString()
    # clicked in reverse order: still start order
    c.clickRow(2, False, False)
    c.clickRow(0, True, False)
    assert c.selection == [0, 2] and _chain_rows(c) == [0, 2] and c.mergeProblem == ""
    c.clickRow(1, True, False)
    assert _chain_rows(c) == [0, 1, 2] and c.mergeProblem == ""
    # d three hours on: the reason; the dialog's re-check without d clears it
    far = "dive 14 and dive 15 are 3.0 hours apart; only dives within 2 hours of each other can be merged"
    c.clickRow(3, True, False)
    assert _chain_rows(c) == [0, 1, 2, 3] and c.mergeProblem == far
    assert c.mergeProblemOf([0, 1, 2]) == "" and c.mergeProblemOf([0, 1, 2, 3]) == far and c.mergeProblemOf([0]) == too_few
    assert c.mergeProblemOf([1, 0]) == "dive 12 starts before dive 13; merge the logs in start order"
    # a Subsurface dive in the selection is named; the whole list too
    c.clickRow(0, False, False)
    c.clickRow(4, True, False)
    not_fit = "z.ssrf is not a Garmin .fit log; Merge joins .fit logs only"
    assert _chain_rows(c) == sorted([0, 4], key=lambda r: c._dives_as_read[r].date_time_utc or c._dives_as_read[r].date_time)
    assert c.mergeProblem == not_fit
    c.selectAll()
    assert len(c.mergeChain) == 9 and c.mergeProblem == not_fit
    # an unsaved edit flags the row (the merge reads the files, not the edit); the chain is unchanged
    c.clickRow(1, False, False)
    assert c.edit(1, _form(c, buddy="Kim")) == ""
    c.clickRow(0, False, False)
    c.clickRow(2, True, False)
    c.clickRow(1, True, False)
    assert _chain_rows(c) == [0, 1, 2] and [r["edited"] for r in c.mergeChain] == [False, True, False] and c.mergeProblem == ""
    c.revert(1)
    # a target equal to a source is refused, nothing written or added
    c.merge([0, 1, 2], _file_url(folder / "b.fit"), False)
    assert c.mergeError == "The merged file cannot replace one of the logs; choose another name." and not c.busy
    assert c.diveCount == 9 and (folder / "b.fit").read_bytes() == files["b.fit"]
    c.merge([0], _file_url(folder / "x.fit"), False)
    assert c.mergeError.startswith("Two or more logs are needed") and not (folder / "x.fit").exists()
    # the merge, keeping the real times: the file, the new row with its source, selected; the originals untouched
    dest = folder / "a (merged).fit"
    expected = tmp_path / "expected.fit"
    want = fit_merge.merge_fits([folder / "a.fit", folder / "b.fit", folder / "c.fit"], expected, False)
    c.merge([0, 1, 2], _file_url(dest), False)
    assert c.busy and c.mergeError == ""
    assert wait_until(qapp, lambda: not c.busy and c.diveCount == 10)
    assert wait_until(qapp, lambda: c.message.startswith("Merged 3 logs into"))
    assert want.filled_samples == 3 and want.warnings == []
    assert c.message == f"Merged 3 logs into {dest}: the surface time added, 3 samples filling it."
    assert c.currentRow == 9 and c.selection == [9] and c._sources[9] == "a (merged).fit" and c._paths[9] == str(dest)
    assert dest.read_bytes() == expected.read_bytes()
    merged, want = c._dives[9], read_fit(str(expected))
    assert merged.date_time_utc == want.date_time_utc == c._dives[0].date_time_utc and merged.duration == want.duration
    assert merged.duration == int((c._dives_as_read[2].date_time_utc - c._dives_as_read[0].date_time_utc).total_seconds()) + c._dives_as_read[2].duration
    assert c.selected["source"] == "a (merged).fit" and c.selected["form"]["dive_number"] == "12"
    assert [c._sources[i] for i in range(5)] == ["a.fit", "b.fit", "c.fit", "d.fit", "z.ssrf"]
    assert all((folder / name).read_bytes() == data for name, data in files.items())
    assert preferences.get_convert_folder("") == str(folder)
    # the merged dive is a normal row: Save as .fit is on for it, alone it is one row; with d it is too far
    assert c.fitSaveTip == "" and _chain_rows(c) == [9] and c.mergeProblem == too_few
    c.clickRow(3, True, False)
    assert _chain_rows(c) == [9, 3] and c.mergeProblem == "dive 12 and dive 15 are 3.0 hours apart; only dives within 2 hours of each other can be merged"
    # a middle row left out drops the tail: a + c alone, the gap closed; an earlier merge to the same file is replaced, not doubled
    want = fit_merge.merge_fits([folder / "a.fit", folder / "c.fit"], expected, True)
    c.merge([0, 2], _file_url(dest), True)
    assert wait_until(qapp, lambda: not c.busy and c.message.startswith("Merged 2 logs"))
    assert want.closed_seconds == 330
    assert c.message == f"Merged 2 logs into {dest}: the surface time removed, 330 seconds taken out of the dive."
    assert c.diveCount == 10 and c.currentRow == 9 and dest.read_bytes() == expected.read_bytes()
    assert c._dives[9].duration == read_fit(str(expected)).duration and c._dives[9].date_time_utc == c._dives[0].date_time_utc
    # a MergeError (d is too far from a): the error, nothing added, the target not written
    c.merge([0, 3], _file_url(folder / "a+d.fit"), False)
    assert wait_until(qapp, lambda: not c.busy)
    assert c.mergeError == fit_merge.merge_problem([folder / "a.fit", folder / "d.fit"])
    assert c.mergeError == "a.fit and d.fit are 3.2 hours apart; only dives within 2 hours of each other can be merged"
    assert c.diveCount == 10 and not (folder / "a+d.fit").exists()
    # a new merge clears the error; Clear clears it too
    c.merge([0, 1], _file_url(folder / "a.fit"), False)
    assert c.mergeError.startswith("The merged file cannot replace")
    c.clear()
    assert c.mergeError == "" and c.mergeChain == [] and c.mergeProblem == too_few


@pytest.mark.skipif(not os.path.isdir(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test_data", "fit")),
                    reason="the owner's test_data/fit dives are not present")
def test_dive_edit_merge_the_owners_split_dive(qapp, scratch_data_dir, fake_keyring, tmp_path):
    """The owner's real split dive (three Descent X50i logs in
    test_data/fit, read-only, skipped when absent) through the controller:
    opened as a folder, the three selected (last first) are the chain in
    start order with no problem, merged keep and close into tmp paths; read
    back the merged dive spans 3468 s (keep) and 3189 s (close) as the
    merge tests found; the originals untouched."""
    from tests.test_convert_fit_patch import OWNER_FITS
    from desktop.controllers.convert import ConvertController
    if len(OWNER_FITS) != 3:
        pytest.skip("the owner's three split-dive logs are not present")
    originals = [open(p, "rb").read() for p in OWNER_FITS]
    c = ConvertController()
    c.openFolder(_file_url(os.path.dirname(OWNER_FITS[0])))
    assert wait_until(qapp, lambda: not c.busy)
    rows = [c._paths.index(p) for p in OWNER_FITS]

    def select_three():
        c.clickRow(rows[2], False, False)
        c.clickRow(rows[1], True, False)
        c.clickRow(rows[0], True, False)
    select_three()
    assert _chain_rows(c) == rows and c.mergeProblem == ""
    assert [r["source"] for r in c.mergeChain] == [os.path.basename(p) for p in OWNER_FITS]
    assert [r["dive_number"] for r in c.mergeChain] == [c._dives_as_read[r].dive_number for r in rows] and c.mergeChain[0]["time"] == "11:43:31"
    for close_gap, span in ((False, 3468), (True, 3189)):
        dest = tmp_path / ("close.fit" if close_gap else "keep.fit")
        select_three()
        c.merge(_chain_rows(c), _file_url(dest), close_gap)
        assert wait_until(qapp, lambda: not c.busy and c.message.startswith("Merged 3 logs"))
        merged = c._dives[c.currentRow]
        assert c._paths[c.currentRow] == str(dest) and merged.duration == span and merged.dive_number == c._dives_as_read[rows[0]].dive_number
        assert merged.date_time_utc == c._dives_as_read[rows[0]].date_time_utc and merged.computer_serial == "3504700399"
    assert [open(p, "rb").read() for p in OWNER_FITS] == originals


def test_dive_edit_page_merge_dialog(qapp, scratch_data_dir, fake_keyring, monkeypatch, tmp_path):
    """The page's Merge (I17, reworked in I17b): the button sits at the
    bottom of the list card next to its description line, off with one
    dive selected (the tip says to select two or more) and on with two or
    more; the dialog lists the selection in start order (the first fixed,
    the later ones ticked) and, when it cannot be merged (a Subsurface
    dive, a log three hours on), says why in red with Save… off; unticking
    the far row clears the reason, unticking the middle row drops the last
    and Save… goes off with one row left, ticking the last one ticks the
    middle again; the two radios, the unsaved-edits line when a row is
    edited and the Connect note; Save hands the ticked rows and the mode
    to the controller, which merges and adds the merged dive; Cancel does
    nothing; a refused target shows under the list; no QML warning."""
    from PySide6.QtCore import QMetaObject, QObject, Q_ARG
    from desktop import app as desktop_app
    from desktop import logging_bridge
    from src.core import dive_cache
    from src.core.convert import fit_merge, formats
    from src.core.convert.fit_reader import read_fit
    monkeypatch.setattr(dive_cache, "list_garmin_dives", lambda *a, **k: [])
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: [])
    folder, files = _split_dive_folder(tmp_path)
    warnings = []
    controllers = desktop_app.build_controllers(logging_bridge.install())
    c = controllers["convertController"]
    c._apply_read(formats.read_files([str(folder / n) for n in ("a.fit", "b.fit", "c.fit", "d.fit", "z.ssrf")]), {})
    engine = desktop_app.create_engine(controllers, "Dive edit", on_warnings=lambda errs: warnings.extend(str(e.toString()) for e in errs))
    root = engine.rootObjects()[0]
    root.show()
    wait(qapp, 300)
    page = root.findChild(QObject, "convertPage")
    find = lambda name: page.findChild(QObject, name)
    click = lambda name: (QMetaObject.invokeMethod(find(name), "click"), wait(qapp, 150))

    def find_item(item, name):
        for child in item.childItems():
            if child.objectName() == name:
                return child
            found = find_item(child, name)
            if found is not None:
                return found
        return None

    def select(*rows):
        c.clickRow(rows[0], False, False)
        for r in rows[1:]:
            c.clickRow(r, True, False)
        wait(qapp, 150)
    button, tip, dialog, error, hint = find("mergeButton"), find("mergeTip"), find("mergeDialog"), find("mergeError"), find("mergeHint")
    problem, save = find("mergeProblem"), find("mergeSaveButton")
    row_box = lambda i: find_item(dialog.property("contentItem"), f"mergeRow-{i}")
    ticked = lambda: [i for i in range(dialog.property("tickedCount") + 4) if row_box(i) is not None and row_box(i).property("checked") is True]
    # the button and its description sit together under the list, not on the dive card
    assert button.property("text") == "Merge…" and button.parent() == hint.parent() and button.parent() != find("revertButton").parent()
    assert find("selectAllButton").parent() != button.parent() and hint.property("visible") is True
    assert hint.property("text") == "Merge…: select the logs of a dive the computer split in two or more (Cmd/Ctrl-click), then join them into one .fit."
    # one dive selected: off with the tip; two: on with the help
    assert c.currentRow == 0 and c.selectedCount == 1 and button.property("enabled") is False
    assert tip.property("text") == "Select two or more logs of the same dive to merge them."
    assert error.property("visible") is False and dialog.property("opened") is False
    select(0, 1)
    assert button.property("enabled") is True
    assert tip.property("text") == ("Joins the selected Garmin .fit logs of one dive that the computer split into several (same dive computer, each log starting "
                                    "within 2 hours of the previous one) into one .fit file. Select the logs, then Merge… asks whether to add or remove the surface time between them.")
    # a Subsurface dive in the selection: the dialog opens and says why, Save… off
    select(4, 0)
    assert button.property("enabled") is True
    click("mergeButton")
    assert dialog.property("opened") is True and dialog.property("tickedCount") == 2
    assert problem.property("visible") is True and problem.property("text") == "⚠ Cannot merge: z.ssrf is not a Garmin .fit log; Merge joins .fit logs only"
    assert save.property("enabled") is False
    click("mergeCancelButton")
    assert dialog.property("opened") is False
    # a, b, c and d (three hours on), clicked last first: the rows in start order, the reason; unticking d clears it
    select(3, 2, 1, 0)
    click("mergeButton")
    content = dialog.property("contentItem")
    assert dialog.property("tickedCount") == 4 and ticked() == [0, 1, 2, 3]
    assert [find_item(content, f"mergeRow-{i}").parent().childItems()[1].property("text") for i in range(4)] == ["2024-11-08"] * 4
    assert problem.property("visible") is True and save.property("enabled") is False
    assert problem.property("text") == "⚠ Cannot merge: dive 14 and dive 15 are 3.0 hours apart; only dives within 2 hours of each other can be merged"
    QMetaObject.invokeMethod(row_box(3), "click")
    wait(qapp, 100)
    assert ticked() == [0, 1, 2] and problem.property("visible") is False and dialog.property("problem") == "" and save.property("enabled") is True
    QMetaObject.invokeMethod(row_box(3), "click")
    wait(qapp, 100)
    assert ticked() == [0, 1, 2, 3] and problem.property("visible") is True and save.property("enabled") is False
    click("mergeCancelButton")
    # an edit on the middle log: flagged in the dialog
    c.clickRow(1, False, False)
    wait(qapp, 100)
    assert c.edit(1, _form(c, buddy="Kim")) == ""
    select(0, 1, 2)
    click("mergeButton")
    assert dialog.property("opened") is True and dialog.property("tickedCount") == 3 and dialog.property("anyEdited") is True
    content = dialog.property("contentItem")
    assert row_box(0).property("enabled") is False and row_box(0).property("checked") is True and ticked() == [0, 1, 2]
    assert find_item(content, "mergeRowEdited-1").property("text") == "● edited" and find_item(content, "mergeRowEdited-2").property("text") == ""
    assert find("mergeEditedNote").property("visible") is True
    assert find("mergeConnectNote").property("text").startswith("Garmin Connect knows a dive by its start time and device")
    keep, close_gaps = find("mergeKeepTimes"), find("mergeCloseGaps")
    assert keep.property("checked") is True and close_gaps.property("checked") is False and dialog.property("closeGaps") is False
    assert save.property("enabled") is True and problem.property("visible") is False
    # unticking the middle row unticks the last; one row left turns Save… off; ticking the last ticks the middle again
    QMetaObject.invokeMethod(row_box(1), "click")
    wait(qapp, 100)
    assert ticked() == [0] and dialog.property("tickedCount") == 1 and save.property("enabled") is False
    assert problem.property("text") == "⚠ Cannot merge: two or more dives are needed"
    QMetaObject.invokeMethod(row_box(2), "click")
    wait(qapp, 100)
    assert ticked() == [0, 1, 2] and dialog.property("tickedCount") == 3 and save.property("enabled") is True
    QMetaObject.invokeMethod(row_box(2), "click")
    wait(qapp, 100)
    assert ticked() == [0, 1] and dialog.property("tickedCount") == 2
    # Cancel: closed, nothing merged
    click("mergeCancelButton")
    assert dialog.property("opened") is False and c.diveCount == 9 and not (folder / "a (merged).fit").exists()
    # open again: every row ticked again, keep selected; remove the surface time, Save with the file picked -> the controller merges
    click("mergeButton")
    assert ticked() == [0, 1, 2] and dialog.property("tickedCount") == 3
    QMetaObject.invokeMethod(row_box(2), "click")
    wait(qapp, 100)
    QMetaObject.invokeMethod(close_gaps, "click")
    wait(qapp, 100)
    assert dialog.property("closeGaps") is True and keep.property("checked") is False
    dest = folder / "a (merged).fit"
    want = fit_merge.merge_fits([folder / "a.fit", folder / "b.fit"], tmp_path / "want.fit", True)
    QMetaObject.invokeMethod(page, "runMerge", Q_ARG("QVariant", _file_url(dest)))
    assert wait_until(qapp, lambda: not c.busy and c.diveCount == 10)
    wait(qapp, 150)
    assert c.message == f"Merged 2 logs into {dest}: the surface time removed, {want.closed_seconds} seconds taken out of the dive."
    assert dest.read_bytes() == (tmp_path / "want.fit").read_bytes() and c.currentRow == 9 and c._sources[9] == "a (merged).fit"
    assert c._dives[9].duration == read_fit(str(tmp_path / "want.fit")).duration
    assert find("sourceLine").property("text").startswith("From a (merged).fit") and error.property("visible") is False
    assert all((folder / name).read_bytes() == data for name, data in files.items())
    # the merged dive alone: the button is off again
    assert c.selection == [9] and button.property("enabled") is False
    # a refused target: the error in red under the list, nothing added
    click("mergeCancelButton")
    select(0, 1)
    click("mergeButton")
    QMetaObject.invokeMethod(page, "runMerge", Q_ARG("QVariant", _file_url(folder / "a.fit")))
    wait(qapp, 100)
    assert error.property("visible") is True and error.property("text") == "⚠ Merge: " + c.mergeError and c.diveCount == 10
    assert error.parent() == hint.parent().parent()
    click("mergeCancelButton")
    assert warnings == [], warnings
    root.close()
    engine.deleteLater()
    wait(qapp, 50)


def test_editing_helpers_shared_by_both_editors(qapp):
    """``desktop/controllers/editing.py`` (moved out of dives.py for I14):
    the form's number text, the display per system and the no-drift rule."""
    from desktop.controllers import dives, editing
    assert editing.js_number(12.0) == "12" and editing.js_number(28.5) == "28.5" and editing.js_number(None) == "" and editing.js_number("x") == "x"
    assert editing.js_number(True) == "True" and editing.js_number(7) == "7"
    assert editing.optional_float(" 3.5 ") == 3.5 and editing.optional_float("") is None and editing.optional_float("deep") is None
    assert editing.display_number("depth", 30.48, "metric") == "30.48" and editing.display_number("depth", 30.48, "imperial") == "100"
    assert editing.display_number("depth", "30.20", "metric") == "30.20" and editing.display_number("depth", "", "imperial") == ""
    assert editing.display_number("depth", "deep", "imperial") == "deep"
    # untouched: the stored value; edited: converted back at full precision; metric and text pass through
    assert editing.si_number("depth", "100", "100", 30.48, "imperial") == 30.48
    assert editing.si_number("depth", "99", "100", 30.48, "imperial") == pytest.approx(30.1752)
    assert editing.si_number("depth", "31", "30.48", 30.48, "metric") == "31" and editing.si_number("depth", "", "", None, "imperial") == ""
    assert editing.si_number("depth", "deep", "100", 30.48, "imperial") == "deep"
    # the Dives page still reaches them under the names it used
    assert dives.js_number is editing.js_number and dives.si_number is editing.si_number and dives.display_number is editing.display_number


def _ssi_fake_transport(fake, sites):
    """`tests.test_ssi.FakeSsi` plus the public site-database zip."""
    import io
    import json
    import zipfile
    from src.core.services import ssi
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("sites.json", json.dumps(sites))
    zip_bytes = buffer.getvalue()

    def transport(method, url, params, data, timeout):
        if url == ssi.SITES_URL:
            return ssi.HttpResponse(200, zip_bytes)
        return fake(method, url, params, data, timeout)
    return transport


def test_convert_sends_to_ssi_through_a_fake_client(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """Send to SSI: the plan first (duplicates skipped, numbers after SSI's
    highest, the nearest site within 5 km preselected, none otherwise), the
    site changed or cleared per dive, then the upload with every dive read
    back; the site database lands under DATA_DIR/ssi and the token in the
    keychain. Everything against tests/test_ssi.py's fake backend: the
    network is blocked."""
    import requests
    from desktop import credentials as creds_store
    from desktop.controllers.convert import ConvertController
    from src.core import layout
    from src.core.convert.formats import ReadResult
    from src.core.services import ssi
    from tests.test_ssi import EMAIL, PASSWORD, SITES, FakeSsi, logbook_dive, make_dive

    def blocked(self, method, url, *a, **k):
        raise AssertionError(f"network blocked: {method} {url}")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.setattr(ssi, "UPLOAD_ENABLED", True)        # the flow is hidden behind the switch since 2026-10-02

    fake = FakeSsi(dives=[logbook_dive(7, "2026-06-27", "10:00", ref="garmin:24449823352")], sites=[])
    c = ConvertController()
    assert c.ssiEnabled
    c._ssi_client_kwargs = {"transport": _ssi_fake_transport(fake, SITES), "sleep": lambda s: None}
    dives = [make_dive(),                                                              # already in MySSI (same computer ref)
             make_dive(start="2026-06-28 09:00", activity="24449823399"),               # near House Reef (501)
             make_dive(start="2026-06-29 09:00", activity="24449823400", lat=None, lng=None, location="Somewhere")]
    c._apply_read(ReadResult(dives=dives, files=[("/x/a.fit", "fit"), ("/x/b.fit", "fit"), ("/x/c.fit", "fit")]))
    c.selectAll()
    # no login saved: the page says where to add it, nothing is called
    assert not c.ssiConfigured and "Settings" in c.ssiLoginStatus
    c.prepareSsi()
    assert "Settings" in c.ssiMessage and fake.calls == [] and not c.ssiPlanOpen
    creds_store.save_ssi_credentials(EMAIL, PASSWORD)
    c.reloadSsiLogin()
    assert c.ssiConfigured and c.ssiLoginStatus == f"MySSI login: {EMAIL}"
    assert c.ssiNote == ssi.UNOFFICIAL_NOTE and not c.ssiSitesDownloaded

    c.prepareSsi()
    assert c.ssiBusy
    assert wait_until(qapp, lambda: not c.ssiBusy)
    assert c.ssiPlanOpen and fake.whats() == ["authenticate", "get_divelog"]
    assert os.path.isfile(layout.ssi_sites_file()) and c.ssiSitesDownloaded and "3 sites" in c.ssiSitesStatus
    plan = c.ssiPlan
    assert [p["status"] for p in plan] == ["skipped", "planned", "planned"] and c.ssiPlannedCount == 2
    assert "already in MySSI as dive 7" in plan[0]["reason"]
    assert plan[1]["number"] == 8 and plan[1]["site_id"] == 501 and plan[1]["site_label"] == "House Reef (South, Testland)"
    assert plan[2]["number"] == 9 and plan[2]["site_id"] == -1 and plan[2]["site_label"] == ""
    assert any("site name" in line for line in plan[2]["dropped"])
    assert c.ssiMessage.startswith("2 dives to send, 1 already in MySSI (skipped).")
    assert creds_store.load_ssi_credentials().token == "tok-1"           # the on_token hook
    # the site picker: search, pick, clear
    assert [s["id"] for s in c.searchSites("far")] == [502] and c.searchSites("") == []
    c.setSite(2, 502)
    assert c.ssiPlan[2]["site_id"] == 502 and c.ssiPlan[2]["site_label"].startswith("Far Wall")
    c.clearSite(2)
    assert c.ssiPlan[2]["site_id"] == -1
    c.setSite(1, -1)
    c.setSite(1, 501)
    c.setSite(99, 501)                                                    # off the plan: ignored

    c.sendToSsi()
    assert wait_until(qapp, lambda: not c.ssiBusy)
    assert not c.ssiPlanOpen
    results = c.ssiResults
    assert [r["status"] for r in results] == ["skipped", "sent", "sent"]
    assert results[1]["summary"].endswith("sent as dive 8") and results[2]["summary"].endswith("sent as dive 9")
    assert c.ssiMessage.startswith("MySSI: sent 2, skipped 1, failed 0.")
    assert [d["odin_user_log_nr"] for d in fake.saved] == [8, 9]
    assert [d["odin_user_log_dive_sites_id"] for d in fake.saved] == [501, None]   # sending without a site is allowed
    assert fake.logins == 1 and fake.whats().count("save_divelog") == 2            # the token was reused
    # selecting again closes nothing stale: a new plan starts from the results
    c.clickRow(1, False, False)
    assert c.ssiResults and not c.ssiPlanOpen
    c.cancelSsi()
    assert c.ssiMessage == ""
    # a site database is re-downloaded on request
    c.downloadSites()
    assert wait_until(qapp, lambda: not c.ssiBusy)
    assert c.ssiMessage == "SSI site database downloaded: 3 sites."
    # a login MySSI refuses is reported on the page, not raised
    fake.password = "changed"
    fake.expire()
    c.prepareSsi()
    assert wait_until(qapp, lambda: not c.ssiBusy)
    assert c.ssiMessage.startswith("MySSI:") and "did not accept" in c.ssiMessage and not c.ssiPlanOpen


def test_settings_controller_ssi_login(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    """The MySSI card: one login in the keychain, a blank password keeps the
    stored one, Remove clears it, Test logs in off the GUI thread."""
    from desktop import credentials as creds_store
    from desktop.controllers.settings import SettingsController
    from src.core.services import ssi
    from tests.test_ssi import EMAIL, PASSWORD, FakeSsi
    monkeypatch.setattr(ssi, "UPLOAD_ENABLED", True)        # the card is hidden behind the switch since 2026-10-02
    s = SettingsController()
    assert s.ssiEnabled
    assert s.ssiEmail == "" and not s.ssiHasPassword and s.ssiStatus.startswith("No MySSI login saved yet.")
    s.testSsi("", "")
    assert s.ssiStatus == "Enter an email and password first."
    s.saveSsi(f" {EMAIL} ", PASSWORD)
    assert s.ssiEmail == EMAIL and s.ssiHasPassword and s.ssiStatus == f"Saved: {EMAIL}. Press Test to check the login."
    assert s.message == "Saved to keychain." and creds_store.load_ssi_credentials().password == PASSWORD
    s.saveSsi(EMAIL, "")                                       # blank keeps the stored password
    assert creds_store.load_ssi_credentials().password == PASSWORD
    fake = FakeSsi()
    s._ssi_client_kwargs = {"transport": fake, "sleep": lambda s: None}
    s.testSsi(EMAIL, "")
    assert s.ssiStatus == "Testing…"
    assert wait_until(qapp, lambda: s.ssiStatus != "Testing…")
    assert s.ssiStatus == "Login OK." and fake.whats() == ["authenticate"]
    assert creds_store.load_ssi_credentials().token == ""     # Test keeps no token; a send does
    s.testSsi(EMAIL, "wrong")
    assert wait_until(qapp, lambda: s.ssiStatus != "Testing…")
    assert s.ssiStatus == "Login failed."
    s.saveSsi("other@example.com", "")
    assert s.ssiStatus == "No password stored for other@example.com - enter it and save."
    s.clearSsi()
    assert s.ssiEmail == "" and not creds_store.load_ssi_credentials().email


def test_ssi_slots_refuse_while_the_switch_is_off(qapp, scratch_data_dir, fake_keyring, monkeypatch, caplog):
    """`ssi.UPLOAD_ENABLED` is False in the tree (owner's decision 2026-10-02,
    until the live test): every SSI slot of both controllers is a no-op with
    one log line - no MySSI call, no plan, no keychain write - so nothing in
    the UI can reach MySSI even if a stale page still called a slot. The
    keychain login itself stays readable (the helpers keep their tests)."""
    import logging
    import requests
    from desktop import credentials as creds_store
    from desktop.controllers.convert import ConvertController
    from desktop.controllers.settings import SettingsController
    from src.core.convert.formats import ReadResult
    from src.core.services import ssi
    from tests.test_ssi import EMAIL, PASSWORD, FakeSsi, make_dive

    def blocked(self, method, url, *a, **k):
        raise AssertionError(f"network blocked: {method} {url}")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    assert ssi.UPLOAD_ENABLED is False                      # the shipped state

    fake = FakeSsi()
    creds_store.save_ssi_credentials(EMAIL, PASSWORD)      # a login left from before the switch
    c = ConvertController()
    c._ssi_client_kwargs = {"transport": fake, "sleep": lambda s: None}
    c._apply_read(ReadResult(dives=[make_dive()], files=[("/x/a.fit", "fit")]))
    c.selectAll()
    assert not c.ssiEnabled and c.ssiConfigured            # configured, but switched off
    with caplog.at_level(logging.INFO, logger="dive_sync.desktop"):
        c.prepareSsi()
        c.downloadSites()
        c.sendToSsi()
        assert c.searchSites("reef") == []
        c.setSite(0, 501)
        assert not c.ssiBusy and not c.ssiPlanOpen and c.ssiPlan == [] and c.ssiResults == []
        assert c.ssiMessage == "" and fake.calls == [] and not c.ssiSitesDownloaded
        s = SettingsController()
        s._ssi_client_kwargs = {"transport": fake, "sleep": lambda s: None}
        assert not s.ssiEnabled
        s.saveSsi("other@example.com", "new")
        assert creds_store.load_ssi_credentials().email == EMAIL           # nothing written
        s.testSsi(EMAIL, PASSWORD)
        assert s.ssiStatus != "Testing…" and fake.calls == []
        s.clearSsi()
        assert creds_store.load_ssi_credentials().email == EMAIL           # nothing cleared
    refused = [r.getMessage() for r in caplog.records if "switched off" in r.getMessage()]
    assert len(refused) == 8 and all("ssi.UPLOAD_ENABLED" in m for m in refused)
    for action in ("prepareSsi", "downloadSites", "sendToSsi", "searchSites", "setSite", "saveSsi", "testSsi", "clearSsi"):
        assert any(action in m for m in refused), action
