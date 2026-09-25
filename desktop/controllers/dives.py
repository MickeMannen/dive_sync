"""Dive editor backend: a ``QAbstractTableModel`` over the local dive cache
of one service, plus save / delete / refresh (rework.md D4, parity with the
Toga B5/B6/B9 editor). Column choice and sort order persist through
``desktop/preferences.py`` exactly as before."""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from PySide6.QtCore import (
    Property,
    QAbstractTableModel,
    QModelIndex,
    QObject,
    Qt,
    QTimer,
    Signal,
    Slot,
)

from desktop import credentials, preferences
from desktop.jobs import Worker
from src.core import dive_cache, scheduler

logger = logging.getLogger("dive_sync.desktop.dives")

# (key, heading, numeric). Every key here is one ``dive_cache.list_*_dives``
# puts on each row for both services; the first nine are what a fresh install
# shows, the rest are there to be switched on in the Columns dialog.
ALL_COLUMNS = [
    ("date", "Date", False),
    ("time", "Time", False),
    ("dive_number", "Dive #", True),
    ("location", "Location", False),
    ("max_depth", "Max Depth", True),
    ("duration", "Duration", True),
    ("buddy", "Buddy", False),
    ("weight", "Weight", True),
    ("visibility", "Visibility", True),
    ("water_temp", "Water temp", True),
    ("avg_depth", "Avg Depth", True),
    ("sac", "SAC (L/min)", True),
    ("tanks", "Tanks", False),
    ("notes", "Notes", False),
    ("id", "Dive ID", False),
]
# Columns only one service has a value for, so the other's dialog doesn't
# offer a column that would always be blank.
SERVICE_EXTRA_COLUMNS = {
    # Divelogs stores the id of the Garmin activity a dive came from; seeing
    # it is the quickest way to tell a synced dive from a native one.
    "divelogs": [("garmin_id", "Garmin ID", False)],
    # Whether the dive's original .fit has been downloaded (garmin_files).
    "garmin": [("fit", "FIT", False)],
}
# Service columns that belong next to a shared one rather than at the end:
# (column, the shared column it goes in front of). Garmin keeps a dive's
# title (activity name) apart from its location name, which is what its
# Location column shows.
SERVICE_PLACED_COLUMNS = {
    "garmin": [(("activity_name", "Activity name", False), "location")],
}
# Extra columns shown by default, and switched on once for a table whose
# saved column choice predates them (preferences.introduce_columns).
SERVICE_DEFAULT_ON = {"garmin": ["fit", "activity_name"]}


# Columns a service never has a value for, left out of its table and dialog.
SERVICE_HIDDEN_COLUMNS = {"subsurface": {"visibility"}}      # Subsurface rates visibility 0-5, no distance
# What a shared column means on a service, where it differs from the heading.
SERVICE_COLUMN_LABELS = {"subsurface": {"location": "Dive site"}, "divelogs": {"location": "Location, dive site"}}


def column_label(service: str, key: str) -> str:
    return SERVICE_COLUMN_LABELS.get(service, {}).get(key) or COLUMN_LABELS.get(key, key)


def columns_for(service: str) -> List[tuple]:
    columns = [c for c in ALL_COLUMNS if c[0] not in SERVICE_HIDDEN_COLUMNS.get(service, set())]
    for column, before in SERVICE_PLACED_COLUMNS.get(service, []):
        columns.insert(next(i for i, c in enumerate(columns) if c[0] == before), column)
    return columns + SERVICE_EXTRA_COLUMNS.get(service, [])


_EVERY_COLUMN = (ALL_COLUMNS + [c for extra in SERVICE_EXTRA_COLUMNS.values() for c in extra]
                 + [c for placed in SERVICE_PLACED_COLUMNS.values() for c, _ in placed])
COLUMN_NUMERIC = {key: numeric for key, _, numeric in _EVERY_COLUMN}
COLUMN_LABELS = {key: heading for key, heading, _ in _EVERY_COLUMN}
DEFAULT_VISIBLE_COLUMNS = [key for key, _, _ in ALL_COLUMNS[:9]]
COLUMN_WIDTHS = {"date": 100, "time": 80, "dive_number": 70, "location": 240, "max_depth": 100, "duration": 90,
                 "buddy": 110, "weight": 90, "visibility": 90, "water_temp": 110, "avg_depth": 100,
                 "sac": 100, "tanks": 180, "notes": 220, "id": 110, "garmin_id": 110, "fit": 50,
                 "activity_name": 220}
