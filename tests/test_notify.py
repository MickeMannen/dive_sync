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
