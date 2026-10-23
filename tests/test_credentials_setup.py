"""Credential models for Subsurface Cloud, their verification helper, and
the interactive setup script (driven with fake input; no network)."""
import json
import os
from types import SimpleNamespace

import pytest

import setup_credentials
from src.core.config import ConfigManager, CredentialsModel, SubsurfaceCredentials
from src.core.services import subsurface_cloud


# ---------------------------------------------------------------- models

def test_old_credentials_file_loads_with_empty_new_sections(tmp_path):
    path = str(tmp_path / "credentials.json")
    with open(path, "w") as f:
        json.dump({"garmin": {"username": "g", "password": "p", "token_dir": "tokens/garmin"},
                   "divelogs": {"username": "d", "password": "p"}}, f)
    creds = ConfigManager.load_credentials(path)
    assert creds.configured_services() == ["garmin", "divelogs"]
    assert not creds.subsurface.configured
    ConfigManager.save_credentials(creds, path)
    saved = json.load(open(path))
    assert saved["garmin"]["username"] == "g" and saved["subsurface"]["email"] == ""


def test_new_sections_round_trip_and_configured_rules(tmp_path):
    creds = CredentialsModel(subsurface=SubsurfaceCredentials(email="me@x.org", password="pw"))
    path = str(tmp_path / "credentials.json")
    ConfigManager.save_credentials(creds, path)
    loaded = ConfigManager.load_credentials(path)
    assert loaded == creds and loaded.configured_services() == ["subsurface"]


def test_credentials_file_with_a_submersion_store_still_loads(tmp_path):
    """plans/subsurface-submersion.md F18: Submersion support is gone. A
    credentials.json an older build wrote with a ``submersion`` block loads
    with every other section intact, the block is ignored (not a configured
    service) and the next save leaves it out."""
    path = str(tmp_path / "credentials.json")
    with open(path, "w") as f:
        json.dump({"garmin": {"username": "g", "password": "p"},
                   "subsurface": {"email": "me@x.org", "password": "pw"},
                   "submersion": {"store_type": "s3", "endpoint_url": "https://s3.eu-central-003.backblazeb2.com",
                                  "bucket": "dives", "access_key_id": "id", "secret_access_key": "key",
                                  "passphrase": "hunter2"}}, f)
    creds = ConfigManager.load_credentials(path)
    assert creds.configured_services() == ["garmin", "subsurface"]
    assert not hasattr(creds, "submersion")
    ConfigManager.save_credentials(creds, path)
    saved = json.load(open(path))
    assert "submersion" not in saved and saved["subsurface"]["email"] == "me@x.org"


# ---------------------------------------------------------------- subsurface cloud check

def test_subsurface_cloud_urls_and_login_check(monkeypatch):
    assert subsurface_cloud.repo_url("me@x.org") == "https://cloud.subsurface-divelog.org/git/me@x.org"
    assert subsurface_cloud.repo_url("me@x.org", "https://ssrf-cloud-eu.subsurface-divelog.org") == \
        "https://ssrf-cloud-eu.subsurface-divelog.org/git/me@x.org"
    assert subsurface_cloud.branch_name(" me@x.org ") == "me@x.org"

    calls = []
    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        calls.append((url, params, auth))
        return SimpleNamespace(status_code=fake_get.status, content=fake_get.body)
    monkeypatch.setattr(subsurface_cloud.requests, "get", fake_get)

    fake_get.status, fake_get.body = 200, b"001e# service=git-upload-pack\n0000"
    ok, msg = subsurface_cloud.check_cloud_login("me@x.org", "pw")
    assert ok and "branch 'me@x.org'" in msg
    assert calls[0] == ("https://cloud.subsurface-divelog.org/git/me@x.org/info/refs",
                        {"service": "git-upload-pack"}, ("me@x.org", "pw"))
    fake_get.status = 401
    assert subsurface_cloud.check_cloud_login("me@x.org", "pw") == (False, subsurface_cloud.check_cloud_login("me@x.org", "pw")[1])
    assert "rejected" in subsurface_cloud.check_cloud_login("me@x.org", "pw")[1]
    fake_get.status = 404
    assert "no repository" in subsurface_cloud.check_cloud_login("me@x.org", "pw")[1]
    fake_get.status, fake_get.body = 200, b"<html>"
    assert "not like a git repository" in subsurface_cloud.check_cloud_login("me@x.org", "pw")[1]
    assert subsurface_cloud.check_cloud_login("", "pw")[0] is False

    def boom(*a, **k):
        raise subsurface_cloud.requests.ConnectionError("down")
    monkeypatch.setattr(subsurface_cloud.requests, "get", boom)
    assert "Could not reach" in subsurface_cloud.check_cloud_login("me@x.org", "pw")[1]


# ---------------------------------------------------------------- setup script

