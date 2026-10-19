"""CLI flags added in rework.md C5/C9, run as subprocesses against a scratch
DATA_DIR and the offline mock adapters."""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(tmp_path, *args, stdin=""):
    env = dict(os.environ, DATA_DIR=str(tmp_path), PYTHONPATH=ROOT)
    return subprocess.run([sys.executable, os.path.join(ROOT, "sync.py"), "--mock-data-dir", str(tmp_path / "mock"), *args],
                          cwd=ROOT, env=env, capture_output=True, text=True, input=stdin, timeout=60)


def _write_mock(tmp_path, garmin=None, divelogs=None):
    for service, items in (("garmin", garmin or []), ("divelogs", divelogs or [])):
        d = tmp_path / "mock" / service / "default" / "data"
        d.mkdir(parents=True, exist_ok=True)
        for i, item in enumerate(items):
            # the mock adapters name files by dive number
            number = item.get("divenumber") or (item.get("summary") or {}).get("metadataDTO", {}).get("diveNumber") or i
            (d / f"{number}.json").write_text(json.dumps(item))


G1 = {"summary": {"activityId": "10001", "activityName": "Wreck", "startTimeLocal": "2026-06-22 10:00:00",
                  "metadataDTO": {"diveNumber": 1}, "summaryDTO": {"duration": 2700, "maxDepth": 18.2},
                  "description": "garmin notes"}, "details": {}}
D1 = {"id": "50001", "divenumber": 1, "date": "2026-06-22", "time": "10:02:00", "duration": 2700, "maxdepth": 18.2,
      "location": "Malmö", "divesite": "Ön", "notes": "divelogs notes"}


def test_show_mapping(tmp_path):
    res = _run(tmp_path, "--show-mapping")
    assert res.returncode == 0, res.stderr
    assert "Mapping board for garmin -> divelogs" in res.stdout
    assert "activity_name" in res.stdout and "prefer_non_empty" in res.stdout and "{divelogs.location}" in res.stdout


def test_export_validate_import_profile(tmp_path):
    out = tmp_path / "profile.json"
    res = _run(tmp_path, "--export-profile", str(out))
    assert res.returncode == 0 and out.exists()
    data = json.loads(out.read_text())
    assert data["dive_sync_profile"] == 2 and "password" not in out.read_text()
    assert data["sync_pairs"][0]["id"] == "garmin_divelogs" and "field_links" not in data

    # a version-1 style edit (top-level field_links) still imports onto the garmin_divelogs pair
    data["grace_window_minutes"] = 45
    data["field_links"] = [{"id": "buddy", "source": ["garmin.buddy"], "target": "divelogs.buddy", "conflict": "manual"},
                           {"id": "future", "source": ["garmin.rating"], "target": "divelogs.rating"}]
    out.write_text(json.dumps(data))
    res = _run(tmp_path, "--validate-profile", str(out))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "grace_window_minutes: 15 -> 45" in res.stdout and "skipped links (unknown fields): future" in res.stdout
    assert "Profile is valid." in res.stdout

    res = _run(tmp_path, "--import-profile", str(out), stdin="n\n")
    assert res.returncode == 1 and "Import cancelled" in res.stdout
    res = _run(tmp_path, "--import-profile", str(out), "--yes")
    assert res.returncode == 0, res.stdout + res.stderr
    saved = json.loads((tmp_path / "settings.json").read_text())
    rules = saved["sync_pairs"][0]["rules"]
    assert saved["grace_window_minutes"] == 45
    assert [r["id"] for r in rules["divelogs"]] == ["buddy"] and [r["id"] for r in rules["garmin"]] == ["buddy"]

    data["dive_sync_profile"] = 99
    out.write_text(json.dumps(data))
    res = _run(tmp_path, "--validate-profile", str(out))
    assert res.returncode == 1 and "newer Dive Sync" in res.stdout
    data["dive_sync_profile"] = 2
    data["field_links"] = [{"id": "bad", "source": ["garmin.buddy"], "target": "divelogs.max_depth"}]
    out.write_text(json.dumps(data))
    res = _run(tmp_path, "--import-profile", str(out), "--yes")
    assert res.returncode == 1 and "cannot link garmin.buddy" in res.stdout


