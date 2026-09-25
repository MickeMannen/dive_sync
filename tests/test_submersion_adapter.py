"""Submersion peer adapter (rework.md F12): read the real fixture as a folder
store, and round-trip writes through a scratch folder store."""
import base64
import json
import os
import shutil
from datetime import datetime

import pytest

from src.core.models import GasMixture, UnifiedDive, UnifiedSample
from src.core.config import SubmersionCredentials
from src.core.services.submersion import crypto, store as st
from src.core.services.submersion.adapter import SubmersionAdapter

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "submersion")
pytestmark = pytest.mark.skipif(not os.path.isdir(FIXTURE), reason="submersion fixtures missing")


def _folder_config(path):
    return SubmersionCredentials(store_type="folder", folder_path=str(path))


def test_reads_the_fixture_library(tmp_path):
    a = SubmersionAdapter(_folder_config(FIXTURE), device_state_dir=str(tmp_path / "state"))
    assert a.login()
    dives = a.fetch_dives()
    # two devices, same five dives merged by HLC -> five unique dives
    assert len(dives) == 5
    d = dives[0]
    assert d.external_ids["submersion"] and d.date_time.year == 2026
    assert d.duration > 0 and d.max_depth > 0 and d.location
    two_tank = [x for x in dives if len(x.gas_mixtures) == 2]
    assert two_tank, "expected the two-tank dives from the fixture"
    withprofile = [x for x in dives if x.samples]
    assert withprofile and withprofile[0].samples[0].depth is not None
    # date filter
    assert a.fetch_dives(date_from=datetime(2027, 1, 1)) == []


def test_write_round_trip_through_a_scratch_store(tmp_path):
    store_dir = tmp_path / "store"
    state_dir = tmp_path / "state"
    # seed a default diver so created dives get a diverId
    os.makedirs(store_dir)
    a = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(state_dir), device_id="dev-a")
    assert a.login() and a.fetch_dives() == []

    dive = UnifiedDive(
        date_time=datetime(2026, 9, 1, 10, 30, 0), duration=2500, max_depth=21.5, avg_depth=12.0, temp_min=24.0,
        external_ids={"garmin": "555"}, dive_number=14, buddy="Anna, Bo", notes="Nice",
        location="Zenobia", lat=34.887, lng=33.657, weight=6.0, weight_unit="kilogram", visibility=25.0, visibility_unit="meter",
        gas_mixtures=[GasMixture(oxygen=32.0, start_pressure=210.0, end_pressure=60.0, tank_volume=12.0),
                      GasMixture(oxygen=50.0, start_pressure=200.0, end_pressure=150.0, tank_volume=7.0)],
        samples=[UnifiedSample(depth=0.0, temp=25.0, time=0), UnifiedSample(depth=21.5, temp=24.0, time=600),
                 UnifiedSample(depth=0.3, time=2500)],
    )
    new_id = a.add_dive(dive)
    a.finish()

    # a fresh adapter (another "device") reads what was published
    b = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state_b"), device_id="dev-b")
    b.login()
    dives = b.fetch_dives()
    assert len(dives) == 1
    got = dives[0]
    assert got.external_ids["submersion"] == new_id and got.external_ids["garmin"] == "555"
    # No diveDataSources row: that table belongs to the .fit import the diver
    # runs in the Submersion app, and every profile / tank-pressure / gas-switch
    # row hangs off it (rework.md F17). dive_sync's own link back to the Garmin
    # activity is importSource/importId on the dive row.
    assert b.library.children("diveDataSources", new_id) == []
    assert (b.library.tables["dives"][new_id]["importSource"],
            b.library.tables["dives"][new_id]["importId"]) == ("garmin", "555")
    assert got.date_time == dive.date_time and got.duration == 2500 and got.max_depth == 21.5
    assert got.location == "Zenobia" and (round(got.lat, 3), round(got.lng, 3)) == (34.887, 33.657)
    assert got.dive_number == 14 and got.weight == 6.0 and got.visibility == 25.0
    assert sorted(b for b in (got.buddy or "").split(", ")) == ["Anna", "Bo"]
    assert [round(g.oxygen) for g in got.gas_mixtures] == [32, 50] and got.gas_mixtures[0].tank_volume == 12.0
    assert len(got.samples) == 3 and got.samples[1].depth == 21.5

    # the base file and manifest exist and are well-formed
    devices = st.list_devices(st.FolderStore(str(store_dir)))
    assert "dev-a" in devices and devices["dev-a"]["manifest"]
    manifest = st.read_json(st.FolderStore(str(store_dir)), devices["dev-a"]["manifest"])
    assert manifest["schemaVersion"] == 210 and manifest["baseSeq"] == 1

    # update then delete, republish, and confirm through a fresh reader each time
    got.notes = "Edited"
    got.buddy = "Anna"
    assert a.update_dive(new_id, got)
    a.finish()
    c = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state_c"))
    c.login()
    edited = c.fetch_dives()[0]
    assert edited.notes == "Edited" and edited.buddy == "Anna"

    assert a.delete_dive(new_id)
    a.finish()
    d = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state_d"))
    d.login()
    assert d.fetch_dives() == []
    # deletions are published grouped by Submersion table name (not a flat "rows" key)
    base = st.read_json(st.FolderStore(str(store_dir)), st.list_devices(st.FolderStore(str(store_dir)))["dev-a"]["bases"][1][0])
    assert "dives" in base["deletions"] and "rows" not in base["deletions"]
    assert all("entityType" in r and "hlc" in r for recs in base["deletions"].values() for r in recs)