# The one column that takes whatever width is left over, so the table fits
# the window at any size instead of only near the size it was designed at:
# the fixed columns are all short, bounded values, and the location is the
# one that benefits from the extra room (and elides when there is none).
# COLUMN_WIDTHS["location"] is only its starting width now.
#
# ELASTIC_MIN_WIDTH is where it stops shrinking and the table scrolls
# horizontally instead. 100 is what makes the default nine columns fit at the
# app's minimum window size: 1100 window - 200 sidebar - 32 page margins - 28
# card padding - 2 border = 838 for a row, and the eight fixed default
# columns come to 730.
ELASTIC_COLUMN = "location"
ELASTIC_MIN_WIDTH = 100


def sort_key(value: Any, numeric: bool):
    if value in (None, ""):
        return (1, 0.0 if numeric else "")
    if numeric:
        m = re.match(r"\s*(-?\d+(?:\.\d+)?)", str(value))
        return (0, float(m.group(1)) if m else 0.0)
    return (0, str(value).lower())


class DiveTableModel(QAbstractTableModel):
    """Rows are the dicts ``dive_cache.list_*_dives`` returns; columns are
    the visible subset of ALL_COLUMNS."""

    def __init__(self, parent: Optional[QObject] = None, service: str = ""):
        super().__init__(parent)
        self._rows: List[Dict[str, Any]] = []
        # filename -> column values of a dive's staged (unsaved) edits, shown
        # in place of the stored ones so the table matches the form
        self._overlay: Dict[str, Dict[str, Any]] = {}
        self._deleting: set = set()      # filenames marked for deletion
        self._columns: List[str] = list(DEFAULT_VISIBLE_COLUMNS)
        # Which columns this service offers - the shared set plus its own.
        self._allowed = {key for key, _, _ in columns_for(service)}
        self._service = service

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._columns)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role not in (Qt.DisplayRole, Qt.EditRole):
            return None
        row = self._rows[index.row()]
        key = self._columns[index.column()]
        staged = self._overlay.get(row.get("filename"))
        value = staged[key] if staged and key in staged else row.get(key)
        return "" if value is None else str(value)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal and section < len(self._columns):
            return column_label(self._service, self._columns[section])
        return str(section + 1)

    def roleNames(self):
        return {Qt.DisplayRole: b"display"}

    # -- api used by the controller ---------------------------------------

    def set_rows(self, rows: List[Dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def set_deleting(self, filenames) -> None:
        self._deleting = set(filenames)

    def set_overlay(self, overlay: Dict[str, Dict[str, Any]]) -> None:
        self._overlay = overlay
        if self._rows and self._columns:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._rows) - 1, len(self._columns) - 1))

    def set_columns(self, columns: List[str]) -> None:
        self.beginResetModel()
        self._columns = [c for c in columns if c in self._allowed] or list(DEFAULT_VISIBLE_COLUMNS)
        self.endResetModel()

    def sort_rows(self, key: str, ascending: bool) -> None:
        """Blank values go last whichever way the sort runs."""
        numeric = COLUMN_NUMERIC.get(key, False)
        self.beginResetModel()
        filled = [r for r in self._rows if sort_key(r.get(key), numeric)[0] == 0]
        blank = [r for r in self._rows if sort_key(r.get(key), numeric)[0] == 1]
        filled.sort(key=lambda d: sort_key(d.get(key), numeric)[1], reverse=not ascending)
        self._rows = filled + blank
        self.endResetModel()

    @Slot(int, result="QVariantMap")
    def row(self, index: int) -> Dict[str, Any]:
        if 0 <= index < len(self._rows):
            return dict(self._rows[index])
        return {}

    @Slot(int, result=str)
    def columnKey(self, column: int) -> str:
        return self._columns[column] if 0 <= column < len(self._columns) else ""

    @Slot(int, result=int)
    def columnWidth(self, column: int) -> int:
        return COLUMN_WIDTHS.get(self.columnKey(column), 100)

    @Slot(int, result=bool)
    def isElasticColumn(self, column: int) -> bool:
        return self.columnKey(column) == ELASTIC_COLUMN

    @Slot(result=int)
    def fixedColumnsWidth(self) -> int:
        """Total width of every visible column except the elastic one - what
        the view subtracts from its own width to size that one."""
        return sum(COLUMN_WIDTHS.get(key, 100) for key in self._columns if key != ELASTIC_COLUMN)

    @Slot(result=int)
    def elasticMinWidth(self) -> int:
        return ELASTIC_MIN_WIDTH

    @property
    def columns(self) -> List[str]:
        return list(self._columns)


