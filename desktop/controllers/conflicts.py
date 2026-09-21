"""Conflicts view backend (rework.md D6)."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Property, QObject, Signal, Slot

from desktop import credentials
from desktop.jobs import Worker


class ConflictsController(QObject):
    conflictsChanged = Signal()
    messageChanged = Signal()
    busyChanged = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._conflicts = []
        self._message = ""
        self._busy = False
        self._pair_id = "default"
        self._worker = None

    @Property("QVariantList", notify=conflictsChanged)
    def conflicts(self):
        return list(self._conflicts)

    @Property(str, notify=messageChanged)
    def message(self) -> str:
        return self._message

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy

    def _engine(self):
        from src.core.config import ConfigManager
        from src.core.pairs import engine_for_pair, find_pair
        from src.core.sync_engine import SyncEngine
        if self._pair_id == "default":
            return SyncEngine()
        return engine_for_pair(find_pair(ConfigManager.load_settings(), self._pair_id))

    @Slot(str)
    def load(self, pair_id: str = "default") -> None:
        self._pair_id = pair_id or "default"
        try:
            self._conflicts = [c.model_dump(mode="json") for c in self._engine().list_conflicts()]
            self._message = "" if self._conflicts else "No conflicts waiting."
        except Exception as e:
            self._conflicts = []
            self._message = f"Could not load conflicts: {e}"
        self.conflictsChanged.emit()
        self.messageChanged.emit()

    @Slot(str, str)
    def resolve(self, conflict_id: str, winner: str) -> None:
        if self._busy:
            return
        self._busy = True
        self.busyChanged.emit()
        self._message = "Resolving…"
        self.messageChanged.emit()

        def work():
            credentials.begin_operation()
            try:
                return self._engine().resolve_conflict(conflict_id, winner).id
            finally:
                credentials.end_operation()

        def done(_):
            self._busy = False
            self.busyChanged.emit()
            self._message = "Resolved."
            self.messageChanged.emit()
            self.load(self._pair_id)

        def fail(message):
            self._busy = False
            self.busyChanged.emit()
            self._message = f"Failed: {message}"
            self.messageChanged.emit()

        worker = Worker(work, parent=self)
        worker.finished_ok.connect(done)
        worker.failed.connect(fail)
        self._worker = worker
        worker.start()
