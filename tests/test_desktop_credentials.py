import os

import keyring
import pytest

import desktop.credentials as creds_store
from src.core.config import CredentialsModel, GarminCredentials, DivelogsCredentials
from src.core.services.garmin import safe_token_filename


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, key, value):
        self.store[(service, key)] = value

    def get_password(self, service, key):
        return self.store.get((service, key))

    def delete_password(self, service, key):
        if (service, key) not in self.store:
            raise creds_store.keyring.errors.PasswordDeleteError()
        del self.store[(service, key)]


@pytest.fixture
def fake_keyring(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(creds_store.keyring, "set_password", fake.set_password)
    monkeypatch.setattr(creds_store.keyring, "get_password", fake.get_password)
    monkeypatch.setattr(creds_store.keyring, "delete_password", fake.delete_password)
    return fake


@pytest.fixture(autouse=True)
def isolated_preferences(tmp_path, monkeypatch):
    """Garmin token_dir (and Subsurface/Submersion's non-secret fields) live
    in desktop/preferences.py's plain JSON file since 2026-09-22 (D9
    finding: too many separate keychain items), so account round-trip tests
    now touch it too - must never be the real one (AGENTS.md/rework.md:
    desktop tests never touch real app-data)."""
    import desktop.preferences as prefs
    monkeypatch.setattr(prefs, "PREFS_FILE", str(tmp_path / "desktop_prefs.json"))


def test_has_any_credentials_initially_false(fake_keyring):
    assert creds_store.has_any_credentials() is False


def test_keychain_service_is_the_app_identity():
    assert creds_store.SERVICE_NAME == "org.christersson.dive_sync"


def test_token_file_lands_in_the_accounts_own_folder(fake_keyring, tmp_path, monkeypatch):
    """A blank token_dir (the default) is the account's garmin/<account>/tokens
    folder under DATA_DIR (rework.md E21), where the token is materialized
    for a login and removed again afterwards."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    creds_store.save_credentials_model(CredentialsModel(garmin=[GarminCredentials(username="diver1", password="pw", token_dir="")]))
    account = creds_store.load_credentials_model().get_garmin_accounts()[0]
    assert account.token_dir == ""
    keyring.set_password(creds_store.SERVICE_NAME, creds_store._garmin_token_key("diver1"), '{"cached": true}')
    creds_store.materialize_garmin_token("diver1", account.token_dir)
    folder = tmp_path / "garmin" / "diver1" / "tokens"
    assert [p.name for p in folder.iterdir()] == [safe_token_filename("diver1")]
    creds_store.clear_garmin_token_file("diver1", account.token_dir)
    assert list(folder.iterdir()) == []


def test_save_and_load_round_trip(fake_keyring):
    model = CredentialsModel(
        garmin=GarminCredentials(username="diver1", password="secret1", token_dir="tokens/garmin"),
        divelogs=DivelogsCredentials(username="diver1_dl", password="secret2"),
    )
    creds_store.save_credentials_model(model)

    assert creds_store.has_any_credentials() is True
    loaded = creds_store.load_credentials_model()
    garmin = loaded.get_garmin_accounts()[0]
    divelogs = loaded.get_divelogs_accounts()[0]
    assert garmin.username == "diver1"
    assert garmin.password == "secret1"
    assert garmin.token_dir == "tokens/garmin"
    assert divelogs.username == "diver1_dl"
    assert divelogs.password == "secret2"


def test_save_clearing_password_deletes_it(fake_keyring):
    creds_store.save_credentials_model(
        CredentialsModel(garmin=GarminCredentials(username="diver1", password="secret1"))
    )
    creds_store.save_credentials_model(
        CredentialsModel(garmin=GarminCredentials(username="", password=""))
    )

    assert creds_store.load_credentials_model().get_garmin_accounts() == []


def test_multiple_garmin_accounts_round_trip(fake_keyring):
    model = CredentialsModel(garmin=[
        GarminCredentials(username="diver1", password="secret1", token_dir="tokens/diver1"),
        GarminCredentials(username="diver2@example.com", password="secret2", token_dir="tokens/diver2"),
    ])
    creds_store.save_credentials_model(model)

    loaded = {a.username: a for a in creds_store.load_credentials_model().get_garmin_accounts()}
    assert loaded["diver1"].password == "secret1" and loaded["diver1"].token_dir == "tokens/diver1"
    assert loaded["diver2@example.com"].password == "secret2"


def test_saving_blank_password_keeps_existing_one(fake_keyring):
    creds_store.save_credentials_model(CredentialsModel(garmin=[GarminCredentials(username="diver1", password="secret1")]))
    creds_store.save_credentials_model(CredentialsModel(garmin=[
        GarminCredentials(username="diver1", password=""),
        GarminCredentials(username="diver2", password="secret2"),
    ]))

    loaded = {a.username: a for a in creds_store.load_credentials_model().get_garmin_accounts()}
    assert loaded["diver1"].password == "secret1"
    assert loaded["diver2"].password == "secret2"


def test_removed_account_is_dropped(fake_keyring):
    creds_store.save_credentials_model(CredentialsModel(garmin=[
        GarminCredentials(username="diver1", password="secret1"),
        GarminCredentials(username="diver2", password="secret2"),
    ]))
    creds_store.save_credentials_model(CredentialsModel(garmin=[GarminCredentials(username="diver1", password="secret1")]))

    usernames = [a.username for a in creds_store.load_credentials_model().get_garmin_accounts()]
    assert usernames == ["diver1"]


def test_legacy_single_account_migrates_on_load(fake_keyring):
    # Simulates a keychain written by the pre-E7 single-account code path.
    creds_store._set("garmin", "username", "diver1")
    creds_store._set("garmin", "password", "secret1")
    creds_store._set("garmin", "token_dir", "tokens/garmin")

    loaded = creds_store.load_credentials_model()
    assert [a.username for a in loaded.get_garmin_accounts()] == ["diver1"]
    assert loaded.get_garmin_accounts()[0].password == "secret1"
    assert loaded.get_garmin_accounts()[0].token_dir == "tokens/garmin"
    # The migration is self-healing: the new consolidated item now holds it,
    # so a second load doesn't depend on the legacy keys any more, and the
    # (non-secret) token_dir moved into preferences.py.
    assert creds_store._get_json("garmin_accounts") == [{"username": "diver1", "password": "secret1"}]
    assert creds_store._get("garmin", "username") == ""


def test_materialize_local_cache_writes_via_config_manager(fake_keyring, monkeypatch):
    import src.core.config as config

    saved = {}
    monkeypatch.setattr(
        config.ConfigManager, "save_credentials", lambda creds, path=None: saved.update(model=creds)
    )

    creds_store._set("garmin", "username", "diver1")
    creds_store._set("garmin", "password", "secret1")

    creds_store.materialize_local_cache()

    assert saved["model"].get_garmin_accounts()[0].username == "diver1"


def test_clear_local_cache_removes_file(tmp_path, monkeypatch):
    import src.core.config as config

    creds_file = tmp_path / "credentials.json"
    creds_file.write_text("{}")
    monkeypatch.setattr(config, "CREDENTIALS_FILE", str(creds_file))

    creds_store.clear_local_cache()

    assert not creds_file.exists()


def test_clear_local_cache_missing_file_is_a_noop(tmp_path, monkeypatch):
    import src.core.config as config

    monkeypatch.setattr(config, "CREDENTIALS_FILE", str(tmp_path / "does_not_exist.json"))
    creds_store.clear_local_cache()


@pytest.fixture(autouse=True)
def reset_operation_counter():
    creds_store._active_operations = 0
    yield
    creds_store._active_operations = 0


def test_begin_end_operation_materializes_and_clears(fake_keyring, monkeypatch):
    calls = []
    monkeypatch.setattr(creds_store, "materialize_local_cache", lambda: calls.append("materialize"))
    monkeypatch.setattr(creds_store, "clear_local_cache", lambda: calls.append("clear"))

    creds_store.begin_operation()
    assert calls == ["materialize"]

    creds_store.end_operation()
    assert calls == ["materialize", "clear"]


def test_concurrent_operations_only_materialize_and_clear_once(fake_keyring, monkeypatch):
    calls = []
    monkeypatch.setattr(creds_store, "materialize_local_cache", lambda: calls.append("materialize"))
    monkeypatch.setattr(creds_store, "clear_local_cache", lambda: calls.append("clear"))

    creds_store.begin_operation()
    creds_store.begin_operation()
    assert calls == ["materialize"]

    creds_store.end_operation()
    assert calls == ["materialize"]

    creds_store.end_operation()
    assert calls == ["materialize", "clear"]


def test_begin_end_operation_materializes_and_syncs_back_garmin_token(fake_keyring, tmp_path, monkeypatch):
    monkeypatch.setattr(creds_store, "materialize_local_cache", lambda: None)
    monkeypatch.setattr(creds_store, "clear_local_cache", lambda: None)

    token_dir = str(tmp_path / "tokens" / "garmin")
    creds_store.save_credentials_model(
        CredentialsModel(garmin=GarminCredentials(username="diver1", password="secret1", token_dir=token_dir))
    )
    keyring.set_password(creds_store.SERVICE_NAME, creds_store._garmin_token_key("diver1"), '{"cached": true}')

    creds_store.begin_operation()
    token_path = os.path.join(token_dir, safe_token_filename("diver1"))
    assert os.path.exists(token_path)
    with open(token_path) as f:
        assert f.read() == '{"cached": true}'

    # Simulate garth refreshing the token mid-operation.
    with open(token_path, "w") as f:
        f.write('{"cached": true, "refreshed": true}')

    creds_store.end_operation()
    assert not os.path.exists(token_path)
    assert (
        keyring.get_password(creds_store.SERVICE_NAME, creds_store._garmin_token_key("diver1"))
        == '{"cached": true, "refreshed": true}'
    )


def test_begin_operation_without_a_garmin_account_skips_token_handling(fake_keyring, monkeypatch):
    monkeypatch.setattr(creds_store, "materialize_local_cache", lambda: None)
    monkeypatch.setattr(creds_store, "clear_local_cache", lambda: None)

    called = []
    monkeypatch.setattr(creds_store, "materialize_garmin_token", lambda *a: called.append("materialize"))
    monkeypatch.setattr(creds_store, "sync_garmin_token_from_file", lambda *a: called.append("sync"))
    monkeypatch.setattr(creds_store, "clear_garmin_token_file", lambda *a: called.append("clear"))

    creds_store.begin_operation()
    creds_store.end_operation()

    assert called == []
