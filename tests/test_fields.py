"""Track C phase 1: field catalogue, field links, defaults and migration."""
import json
import os
from datetime import datetime

import pytest
from pydantic import ValidationError

from src.core.config import ConfigManager, CronJobModel, SettingsModel
from src.core.fields import (
    FieldLink,
    FieldSpec,
    build_catalog,
    convert_value,
    copy_value,
    default_field_links,
    get_field,
    is_empty,
    match_key_equal,
    set_field,
    validate_field_links,
    values_equal,
)
from src.core.models import GasMixture, UnifiedDive, UnifiedSample
from src.core.services.divelogs import DivelogsAdapter
from src.core.services.garmin import GarminAdapter
from src.core.services.mock_adapters import LocalMockDivelogsAdapter, LocalMockGarminAdapter


def _catalog():
    return build_catalog(GarminAdapter.field_catalog(), DivelogsAdapter.field_catalog())


# ---------------------------------------------------------------- models

def test_field_spec_key_must_have_service_prefix():
    FieldSpec(key="garmin.buddy", label="Buddy", type="text")
    with pytest.raises(ValidationError):
        FieldSpec(key="buddy", label="Buddy", type="text")
    spec = FieldSpec(key="divelogs.divesite", label="Site", type="text")
    assert spec.service_id == "divelogs"
    assert spec.name == "divesite"


def test_field_link_structure_rules():
    FieldLink(id="a", source=["garmin.buddy"], target="divelogs.buddy")
    with pytest.raises(ValidationError, match="needs a template"):
        FieldLink(id="c", source=["divelogs.location", "divelogs.divesite"], target="garmin.activityName")
    with pytest.raises(ValidationError, match="one-way"):
        FieldLink(id="c", source=["divelogs.location", "divelogs.divesite"], target="garmin.activityName",
                  template="{divelogs.location} {divelogs.divesite}", direction="bidirectional")
    with pytest.raises(ValidationError, match="cannot be a match key"):
        FieldLink(id="c", source=["divelogs.location", "divelogs.divesite"], target="garmin.activityName",
                  template="x", direction="to_target", match_order=1)
    with pytest.raises(ValidationError, match="also a source"):
        FieldLink(id="d", source=["garmin.buddy"], target="garmin.buddy")
    with pytest.raises(ValidationError):
        FieldLink(id="e", source=[], target="garmin.buddy")
    with pytest.raises(ValidationError):
        FieldLink(id="f", source=["garmin.buddy"], target="divelogs.buddy", direction="sideways")


# ---------------------------------------------------------------- catalogue

def test_adapters_declare_service_ids_and_catalogues():
    assert GarminAdapter.service_id == "garmin"
    assert DivelogsAdapter.service_id == "divelogs"
    assert LocalMockGarminAdapter.service_id == "garmin"
    assert LocalMockDivelogsAdapter.service_id == "divelogs"
    assert LocalMockGarminAdapter.field_catalog() == GarminAdapter.field_catalog()

    catalog = _catalog()
    for key in ("garmin.buddy", "garmin.gps", "garmin.tanks", "garmin.activityName", "garmin.locationName",
                "divelogs.buddy", "divelogs.location", "divelogs.divesite", "divelogs.tanks", "divelogs.samples"):
        assert key in catalog, key
    # Everything a catalogue declares as unified must exist on UnifiedDive
    # (or be one of the tuple/alias fields get_field knows about).
    dive = UnifiedDive(date_time=datetime(2026, 1, 1), duration=1, max_depth=1.0)
    for spec in catalog.values():
        get_field(dive, spec)  # must not raise
    # Garmin cannot write gases or samples; Divelogs writes everything
    assert catalog["garmin.tanks"].writable is False
    assert catalog["garmin.samples"].writable is False
    assert all(spec.writable for spec in DivelogsAdapter.field_catalog())


def test_default_links_validate_against_catalogue():
    links = default_field_links()
    assert validate_field_links(links, _catalog()) == []
    ids = [l.id for l in links]
    assert len(ids) == len(set(ids))
    by_id = {l.id: l for l in links}
    # Today's behaviour, spelled out
    assert by_id["buddy"].direction == "bidirectional" and by_id["buddy"].conflict == "source_wins"
    assert by_id["gps"].direction == "to_target"
    assert by_id["gps_fill"].source == ["divelogs.gps"] and by_id["gps_fill"].conflict == "prefer_non_empty"
    assert by_id["tanks"].direction == "to_target" and by_id["samples"].direction == "to_target"
    assert by_id["dive_number"].direction == "off" and by_id["dive_number"].match_order == 1
    assert by_id["site_to_garmin"].is_composite and by_id["site_to_garmin"].direction == "off"


