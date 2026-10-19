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


# --- MySSI accounts (plans/myssi-sync.md K2) ---------------------------------------

def test_ssi_accounts_round_trip_one_keychain_item(fake_keyring):
    """Several MySSI accounts in one keychain item (``ssi_accounts``, the
    consolidation of 2026-09-22), each with its password and token; a blank
    password keeps the stored one, a kept account keeps its token, a
    removed account goes with its token."""
    from src.core.config import SsiCredentials
    assert creds_store.load_credentials_model().get_ssi_accounts() == [] and not creds_store.has_any_credentials()
    assert not hasattr(creds_store, "load_ssi_credentials") and not hasattr(creds_store, "ssi_token_hook")   # the Dive edit era's first-account shim went with K4
    creds_store.save_ssi_accounts([SsiCredentials(email=" diver@example.com ", password="pw1", token="tok-1"),
                                   SsiCredentials(email="other@example.com", password="pw2"),
                                   SsiCredentials(email="diver@example.com", password="dup"),      # a repeat is dropped
                                   SsiCredentials(email="", password="nobody")])
    assert list(fake_keyring.store) == [(creds_store.SERVICE_NAME, "ssi_accounts")]
    loaded = creds_store.load_credentials_model()
    assert [(a.email, a.password, a.token) for a in loaded.get_ssi_accounts()] == [
        ("diver@example.com", "pw1", "tok-1"), ("other@example.com", "pw2", "")]
    assert loaded.configured_services() == ["ssi"] and creds_store.has_any_credentials()
    first = loaded.get_ssi_accounts()[0]
    assert first.configured and (first.email, first.token) == ("diver@example.com", "tok-1")
    # a blank password keeps the stored one and the token; the given token wins over the stored one
    creds_store.save_ssi_accounts([SsiCredentials(email="diver@example.com", password=""),
                                   SsiCredentials(email="other@example.com", password="", token="tok-2")])
    assert [(a.email, a.password, a.token) for a in creds_store.load_credentials_model().get_ssi_accounts()] == [
        ("diver@example.com", "pw1", "tok-1"), ("other@example.com", "pw2", "tok-2")]
    # a removed account takes its token with it; saving none clears the item
    creds_store.save_ssi_accounts([SsiCredentials(email="other@example.com", password="")])
    assert [(a.email, a.token) for a in creds_store.load_credentials_model().get_ssi_accounts()] == [("other@example.com", "tok-2")]
    creds_store.save_ssi_accounts([SsiCredentials(email="diver@example.com", password="pw1")])
    assert creds_store.load_credentials_model().get_ssi_accounts()[0].token == ""
    creds_store.save_ssi_accounts([])
    assert fake_keyring.store == {} and not creds_store.has_any_credentials()
    # the whole-model save carries the list like the other services'
    creds_store.save_credentials_model(CredentialsModel(ssi=[SsiCredentials(email="diver@example.com", password="pw1")]))
    assert [a.email for a in creds_store.load_credentials_model().get_ssi_accounts()] == ["diver@example.com"]


def test_single_ssi_login_migrates_once_into_the_account_list(fake_keyring):
    """The Dive edit era's one ``ssi`` item ({"email", "password", "token"},
    plans/convert.md I7) becomes the first account, token kept, and the
    old item is removed; a second load reads the list only."""
    creds_store._set_json("ssi", {"email": "diver@example.com", "password": "pw1", "token": "tok-old"})
    accounts = creds_store.load_credentials_model().get_ssi_accounts()
    assert [(a.email, a.password, a.token) for a in accounts] == [("diver@example.com", "pw1", "tok-old")]
    assert list(fake_keyring.store) == [(creds_store.SERVICE_NAME, "ssi_accounts")]
    assert creds_store._get_json("ssi_accounts") == [{"username": "diver@example.com", "password": "pw1", "token": "tok-old"}]
    # idempotent: the list is the source now, even with a stale single item put back
    creds_store._set_json("ssi", {"email": "stale@example.com", "password": "x", "token": ""})
    assert [a.email for a in creds_store.load_credentials_model().get_ssi_accounts()] == ["diver@example.com"]
    # a single item without an email is dropped, not migrated
    fake_keyring.store.clear()
    creds_store._set_json("ssi", {"email": "", "password": "pw", "token": "t"})
    assert creds_store.load_credentials_model().get_ssi_accounts() == [] and fake_keyring.store == {}


