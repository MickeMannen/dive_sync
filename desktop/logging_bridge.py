import logging
import queue


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


def install() -> queue.Queue:
    """Attach a queue-backed handler to the shared "dive_sync" logger so any
    section can drain sync engine / adapter log output for display, the same
    way src/web/app.py's SSELogHandler feeds the Docker status page."""
    log_queue = queue.Queue()
    handler = QueueLogHandler(log_queue)
    handler.setFormatter(ShortFormatter())
    logger = logging.getLogger("dive_sync")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return log_queue