def test_validate_field_links_reports_problems():
    catalog = _catalog()
    problems = validate_field_links([
        FieldLink(id="x", source=["garmin.nope"], target="divelogs.buddy"),
        FieldLink(id="y", source=["garmin.buddy"], target="divelogs.max_depth"),
        FieldLink(id="z", source=["divelogs.tanks"], target="garmin.tanks", direction="to_target"),
        FieldLink(id="w", source=["garmin.tanks"], target="divelogs.tanks", direction="bidirectional"),
        FieldLink(id="m", source=["garmin.buddy"], target="divelogs.buddy", match_order=1),
        FieldLink(id="m", source=["garmin.notes"], target="divelogs.notes"),
        FieldLink(id="c", source=["garmin.tanks", "garmin.buddy"], target="divelogs.notes", template="t", direction="to_target"),
    ], catalog)
    text = "\n".join(problems)
    assert "Link 'x': unknown field(s) garmin.nope" in text
    assert "cannot link garmin.buddy (text) to divelogs.max_depth (number)" in text
    assert "garmin.tanks cannot be written" in text
    assert "tanks links are one-way" in text
    assert "only number or datetime fields can be match keys" in text
    assert "duplicate link id" in text
    assert "cannot be used inside a template" in text
    # list <-> text is the one allowed mixed link
    cat = dict(catalog)
    cat["x.tags"] = FieldSpec(key="x.tags", label="Tags", type="list")
    assert validate_field_links([FieldLink(id="t", source=["x.tags"], target="divelogs.notes")], cat) == []


# ---------------------------------------------------------------- values

def test_get_set_field_shapes():
    catalog = _catalog()
    dive = UnifiedDive(date_time=datetime(2026, 1, 1), duration=1, max_depth=1.0,
                       lat=1.5, lng=2.5, weight=5.0, weight_unit="kilogram", buddy="Bo",
                       gas_mixtures=[GasMixture(oxygen=32.0)],
                       service_fields={"activityName": "Wreck", "locationName": "Malmö"})
    assert get_field(dive, catalog["garmin.gps"]) == (1.5, 2.5)
    assert get_field(dive, catalog["garmin.weight"]) == (5.0, "kilogram")
    assert get_field(dive, catalog["garmin.buddy"]) == "Bo"
    assert get_field(dive, catalog["garmin.tanks"])[0].oxygen == 32.0
    assert get_field(dive, catalog["garmin.activityName"]) == "Wreck"
    assert get_field(dive, catalog["garmin.locationName"]) == "Malmö"
    assert get_field(dive, catalog["divelogs.divesite"]) is None

    set_field(dive, catalog["garmin.gps"], (3.0, 4.0))
    assert (dive.lat, dive.lng) == (3.0, 4.0)
    set_field(dive, catalog["garmin.gps"], None)
    assert (dive.lat, dive.lng) == (None, None)
    set_field(dive, catalog["garmin.weight"], (6.0, "pound"))
    assert (dive.weight, dive.weight_unit) == (6.0, "pound")
    set_field(dive, catalog["divelogs.divesite"], "Reef")
    assert dive.service_fields["divesite"] == "Reef"
    set_field(dive, catalog["divelogs.tanks"], [GasMixture(oxygen=21.0)])
    assert dive.gas_mixtures[0].oxygen == 21.0


def test_value_helpers():
    assert values_equal("text", None, "None") and values_equal("text", " a ", "a")
    assert not values_equal("text", "a", "b")
    assert values_equal("tanks", [GasMixture(oxygen=21, tank_name="x")], [GasMixture(oxygen=21, tank_name="y")])
    assert not values_equal("tanks", [GasMixture(oxygen=21)], [GasMixture(oxygen=32)])
    assert is_empty("text", "None") and is_empty("gps", (None, 3.0)) and is_empty("number", (None, None))
    assert not is_empty("number", 0) and not is_empty("gps", (1.0, 2.0)) and is_empty("tanks", [])
    copied = copy_value("tanks", [GasMixture(oxygen=32, helium=10, start_pressure=200, tank_name="left")])
    assert copied[0].oxygen == 32 and copied[0].tank_name is None  # names dropped, as the old loop did
    samples = [UnifiedSample(depth=1.0)]
    assert copy_value("samples", samples) == samples and copy_value("samples", samples) is not samples
    assert convert_value(["a", "b"], "list", "text") == "a, b"
    assert convert_value("a, b", "text", "list") == ["a", "b"]
    assert convert_value("x", "text", "text") == "x"
    with pytest.raises(ValueError):
        convert_value(1, "number", "text")
    assert match_key_equal("number", 5, "5.0") and not match_key_equal("number", 0, 0)
    assert not match_key_equal("number", 5, None) and not match_key_equal("number", "a", "a")
    assert match_key_equal("datetime", datetime(2026, 1, 1), datetime(2026, 1, 1))
    assert not match_key_equal("text", "a", "a")