def test_conflicts_list_and_resolve(tmp_path):
    _write_mock(tmp_path, [G1], [D1])
    settings = {"sync_filters": {"only_new": False}, "api_cooldown_seconds": 0,
                "field_links": [{"id": "notes", "source": ["garmin.notes"], "target": "divelogs.notes", "conflict": "manual"}]}
    (tmp_path / "settings.json").write_text(json.dumps(settings))

    res = _run(tmp_path, "--list-conflicts")
    assert res.returncode == 0 and "No conflicts waiting" in res.stdout

    res = _run(tmp_path, "--full-sync")
    assert res.returncode == 0, res.stderr
    assert "Conflicts for manual resolution: 1" in res.stdout

    res = _run(tmp_path, "--list-conflicts")
    assert "notes" in res.stdout and "'garmin notes'" in res.stdout and "'divelogs notes'" in res.stdout
    import re
    conflict_id = next(m.group(1) for m in (re.match(r"^([0-9a-f]{10})\s", l) for l in res.stdout.splitlines()) if m)

    res = _run(tmp_path, "--resolve", conflict_id, "source")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Resolved conflict" in res.stdout
    updated = json.loads((tmp_path / "mock" / "divelogs" / "default" / "data" / "1.json").read_text())
    assert updated["notes"] == "garmin notes"
    res = _run(tmp_path, "--list-conflicts")
    assert "No conflicts waiting" in res.stdout


def test_test_mapping_is_read_only(tmp_path):
    _write_mock(tmp_path, [G1], [D1])
    (tmp_path / "settings.json").write_text(json.dumps({"api_cooldown_seconds": 0}))
    before = (tmp_path / "mock" / "divelogs" / "default" / "data" / "1.json").read_text()
    res = _run(tmp_path, "--test-mapping")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "1 matched" in res.stdout and "kept" in res.stdout and "Read-only" in res.stdout
    assert (tmp_path / "mock" / "divelogs" / "default" / "data" / "1.json").read_text() == before
    assert not (tmp_path / "mock" / "sync" / "sync_state.json").exists()


# --- --units (plans/units.md J6) ----------------------------------------------

G_DEEP = {"summary": dict(G1["summary"], summaryDTO={"duration": 2700, "maxDepth": 30.2}), "details": {}}
D_DEEP = dict(D1, maxdepth=30.5)


