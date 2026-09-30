"""Shearwater app database adapter (rework.md Track H, H1).

The fixture is the owner's own ``dive_data.db`` with its free text
(Buddy, Location, Site, Notes) replaced by placeholders; ids, dates,
depths, gases, profile blobs and the cloud bookkeeping are the real ones.
"""
import logging
import os
import shutil
from datetime import datetime

import pytest

from src.core.services.shearwater import (
    ShearwaterAdapter, DROPDOWN_OPTIONS, parse_gnss, parse_number, psi_to_bar, bar_to_psi_text,
)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "shearwater", "dive_data.db")


@pytest.fixture
def db_copy(tmp_path):
    dst = str(tmp_path / "dive_data.db")
    shutil.copy(FIXTURE, dst)
    return dst


def _by_number(dives):
    return {d.dive_number: d for d in dives}


def test_scalars():
    assert parse_number("2857.24") == 2857.24 and parse_number("") is None and parse_number(None) is None
    assert psi_to_bar("2938") == 202.57 and psi_to_bar("") is None
    assert bar_to_psi_text(200.0) == "2900.75" and bar_to_psi_text(None) == ""
    assert parse_gnss("5.1234,103.4321") == (5.1234, 103.4321)
    assert parse_gnss("5.1234 103.4321") == (5.1234, 103.4321)
    assert parse_gnss('{"Latitude": 5.1, "Longitude": 103.4}') == (5.1, 103.4)
    assert parse_gnss("") == (None, None) and parse_gnss("nowhere") == (None, None)


def test_reads_the_owner_fixture(db_copy):
    adapter = ShearwaterAdapter(db_copy)
    assert adapter.login()
    dives = adapter.fetch_dives()
    assert len(dives) == 27
    assert [d.date_time for d in dives] == sorted(d.date_time for d in dives)
    assert all(d.device_logged and d.samples and d.external_ids["shearwater"] for d in dives)   # every dive has its profile (H5)

    d = _by_number(dives)[452]                        # the dive edited in the app on 2026-09-29
    assert d.external_ids == {"shearwater": "881855471760887268"}
    assert d.date_time == datetime(2025, 10, 19, 15, 21, 8)   # local time as the diver saw it
    assert (d.duration, d.max_depth) == (1788, 25.3)
    assert round(d.avg_depth, 2) == 15.7 and (d.temp_min, d.temp_max) == (29.0, 30.0)   # from log_data's calculated JSON
    assert round(d.temp_avg, 1) == 29.1
    assert (d.location, d.buddy, d.notes) == ("Site 452", "Buddy 452", "Notes 452")
    assert d.service_fields["location"] == "Location 452"
    assert d.service_fields["environment"] == "Lake/Quarry" and d.service_fields["thermal_comfort"] == "Very Hot"
    assert d.service_fields["malfunctions"] == "None"           # the option's label, not a null
    assert d.service_fields["air_temperature"] == 12.0
    assert d.service_fields["environment_notes"] == "Environment notes 452"
    assert d.weight is None and d.lat is None and d.lng is None
    # one transmitter on, pressures from TankProfileData (PSI -> bar), named after the transmitter
    assert len(d.gas_mixtures) == 1
    gas = d.gas_mixtures[0]
    assert (gas.oxygen, gas.helium, gas.tank_name) == (21.0, 0.0, "T1")
    assert (gas.start_pressure, gas.end_pressure) == (202.57, 112.52)
    # the profile decoded from the computer's own log (H5): every sample as logged, 2 s apart, with
    # the transmitter's pressure; the 60 s surface tail after the dive time is kept (decision 2026-09-30)
    assert len(d.samples) == 927
    assert d.samples[0].model_dump() == {"depth": 1.0, "temp": 30.0, "time": 2, "pressure": 202.57}
    assert (d.samples[-1].time, d.samples[-1].depth, d.samples[-1].pressure) == (1854, 0.0, 111.28)
    assert max(s.depth for s in d.samples) == d.max_depth
    assert next(s.pressure for s in d.samples if s.time == d.duration) == gas.end_pressure
    assert all(s.pressure is None for s in _by_number(dives)[427].samples)   # no transmitter on that dive

    # the editable pressure columns win over the JSON
    d432 = _by_number(dives)[432]
    assert d432.gas_mixtures[0].start_pressure == psi_to_bar("2857.24") == 197.0
    assert d432.gas_mixtures[0].end_pressure == psi_to_bar("1218.32")
    assert d432.service_fields["gas_notes"] == "Gas notes 432"
    # untouched dives carry nothing but the computer's data
    d454 = _by_number(dives)[454]
    assert d454.location is None and d454.buddy is None and d454.service_fields == {}


