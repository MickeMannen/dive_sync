"""Credential models for Subsurface Cloud and the Submersion S3 store, their
verification helpers, and the interactive setup script (driven with fake
input; no network)."""
import json
import os
from types import SimpleNamespace

import pytest

import setup_credentials
from src.core.config import ConfigManager, CredentialsModel, SubmersionCredentials, SubsurfaceCredentials
from src.core.services import subsurface_cloud
from src.core.services.submersion import store as submersion_store


# ---------------------------------------------------------------- models

def test_old_credentials_file_loads_with_empty_new_sections(tmp_path):
    path = str(tmp_path / "credentials.json")
    with open(path, "w") as f:
        json.dump({"garmin": {"username": "g", "password": "p", "token_dir": "tokens/garmin"},
                   "divelogs": {"username": "d", "password": "p"}}, f)
    creds = ConfigManager.load_credentials(path)
    assert creds.configured_services() == ["garmin", "divelogs"]
    assert not creds.subsurface.configured and not creds.submersion.configured
    ConfigManager.save_credentials(creds, path)
    saved = json.load(open(path))
    assert saved["garmin"]["username"] == "g" and saved["subsurface"]["email"] == ""


def test_new_sections_round_trip_and_configured_rules(tmp_path, submersion_enabled):
    creds = CredentialsModel(
        subsurface=SubsurfaceCredentials(email="me@x.org", password="pw"),
        submersion=SubmersionCredentials(endpoint_url="https://s3.eu-central-003.backblazeb2.com", region="eu-central-003",
                                         bucket="dives", access_key_id="id", secret_access_key="key"),
    )
    path = str(tmp_path / "credentials.json")
    ConfigManager.save_credentials(creds, path)
    loaded = ConfigManager.load_credentials(path)
    assert loaded == creds and loaded.configured_services() == ["subsurface", "submersion"]
    assert not SubmersionCredentials(endpoint_url="x", bucket="b").configured
    assert SubmersionCredentials(store_type="folder", folder_path="/x").configured


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


# ---------------------------------------------------------------- S3 store

