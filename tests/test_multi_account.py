"""Several accounts per service in the desktop app (rework.md E19): Subsurface
Cloud accounts, per-account dive caches, per-account sync state and the
account each page picks."""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from src.core import dive_cache
from src.core.config import CredentialsModel, DivelogsCredentials, GarminCredentials, SubsurfaceCredentials
from tests.test_desktop_credentials import fake_keyring, isolated_preferences  # noqa: F401  (fixtures; the second is autouse)
from tests.test_desktop_qt import qapp, scratch_data_dir, wait_until  # noqa: F401  (fixtures)


def _two_of_each():
    return CredentialsModel(
        garmin=[GarminCredentials(username="live@g", password="pw"), GarminCredentials(username="test@g", password="pw")],
        divelogs=[DivelogsCredentials(username="live", password="pw"), DivelogsCredentials(username="test", password="pw")],
        subsurface=[SubsurfaceCredentials(email="live@s.org", password="pw"),
                    SubsurfaceCredentials(email="test@s.org", password="pw")],
    )


# ------------------------------------------------------------------- core

def test_credentials_model_lists_subsurface_accounts_in_both_shapes():
    single = CredentialsModel(subsurface=SubsurfaceCredentials(email="a@x", password="pw"))
    assert [a.email for a in single.get_subsurface_accounts()] == ["a@x"]
    assert single.first_subsurface_account().email == "a@x"
    assert CredentialsModel().get_subsurface_accounts() == [] and not CredentialsModel().first_subsurface_account().email
    many = _two_of_each()
    assert [a.email for a in many.get_subsurface_accounts()] == ["live@s.org", "test@s.org"]
    assert "subsurface" in many.configured_services()
    # the list survives a save/load round trip through credentials.json
    assert CredentialsModel.model_validate(json.loads(many.model_dump_json())).get_subsurface_accounts()[1].email == "test@s.org"


def test_build_adapter_picks_the_named_subsurface_account(tmp_path):
    from src.core.config import ConfigManager, SettingsModel
    from src.core.pairs import build_adapter
    creds = tmp_path / "credentials.json"
    ConfigManager.save_credentials(_two_of_each(), str(creds))
    adapter = build_adapter("subsurface-cloud", SettingsModel(), credentials_path=str(creds), subsurface_username="test@s.org")
    assert adapter.email == "test@s.org"
    assert adapter.clone_dir.endswith(os.path.join("subsurface", "test@s.org", "cloud"))
    with pytest.raises(ValueError, match="Multiple Subsurface Cloud accounts"):
        build_adapter("subsurface-cloud", SettingsModel(), credentials_path=str(creds))
    with pytest.raises(ValueError, match="nobody@s.org"):
        build_adapter("subsurface-cloud", SettingsModel(), credentials_path=str(creds), subsurface_username="nobody@s.org")


def test_account_state_file_names(tmp_path):
    from src.core.pairs import account_state_file, legacy_state_file
    d = str(tmp_path)
    # Garmin -> Divelogs keeps the name it already had with several accounts
    assert account_state_file(d, "garmin", "g@x", "divelogs", "d") == os.path.join(d, "sync", "sync_state_g@x_d.json")
    assert account_state_file(d, "garmin", "g@x", "subsurface", "me@x.org") == \
        os.path.join(d, "sync", "sync_state_garmin-g@x_subsurface-me@x.org.json")
    assert account_state_file(d, "garmin", "g@x", "uddf", "") == os.path.join(d, "sync", "sync_state_garmin-g@x_uddf.json")
    assert account_state_file(d, "garmin", "a/b", "uddf", "") == os.path.join(d, "sync", "sync_state_garmin-a_b_uddf.json")
    assert legacy_state_file(d, "garmin", "divelogs") == os.path.join(d, "sync", "sync_state.json")
    assert legacy_state_file(d, "garmin", "subsurface") == os.path.join(d, "sync", "sync_state_garmin_subsurface.json")