def test_date_window_and_fetch_by_id(db_copy):
    adapter = ShearwaterAdapter(db_copy)
    october = adapter.fetch_dives(date_from=datetime(2025, 10, 1), date_to=datetime(2025, 10, 31))
    assert [d.dive_number for d in october] == list(range(444, 455))[-len(october):]
    assert all(d.date_time.month == 10 for d in october)
    one = adapter.fetch_dive("881855471760887268")
    assert one is not None and one.dive_number == 452
    assert adapter.fetch_dive("nope") is None


def test_refuses_to_create_or_delete(db_copy, tmp_path, caplog):
    from src.core.models import UnifiedDive
    adapter = ShearwaterAdapter(db_copy)
    dive = UnifiedDive(date_time=datetime(2026, 1, 1, 10), duration=1000, max_depth=10.0)
    assert adapter.add_dive(dive) is None
    assert adapter.delete_dive("881855471760887268") is False
    assert len(ShearwaterAdapter(db_copy).fetch_dives()) == 27                # nothing changed
    assert "was not added" in caplog.text and "never deleted" in caplog.text
    # not a database / missing file
    assert ShearwaterAdapter(str(tmp_path / "missing.db")).login() is False
    bad = tmp_path / "bad.db"; bad.write_text("hello")
    assert ShearwaterAdapter(str(bad)).login() is False


def test_catalogue_covers_every_editable_column():
    specs = {f.name: f for f in ShearwaterAdapter.field_catalog()}
    assert all(f.key.startswith("shearwater.") for f in specs.values())
    from src.core.services.shearwater import COLUMNS
    for name in COLUMNS:
        assert name in specs, name
    assert specs["air_temperature"].type == "number" and specs["environment"].type == "text"
    assert specs["site"].label == "Site" and specs["site"].unified == "location"       # the app's Site = the dive site
    assert specs["location"].unified is None and "area" in specs["location"].label     # the app's Location = the area
    assert len({f.key.lower() for f in specs.values()}) == len(specs)                   # no two keys differing by case
    assert specs["tanks"].type == "tanks" and specs["gps"].type == "gps"
    for name in ("date_time", "duration", "max_depth", "avg_depth", "temp_min", "temp_max", "tanks"):
        assert not specs[name].writable, name                                 # the computer's data
    for name in ("buddy", "notes", "site", "location", "dive_number", "weight", "gps", "environment", "air_temperature", "other1"):
        assert specs[name].writable, name                                     # what the app lets you edit
    from src.core.services.shearwater import NAME_OF_COLUMN
    for column, options in DROPDOWN_OPTIONS.items():
        assert NAME_OF_COLUMN[column] in specs and len(options) == len(set(options))


