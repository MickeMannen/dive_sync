"""The unified models (src/core/models.py) and the fields that are carried,
not synced (plans/convert.md I1): per-sample channels, dive events, the dive
computer and its settings.

Two promises are pinned here. A dive without the carried data serialises
byte for byte as it did before those fields existed (the caches, the pre-sync
backups and conflicts.json all store ``model_dump(mode="json")``), and a dive
with them comes back from JSON exactly as it went in."""
import json
from datetime import datetime

import pytest

from src.core import models
from src.core.models import (
    CARRIED_DIVE_FIELDS,
    EVENT_ALERT,
    EVENT_GAS_SWITCH,
    EVENT_SETPOINT_CHANGE,
    KNOWN_DIVE_MODES,
    KNOWN_EVENT_TYPES,
    KNOWN_WATER_TYPES,
    DiveEvent,
    GasMixture,
    SampleChannels,
    UnifiedDive,
    UnifiedSample,
)
from src.core.pairs import field_catalog_of

# The keys a UnifiedDive / UnifiedSample had before I1, in their order.
LEGACY_DIVE_KEYS = [
    "date_time", "date_time_utc", "timezone", "duration", "max_depth", "avg_depth", "temp_min", "temp_max",
    "temp_avg", "external_ids", "gas_mixtures", "location", "notes", "dive_number", "weight", "weight_unit",
    "visibility", "visibility_unit", "buddy", "lat", "lng", "samples", "device_logged", "service_fields",
]
LEGACY_SAMPLE_KEYS = ["depth", "temp", "time", "pressure"]

# What json.dump(dive.model_dump(mode="json"), f, indent=2) wrote for
# legacy_dive() before I1, captured from the code as it was then. This is the
# text of a cache file and of one entry of a backup file.
LEGACY_DIVE_JSON = """{
  "date_time": "2026-08-29T10:15:00",
  "date_time_utc": "2026-08-29T02:15:00",
  "timezone": "Asia/Kuala_Lumpur",
  "duration": 4129,
  "max_depth": 18.3,
  "avg_depth": 8.196,
  "temp_min": 27.0,
  "temp_max": 29.0,
  "temp_avg": 28.0,
  "external_ids": {
    "submersion": "1E5A-9",
    "garmin": "123"
  },
  "gas_mixtures": [
    {
      "oxygen": 32.0,
      "helium": 0.0,
      "start_pressure": 162.57,
      "end_pressure": 78.04,
      "tank_volume": 11.1,
      "tank_name": "Left",
      "tank_role": "sidemountLeft"
    },
    {
      "oxygen": 21.0,
      "helium": 0.0,
      "start_pressure": null,
      "end_pressure": null,
      "tank_volume": null,
      "tank_name": null,
      "tank_role": null
    }
  ],
  "location": "Fixture Bay",
  "notes": "drift",
  "dive_number": 42,
  "weight": 6.0,
  "weight_unit": "kilogram",
  "visibility": 20.0,
  "visibility_unit": "meter",
  "buddy": "Ann",
  "lat": 7.6,
  "lng": 98.37,
  "samples": [
    {
      "depth": 0.0,
      "temp": 28.0,
      "time": 0,
      "pressure": 162.57
    },
    {
      "depth": 18.3,
      "temp": null,
      "time": null,
      "pressure": null
    }
  ],
  "device_logged": true,
  "service_fields": {
    "activityName": "Fixture Bay dive"
  }
}"""

# The same for a dive with nothing but its required fields, as model_dump_json() gave it.
BARE_DIVE_JSON = (
    '{"date_time":"2026-01-02T03:04:05","date_time_utc":null,"timezone":null,"duration":60,"max_depth":5.0,'
    '"avg_depth":null,"temp_min":null,"temp_max":null,"temp_avg":null,"external_ids":{},"gas_mixtures":[],'
    '"location":null,"notes":null,"dive_number":null,"weight":null,"weight_unit":null,"visibility":null,'
    '"visibility_unit":null,"buddy":null,"lat":null,"lng":null,"samples":[],"device_logged":null,"service_fields":{}}'
)