def test_refuses_encrypted_store(tmp_path):
    store_dir = tmp_path / "enc"
    os.makedirs(store_dir)
    (store_dir / "ssv1.x.manifest.json").write_bytes(b"SBE1" + b"\x00" * 40)
    a = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state"))
    # login only lists (no read), so it succeeds; the read raises the clear error
    with pytest.raises(st.EncryptedStoreError):
        a.fetch_dives()


def _seed_keyslots(store_dir, passphrase: str, kdf=None) -> str:
    """Write a real ``submersion_keyslots.json`` (the same shape Submersion's
    app would) with one passphrase slot, and return the library key id."""
    kdf = kdf or crypto.KdfParams(m=1024, t=3, p=1)  # small params keep tests fast
    mlk = os.urandom(32)
    library_key_id = "8f14e45f-ceea-467f-ab37-a10a8d5f4c11"
    slot = crypto.create_slot("passphrase", passphrase, mlk, kdf=kdf)
    file = crypto.KeyslotFile(version=1, library_key_id=library_key_id, slots=[slot])
    st.FolderStore(str(store_dir)).put(crypto.KeyslotFile.CLOUD_FILE_NAME, file.to_json_bytes())
    return library_key_id


def _encrypted_config(path, passphrase=""):
    return SubmersionCredentials(store_type="folder", folder_path=str(path), passphrase=passphrase)


def test_encrypted_store_write_read_round_trip(tmp_path):
    store_dir = tmp_path / "store"
    os.makedirs(store_dir)
    library_key_id = _seed_keyslots(store_dir, "correct horse battery staple")

    a = SubmersionAdapter(_encrypted_config(store_dir, "correct horse battery staple"),
                          device_state_dir=str(tmp_path / "state_a"), device_id="dev-a")
    assert a.login()
    assert a.store.encryption is not None and a.store.encryption.library_key_id == library_key_id

    dive = UnifiedDive(date_time=datetime(2026, 9, 1, 10, 30, 0), duration=2500, max_depth=21.5,
                       location="Zenobia", notes="Encrypted round trip")
    new_id = a.add_dive(dive)
    a.finish()

    # every ssv1.* file actually written is a genuine SBE1 envelope, not
    # silently plaintext - proves finish() sealed rather than skipped it
    plain_store = st.FolderStore(str(store_dir))
    devices = st.list_devices(plain_store)
    manifest_name = devices["dev-a"]["manifest"]
    assert plain_store.get(manifest_name)[:4] == crypto.MAGIC
    base_part_name = devices["dev-a"]["bases"][1][0]
    assert plain_store.get(base_part_name)[:4] == crypto.MAGIC

    # a second device with the right passphrase reads it back
    b = SubmersionAdapter(_encrypted_config(store_dir, "correct horse battery staple"),
                          device_state_dir=str(tmp_path / "state_b"))
    assert b.login()
    dives = b.fetch_dives()
    assert len(dives) == 1
    assert dives[0].external_ids["submersion"] == new_id
    assert dives[0].location == "Zenobia" and dives[0].notes == "Encrypted round trip"


