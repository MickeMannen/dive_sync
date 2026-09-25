import pytest


@pytest.fixture
def submersion_enabled(monkeypatch):
    """Submersion sync is switched off in the product (``SUBMERSION_ENABLED``);
    tests that exercise its code paths turn it back on."""
    from src.core import config
    monkeypatch.setattr(config, "SUBMERSION_ENABLED", True)
