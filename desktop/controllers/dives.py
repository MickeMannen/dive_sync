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

from desktop import accounts, credentials, preferences
from desktop.jobs import Worker
from src.core import dive_cache, scheduler
from src.core import units as core_units
from src.core.convert import profile
from desktop.controllers.editing import display_number, js_number, si_number  # shared with the Dive edit page (I14)

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
    ("air_temp", "Air temp", True),
    ("water_type", "Water type", False),
    ("suit", "Suit", False),
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
    # Whether the dive's original .fit has been downloaded (garmin_files),
    # and the dive computer that recorded it (garmin_files.device_name).
    "garmin": [("fit", "FIT", False), ("device", "Dive computer", False)],
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
SERVICE_DEFAULT_ON = {"garmin": ["fit", "activity_name", "device"]}


# Columns a service never has a value for, left out of its table and dialog.
# The air temperature, water type and suit (R10) are held by the services cached as
# UnifiedDive; Garmin's raw cache has no slot for them, Divelogs' only for the
# air temperature (its ``airtemp``, R10b).
_NO_R10_COLUMNS = {"air_temp", "water_type", "suit"}
SERVICE_HIDDEN_COLUMNS = {"subsurface": {"visibility"},      # Subsurface rates visibility 0-5, no distance
                          "garmin": set(_NO_R10_COLUMNS), "divelogs": {"water_type", "suit"}}
# What a shared column means on a service, where it differs from the heading.
SERVICE_COLUMN_LABELS = {"subsurface": {"location": "Dive site"}, "divelogs": {"location": "Location, dive site"},
                         "ssi": {"location": "Dive site"}}      # MySSI: a site from SSI's own database
SERVICE_NAMES = {"garmin": "Garmin Connect", "divelogs": "Divelogs.org", "submersion": "Submersion",
                 "subsurface": "Subsurface", "ssi": "MySSI"}


def adapter_flag(service: str, name: str, default: bool = True) -> bool:
    """A page-level fact read from the service's adapter class
    (``accepts_updates`` and the like, src/core/adapter.py) without building
    an adapter; ``default`` for a service with no registered adapter."""
    from src.core.pairs import adapter_class_of
    try:
        return bool(getattr(adapter_class_of(service), name, default))
    except ValueError:
        return default


def column_label(service: str, key: str, units: str = core_units.METRIC) -> str:
    """A column's heading on a service; the SAC heading names the unit system
    it is shown in (plans/units.md J3), the others carry no unit."""
    if key == "sac":
        return f"SAC ({core_units.sac_label(units)})"
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
                 "sac": 100, "tanks": 180, "notes": 220, "id": 110, "garmin_id": 110, "fit": 50, "device": 150,
                 "activity_name": 220, "air_temp": 90, "water_type": 90, "suit": 120}
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


def _garmin_table_values(rows: List[Dict[str, Any]]) -> None:
    for r in rows:
        # The table's FIT column: M for a hand-logged dive (its FIT is one
        # Connect makes up from the typed-in fields), otherwise a tick
        # when the device's .fit is on disk and a cross when it is not.
        r["fit"] = "M" if r.get("manual") else ("✓" if r.get("fit_file") else "✗")
        # Location is Garmin's location name; the title has its own column
        r["location"] = r.get("location_name") or ""


# ---------------------------------------------------------------------------
# Units (rework.md Track J, plans/units.md J3). The rows dive_cache lists are
# SI (metres, bar, °C, litres; weight and visibility in the dive's own unit,
# "4 kg" / "30 ft"), and so is everything the save path sends. Only what the
# table and the form show converts, here, through src/core/units.py: in
# metric every value is shown exactly as stored (nothing is reformatted), in
# imperial converted and rounded per the display rule. Sorting always runs
# on the stored rows, so the order is the same in both systems.

_IMPERIAL_SUFFIXES = ("lb", "lbs", "pound", "pounds", "ft", "foot", "feet")
_NUMBER_WITH_UNIT = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")
# The tank table's numbers and their kinds (name, O2 and He carry no unit).
_TANK_NUMBERS = (("volume", "volume"), ("start_pressure", "pressure"), ("end_pressure", "pressure"))


def _split_unit(text: Any) -> tuple:
    """``"4 kg"`` -> (4.0, "kg"), ``"10"`` -> (10.0, ""), anything else -> (None, "")."""
    m = _NUMBER_WITH_UNIT.match("" if text is None else str(text))
    return (float(m.group(1)), m.group(2).lower()) if m else (None, "")


def _unit_system(suffix: str) -> str:
    """The system a weight / visibility suffix belongs to; none means metric
    (the services' default: kilograms, metres)."""
    return core_units.IMPERIAL if suffix in _IMPERIAL_SUFFIXES else core_units.METRIC


