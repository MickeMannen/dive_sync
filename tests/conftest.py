import pytest


@pytest.fixture
def submersion_enabled(monkeypatch):
    """Submersion sync is switched off in the product (``SUBMERSION_ENABLED``);
    tests that exercise its code paths turn it back on."""
    from src.core import config
    monkeypatch.setattr(config, "SUBMERSION_ENABLED", True)


@pytest.fixture(autouse=True)
def isolated_run_history(monkeypatch, tmp_path):
    """Every sync run appends to the run history; keep that out of the repo."""
    from src.core import run_history
    monkeypatch.setattr(run_history, "HISTORY_FILE", str(tmp_path / "sync_history.jsonl"))


@pytest.fixture(autouse=True)
def isolated_credentials_file(monkeypatch, tmp_path):
    """``ConfigManager.load_credentials()`` writes an empty default file when
    none exists, so a test that reaches it through a default path (a
    scheduler run, the web app) left an empty ``./credentials.json`` in the
    repo root (found during plans/myssi-sync.md K2/K3). Every default-path
    read and write goes to the test's own folder; explicit paths are
    untouched, and tests that patch these methods themselves still win."""
    from src.core import config
    creds_file = str(tmp_path / "credentials.json")
    load, save, store = (config.ConfigManager.load_credentials, config.ConfigManager.save_credentials,
                         config.ConfigManager.store_ssi_token)
    monkeypatch.setattr(config, "CREDENTIALS_FILE", creds_file)
    monkeypatch.setattr(config.ConfigManager, "load_credentials", staticmethod(lambda path=creds_file: load(path)))
    monkeypatch.setattr(config.ConfigManager, "save_credentials",
                        staticmethod(lambda credentials, path=creds_file: save(credentials, path)))
    monkeypatch.setattr(config.ConfigManager, "store_ssi_token",
                        staticmethod(lambda email, token, path=creds_file: store(email, token, path)))


@pytest.fixture(autouse=True)
def no_live_shearwater(monkeypatch):
    """The owner's Mac has the Shearwater app installed, so its live
    dive_data.db would otherwise be auto-detected and turn up as a configured
    service in every test. Tests of the detection itself patch _users_dirs
    to a folder of their own."""
    import src.core.services.shearwater as shearwater
    monkeypatch.setattr(shearwater, "_users_dirs", lambda: [])
