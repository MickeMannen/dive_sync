import logging
import queue
from collections import deque
from typing import Deque, List


class ShortFormatter(logging.Formatter):
    """What the Sync page shows per line: the time and the message, with the
    level only when it is a warning or an error (the full logger name and
    date push the message itself off the screen)."""

    def __init__(self):
        super().__init__("%(asctime)s  %(message)s", datefmt="%H:%M:%S")

    def format(self, record):
        line = super().format(record)
        if record.levelno >= logging.WARNING:
            stamp, _, message = line.partition("  ")
            line = f"{stamp}  {record.levelname}: {message}"
        return line


class QueueLogHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        try:
            self.log_queue.put_nowait(self.format(record))
        except Exception:
            pass


# The last lines logged, for the debug report (plans/convert.md I21): a
# bounded ring, one for the process, filled by `RecentLinesHandler` (installed
# once by `install`) and read by `recent_lines`.
RECENT_LINES = 300
_recent: Deque[str] = deque(maxlen=RECENT_LINES)


class RecentLinesHandler(logging.Handler):
    """Keeps the last `RECENT_LINES` formatted lines in `_recent` (date,
    time, level, logger and message - fuller than the page's line, since
    the report is read away from the app)."""

    def __init__(self):
        super().__init__()
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

    def emit(self, record):
        try:
            _recent.append(self.format(record))
        except Exception:
            pass


def recent_lines() -> List[str]:
    """The last `RECENT_LINES` lines logged under "dive_sync", oldest first."""
    return list(_recent)


def install() -> queue.Queue:
    """Attach a queue-backed handler to the shared "dive_sync" logger so any
    section can drain sync engine / adapter log output for display, the same
    way src/web/app.py's SSELogHandler feeds the Docker status page; and,
    once per process, the ring of recent lines the debug report includes."""
    log_queue = queue.Queue()
    handler = QueueLogHandler(log_queue)
    handler.setFormatter(ShortFormatter())
    logger = logging.getLogger("dive_sync")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    if not any(isinstance(h, RecentLinesHandler) for h in logger.handlers):
        logger.addHandler(RecentLinesHandler())
    return log_queue