def test_pairs_wiring(tmp_path, monkeypatch):
    from src.core import pairs
    from src.core.config import SettingsModel
    assert pairs.parse_service_spec("shearwater:/x/dive_data.db") == ("shearwater", "/x/dive_data.db")
    assert pairs.parse_service_spec("shearwater") == ("shearwater", None)      # the configured account's file
    assert pairs.display_name_of("shearwater:x.db") == pairs.display_name_of("shearwater") == "Shearwater app"
    assert {f.key for f in pairs.field_catalog_of("shearwater")} == {f.key for f in ShearwaterAdapter.field_catalog()}
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    adapter = pairs.build_adapter("shearwater:dive_data.db", SettingsModel())
    assert isinstance(adapter, ShearwaterAdapter) and adapter.path == str(tmp_path / "dive_data.db")
    links = pairs.default_links_for("shearwater", "uddf")
    assert links and all(l.target.startswith("uddf.") for l in links)
    # the shipped boards name the site fields (Site = dive site, Location = area)
    by_id = {l.id: l for l in pairs.default_links_for("garmin", "shearwater")}
    assert (by_id["site"].source, by_id["site"].target) == (["garmin.locationName"], "shearwater.site")
    by_id = {l.id: l for l in pairs.default_links_for("shearwater", "divelogs")}
    assert (by_id["site"].source, by_id["site"].target) == (["shearwater.site"], "divelogs.divesite")
    assert (by_id["area"].source, by_id["area"].target) == (["shearwater.location"], "divelogs.location")
    by_id = {l.id: l for l in pairs.default_links_for("subsurface", "shearwater")}
    assert (by_id["site"].source, by_id["site"].target) == (["subsurface.location"], "shearwater.site")


def test_engine_creates_the_dives_on_a_uddf_file(db_copy, tmp_path):
    """Shearwater as the source of a run: every dive is created on the other
    side with its computer data and gases; a second run creates nothing."""
    from src.core.config import ConfigManager, SettingsModel, SyncFilters
    from src.core.services.uddf import UddfAdapter
    from src.core.sync_engine import SyncEngine
    path = str(tmp_path / "settings.json")
    ConfigManager.save_settings(SettingsModel(directionality="to_uddf", sync_filters=SyncFilters(only_new=False),
                                              api_cooldown_seconds=0.0), path)
    out = str(tmp_path / "out.uddf")
    engine = SyncEngine(settings_path=path, credentials_path=str(tmp_path / "c.json"),
                        source_adapter=ShearwaterAdapter(db_copy), target_adapter=UddfAdapter(out))
    res = engine.run_sync(dry_run=False)
    assert len(res["uploaded_to_uddf"]) == 27 and res["uploaded_to_shearwater"] == []
    written = _by_number(UddfAdapter(out).fetch_dives())
    assert (written[452].max_depth, written[452].duration, written[452].location) == (25.3, 1788, "Site 452")
    assert written[452].gas_mixtures[0].start_pressure == 202.57
    profile = written[452].samples                                     # the UDDF file gets the profile
    assert len(profile) == 927 and profile[0].time == 2 and max(s.depth for s in profile) == 25.3
    again = SyncEngine(settings_path=path, credentials_path=str(tmp_path / "c.json"),
                       source_adapter=ShearwaterAdapter(db_copy), target_adapter=UddfAdapter(out))
    res = again.run_sync(dry_run=False)
    assert res["uploaded_to_uddf"] == [] and res["uploaded_to_shearwater"] == []