def test_engine_keeps_state_per_account_combination_only_when_asked(tmp_path):
    from src.core.config import ConfigManager
    from src.core.pairs import engine_for
    settings, creds = str(tmp_path / "settings.json"), str(tmp_path / "credentials.json")
    ConfigManager.save_credentials(_two_of_each(), creds)
    kwargs = dict(settings_path=settings, credentials_path=creds)
    scoped = engine_for("garmin", "subsurface-cloud", garmin_username="test@g", subsurface_username="test@s.org",
                        account_scoped_state=True, **kwargs)
    assert os.path.basename(scoped.state_file) == "sync_state_garmin-test@g_subsurface-test@s.org.json"
    assert os.path.basename(scoped.conflicts_file) == "conflicts_garmin-test@g_subsurface-test@s.org.json"
    classic = engine_for("garmin", "divelogs", garmin_username="test@g", divelogs_username="test",
                         account_scoped_state=True, **kwargs)
    assert os.path.basename(classic.state_file) == "sync_state_test@g_test.json"
    # the web UI / Docker never ask, and keep today's names
    plain = engine_for("garmin", "subsurface-cloud", garmin_username="test@g", subsurface_username="test@s.org", **kwargs)
    assert os.path.basename(plain.state_file) == "sync_state_garmin_subsurface.json"


def test_scheduler_passes_subsurface_account_and_scoped_state(monkeypatch):
    from src.core import scheduler
    import src.core.pairs as pairs
    captured = {}

    class Engine:
        run_overrides = {}
        settings = None

        def run_sync(self, *a, **k):
            return {}

    def fake_engine_for(source, target, **kwargs):
        captured.update(kwargs)
        return Engine()
    monkeypatch.setattr(pairs, "engine_for", fake_engine_for)
    scheduler.run_sync_thread(True, {"source": "garmin", "target": "subsurface-cloud", "garmin_username": "test@g",
                                     "subsurface_username": "test@s.org", "account_scoped": True})
    assert captured["subsurface_username"] == "test@s.org" and captured["garmin_username"] == "test@g"
    assert captured["account_scoped_state"] is True


class _FakeCloud:
    def __init__(self, dives):
        self.dives, self.updated = dives, []

    def login(self):
        return True

    def fetch_dives(self):
        return self.dives

    def update_dive(self, external_id, dive):
        self.updated.append(external_id)
        return True

    def finish(self):
        pass


def _dive(external_id, when="2026-06-22T10:00:00"):
    return {"date_time": when, "external_ids": {"subsurface": external_id}, "max_depth": 10.0, "duration": 1800}


def test_subsurface_cache_is_kept_per_account(tmp_path, monkeypatch):
    base = str(tmp_path)
    adapters = {"live@s.org": _FakeCloud([_dive("same"), _dive("live-only")]),
                "test@s.org": _FakeCloud([_dive("same")])}
    used = []
    monkeypatch.setattr(dive_cache, "_unified_adapter", lambda service, username=None: used.append(username) or adapters[username])
    assert dive_cache.download_service_dives("subsurface", base_dir=base, username="live@s.org") == 2
    assert dive_cache.download_service_dives("subsurface", base_dir=base, username="test@s.org") == 1
    service_dir = os.path.join(base, "subsurface")
    assert sorted(os.listdir(service_dir)) == ["live@s.org", "test@s.org"]
    live = dive_cache.list_dives("subsurface", "live@s.org", base)
    test = dive_cache.list_dives("subsurface", "test@s.org", base)
    assert sorted(r["filename"] for r in live) == ["live-only.json", "same.json"]
    assert [r["filename"] for r in test] == ["same.json"]
    assert dive_cache.list_dives("subsurface", "nobody@s.org", base) == []

    # the same file name in two accounts: the update goes to the test account only
    path = dive_cache.update_dive_fields("subsurface", "same.json", "test@s.org", base_dir=base, buddy="Bo")
    assert path == os.path.join(service_dir, "test@s.org", "data", "same.json")
    used.clear()
    assert dive_cache.push_remote_update("subsurface", path)            # account read from the path
    assert used == ["test@s.org"] and adapters["test@s.org"].updated == ["same"] and adapters["live@s.org"].updated == []
    with open(os.path.join(service_dir, "live@s.org", "data", "same.json")) as f:
        assert not json.load(f).get("buddy")