def _drive(monkeypatch, answers, secrets):
    answers, secrets = list(answers), list(secrets)
    monkeypatch.setattr("builtins.input", lambda prompt="": answers.pop(0))
    monkeypatch.setattr(setup_credentials.getpass, "getpass", lambda prompt="": secrets.pop(0))


def test_setup_adds_subsurface_without_touching_existing(tmp_path, monkeypatch):
    path = str(tmp_path / "credentials.json")
    with open(path, "w") as f:
        json.dump({"garmin": [{"username": "g1", "password": "p1"}, {"username": "g2", "password": "p2"}],
                   "divelogs": {"username": "d", "password": "p"}}, f)
    monkeypatch.setattr(subsurface_cloud, "check_cloud_login", lambda e, p, b: (True, "ok"))
    _drive(monkeypatch, ["me@x.org", ""], ["pw"])              # subsurface: email, server
    rc = setup_credentials.run(["subsurface"], path=path, assume_yes=True)
    assert rc == 0
    saved = json.load(open(path))
    assert saved["garmin"][0]["username"] == "g1" and saved["garmin"][1]["username"] == "g2"
    assert saved["divelogs"]["username"] == "d"
    assert saved["subsurface"]["email"] == "me@x.org" and saved["subsurface"]["password"] == "pw"
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_setup_skips_configured_sections_and_keeps_failed_out(tmp_path, monkeypatch):
    path = str(tmp_path / "credentials.json")
    ConfigManager.save_credentials(CredentialsModel(subsurface=SubsurfaceCredentials(email="old@x", password="pw")), path)
    # decline to change subsurface; garmin fails verification and we decline to save
    from src.core.services import garmin as garmin_mod
    monkeypatch.setattr(garmin_mod.GarminAdapter, "login", lambda self: False)
    _drive(monkeypatch, ["n", "g", "n"], ["pw"])
    rc = setup_credentials.run(["subsurface", "garmin"], path=path)
    assert rc == 0
    saved = json.load(open(path))
    assert saved["subsurface"]["email"] == "old@x" and saved["garmin"]["username"] == ""

    # empty answers skip a section entirely
    _drive(monkeypatch, [""], [""])
    assert setup_credentials.run(["divelogs"], path=path, assume_yes=True) == 0
    assert json.load(open(path))["divelogs"]["username"] == ""


def test_setup_main_rejects_unknown_service():
    with pytest.raises(SystemExit):
        setup_credentials.main(["--services", "garmin,nope"])
    # Submersion support is gone (plans/subsurface-submersion.md F18)
    assert "submersion" not in setup_credentials.SERVICES
    with pytest.raises(SystemExit):
        setup_credentials.main(["--services", "submersion"])


def test_setup_ssi_edits_the_first_account_and_keeps_the_others(tmp_path, monkeypatch):
    """plans/myssi-sync.md K5: ``--services ssi`` asks email and password,
    checks the login once (faked here), writes the list shape, keeps a
    second account and the first one's token when its email is unchanged."""
    from src.core.services.ssi import SsiAdapter
    assert "ssi" in setup_credentials.SERVICES
    path = str(tmp_path / "credentials.json")
    with open(path, "w") as f:
        json.dump({"garmin": {"username": "g", "password": "p"},
                   "ssi": [{"email": "one@example.com", "password": "old", "token": "tok-1"},
                           {"email": "two@example.com", "password": "pw2"}]}, f)
    logins = []
    monkeypatch.setattr(SsiAdapter, "login", lambda self: logins.append(self.email) or True)
    _drive(monkeypatch, [""], ["new"])            # keep the email, a new password
    assert setup_credentials.run(["ssi"], path=path, assume_yes=True) == 0
    saved = json.load(open(path))
    assert saved["ssi"] == [{"email": "one@example.com", "password": "new", "token": "tok-1"},
                            {"email": "two@example.com", "password": "pw2", "token": ""}]
    assert saved["garmin"]["username"] == "g" and logins == ["one@example.com"]

    # another email is another account: no stored token travels with it; a
    # failed check is saved only on request
    monkeypatch.setattr(SsiAdapter, "login", lambda self: False)
    _drive(monkeypatch, ["y", "three@example.com", "n"], ["pw3"])     # change it? yes; save the failed check? no
    assert setup_credentials.run(["ssi"], path=path, assume_yes=False) == 0
    assert json.load(open(path))["ssi"][0]["email"] == "one@example.com"
    _drive(monkeypatch, ["three@example.com"], ["pw3"])
    assert setup_credentials.run(["ssi"], path=path, assume_yes=True) == 1
    assert json.load(open(path))["ssi"][0] == {"email": "three@example.com", "password": "pw3", "token": ""}

    # an empty answer skips the section, and a fresh file gets the list shape
    _drive(monkeypatch, [""], [""])
    assert setup_credentials.run(["ssi"], path=str(tmp_path / "none.json"), assume_yes=True) == 0
    assert not os.path.exists(tmp_path / "none.json")
