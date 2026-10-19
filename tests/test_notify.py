import requests

from src.core import notify


class _FakeResponse:
    def __init__(self, ok, status_code):
        self.ok = ok
        self.status_code = status_code


def test_send_notification_success(monkeypatch):
    captured = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        captured.update(url=url, data=data, headers=headers, timeout=timeout)
        return _FakeResponse(True, 200)

    monkeypatch.setattr(requests, "post", fake_post)

    ok, detail = notify.send_notification("https://ntfy.sh/mytopic", "Dive Sync", "job failed: boom")

    assert ok is True and "200" in detail
    assert captured["url"] == "https://ntfy.sh/mytopic"
    assert captured["data"] == b"job failed: boom"
    assert captured["headers"]["Title"] == "Dive Sync"
    assert captured["headers"]["Content-Type"].startswith("text/plain")


def test_send_notification_server_error(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeResponse(False, 500))

    ok, detail = notify.send_notification("https://example.com/hook", "t", "m")

    assert ok is False and "500" in detail


def test_send_notification_network_error(monkeypatch):
    def raise_error(*a, **kw):
        raise requests.RequestException("connection refused")

    monkeypatch.setattr(requests, "post", raise_error)

    ok, detail = notify.send_notification("https://example.com/hook", "t", "m")

    assert ok is False and "connection refused" in detail


def test_send_notification_no_url():
    ok, detail = notify.send_notification("", "t", "m")
    assert ok is False and "No notify_url" in detail


def test_notify_run_failure_never_raises_on_bad_url(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **kw: (_ for _ in ()).throw(requests.RequestException("boom")))
    notify.notify_run_failure("https://example.com/hook", "job-1", "kaboom")  # must not raise


# --- plans/gap-roadmap.md R24: a summary on success, opt-in through notify_on ---

_RESULTS = {
    "uploaded_to_divelogs": [{"id": "1"}, {"id": "2"}],
    "updated_on_divelogs": [{"id": "3"}],
    "skipped": [{"reason": "create_on_garmin_off"}],
    "failed": [],
    "conflicts": [],
    "matched_count": 7,
}


def test_run_counts_sums_every_bucket():
    counts = notify.run_counts({**_RESULTS, "deleted_on_garmin": [{}], "uploaded_to_garmin": [{}],
                                "failed": [{}], "conflicts": [{}, {}]})
    assert counts == {"added": 3, "updated": 1, "deleted": 1, "skipped": 1, "failed": 1, "conflicts": 2}
    assert notify.run_counts(None) == {"added": 0, "updated": 0, "deleted": 0, "skipped": 0,
                                       "failed": 0, "conflicts": 0}


def test_run_summary_reads_like_the_failure_message():
    assert notify.run_summary("nightly", "garmin -> divelogs", _RESULTS, False) == \
        "Job 'nightly' (garmin -> divelogs) finished: 2 added, 1 updated, 1 skipped, 0 failed."


def test_run_summary_names_deletes_conflicts_and_dry_run_only_when_there():
    text = notify.run_summary("Manual", None, {"deleted_on_garmin": [{}], "conflicts": [{}]}, True)
    assert text == ("Job 'Manual' finished: 0 added, 0 updated, 1 deleted, 0 skipped, 0 failed, "
                    "1 conflict waiting. Dry run: nothing was written.")
    assert "2 conflicts waiting" in notify.run_summary("j", None, {"conflicts": [{}, {}]}, False)


def test_should_notify_finished_per_choice():
    quiet = {"skipped": [{}], "matched_count": 4}
    assert notify.should_notify_finished("failure", _RESULTS, False) is False      # as before R24
    assert notify.should_notify_finished("always", quiet, False) is True
    assert notify.should_notify_finished("always", quiet, True) is True             # dry runs too
    assert notify.should_notify_finished("changes", _RESULTS, False) is True
    assert notify.should_notify_finished("changes", quiet, False) is False          # skips are no change
    assert notify.should_notify_finished("changes", _RESULTS, True) is False        # a dry run writes nothing
    assert notify.should_notify_finished("changes", {"failed": [{}]}, False) is True
    assert notify.should_notify_finished("changes", {"conflicts": [{}]}, False) is True
    assert notify.should_notify_finished("changes", {"deleted_on_garmin": [{}]}, False) is True


def test_notify_run_finished_posts_the_summary(monkeypatch):
    captured = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        captured.update(url=url, data=data, headers=headers)
        return _FakeResponse(True, 200)

    monkeypatch.setattr(requests, "post", fake_post)
    notify.notify_run_finished("https://ntfy.sh/mytopic", "nightly", "garmin -> divelogs", _RESULTS, False)
    assert captured["url"] == "https://ntfy.sh/mytopic"
    assert captured["data"] == b"Job 'nightly' (garmin -> divelogs) finished: 2 added, 1 updated, 1 skipped, 0 failed."
    assert captured["headers"]["Title"] == "Dive Sync"


def test_notify_run_finished_never_raises(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("unreachable")

    monkeypatch.setattr(requests, "post", boom)
    notify.notify_run_finished("https://example.com/hook", "job-1", None, _RESULTS, False)  # must not raise