def test_encrypted_store_wrong_or_missing_passphrase_fails_login(tmp_path):
    store_dir = tmp_path / "store"
    os.makedirs(store_dir)
    _seed_keyslots(store_dir, "correct horse battery staple")

    wrong = SubmersionAdapter(_encrypted_config(store_dir, "wrong guess"), device_state_dir=str(tmp_path / "state_w"))
    assert wrong.login() is False
    assert wrong.store.encryption is None

    blank = SubmersionAdapter(_encrypted_config(store_dir, ""), device_state_dir=str(tmp_path / "state_n"))
    assert blank.login() is False
    assert blank.store.encryption is None


def test_unencrypted_store_login_leaves_encryption_unset(tmp_path):
    """No submersion_keyslots.json at all -> plaintext, exactly as before E11."""
    store_dir = tmp_path / "store"
    os.makedirs(store_dir)
    a = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state"))
    assert a.login()
    assert a.store.encryption is None


def test_rejoins_after_retirement(tmp_path):
    store_dir = tmp_path / "store"
    os.makedirs(store_dir)
    a = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state"), device_id="dev-a")
    a.login()
    a.add_dive(UnifiedDive(date_time=datetime(2026, 1, 1, 9), duration=100, max_depth=5.0, location="X"))
    a.finish()
    # a peer retires dev-a
    st.FolderStore(str(store_dir)).put(st.retired_name("dev-a"), b"{}")
    a2 = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state"), device_id="dev-a")
    a2.login()
    a2.fetch_dives()
    assert a2.device_id != "dev-a"   # re-joined under a new id


def test_hlc_higher_wins_across_devices(tmp_path):
    """A row present in two device payloads is resolved by HLC."""
    from src.core.services.submersion.library import Library, dive_to_unified
    lib = Library()
    lib.apply_payload({"data": {"divers": [{"id": "dv", "isDefault": True, "hlc": "000000000000001:000000:a"}],
                                "dives": [{"id": "d1", "diveDateTime": 1000, "runtime": 100, "maxDepth": 10.0,
                                           "notes": "old", "hlc": "000000000000010:000000:a"}]},
                       "deletions": {}})
    lib.apply_payload({"data": {"dives": [{"id": "d1", "diveDateTime": 1000, "runtime": 100, "maxDepth": 10.0,
                                           "notes": "new", "hlc": "000000000000020:000000:b"}]}, "deletions": {}})
    dives = lib.dives()
    assert len(dives) == 1 and dives[0]["notes"] == "new"
    # a tombstone newer than the row hides it
    lib.apply_payload({"data": {}, "deletions": {"dives": [{"id": "d1", "hlc": "000000000000030:000000:b"}]}})
    assert lib.dives() == []



def test_matched_dive_keeps_its_existing_garmin_source(tmp_path):
    """A dive that already has a Garmin (FIT) data source must not gain a
    second one when dive_sync links it; a created dive gets exactly one."""
    store_dir = tmp_path / "store"
    os.makedirs(store_dir)
    a = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "sa"), device_id="a")
    a.login()
    # seed a dive that already carries a Garmin FIT source, as a peer import would
    from src.core.services.submersion import store as st
    import uuid as _uuid
    dive_id = str(_uuid.uuid4())
    a.library.tables["dives"][dive_id] = {"id": dive_id, "diveDateTime": 1788007920000, "runtime": 100,
                                          "maxDepth": 10.0, "hlc": "000000000000001:000000:peer", "notes": ""}
    src_id = str(_uuid.uuid4())
    a.library.tables["diveDataSources"][src_id] = {"id": src_id, "diveId": dive_id,
        "sourceUuid": "garmin-3504700399-1788007920000", "isPrimary": True, "hlc": "000000000000001:000000:peer"}
    a._loaded = True   # keep the seeded rows; skip the reload update_dive would otherwise do
    from datetime import datetime
    dive = UnifiedDive(date_time=datetime(2026, 8, 29, 12, 52), duration=100, max_depth=10.0,
                       external_ids={"submersion": dive_id, "garmin": "999"}, buddy="X")
    a.update_dive(dive_id, dive)
    sources = a.library.children("diveDataSources", dive_id)
    assert len(sources) == 1 and sources[0]["sourceUuid"].startswith("garmin-3504700399")   # original kept, no duplicate