def test_profile_failures_leave_the_dive_without_samples(db_copy, caplog, monkeypatch):
    """A profile never makes loading a dive fail (H5b): a blob the decoder
    rejects, another log format, a missing log row or any other error give
    the dive without samples and one warning naming it."""
    import sqlite3
    from src.core.services import shearwater as sw
    conn = sqlite3.connect(db_copy)
    conn.execute("UPDATE log_data SET data_bytes_1 = ? WHERE log_id = ?", (b"\x10\x00\x00\x00not a gzip stream", "881855471760887268"))
    conn.execute("UPDATE log_data SET format = 'sw-legacy' WHERE log_id = (SELECT DiveId FROM dive_details WHERE DiveNumber = '433')")
    conn.execute("DELETE FROM log_data WHERE log_id = (SELECT DiveId FROM dive_details WHERE DiveNumber = '427')")
    conn.commit()
    conn.close()
    adapter = ShearwaterAdapter(db_copy)
    with caplog.at_level(logging.WARNING):
        dives = _by_number(adapter.fetch_dives())
    assert len(dives) == 27 and sum(1 for d in dives.values() if d.samples) == 24
    assert dives[452].samples == [] and (dives[452].max_depth, dives[452].duration) == (25.3, 1788)   # the header data stays
    assert dives[433].samples == [] and dives[427].samples == []
    messages = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Shearwater: dive")]
    assert len(messages) == 3
    assert any("881855471760887268: no profile - profile blob is not a gzip stream" in m for m in messages)
    assert any("log format 'sw-legacy' is not sw-pnf" in m for m in messages)
    assert any("has no log in log_data; no profile" in m for m in messages)
    # an unexpected error inside the decoder is a warning too
    monkeypatch.setattr(sw, "decode_profile", lambda blob: (_ for _ in ()).throw(RuntimeError("boom")))
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        dives = _by_number(adapter.fetch_dives())
    assert len(dives) == 27 and not any(d.samples for d in dives.values())
    assert sum(1 for r in caplog.records if "no profile - unexpected RuntimeError: boom" in r.getMessage()) == 25   # every dive with an sw-pnf blob


def test_profile_on_the_board_and_in_the_other_services(db_copy, tmp_path):
    """``shearwater.samples`` is read-only in the catalogue; the shipped
    boards send it to Subsurface, Divelogs and UDDF and never into
    Shearwater (the log is the computer's). Divelogs takes the samples on
    its grid without a pressure slot; Subsurface keeps them as they are
    with the transmitter's pressure on sensor 0."""
    from src.core import pairs
    from src.core.services.divelogs import DivelogsAdapter
    from src.core.services.subsurface import SubsurfaceAdapter
    specs = {f.name: f for f in ShearwaterAdapter.field_catalog()}
    assert (specs["samples"].type, specs["samples"].unified, specs["samples"].writable) == ("samples", "samples", False)
    for other in ("subsurface", "divelogs", "uddf"):
        by_id = {l.id: l for l in pairs.default_links_for("shearwater", other)}
        assert (by_id["samples"].source, by_id["samples"].target, by_id["samples"].direction, by_id["samples"].conflict) == \
            (["shearwater.samples"], f"{other}.samples", "to_target", "prefer_non_empty")
        assert "samples" not in {l.id for l in pairs.default_links_for(other, "shearwater")}
    dive = ShearwaterAdapter(db_copy).fetch_dive("881855471760887268")

    divelogs = DivelogsAdapter("dummy", "dummy")
    divelogs.imperial_units = False
    payload = divelogs._map_from_unified(dive)
    assert payload["samplerate"] == 2 and len(payload["sampledata"]) == 927
    assert payload["sampledata"][0] == {"d": 1.0, "t": 30.0} and payload["sampledata"][-1] == {"d": 0.0, "t": 30.0}

    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    subsurface = SubsurfaceAdapter(repo)
    assert subsurface.login()
    new_id = subsurface.add_dive(dive)
    assert new_id == "2025/10/19-Sun-15=21=08/Dive-452"
    computer = open(os.path.join(repo, "2025", "10", "19-Sun-15=21=08", "Divecomputer")).read()
    assert "\n  0:02 1.0m 30.0°C 202.57bar:0\n" in computer and "\n 30:54 0.0m 30.0°C 111.28bar:0" in computer
    assert computer.count("bar:0") == 927
    back = {d.external_ids["subsurface"]: d for d in subsurface.fetch_dives()}[new_id]
    assert len(back.samples) == 927 and back.samples[0].pressure == 202.57 and back.samples[-1].depth == 0.0
    assert (back.gas_mixtures[0].start_pressure, back.gas_mixtures[0].end_pressure) == (202.57, 112.52)


# ---------------------------------------------------------------- H2: finding the app's file, boards, endpoints

