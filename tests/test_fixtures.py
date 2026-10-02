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


# --- Garmin dive FITs (plans/convert.md I2), made by tests/tools/anonymize_fit.py ---

GARMIN_FIT = os.path.join(DATA, "garmin_fit")
FIT_FIXTURES = ("single_gas.fit", "two_tanks.fit")
PLACEHOLDER_SERIAL, PLACEHOLDER_SENSORS = 1000000001, {2000000001, 2000000002}
# The sensor profile's fields the tool keeps (it is message 147, which the FIT profile does not name).
SENSOR_PROFILE_FIELDS = {0, 2, 3, 52, 74, 75, 76, 77, 78, 91, 254}
KEPT_MESSAGES = {"file_id", "file_creator", "activity", "session", "lap", "event", "device_info", "record",
                 "dive_settings", "dive_gas", "dive_summary", "tank_update", "tank_summary", "147"}


def _decode_fit(name):
    pytest.importorskip("garmin_fit_sdk", reason="garmin-fit-sdk is a dev-only dependency (requirements-dev.txt)")
    from garmin_fit_sdk import Decoder, Stream

    path = os.path.join(GARMIN_FIT, name)
    assert Decoder(Stream.from_file(path)).check_integrity(), name
    messages, errors = Decoder(Stream.from_file(path)).read()
    assert errors == [], (name, errors)
    return {key[:-6] if key.endswith("_mesgs") else key: items for key, items in messages.items()}


@pytest.mark.parametrize("name", FIT_FIXTURES)
def test_garmin_fit_fixture_decodes_and_is_small(name):
    m = _decode_fit(name)
    assert set(m) <= KEPT_MESSAGES, set(m) - KEPT_MESSAGES
    assert os.path.getsize(os.path.join(GARMIN_FIT, name)) <= 32 * 1024
    assert 200 <= len(m["record"]) <= 500
    session = m["session"][0]
    assert (session["sport"], session["sub_sport"]) == ("diving", "single_gas_diving")
    assert m["dive_summary"][0]["dive_number"] > 0
    activity = m["activity"][0]
    assert activity["local_timestamp"] is not None  # the time zone survives


@pytest.mark.parametrize("name", FIT_FIXTURES)
def test_garmin_fit_fixture_holds_no_identifier(name):
    import struct

    m = _decode_fit(name)  # skips without garmin-fit-sdk, which the tool below imports too
    from tests.tools.anonymize_fit import FIXED_LAT_DEG, FIXED_LONG_DEG, degrees

    assert "user_profile" not in m
    serials = {d["serial_number"] for d in m["file_id"] + m["device_info"] if "serial_number" in d}
    assert serials == {PLACEHOLDER_SERIAL}
    sensors = ({d[24] for d in m["device_info"] if 24 in d} | {t["sensor"] for t in m["tank_update"]}
               | {t["sensor"] for t in m["tank_summary"]} | {p[0] for p in m["147"]})
    assert sensors <= PLACEHOLDER_SENSORS
    for profile in m["147"]:
        assert set(k for k in profile) <= SENSOR_PROFILE_FIELDS
        assert profile[2] == profile[91] and re.fullmatch(r"Tank [12]", profile[2])
    texts = [v for items in m.values() for item in items for v in item.values() if isinstance(v, str)]
    assert not any(re.search(r"[a-z]{4,}\d|@|micke", t, re.I) for t in texts)
    unnamed = {(k, f) for k, items in m.items() if k != "147" for item in items for f in item if isinstance(f, int)}
    assert unnamed <= {("device_info", 24)}, unnamed
    for key in ("session", "lap"):
        for item in m[key]:
            for fname, value in item.items():
                if fname.endswith("_lat"):
                    assert abs(degrees(value) - FIXED_LAT_DEG) < 0.01, (key, fname)
                elif fname.endswith("_long"):
                    assert abs(degrees(value) - FIXED_LONG_DEG) < 0.01, (key, fname)
    raw = open(os.path.join(GARMIN_FIT, name), "rb").read()
    assert struct.pack("<I", 3504700399) not in raw  # the source watch's serial (see the leak test above)
    assert not any("position" in f for r in m["record"] for f in r if isinstance(f, str))


def test_garmin_fit_single_gas_fixture():
    m = _decode_fit("single_gas.fit")
    assert [(g["oxygen_content"], g["helium_content"], g["status"]) for g in m["dive_gas"]] == [(32, 0, "enabled")]
    assert len(m["tank_summary"]) == 1 and {t["sensor"] for t in m["tank_update"]} == {2000000001}
    assert max(r["depth"] for r in m["record"]) == pytest.approx(m["dive_summary"][0]["max_depth"])


def test_garmin_fit_two_tanks_fixture():
    """Two transmitters on one gas. The source dives hold no mid-dive gas
    switch (only the start-of-dive one), so this fixture has none either."""
    m = _decode_fit("two_tanks.fit")
    assert [(g["oxygen_content"], g["status"]) for g in m["dive_gas"]] == [(21, "enabled")]
    summaries = {t["sensor"]: (t["start_pressure"], t["end_pressure"]) for t in m["tank_summary"]}
    assert set(summaries) == PLACEHOLDER_SENSORS
    for sensor, (start, end) in summaries.items():
        own = [t["pressure"] for t in m["tank_update"] if t["sensor"] == sensor]
        assert len(own) > 100 and own[0] == pytest.approx(start, abs=1.0) and start > end
    first = m["record"][0]["timestamp"]
    switches = [e for e in m["event"] if e["event"] == "dive_gas_switched"]
    assert [(e["timestamp"], e["data"]) for e in switches] == [(first, 0)]
    pods = {e["data"] for e in m["event"] if e["event"] == "tank_pod_connected"}
    assert pods == PLACEHOLDER_SENSORS
    assert max(r["depth"] for r in m["record"]) == pytest.approx(m["dive_summary"][0]["max_depth"])