# ---------------------------------------------------------------- E5: tank order and role

def test_tank_role_and_name_are_not_confused(tmp_path):
    """Regression: the adapter used to store tank_name into Submersion's
    tankRole column (and vice versa on read), so a real tank name like
    'D12' would silently become a nonsense role. tank_role and tank_name are
    now distinct fields end to end."""
    store_dir = tmp_path / "store"
    os.makedirs(store_dir)
    a = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state"), device_id="a")
    a.login()
    dive = UnifiedDive(
        date_time=datetime(2026, 9, 1, 10), duration=1000, max_depth=15.0,
        gas_mixtures=[
            GasMixture(oxygen=21.0, tank_volume=11.1, tank_name="D12", tank_role="backGas"),
            GasMixture(oxygen=50.0, tank_volume=7.0, tank_name="Deco 50", tank_role="deco"),
            GasMixture(oxygen=21.0, tank_volume=3.0, tank_role="sidemountRight"),
        ],
    )
    dive_id = a.add_dive(dive)
    a.finish()
    b = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state_b"))
    b.login()
    got = b.fetch_dives()[0]
    assert [(g.tank_name, g.tank_role) for g in got.gas_mixtures] == \
        [("D12", "backGas"), ("Deco 50", "deco"), (None, "sidemountRight")]
    # order preserved via tankOrder
    tanks = sorted(b.library.children("diveTanks", dive_id), key=lambda t: t["tankOrder"])
    assert [t["tankRole"] for t in tanks] == ["backGas", "deco", "sidemountRight"]


def test_update_leaves_the_cylinders_and_profile_alone(tmp_path):
    """rework.md F17: a dive that already exists in Submersion gets its
    metadata only. Its cylinders, depth profile, tank-pressure series and gas
    switches were derived by the app's own .fit import from one file and
    reference one another; replacing the tank rows from a sender's gas data
    broke that (and could not carry a role or name at all, since neither
    Garmin nor Divelogs has one). Soft fields still apply."""
    store_dir = tmp_path / "store"
    os.makedirs(store_dir)
    a = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state"), device_id="a")
    a.login()
    dive_id = a.add_dive(UnifiedDive(
        date_time=datetime(2026, 9, 1, 10), duration=1000, max_depth=15.0,
        gas_mixtures=[GasMixture(oxygen=32.0, start_pressure=220.0, end_pressure=70.0,
                                 tank_name="D12", tank_role="backGas")],
        samples=[UnifiedSample(depth=1.0, time=0), UnifiedSample(depth=15.0, time=300)]))
    a.finish()
    before_tanks = a.library.children("diveTanks", dive_id)
    before_profile = a.library.children("diveProfileSeries", dive_id)

    # an update carrying different gas and a shorter profile, as a Garmin-side
    # read would (no role, no name, its own pressures), plus a soft field
    incoming = UnifiedDive(date_time=datetime(2026, 9, 1, 10), duration=1000, max_depth=15.0, buddy="Anna",
                           gas_mixtures=[GasMixture(oxygen=21.0, start_pressure=200.0, end_pressure=60.0)],
                           samples=[UnifiedSample(depth=2.0, time=0)])
    a.update_dive(dive_id, incoming)
    a.finish()
    # not a single tank or profile row touched: same ids, same values
    assert a.library.children("diveTanks", dive_id) == before_tanks
    assert a.library.children("diveProfileSeries", dive_id) == before_profile

    c = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state_c"))
    c.login()
    got = c.fetch_dives()[0]
    assert got.buddy == "Anna"                            # the soft field did apply
    gas = got.gas_mixtures[0]
    assert (gas.tank_name, gas.tank_role) == ("D12", "backGas")
    assert (gas.oxygen, gas.start_pressure, gas.end_pressure) == (32.0, 220.0, 70.0)
    assert [s.depth for s in got.samples] == [1.0, 15.0]