# ---------------------------------------------------------------- settings / migration

def test_settings_default_field_links_and_round_trip(tmp_path):
    settings = SettingsModel()
    assert [l.id for l in settings.field_links] == [l.id for l in default_field_links()]
    path = str(tmp_path / "settings.json")
    ConfigManager.save_settings(settings, path)
    loaded = ConfigManager.load_settings(path)
    assert loaded == settings
    raw = json.load(open(path))
    assert raw["field_links"][0]["id"] == "buddy"


def test_old_settings_file_loads_unchanged_with_default_links(tmp_path):
    """A settings.json written before Track C has no field_links key; it must
    load with every old value intact and the default board attached."""
    old = {
        "directionality": "to_divelogs",
        "sync_filters": {"date_from": "2026-01-01", "date_to": None, "only_new": False, "sync_gases": False, "sync_fit": True},
        "grace_window_minutes": 20,
        "api_cooldown_seconds": 2.5,
        "schedule": [{"hour": 3, "minute": 15}],
        "cron_jobs": [{
            "id": "nightly", "directionality": "to_garmin", "frequency": "daily", "hour": 1, "minute": 2,
            "day_of_week": 0, "interval_minutes": 60, "only_new": True, "sync_gases": True, "sync_fit": False, "enabled": True
        }],
    }
    path = str(tmp_path / "settings.json")
    with open(path, "w") as f:
        json.dump(old, f)
    loaded = ConfigManager.load_settings(path)
    assert loaded.directionality == "to_divelogs"
    assert loaded.sync_filters.model_dump() == old["sync_filters"]
    assert loaded.grace_window_minutes == 20 and loaded.api_cooldown_seconds == 2.5
    assert loaded.schedule[0].hour == 3
    assert loaded.cron_jobs[0].id == "nightly" and loaded.cron_jobs[0].field_links is None
    assert loaded.field_links == default_field_links()
    # and every old key survives a save
    ConfigManager.save_settings(loaded, path)
    saved = json.load(open(path))
    for key, value in old.items():
        if key == "cron_jobs":
            assert saved[key][0]["id"] == "nightly"
        else:
            assert saved[key] == value


def test_cron_job_accepts_link_override():
    job = CronJobModel(id="j", field_links=[{"id": "buddy", "source": ["garmin.buddy"], "target": "divelogs.buddy", "direction": "off"}])
    assert job.field_links[0].direction == "off"
    assert CronJobModel(id="k").model_dump()["field_links"] is None


# ---------------------------------------------------------------- service fields through the adapters

def test_adapters_fill_and_push_service_fields():
    g = GarminAdapter("dummy", "dummy")
    dive = g._map_to_unified({}, {
        "activityId": 1, "activityName": "Wreck dive", "locationName": "Malmö",
        "summaryDTO": {"startTimeLocal": "2026-06-22T12:00:00", "duration": 100, "maxDepth": 10.0},
    })
    assert dive.service_fields == {"activityName": "Wreck dive", "locationName": "Malmö"}
    # The unified site name is still filled for uploads; the declarative
    # garmin_to_divelogs.json overlay composes it as "locationName, activityName".
    assert dive.location == "Malmö, Wreck dive"
    payload = g._map_from_unified(dive)
    assert payload["activityName"] == "Wreck dive"

    d = DivelogsAdapter("dummy", "dummy")
    dive = d._map_to_unified({"date": "2026-06-22", "time": "12:00:00", "duration": 100, "maxdepth": 10.0,
                              "location": "Malmö, Sweden", "divesite": "Ön"})
    assert dive.service_fields == {"location": "Malmö, Sweden", "divesite": "Ön"}
    assert dive.location == "Malmö, Sweden, Ön"
    payload = d._map_from_unified(dive)
    # native fields round-trip exactly, no comma-splitting of the joined name
    assert (payload["location"], payload["divesite"]) == ("Malmö, Sweden", "Ön")

    # a dive from the other service still goes through the old comma split
    foreign = UnifiedDive(date_time=datetime(2026, 6, 22, 12), duration=100, max_depth=10.0, location="Malmö, Ön")
    payload = d._map_from_unified(foreign)
    assert (payload["location"], payload["divesite"]) == ("Malmö", "Ön")
    payload = g._map_from_unified(foreign)
    assert payload["activityName"] == "Malmö, Ön"
