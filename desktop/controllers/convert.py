"""Convert page backend (plans/convert.md I8).

Open dive-computer files (Garmin ``.fit``, Connect's "export original" zip,
UDDF, Subsurface ``.ssrf``), show the dives they hold, and write the selected
ones as UDDF or Subsurface ``.ssrf`` - one Save-as dialog for one dive, a
folder dialog and one file per dive for several (decision Q8) - or send them
to the diver's MySSI logbook (I7's `src.core.services.ssi`, unofficial).

No account of the sync is involved and nothing is written into the data
folder but the SSI site database the diver asks for (`layout.ssi_sites_file`).
Files are read on a `jobs.Worker`; the MySSI calls run on one too. The file
dialogs open in Documents the first time and then in the last folder used
(`preferences.get_convert_folder`, decision Q11).

A FIT whose dive is in one of the app's Garmin account caches - found by
the activity id in the file's name, or by the start time when the name
holds none (a renamed file, an export of another account's copy) - gets
its site, buddy, notes, weight, visibility and tank volumes from the
cached dive (`src.core.convert.enrich`, I9, decision Q7): on by default, a
checkbox on the page, remembered in `desktop_prefs.json`. The dives are
kept as read and the enriched ones derived from them, so the checkbox
re-applies without re-reading the files; the cache is read once per open,
on the worker, the Garmin dives page's account first.

The list is a working list (I9b): Open adds to it (a dive already loaded,
from the same file or with the same start and number, is skipped), Remove
takes the selected dives off it, Clear empties it; the files on disk are
never touched.

"Open Cached" (I9c) is the same Open dialog started in the Garmin FIT cache
of the preferred account (`layout.garmin_fit_dir`, under DATA_DIR - a folder
the diver would not find by hand); it leaves the remembered last folder
alone, so a Save after it does not land inside the app's data folder. The
button is off without an account or a `.fit` file in the folder; the page
re-checks when it is shown.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from PySide6.QtCore import Property, QObject, QStandardPaths, QUrl, Signal, Slot

from desktop import accounts
from desktop import credentials as creds_store
from desktop import preferences
from desktop.jobs import Worker
from src.core import layout
from src.core.convert import enrich, formats
from src.core.models import UnifiedDive

logger = logging.getLogger("dive_sync.desktop.convert")

# Channel names in the order the page lists them (SampleChannels fields).
CHANNEL_LABELS = (
    ("ndl", "NDL"), ("tts", "time to surface"), ("deco_stop_depth", "deco stop"), ("cns", "CNS"),
    ("ppo2", "PO2"), ("setpoint", "setpoint"), ("gf99", "GF99"), ("gas_time", "gas time"), ("heart_rate", "heart rate"),
)
EVENT_WORDS = {"gas_switch": "gas switch", "alert": "alert", "mode_change": "mode change",
               "setpoint_change": "setpoint change", "bookmark": "bookmark"}
MAX_EVENT_LINES = 40


def local_path(value: Any) -> str:
    """A path from what a QML dialog hands over: a ``QUrl``, a ``file://``
    string or a plain path."""
    if isinstance(value, QUrl):
        return value.toLocalFile() if value.isLocalFile() else value.toString()
    text = str(value or "")
    if text.startswith("file:"):
        return QUrl(text).toLocalFile()
    return text


def documents_folder() -> str:
    """Where the dialogs open the first time (decision Q11): the platform's
    Documents folder through QStandardPaths, the home folder without one."""
    folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    return folder or os.path.expanduser("~")


def preferred_garmin_account() -> str:
    """The Garmin account the cache lookup tries first (rework.md E19): the
    one the Garmin dives page works with, else the Sync page's; "" with
    none configured (the caches are then read in name order)."""
    try:
        return accounts.selected("dives", "garmin") or accounts.selected("sync", "garmin")
    except Exception as e:  # no credentials at all: not a reason to skip the cache
        logger.debug("No preferred Garmin account: %s", e)
        return ""


def garmin_cache_folder(account: str = "") -> str:
    """The folder holding the ``.fit`` files the app downloaded for a Garmin
    account (`layout.garmin_fit_dir`, under DATA_DIR); "" without an account.
    Nothing is created: the Garmin page's Download dives fills it."""
    return layout.garmin_fit_dir(account) if account else ""


def has_fit_files(folder: str) -> bool:
    """Whether ``folder`` exists and holds at least one ``.fit`` file."""
    if not folder or not os.path.isdir(folder):
        return False
    try:
        with os.scandir(folder) as entries:
            return any(e.is_file() and e.name.lower().endswith(".fit") for e in entries)
    except OSError as e:
        logger.debug("Cannot list %s: %s", folder, e)
        return False


def _num(value: Optional[float], unit: str = "", digits: int = 1) -> str:
    if value is None:
        return ""
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".") if digits else f"{value:.0f}"
    return f"{text} {unit}".strip()


def _minutes(seconds: Optional[int]) -> str:
    if seconds is None:
        return ""
    return f"{int(round(seconds / 60.0))} min"