def test_an_update_without_a_site_keeps_the_one_the_dive_has(tmp_path):
    """rework.md F17: a sender that names no site and carries no GPS can only
    ever blank the dive's site, never correct it, so the stored siteId stays."""
    store_dir = tmp_path / "store"
    os.makedirs(store_dir)
    a = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state"), device_id="a")
    a.login()
    dive_id = a.add_dive(UnifiedDive(date_time=datetime(2026, 9, 1, 10), duration=1000, max_depth=15.0,
                                     location="Batu Tokong Daik", lat=5.8, lng=103.1))
    a.finish()
    site_id = a.library.tables["dives"][dive_id]["siteId"]
    assert site_id and a.library._alive("diveSites")[site_id]["name"] == "Batu Tokong Daik"

    a.update_dive(dive_id, UnifiedDive(date_time=datetime(2026, 9, 1, 10), duration=1000, max_depth=15.0,
                                       notes="No site on this side"))
    assert a.library.tables["dives"][dive_id]["siteId"] == site_id
    a.finish()
    c = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state_c"))
    c.login()
    got = c.fetch_dives()[0]
    assert got.location == "Batu Tokong Daik" and got.notes == "No site on this side"


def test_buddy_accepts_a_list_and_is_declared_as_text(tmp_path):
    """A real Garmin -> Submersion run died with "'list' object has no
    attribute 'split'": submersion.buddy was declared a list, so the link
    engine converted Garmin's text buddy into ["Guy"] before handing it over.
    The value this adapter reads and writes is the comma-joined string
    UnifiedDive.buddy holds, so the field is text - and a list is tolerated
    either way rather than crashing the whole sync run."""
    from src.core.fields import convert_value
    from src.core.services.submersion.adapter import _buddy_names

    spec = next(f for f in SubmersionAdapter.field_catalog() if f.key == "submersion.buddy")
    assert spec.type == "text"
    # Same type on both sides now, so no conversion happens in either
    # direction - the character-by-character join is gone too.
    assert convert_value("Guy", "text", "text") == "Guy"

    assert _buddy_names("Guy") == ["Guy"]
    assert _buddy_names("Ann, Bo") == ["Ann", "Bo"]
    assert _buddy_names(["Ann", "Bo"]) == ["Ann", "Bo"]          # what the engine used to hand over
    assert _buddy_names(None) == [] and _buddy_names("") == [] and _buddy_names([]) == []
    assert _buddy_names(" Ann ,, Bo ") == ["Ann", "Bo"]

    store_dir = tmp_path / "store"
    os.makedirs(store_dir)
    a = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state"), device_id="dev-a")
    assert a.login()
    dive = UnifiedDive(date_time=datetime(2026, 9, 1, 10, 30, 0), duration=2500, max_depth=21.5,
                       external_ids={"garmin": "555"}, buddy="Guy")
    dive_id = a.add_dive(dive)
    # The crash was on update of an already-matched dive, with a list arriving
    # from the board. It must survive that and store one row per buddy.
    dive.buddy = ["Ann", "Bo"]
    assert a.update_dive(dive_id, dive) is True
    names = sorted(a.library.tables["buddies"][link["buddyId"]]["name"]
                   for link in a.library.children("diveBuddies", dive_id))
    assert names == ["Ann", "Bo"]


def test_start_time_is_writable_for_hand_entered_dives(tmp_path):
    """rework.md G9: a historical dive no computer ever recorded has to get
    its start time from somewhere, so submersion.date_time is writable (as it
    already was on Garmin and Divelogs). Subsurface keeps its own read-only,
    since a dive's directory name there encodes the time."""
    from src.core.services.submersion.adapter import SubmersionAdapter as A
    spec = next(f for f in A.field_catalog() if f.key == "submersion.date_time")
    assert spec.writable is True

    store_dir = tmp_path / "store"
    os.makedirs(store_dir)
    a = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state"), device_id="dev-a")
    assert a.login()
    old_dive = UnifiedDive(date_time=datetime(1998, 7, 14, 9, 15), duration=2700, max_depth=18.0,
                           location="Blue Hole", dive_number=1)
    dive_id = a.add_dive(old_dive)
    a.finish()

    b = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state_b"), device_id="dev-b")
    b.login()
    assert b.fetch_dives()[0].date_time == datetime(1998, 7, 14, 9, 15)

    # and a correction on an already-matched dive lands too
    corrected = b.fetch_dives()[0].model_copy(update={"date_time": datetime(1998, 7, 14, 10, 0)})
    b.update_dive(dive_id, corrected)
    b.finish()
    c = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state_c"), device_id="dev-c")
    c.login()
    assert c.fetch_dives()[0].date_time == datetime(1998, 7, 14, 10, 0)