def tidy_unit_text(text: Any) -> str:
    """A weight / visibility text with its number shortened for the table
    (``"4.535923700100354 kg"`` -> ``"4.53592 kg"``, ``"4 kg"`` as it is):
    the staged overlay only, the stored full-precision text is what is
    saved."""
    number, suffix = _split_unit(text)
    if number is None:
        return "" if text is None else str(text)
    return f"{number:g} {suffix}".strip()


def unit_text(kind: str, text: Any, system: str) -> str:
    """A weight (``"4 kg"``, ``"10 lbs"``, ``"4"``) or visibility (``"10 m"``,
    ``"30 ft"``; ``kind`` is ``depth``) as the row holds it, shown in
    ``system``: a value already in that system (a bare number counts as
    metric) is shown exactly as stored, one in the other system is converted
    and rounded per the display rule, with the system's label."""
    number, suffix = _split_unit(text)
    if number is None or _unit_system(suffix) == system:
        return "" if text is None else str(text)
    si = core_units.convert_back(kind, number, _unit_system(suffix))
    return f"{core_units.format_value(kind, si, system)} {core_units.unit_label(kind, system)}"


def display_value(row: Dict[str, Any], key: str, system: str) -> str:
    """One table cell: the row's value as text, converted for ``system`` when
    the column carries a unit (depths, SAC, the water temperature and tank
    summaries, weight, visibility)."""
    value = row.get(key)
    # weight and visibility are in the dive's own unit: a dive logged in
    # pounds / feet converts to metric too (one in the shown system is left
    # exactly as stored)
    if key == "weight":
        return unit_text("weight", value, system)
    if key == "visibility":
        return unit_text("depth", value, system)
    if system != core_units.METRIC:
        if key in ("max_depth", "avg_depth"):
            return display_number("depth", value, system)
        if key == "sac":
            return display_number("sac", value, system)
        if key == "air_temp":
            return display_number("temperature", value, system)
        if key == "water_temp" and row.get("water_temps") is not None:
            return dive_cache._format_water_temp(*row["water_temps"], units=system)
        if key == "tanks" and row.get("tanks_detail") is not None:
            return dive_cache._format_tanks(row["tanks_detail"], system)
    return "" if value is None else str(value)


def display_tank(tank: Dict[str, Any], system: str) -> Dict[str, Any]:
    """One tank of the editable tank table, its volume and pressures
    converted (as text) for ``system``; untouched in metric."""
    if system == core_units.METRIC:
        return tank
    out = dict(tank)
    for key, kind in _TANK_NUMBERS:
        if tank.get(key) not in (None, ""):
            out[key] = display_number(kind, tank[key], system)
    return out


def display_row(row: Dict[str, Any], system: str) -> Dict[str, Any]:
    """The row as the form shows it: a copy as stored in metric (except a
    weight or visibility the dive holds in pounds / feet, converted); in
    imperial with every unit-bearing value converted (``display_value``),
    the editable water temperature as converted text and the tank table's
    numbers converted too. The stored row is never changed."""
    out = dict(row)
    for key in ("weight", "visibility"):
        if key in row:
            out[key] = display_value(row, key, system)
    if system == core_units.METRIC:
        return out
    for key in ("max_depth", "avg_depth", "sac", "air_temp", "water_temp", "tanks"):
        if key in row:
            out[key] = display_value(row, key, system)
    if row.get("water_temp_value") not in (None, ""):
        out["water_temp_value"] = display_number("temperature", row["water_temp_value"], system)
    if row.get("tanks_detail"):
        out["tanks_detail"] = [display_tank(t, system) for t in row["tanks_detail"]]
    return out


def si_unit_text(kind: str, typed: Any, shown: str, original: Any, system: str) -> str:
    """A typed weight / visibility as the save path takes it (text, the
    adapters parse the unit suffix): the stored text while the field is
    untouched; a number typed in the chosen system (bare, or with that
    system's unit) converted to the dive's own unit so the service's unit
    field stays what it was, at full precision and with the stored suffix
    (or none, as stored; a bare number on a dive stored with a suffix gets
    that suffix too); a number typed with the other system's unit, or
    text that is no number, sent exactly as typed, as before. With nothing
    stored, a bare number typed in imperial gets that system's label (the
    adapters read a bare number as metric)."""
    typed_text = ("" if typed is None else str(typed)).strip()
    if typed_text == shown.strip():
        return str(original) if original not in (None, "") else typed_text
    number, suffix = _split_unit(typed_text)
    if number is None or (suffix and _unit_system(suffix) != system):
        return typed_text
    own_number, own_suffix = _split_unit(original)
    if own_number is None:
        # nothing stored to keep the unit of: a bare number is in the chosen
        # system, and the adapters take a bare number as metric
        return typed_text if suffix or system == core_units.METRIC else f"{typed_text} {core_units.unit_label(kind, system)}"
    own_system = _unit_system(own_suffix)
    if own_system == system:
        # a bare number keeps the stored suffix, so the adapter keeps the unit
        return typed_text if suffix or not own_suffix else f"{typed_text} {own_suffix}"
    value = core_units.convert(kind, core_units.convert_back(kind, number, system), own_system)
    return f"{value!r} {own_suffix}".strip()


