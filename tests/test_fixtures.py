"""Guards on the committed Submersion / Subsurface Cloud fixtures: they parse,
carry what the adapters will need, and contain no leaked identifiers."""
import base64
import json
import os
import re
import zlib

import pytest

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SUBMERSION = os.path.join(DATA, "submersion")
SUBSURFACE = os.path.join(DATA, "subsurface_cloud")


def _submersion_files():
    return sorted(os.listdir(SUBMERSION)) if os.path.isdir(SUBMERSION) else []


@pytest.mark.skipif(not os.path.isdir(SUBMERSION), reason="submersion fixtures not present")
def test_submersion_fixture_shape():
    manifests = [f for f in _submersion_files() if f.endswith(".manifest.json")]
    bases = [f for f in _submersion_files() if ".base." in f]
    assert len(manifests) == 2 and len(bases) == 2
    for name in manifests:
        m = json.load(open(os.path.join(SUBMERSION, name)))
        assert m["formatVersion"] == 1 and m["schemaVersion"] == 210 and m["provider"] == "s3"
        assert m["deviceName"].startswith("Test device")
        assert name == f"ssv1.{m['deviceId']}.manifest.json"
    peers = [json.load(open(os.path.join(SUBMERSION, n)))["appliedPeerHlc"] for n in manifests]
    assert any(peers), "one device should have applied the other's HLC"

    base = json.load(open(os.path.join(SUBMERSION, bases[0])))
    assert base["version"] == 2 and base["seq"] == 1
    t = base["data"]
    dives = t["dives"]
    assert len(dives) == 5
    assert all(d["diveDateTime"] > 10**12 for d in dives)  # milliseconds
    assert t["divers"][0]["isDefault"] and t["divers"][0]["name"] == "Test Diver"
    tanks_per_dive = {d["id"]: sum(1 for x in t["diveTanks"] if x["diveId"] == d["id"]) for d in dives}
    assert sorted(tanks_per_dive.values()) == [1, 1, 1, 2, 2]
    two_tank = next(d for d in dives if tanks_per_dive[d["id"]] == 2)
    orders = sorted(x["tankOrder"] for x in t["diveTanks"] if x["diveId"] == two_tank["id"])
    assert orders == [0, 1]
    series = next(x for x in t["diveProfileSeries"] if x["diveId"] == dives[0]["id"])
    raw = zlib.decompress(base64.b64decode(series["samples"]))
    assert series["codecVersion"] == 1 and series["sampleCount"] > 100 and len(raw) > series["sampleCount"]
    assert all(row["bytes"] == "" for row in t["importedFiles"])  # stripped


@pytest.mark.skipif(not os.path.isdir(SUBSURFACE), reason="subsurface fixtures not present")
def test_subsurface_cloud_fixture_shape():
    assert os.path.isfile(os.path.join(SUBSURFACE, "00-Subsurface"))
    assert os.path.isdir(os.path.join(SUBSURFACE, "01-Divesites"))
    dive_dirs = []
    for root, _dirs, files in os.walk(SUBSURFACE):
        if any(f.startswith("Dive-") for f in files):
            assert "Divecomputer" in files, root
            dive_dirs.append(root)
    assert len(dive_dirs) == 8
    two_cyl = [d for d in dive_dirs if open(os.path.join(d, next(f for f in os.listdir(d) if f.startswith("Dive-")))).read().count("cylinder ") == 2]
    assert len(two_cyl) == 2
    dc = open(os.path.join(two_cyl[0], "Divecomputer")).read()
    assert 'keyvalue "Serial" "0000000000"' in dc
    assert re.search(r"\d+\.\d+bar:1", dc), "second cylinder pressure samples expected"


def test_fixtures_contain_no_identifiers():
    leaks = []
    for root, _dirs, files in os.walk(DATA):
        for fn in files:
            path = os.path.join(root, fn)
            if fn.endswith((".json", ".p0000")) or "subsurface_cloud" in root:
                text = open(path, encoding="utf-8", errors="replace").read()
                if re.search(r"[A-Za-z0-9._%+-]+@(?!example\.com)[A-Za-z0-9.-]+\.[a-z]{2,}", text):
                    leaks.append((fn, "email"))
                if "backblazeb2" in text or "3504700399" in text:
                    leaks.append((fn, "account/serial"))
    assert leaks == []
