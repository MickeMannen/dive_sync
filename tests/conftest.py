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