def si_tanks(typed: List[Dict[str, Any]], shown: List[Dict[str, Any]], original: List[Dict[str, Any]],
             system: str) -> List[Dict[str, Any]]:
    """The tank table's typed values with volumes and pressures brought back
    to litres and bar (``si_number`` per cell, against the tank shown at the
    same place); names and gas fractions pass through."""
    out = []
    for i, tank in enumerate(typed):
        shown_tank = shown[i] if i < len(shown) else {}
        original_tank = original[i] if i < len(original) else {}
        out.append(dict(tank, **{key: si_number(kind, tank[key], js_number(shown_tank.get(key)), original_tank.get(key), system)
                                 for key, kind in _TANK_NUMBERS if key in tank}))
    return out


def sort_value(row: Dict[str, Any], key: str) -> Any:
    """What a row is sorted on for a column: its value, except that sorting
    by date orders the dives of one day by their time too, whichever way
    the sort runs."""
    if key == "date" and row.get("date"):
        return f"{row['date']} {row.get('time') or ''}".strip()
    return row.get(key)


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
        # the unit system the cells and the SAC heading are shown in; the
        # rows stay SI (display_value)
        self._units = core_units.METRIC

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
        if staged and key in staged:
            value = staged[key]      # already display text (DivesController._staged_columns)
            return "" if value is None else str(value)
        return display_value(row, key, self._units)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal and section < len(self._columns):
            return column_label(self._service, self._columns[section], self._units)
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

    def set_units(self, system: str) -> None:
        """Show every cell (and the SAC heading) in ``system``; the rows and
        the sort are untouched."""
        self._units = system
        if self._rows and self._columns:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._rows) - 1, len(self._columns) - 1))
        if self._columns:
            self.headerDataChanged.emit(Qt.Horizontal, 0, len(self._columns) - 1)

    def set_columns(self, columns: List[str]) -> None:
        self.beginResetModel()
        self._columns = [c for c in columns if c in self._allowed] or list(DEFAULT_VISIBLE_COLUMNS)
        self.endResetModel()

    def sort_rows(self, key: str, ascending: bool) -> None:
        """Blank values go last whichever way the sort runs."""
        numeric = COLUMN_NUMERIC.get(key, False)
        self.beginResetModel()
        filled = [r for r in self._rows if sort_key(sort_value(r, key), numeric)[0] == 0]
        blank = [r for r in self._rows if sort_key(sort_value(r, key), numeric)[0] == 1]
        filled.sort(key=lambda d: sort_key(sort_value(d, key), numeric)[1], reverse=not ascending)
        self._rows = filled + blank
        self.endResetModel()

    def remove_row(self, filename: str) -> None:
        at = next((i for i, r in enumerate(self._rows) if r.get("filename") == filename), -1)
        if at >= 0:
            self.beginRemoveRows(QModelIndex(), at, at)
            del self._rows[at]
            self.endRemoveRows()

    def merge_rows(self, rows: List[Dict[str, Any]], key: str, ascending: bool) -> None:
        """Rows that arrived while a refresh runs: each replaces the listed
        dive it is (by Garmin activity id, as a renamed file keeps its id,
        else by file name) or goes in at its sorted place. Row inserts, not
        a reset, so the table keeps its scroll position."""
        numeric = COLUMN_NUMERIC.get(key, False)

        def goes_before(a, b) -> bool:
            ka, kb = sort_key(sort_value(a, key), numeric), sort_key(sort_value(b, key), numeric)
            if ka[0] != kb[0]:
                return ka[0] < kb[0]            # blanks last either way
            return ka[1] < kb[1] if ascending else ka[1] > kb[1]

        for new in rows:
            same = next((i for i, r in enumerate(self._rows)
                         if (new.get("id") and r.get("id") == new.get("id")) or r.get("filename") == new.get("filename")), -1)
            if same >= 0:
                self.beginRemoveRows(QModelIndex(), same, same)
                del self._rows[same]
                self.endRemoveRows()
            at = next((i for i, r in enumerate(self._rows) if goes_before(new, r)), len(self._rows))
            self.beginInsertRows(QModelIndex(), at, at)
            self._rows.insert(at, new)
            self.endInsertRows()

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
    """One per service with a local dive cache (``garmin``, ``divelogs``,
    ``subsurface``, ``ssi``; pairs.CACHE_SERVICE_ORDER), each its own page."""

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
    accountChanged = Signal()
    savingChanged = Signal()
    # Emitted on the save worker thread, delivered queued on the GUI thread:
    # (dives done, total, the dive being saved now, how to name it) before
    # each dive, and (filename, ok, was a delete) after it.
    _saveStep = Signal(int, int, str, str)
    _itemSaved = Signal(str, bool, bool)

    def __init__(self, service: str, log_queue=None, parent: Optional[QObject] = None, units=None):
        """``units`` is the app's ``UnitsController`` (plans/units.md J2):
        the page shows its system and follows ``unitsChanged``; without one
        everything is metric."""
        super().__init__(parent)
        self.service = service
        self._model = DiveTableModel(self, service=service)
        self._units_controller = units
        if units is not None:
            units.unitsChanged.connect(self._on_units_changed)
            self._model.set_units(self._system())
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
        # While a Garmin refresh runs, dives are listed as they are cached
        # (one every few seconds) instead of all at the end.
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(1500)
        self._live_timer.timeout.connect(self._list_arrived)
        self._live_known: Optional[Dict[str, float]] = None
        self._live_account: Optional[str] = None
        self._selected: Dict[str, Any] = {}
        self._profile = None                    # the selected dive's samples as a UnifiedDive, for sampleDetails (I19)
        # filename -> the form's values brought back to SI (_si_fields): what
        # the overlay formats and the save sends
        self._pending: Dict[str, Dict[str, Any]] = {}
        # filename -> the form's values as typed, in the shown unit system:
        # what the form shows again when the dive is re-selected
        self._typed: Dict[str, Dict[str, Any]] = {}
        # dives marked for deletion: removed from the service (and the cache)
        # by Save all changes, like staged edits; Undo unmarks one
        self._deleting: List[str] = []
        self._log_queue = log_queue
        self._worker: Optional[Worker] = None
        # Save all: the dive being uploaded now, and the staged values each
        # upload was started with (a dive re-edited meanwhile stays pending)
        self._saving_file = ""
        self._save_sent: Dict[str, Dict[str, Any]] = {}
        self._save_known: Optional[Dict[str, float]] = None
        self._save_account: Optional[str] = None
        self._saveStep.connect(self._on_save_step)
        self._itemSaved.connect(self._on_item_saved)
        self._sort_key, self._sort_ascending = preferences.get_sort(service, "date")
        extra_on = SERVICE_DEFAULT_ON.get(service, [])
        columns = preferences.get_visible_columns(service, DEFAULT_VISIBLE_COLUMNS + extra_on)
        wanted = set(columns) | set(preferences.introduce_columns(service, extra_on))
        self._model.set_columns([key for key, _, _ in columns_for(service) if key in wanted])

    # -- properties -------------------------------------------------------

    @Property(QObject, constant=True)
    def model(self):
        return self._model

    # -- account (rework.md E19) -------------------------------------------

    @Property("QVariantList", notify=accountChanged)
    def accounts(self):
        """This service's accounts; the page shows a picker with 2+."""
        return accounts.names(self.service)

    @Property(str, notify=accountChanged)
    def account(self) -> str:
        """The account whose dives this page lists and edits: its own
        remembered pick, independent of the Sync page's."""
        return accounts.selected("dives", self.service)

    def _account(self) -> Optional[str]:
        return self.account or None

    @Slot(str)
    def setAccount(self, account: str) -> None:
        if account == self.account:
            return
        if self._busy:
            self._set("_list_status", "Wait for the current refresh or save to finish first.", self.listStatusChanged)
        elif self._pending or self._deleting:
            # Staged edits belong to the listed account's dives; switching
            # would silently drop them.
            self._set("_list_status", "Save or discard your changes before switching account.", self.listStatusChanged)
        else:
            accounts.select("dives", self.service, account)
            self._set("_list_status", "", self.listStatusChanged)
            self.accountChanged.emit()
            self.load()
            return
        self.accountChanged.emit()     # puts the picker back on the current account

    @Slot()
    def reloadAccounts(self) -> None:
        """Credentials were saved: the list, or the current account, may
        have changed."""
        before = self.account
        self.accountChanged.emit()
        if self.account != before and not (self._pending or self._deleting):
            self.load()

    @Property(str, constant=True)
    def serviceName(self) -> str:
        return SERVICE_NAMES.get(self.service, self.service)

    # -- what the page may change (plans/myssi-sync.md K3) --------------------

    @Property(bool, constant=True)
    def acceptsUpdates(self) -> bool:
        """Whether the service takes changes to a dive it already has
        (the adapter's ``accepts_updates``). When it does not, the page's
        editor is read-only and Save does nothing; listing, the profile and
        a delete still work. MySSI says no until its update test (K6/K7);
        the flag flips there and nothing on the page changes."""
        return adapter_flag(self.service, "accepts_updates")

    @Property(str, constant=True)
    def editLockNote(self) -> str:
        """Why the editor is read-only; blank when it is not."""
        if self.acceptsUpdates:
            return ""
        return f"Editing {self.serviceName} dives comes after the update test."

    @Property(str, constant=True)
    def betaNote(self) -> str:
        """`ssi.BETA_NOTE` on the MySSI page (the service is in beta);
        blank elsewhere."""
        if self.service == "ssi":
            from src.core.services.ssi import BETA_NOTE
            return BETA_NOTE
        return ""

    @Property(str, constant=True)
    def deleteNote(self) -> str:
        """What a delete on this service costs beyond the dive, for the
        confirm dialogs; blank where there is nothing to add."""
        if self.service == "ssi":
            return "A MySSI delete is permanent, and the dive number may not come back."
        return ""

    @Property(bool, constant=True)
    def tanksEditable(self) -> bool:
        # Garmin's gas API is read-only (rework.md E4); Divelogs accepts a
        # full tank list on every update.
        return self.service != "garmin"

    @Property(str, constant=True)
    def locationLabel(self) -> str:
        """What the single location field means for this service (Garmin has two)."""
        return SERVICE_COLUMN_LABELS.get(self.service, {}).get("location", "Location")

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
        # there would either be ignored or renumber the log. MySSI numbers
        # its own dives too (after its highest, ssi.upload_dives).
        return self.service not in ("divelogs", "ssi")

    @Property(bool, constant=True)
    def fitSupported(self) -> bool:
        """Only Garmin keeps the original file a dive computer uploaded."""
        return self.service == "garmin"

    @Property(bool, constant=True)
    def showsDevice(self) -> bool:
        """Garmin: which dive computer recorded the dive (read-only)."""
        return any(key == "device" for key, _, _ in columns_for(self.service))

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

    @Property(bool, notify=progressChanged)
    def stoppable(self) -> bool:
        """A refresh, FIT download or Save all is running: each stops at the
        next dive."""
        return self._busy and (self._progress_timer.isActive() or self._save_known is not None)

    @Slot()
    def stop(self) -> None:
        if self.stoppable:
            from src.core import progress
            progress.request_stop()
            self._set("_list_status", "Stopping after the current dive…", self.listStatusChanged)

    @Property(str, notify=savingChanged)
    def savingFile(self) -> str:
        """The dive Save all is uploading right now, empty otherwise."""
        return self._saving_file

    @Property("QVariantMap", notify=selectedChanged)
    def selected(self):
        return dict(self._selected)

    @Property("QVariantList", notify=columnsChanged)
    def allColumns(self):
        return [{"key": k, "label": column_label(self.service, k, self._system())} for k, _h, _ in columns_for(self.service)]

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

    # -- units (plans/units.md J3) ------------------------------------------

    def _system(self) -> str:
        """The unit system the page shows: the UnitsController's, else metric."""
        if self._units_controller is None:
            return core_units.METRIC
        return core_units.normalize(self._units_controller.units)

    def _on_units_changed(self) -> None:
        """The Settings page switched the system: every cell, heading, the
        form and the staged overlay show the new one at once. Staged edits
        are kept as SI (``_pending``), so nothing is converted twice; only
        their echo in the form (``_typed``) is re-expressed."""
        system = self._system()
        self._model.set_units(system)
        for filename, fields in self._pending.items():
            self._typed.setdefault(filename, {}).update(self._typed_from_si(fields, system))
        self._pending_changed()
        self.columnsChanged.emit()          # the SAC heading in the columns dialog
        if self._selected.get("filename"):
            self._select_filename(self._selected["filename"])

    def _si_fields(self, filename: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        """The form's values with every unit-bearing one brought back to SI
        (``si_number`` / ``si_unit_text`` / ``si_tanks``), each compared
        against what the form showed for the stored dive in the current
        system: a field left as shown keeps the stored value (no drift from
        the rounded display), an edited one converts back at full precision.
        In metric every value passes through as typed."""
        system = self._system()
        original = self._row_for(filename) or {}
        shown = display_row(original, system)
        out = dict(fields)
        if "max_depth" in fields:
            out["max_depth"] = si_number("depth", fields["max_depth"], js_number(shown.get("max_depth")),
                                         original.get("max_depth"), system)
        if "water_temp" in fields:
            out["water_temp"] = si_number("temperature", fields["water_temp"], js_number(shown.get("water_temp_value")),
                                          original.get("water_temp_value"), system)
        if "weight" in fields:
            out["weight"] = si_unit_text("weight", fields["weight"], js_number(shown.get("weight")),
                                         original.get("weight"), system)
        if "visibility" in fields:
            out["visibility"] = si_unit_text("depth", fields["visibility"], js_number(shown.get("visibility")),
                                             original.get("visibility"), system)
        if fields.get("tanks") is not None:
            out["tanks"] = si_tanks([dict(t) for t in fields["tanks"]], shown.get("tanks_detail") or [],
                                    original.get("tanks_detail") or [], system)
        return out

    @staticmethod
    def _echo_number(kind: str, value: Any, system: str) -> str:
        """A staged SI value as the form echoes it after a units switch: the
        display rule in imperial; in metric a converted float rounded to
        six decimals so the field does not show a float's tail."""
        if system == core_units.METRIC and isinstance(value, float):
            return js_number(round(value, 6))
        return display_number(kind, value, system)

    def _typed_from_si(self, fields: Dict[str, Any], system: str) -> Dict[str, Any]:
        """The unit-bearing staged values expressed in ``system`` for the
        form (the inverse of ``_si_fields``, used when the system changes
        while edits are staged)."""
        out: Dict[str, Any] = {}
        if "max_depth" in fields:
            out["max_depth"] = self._echo_number("depth", fields["max_depth"], system)
        if "water_temp" in fields:
            out["water_temp"] = self._echo_number("temperature", fields["water_temp"], system)
        if "weight" in fields:
            out["weight"] = unit_text("weight", fields["weight"], system)
        if "visibility" in fields:
            out["visibility"] = unit_text("depth", fields["visibility"], system)
        if fields.get("tanks") is not None:
            out["tanks"] = [dict(t, **{key: self._echo_number(kind, t[key], system) for key, kind in _TANK_NUMBERS if key in t})
                            for t in fields["tanks"]]
        return out

    # -- listing ----------------------------------------------------------

    def _list_dives(self) -> List[Dict[str, Any]]:
        rows = dive_cache.list_dives(self.service, self._account())
        if self.service == "garmin":
            _garmin_table_values(rows)
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
            self._drop_pending(f)
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
        # the form shows the dive in the chosen unit system (display_row)
        self._selected = display_row(self._model.row(row), self._system()) if row >= 0 else {}
        staged = self._typed.get(self._selected.get("filename"))
        if staged:
            # the form shows the dive as edited, not as stored
            self._selected = dict(self._selected, **self._staged_as_row(staged), pending=True)
        self._profile = None
        if self._selected.get("filename"):
            try:
                self._selected["samples"] = dive_cache.get_samples(self.service, self._selected["filename"],
                                                                    self._account())
            except Exception as e:
                logger.warning("Failed to load samples for %s: %s", self._selected["filename"], e)
                self._selected["samples"] = []
            # the chart's hover (I19) reads one sample at a time from this bare dive;
            # a cache holds depth, temperature and time only, so no ticks and no band
            self._profile = profile.dive_from_samples(self._selected["samples"])
            self._selected["markers"] = profile.gas_switch_markers(self._profile)
            self._selected["ceiling"] = profile.ceiling_series(self._profile)
        self.selectedChanged.emit()

    @Slot(int, result="QVariantMap")
    def sampleDetails(self, index: int):
        """What the profile chart shows for sample ``index`` of the selected
        dive under the mouse (I19, ``profile.sample_details`` in the current
        unit system): time, depth and temperature - all a service cache
        holds. ``{}`` with no dive selected."""
        if self._profile is None:
            return {}
        return profile.sample_details(self._profile, int(index), self._system())

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

    def _start_live_listing(self, account: Optional[str]) -> None:
        if self.service != "garmin":
            return      # the others arrive in one piece at the end anyway
        self._live_account = account
        self._live_known = dive_cache.garmin_file_mtimes(account)
        self._live_timer.start()

    def _stop_live_listing(self) -> None:
        self._live_timer.stop()
        self._live_known = None

    def _list_arrived(self) -> None:
        if self._live_known is None:
            return
        try:
            rows = dive_cache.list_garmin_dives(self._live_account, known=self._live_known)
        except Exception as e:
            logger.debug("Listing the dives cached so far failed: %s", e)
            return
        if not rows:
            return
        _garmin_table_values(rows)
        self._model.merge_rows(rows, self._sort_key, self._sort_ascending)
        self.diveCountChanged.emit()

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
        account = self._account()
        self._start_live_listing(account)

        def work():
            credentials.begin_operation()
            try:
                # a Garmin full refresh replaces the saved FIT files too
                scheduler.run_download_thread(force, None, services=[service],
                                              refresh_fits=force and service == "garmin",
                                              accounts={service: account} if account else None)
            finally:
                credentials.end_operation()
            return dict(scheduler.last_download_results)

        def done(results):
            self._set("_busy", False, self.busyChanged)
            self._stop_progress()
            self._stop_live_listing()
            if results.get("error"):
                text = f"Refresh failed: {results['error']}"
            elif results.get("stopped"):
                text = "Stopped. The dives downloaded so far are listed."
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
            self._stop_live_listing()
            self._set("_list_status", f"Refresh failed: {message}", self.listStatusChanged)
            self.load()     # what did arrive is cached; list it as it is on disk

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
        account = self._account()

        def work():
            credentials.begin_operation()
            try:
                return scheduler.run_fit_download_thread(filenames, account)
            finally:
                credentials.end_operation()

        def done(result):
            self._set("_busy", False, self.busyChanged)
            self._stop_progress()
            if result.get("error"):
                text = f"FIT download failed: {result['error']}"
            elif result.get("stopped"):
                text = "FIT download stopped. The files downloaded so far are kept."
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
        """The table's column values for a dive's staged edits (SI, see
        ``_si_fields``), formatted the way the list formats stored ones
        (dive_cache.list_*_dives) in the shown unit system."""
        system = self._system()
        out: Dict[str, Any] = {}
        for key in ("date", "time", "duration", "buddy", "notes"):
            if key in fields:
                out[key] = str(fields[key])
        if "max_depth" in fields:
            out["max_depth"] = self._echo_number("depth", fields["max_depth"], system)
        if "weight" in fields:
            out["weight"] = unit_text("weight", tidy_unit_text(fields["weight"]), system)
        if "visibility" in fields:
            out["visibility"] = unit_text("depth", tidy_unit_text(fields["visibility"]), system)
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
            out["water_temp"] = dive_cache._format_water_temp(temp, temp, temp, system) if temp is not None else ""
        if self.tanksEditable and fields.get("tanks") is not None:
            tanks = [{k: (self._optional_float(str(t.get(k, ""))) if k != "tank_name" else (t.get(k) or None))
                      for k in ("tank_name", "oxygen", "helium", "volume", "start_pressure", "end_pressure")}
                     for t in fields["tanks"]]
            out["tanks"] = dive_cache._format_tanks(tanks, system)
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
        """Keep the form's values for ``filename`` until it is saved or undone:
        as typed for the form, and brought back to SI (``_si_fields``) for
        the table and the save."""
        if not filename or not self._row_for(filename) or filename in self._deleting:
            return
        if not self.acceptsUpdates:
            return      # a read-only page: the form never differs from the stored dive
        fields = dict(fields)
        self._typed[filename] = fields
        self._pending[filename] = self._si_fields(filename, fields)
        self._pending_changed()

    def _drop_pending(self, filename: str) -> None:
        self._pending.pop(filename, None)
        self._typed.pop(filename, None)

    @Slot(str)
    def discard(self, filename: str) -> None:
        """Undo: drop the dive's staged edits (or its deletion mark) and show
        its stored values again."""
        self._drop_pending(filename)
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
        elif not self.acceptsUpdates:
            self._set("_status", self.editLockNote, self.statusChanged)
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
        from src.core import progress
        filenames = [f for f in filenames if self._row_for(f)]
        self._save_sent = {f: self._pending[f] for f in filenames}
        jobs = [(f, self._kwargs_for(f, self._pending[f]), self._save_label(f)) for f in filenames]
        deletes = [(f, self._save_label(f)) for f in deletes if self._row_for(f)]
        keep_selected = self._selected.get("filename")
        account = self._account()
        self._save_account = account
        # Garmin lists only the files written since (see _refresh_saved_row)
        self._save_known = dive_cache.garmin_file_mtimes(account) if self.service == "garmin" else {}
        self._set("_busy", True, self.busyChanged)
        total = len(jobs) + len(deletes)
        self._set("_status", f"Saving {total} change(s) to {self.serviceName}…" if total > 1 else "Saving…",
                  self.statusChanged)
        self._progress_fraction, self._progress_text = 0.0, ""
        self.progressChanged.emit()
        progress.clear()

        def work():
            credentials.begin_operation()
            results = {}
            try:
                # one signed-in session for the whole batch rather than a
                # sign-in per dive (None: each push opens its own)
                try:
                    session = dive_cache.remote_adapter(self.service, account)
                except Exception as e:
                    logger.warning("Could not prepare a %s session: %s", self.serviceName, e)
                    session = None
                step = 0
                for filename, kwargs, label in jobs:
                    if progress.stop_requested():
                        break
                    self._saveStep.emit(step, total, filename, f"Saving {label}")
                    try:
                        filepath = dive_cache.update_dive_fields(self.service, filename, username=account, **kwargs)
                        results[filename] = bool(dive_cache.push_remote_update(self.service, filepath, account,
                                                                               adapter=session))
                    except Exception as e:
                        logger.error("Saving %s failed: %s", filename, e)
                        results[filename] = False
                    step += 1
                    self._itemSaved.emit(filename, results[filename], False)
                for filename, label in deletes:
                    if progress.stop_requested():
                        break
                    self._saveStep.emit(step, total, filename, f"Deleting {label}")
                    results[filename] = self._delete_one(filename, account, session)
                    step += 1
                    self._itemSaved.emit(filename, results[filename], True)
            finally:
                credentials.end_operation()
                stopped = progress.stop_requested()
                progress.clear()
            return {"results": results, "stopped": stopped}

        def finish():
            self._set("_busy", False, self.busyChanged)
            self._save_known = None
            self._save_sent = {}
            self._set("_saving_file", "", self.savingChanged)
            self._progress_fraction, self._progress_text = -1.0, ""
            self.progressChanged.emit()

        def done(outcome):
            finish()
            results = outcome.get("results", {})
            saved = [f for f, ok in results.items() if ok]
            failed = len(results) - len(saved)
            removed = sum(1 for f in saved if f in dict(deletes))
            done_text = f"Saved {len(saved) - removed} dive(s), deleted {removed}." if removed else f"Saved {len(saved)} dives."
            if outcome.get("stopped"):
                status = f"Stopped after {len(results)} of {total} change(s); the rest are still pending."
            elif not failed:
                status = "Saved." if len(results) == 1 and not removed else ("Deleted." if len(results) == 1 else done_text)
            elif len(results) == 1:
                status = ("Delete failed on " + self.serviceName + " - still marked, Save all retries it."
                          if deletes else "Saved locally (remote push failed — check credentials/log). Still pending: Save retries it.")
            else:
                status = f"{len(saved)} of {len(results)} changes done; {failed} failed (still pending, Save all retries them)."
            self._set("_status", status, self.statusChanged)
            self._set("_list_status", "", self.listStatusChanged)
            self.load(keep_selected)

        def fail(message):
            finish()
            self._set("_status", f"Failed: {message}", self.statusChanged)
            self.load(keep_selected)

        self._run(work, done, fail)

    def _save_label(self, filename: str) -> str:
        """How the progress line names a dive: its date and name."""
        row = self._row_for(filename) or {}
        name = (row.get("activity_name") if self.splitSiteNames else None) or row.get("location") or ""
        return " ".join(p for p in (row.get("date") or "", name) if p) or filename

    def _on_save_step(self, done: int, total: int, filename: str, text: str) -> None:
        self._progress_fraction = done / total if total else -1.0
        self._progress_text = f"{text} ({done + 1}/{total})"
        self.progressChanged.emit()
        self._set("_saving_file", filename, self.savingChanged)

    def _on_item_saved(self, filename: str, ok: bool, deleted: bool) -> None:
        """One dive of a Save all is done: show it as saved right away rather
        than when the whole batch is."""
        self._set("_saving_file", "", self.savingChanged)
        if not ok:
            return
        if deleted:
            if filename in self._deleting:
                self._deleting.remove(filename)
            self._drop_pending(filename)
            self._model.remove_row(filename)
            self.diveCountChanged.emit()
        else:
            # only the values that were sent: a dive edited again while it
            # uploaded keeps its newer edits pending
            if self._pending.get(filename) is self._save_sent.get(filename):
                self._drop_pending(filename)
            self._refresh_saved_row(filename)
        self._pending_changed()

    def _refresh_saved_row(self, filename: str) -> None:
        try:
            if self.service == "garmin" and self._save_known is not None:
                rows = dive_cache.list_garmin_dives(self._save_account, known=self._save_known)
                _garmin_table_values(rows)
            else:
                rows = [r for r in self._list_dives() if r.get("filename") == filename]
        except Exception as e:
            logger.debug("Re-listing saved dive %s failed: %s", filename, e)
            return
        if rows:
            self._model.merge_rows(rows, self._sort_key, self._sort_ascending)

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
        self._drop_pending(filename)
        if filename not in self._deleting:
            self._deleting.append(filename)
        self._pending_changed()
        self._select_filename(filename)
        self._set("_status", f"Marked for deletion. Save all changes deletes it from {self.serviceName}; "
                             "Undo keeps it.", self.statusChanged)

    def _delete_one(self, filename: str, account: Optional[str] = None, session=None) -> bool:
        """Worker thread: delete online first, then the cache file - a failed
        online delete leaves the dive marked and cached, to retry."""
        try:
            filepath, external_id = dive_cache.dive_external_id(self.service, filename, account)
            if external_id and not dive_cache.push_remote_delete(self.service, external_id, account, filepath=filepath,
                                                                          adapter=session):
                return False
            if not external_id:
                logger.warning("%s has no %s id; removing it from the local cache only.", filename, self.serviceName)
            dive_cache.delete_dive_local(self.service, filename, account)
            return True
        except Exception as e:
            logger.error("Deleting %s failed: %s", filename, e)
            return False