def test_unified_adapter_maps_a_cache_folder_back_to_its_email(monkeypatch, tmp_path):
    import src.core.pairs as pairs
    from src.core.config import ConfigManager
    monkeypatch.setattr(ConfigManager, "load_credentials", staticmethod(lambda path=None: CredentialsModel(
        subsurface=[SubsurfaceCredentials(email="we ird@s.org", password="pw")])))
    monkeypatch.setattr(ConfigManager, "load_settings", staticmethod(lambda path=None: None))
    seen = {}
    monkeypatch.setattr(pairs, "build_adapter", lambda spec, settings, **k: seen.update(k) or "adapter")
    assert dive_cache.account_dir_name("we ird@s.org") == "we_ird@s.org"
    dive_cache._unified_adapter("subsurface", "we_ird@s.org")
    assert seen["subsurface_username"] == "we ird@s.org"


def test_garmin_listing_never_shows_another_accounts_dives(tmp_path):
    base = str(tmp_path)
    live = tmp_path / "garmin" / "live@g" / "data"
    live.mkdir(parents=True)
    (live / "1.json").write_text(json.dumps({"summary": {"activityId": 1, "startTimeLocal": "2026-06-22 10:00:00"}}))
    assert [r["filename"] for r in dive_cache.list_dives("garmin", "live@g", base)] == ["1.json"]
    # an account with nothing downloaded yet lists nothing - not live@g's dives
    assert dive_cache.list_dives("garmin", "test@g", base) == []


# ---------------------------------------------------------------- desktop

def test_keychain_moves_the_single_subsurface_account_into_the_list(fake_keyring):
    import desktop.credentials as creds_store
    fake_keyring.store[(creds_store.SERVICE_NAME, "subsurface")] = json.dumps({"email": "me@x.org", "password": "pw"})
    model = creds_store.load_credentials_model()
    assert [(a.email, a.password) for a in model.get_subsurface_accounts()] == [("me@x.org", "pw")]
    assert (creds_store.SERVICE_NAME, "subsurface") not in fake_keyring.store
    assert json.loads(fake_keyring.store[(creds_store.SERVICE_NAME, "subsurface_accounts")]) == [{"username": "me@x.org", "password": "pw"}]
    # and saving the whole model keeps every account
    creds_store.save_credentials_model(_two_of_each())
    assert [a.email for a in creds_store.load_credentials_model().get_subsurface_accounts()] == ["live@s.org", "test@s.org"]


def test_selected_account_is_remembered_per_page(fake_keyring, scratch_data_dir):
    import desktop.credentials as creds_store
    from desktop import accounts
    creds_store.save_credentials_model(_two_of_each())
    assert accounts.selected("sync", "garmin") == "live@g"          # the first until something is picked
    accounts.select("sync", "garmin", "test@g")
    assert accounts.selected("sync", "garmin") == "test@g" and accounts.selected("dives", "garmin") == "live@g"
    accounts.select("dives", "subsurface", "gone@s.org")             # an account since removed
    assert accounts.selected("dives", "subsurface") == "live@s.org"
    assert accounts.sync_selection() == {"garmin": "test@g", "divelogs": "live", "subsurface": "live@s.org"}
    assert accounts.accounts_for_spec("subsurface-cloud") == ["live@s.org", "test@s.org"]
    assert accounts.accounts_for_spec("subsurface:/some/checkout") == [""]
    assert accounts.accounts_for_spec("uddf:x.uddf") == [""]


