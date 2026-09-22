"""Failure alerts (rework.md A11): an optional webhook POSTed to when a
scheduled or manual sync run fails.

One plain-text POST body with a ``Title`` header - the exact shape ntfy's
own docs use (``curl -H "Title: ..." -d "message" ntfy.sh/<topic>``), and
close enough to work as a generic webhook receiver. Gotify's own REST API
wants JSON/form fields with a token in the URL, not a raw body, so this only
reaches a Gotify server if ``notify_url`` points at something that accepts
plain text (a relay, or a Gotify version/plugin that does) - a real
limitation, documented in the status page and README rather than papered
over with per-service branching this module has no way to detect reliably
from a URL alone.
"""
import logging
from typing import Tuple

import requests

logger = logging.getLogger("dive_sync.notify")


def send_notification(url: str, title: str, message: str, timeout: float = 10.0) -> Tuple[bool, str]:
    """POST ``message`` to ``url``. Returns (ok, detail) - detail is a short
    human-readable status/error, never raises."""
    if not url:
        return False, "No notify_url configured."
    try:
        response = requests.post(
            url,
            data=message.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8", "Title": title},
            timeout=timeout,
        )
        if response.ok:
            return True, f"Sent (HTTP {response.status_code})."
        return False, f"Server returned HTTP {response.status_code}."
    except requests.RequestException as e:
        logger.warning("Failure-alert webhook to %s failed: %s", url, e)
        return False, str(e)


def notify_run_failure(url: str, job_id: str, error: str) -> None:
    """Best-effort: a broken or unreachable notify_url must never fail the
    sync run it's reporting on, so this only logs, it never raises."""
    ok, detail = send_notification(url, "Dive Sync", f"Job '{job_id}' failed: {error}")
    if not ok:
        logger.warning("Failure alert for job '%s' was not delivered: %s", job_id, detail)
