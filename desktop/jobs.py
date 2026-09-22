"""Background work for the Qt desktop app.

``Worker`` runs one callable on a ``QThread`` and reports back through
signals on the GUI thread. ``LogPump`` drains the ``dive_sync`` log queue
(see ``desktop/logging_bridge.py``) on a ``QTimer`` and emits each line, so
pages can show a live log without touching threads themselves.
"""
from __future__ import annotations

import queue
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, QTimer, Signal


class Worker(QThread):
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, target: Callable[[], object], parent: Optional[QObject] = None):
        super().__init__(parent)
        self._target = target

    def run(self) -> None:
        try:
            result = self._target()
        except Exception as e:  # reported to the UI, never swallowed
            self.failed.emit(str(e))
            return
        self.finished_ok.emit(result)


class LogPump(QObject):
    line = Signal(str)

    def __init__(self, log_queue: queue.Queue, interval_ms: int = 250, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._queue = log_queue
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.drain)

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self.drain()

    def drain(self) -> None:
        while True:
            try:
                text = self._queue.get_nowait()
            except queue.Empty:
                return
            self.line.emit(text)