def test_dives_page_lists_and_saves_its_own_account(qapp, fake_keyring, scratch_data_dir, monkeypatch):
    import desktop.credentials as creds_store
    from desktop.controllers.dives import DivesController
    from src.core import scheduler
    creds_store.save_credentials_model(_two_of_each())
    listed = []
    monkeypatch.setattr(dive_cache, "list_dives", lambda service, username=None, *a: listed.append(username) or
                        [{"date": "2026-06-22", "time": "10:00:00", "date_time": "2026-06-22 10:00:00",
                          "filename": f"{username}.json"}])
    monkeypatch.setattr(dive_cache, "get_samples", lambda *a, **k: [])
    c = DivesController("subsurface")
    assert c.accounts == ["live@s.org", "test@s.org"] and c.account == "live@s.org"
    c.load()
    assert listed[-1] == "live@s.org"
    c.setAccount("test@s.org")
    assert c.account == "test@s.org" and listed[-1] == "test@s.org"
    assert DivesController("subsurface").account == "test@s.org"      # remembered

    # a staged edit pins the page to its account until it is saved or discarded
    c.stage("test@s.org.json", {"buddy": "x"})
    c.setAccount("live@s.org")
    assert c.account == "test@s.org" and "Save or discard" in c.listStatus

    # a refresh downloads only this page's account
    downloads = {}
    monkeypatch.setattr(scheduler, "run_download_thread",
                        lambda overwrite, base_dir=None, services=None, refresh_fits=False, accounts=None:
                        downloads.update(services=services, accounts=accounts))
    c.discard("test@s.org.json")
    c.refresh()
    assert wait_until(qapp, lambda: not c.busy)
    assert downloads == {"services": ["subsurface"], "accounts": {"subsurface": "test@s.org"}}


def test_sync_page_runs_and_downloads_as_the_picked_accounts(qapp, fake_keyring, scratch_data_dir, monkeypatch):
    import desktop.credentials as creds_store
    from desktop import logging_bridge
    from desktop.controllers.sync import SyncController
    from src.core import scheduler
    creds_store.save_credentials_model(_two_of_each())
    c = SyncController(logging_bridge.install())
    assert c.subsurfaceAccounts == ["live@s.org", "test@s.org"]
    c.setSelectedAccount("garmin", "test@g")
    c.setSelectedAccount("subsurface", "test@s.org")
    assert c.selectedAccounts == {"garmin": "test@g", "divelogs": "live", "subsurface": "test@s.org"}
    seen = {}
    monkeypatch.setattr(scheduler, "run_sync_thread", lambda dry_run, custom_settings=None: seen.update(custom=custom_settings))
    monkeypatch.setattr(scheduler, "is_sync_running", False)
    c.runSyncBetween(True, "garmin", "subsurface-cloud", True, True, "", "", True, False)
    assert wait_until(qapp, lambda: not c.running)
    custom = seen["custom"]
    assert (custom["garmin_username"], custom["subsurface_username"]) == ("test@g", "test@s.org")
    assert custom["account_scoped"] is True

    downloads = {}
    monkeypatch.setattr(scheduler, "run_download_thread",
                        lambda overwrite, base_dir=None, services=None, accounts=None: downloads.update(accounts=accounts))
    monkeypatch.setattr(scheduler, "is_download_running", False)
    c.download(False, "")
    assert wait_until(qapp, lambda: not c.running)
    assert downloads["accounts"] == {"garmin": "test@g", "divelogs": "live", "subsurface": "test@s.org"}


def test_account_pickers_show_in_the_app_with_two_accounts(qapp, fake_keyring, scratch_data_dir, monkeypatch):
    from PySide6.QtCore import QObject
    import desktop.credentials as creds_store
    from desktop import app as desktop_app
    from desktop import logging_bridge
    creds_store.save_credentials_model(CredentialsModel(
        garmin=[GarminCredentials(username="g@x", password="pw")],
        subsurface=[SubsurfaceCredentials(email="live@s.org", password="pw"),
                    SubsurfaceCredentials(email="test@s.org", password="pw")]))
    monkeypatch.setattr(dive_cache, "list_dives", lambda *a, **k: [])
    warnings = []
    controllers = desktop_app.build_controllers(logging_bridge.install())
    engine = desktop_app.create_engine(controllers, "Sync", on_warnings=lambda w: warnings.extend(w))
    root = engine.rootObjects()[0]
    assert root.findChild(QObject, "syncSubsurfaceAccount").property("visible") is True
    assert root.findChild(QObject, "syncGarminAccount").property("visible") is False    # one account: nothing to pick
    assert not [w for w in warnings if "AccountPicker" in w.toString() or "SyncPage" in w.toString()]