def test_extra_dive_and_site_fields_round_trip(tmp_path):
    """rework.md G9: the fields the owner marked RW in
    docs/submersion_field_inventory.md. They have no UnifiedDive attribute, so
    they travel in service_fields; the site_* ones live on the shared
    diveSites row, which is what finally gives Divelogs' own location field a
    Submersion counterpart."""
    from src.core.services.submersion.library import EXTRA_FIELDS
    store_dir = tmp_path / "store"
    os.makedirs(store_dir)
    a = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state"), device_id="dev-a")
    assert a.login()

    extras = {
        "dive_name": "Racha Yai morning", "bottomTime": 4080,
        "entryTime": datetime(2026, 8, 29, 12, 52), "exitTime": datetime(2026, 8, 29, 14, 4),
        "diveType": "technical", "diveMode": "ccr", "waterType": "salt",
        "altitude": 0.0, "airTemp": 31.5, "entryMethod": "boat",
        "exitLatitude": 7.61, "exitLongitude": 98.38,
        "boatName": "Sea Bees II", "diveOperator": "All4Diving",
        "site_region": "Phuket", "site_country": "Thailand", "site_island": "Racha Yai",
        "site_city": "Phuket", "site_notes": "Sandy bottom, mooring at the north end",
        "site_waterType": "salt", "site_entryMethod": "giant stride", "site_exitMethod": "ladder",
    }
    assert set(extras) == {name for name, *_ in EXTRA_FIELDS}, "test and declaration out of step"

    dive = UnifiedDive(date_time=datetime(2026, 8, 29, 12, 52), duration=4320, max_depth=13.8,
                       location="Home Run", lat=7.6093, lng=98.3789, dive_number=36,
                       service_fields=dict(extras))
    new_id = a.add_dive(dive)
    a.finish()

    b = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state_b"), device_id="dev-b")
    b.login()
    got = b.fetch_dives()[0]
    for name, value in extras.items():
        assert got.service_fields[name] == value, f"{name}: {got.service_fields[name]!r} != {value!r}"

    # the site fields really landed on the shared diveSites row, not the dive
    site = b.library._alive("diveSites")[b.library._alive("dives")[new_id]["siteId"]]
    assert site["region"] == "Phuket" and site["country"] == "Thailand" and site["island"] == "Racha Yai"
    assert site["name"] == "Home Run"          # still the unified location

    # a sender that knows nothing of these must not blank them (same rule as tanks)
    plain = UnifiedDive(date_time=datetime(2026, 8, 29, 12, 52), duration=4320, max_depth=13.8,
                        location="Home Run", lat=7.6093, lng=98.3789, dive_number=36,
                        external_ids={"submersion": new_id})
    b.update_dive(new_id, plain)
    b.finish()
    c = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state_c"), device_id="dev-c")
    c.login()
    kept = c.fetch_dives()[0]
    assert kept.service_fields["boatName"] == "Sea Bees II"
    assert kept.service_fields["site_country"] == "Thailand"

    # ... but an explicit None clears one on purpose
    cleared = plain.model_copy(update={"service_fields": {"boatName": None, "site_country": None}})
    c.update_dive(new_id, cleared)
    c.finish()
    d = SubmersionAdapter(_folder_config(store_dir), device_state_dir=str(tmp_path / "state_d"), device_id="dev-d")
    d.login()
    after = d.fetch_dives()[0]
    assert after.service_fields["boatName"] is None and after.service_fields["site_country"] is None
    assert after.service_fields["site_island"] == "Racha Yai"      # untouched
