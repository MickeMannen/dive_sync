"""Conflicts view backend (rework.md D6).

Lists the conflicts of every sync pair at once - saved pairs and the
combinations of configured services the Mapping page offers - grouped by
pair, with field labels and readable values, so nothing waits unseen on a
pair the Mapping page does not happen to have open."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Property, QObject, Signal, Slot

from desktop import credentials
from desktop.jobs import Worker


from src.core.conflicts import display_value as brief  # noqa: E402  (kept importable as brief)


class ConflictsController(QObject):
    conflictsChanged = Signal()
    messageChanged = Signal()
    busyChanged = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._groups: List[Dict[str, Any]] = []
        self._message = ""
        self._busy = False
        self._worker = None

    @Property("QVariantList", notify=conflictsChanged)
    def groups(self):
        """One entry per pair with waiting conflicts: {pair_id, label, source_name,
        target_name, conflicts: [...]}, each conflict with display fields added."""
        return list(self._groups)

    @Property("QVariantList", notify=conflictsChanged)
    def conflicts(self):
        """Every waiting conflict, flat (each carries its pair_id)."""
        return [c for g in self._groups for c in g["conflicts"]]

    @Property(int, notify=conflictsChanged)
    def count(self) -> int:
        return sum(len(g["conflicts"]) for g in self._groups)

    @Property(str, notify=messageChanged)
    def message(self) -> str:
        return self._message

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy

    def _set_message(self, text: str) -> None:
        self._message = text
        self.messageChanged.emit()

    @staticmethod
    def _boards() -> List[Dict[str, Any]]:
        from src.core.config import ConfigManager
        from src.core.pairs import board_pairs
        try:
            configured = credentials.load_credentials_model().configured_services()
        except Exception:
            configured = []
        return board_pairs(ConfigManager.load_settings(), configured)

    def _engine(self, pair_id: str):
        from src.core.config import ConfigManager
        from src.core.pairs import engine_for, engine_for_pair, find_pair
        board = next((b for b in self._boards() if b["id"] == pair_id), None)
        if board is None:
            raise ValueError(f"No sync pair {pair_id!r}")
        if board["saved"]:
            return engine_for_pair(find_pair(ConfigManager.load_settings(), pair_id))
        return engine_for(board["source"], board["target"])

    @staticmethod
    def _conflicts_file(board: Dict[str, Any]) -> str:
        """Where a pair's engine keeps its conflicts (SyncEngine: beside its
        state file) - read directly, so listing needs no engine and no login."""
        from src.core import config
        from src.core.conflicts import conflicts_path_for
        from src.core.pairs import service_id_of
        s, t = service_id_of(board["source"]), service_id_of(board["target"])
        name = "sync_state.json" if (s, t) == ("garmin", "divelogs") else f"sync_state_{s}_{t}.json"
        return conflicts_path_for(os.path.join(os.path.dirname(config.SETTINGS_FILE) or ".", name))

    @staticmethod
    def _label(key: str) -> str:
        from src.core.pairs import field_catalog_of
        service, _, _name = key.partition(".")
        try:
            return next((f.label for f in field_catalog_of(service) if f.key == key), key)
        except ValueError:
            return key

    @Slot()
    @Slot(str)
    def load(self, _pair_id: str = "") -> None:
        """Reload every pair's conflicts (the argument is accepted for older
        callers and ignored: the page shows all pairs)."""
        from src.core.conflicts import ConflictStore
        from src.core.pairs import display_name_of, service_id_of
        groups, problems = [], []
        for board in self._boards():
            try:
                items = ConflictStore(self._conflicts_file(board)).load()
            except Exception as e:
                problems.append(f"{board['id']}: {e}")
                continue
            if not items:
                continue
            names = {service_id_of(board["source"]): display_name_of(board["source"]),
                     service_id_of(board["target"]): display_name_of(board["target"])}
            conflicts = []
            for c in items:
                row = c.model_dump(mode="json")
                row.update(pair_id=board["id"],
                           source_name=names.get(c.source_service, c.source_service),
                           target_name=names.get(c.target_service, c.target_service),
                           source_label=self._label(c.source_key), target_label=self._label(c.target_key),
                           source_text=brief(row["source_value"]), target_text=brief(row["target_value"]))
                conflicts.append(row)
            groups.append({"pair_id": board["id"],
                           "label": f"{display_name_of(board['source'])} ↔ {display_name_of(board['target'])}",
                           "conflicts": conflicts})
        self._groups = groups
        self.conflictsChanged.emit()
        if problems:
            self._set_message("Could not read conflicts for " + "; ".join(problems))
        else:
            self._set_message("" if groups else "No conflicts waiting.")

    @Slot(str, str, str)
    def resolve(self, pair_id: str, conflict_id: str, winner: str) -> None:
        """``winner``: "source" or "target", as the conflict records its sides."""
        if self._busy:
            return
        self._busy = True
        self.busyChanged.emit()
        self._set_message("Updating the service…")

        def work():
            credentials.begin_operation()
            try:
                return self._engine(pair_id).resolve_conflict(conflict_id, winner).id
            finally:
                credentials.end_operation()

        def done(_):
            self._busy = False
            self.busyChanged.emit()
            self.load()
            if not self._message:
                self._set_message("Resolved.")

        def fail(message):
            self._busy = False
            self.busyChanged.emit()
            self._set_message(f"Failed: {message}")

        worker = Worker(work, parent=self)
        worker.finished_ok.connect(done)
        worker.failed.connect(fail)
        self._worker = worker
        worker.start()