def _fake_install(tmp_path, monkeypatch, accounts, active=None):
    """A users/ folder like the app's, with a dive_data.db per account."""
    import src.core.services.shearwater as shearwater
    users = tmp_path / "users"
    for account in accounts:
        (users / account).mkdir(parents=True)
        shutil.copy(FIXTURE, users / account / "dive_data.db")
    if active is not None:
        (users / "active_account").write_text(active)
    monkeypatch.setattr(shearwater, "_users_dirs", lambda: [str(users)])
    return users


def test_find_live_databases(tmp_path, monkeypatch):
    from src.core.services.shearwater import find_live_database, find_live_databases, account_of_path
    assert find_live_databases() == [] and find_live_database() is None      # conftest: nothing on this machine
    users = _fake_install(tmp_path, monkeypatch, ["a@x", "b@x"], active="b@x")
    (users / "loadinguser").mkdir()
    shutil.copy(FIXTURE, users / "loadinguser" / "dive_data.db")
    assert find_live_databases() == [("b@x", str(users / "b@x" / "dive_data.db")),
                                     ("a@x", str(users / "a@x" / "dive_data.db"))]   # the active account first, the placeholder ignored
    assert find_live_database() == str(users / "b@x" / "dive_data.db")
    (users / "active_account").unlink()
    assert [a for a, _ in find_live_databases()] == ["a@x", "b@x"]            # no marker: alphabetical
    assert account_of_path(str(users / "a@x" / "dive_data.db")) == "a@x"
    assert account_of_path("/tmp/copies/dive_data.db") == "copies"


def test_shearwater_accounts_in_the_credentials(tmp_path, monkeypatch):
    from src.core.config import CredentialsModel, ShearwaterCredentials
    creds = CredentialsModel()
    assert creds.get_shearwater_accounts() == [] and creds.configured_specs() == []
    # nothing saved: the app's active account on this computer is the one
    users = _fake_install(tmp_path, monkeypatch, ["live@x", "test@x"], active="test@x")
    live = creds.get_shearwater_accounts()
    assert [a.name for a in live] == ["test@x"] and live[0].database == str(users / "test@x" / "dive_data.db")
    assert creds.configured_services() == ["shearwater"] and creds.configured_specs() == ["shearwater"]
    assert creds.saved_shearwater_accounts() == []
    # saved databases: both accounts, a copy with its own label, a missing file
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    shutil.copy(FIXTURE, tmp_path / "copy.db")
    creds = CredentialsModel(shearwater=[
        ShearwaterCredentials(database=str(users / "live@x" / "dive_data.db")),
        ShearwaterCredentials(database=str(users / "test@x" / "dive_data.db")),
        ShearwaterCredentials(database="copy.db", account="Backup copy"),
        ShearwaterCredentials(database="missing.db"),
    ])
    assert [a.name for a in creds.saved_shearwater_accounts()] == ["live@x", "test@x", "Backup copy", tmp_path.name]
    assert [a.name for a in creds.get_shearwater_accounts()] == ["live@x", "test@x", "Backup copy"]
    assert creds.get_shearwater_accounts()[2].resolved_path() == str(tmp_path / "copy.db")
    # the single form the web UI writes
    single = CredentialsModel(shearwater=ShearwaterCredentials(database="copy.db"))
    assert [a.name for a in single.get_shearwater_accounts()] == [tmp_path.name]


