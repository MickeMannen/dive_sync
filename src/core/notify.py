"""Failure alerts (rework.md A11): an optional webhook POSTed to when a
scheduled or manual sync run fails - and, opt-in through ``notify_on``
(plans/gap-roadmap.md R24), after a run that changed something or after
every finished run, with a one-line summary of its counts.

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
from typing import Any, Dict, Optional, Tuple

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


def run_counts(results: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """What a finished run did, summed over its receivers: added
    (``uploaded_to_*``), updated (``updated_on_*``), deleted (``deleted_on_*``),
    skipped, failed and conflicts (plans/gap-roadmap.md R24)."""
    counts = {"added": 0, "updated": 0, "deleted": 0, "skipped": 0, "failed": 0, "conflicts": 0}
    for key, value in (results or {}).items():
        if not isinstance(value, list):
            continue
        if key.startswith("uploaded_to_"):
            counts["added"] += len(value)
        elif key.startswith("updated_on_"):
            counts["updated"] += len(value)
        elif key.startswith("deleted_on_"):
            counts["deleted"] += len(value)
        elif key in ("skipped", "failed", "conflicts"):
            counts[key] += len(value)
    return counts


def run_changed_something(results: Optional[Dict[str, Any]], dry_run: bool) -> bool:
    """``notify_on: changes``: the run wrote something (added, updated or
    deleted a dive), raised a conflict for the owner, or had writes the
    receiver refused. A dry run writes nothing, so it never counts."""
    if dry_run:
        return False
    counts = run_counts(results)
    return any(counts[k] for k in ("added", "updated", "deleted", "failed", "conflicts"))


def should_notify_finished(notify_on: str, results: Optional[Dict[str, Any]], dry_run: bool) -> bool:
    """Whether a run that finished (no exception, not stopped) is reported:
    never under 'failure' (the pre-R24 behaviour), when it changed something
    under 'changes', every time under 'always'."""
    if notify_on == "always":
        return True
    if notify_on == "changes":
        return run_changed_something(results, dry_run)
    return False


def run_summary(job_id: str, pair: Optional[str], results: Optional[Dict[str, Any]], dry_run: bool) -> str:
    """The success message, in the failure message's form ("Job 'x' failed:
    ..."): ``Job 'Manual' (garmin -> divelogs) finished: 2 added, 1 updated,
    0 skipped, 0 failed.`` - deleted and conflicts named only when there
    were any, and a dry run says so."""
    counts = run_counts(results)
    parts = [f"{counts['added']} added", f"{counts['updated']} updated"]
    if counts["deleted"]:
        parts.append(f"{counts['deleted']} deleted")
    parts += [f"{counts['skipped']} skipped", f"{counts['failed']} failed"]
    if counts["conflicts"]:
        parts.append(f"{counts['conflicts']} conflict{'s' if counts['conflicts'] != 1 else ''} waiting")
    where = f" ({pair})" if pair else ""
    text = f"Job '{job_id}'{where} finished: {', '.join(parts)}."
    if dry_run:
        text += " Dry run: nothing was written."
    return text


def notify_run_finished(url: str, job_id: str, pair: Optional[str],
                        results: Optional[Dict[str, Any]], dry_run: bool) -> None:
    """Best-effort success message (plans/gap-roadmap.md R24): like
    ``notify_run_failure`` it only logs, it never raises."""
    ok, detail = send_notification(url, "Dive Sync", run_summary(job_id, pair, results, dry_run))
    if not ok:
        logger.warning("Run summary for job '%s' was not delivered: %s", job_id, detail)
