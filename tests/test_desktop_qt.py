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
    assert [t["id"] for t in c.targets] == ["uddf", "ssrf"] and c.targets[1]["extension"] == ".ssrf"
    assert c.openNameFilters[0].startswith("Dive files (") and c.saveNameFilters("uddf") == ["UDDF (*.uddf)"]
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
    """The Convert section sits between Conflicts and Settings (Q10); the page
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
    engine = desktop_app.create_engine(controllers, "Convert",
                                       on_warnings=lambda errs: warnings.extend(str(e.toString()) for e in errs))
    root = engine.rootObjects()[0]
    wait(qapp, 300)
    sections = root.property("sections").toVariant()
    assert sections.index("Convert") == sections.index("Conflicts") + 1 == sections.index("Settings") - 1
    assert root.property("currentSection") == sections.index("Convert")
    page = root.findChild(QObject, "convertPage")
    assert page is not None and page.findChild(QObject, "convertDiveList") is not None
    assert page.findChild(QObject, "saveTargets").property("count") == 2     # Save as UDDF…, Save as Subsurface…
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
