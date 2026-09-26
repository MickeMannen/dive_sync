"""The data folder layout (rework.md E21, src/core/layout.py)."""
import os

from src.core import layout


def test_every_account_keeps_its_files_in_one_folder(tmp_path):
    base = str(tmp_path)
    assert layout.dives_dir("garmin", "me@x.org", base) == os.path.join(base, "garmin", "me@x.org", "data")
    assert layout.garmin_fit_dir("me@x.org", base) == os.path.join(base, "garmin", "me@x.org", "fit")
    assert layout.garmin_token_dir("me@x.org", "", base) == os.path.join(base, "garmin", "me@x.org", "tokens")
    assert layout.subsurface_cloud_dir("me@x.org", base) == os.path.join(base, "subsurface", "me@x.org", "cloud")
    # a side without accounts
    assert layout.dives_dir("submersion", None, base) == os.path.join(base, "submersion", "default", "data")


def test_account_folder_names_stay_inside_the_service_folder():
    assert layout.account_dir_name("we ird/../x@s.org") == "we_ird_.._x@s.org"
    assert layout.account_dir_name("..") == "default"
    assert layout.account_dir_name("") == "default"


def test_token_dir_setting(tmp_path):
    base = str(tmp_path)
    own = os.path.join(base, "garmin", "u", "tokens")
    # blank and the old shared default both mean the account's own folder
    assert layout.garmin_token_dir("u", "", base) == own
    assert layout.garmin_token_dir("u", "tokens/garmin", base) == own
    assert layout.garmin_token_dir("u", "tokens/garmin/", base) == own
    assert layout.garmin_token_dir("u", "custom", base) == os.path.join(base, "custom")
    assert layout.garmin_token_dir("u", "/abs/tokens", base) == "/abs/tokens"


def test_account_of_path_and_every_accounts_folder(tmp_path):
    base = str(tmp_path)
    for account in ("a@x", "b@x"):
        os.makedirs(layout.dives_dir("divelogs", account, base))
    os.makedirs(os.path.join(base, "divelogs", "stray"))          # no data folder: not an account cache
    assert [name for name, _ in layout.all_dives_dirs("divelogs", base)] == ["a@x", "b@x"]
    assert layout.account_of_path("divelogs", os.path.join(base, "divelogs", "a@x", "data", "1.json")) == "a@x"
    assert layout.account_of_path("divelogs", os.path.join(base, "divelogs", "default", "data", "1.json")) is None
    assert layout.account_of_path("garmin", os.path.join(base, "divelogs", "a@x", "data", "1.json")) is None


def test_engine_keeps_state_in_sync_and_backups_in_the_root(tmp_path):
    from src.core.sync_engine import SyncEngine
    (tmp_path / "credentials.json").write_text("{}")
    engine = SyncEngine(settings_path=str(tmp_path / "settings.json"), credentials_path=str(tmp_path / "credentials.json"))
    assert engine.state_file == os.path.join(str(tmp_path), "sync", "sync_state.json")
    assert engine.conflicts_file == os.path.join(str(tmp_path), "sync", "conflicts.json")
    assert engine.data_home == str(tmp_path)
    engine.save_state(links={"a": "b"})
    assert os.path.exists(engine.state_file)


def test_desktop_data_folder_per_platform(monkeypatch):
    import platformdirs
    from desktop import paths
    seen = []
    monkeypatch.setattr(platformdirs, "user_data_dir", lambda *a, **k: seen.append((a, k)) or "x")
    for platform in ("darwin", "win32", "linux"):
        paths._platform_data_dir(platform)
    assert seen == [(("org.christersson.dive_sync",), {"appauthor": False}),
                    (("DiveSync", "Christersson"), {"roaming": False}),
                    (("dive-sync",), {"appauthor": False})]