def _table_lines(stdout):
    """stdout without the timestamped log lines (the table and the notes)."""
    import re
    return [l for l in stdout.splitlines() if not re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ \[", l)]


def test_units_flag_converts_the_conflict_list_and_the_test_mapping(tmp_path):
    """``--units imperial`` shows a depth conflict and a depth rule's values
    in feet with the label, found from the catalogue; text is left alone;
    no flag (and ``--units metric``) prints exactly what it printed before."""
    _write_mock(tmp_path, [G_DEEP], [D_DEEP])
    settings = {"sync_filters": {"only_new": False}, "api_cooldown_seconds": 0,
                "field_links": [{"id": "notes", "source": ["garmin.notes"], "target": "divelogs.notes", "conflict": "manual"},
                                {"id": "depth", "source": ["garmin.max_depth"], "target": "divelogs.max_depth", "conflict": "manual"}]}
    (tmp_path / "settings.json").write_text(json.dumps(settings))

    # the rehearsal, before anything is written
    res = _run(tmp_path, "--test-mapping")
    assert res.returncode == 0, res.stdout + res.stderr
    depth_row = next(l for l in _table_lines(res.stdout) if "garmin.max_depth" in l)
    assert "30.2" in depth_row and "30.5" in depth_row and "ft" not in depth_row
    # the engine's own log line about the conflict prints the recorded values
    assert "(garmin.max_depth=30.2, divelogs.max_depth=30.5)" in res.stdout
    assert _table_lines(_run(tmp_path, "--test-mapping", "--units", "metric").stdout) == _table_lines(res.stdout)
    res = _run(tmp_path, "--test-mapping", "--units", "imperial")
    assert res.returncode == 0, res.stdout + res.stderr
    depth_row = next(l for l in _table_lines(res.stdout) if "garmin.max_depth" in l)
    assert "99 ft" in depth_row and "100 ft" in depth_row and "30.2" not in depth_row
    notes_row = next(l for l in _table_lines(res.stdout) if "garmin.notes" in l)
    assert "garmin notes" in notes_row and "divelogs notes" in notes_row
    assert "Read-only" in res.stdout
    # ... and so does the log line, with the flag
    assert "(garmin.max_depth='99 ft', divelogs.max_depth='100 ft')" in res.stdout

    res = _run(tmp_path, "--full-sync")
    assert res.returncode == 0, res.stderr
    assert "Conflicts for manual resolution: 2" in res.stdout
    assert "(garmin.max_depth=30.2, divelogs.max_depth=30.5)" in res.stdout

    metric = _run(tmp_path, "--list-conflicts")
    assert metric.returncode == 0, metric.stderr
    assert "garmin.max_depth=30.2" in metric.stdout and "divelogs.max_depth=30.5" in metric.stdout
    assert "garmin.notes='garmin notes'" in metric.stdout
    # the default is metric, and metric is byte-identical to the unflagged run
    assert _table_lines(_run(tmp_path, "--list-conflicts", "--units", "metric").stdout) == _table_lines(metric.stdout)

    imperial = _run(tmp_path, "--list-conflicts", "--units", "imperial")
    assert imperial.returncode == 0, imperial.stderr
    assert "garmin.max_depth=99 ft" in imperial.stdout and "divelogs.max_depth=100 ft" in imperial.stdout
    assert "30.2" not in imperial.stdout
    assert "garmin.notes='garmin notes'" in imperial.stdout and "divelogs.notes='divelogs notes'" in imperial.stdout
    assert "Resolve with: python sync.py --resolve <id> source|target" in imperial.stdout
    # the flag changes nothing on disk: the conflicts file still holds metres
    stored = json.loads(next((tmp_path / "mock" / "sync").glob("conflicts*.json")).read_text())
    assert sorted(c["source_value"] for c in stored["conflicts"] if c["link_id"] == "depth") == [30.2]


def test_units_flag_rejects_unknown_values(tmp_path):
    res = _run(tmp_path, "--units", "bogus", "--list-conflicts")
    assert res.returncode == 2 and "--units" in res.stderr and "invalid choice" in res.stderr
    res = _run(tmp_path, "--help")
    assert res.returncode == 0 and "--units {metric,imperial}" in res.stdout and "imperial (ft, psi" in res.stdout


def _write_ssi_credentials(tmp_path, emails):
    """A credentials.json in the scratch DATA_DIR with MySSI accounts only
    (plans/myssi-sync.md K5). Nothing here talks to MySSI: building the
    adapter needs no network, and --show-mapping reads no dives."""
    (tmp_path / "credentials.json").write_text(json.dumps({"ssi": [{"email": e, "password": "pw"} for e in emails]}))


def test_myssi_is_a_side_and_the_account_is_picked_by_spec_or_flag(tmp_path):
    _write_ssi_credentials(tmp_path, ["one@example.com", "two@example.com"])
    # two accounts and no pick: the run stops and names the flag
    res = _run(tmp_path, "--source", "garmin", "--target", "ssi", "--show-mapping")
    assert res.returncode == 1
    assert "Multiple MySSI accounts configured. Please specify --ssi-account" in res.stdout
    # --ssi-account picks one; the board is the Garmin -> MySSI default (K1: no dive-number rule)
    res = _run(tmp_path, "--source", "garmin", "--target", "ssi", "--ssi-account", "two@example.com", "--show-mapping")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Mapping board for garmin -> ssi" in res.stdout and "ssi.notes" in res.stdout
    assert "Match keys: none (start time only)" in res.stdout
    # the spec's argument is the same pick, either side
    res = _run(tmp_path, "--source", "ssi:one@example.com", "--target", "garmin", "--show-mapping")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Mapping board for ssi -> garmin" in res.stdout
    # an unknown account is an error, not a silent fallback
    res = _run(tmp_path, "--source", "garmin", "--target", "ssi", "--ssi-account", "nobody@example.com", "--show-mapping")
    assert res.returncode == 1 and "No MySSI account configured matching username: nobody@example.com" in res.stdout


def test_myssi_account_flag_reaches_a_saved_pair(tmp_path):
    _write_ssi_credentials(tmp_path, ["one@example.com", "two@example.com"])
    (tmp_path / "settings.json").write_text(json.dumps({"settings_version": 2, "sync_pairs": [
        {"id": "garmin_divelogs", "source": "garmin", "target": "divelogs"},
        {"id": "g2s", "source": "garmin", "target": "ssi", "directionality": "to_ssi"}]}))
    res = _run(tmp_path, "--pair", "g2s", "--show-mapping")
    assert res.returncode == 1 and "--ssi-account" in res.stdout
    res = _run(tmp_path, "--pair", "g2s", "--ssi-account", "one@example.com", "--show-mapping")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Mapping board for garmin -> ssi (pair g2s, directionality: to_ssi" in res.stdout


def test_help_names_myssi_and_the_account_flag(tmp_path):
    res = _run(tmp_path, "--help")
    assert res.returncode == 0
    text = " ".join(res.stdout.split())
    assert "--ssi-account EMAIL" in text and "ssi[:<email>] (MySSI, beta)" in text
    assert "new dives only" in text and "MySSI" in text
