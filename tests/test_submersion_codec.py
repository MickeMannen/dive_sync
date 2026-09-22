"""Submersion codec, HLC and store layer (rework.md F10/F11) against the real
fixtures in tests/data/submersion."""
import base64
import json
import os
import zlib

import pytest

from src.core.services.submersion import codec
from src.core.services.submersion.hlc import Hlc, HlcClock, hlc_key
from src.core.services.submersion import store as st

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "submersion")
pytestmark = pytest.mark.skipif(not os.path.isdir(FIXTURE), reason="submersion fixtures missing")


def _bases():
    out = []
    for name in sorted(os.listdir(FIXTURE)):
        if ".base." in name:
            out.append(json.load(open(os.path.join(FIXTURE, name))))
    return out


# ---------------------------------------------------------------- codec

def test_profile_codec_round_trips_every_fixture_series_byte_exact():
    checked = 0
    for base in _bases():
        for row in base["data"]["diveProfileSeries"]:
            blob = base64.b64decode(row["samples"])
            samples = codec.decode_profile(blob)
            assert len(samples) == row["sampleCount"]
            assert samples[0][0] == row["startTimestamp"] and samples[-1][0] == row["endTimestamp"]
            depths = [s[1] for s in samples if s[1] is not None]
            assert max(depths) == pytest.approx(row["maxDepth"]) and depths[0] == pytest.approx(row["firstDepth"])
            # decode every known column, re-encode them, and require byte equality
            # with the original up to there (the trailing columns of the app's
            # table are not something dive_sync ever writes non-null)
            known = codec.PROFILE_COLUMNS[:17]
            full = codec.decode_series(blob, known)
            count = full.pop("__count__")[0]
            encoded = zlib.decompress(codec.encode_series(full, count, known))
            assert zlib.decompress(blob).startswith(encoded), row["id"]
            checked += 1
    assert checked >= 5


def test_pressure_codec_round_trips_fixture_series():
    for base in _bases():
        for row in base["data"]["tankPressureSeries"]:
            blob = base64.b64decode(row["samples"])
            samples = codec.decode_pressures(blob)
            assert len(samples) == row["sampleCount"] and samples[0][0] == row["startTimestamp"]
            assert zlib.decompress(codec.encode_pressures(samples)) == zlib.decompress(blob)


def test_encode_profile_from_unified_style_samples():
    samples = [(0, 0.0, 30.0), (2, 1.5, None), (4, None, 29.5), (7, 12.25, 29.0)]
    blob = codec.encode_profile(samples)
    assert codec.decode_profile(blob) == samples
    assert codec.profile_summary(samples) == {"sampleCount": 4, "startTimestamp": 0, "endTimestamp": 7, "maxDepth": 12.25,
                                              "firstDepth": 0.0, "lastDepth": 12.25, "codecVersion": 1}
    # every column of the v1 table is present in the output, the unused ones as all-null
    raw = zlib.decompress(blob)
    assert raw[0] == 1 and raw.endswith(b"\x00" * 20)
    with pytest.raises(codec.CodecError):
        codec.decode_profile(zlib.compress(b"\x02\x00"))


# ---------------------------------------------------------------- hlc

def test_hlc_format_ordering_and_clock(tmp_path):
    a = Hlc.parse("001789551131792:000000:522e1189-ae28-49cf-bd0e-24cbbaeff71e")
    assert str(a) == "001789551131792:000000:522e1189-ae28-49cf-bd0e-24cbbaeff71e"
    assert hlc_key("001789551131792:000001:x") > hlc_key(str(a)) > hlc_key(None) and Hlc.parse("junk") is None

    times = iter([1000, 1000, 1000, 2000])
    path = str(tmp_path / "clock.json")
    clock = HlcClock("dev", path, now_ms=lambda: next(times))
    t1, t2, t3 = clock.tick(), clock.tick(), clock.tick()
    assert (t1.physical_ms, t1.counter) == (1000, 0) and (t2.counter, t3.counter) == (1, 2)
    clock.observe("000000000005000:000004:peer")
    assert clock.current.physical_ms == 5000 and clock.current.counter == 4
    reloaded = HlcClock("dev", path, now_ms=lambda: 2000)
    assert reloaded.tick() == Hlc(5000, 5, "dev")   # persisted and still monotonic


# ---------------------------------------------------------------- store layer

def test_names_and_checksums():
    assert st.parse_name("ssv1.522e1189-ae28-49cf-bd0e-24cbbaeff71e.base.000000000001.p0000") == \
        {"device": "522e1189-ae28-49cf-bd0e-24cbbaeff71e", "kind": "base", "seq": 1, "part": 0}
    assert st.parse_name("ssv1.522e1189-ae28-49cf-bd0e-24cbbaeff71e.cs.000000000017.json")["seq"] == 17
    assert st.parse_name("ssv1.522e1189-ae28-49cf-bd0e-24cbbaeff71e.retired.json")["kind"] == "retired"
    assert st.parse_name("other.json") is None
    assert st.base_name("d", 3, 1) == "ssv1.d.base.000000000003.p0001"
    data = {"dives": [{"id": "a", "name": "Ön", "maxDepth": 12.5}]}
    assert st.data_checksum(data) == st.data_checksum(json.loads(st.encode_payload(data)))
    assert st.file_checksum(b"x").startswith("sha256:")
    with pytest.raises(st.EncryptedStoreError, match="end-to-end encrypted"):
        st.ensure_plain(b"SBE1" + b"\x00" * 10, "ssv1.x.manifest.json")


def test_folder_store_reads_the_fixture_peers(tmp_path):
    store = st.FolderStore(FIXTURE)
    devices = st.list_devices(store)
    assert len(devices) == 2 and all(d["manifest"] and 1 in d["bases"] and not d["retired"] for d in devices.values())
    peer_id = sorted(devices)[0]
    peer = st.read_peer(store, peer_id, devices[peer_id])
    assert peer["manifest"]["deviceId"] == peer_id and len(peer["payloads"]) == 1
    assert len(peer["payloads"][0]["data"]["dives"]) == 5
    assert st.read_epoch(store) is None


def test_publish_base_and_heartbeat_round_trip(tmp_path):
    store = st.FolderStore(str(tmp_path / "sync"))
    data = {"dives": [{"id": "d1", "hlc": "000000000001000:000000:me"}], "diveTanks": []}
    payload = st.build_payload("me", data, {"dives": []}, seq=1, to_hlc="000000000001000:000000:me", since_hlc=None,
                               epoch_id=None, now_ms=5000)
    assert payload["checksum"] == st.data_checksum(data)
    manifest = st.publish_base(store, "me", "dive_sync", payload, {"peer": "000000000000900:000000:peer"}, "folder", 5000)
    assert manifest["baseSeq"] == 1 and manifest["headSeq"] == 1 and manifest["schemaVersion"] == 210
    assert manifest["basePartCount"] == 1 and manifest["appliedPeerHlc"] == {"peer": "000000000000900:000000:peer"}
    devices = st.list_devices(store)
    assert set(devices) == {"me"}
    peer = st.read_peer(store, "me", devices["me"])
    assert peer["payloads"][0]["data"] == data and peer["manifest"]["baseChecksum"] == manifest["baseChecksum"]
    touched = st.touch_manifest(store, "me", manifest, 9000)
    assert touched["updatedAt"] == 9000 and touched["uploadNonce"] != manifest["uploadNonce"]
    assert st.read_json(store, st.manifest_name("me"))["updatedAt"] == 9000
    st.remove_device_files(store, "me")
    assert st.list_devices(store) == {}
