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
def no_live_shearwater(monkeypatch):
    """The owner's Mac has the Shearwater app installed, so its live
    dive_data.db would otherwise be auto-detected and turn up as a configured
    service in every test. Tests of the detection itself patch _users_dirs
    to a folder of their own."""
    import src.core.services.shearwater as shearwater
    monkeypatch.setattr(shearwater, "_users_dirs", lambda: [])