def _clock(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def mix_name(oxygen: Optional[float], helium: Optional[float]) -> str:
    """``air``, ``EAN32``, ``TMX 18/45``."""
    o2 = oxygen if oxygen is not None else 21.0
    he = helium or 0.0
    if he:
        return f"TMX {o2:g}/{he:g}"
    if abs(o2 - 21.0) > 0.5:
        return f"EAN{o2:g}"
    return "air"


def dive_row(index: int, dive: UnifiedDive, source: str = "") -> Dict[str, Any]:
    """One line of the page's dive list."""
    return {
        "index": index,
        "date": dive.date_time.strftime("%Y-%m-%d"),
        "time": dive.date_time.strftime("%H:%M"),
        "dive_number": dive.dive_number if dive.dive_number is not None else "",
        "max_depth": _num(dive.max_depth, "m"),
        "duration": _minutes(dive.duration),
        "location": dive.location or "",
        "source": source,
    }


def dive_details(dive: UnifiedDive, source: str = "", enriched: str = "") -> Dict[str, Any]:
    """Everything the right-hand pane shows for one dive: the summary, the
    tanks, the profile samples (ProfileChart's shape), which extra channels
    the file has and the events. ``enriched`` is what the Garmin cache
    filled in (`enrich.Enrichment.summary`), blank for nothing."""
    computer = " ".join(p for p in (dive.computer_vendor, dive.computer_model) if p)
    extras = [p for p in (f"serial {dive.computer_serial}" if dive.computer_serial else "",
                          f"firmware {dive.computer_firmware}" if dive.computer_firmware else "") if p]
    if extras:
        computer = f"{computer} ({', '.join(extras)})" if computer else ", ".join(extras)
    tanks = []
    for i, gm in enumerate(dive.gas_mixtures):
        tanks.append({
            "index": i + 1,
            "name": gm.tank_name or "",
            "mix": mix_name(gm.oxygen, gm.helium),
            "oxygen": _num(gm.oxygen, "%"), "helium": _num(gm.helium, "%"),
            "volume": _num(gm.tank_volume, "l"),
            "start_pressure": _num(gm.start_pressure, "bar", 0), "end_pressure": _num(gm.end_pressure, "bar", 0),
            "role": (gm.tank_role or "").replace("_", " "),
        })
    samples = [{"time": s.time if s.time is not None else 0, "depth": s.depth, "temp": s.temp} for s in dive.samples]
    present = set()
    pressure_tanks = set()
    for s in dive.samples:
        if s.channels is None:
            if s.pressure is not None:
                pressure_tanks.add(0)
            continue
        pressure_tanks.update(s.channels.pressures.keys())
        for key, _label in CHANNEL_LABELS:
            if getattr(s.channels, key) is not None:
                present.add(key)
    channels = []
    if pressure_tanks:
        which = ", ".join(f"tank {i + 1}" for i in sorted(pressure_tanks))
        channels.append(f"tank pressure ({which})")
    channels += [label for key, label in CHANNEL_LABELS if key in present]
    events = []
    for e in dive.events[:MAX_EVENT_LINES]:
        kind = EVENT_WORDS.get(e.type, e.type.replace("_", " "))
        detail = ""
        if e.type == "gas_switch":
            if e.tank is not None:
                detail = f"to tank {e.tank + 1}"
            elif e.oxygen is not None:
                detail = f"to {mix_name(e.oxygen, e.helium)}"
        elif e.type == "setpoint_change" and e.value is not None:
            detail = f"{e.value:g} bar"
        elif e.name:
            detail = e.name
        elif e.tank is not None:
            detail = f"tank {e.tank + 1}"
        events.append(f"{_clock(e.time)}  {kind}" + (f": {detail}" if detail else ""))
    if len(dive.events) > MAX_EVENT_LINES:
        events.append(f"... and {len(dive.events) - MAX_EVENT_LINES} more")
    weight = _num(dive.weight, "lb" if (dive.weight_unit or "").startswith("pound") else "kg")
    visibility = _num(dive.visibility, "ft" if (dive.visibility_unit or "").startswith("f") else "m")
    gf = f"{dive.gf_low}/{dive.gf_high}" if dive.gf_low is not None and dive.gf_high is not None else ""
    ids = ", ".join(f"{k} {v}" for k, v in sorted(dive.external_ids.items()) if v)
    return {
        "title": dive.date_time.strftime("%Y-%m-%d %H:%M") + (f" — {dive.location}" if dive.location else ""),
        "date_time": dive.date_time.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": dive.timezone or "",
        "dive_number": dive.dive_number if dive.dive_number is not None else "",
        "duration": _minutes(dive.duration),
        "bottom_time": _minutes(dive.bottom_time),
        "surface_interval": _minutes(dive.surface_interval),
        "max_depth": _num(dive.max_depth, "m"), "avg_depth": _num(dive.avg_depth, "m"),
        "temp_min": _num(dive.temp_min, "°C"), "temp_max": _num(dive.temp_max, "°C"), "temp_avg": _num(dive.temp_avg, "°C"),
        "location": dive.location or "", "buddy": dive.buddy or "", "notes": dive.notes or "",
        "weight": weight, "visibility": visibility,
        "lat": _num(dive.lat, "", 6), "lng": _num(dive.lng, "", 6),
        "exit_lat": _num(dive.exit_lat, "", 6), "exit_lng": _num(dive.exit_lng, "", 6),
        "computer": computer, "gf": gf, "deco_model": dive.deco_model or "",
        "dive_mode": (dive.dive_mode or "").replace("_", " "), "water_type": dive.water_type or "",
        "water_density": _num(dive.water_density, "kg/m³", 0),
        "cns": (f"{_num(dive.cns_start, '%', 0)} → {_num(dive.cns_end, '%', 0)}"
                if dive.cns_start is not None or dive.cns_end is not None else ""),
        "external_ids": ids,
        "source": source,
        "enriched": enriched,
        "tanks": tanks,
        "samples": samples,
        "sample_count": len(samples),
        "channels": channels,
        "events": events,
        "event_count": len(dive.events),
    }


class ConvertController(QObject):
    divesChanged = Signal()
    selectionChanged = Signal()
    messageChanged = Signal()
    busyChanged = Signal()
    folderChanged = Signal()
    cacheChanged = Signal()        # the Open Cached button (I9c)
    enrichChanged = Signal()       # the checkbox
    ssiChanged = Signal()          # login, plan, results, sites, message
    ssiBusyChanged = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._dives: List[UnifiedDive] = []     # what the page shows (enriched when the checkbox is on)
        self._dives_as_read: List[UnifiedDive] = []
        self._cached: Dict[str, enrich.CachedDive] = {}   # the Garmin cache entries of the dives read (enrich.load_cached_dives keys)
        self._enrichments: List[enrich.Enrichment] = []
        self._enrich_enabled = preferences.get_convert_enrich()
        self._read_text = ""                    # the "n dives from m files" line, without the enrich count
        self._sources: List[str] = []           # one file name per dive (what the page shows)
        self._paths: List[str] = []             # the same as full paths (what "already loaded" compares)
        self._files: List[str] = []
        self._warnings: List[str] = []          # from the last read
        self._selection: List[int] = []
        self._current = -1                      # the dive the detail pane shows
        self._anchor = -1                       # for shift-click ranges
        self._message = ""
        self._save_warnings: List[str] = []
        self._busy = False
        self._worker = None
        # MySSI
        self._ssi_login = creds_store.load_ssi_credentials()
        self._ssi_busy = False
        self._ssi_message = ""
        self._ssi_plan: List[Any] = []          # ssi.UploadResult, from plan_upload
        self._ssi_plan_dives: List[UnifiedDive] = []
        self._ssi_sites: Dict[int, Optional[int]] = {}   # plan row -> chosen site id (None = no site)
        self._ssi_results: List[Any] = []
        self._site_index = None                 # ssi.SiteIndex: logbook sites + the downloaded database
        self._zip_index = None                  # the downloaded database alone, read once
        # Tests give the client a fake transport and a no-op sleep; the app
        # uses the defaults (requests, the AGENTS.md cool-down).
        self._ssi_client_kwargs: Dict[str, Any] = {}

    # -- files and dives ---------------------------------------------------

    @Property("QVariantList", notify=divesChanged)
    def dives(self):
        return [dive_row(i, d, self._sources[i] if i < len(self._sources) else "") for i, d in enumerate(self._dives)]

    @Property(int, notify=divesChanged)
    def diveCount(self) -> int:
        return len(self._dives)

    @Property("QVariantList", notify=divesChanged)
    def files(self):
        """The files whose dives are in the list, as names."""
        return [os.path.basename(f) for f in self._files]

    @Property("QVariantList", notify=divesChanged)
    def warnings(self):
        """What the readers reported about the files opened last."""
        return list(self._warnings)

    @Property("QVariantList", notify=selectionChanged)
    def selection(self):
        return list(self._selection)

    @Property(int, notify=selectionChanged)
    def selectedCount(self) -> int:
        return len(self._selection)

    @Property(int, notify=selectionChanged)
    def currentRow(self) -> int:
        return self._current

    @Property("QVariantMap", notify=selectionChanged)
    def selected(self):
        """The dive the detail pane shows (the one clicked last); {} with none."""
        if 0 <= self._current < len(self._dives):
            return dive_details(self._dives[self._current], self._sources[self._current], self._enriched_summary(self._current))
        return {}

    @Property(bool, notify=enrichChanged)
    def enrichFromCache(self) -> bool:
        """The checkbox (decision Q7): fill a FIT's site, buddy, notes, weight,
        visibility and tank volumes from the cached Garmin dive with the same
        activity id. On by default; remembered in desktop_prefs.json."""
        return self._enrich_enabled

    @Slot(bool)
    def setEnrichFromCache(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._enrich_enabled:
            return
        self._enrich_enabled = enabled
        preferences.set_convert_enrich(enabled)
        self.enrichChanged.emit()
        self._reapply_enrich()

    @Property(int, notify=divesChanged)
    def enrichedCount(self) -> int:
        """How many of the dives shown got something from the Garmin cache."""
        return len(self._enrichments)

    def _enriched_summary(self, index: int) -> str:
        for e in self._enrichments:
            if e.index == index:
                return e.summary
        return ""

    @Property(str, notify=messageChanged)
    def message(self) -> str:
        return self._message

    @Property("QVariantList", notify=messageChanged)
    def saveWarnings(self):
        """What the last write left out (the writer's drop lines)."""
        return list(self._save_warnings)

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy

    @Property("QVariantList", constant=True)
    def openNameFilters(self):
        return formats.open_name_filters()

    @Property("QVariantList", constant=True)
    def targets(self):
        """The file targets: [{id, label, extension}], from the registry."""
        return [{"id": f.id, "label": f.label, "extension": f.extension} for f in formats.writable_formats()]

    @Slot(str, result="QVariantList")
    def saveNameFilters(self, format_id: str):
        return formats.save_name_filters(format_id)

    @Property(str, notify=folderChanged)
    def dialogFolder(self) -> str:
        """Where the dialogs open, as a file URL: Documents the first time,
        then the last folder used (decision Q11)."""
        return QUrl.fromLocalFile(preferences.get_convert_folder(documents_folder())).toString()

    def _remember_folder(self, path: str) -> None:
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        if folder:
            preferences.set_convert_folder(folder)
            self.folderChanged.emit()

    # -- the Garmin FIT cache (I9c) --------------------------------------------

    @Property(str, notify=cacheChanged)
    def cacheAccount(self) -> str:
        """The Garmin account whose FIT cache Open Cached shows: the one the
        Garmin dives page works with (`preferred_garmin_account`); "" with none."""
        return preferred_garmin_account()

    @Property(str, notify=cacheChanged)
    def cacheFolder(self) -> str:
        """That account's FIT cache folder as a file URL for the dialog's
        ``currentFolder``; "" without an account."""
        folder = garmin_cache_folder(self.cacheAccount)
        return QUrl.fromLocalFile(folder).toString() if folder else ""

    @Property(bool, notify=cacheChanged)
    def cacheAvailable(self) -> bool:
        """Whether Open Cached has anything to open: an account, and a
        ``.fit`` file in its cache folder."""
        return has_fit_files(garmin_cache_folder(self.cacheAccount))

    @Property(str, notify=cacheChanged)
    def cacheTip(self) -> str:
        """The button's help: what it opens when it can, else why it is off."""
        account = self.cacheAccount
        if not account:
            return ("No Garmin account is set up. Open Cached opens the .fit files this app downloads from "
                    "Garmin Connect; add an account on the Settings page and use the Garmin page's Download dives.")
        if not self.cacheAvailable:
            return (f"Nothing cached yet for {account}: the Garmin page's Download dives fetches the .fit files "
                    "from Garmin Connect, and they are kept here.")
        return (f"Opens the .fit files this app downloaded from Garmin Connect (the Garmin page's dives) "
                f"for {account}. The dives are added to the list.")

    @Slot()
    def refreshCache(self) -> None:
        """Re-read the cache state (the page calls it when shown, so dives
        downloaded on the Garmin page meanwhile enable the button)."""
        self.cacheChanged.emit()

    def _set_message(self, text: str, save_warnings: Optional[List[str]] = None) -> None:
        self._message = text
        self._save_warnings = list(save_warnings or [])
        self.messageChanged.emit()

    def _set_busy(self, value: bool) -> None:
        self._busy = value
        self.busyChanged.emit()

    @Slot("QVariantList")
    def openFiles(self, urls) -> None:
        """Read every file picked in the Open dialog (several at once, Q6)
        and add their dives to the list (I9b); a dive already in it is
        skipped and counted in the message. The files' folder becomes the
        one the dialogs open in next."""
        self._open(urls, remember=True)

    @Slot("QVariantList")
    def openCachedFiles(self, urls) -> None:
        """`openFiles` for the Open Cached dialog (I9c): the same read, but
        the app's cache folder is not remembered as the last folder used,
        so a Save after it still opens where the diver's own files are."""
        self._open(urls, remember=False)

    def _open(self, urls, remember: bool) -> None:
        paths = [local_path(u) for u in urls or []]
        paths = [p for p in paths if p]
        if not paths or self._busy:
            return
        self._set_busy(True)
        self._set_message(f"Reading {len(paths)} file{'s' if len(paths) != 1 else ''}…")
        preferred = preferred_garmin_account()

        def work():
            result = formats.read_files(paths)
            # the cache entries of the dives read, looked up here so the
            # checkbox can re-apply them without touching the disk again
            cached = enrich.load_cached_dives(result.dives, preferred_account=preferred)
            return result, cached

        def done(outcome):
            result, cached = outcome
            self._set_busy(False)
            self._apply_read(result, cached, append=True)
            if remember:
                self._remember_folder(paths[0])

        def fail(text):
            self._set_busy(False)
            self._set_message(f"Could not read: {text}")

        worker = Worker(work, parent=self)
        worker.finished_ok.connect(done)
        worker.failed.connect(fail)
        self._worker = worker
        worker.start()

    def _enrich(self, dives: List[UnifiedDive]) -> List[UnifiedDive]:
        """The dives to show: filled from the Garmin cache entries read with
        them (`self._cached`) when the checkbox is on, else as read. Records
        what was filled for the detail pane and the page."""
        if not self._enrich_enabled or not self._cached:
            self._enrichments = []
            return list(dives)
        result = enrich.enrich_dives(dives, self._cached)
        self._enrichments = list(result.enrichments)
        return result.dives

    def _reapply_enrich(self) -> None:
        """The checkbox changed: derive the shown dives from the ones as read
        again, keeping the selection."""
        self._dives = self._enrich(self._dives_as_read)
        self._close_ssi_plan()
        self.divesChanged.emit()
        self.selectionChanged.emit()
        if self._read_text:
            self._set_message(self._read_text + self._enrich_text())

    def _enrich_text(self) -> str:
        n = len(self._enrichments)
        return f" {n} filled from the Garmin cache." if n else ""

    @staticmethod
    def _paths_of(result: formats.ReadResult) -> List[str]:
        """The file each dive came from, one path per dive: ``result.sources``
        when the reader filled it; for a result built by hand, the one file
        for every dive, or one file per dive when the counts line up, else blanks."""
        if len(result.sources) == len(result.dives):
            return [os.fspath(p) for p in result.sources]
        if len(result.files) == 1:
            return [result.files[0][0]] * len(result.dives)
        if len(result.files) == len(result.dives):
            return [path for path, _fmt in result.files]
        return [""] * len(result.dives)

    @staticmethod
    def _dive_key(dive: UnifiedDive):
        """What makes two dives the same one for the list: the start and the number."""
        return dive.date_time, dive.dive_number

    def _apply_read(self, result: formats.ReadResult, cached: Optional[Dict[str, enrich.CachedDive]] = None,
                    append: bool = False) -> None:
        """Show a `formats.ReadResult` - in place of the list, or added to it
        (``append``, what Open does; a dive from a file already in the list,
        or with the start and number of one already there, is skipped). The
        tests feed a result directly. ``cached`` is what the worker found in
        the Garmin cache for the dives read; None looks it up here (never
        raises: a broken entry is a log line)."""
        if cached is None:
            cached = enrich.load_cached_dives(result.dives, preferred_account=preferred_garmin_account())
        paths = self._paths_of(result)
        if not append:
            self._dives_as_read, self._sources, self._paths, self._files, self._cached = [], [], [], [], {}
        known_files = set(self._files)
        known_keys = {self._dive_key(d) for d in self._dives_as_read}
        first_new = len(self._dives_as_read)
        added = skipped = 0
        for dive, path in zip(result.dives, paths):
            key = self._dive_key(dive)
            if (path and path in known_files) or key in known_keys:
                skipped += 1
                continue
            known_keys.add(key)
            self._dives_as_read.append(dive)
            self._sources.append(os.path.basename(path))
            self._paths.append(path)
            if path and path not in self._files:
                self._files.append(path)
            added += 1
        self._cached.update(cached)
        self._dives = self._enrich(self._dives_as_read)
        self._warnings = list(result.warnings)
        if added or not append:
            self._selection = [first_new] if self._dives else []
            self._current = self._anchor = first_new if self._dives else -1
        self._close_ssi_plan()
        self.divesChanged.emit()
        self.selectionChanged.emit()
        n, m, total = len(result.dives), len(result.files), len(self._dives)
        if append and total > added:
            text = f"{added} dive{'s' if added != 1 else ''} from {m} file{'s' if m != 1 else ''} added; {total} in the list."
            if skipped:
                text += f" {skipped} already loaded."
        else:
            text = f"{n} dive{'s' if n != 1 else ''} from {m} file{'s' if m != 1 else ''}."
        if result.warnings:
            text += f" {len(result.warnings)} warning{'s' if len(result.warnings) != 1 else ''}."
        self._read_text = text
        self._set_message(text + self._enrich_text())

    @Slot()
    def removeSelected(self) -> None:
        """Take the selected dives off the list (I9b): the files on disk are
        untouched. The row that moves into the first removed one's place is
        selected next; a file none of whose dives is left is forgotten, so
        opening it again adds them back."""
        rows = sorted(r for r in set(self._selection) if 0 <= r < len(self._dives_as_read))
        if not rows:
            return
        gone = set(rows)
        self._dives_as_read = [d for i, d in enumerate(self._dives_as_read) if i not in gone]
        self._sources = [s for i, s in enumerate(self._sources) if i not in gone]
        self._paths = [p for i, p in enumerate(self._paths) if i not in gone]
        left = set(self._paths)
        self._files = [p for p in self._files if p in left]
        self._dives = self._enrich(self._dives_as_read)
        if self._dives:
            row = min(rows[0], len(self._dives) - 1)
            self._selection, self._current, self._anchor = [row], row, row
        else:
            self._selection, self._current, self._anchor = [], -1, -1
        self._close_ssi_plan()
        self.divesChanged.emit()
        self.selectionChanged.emit()
        n, total = len(rows), len(self._dives)
        self._read_text = f"Removed {n} dive{'s' if n != 1 else ''}; {total} in the list." if total else ""
        self._set_message(self._read_text + self._enrich_text() if total else f"Removed {n} dive{'s' if n != 1 else ''}.")

    @Slot()
    def clear(self) -> None:
        """Empty the list; nothing on disk changes."""
        self._apply_read(formats.ReadResult(), {})
        self._read_text = ""
        self._set_message("")

    # -- selection ---------------------------------------------------------

    @Slot(int, bool, bool)
    def clickRow(self, row: int, toggle: bool = False, extend: bool = False) -> None:
        """A click on a dive: plain selects it alone, Ctrl/Cmd toggles it,
        Shift selects the range from the last plain click."""
        if not 0 <= row < len(self._dives):
            return
        if extend and self._anchor >= 0:
            lo, hi = sorted((self._anchor, row))
            rows = list(range(lo, hi + 1))
            self._selection = sorted(set(self._selection) | set(rows)) if toggle else rows
        elif toggle:
            if row in self._selection:
                self._selection = [r for r in self._selection if r != row]
            else:
                self._selection = sorted(self._selection + [row])
            self._anchor = row
        else:
            self._selection = [row]
            self._anchor = row
        self._current = row
        self._close_ssi_plan()
        self.selectionChanged.emit()

    @Slot(int)
    def select(self, row: int) -> None:
        self.clickRow(row, False, False)

    @Slot()
    def selectAll(self) -> None:
        self._selection = list(range(len(self._dives)))
        if self._current < 0 and self._dives:
            self._current = self._anchor = 0
        self._close_ssi_plan()
        self.selectionChanged.emit()

    def _selected_dives(self) -> List[UnifiedDive]:
        return [self._dives[i] for i in self._selection if 0 <= i < len(self._dives)]

    # -- save as -----------------------------------------------------------

    @Slot(str, result=str)
    def suggestedFileName(self, format_id: str) -> str:
        """The Save-as dialog's file name for the one selected dive (I4's
        ``<date> <time> dive <n>.<ext>``); blank unless exactly one is selected."""
        dives = self._selected_dives()
        if len(dives) != 1:
            return ""
        return formats.output_file_name(dives[0], format_id)

    @Slot(str, result=str)
    def suggestedFileUrl(self, format_id: str) -> str:
        """`suggestedFileName` inside the dialogs' folder, as the file URL the
        Save-as dialog's ``currentFile`` takes; blank unless one dive is selected."""
        name = self.suggestedFileName(format_id)
        if not name:
            return ""
        folder = preferences.get_convert_folder(documents_folder())
        return QUrl.fromLocalFile(os.path.join(folder, name)).toString()

    @Slot(str, result="QVariantList")
    def targetDrops(self, format_id: str):
        """What writing the selected dives as ``format_id`` would leave out."""
        try:
            return formats.describe_drops(self._selected_dives(), format_id)
        except Exception as e:  # a preview never breaks the page
            logger.debug("No drop preview for %s: %s", format_id, e)
            return []

    @Slot(str, str)
    def saveAs(self, format_id: str, url: str) -> None:
        """Write the one selected dive to the file picked in the Save-as dialog."""
        dives = self._selected_dives()
        path = local_path(url)
        if not dives or not path:
            return
        if len(dives) != 1:
            self._set_message("Several dives are selected: pick a folder instead, one file is written per dive.")
            return
        try:
            warnings = formats.write_file(dives, format_id, path)
        except Exception as e:
            self._set_message(f"Could not write {os.path.basename(path)}: {e}")
            return
        self._remember_folder(path)
        self._set_message(f"Wrote {path}", warnings)

    @Slot(str, str)
    def saveEach(self, format_id: str, folder_url: str) -> None:
        """Write the selected dives into the folder picked, one file per dive
        (decision Q8); a name already taken gets a (2), (3) suffix."""
        dives = self._selected_dives()
        folder = local_path(folder_url)
        if not dives or not folder:
            return
        try:
            result = formats.write_each(dives, format_id, folder)
        except Exception as e:
            self._set_message(f"Could not write into {folder}: {e}")
            return
        self._remember_folder(folder)
        n = len(result.paths)
        self._set_message(f"Wrote {n} file{'s' if n != 1 else ''} to {folder}", result.warnings)

    # -- MySSI -----------------------------------------------------------
    #
    # Hidden behind `ssi.UPLOAD_ENABLED` (off since 2026-10-02, until the
    # owner's live test): the page shows nothing of it and every slot below
    # that could reach MySSI refuses through `_ssi_allowed`.

    @Property(bool, constant=True)
    def ssiEnabled(self) -> bool:
        """Whether Send to SSI is offered at all (`ssi.UPLOAD_ENABLED`)."""
        from src.core.services import ssi
        return bool(ssi.UPLOAD_ENABLED)

    def _ssi_allowed(self, action: str) -> bool:
        """False, with one log line, when the SSI upload is switched off."""
        if self.ssiEnabled:
            return True
        logger.info("Send to SSI is switched off (ssi.UPLOAD_ENABLED): %s ignored", action)
        return False

    @Property(str, constant=True)
    def ssiNote(self) -> str:
        from src.core.services.ssi import UNOFFICIAL_NOTE
        return UNOFFICIAL_NOTE

    @Property(bool, notify=ssiChanged)
    def ssiConfigured(self) -> bool:
        return self._ssi_login.configured

    @Property(str, notify=ssiChanged)
    def ssiLoginStatus(self) -> str:
        if self._ssi_login.configured:
            return f"MySSI login: {self._ssi_login.email}"
        if self._ssi_login.email:
            return f"No password stored for {self._ssi_login.email}: enter it on the Settings page."
        return "No MySSI login saved: add it on the Settings page (Settings → MySSI)."

    @Slot()
    def reloadSsiLogin(self) -> None:
        """Settings saved another login: forget the plan made with the old one."""
        self._ssi_login = creds_store.load_ssi_credentials()
        self._close_ssi_plan(emit=False)
        self._ssi_results = []
        self.ssiChanged.emit()

    @Property(bool, notify=ssiBusyChanged)
    def ssiBusy(self) -> bool:
        return self._ssi_busy

    @Property(str, notify=ssiChanged)
    def ssiMessage(self) -> str:
        return self._ssi_message

    @Property(bool, notify=ssiChanged)
    def ssiPlanOpen(self) -> bool:
        """A plan is shown and waits for Send (or Cancel)."""
        return bool(self._ssi_plan)

    @Property("QVariantList", notify=ssiChanged)
    def ssiPlan(self):
        """One entry per selected dive: {row, when, status, reason, number,
        site_id, site_label, dropped, summary}; ``status`` is planned or
        skipped (a duplicate)."""
        out = []
        for i, r in enumerate(self._ssi_plan):
            site_id = self._ssi_sites.get(i)
            site = self._site_index.get(site_id) if (self._site_index is not None and site_id is not None) else None
            out.append({
                "row": i,
                "when": r.dive.date_time.strftime("%Y-%m-%d %H:%M"),
                "status": r.status,
                "reason": r.reason,
                "number": r.number if r.number is not None else "",
                "site_id": site_id if site_id is not None else -1,
                "site_label": site.label if site else "",
                "dropped": list(r.dropped),
                "summary": r.summary,
            })
        return out

    @Property(int, notify=ssiChanged)
    def ssiPlannedCount(self) -> int:
        from src.core.services.ssi import STATUS_PLANNED
        return sum(1 for r in self._ssi_plan if r.status == STATUS_PLANNED)

    @Property("QVariantList", notify=ssiChanged)
    def ssiResults(self):
        """After a send: [{summary, status, dropped}] per dive."""
        return [{"summary": r.summary, "status": r.status, "dropped": list(r.dropped)} for r in self._ssi_results]

    @Property(bool, notify=ssiChanged)
    def ssiSitesDownloaded(self) -> bool:
        return os.path.isfile(layout.ssi_sites_file())

    @Property(str, notify=ssiChanged)
    def ssiSitesStatus(self) -> str:
        path = layout.ssi_sites_file()
        if not os.path.isfile(path):
            return "SSI site database not downloaded yet (tens of MB, once): it is fetched when you first send, or with the button."
        when = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
        count = f"{len(self._zip_index)} sites, " if self._zip_index is not None else ""
        return f"SSI site database: {count}downloaded {when}."

    def _set_ssi_busy(self, value: bool) -> None:
        self._ssi_busy = value
        self.ssiBusyChanged.emit()

    def _set_ssi_message(self, text: str) -> None:
        self._ssi_message = text
        self.ssiChanged.emit()

    def _close_ssi_plan(self, emit: bool = True) -> None:
        if self._ssi_plan or self._ssi_plan_dives:
            self._ssi_plan, self._ssi_plan_dives, self._ssi_sites = [], [], {}
            if emit:
                self.ssiChanged.emit()

    def _make_client(self):
        """An `SsiAdapter` from the keychain login, read afresh so the token
        the last client stored there (``on_token``) is reused instead of
        logging in again."""
        from src.core.services.ssi import SsiAdapter
        login = self._ssi_login = creds_store.load_ssi_credentials()
        return SsiAdapter(login.email, login.password, login.token or None,
                          on_token=creds_store.save_ssi_token, **self._ssi_client_kwargs)

    def _load_site_index(self, client, logbook, download: bool):
        """The sites to pick from: the diver's own logbook sites plus the
        downloaded database, fetched first when ``download`` and missing."""
        from src.core.services.ssi import SiteIndex
        path = layout.ssi_sites_file()
        if download and not os.path.isfile(path):
            logger.info("Downloading the SSI site database to %s", path)
            client.download_sites(path)
            self._zip_index = None
        if self._zip_index is None and os.path.isfile(path):
            try:
                self._zip_index = SiteIndex.from_zip(path)
            except Exception as e:
                logger.warning("Could not read the SSI site database %s: %s", path, e)
                self._zip_index = SiteIndex()
        index = SiteIndex.from_logbook(logbook) if logbook is not None else SiteIndex()
        if self._zip_index is not None:
            index.extend(self._zip_index)
        return index

    def _run_ssi(self, work, done, start_text: str) -> None:
        if self._ssi_busy:
            return
        self._set_ssi_busy(True)
        self._set_ssi_message(start_text)

        def finished(result):
            self._set_ssi_busy(False)
            done(result)

        def fail(text):
            self._set_ssi_busy(False)
            self._set_ssi_message(f"MySSI: {text}")

        worker = Worker(work, parent=self)
        worker.finished_ok.connect(finished)
        worker.failed.connect(fail)
        self._worker = worker
        worker.start()

    @Slot()
    def prepareSsi(self) -> None:
        """Step one of Send to SSI: read the logbook, find duplicates, number
        the new dives and preselect the nearest SSI site within 5 km of each
        (decision 2026-10-01); the site database is downloaded the first
        time. Nothing is sent until `sendToSsi`."""
        if not self._ssi_allowed("prepareSsi"):
            return
        from src.core.services.ssi import plan_upload
        dives = self._selected_dives()
        if not dives:
            self._set_ssi_message("Select the dives to send first.")
            return
        if not self._ssi_login.configured:
            self._set_ssi_message(self.ssiLoginStatus)
            return

        def work():
            adapter = self._make_client()
            logbook = adapter.client.get_divelog()
            index = self._load_site_index(adapter.client, logbook, download=True)
            return plan_upload(logbook, dives, index), index

        def done(result):
            plan, index = result
            self._ssi_plan, self._ssi_plan_dives, self._site_index = plan, list(dives), index
            self._ssi_sites = {i: (r.site.id if r.site else None) for i, r in enumerate(plan)}
            self._ssi_results = []
            planned = self.ssiPlannedCount
            skipped = len(plan) - planned
            text = f"{planned} dive{'s' if planned != 1 else ''} to send"
            if skipped:
                text += f", {skipped} already in MySSI (skipped)"
            self._set_ssi_message(text + ". Check the sites, then press Send.")

        self._run_ssi(work, done, "Reading your MySSI logbook…")

    @Slot()
    def downloadSites(self) -> None:
        """Fetch (again) the public SSI site database the picker searches."""
        if not self._ssi_allowed("downloadSites"):
            return

        def work():
            path = layout.ssi_sites_file()
            adapter = self._make_client()
            adapter.client.download_sites(path)
            self._zip_index = None
            return self._load_site_index(adapter.client, None, download=False)

        def done(index):
            # a plan on screen keeps its logbook sites and gains the database
            if self._site_index is not None:
                self._site_index.extend(index)
            self._set_ssi_message(f"SSI site database downloaded: {len(self._zip_index or [])} sites.")

        self._run_ssi(work, done, "Downloading the SSI site database…")

    @Slot(str, result="QVariantList")
    def searchSites(self, text: str):
        """Sites whose name, region or country contains ``text``: [{id, label}]."""
        if not self._ssi_allowed("searchSites") or self._site_index is None:
            return []
        return [{"id": s.id, "label": s.label} for s in self._site_index.search(text)]

    @Slot(int, int)
    def setSite(self, row: int, site_id: int) -> None:
        """Pick the SSI site of one planned dive; ``site_id`` < 0 clears it
        (sending without a site is allowed)."""
        if not self._ssi_allowed("setSite") or not 0 <= row < len(self._ssi_plan):
            return
        self._ssi_sites[row] = site_id if site_id >= 0 else None
        self.ssiChanged.emit()

    @Slot(int)
    def clearSite(self, row: int) -> None:
        self.setSite(row, -1)

    @Slot()
    def cancelSsi(self) -> None:
        self._close_ssi_plan()
        self._set_ssi_message("")

    @Slot()
    def sendToSsi(self) -> None:
        """Send the planned dives with the sites chosen; each is read back
        and checked (`ssi.upload_dives`). The plan closes and the results
        take its place."""
        if not self._ssi_allowed("sendToSsi"):
            return
        from src.core.services.ssi import upload_dives
        if not self._ssi_plan:
            self._set_ssi_message("Prepare the upload first.")
            return
        dives = list(self._ssi_plan_dives)
        site_ids = dict(self._ssi_sites)
        index = self._site_index

        def work():
            adapter = self._make_client()
            return upload_dives(adapter.client, dives, site_ids=site_ids, site_index=index)

        def done(results):
            from src.core.services.ssi import STATUS_FAILED, STATUS_SENT, STATUS_SKIPPED
            self._ssi_results = list(results)
            self._close_ssi_plan(emit=False)
            counts = {s: sum(1 for r in results if r.status == s) for s in (STATUS_SENT, STATUS_SKIPPED, STATUS_FAILED)}
            self._set_ssi_message(f"MySSI: sent {counts[STATUS_SENT]}, skipped {counts[STATUS_SKIPPED]}, "
                                  f"failed {counts[STATUS_FAILED]}. The MySSI app shows new dives after a pull-to-refresh.")

        n = len(dives)
        self._run_ssi(work, done, f"Sending {n} dive{'s' if n != 1 else ''} to MySSI…")
