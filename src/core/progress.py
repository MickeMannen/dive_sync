"""One place to say how far a long job has got (rework.md E14).

A Garmin refresh costs three API calls per dive with a cool-down between
each, so fetching a few dozen takes minutes. The adapters have always logged
``[3/40] Fetching...``, but a log line is not something a progress bar can
read, so both UIs could only show an indeterminate spinner.

Adapters call ``report()`` as they go; whatever is driving the UI reads
``current()``. Deliberately a single module-level slot rather than a
callback threaded through every adapter signature: only one long job runs at
a time (``scheduler.is_download_running`` and ``is_sync_running`` both
enforce that), and a global keeps the reporting call a one-liner at the few
places that know the count.

Nothing here blocks, and reading is always safe: a UI that polls while
nothing is running simply gets ``None``.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

_lock = threading.Lock()
_state: Optional[Dict[str, Any]] = None


def report(done: int, total: int, message: str = "", service: str = "") -> None:
    """Called from the worker thread as a job advances. ``total`` of 0 means
    the size is not known yet, which a UI should show as indeterminate."""
    global _state
    with _lock:
        _state = {
            "done": int(done),
            "total": int(total),
            "message": message,
            "service": service,
            "fraction": (done / total) if total > 0 else None,
            "updated_at": time.time(),
        }


def clear() -> None:
    global _state
    with _lock:
        _state = None


def current() -> Optional[Dict[str, Any]]:
    """A snapshot, or None when nothing is reporting."""
    with _lock:
        return dict(_state) if _state else None


def text() -> str:
    """One line for a status label, empty when nothing is running."""
    state = current()
    if not state:
        return ""
    if state["total"] > 0:
        head = f"{state['done']}/{state['total']}"
        return f"{state['message']} ({head})" if state["message"] else head
    return state["message"]
