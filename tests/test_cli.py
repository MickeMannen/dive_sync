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
        d = tmp_path / "mock" / service
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
    updated = json.loads((tmp_path / "mock" / "divelogs" / "1.json").read_text())
    assert updated["notes"] == "garmin notes"
    res = _run(tmp_path, "--list-conflicts")
    assert "No conflicts waiting" in res.stdout


def test_test_mapping_is_read_only(tmp_path):
    _write_mock(tmp_path, [G1], [D1])
    (tmp_path / "settings.json").write_text(json.dumps({"api_cooldown_seconds": 0}))
    before = (tmp_path / "mock" / "divelogs" / "1.json").read_text()
    res = _run(tmp_path, "--test-mapping")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "1 matched" in res.stdout and "kept" in res.stdout and "Read-only" in res.stdout
    assert (tmp_path / "mock" / "divelogs" / "1.json").read_text() == before
    assert not (tmp_path / "mock" / "sync_state.json").exists()