def test_ssi_token_is_stored_per_account(fake_keyring):
    from src.core.config import SsiCredentials
    creds_store.save_ssi_token("diver@example.com", "orphan")          # no such account: nothing happens
    assert fake_keyring.store == {}
    creds_store.save_ssi_accounts([SsiCredentials(email="diver@example.com", password="pw1"),
                                   SsiCredentials(email="other@example.com", password="pw2")])
    creds_store.save_ssi_token("other@example.com", "tok-o")
    creds_store.save_ssi_token("diver@example.com", "tok-d")
    tokens = {a.email: a.token for a in creds_store.load_credentials_model().get_ssi_accounts()}
    assert tokens == {"diver@example.com": "tok-d", "other@example.com": "tok-o"}
    creds_store.save_ssi_token("other@example.com", "")                 # a blank token forgets the stored one
    assert {a.email: a.token for a in creds_store.load_credentials_model().get_ssi_accounts()}["other@example.com"] == ""
    assert list(fake_keyring.store) == [(creds_store.SERVICE_NAME, "ssi_accounts")]


def test_ssi_client_token_lands_in_the_keychain_through_save_ssi_token(fake_keyring):
    """An `SsiClient` built straight from a keychain account keeps the
    token it is handed with that account through ``save_ssi_token`` as its
    ``on_token`` (the sync goes through credentials.json and
    sync_ssi_tokens_from_file instead)."""
    from src.core.config import SsiCredentials
    from src.core.services.ssi import HttpResponse, SsiClient

    creds_store.save_ssi_accounts([SsiCredentials(email="diver@example.com", password="pw1")])
    def transport(method, url, params, data, timeout, headers):
        return HttpResponse(200, b'{"authenticated": true, "token": "tok-9", "mid": "1"}')
    stored = creds_store.load_credentials_model().get_ssi_accounts()[0]
    client = SsiClient(stored.email, stored.password, stored.token or None, transport=transport,
                       on_token=lambda token: creds_store.save_ssi_token(stored.email, token), sleep=lambda s: None)
    assert client.authenticate() == "tok-9"
    assert creds_store.load_credentials_model().get_ssi_accounts()[0].token == "tok-9"


def test_end_operation_brings_a_myssi_token_from_the_credentials_file_into_the_keychain(fake_keyring, tmp_path, monkeypatch):
    """A sync builds its MySSI adapter from the materialised credentials.json
    (pairs.build_adapter) and the token MySSI hands out is written there;
    end_operation reads it back into the account's keychain entry before
    the file is cleared, like the Garmin token. Accounts not in the
    keychain, and a blank token, change nothing."""
    import src.core.config as config
    from src.core.config import ConfigManager, SsiCredentials
    creds_file = tmp_path / "credentials.json"
    monkeypatch.setattr(config, "CREDENTIALS_FILE", str(creds_file))
    creds_store.save_ssi_accounts([SsiCredentials(email="diver@example.com", password="pw1", token="tok-old"),
                                   SsiCredentials(email="other@example.com", password="pw2")])
    creds_store.begin_operation()
    assert creds_file.exists()
    materialised = ConfigManager.load_credentials(str(creds_file))
    assert [(a.email, a.token) for a in materialised.get_ssi_accounts()] == [("diver@example.com", "tok-old"), ("other@example.com", "")]
    # what pairs.build_adapter's hook does during the run
    assert ConfigManager.store_ssi_token("diver@example.com", "tok-new", str(creds_file)) is True
    assert ConfigManager.store_ssi_token("other@example.com", "", str(creds_file)) is False        # unchanged: not written
    assert ConfigManager.store_ssi_token("stranger@example.com", "tok-x", str(creds_file)) is False
    creds_store.end_operation()
    assert not creds_file.exists()
    tokens = {a.email: a.token for a in creds_store.load_credentials_model().get_ssi_accounts()}
    assert tokens == {"diver@example.com": "tok-new", "other@example.com": ""}
    # no file (nothing materialised, or already cleared): a no-op
    creds_store.sync_ssi_tokens_from_file()
    assert ConfigManager.store_ssi_token("diver@example.com", "tok-x", str(creds_file)) is False
