"""Conflicts view backend (rework.md D6).

Lists the conflicts of every sync pair at once - saved pairs and the
combinations of configured services the Mapping page offers - grouped by
pair, with field labels and readable values, so nothing waits unseen on a
pair the Mapping page does not happen to have open. A pair's history is kept
per account combination (rework.md E19), so each combination with
conflicts waiting is its own group, labelled with its accounts."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Property, QObject, Signal, Slot

from desktop import accounts, credentials
from desktop.jobs import Worker


from src.core.conflicts import display_value as brief  # noqa: E402  (kept importable as brief)


class ConflictsController(QObject):
    conflictsChanged = Signal()
    messageChanged = Signal()
    busyChanged = Signal()
    stagedChanged = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._groups: List[Dict[str, Any]] = []
        self._message = ""
        self._busy = False
        self._worker = None
        # Picks made on the page wait here until a Save button writes them;
        # picking the same side again, or Discard picks, takes them back.
        # conflict id -> {"pair_id", "winner", "service", "service_name"},
        # service being the side that gets updated (the loser).
        self._staged: Dict[str, Dict[str, str]] = {}

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

    @Property("QVariantMap", notify=stagedChanged)
    def staged(self):
        """conflict id -> the side picked to win ("source" / "target"), for
        every pick not yet saved."""
        return {cid: pick["winner"] for cid, pick in self._staged.items()}

    @Property(int, notify=stagedChanged)
    def stagedCount(self) -> int:
        return len(self._staged)

    @Property("QVariantList", notify=stagedChanged)
    def pendingServices(self):
        """One entry per service with picks waiting to be written to it:
        [{service, name, count}], the page's Save buttons."""
        by_service: Dict[str, Dict[str, Any]] = {}
        for pick in self._staged.values():
            entry = by_service.setdefault(pick["service"], {"service": pick["service"], "name": pick["service_name"], "count": 0})
            entry["count"] += 1
        return sorted(by_service.values(), key=lambda e: e["name"])

    def _set_message(self, text: str) -> None:
        self._message = text
        self.messageChanged.emit()

    @staticmethod
    def _boards() -> List[Dict[str, Any]]:
        from src.core.config import ConfigManager
        from src.core.pairs import board_pairs
        try:
            configured = credentials.load_credentials_model().configured_specs()
        except Exception:
            configured = []
        return board_pairs(ConfigManager.load_settings(), configured)

    # A group is one pair run as one account combination; its key travels
    # through QML as the conflicts' pair_id.
    _KEY_SEP = "::"

    @classmethod
    def _group_key(cls, board_id: str, source_account: str, target_account: str) -> str:
        return cls._KEY_SEP.join((board_id, source_account, target_account))

    def _engine(self, group_key: str):
        from src.core.config import ConfigManager
        from src.core.pairs import engine_for, engine_for_pair, find_pair, service_id_of
        board_id, _, rest = group_key.partition(self._KEY_SEP)
        source_account, _, target_account = rest.partition(self._KEY_SEP)
        board = next((b for b in self._boards() if b["id"] == board_id), None)
        if board is None:
            raise ValueError(f"No sync pair {board_id!r}")
        selection = {service_id_of(board["source"]): source_account, service_id_of(board["target"]): target_account}
        kwargs = accounts.engine_kwargs({k: v for k, v in selection.items() if v})
        if board["saved"]:
            return engine_for_pair(find_pair(ConfigManager.load_settings(), board_id), **kwargs)
        return engine_for(board["source"], board["target"], **kwargs)

    @staticmethod
    def _conflicts_file(board: Dict[str, Any], source_account: str, target_account: str) -> str:
        """Where a pair's engine keeps one account combination's conflicts
        (SyncEngine: beside its state file) - read directly, so listing needs
        no engine and no login."""
        from src.core import config
        from src.core.conflicts import conflicts_path_for
        from src.core.pairs import account_state_file, service_id_of
        state_dir = os.path.dirname(config.SETTINGS_FILE) or "."
        return conflicts_path_for(account_state_file(state_dir, service_id_of(board["source"]), source_account,
                                                     service_id_of(board["target"]), target_account))

    @staticmethod
    def _side_label(spec: str, account: str) -> str:
        from src.core.pairs import display_name_of
        return f"{display_name_of(spec)} ({account})" if account else display_name_of(spec)

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
        try:
            model = credentials.load_credentials_model()
        except Exception:
            model = None
        combos = [(board, sa, ta) for board in self._boards()
                  for sa in accounts.accounts_for_spec(board["source"], model)
                  for ta in accounts.accounts_for_spec(board["target"], model)]
        for board, source_account, target_account in combos:
            key = self._group_key(board["id"], source_account, target_account)
            try:
                items = ConflictStore(self._conflicts_file(board, source_account, target_account)).load()
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
                row.update(pair_id=key,
                           source_name=names.get(c.source_service, c.source_service),
                           target_name=names.get(c.target_service, c.target_service),
                           source_label=self._label(c.source_key), target_label=self._label(c.target_key),
                           source_text=brief(row["source_value"]), target_text=brief(row["target_value"]))
                conflicts.append(row)
            groups.append({"pair_id": key,
                           "label": f"{self._side_label(board['source'], source_account)} ↔ "
                                    f"{self._side_label(board['target'], target_account)}",
                           "conflicts": conflicts})
        self._groups = groups
        self.conflictsChanged.emit()
        # a pick for a conflict that a sync meanwhile re-evaluated away is dropped
        present = {c["id"] for g in groups for c in g["conflicts"]}
        if any(cid not in present for cid in self._staged):
            self._staged = {cid: pick for cid, pick in self._staged.items() if cid in present}
            self.stagedChanged.emit()
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

    def _conflict(self, conflict_id: str) -> Optional[Dict[str, Any]]:
        return next((c for g in self._groups for c in g["conflicts"] if c["id"] == conflict_id), None)

    @Slot(str, str, str)
    def stage(self, pair_id: str, conflict_id: str, winner: str) -> None:
        """Pick a side for one conflict; nothing is written until save().
        Picking the side already picked undoes the pick."""
        if self._busy or winner not in ("source", "target"):
            return
        current = self._staged.get(conflict_id)
        if current and current["winner"] == winner:
            self.unstage(conflict_id)
            return
        row = self._conflict(conflict_id)
        if row is None:
            return
        loser = "target" if winner == "source" else "source"
        self._staged[conflict_id] = {"pair_id": pair_id, "winner": winner,
                                     "service": row[f"{loser}_service"], "service_name": row[f"{loser}_name"]}
        self.stagedChanged.emit()

    @Slot(str)
    def unstage(self, conflict_id: str) -> None:
        """Undo one pick."""
        if self._busy or conflict_id not in self._staged:
            return
        del self._staged[conflict_id]
        self.stagedChanged.emit()

    @Slot()
    def discard(self) -> None:
        """Take back every pick."""
        if self._busy or not self._staged:
            return
        self._staged = {}
        self.stagedChanged.emit()
        self._set_message("")

    @Slot()
    @Slot(str)
    def save(self, service: str = "") -> None:
        """Write the staged picks to the losing service - the picks that
        update ``service`` (one Save button), or every pick when it is empty.
        One engine (one login) per pair, every pick attempted even when an
        earlier one fails; picks that failed stay staged for another try."""
        if self._busy:
            return
        picks = {cid: pick for cid, pick in self._staged.items() if not service or pick["service"] == service}
        if not picks:
            return
        decisions = [(cid, pick["pair_id"], pick["winner"]) for cid, pick in picks.items()]
        names = sorted({pick["service_name"] for pick in picks.values()})
        self._busy = True
        self.busyChanged.emit()
        n = len(decisions)
        self._set_message(f"Updating {' and '.join(names)} with {n} {'change' if n == 1 else 'changes'}…")

        def work():
            credentials.begin_operation()
            engines: Dict[str, Any] = {}
            done, failures = [], []
            try:
                for cid, pair_id, winner in decisions:
                    try:
                        if pair_id not in engines:
                            engines[pair_id] = self._engine(pair_id)
                        engines[pair_id].resolve_conflict(cid, winner)
                        done.append(cid)
                    except Exception as e:
                        failures.append((cid, str(e)))
            finally:
                credentials.end_operation()
            return done, failures

        def finished(result):
            done, failures = result
            for cid in done:
                self._staged.pop(cid, None)
            self.stagedChanged.emit()
            self._busy = False
            self.busyChanged.emit()
            self.load()
            parts = [f"{' and '.join(names)}: saved {len(done)} of {n}."]
            if failures:
                parts.append(f"{len(failures)} failed and {'is' if len(failures) == 1 else 'are'} still staged: "
                             + "; ".join(detail for _, detail in failures))
            self._set_message(" ".join(parts))

        def fail(message):
            self._busy = False
            self.busyChanged.emit()
            self._set_message(f"Failed: {message}")

        worker = Worker(work, parent=self)
        worker.finished_ok.connect(finished)
        worker.failed.connect(fail)
        self._worker = worker
        worker.start()