def legacy_dive(**overrides) -> UnifiedDive:
    """A dive with every pre-I1 field set and none of the carried ones."""
    base = dict(
        date_time=datetime(2026, 8, 29, 10, 15, 0),
        date_time_utc=datetime(2026, 8, 29, 2, 15, 0),
        timezone="Asia/Kuala_Lumpur",
        duration=4129, max_depth=18.3, avg_depth=8.196,
        temp_min=27.0, temp_max=29.0, temp_avg=28.0,
        external_ids={"submersion": "1E5A-9", "garmin": "123"},
        gas_mixtures=[
            GasMixture(oxygen=32, helium=0, start_pressure=162.57, end_pressure=78.04, tank_volume=11.1,
                       tank_name="Left", tank_role="sidemountLeft"),
            GasMixture(),
        ],
        location="Fixture Bay", notes="drift", dive_number=42,
        weight=6.0, weight_unit="kilogram", visibility=20.0, visibility_unit="meter",
        buddy="Ann", lat=7.6, lng=98.37,
        samples=[UnifiedSample(depth=0, temp=28, time=0, pressure=162.57), UnifiedSample(depth=18.3)],
        device_logged=True,
        service_fields={"activityName": "Fixture Bay dive"},
    )
    base.update(overrides)
    return UnifiedDive(**base)


def computer_dive() -> UnifiedDive:
    """What a reader of a two-transmitter dive computer file would build:
    every carried field set."""
    return legacy_dive(
        samples=[
            UnifiedSample(depth=0.0, temp=28.0, time=0, pressure=201.3,
                          channels=SampleChannels(pressures={0: 201.3, 1: 198.0}, ndl=5940, tts=60, cns=0.0,
                                                  ppo2=0.32, heart_rate=71, gas_time=7200)),
            UnifiedSample(depth=30.1, temp=26.5, time=600, pressure=150.2,
                          channels=SampleChannels(pressures={0: 150.2, 1: 197.5}, ndl=0, tts=540, deco_stop_depth=6.0,
                                                  deco_stop_time=180, cns=9.5, ppo2=1.28, setpoint=1.3, gf99=62.0)),
            UnifiedSample(depth=5.0, temp=27.5, time=1800),
        ],
        events=[
            DiveEvent(time=0, type=EVENT_GAS_SWITCH, tank=0, oxygen=32.0, helium=0.0),
            DiveEvent(time=1500, type=EVENT_GAS_SWITCH, tank=1),
            DiveEvent(time=1620, type=EVENT_ALERT, name="Safety stop begin"),
            DiveEvent(time=1700, type=EVENT_SETPOINT_CHANGE, value=1.3),
        ],
        computer_vendor="Garmin", computer_model="Descent Mk3i", computer_serial="1234567890", computer_firmware="13.05",
        gf_low=45, gf_high=85, deco_model="ZHL-16C",
        water_type="salt", water_density=1025.0, dive_mode="oc_multi_gas",
        exit_lat=7.601, exit_lng=98.371, surface_interval=5400, bottom_time=3900, cns_start=2.0, cns_end=11.0,
    )


# ---------------------------------------------------------------- nothing changes for a dive without the new data

def test_dive_without_carried_data_serialises_as_before():
    dive = legacy_dive()
    assert json.dumps(dive.model_dump(mode="json"), indent=2) == LEGACY_DIVE_JSON
    assert list(dive.model_dump()) == LEGACY_DIVE_KEYS
    assert [list(s) for s in dive.model_dump()["samples"]] == [LEGACY_SAMPLE_KEYS, LEGACY_SAMPLE_KEYS]

    bare = UnifiedDive(date_time=datetime(2026, 1, 2, 3, 4, 5), duration=60, max_depth=5.0)
    assert bare.model_dump_json() == BARE_DIVE_JSON
    assert UnifiedSample(depth=1.0).model_dump() == {"depth": 1.0, "temp": None, "time": None, "pressure": None}
    assert UnifiedSample(depth=1.0).model_dump_json() == '{"depth":1.0,"temp":null,"time":null,"pressure":null}'


def test_json_written_before_the_carried_fields_loads_and_writes_back_unchanged():
    dive = UnifiedDive(**json.loads(LEGACY_DIVE_JSON))
    assert dive == legacy_dive()
    assert dive.events == [] and all(s.channels is None for s in dive.samples)
    assert all(getattr(dive, name) in (None, []) for name in CARRIED_DIVE_FIELDS)
    assert json.dumps(dive.model_dump(mode="json"), indent=2) == LEGACY_DIVE_JSON
    assert UnifiedDive.model_validate_json(BARE_DIVE_JSON).model_dump_json() == BARE_DIVE_JSON