class DivesController(QObject):
    """One per service ("garmin" / "divelogs")."""

    modelChanged = Signal()
    statusChanged = Signal()
    listStatusChanged = Signal()
    diveCountChanged = Signal()
    busyChanged = Signal()
    progressChanged = Signal()
    selectedChanged = Signal()
    pendingChanged = Signal()
    columnsChanged = Signal()
    sortChanged = Signal()
    confirmDelete = Signal(str)

    def __init__(self, service: str, log_queue=None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.service = service
        self._model = DiveTableModel(self, service=service)
        self._status = ""
        self._list_status = ""
        self._busy = False
        # Polled from the GUI thread while a refresh runs on a worker thread
        # (rework.md E14): src.core.progress is where the adapters report.
        self._progress_fraction = -1.0
        self._progress_text = ""
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(400)
        self._progress_timer.timeout.connect(self._poll_progress)
        self._selected: Dict[str, Any] = {}
        self._pending: Dict[str, Dict[str, Any]] = {}
        # dives marked for deletion: removed from the service (and the cache)
        # by Save all changes, like staged edits; Undo unmarks one
        self._deleting: List[str] = []
        self._log_queue = log_queue
        self._worker: Optional[Worker] = None
        self._sort_key, self._sort_ascending = preferences.get_sort(service, "date")
        extra_on = SERVICE_DEFAULT_ON.get(service, [])
        columns = preferences.get_visible_columns(service, DEFAULT_VISIBLE_COLUMNS + extra_on)
        wanted = set(columns) | set(preferences.introduce_columns(service, extra_on))
        self._model.set_columns([key for key, _, _ in columns_for(service) if key in wanted])

    # -- properties -------------------------------------------------------

    @Property(QObject, constant=True)
    def model(self):
        return self._model

    @Property(str, constant=True)
    def serviceName(self) -> str:
        return {"garmin": "Garmin Connect", "divelogs": "Divelogs.org",
                "submersion": "Submersion", "subsurface": "Subsurface"}.get(self.service, self.service)

    @Property(bool, constant=True)
    def tanksEditable(self) -> bool:
        # Garmin's gas API is read-only (rework.md E4); Divelogs accepts a
        # full tank list on every update.
        return self.service != "garmin"

    @Property(str, constant=True)
    def locationLabel(self) -> str:
        """What the single location field means for this service (Garmin has two)."""
        return {"divelogs": "Location, dive site", "subsurface": "Dive site"}.get(self.service, "Location")

    @Property(str, constant=True)
    def locationHint(self) -> str:
        return "split at the first comma, e.g. Gozo, Blue Hole" if self.service == "divelogs" else ""

    @Property(bool, constant=True)
    def hasVisibility(self) -> bool:
        return "visibility" not in SERVICE_HIDDEN_COLUMNS.get(self.service, set())

    @Property(bool, constant=True)
    def profileLocksDepths(self) -> bool:
        """Subsurface: a dive with a recorded profile keeps its duration and
        depths (they follow from the profile); hand-logged ones take edits."""
        return self.service == "subsurface"

    @Property(bool, constant=True)
    def splitSiteNames(self) -> bool:
        """Garmin: Activity name and Location name are two fields."""
        return self.service == "garmin"

    @Property(bool, constant=True)
    def diveNumberEditable(self) -> bool:
        # Divelogs derives the dive number from where the dive falls in the
        # date/time order of the log, so it is not ours to set - editing it
        # there would either be ignored or renumber the log.
        return self.service != "divelogs"

    @Property(bool, constant=True)
    def fitSupported(self) -> bool:
        """Only Garmin keeps the original file a dive computer uploaded."""
        return self.service == "garmin"

    @Property(float, notify=progressChanged)
    def progressFraction(self) -> float:
        """0..1 while the job knows its size, -1 when it does not yet (the
        QML progress bar shows indeterminate for -1)."""
        return self._progress_fraction

    @Property(str, notify=progressChanged)
    def progressText(self) -> str:
        return self._progress_text

    @Property(str, notify=statusChanged)
    def status(self) -> str:
        return self._status

    @Property(str, notify=listStatusChanged)
    def listStatus(self) -> str:
        return self._list_status

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy

    @Property("QVariantMap", notify=selectedChanged)
    def selected(self):
        return dict(self._selected)

    @Property("QVariantList", constant=True)
    def allColumns(self):
        return [{"key": k, "label": column_label(self.service, k)} for k, _h, _ in columns_for(self.service)]

    @Property("QVariantList", notify=columnsChanged)
    def visibleColumns(self):
        return self._model.columns

    @Property(str, notify=sortChanged)
    def sortKey(self) -> str:
        return self._sort_key

    @Property(bool, notify=sortChanged)
    def sortAscending(self) -> bool:
        return self._sort_ascending

    def _set(self, attr: str, value, signal) -> None:
        setattr(self, attr, value)
        signal.emit()

    # -- listing ----------------------------------------------------------

    def _list_dives(self) -> List[Dict[str, Any]]:
        rows = dive_cache.list_dives(self.service)
        if self.service == "garmin":
            # The table's FIT column: M for a hand-logged dive (its FIT is one
            # Connect makes up from the typed-in fields), otherwise a tick
            # when the device's .fit is on disk and a cross when it is not.
            for r in rows:
                r["fit"] = "M" if r.get("manual") else ("✓" if r.get("fit_file") else "✗")
                # Location is Garmin's location name; the title has its own column
                r["location"] = r.get("location_name") or ""
        return rows

    @Slot()
    def load(self, keep_filename: Optional[str] = None) -> None:
        try:
            rows = self._list_dives()
        except Exception as e:
            rows = []
            self._set("_status", f"Failed to load dives: {e}", self.statusChanged)
        self._model.set_rows(rows)
        self._model.sort_rows(self._sort_key, self._sort_ascending)
        self.diveCountChanged.emit()
        # staged edits and deletions of dives no longer listed (deleted elsewhere) go
        dropped = [f for f in list(self._pending) + self._deleting if not self._row_for(f)]
        for f in dropped:
            self._pending.pop(f, None)
        self._deleting = [f for f in self._deleting if f not in dropped]
        if dropped:
            self._pending_changed()
        if not (keep_filename and self._select_filename(keep_filename)):
            self.select(-1)

    def _select_filename(self, filename: str) -> bool:
        index = next((i for i, r in enumerate(self._model._rows) if r.get("filename") == filename), -1)
        if index < 0:
            return False
        self.select(index)
        return True

    @Slot(str, result=int)
    def rowOf(self, filename: str) -> int:
        """Where a dive sits in the (sorted) table, -1 when it is not listed."""
        return next((i for i, r in enumerate(self._model._rows) if r.get("filename") == filename), -1)

    @Slot(int)
    def select(self, row: int) -> None:
        self._selected = self._model.row(row) if row >= 0 else {}
        staged = self._pending.get(self._selected.get("filename"))
        if staged:
            # the form shows the dive as edited, not as stored
            self._selected = dict(self._selected, **self._staged_as_row(staged), pending=True)
        if self._selected.get("filename"):
            try:
                self._selected["samples"] = dive_cache.get_samples(self.service, self._selected["filename"])
            except Exception as e:
                logger.warning("Failed to load samples for %s: %s", self._selected["filename"], e)
                self._selected["samples"] = []
        self.selectedChanged.emit()

    @Slot("QVariantList")
    def setVisibleColumns(self, keys) -> None:
        """Ordered by the catalogue, not by the order the boxes happened to be
        ticked in: otherwise a column switched off and back on jumps to the
        end of the table instead of returning to where it was."""
        wanted = {str(k) for k in keys}
        keys = [key for key, _, _ in columns_for(self.service) if key in wanted]
        self._model.set_columns(keys)
        preferences.set_visible_columns(self.service, self._model.columns)
        self.columnsChanged.emit()

    @Slot(str, bool)
    def setSort(self, key: str, ascending: bool) -> None:
        self._sort_key, self._sort_ascending = key, ascending
        preferences.set_sort(self.service, key, ascending)
        self._model.sort_rows(key, ascending)
        self.sortChanged.emit()
        self.select(-1)

    @Slot(str)
    def toggleSort(self, key: str) -> None:
        """A click on a column header: sort by it, or flip the direction when
        it is the column already being sorted by. A new column starts
        ascending, except for the date/time ones - newest first is what you
        want from those, and it is how the cached listing already arrives."""
        if not key:
            return
        if key == self._sort_key:
            self.setSort(key, not self._sort_ascending)
        else:
            self.setSort(key, key not in ("date", "time", "date_time"))

    # -- refresh (download this service only) ------------------------------

    def _poll_progress(self) -> None:
        from src.core import progress as progress_module
        state = progress_module.current()
        fraction = -1.0 if not state or state.get("fraction") is None else float(state["fraction"])
        text = progress_module.text() if state else ""
        if (fraction, text) != (self._progress_fraction, self._progress_text):
            self._progress_fraction, self._progress_text = fraction, text
            self.progressChanged.emit()

    def _start_progress(self) -> None:
        self._progress_fraction, self._progress_text = -1.0, ""
        self.progressChanged.emit()
        self._progress_timer.start()

    def _stop_progress(self) -> None:
        self._progress_timer.stop()
        self._progress_fraction, self._progress_text = -1.0, ""
        self.progressChanged.emit()

    @Slot()
    def refreshAll(self) -> None:
        """Re-fetch every dive, ignoring the cache. A plain Refresh skips a
        dive whose listing entry is unchanged, and the listing carries no
        notes, buddy, weight or visibility - so an edit to only those on
        Garmin needs this (rework.md E15)."""
        self.refresh(force=True)

    @Slot()
    def refresh(self, force: bool = False) -> None:
        if self._busy or scheduler.is_sync_running or scheduler.is_download_running:
            self._set("_list_status", "A sync or download is currently running — try again once it finishes.", self.listStatusChanged)
            return
        self._set("_busy", True, self.busyChanged)
        self._set("_list_status", ("Re-fetching every dive and its FIT file…" if self.service == "garmin" else "Re-fetching every dive…")
                  if force else "Refreshing…", self.listStatusChanged)
        self._start_progress()
        service = self.service

        def work():
            credentials.begin_operation()
            try:
                # a Garmin full refresh replaces the saved FIT files too
                scheduler.run_download_thread(force, None, services=[service],
                                              refresh_fits=force and service == "garmin")
            finally:
                credentials.end_operation()
            return dict(scheduler.last_download_results)

        def done(results):
            self._set("_busy", False, self.busyChanged)
            self._stop_progress()
            if results.get("error"):
                text = f"Refresh failed: {results['error']}"
            elif results.get("success"):
                text = "Refreshed."
            else:
                text = "Refresh finished with errors — check the Sync tab log."
            fits = results.get("fit")
            if fits and not results.get("error"):
                text += f" {len(fits['downloaded'])} FIT file(s) replaced"
                text += f", {len(fits['failed'])} failed." if fits["failed"] else "."
            self._set("_list_status", text, self.listStatusChanged)
            self.load()

        def fail(message):
            self._set("_busy", False, self.busyChanged)
            self._stop_progress()
            self._set("_list_status", f"Refresh failed: {message}", self.listStatusChanged)

        self._run(work, done, fail)

    # -- FIT download (Garmin only) ---------------------------------------

    @Slot()
    def downloadFit(self) -> None:
        """The selected dive's original .fit."""
        if self._selected.get("filename"):
            self._download_fits([self._selected["filename"]])

    @Slot()
    def downloadMissingFits(self) -> None:
        """Every listed dive whose .fit is not on disk yet."""
        filenames = [r["filename"] for r in self._model._rows
                     if r.get("filename") and not r.get("fit_file") and not r.get("manual")]
        if not filenames:
            self._set("_list_status", "Every dive's FIT is already downloaded.", self.listStatusChanged)
            return
        self._download_fits(filenames)

    def _download_fits(self, filenames: List[str]) -> None:
        if not self.fitSupported:
            return
        if self._busy or scheduler.is_sync_running or scheduler.is_download_running:
            self._set("_list_status", "A sync or download is currently running — try again once it finishes.", self.listStatusChanged)
            return
        self._set("_busy", True, self.busyChanged)
        self._set("_list_status", f"Downloading {len(filenames)} FIT file(s)…", self.listStatusChanged)
        self._start_progress()

        def work():
            credentials.begin_operation()
            try:
                return scheduler.run_fit_download_thread(filenames)
            finally:
                credentials.end_operation()

        def done(result):
            self._set("_busy", False, self.busyChanged)
            self._stop_progress()
            if result.get("error"):
                text = f"FIT download failed: {result['error']}"
            else:
                text = f"Downloaded {len(result['downloaded'])} FIT file(s)."
                failed = result.get("failed") or {}
                if failed:
                    first = next(iter(failed.items()))
                    text += f" {len(failed)} failed ({first[0]}: {first[1]}{', …' if len(failed) > 1 else ''})."
            self._set("_list_status", text, self.listStatusChanged)
            self.load()

        def fail(message):
            self._set("_busy", False, self.busyChanged)
            self._stop_progress()
            self._set("_list_status", f"FIT download failed: {message}", self.listStatusChanged)

        self._run(work, done, fail)

    def _run(self, work, done, fail) -> None:
        worker = Worker(work, parent=self)
        worker.finished_ok.connect(done)
        worker.failed.connect(fail)
        self._worker = worker
        worker.start()

    # -- save / delete ----------------------------------------------------

    @staticmethod
    def _optional_int(text):
        text = (text or "").strip()
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            return None

    @staticmethod
    def _optional_float(text):
        text = (text or "").strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def _pending_changed(self) -> None:
        self._model.set_overlay({f: self._staged_columns(fields) for f, fields in self._pending.items()})
        self._model.set_deleting(set(self._deleting))
        self.pendingChanged.emit()

    def _staged_columns(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        """The table's column values for a dive's staged edits, formatted the
        way the list formats stored ones (dive_cache.list_*_dives)."""
        out: Dict[str, Any] = {}
        for key in ("date", "time", "duration", "max_depth", "buddy", "weight", "visibility", "notes"):
            if key in fields:
                out[key] = str(fields[key])
        if self.diveNumberEditable and "dive_number" in fields:
            out["dive_number"] = str(fields["dive_number"])
        if self.splitSiteNames:
            if "activity_name" in fields:
                out["activity_name"] = str(fields["activity_name"])
            if "location_name" in fields:
                out["location"] = str(fields["location_name"])
        elif "location" in fields:
            out["location"] = str(fields["location"])
        if "water_temp" in fields:
            temp = self._optional_float(str(fields["water_temp"] or ""))
            out["water_temp"] = dive_cache._format_water_temp(temp, temp, temp) if temp is not None else ""
        if self.tanksEditable and fields.get("tanks") is not None:
            tanks = [{k: (self._optional_float(str(t.get(k, ""))) if k != "tank_name" else (t.get(k) or None))
                      for k in ("tank_name", "oxygen", "helium", "volume", "start_pressure", "end_pressure")}
                     for t in fields["tanks"]]
            out["tanks"] = dive_cache._format_tanks(tanks)
        return out

    def _staged_as_row(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        """Staged form values under the row keys the form reads."""
        # the form sends every field, the hidden ones of the other layout too
        hidden = ("location",) if self.splitSiteNames else ("activity_name", "location_name")
        out = {k: v for k, v in fields.items() if k not in ("tanks", "water_temp") + hidden}
        if "water_temp" in fields:
            out["water_temp_value"] = fields["water_temp"]
        if "tanks" in fields:
            out["tanks_detail"] = fields["tanks"]
        if self.splitSiteNames and "location_name" in fields:
            out["location"] = fields["location_name"]
        return out

    @classmethod
    def _changed_coord(cls, text, current):
        """The edited coordinate, or None (leave it) when the form still shows
        the stored one - the form rounds to 6 decimals, and saving that back
        would nudge the dive's position on the service."""
        value = cls._optional_float(str(text if text is not None else ""))
        try:
            if value is not None and current not in (None, "") and round(float(current), 6) == round(value, 6):
                return None
        except (TypeError, ValueError):
            pass
        return value

    # Edits are staged per dive (``_pending``: filename -> the form's values)
    # and uploaded when the diver saves that dive or all of them: Garmin takes
    # seconds per dive, so editing several and saving once beats waiting on
    # every field.

    def _kwargs_for(self, filename: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        """``update_dive_fields`` arguments from the form's values."""
        original = self._row_for(filename) or {}
        date_val = str(fields.get("date", "")).strip()
        time_val = str(fields.get("time", "")).strip()
        date_time = f"{date_val} {time_val}" if date_val and time_val else (date_val or time_val)
        kwargs = dict(
            date_time=date_time,
            duration=self._optional_int(str(fields.get("duration", ""))),
            max_depth=self._optional_float(str(fields.get("max_depth", ""))),
            notes=str(fields.get("notes", "")),
            weight=str(fields.get("weight", "")),
            visibility=str(fields.get("visibility", "")),
            buddy=str(fields.get("buddy", "")),
            lat=self._changed_coord(fields.get("lat", ""), original.get("lat")),
            lng=self._changed_coord(fields.get("lng", ""), original.get("lng")),
            water_temp=self._optional_float(str(fields.get("water_temp", ""))),
        )
        # Garmin keeps the activity's title and its location name apart; the
        # other services have one location text.
        if self.service == "garmin":
            kwargs["activity_name"] = str(fields.get("activity_name", ""))
            kwargs["location_name"] = str(fields.get("location_name", ""))
        else:
            kwargs["location"] = str(fields.get("location", ""))
        # Divelogs owns its own numbering (see diveNumberEditable), so it is
        # never sent there - not even if the form managed to hand one over.
        if self.diveNumberEditable:
            kwargs["dive_number"] = str(fields.get("dive_number", ""))
        if self.tanksEditable and fields.get("tanks") is not None:
            kwargs["tanks"] = [
                {
                    "tank_name": str(t.get("tank_name", "")) or None,
                    "oxygen": self._optional_float(str(t.get("oxygen", ""))),
                    "helium": self._optional_float(str(t.get("helium", ""))),
                    "volume": self._optional_float(str(t.get("volume", ""))),
                    "start_pressure": self._optional_float(str(t.get("start_pressure", ""))),
                    "end_pressure": self._optional_float(str(t.get("end_pressure", ""))),
                }
                for t in fields.get("tanks")
            ]
        return kwargs

    def _row_for(self, filename: str) -> Optional[Dict[str, Any]]:
        return next((r for r in self._model._rows if r.get("filename") == filename), None)

    @Property(int, notify=diveCountChanged)
    def diveCount(self) -> int:
        """How many dives the page lists (the local cache of the service)."""
        return self._model.rowCount()

    @Property(int, notify=pendingChanged)
    def pendingCount(self) -> int:
        return len(self.pendingFiles)

    @Property("QVariantList", notify=pendingChanged)
    def pendingFiles(self):
        """Every dive with an unsaved change: edited or marked for deletion."""
        return list(self._pending) + [f for f in self._deleting if f not in self._pending]

    @Property("QVariantList", notify=pendingChanged)
    def deletingFiles(self):
        return list(self._deleting)

    @Slot(str, "QVariantMap")
    def stage(self, filename: str, fields) -> None:
        """Keep the form's values for ``filename`` until it is saved or undone."""
        if not filename or not self._row_for(filename) or filename in self._deleting:
            return
        self._pending[filename] = dict(fields)
        self._pending_changed()

    @Slot(str)
    def discard(self, filename: str) -> None:
        """Undo: drop the dive's staged edits (or its deletion mark) and show
        its stored values again."""
        self._pending.pop(filename, None)
        if filename in self._deleting:
            self._deleting.remove(filename)
        self._pending_changed()
        if self._selected.get("filename") == filename:
            self._select_filename(filename)
        self._set("_status", "Changes undone.", self.statusChanged)

    @Slot("QVariantMap")
    def save(self, fields) -> None:
        """Stage ``fields`` for the selected dive and upload that dive."""
        if not self._selected.get("filename"):
            return
        self.stage(self._selected["filename"], fields)
        self.saveDive(self._selected["filename"])

    @Property(bool, constant=True)
    def saveStagesOnly(self) -> bool:
        """Garmin: a dive's Save only stages it and Save all sends every staged
        dive in one go - each upload takes seconds. Elsewhere Save uploads."""
        return self.service == "garmin"

    @Slot(str)
    def saveDive(self, filename: str) -> None:
        if filename in self._deleting:
            self._set("_status", f"Marked for deletion: Save all changes deletes it from {self.serviceName}.",
                      self.statusChanged)
        elif filename not in self._pending:
            self._set("_status", "Nothing to save for this dive.", self.statusChanged)
        elif self.saveStagesOnly:
            self._set("_status", f"Staged. Press Save all changes to send {len(self._pending)} dive(s) to "
                                 f"{self.serviceName}.", self.statusChanged)
        else:
            self._save_files([filename])

    @Slot()
    def saveAll(self) -> None:
        if self._pending or self._deleting:
            self._save_files(list(self._pending), list(self._deleting))
        else:
            self._set("_status", "No unsaved changes.", self.statusChanged)

    def _save_files(self, filenames: List[str], deletes: List[str] = ()) -> None:
        if self._busy:
            return
        if scheduler.is_sync_running or scheduler.is_download_running:
            self._set("_status", "A sync or download is currently running — try again once it finishes.", self.statusChanged)
            return
        jobs = [(f, self._kwargs_for(f, self._pending[f])) for f in filenames if self._row_for(f)]
        deletes = [f for f in deletes if self._row_for(f)]
        keep_selected = self._selected.get("filename")
        self._set("_busy", True, self.busyChanged)
        total = len(jobs) + len(deletes)
        self._set("_status", f"Saving {total} change(s) to {self.serviceName}…" if total > 1 else "Saving…",
                  self.statusChanged)

        def work():
            credentials.begin_operation()
            results = {}
            try:
                for filename, kwargs in jobs:
                    try:
                        filepath = dive_cache.update_dive_fields(self.service, filename, **kwargs)
                        results[filename] = bool(dive_cache.push_remote_update(self.service, filepath))
                    except Exception as e:
                        logger.error("Saving %s failed: %s", filename, e)
                        results[filename] = False
                for filename in deletes:
                    results[filename] = self._delete_one(filename)
            finally:
                credentials.end_operation()
            return results

        def done(results):
            self._set("_busy", False, self.busyChanged)
            saved = [f for f, ok in results.items() if ok]
            for f in saved:
                self._pending.pop(f, None)
                if f in self._deleting:
                    self._deleting.remove(f)
            self._pending_changed()
            failed = len(results) - len(saved)
            removed = sum(1 for f in saved if f in deletes)
            done_text = f"Saved {len(saved) - removed} dive(s), deleted {removed}." if removed else f"Saved {len(saved)} dives."
            if not failed:
                status = "Saved." if len(results) == 1 and not removed else ("Deleted." if len(results) == 1 else done_text)
            elif len(results) == 1:
                status = ("Delete failed on " + self.serviceName + " - still marked, Save all retries it."
                          if deletes else "Saved locally (remote push failed — check credentials/log). Still pending: Save retries it.")
            else:
                status = f"{len(saved)} of {len(results)} changes done; {failed} failed (still pending, Save all retries them)."
            self._set("_status", status, self.statusChanged)
            self.load(keep_selected)

        def fail(message):
            self._set("_busy", False, self.busyChanged)
            self._set("_status", f"Failed: {message}", self.statusChanged)

        self._run(work, done, fail)

    @Slot()
    def requestDelete(self) -> None:
        if self._selected:
            self.confirmDelete.emit(f"Delete this dive from {self.serviceName} (removes the local cache file and the remote dive)?")

    @Slot()
    def deleteSelected(self) -> None:
        """Mark the selected dive for deletion; Save all changes deletes it
        from the service, Undo unmarks it."""
        filename = self._selected.get("filename")
        if not filename:
            return
        self._pending.pop(filename, None)
        if filename not in self._deleting:
            self._deleting.append(filename)
        self._pending_changed()
        self._select_filename(filename)
        self._set("_status", f"Marked for deletion. Save all changes deletes it from {self.serviceName}; "
                             "Undo keeps it.", self.statusChanged)

    def _delete_one(self, filename: str) -> bool:
        """Worker thread: delete online first, then the cache file - a failed
        online delete leaves the dive marked and cached, to retry."""
        try:
            filepath, external_id = dive_cache.dive_external_id(self.service, filename)
            if external_id and not dive_cache.push_remote_delete(self.service, external_id, filepath=filepath):
                return False
            if not external_id:
                logger.warning("%s has no %s id; removing it from the local cache only.", filename, self.serviceName)
            dive_cache.delete_dive_local(self.service, filename)
            return True
        except Exception as e:
            logger.error("Deleting %s failed: %s", filename, e)
            return False
