"""Sync pairs (rework.md F3): service specs, default boards per pair, engines
built from specs, the CLI --source/--target/--pair options and cron jobs
naming a pair."""
import json
import os
import shutil
import subprocess
import sys

import pytest

from src.core.config import ConfigManager, CronJobModel, SettingsModel, SyncFilters, SyncPairModel
from src.core.pairs import build_adapter, default_links_for, engine_for, engine_for_pair, find_pair, parse_service_spec, service_id_of

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "data", "subsurface_cloud")


def test_parse_service_spec():
    assert parse_service_spec("garmin") == ("garmin", None)
    assert parse_service_spec(" UDDF:out.uddf ") == ("uddf", "out.uddf")
    assert parse_service_spec("subsurface:/abs/dir") == ("subsurface", "/abs/dir")
    for bad in ("nope", "uddf", "garmin:x"):
        with pytest.raises(ValueError):
            parse_service_spec(bad)


def test_default_links_per_pair():
    ids = [l.id for l in default_links_for("garmin", "divelogs")]
    assert "site" in ids and "dive_number" not in ids
    ids = {l.id: l for l in default_links_for("garmin", "subsurface")}
    assert ids["dive_number"].match_order == 1 and ids["buddy"].source == ["garmin.buddy"] and ids["buddy"].target == "subsurface.buddy"
    assert "dive_number" not in {l.id for l in default_links_for("divelogs", "uddf")}


def test_build_adapter_from_specs(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = SettingsModel()
    a = build_adapter("uddf:log.uddf", settings)
    assert a.service_id == "uddf" and a.path == str(tmp_path / "log.uddf")
    b = build_adapter("subsurface:" + str(tmp_path / "repo"), settings)
    assert b.service_id == "subsurface" and b.path == str(tmp_path / "repo")
    g = build_adapter("garmin", settings, mock_data_dir=str(tmp_path))
    assert g.service_id == "garmin" and type(g).__name__ == "LocalMockGarminAdapter"
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"garmin": {"username": "u", "password": "p"}, "divelogs": [
        {"username": "a", "password": "p"}, {"username": "b", "password": "p"}]}))
    monkeypatch.setattr("src.core.services.garmin.Garmin", lambda *a, **k: object())
    real = build_adapter("garmin", settings, credentials_path=str(creds))
    assert real.username == "u" and real.token_dir == str(tmp_path / "tokens" / "garmin")
    with pytest.raises(ValueError, match="Multiple Divelogs"):
        build_adapter("divelogs", settings, credentials_path=str(creds))
    assert build_adapter("divelogs", settings, credentials_path=str(creds), divelogs_username="b").username == "b"


def test_engine_for_specs_and_pairs(tmp_path):
    spath = str(tmp_path / "settings.json")
    pair = SyncPairModel(id="to-file", source="garmin", target="uddf:out.uddf", directionality="to_uddf",
                         grace_window_minutes=5)
    ConfigManager.save_settings(SettingsModel(sync_filters=SyncFilters(only_new=False), sync_pairs=[pair]), spath)
    os.environ["DATA_DIR"] = str(tmp_path)
    try:
        engine = engine_for_pair(find_pair(ConfigManager.load_settings(spath), "to-file"), settings_path=spath,
                                 credentials_path=str(tmp_path / "c.json"), mock_data_dir=str(tmp_path))
        assert (engine.source_id, engine.target_id) == ("garmin", "uddf")
        # rework.md G1: the engine reads direction, board and options from its pair
        assert engine.pair_id == "to-file" and engine.direction == "to_uddf" and engine.settings.grace_window_minutes == 5
        assert [l.id for l in engine.field_links] == [l.id for l in default_links_for("garmin", "uddf")]
        res = engine.run_sync(dry_run=True, **engine.run_overrides)
        assert res["source"] == "garmin" and res["directionality"] == "to_uddf"
        with pytest.raises(ValueError, match="No sync pair"):
            find_pair(ConfigManager.load_settings(spath), "missing")
        # the classic pair keeps the classic constructor and no overrides
        classic = engine_for("garmin", "divelogs", settings_path=spath, credentials_path=str(tmp_path / "c.json"),
                             mock_data_dir=str(tmp_path))
        assert classic.run_overrides == {} and classic.garmin is classic.source
        with pytest.raises(ValueError, match="itself"):
            engine_for("uddf:a.uddf", "uddf:b.uddf", settings_path=spath, credentials_path=str(tmp_path / "c.json"))
    finally:
        os.environ.pop("DATA_DIR", None)


def test_cron_job_names_a_pair(tmp_path, monkeypatch):
    from src.core import scheduler
    captured = {}

    class FakeEngine:
        run_overrides = {"direction_override": "to_uddf", "field_links_override": []}

        def run_sync(self, dry_run=False, **kw):
            captured.update(kw, dry_run=dry_run)
            return {"ok": True}

    monkeypatch.setattr("src.core.pairs.engine_for_pair", lambda pair, **kw: FakeEngine())
    monkeypatch.setattr(ConfigManager, "load_settings", lambda path=None: SettingsModel(
        sync_pairs=[SyncPairModel(id="p1", source="garmin", target="uddf:x.uddf")]))
    scheduler.run_sync_thread(False, CronJobModel(id="j", pair="p1", only_new=False).model_dump())
    assert scheduler.last_sync_results["j"] == {"ok": True}
    assert captured["direction_override"] == "to_uddf"      # no direction on the job -> the pair's saved one
    assert captured["only_new_override"] is False and captured["dry_run"] is False
    # rework.md G0: a job that names a direction is that directed run (a
    # two-way schedule is two such jobs on the same pair)
    captured.clear()
    scheduler.run_sync_thread(False, CronJobModel(id="j", pair="p1", directionality="to_garmin").model_dump())
    assert captured["direction_override"] == "to_garmin"
    # and a job written when 'bidirectional' still existed follows the pair
    assert CronJobModel(id="old", pair="p1", directionality="bidirectional").directionality is None