def test_every_field_is_either_as_before_or_carried():
    """A new UnifiedDive field has to choose: it is listed as carried (and so
    left out of the JSON when unset), or it changes what every cache file
    holds and this list is edited on purpose."""
    assert list(UnifiedDive.model_fields) == LEGACY_DIVE_KEYS + list(CARRIED_DIVE_FIELDS)
    assert list(UnifiedSample.model_fields) == LEGACY_SAMPLE_KEYS + ["channels"]
    for name in CARRIED_DIVE_FIELDS:
        assert not UnifiedDive.model_fields[name].is_required(), name
    assert not any(f.is_required() for f in SampleChannels.model_fields.values())
    assert [n for n, f in DiveEvent.model_fields.items() if f.is_required()] == ["time", "type"]


def test_empty_channels_are_the_same_as_none_on_disk():
    assert UnifiedSample(depth=1.0, channels=SampleChannels()).model_dump() == UnifiedSample(depth=1.0).model_dump()
    assert legacy_dive(events=[]).model_dump() == legacy_dive().model_dump()


# ---------------------------------------------------------------- the carried data

def test_carried_data_round_trips_through_json():
    dive = computer_dive()
    text = json.dumps(dive.model_dump(mode="json"), indent=2)
    back = UnifiedDive(**json.loads(text))
    assert back == dive
    assert back.samples[0].channels.pressures == {0: 201.3, 1: 198.0}      # JSON keys are text; the index comes back a number
    assert back.samples[2].channels is None
    assert json.dumps(back.model_dump(mode="json"), indent=2) == text
    assert UnifiedDive.model_validate_json(dive.model_dump_json()) == dive

    data = json.loads(text)
    assert list(data) == LEGACY_DIVE_KEYS + list(CARRIED_DIVE_FIELDS)        # after the keys a dive always had
    assert {k: data[k] for k in LEGACY_DIVE_KEYS if k != "samples"} == \
        {k: v for k, v in json.loads(LEGACY_DIVE_JSON).items() if k != "samples"}


def test_only_the_channels_and_event_keys_that_hold_something_are_written():
    data = computer_dive().model_dump(mode="json")
    assert data["samples"][0] == {
        "depth": 0.0, "temp": 28.0, "time": 0, "pressure": 201.3,
        "channels": {"pressures": {"0": 201.3, "1": 198.0}, "ndl": 5940, "tts": 60, "cns": 0.0, "ppo2": 0.32,
                     "gas_time": 7200, "heart_rate": 71},
    }
    assert data["samples"][1]["channels"] == {
        "pressures": {"0": 150.2, "1": 197.5}, "ndl": 0, "tts": 540, "deco_stop_depth": 6.0, "deco_stop_time": 180,
        "cns": 9.5, "ppo2": 1.28, "setpoint": 1.3, "gf99": 62.0,
    }
    assert data["samples"][2] == {"depth": 5.0, "temp": 27.5, "time": 1800, "pressure": None}
    assert data["events"] == [
        {"time": 0, "type": "gas_switch", "tank": 0, "oxygen": 32.0, "helium": 0.0},
        {"time": 1500, "type": "gas_switch", "tank": 1},
        {"time": 1620, "type": "alert", "name": "Safety stop begin"},
        {"time": 1700, "type": "setpoint_change", "value": 1.3},
    ]


def test_zero_is_a_value_not_unset():
    """NDL 0, CNS 0 %, 'no stop' and tank 0 are things a computer logs."""
    channels = SampleChannels(ndl=0, cns=0.0, deco_stop_depth=0.0, deco_stop_time=0, heart_rate=0)
    assert channels.model_dump() == {"ndl": 0, "deco_stop_depth": 0.0, "deco_stop_time": 0, "cns": 0.0, "heart_rate": 0}
    assert DiveEvent(time=0, type=EVENT_GAS_SWITCH, tank=0, helium=0.0, value=0.0).model_dump() == \
        {"time": 0, "type": "gas_switch", "tank": 0, "helium": 0.0, "value": 0.0}
    dive = legacy_dive(gf_low=0, surface_interval=0, cns_start=0.0, exit_lat=0.0, exit_lng=0.0, bottom_time=0)
    data = dive.model_dump()
    assert [k for k in data if k in CARRIED_DIVE_FIELDS] == ["gf_low", "exit_lat", "exit_lng", "surface_interval",
                                                             "bottom_time", "cns_start"]


