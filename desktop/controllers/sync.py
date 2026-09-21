"""Sync page backend: run a sync or a raw download off the GUI thread and
stream the log (rework.md D2/D3, parity with the Toga B2/B3 sections)."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Property, QObject, Signal, Slot

from desktop import credentials
from desktop.jobs import LogPump, Worker
from src.core import scheduler


class SyncController(QObject):
    runningChanged = Signal()
    statusChanged = Signal()
    logLine = Signal(str)
    pairsChanged = Signal()
    finished = Signal(bool)   # ok

    def __init__(self, log_queue, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._running = False
        self._status = "Idle"
        self._worker: Optional[Worker] = None
        self._pump = LogPump(log_queue, parent=self)
        self._pump.line.connect(self.logLine)
        self._pump.start()

    # -- properties -------------------------------------------------------

    @Property(bool, notify=runningChanged)
    def running(self) -> bool:
        return self._running

    @Property(str, notify=statusChanged)
    def status(self) -> str:
        return self._status

    @Property("QVariantList", notify=pairsChanged)
    def pairs(self):
        from src.core.config import ConfigManager
        settings = ConfigManager.load_settings()
        out = [{"id": "", "label": "Garmin ↔ Divelogs", "source": "garmin", "target": "divelogs"}]
        for pair in settings.sync_pairs:
            if pair.enabled:
                out.append({"id": pair.id, "label": f"{pair.id} ({pair.source} → {pair.target})",
                            "source": pair.source, "target": pair.target})
        return out

    @Slot(str, result="QVariantList")
    def directionsFor(self, pair_id: str):
        for pair in self.pairs:
            if pair["id"] == pair_id:
                s, t = pair["source"].split(":")[0], pair["target"].split(":")[0]
                if s == "subsurface-cloud":
                    s = "subsurface"
                if t == "subsurface-cloud":
                    t = "subsurface"
                return [{"value": "bidirectional", "label": "Bidirectional"},
                        {"value": f"to_{t}", "label": f"To {t}"},
                        {"value": f"to_{s}", "label": f"To {s}"}]
        return [{"value": "bidirectional", "label": "Bidirectional"}]

    def _set_status(self, text: str) -> None:
        self._status = text
        self.statusChanged.emit()

    def _set_running(self, on: bool) -> None:
        self._running = on
        self.runningChanged.emit()

    def _busy(self) -> bool:
        return self._running or scheduler.is_sync_running or scheduler.is_download_running

    # -- actions ----------------------------------------------------------

    @Slot(bool, str, bool, bool, bool, str)
    def runSync(self, dry_run: bool, direction: str, only_new: bool, sync_gases: bool, sync_fit: bool, pair_id: str) -> None:
        if self._busy():
            self._set_status("A sync or download is already running.")
            return
        custom = {"directionality": direction, "only_new": only_new, "sync_gases": sync_gases, "sync_fit": sync_fit}
        if pair_id:
            custom["pair"] = pair_id
        self._start("Running…", lambda: self._run_sync(dry_run, custom),
                    lambda: "Dry run complete." if dry_run else "Sync complete.")

    @Slot(bool, str)
    def download(self, overwrite: bool, service: str) -> None:
        if self._busy():
            self._set_status("A sync or download is already running.")
            return
        include_garmin = service in ("", "garmin")
        include_divelogs = service in ("", "divelogs")
        self._start("Downloading…", lambda: self._run_download(overwrite, include_garmin, include_divelogs),
                    lambda: "Download complete.")

    def _start(self, status: str, target, done_text) -> None:
        self._set_running(True)
        self._set_status(status)
        worker = Worker(target, parent=self)

        def on_ok(result):
            self._pump.drain()
            self._set_running(False)
            error = (result or {}).get("error")
            if error:
                self._set_status(f"Failed: {error}")
                self.finished.emit(False)
            elif result is not None and result.get("success") is False:
                self._set_status("Finished with errors — check the log.")
                self.finished.emit(False)
            else:
                self._set_status(done_text())
                self.finished.emit(True)

        def on_fail(message):
            self._pump.drain()
            self._set_running(False)
            self._set_status(f"Failed: {message}")
            self.finished.emit(False)

        worker.finished_ok.connect(on_ok)
        worker.failed.connect(on_fail)
        self._worker = worker
        worker.start()

    @staticmethod
    def _run_sync(dry_run: bool, custom: dict) -> dict:
        credentials.begin_operation()
        try:
            scheduler.run_sync_thread(dry_run, custom)
        finally:
            credentials.end_operation()
        return dict(scheduler.last_sync_results)

    @staticmethod
    def _run_download(overwrite: bool, include_garmin: bool, include_divelogs: bool) -> dict:
        credentials.begin_operation()
        try:
            scheduler.run_download_thread(overwrite, None, include_garmin, include_divelogs)
        finally:
            credentials.end_operation()
        return dict(scheduler.last_download_results)