def test_boards_endpoints_and_the_account_pick(tmp_path, monkeypatch):
    import src.core.config as config
    from src.core.config import CredentialsModel, GarminCredentials, SettingsModel, ShearwaterCredentials, SyncPairModel
    from src.core import pairs
    from src.core.pairs import board_pairs, build_adapter, configured_specs, engine_for, sync_endpoints, adapter_account
    assert pairs.parse_service_spec("shearwater") == ("shearwater", None)
    assert pairs.parse_service_spec("shearwater:/x/dive_data.db") == ("shearwater", "/x/dive_data.db")
    assert configured_specs(["garmin", "shearwater", "subsurface", "shearwater:/x.db", "bogus"]) == \
        ["garmin", "shearwater", "subsurface-cloud", "shearwater:/x.db"]
    settings = SettingsModel()
    combos = [(b["id"], b["source"], b["target"]) for b in board_pairs(settings, ["garmin", "divelogs", "subsurface", "shearwater"]) if not b["saved"]]
    assert ("garmin_shearwater", "garmin", "shearwater") in combos
    assert ("divelogs_shearwater", "divelogs", "shearwater") in combos
    assert ("subsurface_shearwater", "subsurface-cloud", "shearwater") in combos
    settings.sync_pairs.append(SyncPairModel(id="sw2ss", source="shearwater", target="subsurface-cloud"))
    assert [b["id"] for b in board_pairs(settings, ["subsurface", "shearwater"])] == ["garmin_divelogs", "sw2ss"]
    assert {"spec": "shearwater", "id": "shearwater", "label": "Shearwater app"} in sync_endpoints(settings, ["shearwater"])

    # build_adapter: the account picks the file; one account needs no pick; a path names a file
    users = _fake_install(tmp_path, monkeypatch, ["live@x", "test@x"], active="live@x")
    creds = CredentialsModel(garmin=[GarminCredentials(username="g", password="p")],
                             shearwater=[ShearwaterCredentials(database=str(users / a / "dive_data.db")) for a in ("live@x", "test@x")])
    monkeypatch.setattr(config.ConfigManager, "load_credentials", staticmethod(lambda path=None: creds))
    adapter = build_adapter("shearwater", settings, shearwater_account="test@x")
    assert adapter.path == str(users / "test@x" / "dive_data.db") and adapter_account(adapter) == "test@x"
    with pytest.raises(ValueError, match="Multiple Shearwater app accounts"):
        build_adapter("shearwater", settings)
    with pytest.raises(ValueError, match="No Shearwater app account configured matching"):
        build_adapter("shearwater", settings, shearwater_account="nobody")
    assert build_adapter("shearwater:" + str(users / "live@x" / "dive_data.db"), settings).account == "live@x"
    creds.shearwater = creds.shearwater[:1]
    assert build_adapter("shearwater", settings).account == "live@x"
    import src.core.services.shearwater as sw
    monkeypatch.setattr(sw, "_users_dirs", lambda: [])                       # no app on this machine either
    monkeypatch.setattr(config.ConfigManager, "load_credentials", staticmethod(lambda path=None: CredentialsModel()))
    with pytest.raises(ValueError, match="No Shearwater app database"):
        build_adapter("shearwater", settings)
    # engine_for passes the pick through and keeps per-account state
    monkeypatch.setattr(config.ConfigManager, "load_credentials", staticmethod(lambda path=None: creds))
    from src.core.config import ConfigManager
    spath = str(tmp_path / "settings.json"); ConfigManager.save_settings(settings, spath)
    engine = engine_for("garmin", "shearwater", settings_path=spath, credentials_path=str(tmp_path / "c.json"),
                        garmin_username="g", shearwater_account="live@x", account_scoped_state=True)
    assert engine.target.path == str(users / "live@x" / "dive_data.db")
    assert engine.state_file.endswith("sync_state_garmin-g_shearwater-live@x.json")


# ---------------------------------------------------------------- H3: writing metadata the way the app does

DOTNET_MIN = -62135596800000


def _meta(path, dive_id):
    import json, sqlite3
    c = sqlite3.connect(path)
    row = c.execute("SELECT LastModifiedServerTime, FieldTimeStampJson FROM SyncV3MetadataDiveDetail WHERE Id=?", (dive_id,)).fetchone()
    detail = c.execute("SELECT * FROM dive_details WHERE DiveId=?", (dive_id,)).fetchone()
    cols = [d[0] for d in c.execute("SELECT * FROM dive_details LIMIT 0").description]
    c.close()
    return row[0], json.loads(row[1]), dict(zip(cols, detail))


