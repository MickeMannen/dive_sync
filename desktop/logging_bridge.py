import logging
import queue


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
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger = logging.getLogger("dive_sync")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return log_queue