@pytest.mark.skipif(not os.path.isdir(FIXTURE), reason="subsurface fixture missing")
def test_cli_source_target_offline(tmp_path):
    """garmin (mock) -> subsurface:<fixture copy> and -> uddf:<file> through sync.py."""
    data_dir = tmp_path / "data"
    mock = tmp_path / "mock"
    (mock / "garmin").mkdir(parents=True)
    (mock / "garmin" / "1.json").write_text(json.dumps({"summary": {
        "activityId": "10001", "activityName": "Zenobia", "startTimeLocal": "2026-06-22 10:00:00",
        "metadataDTO": {"diveNumber": 1}, "summaryDTO": {"duration": 2700, "maxDepth": 18.2, "startLatitude": 34.887, "startLongitude": 33.657},
        "description": "garmin notes"}, "details": {}}))
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    data_dir.mkdir()
    (data_dir / "settings.json").write_text(json.dumps({"sync_filters": {"only_new": False}, "api_cooldown_seconds": 0, "sync_pairs": [
        {"id": "ssrf", "source": "garmin", "target": f"subsurface:{repo}", "directionality": "to_subsurface"}]}))
    env = dict(os.environ, DATA_DIR=str(data_dir), PYTHONPATH=ROOT)

    def run(*args):
        return subprocess.run([sys.executable, os.path.join(ROOT, "sync.py"), "--mock-data-dir", str(mock), *args],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)

    res = run("--pair", "ssrf", "--show-mapping")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "garmin -> subsurface" in res.stdout and "garmin.buddy" in res.stdout and "subsurface.buddy" in res.stdout

    res = run("--pair", "ssrf", "--direction", "to_subsurface", "--full-sync")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Uploaded to Subsurface: 1" in res.stdout
    res = run("--pair", "ssrf", "--direction", "sideways", "--dry-run")
    assert res.returncode == 2 and "to_<service>" in res.stderr
    assert os.path.isdir(repo / "2026" / "06" / "22-Mon-10=00=00")
    assert "garmin notes" in open(repo / "2026" / "06" / "22-Mon-10=00=00" / "Dive-1").read()
    state = json.load(open(data_dir / "sync_state_garmin_subsurface.json"))
    # the link keeps the dive's directory: it survives a renumbering (Dive-N changes)
    assert state["links"]["10001"] == "2026/06/22-Mon-10=00=00"

    res = run("--source", "garmin", "--target", "uddf:out.uddf", "--full-sync")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Uploaded to UDDF file: 1" in res.stdout and (data_dir / "out.uddf").exists()
    res = run("--source", "garmin", "--target", "uddf:out.uddf", "--full-sync")
    assert "Uploaded to UDDF file: 0" in res.stdout   # remembered pair, no duplicate


def test_submersion_spec_is_refused_while_disabled():
    with pytest.raises(ValueError, match="disabled"):
        parse_service_spec("submersion")


def test_submersion_spec_builds_adapter(tmp_path, monkeypatch, submersion_enabled):
    import json
    from src.core.config import SettingsModel
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"submersion": {"store_type": "folder", "folder_path": str(tmp_path / "store")}}))
    a = build_adapter("submersion", SettingsModel(), credentials_path=str(creds))
    assert a.service_id == "submersion" and a.config.folder_path == str(tmp_path / "store")
    assert service_id_of("submersion") == "submersion"
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    with pytest.raises(ValueError, match="not configured"):
        build_adapter("submersion", SettingsModel(), credentials_path=str(empty))


def test_every_shipped_default_board_is_valid():
    """No default link may name a field its service lacks (Subsurface has no
    visibility, UDDF no weight): the mapping board would refuse to save it."""
    from itertools import combinations
    from src.core.fields import build_catalog
    from src.core.pairs import field_catalog_of
    from src.core.templates import validate_links
    for a, b in combinations(["garmin", "divelogs", "subsurface", "uddf", "submersion"], 2):
        catalog = build_catalog(field_catalog_of(a), field_catalog_of(b))
        assert validate_links(default_links_for(a, b), catalog) == [], (a, b)


def test_board_pairs_offer_configured_combinations_not_saved_yet():
    from src.core.pairs import board_pairs
    settings = SettingsModel()
    settings.sync_pairs.append(SyncPairModel(id="g2s", source="garmin", target="subsurface-cloud"))
    boards = board_pairs(settings, ["garmin", "divelogs", "subsurface"])
    assert [(b["id"], b["source"], b["target"], b["saved"]) for b in boards] == [
        ("garmin_divelogs", "garmin", "divelogs", True),
        ("g2s", "garmin", "subsurface-cloud", True),                    # already saved: not offered twice
        ("divelogs_subsurface", "divelogs", "subsurface-cloud", False),
    ]