def test_update_writes_columns_and_cloud_stamps(db_copy, tmp_path, monkeypatch, caplog):
    import src.core.services.shearwater as sw
    from datetime import timezone
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sw, "app_is_running", lambda: False)
    fixed = datetime(2026, 9, 30, 1, 2, 3, tzinfo=timezone.utc)
    monkeypatch.setattr(sw, "_now", lambda: fixed)
    adapter = ShearwaterAdapter(db_copy)
    dive_id = "881855471760954690"                           # dive 454, nothing typed in yet
    before_lmst, before_stamps, before_row = _meta(db_copy, dive_id)
    assert before_stamps["Buddy"] == DOTNET_MIN and before_row["Buddy"] is None

    dive = adapter.fetch_dive(dive_id)
    dive.buddy, dive.notes, dive.location = "Anna", "Great viz", "House Reef"
    dive.dive_number = 500
    dive.weight, dive.weight_unit = 10.0, "pound"
    dive.lat, dive.lng = 5.123456, 103.654321
    dive.service_fields.update({"location": "Tenggol", "environment": "ocean/sea", "weather": "Hailstorm",
                                "air_temperature": 30.0, "symptoms": "none"})
    assert adapter.update_dive(dive_id, dive) is True

    lmst, stamps, row = _meta(db_copy, dive_id)
    assert (row["Buddy"], row["Notes"], row["Site"], row["Location"]) == ("Anna", "Great viz", "House Reef", "Tenggol")
    assert (row["DiveNumber"], row["Weight"], row["GnssEntryLocation"]) == ("500", "4.54", "5.123456,103.654321")
    assert (row["Environment"], row["AirTemperature"], row["Symptoms"]) == ("Ocean/Sea", "30", "none")   # case-fixed to the app's label
    assert row["Weather"] is None and "Hailstorm" in caplog.text                                          # not an option: skipped
    assert row["LastModified"] == "2026-09-30 01:02:03" and lmst == "2026-09-30 01:02:03"
    ms = int(fixed.timestamp() * 1000)
    for column in ("Buddy", "Notes", "Site", "Location", "DiveNumber", "Weight", "GnssEntryLocation", "Environment", "AirTemperature", "Symptoms"):
        assert stamps[column] == ms, column
    assert stamps["Weather"] == DOTNET_MIN and stamps["Conditions"] == DOTNET_MIN                        # untouched columns keep their stamps
    assert stamps["TankProfileData"] == before_stamps["TankProfileData"]
    # the computer's data and the tank columns are never touched
    for column in ("DiveDate", "Depth", "DiveLengthTime", "SerialNumber", "TankProfileData", "Tank1PressureStart"):
        assert row[column] == before_row[column], column
    # reading back agrees, and writing the same again changes nothing
    again = adapter.fetch_dive(dive_id)
    assert (again.buddy, again.dive_number, again.weight, again.lat, again.service_fields["environment"]) == ("Anna", 500, 4.54, 5.123456, "Ocean/Sea")
    monkeypatch.setattr(sw, "_now", lambda: datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert adapter.update_dive(dive_id, again) is True
    assert _meta(db_copy, dive_id)[2]["LastModified"] == "2026-09-30 01:02:03"
    # one backup copy per adapter, under DATA_DIR/backups/shearwater
    backups = os.listdir(tmp_path / "backups" / "shearwater")
    assert backups == ["20260930T010203-dive_data.db"]
    # clearing a field
    again.buddy = None
    assert adapter.update_dive(dive_id, again) is True
    assert _meta(db_copy, dive_id)[2]["Buddy"] is None


def test_update_refuses_while_the_app_may_write(db_copy, tmp_path, monkeypatch):
    import src.core.services.shearwater as sw
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    adapter = ShearwaterAdapter(db_copy)
    dive = adapter.fetch_dive("881855471760954690")
    dive.buddy = "Anna"
    monkeypatch.setattr(sw, "app_is_running", lambda: True)
    assert adapter.update_dive("881855471760954690", dive) is False
    monkeypatch.setattr(sw, "app_is_running", lambda: False)
    journal = db_copy + "-journal"
    open(journal, "w").close()
    assert adapter.update_dive("881855471760954690", dive) is False
    os.remove(journal)
    assert adapter.update_dive("881855471760954690", dive) is True
    assert adapter.update_dive("no-such-dive", dive) is False
    assert not os.path.exists(tmp_path / "backups" / "shearwater" / "x") and len(os.listdir(tmp_path / "backups" / "shearwater")) == 1


def test_update_gives_an_unsynced_dive_its_bookkeeping_row(db_copy, tmp_path, monkeypatch):
    import sqlite3
    import src.core.services.shearwater as sw
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sw, "app_is_running", lambda: False)
    dive_id = "881855471760954690"
    c = sqlite3.connect(db_copy); c.execute("DELETE FROM SyncV3MetadataDiveDetail WHERE Id=?", (dive_id,)); c.commit(); c.close()
    adapter = ShearwaterAdapter(db_copy)
    dive = adapter.fetch_dive(dive_id)
    dive.notes = "typed by dive_sync"
    assert adapter.update_dive(dive_id, dive) is True
    lmst, stamps, row = _meta(db_copy, dive_id)
    assert row["Notes"] == "typed by dive_sync" and list(stamps) == ["Notes"] and lmst == row["LastModified"]


