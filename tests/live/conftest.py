"""Opt-in live tests against real test accounts.

They run only when ``DIVE_SYNC_LIVE_DATA_DIR`` points at a data directory
holding a ``credentials.json`` for the *test* accounts (never the real ones).
Everything in this package is read-only unless a test says otherwise in its
name and docstring. The normal suite skips the whole directory.

    DIVE_SYNC_LIVE_DATA_DIR=~/.dive_sync_test ./run_tests.sh tests/live
"""
import os

import pytest

LIVE_ENV = "DIVE_SYNC_LIVE_DATA_DIR"


def pytest_collection_modifyitems(config, items):
    if os.environ.get(LIVE_ENV):
        return
    skip = pytest.mark.skip(reason=f"live tests need {LIVE_ENV} to point at a test-account data dir")
    for item in items:
        if "/tests/live/" in str(item.fspath).replace(os.sep, "/") + "/":
            item.add_marker(skip)


@pytest.fixture(scope="session")
def live_data_dir():
    raw = os.environ.get(LIVE_ENV)
    if not raw:
        pytest.skip(f"{LIVE_ENV} not set")
    path = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isfile(os.path.join(path, "credentials.json")):
        pytest.fail(f"{LIVE_ENV}={raw} has no credentials.json")
    previous = os.environ.get("DATA_DIR")
    # SyncEngine resolves the Garmin token dir against DATA_DIR at construction.
    os.environ["DATA_DIR"] = path
    yield path
    if previous is None:
        os.environ.pop("DATA_DIR", None)
    else:
        os.environ["DATA_DIR"] = previous


@pytest.fixture(scope="session")
def live_credentials(live_data_dir):
    from src.core.config import ConfigManager
    return ConfigManager.load_credentials(os.path.join(live_data_dir, "credentials.json"))


@pytest.fixture(scope="session")
def live_engine(live_data_dir):
    """One engine, hence one Garmin login, for the whole session."""
    from src.core.sync_engine import SyncEngine
    engine = SyncEngine(settings_path=os.path.join(live_data_dir, "settings.json"),
                        credentials_path=os.path.join(live_data_dir, "credentials.json"))
    assert engine.source.login(), "Garmin login failed"
    assert engine.target.login(), "Divelogs login failed"
    return engine