class FakeS3Client:
    def __init__(self, objects, error=None):
        self.objects = objects
        self.error = error
        self.calls = []

    def list_objects_v2(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        keys = [k for k in self.objects if k.startswith(kw["Prefix"])]
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def get_object(self, Bucket, Key):
        return {"Body": SimpleNamespace(read=lambda: self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key)


def _b2():
    # No region: it is read out of the endpoint, exactly as Submersion does it.
    return SubmersionCredentials(endpoint_url="https://s3.eu-central-003.backblazeb2.com",
                                 bucket="dives", prefix="submersion-sync/", access_key_id="id", secret_access_key="key")


@pytest.mark.parametrize("typed, endpoint, region", [
    # Submersion asks for a bare host; https is assumed, never plain http.
    ("s3.eu-central-003.backblazeb2.com", "https://s3.eu-central-003.backblazeb2.com", "eu-central-003"),
    ("https://s3.eu-central-003.backblazeb2.com/", "https://s3.eu-central-003.backblazeb2.com", "eu-central-003"),
    ("  s3.us-west-002.backblazeb2.com  ", "https://s3.us-west-002.backblazeb2.com", "us-west-002"),
    ("abc123.r2.cloudflarestorage.com", "https://abc123.r2.cloudflarestorage.com", "auto"),
    ("s3.amazonaws.com", "https://s3.amazonaws.com", "us-east-1"),
    ("s3.eu-west-1.amazonaws.com", "https://s3.eu-west-1.amazonaws.com", "eu-west-1"),
    ("fra1.digitaloceanspaces.com", "https://fra1.digitaloceanspaces.com", "fra1"),
    # An explicit http:// is kept - a self-hosted store without TLS has to ask
    # for it - and such a host names no region of its own.
    ("http://192.168.1.5:9000", "http://192.168.1.5:9000", ""),
    ("", "", ""),
])
def test_endpoint_is_normalized_and_region_read_from_it(typed, endpoint, region):
    creds = SubmersionCredentials(endpoint_url=typed, bucket="b", access_key_id="i", secret_access_key="s")
    assert creds.endpoint_url == endpoint
    assert creds.region == ""  # nothing saved; derived on use
    assert creds.effective_region == region


def test_region_override_wins_over_the_endpoint():
    creds = _b2().model_copy(update={"region": "somewhere-else"})
    assert creds.effective_region == "somewhere-else"


def test_s3_store_operations_and_access_check():
    store = submersion_store.S3Store(_b2())
    client = FakeS3Client({"submersion-sync/ssv1.manifest.abc.json": b"{}", "submersion-sync/other.txt": b"x",
                           "elsewhere/ssv1.manifest.zzz.json": b"{}"})
    store._client = client
    assert sorted(store.list()) == ["other.txt", "ssv1.manifest.abc.json"]
    assert store.list("ssv1.") == ["ssv1.manifest.abc.json"]
    assert client.calls[0]["Bucket"] == "dives" and client.calls[0]["Prefix"] == "submersion-sync/"
    assert store.get("ssv1.manifest.abc.json") == b"{}"
    store.put("new.json", b"1")
    assert store.get("new.json") == b"1"
    store.delete("new.json")
    ok, msg = store.check_access()
    assert ok and "1 Submersion sync file(s)" in msg

    class AccessDenied(Exception):
        response = {"Error": {"Code": "AccessDenied"}}
    store._client = FakeS3Client({}, error=AccessDenied("denied"))
    ok, msg = store.check_access()
    assert not ok and "AccessDenied" in msg and "application key" in msg

    with pytest.raises(submersion_store.StoreError):
        submersion_store.S3Store(SubmersionCredentials(store_type="folder", folder_path="/x"))


def test_s3_client_is_built_from_config(monkeypatch):
    captured = {}
    class FakeConfig:
        def __init__(self, **kw):
            captured["config"] = kw
    def fake_client(name, **kw):
        captured["client"] = (name, kw)
        return object()
    import boto3
    import botocore.config
    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.setattr(botocore.config, "Config", FakeConfig)
    store = submersion_store.S3Store(_b2().model_copy(update={"path_style": True}))
    store.client
    name, kw = captured["client"]
    assert name == "s3" and kw["endpoint_url"] == "https://s3.eu-central-003.backblazeb2.com"
    assert kw["region_name"] == "eu-central-003" and kw["aws_access_key_id"] == "id"
    assert captured["config"]["s3"] == {"addressing_style": "path"}
    assert captured["config"]["request_checksum_calculation"] == "when_required"


def test_folder_store_check(tmp_path):
    ok, msg = submersion_store.check_store_access(SubmersionCredentials(store_type="folder", folder_path=str(tmp_path)))
    assert ok and "0 Submersion" in msg
    ok, msg = submersion_store.check_store_access(SubmersionCredentials(store_type="folder", folder_path=str(tmp_path / "no")))
    assert not ok


# ---------------------------------------------------------------- setup script

def _drive(monkeypatch, answers, secrets):
    answers, secrets = list(answers), list(secrets)
    monkeypatch.setattr("builtins.input", lambda prompt="": answers.pop(0))
    monkeypatch.setattr(setup_credentials.getpass, "getpass", lambda prompt="": secrets.pop(0))


def test_setup_adds_subsurface_and_b2_without_touching_existing(tmp_path, monkeypatch):
    path = str(tmp_path / "credentials.json")
    with open(path, "w") as f:
        json.dump({"garmin": [{"username": "g1", "password": "p1"}, {"username": "g2", "password": "p2"}],
                   "divelogs": {"username": "d", "password": "p"}}, f)
    monkeypatch.setattr(subsurface_cloud, "check_cloud_login", lambda e, p, b: (True, "ok"))
    monkeypatch.setattr(submersion_store, "check_store_access", lambda c: (True, "ok"))
    # subsurface: email, server; submersion: endpoint (typed without a scheme,
    # as Submersion itself shows it), bucket, key id, no region override, prefix
    _drive(monkeypatch, ["me@x.org", "", "s3.eu-central-003.backblazeb2.com", "dives", "keyid", "", ""],
           ["pw", "secret"])
    rc = setup_credentials.run(["subsurface", "submersion"], path=path, assume_yes=True)
    assert rc == 0
    saved = json.load(open(path))
    assert saved["garmin"][0]["username"] == "g1" and saved["garmin"][1]["username"] == "g2"
    assert saved["divelogs"]["username"] == "d"
    assert saved["subsurface"]["email"] == "me@x.org" and saved["subsurface"]["password"] == "pw"
    sub = saved["submersion"]
    assert sub["endpoint_url"] == "https://s3.eu-central-003.backblazeb2.com"
    # Nothing saved for the region: it is read back out of the endpoint on use.
    assert sub["region"] == "" and sub["bucket"] == "dives" and sub["prefix"] == "submersion-sync/"
    assert SubmersionCredentials(**sub).effective_region == "eu-central-003"
    assert sub["access_key_id"] == "keyid" and sub["secret_access_key"] == "secret"
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
