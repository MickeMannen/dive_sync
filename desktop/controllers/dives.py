"""Dive editor backend: a ``QAbstractTableModel`` over the local dive cache
of one service, plus save / delete / refresh (rework.md D4, parity with the
Toga B5/B6/B9 editor). Column choice and sort order persist through
``desktop/preferences.py`` exactly as before."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from PySide6.QtCore import (
    Property,
    QAbstractTableModel,
    QModelIndex,
    QObject,
    Qt,
    Signal,
    Slot,
)

from desktop import credentials, preferences
from desktop.jobs import Worker
from src.core import dive_cache, scheduler

# (key, heading, numeric)
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
]
COLUMN_NUMERIC = {key: numeric for key, _, numeric in ALL_COLUMNS}
DEFAULT_VISIBLE_COLUMNS = [key for key, _, _ in ALL_COLUMNS[:9]]
COLUMN_WIDTHS = {"date": 100, "time": 80, "dive_number": 70, "location": 240, "max_depth": 100, "duration": 90,
                 "buddy": 110, "weight": 90, "visibility": 90, "water_temp": 110}


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

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._rows: List[Dict[str, Any]] = []
        self._columns: List[str] = list(DEFAULT_VISIBLE_COLUMNS)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._columns)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role not in (Qt.DisplayRole, Qt.EditRole):
            return None
        value = self._rows[index.row()].get(self._columns[index.column()])
        return "" if value is None else str(value)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal and section < len(self._columns):
            return dict((k, h) for k, h, _ in ALL_COLUMNS)[self._columns[section]]
        return str(section + 1)

    def roleNames(self):
        return {Qt.DisplayRole: b"display"}

    # -- api used by the controller ---------------------------------------

    def set_rows(self, rows: List[Dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def set_columns(self, columns: List[str]) -> None:
        self.beginResetModel()
        self._columns = [c for c in columns if c in COLUMN_NUMERIC] or list(DEFAULT_VISIBLE_COLUMNS)
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

    @property
    def columns(self) -> List[str]:
        return list(self._columns)


class DivesController(QObject):
    """One per service ("garmin" / "divelogs")."""

    modelChanged = Signal()
    statusChanged = Signal()
    listStatusChanged = Signal()
    busyChanged = Signal()
    selectedChanged = Signal()
    columnsChanged = Signal()
    sortChanged = Signal()
    confirmDelete = Signal(str)

    def __init__(self, service: str, log_queue=None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.service = service
        self._model = DiveTableModel(self)
        self._status = ""
        self._list_status = ""
        self._busy = False
        self._selected: Dict[str, Any] = {}
        self._log_queue = log_queue
        self._worker: Optional[Worker] = None
        self._sort_key, self._sort_ascending = preferences.get_sort(service, "date")
        self._model.set_columns(preferences.get_visible_columns(service, DEFAULT_VISIBLE_COLUMNS))

    # -- properties -------------------------------------------------------

    @Property(QObject, constant=True)
    def model(self):
        return self._model

    @Property(str, constant=True)
    def serviceName(self) -> str:
        return {"garmin": "Garmin Connect", "divelogs": "Divelogs.org"}.get(self.service, self.service)

    @Property(bool, constant=True)
    def tanksEditable(self) -> bool:
        # Garmin's gas API is read-only (rework.md E4); Divelogs accepts a
        # full tank list on every update.
        return self.service != "garmin"

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
        return [{"key": k, "label": h} for k, h, _ in ALL_COLUMNS]

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
        if self.service == "garmin":
            return dive_cache.list_garmin_dives()
        return dive_cache.list_divelogs_dives()

    @Slot()
    def load(self) -> None:
        try:
            rows = self._list_dives()
        except Exception as e:
            rows = []
            self._set("_status", f"Failed to load dives: {e}", self.statusChanged)
        self._model.set_rows(rows)
        self._model.sort_rows(self._sort_key, self._sort_ascending)
        self.select(-1)

    @Slot(int)
    def select(self, row: int) -> None:
        self._selected = self._model.row(row) if row >= 0 else {}
        self.selectedChanged.emit()

    @Slot("QVariantList")
    def setVisibleColumns(self, keys) -> None:
        keys = [str(k) for k in keys]
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

    # -- refresh (download this service only) ------------------------------

    @Slot()
    def refresh(self) -> None:
        if self._busy or scheduler.is_sync_running or scheduler.is_download_running:
            self._set("_list_status", "A sync or download is currently running — try again once it finishes.", self.listStatusChanged)
            return
        self._set("_busy", True, self.busyChanged)
        self._set("_list_status", "Refreshing…", self.listStatusChanged)
        include_garmin = self.service == "garmin"
        include_divelogs = self.service == "divelogs"

        def work():
            credentials.begin_operation()
            try:
                scheduler.run_download_thread(False, None, include_garmin, include_divelogs)
            finally:
                credentials.end_operation()
            return dict(scheduler.last_download_results)

        def done(results):
            self._set("_busy", False, self.busyChanged)
            if results.get("error"):
                text = f"Refresh failed: {results['error']}"
            elif results.get("success"):
                text = "Refreshed."
            else:
                text = "Refresh finished with errors — check the Sync tab log."
            self._set("_list_status", text, self.listStatusChanged)
            self.load()

        def fail(message):
            self._set("_busy", False, self.busyChanged)
            self._set("_list_status", f"Refresh failed: {message}", self.listStatusChanged)

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

    @Slot("QVariantMap")
    def save(self, fields) -> None:
        if not self._selected or self._busy:
            return
        if scheduler.is_sync_running or scheduler.is_download_running:
            self._set("_status", "A sync or download is currently running — try again once it finishes.", self.statusChanged)
            return
        fields = dict(fields)
        filename = self._selected["filename"]
        date_val = str(fields.get("date", "")).strip()
        time_val = str(fields.get("time", "")).strip()
        date_time = f"{date_val} {time_val}" if date_val and time_val else (date_val or time_val)
        kwargs = dict(
            dive_number=str(fields.get("dive_number", "")),
            date_time=date_time,
            duration=self._optional_int(str(fields.get("duration", ""))),
            max_depth=self._optional_float(str(fields.get("max_depth", ""))),
            location=str(fields.get("location", "")),
            notes=str(fields.get("notes", "")),
            weight=str(fields.get("weight", "")),
            visibility=str(fields.get("visibility", "")),
            buddy=str(fields.get("buddy", "")),
            lat=self._optional_float(str(fields.get("lat", ""))),
            lng=self._optional_float(str(fields.get("lng", ""))),
            water_temp=self._optional_float(str(fields.get("water_temp", ""))),
        )
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
        self._set("_busy", True, self.busyChanged)
        self._set("_status", "Saving…", self.statusChanged)

        def work():
            credentials.begin_operation()
            try:
                filepath = dive_cache.update_dive_fields(self.service, filename, **kwargs)
                return dive_cache.push_remote_update(self.service, filepath)
            finally:
                credentials.end_operation()

        def done(pushed):
            self._set("_busy", False, self.busyChanged)
            self._set("_status", "Saved." if pushed else "Saved locally (remote push failed — check credentials/log).", self.statusChanged)
            self.load()

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
        if not self._selected or self._busy:
            return
        if scheduler.is_sync_running or scheduler.is_download_running:
            self._set("_status", "A sync or download is currently running — try again once it finishes.", self.statusChanged)
            return
        filename = self._selected["filename"]
        self._set("_busy", True, self.busyChanged)
        self._set("_status", "Deleting…", self.statusChanged)

        def work():
            credentials.begin_operation()
            try:
                filepath, external_id = dive_cache.delete_dive_local(self.service, filename)
                if not external_id:
                    return False
                return dive_cache.push_remote_delete(self.service, external_id, filepath=filepath)
            finally:
                credentials.end_operation()

        def done(pushed):
            self._set("_busy", False, self.busyChanged)
            self._set("_status", "Deleted." if pushed else "Deleted locally (remote delete failed, skipped, or had no linked ID).", self.statusChanged)
            self.load()

        def fail(message):
            self._set("_busy", False, self.busyChanged)
            self._set("_status", f"Failed: {message}", self.statusChanged)

        self._run(work, done, fail)