def test_engine_writes_garmin_metadata_into_the_app(db_copy, tmp_path, monkeypatch, caplog):
    """Garmin -> Shearwater with the shipped board: buddy and notes land in
    the app's columns with their stamps; the matched dive is not re-created
    anywhere."""
    import src.core.services.shearwater as sw
    from src.core.config import ConfigManager, SettingsModel, SyncFilters
    from src.core.fields import common_default_links
    from src.core.sync_engine import SyncEngine
    from tests.test_link_engine import FakeGarmin, _dive
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sw, "app_is_running", lambda: False)
    path = str(tmp_path / "settings.json")
    ConfigManager.save_settings(SettingsModel(directionality="to_shearwater", sync_filters=SyncFilters(only_new=False),
                                              api_cooldown_seconds=0.0,
                                              field_links=common_default_links("garmin", "shearwater")), path)
    garmin = [_dive(date_time=datetime(2025, 10, 20, 10, 4, 50), external_ids={"garmin": "g454"}, dive_number=454,
                    buddy="Anna", notes="From Garmin", duration=2831, max_depth=21.1),
              # two dives Shearwater does not have: left alone, said once, not an error per dive
              _dive(date_time=datetime(1992, 6, 13, 23, 50), external_ids={"garmin": "g1"}, dive_number=1),
              _dive(date_time=datetime(1993, 8, 28, 15, 29), external_ids={"garmin": "g2"}, dive_number=2)]
    engine = SyncEngine(settings_path=path, credentials_path=str(tmp_path / "c.json"),
                        source_adapter=FakeGarmin(garmin), target_adapter=ShearwaterAdapter(db_copy))
    import logging
    with caplog.at_level(logging.INFO):
        res = engine.run_sync(dry_run=False)
    assert res["matched_count"] == 1 and res["uploaded_to_shearwater"] == [] and res["uploaded_to_garmin"] == []
    assert len(res["updated_on_shearwater"]) == 1
    assert [s["reason"] for s in res["skipped"]] == ["receiver_takes_no_new_dives"] * 2
    assert "ERROR" not in caplog.text and "not added" not in caplog.text
    assert caplog.text.count("Garmin Connect has 2 dive(s) that Shearwater app does not have (1992-06-13 to 1993-08-28)") == 1
    _, stamps, row = _meta(db_copy, "881855471760954690")
    assert (row["Buddy"], row["Notes"]) == ("Anna", "From Garmin") and stamps["Buddy"] > 0 and stamps["Notes"] > 0