def test_one_carried_field_adds_one_key():
    data = legacy_dive(computer_model="Perdix 2").model_dump(mode="json")
    assert list(data) == LEGACY_DIVE_KEYS + ["computer_model"] and data["computer_model"] == "Perdix 2"


def test_carried_fields_default_to_unset_and_validate_their_types():
    channels = SampleChannels()
    assert channels.pressures == {} and channels.model_dump() == {}
    assert all(getattr(channels, n) is None for n in SampleChannels.model_fields if n != "pressures")
    assert SampleChannels().pressures is not SampleChannels().pressures         # no shared default dict
    assert legacy_dive().events is not legacy_dive().events
    with pytest.raises(ValueError):
        SampleChannels(ndl="soon")
    with pytest.raises(ValueError):
        SampleChannels(pressures={"left": 200.0})                                # a tank is an index into gas_mixtures
    with pytest.raises(ValueError):
        DiveEvent(type=EVENT_ALERT)                                              # an event needs its time
    # a reader's own word for a kind, a mode or a water type is carried as it is
    assert DiveEvent(time=5, type="surface").type == "surface"
    assert legacy_dive(dive_mode="sidemount", water_type="brackish").dive_mode == "sidemount"


def test_known_vocabularies():
    assert KNOWN_EVENT_TYPES == ("gas_switch", "alert", "mode_change", "setpoint_change", "bookmark")
    assert models.EVENT_MODE_CHANGE == "mode_change" and models.EVENT_BOOKMARK == "bookmark"
    assert KNOWN_DIVE_MODES == ("oc_single_gas", "oc_multi_gas", "gauge", "ccr", "scr", "apnea")
    assert KNOWN_WATER_TYPES == ("fresh", "salt", "en13319", "custom")


def test_model_dump_options_still_apply():
    """The serialisers only leave out unset carried data; exclude_none,
    include and exclude work as on any model."""
    sample = UnifiedSample(depth=1.0, channels=SampleChannels(ndl=60))
    assert sample.model_dump(exclude_none=True) == {"depth": 1.0, "channels": {"ndl": 60}}
    assert sample.model_dump(include={"depth", "time"}) == {"depth": 1.0, "time": None}
    assert sample.model_dump(exclude={"channels"}) == {"depth": 1.0, "temp": None, "time": None, "pressure": None}
    assert UnifiedSample(depth=1.0).model_dump(exclude_none=True) == {"depth": 1.0}
    assert computer_dive().model_dump(include={"gf_low", "events"}, exclude={"events": {0, 1, 2}}) == \
        {"gf_low": 45, "events": [{"time": 1700, "type": "setpoint_change", "value": 1.3}]}
    assert legacy_dive().model_dump(include={"gf_low", "duration"}) == {"duration": 4129}


def test_copies_keep_the_carried_data():
    dive = computer_dive()
    deep = dive.model_copy(deep=True)
    assert deep == dive and deep.samples[0].channels is not dive.samples[0].channels
    deep.samples[0].channels.pressures[0] = 1.0
    deep.events[0].tank = 1
    assert dive.samples[0].channels.pressures[0] == 201.3 and dive.events[0].tank == 0


def test_without_unset_keeps_named_keys_and_real_values():
    assert models._without_unset({"a": None, "b": [], "c": {}, "d": 0, "e": False, "f": "", "g": [0]}) == \
        {"d": 0, "e": False, "f": "", "g": [0]}
    assert models._without_unset({"a": None, "b": 1}, keep=("a",)) == {"a": None, "b": 1}


# ---------------------------------------------------------------- carried, not synced

@pytest.mark.parametrize("service_id", ["garmin", "divelogs", "subsurface", "uddf", "submersion", "shearwater"])
def test_no_field_catalogue_names_a_carried_field(service_id):
    """What the boards, the engine and conflicts can see is the catalogue;
    the carried fields are in none of them."""
    carried = set(CARRIED_DIVE_FIELDS) | {"channels"} | set(SampleChannels.model_fields)
    for spec in field_catalog_of(service_id):
        assert spec.unified not in carried, spec.key
        assert spec.name not in carried, spec.key
