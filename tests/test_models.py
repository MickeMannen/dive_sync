"""The unified models (src/core/models.py) and the fields that are carried,
not synced (plans/convert.md I1): per-sample channels, dive events, the dive
computer and its settings - plus the three sparse fields a catalogue may
name (water type, air temperature, suit; plans/gap-roadmap.md R10).

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
    SPARSE_DIVE_FIELDS,
    SYNCED_SPARSE_FIELDS,
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
        sac=11.2, rmv=22.53, otu=19.0,
        air_temp=31.5, suit="3 mm shorty",
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
    assert all(getattr(dive, name) in (None, []) for name in SPARSE_DIVE_FIELDS)
    assert json.dumps(dive.model_dump(mode="json"), indent=2) == LEGACY_DIVE_JSON
    assert UnifiedDive.model_validate_json(BARE_DIVE_JSON).model_dump_json() == BARE_DIVE_JSON


def test_every_field_is_either_as_before_or_sparse():
    """A new UnifiedDive field has to choose: it is listed as sparse (and so
    left out of the JSON when unset) - carried, or one of the few a
    catalogue may name - or it changes what every cache file holds and
    this list is edited on purpose."""
    assert list(UnifiedDive.model_fields) == LEGACY_DIVE_KEYS + list(SPARSE_DIVE_FIELDS)
    assert set(SYNCED_SPARSE_FIELDS) <= set(SPARSE_DIVE_FIELDS)
    assert set(CARRIED_DIVE_FIELDS) == set(SPARSE_DIVE_FIELDS) - set(SYNCED_SPARSE_FIELDS)
    assert SYNCED_SPARSE_FIELDS == ("water_type", "air_temp", "suit", "exit_lat", "exit_lng")
    assert list(UnifiedSample.model_fields) == LEGACY_SAMPLE_KEYS + ["channels"]
    for name in SPARSE_DIVE_FIELDS:
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
    assert list(data) == LEGACY_DIVE_KEYS + list(SPARSE_DIVE_FIELDS)         # after the keys a dive always had
    assert data["air_temp"] == 31.5 and data["suit"] == "3 mm shorty"
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


def test_has_ccr_data_names_what_the_ccr_note_is_shown_for():
    """R18b: a dive holds rebreather data when its mode is closed or
    semi-closed circuit, a tank is the diluent or the oxygen, or a sample
    carries a setpoint or O2 cells; an open-circuit dive, cells or not,
    does not. The note itself says CCR is untested."""
    from src.core.models import CCR_NOTE, has_ccr_data
    base = UnifiedDive(date_time=datetime(2026, 5, 1, 10, 0), duration=3600, max_depth=25.0)
    assert has_ccr_data(base) is False
    assert has_ccr_data(base.model_copy(update={"dive_mode": "ccr"})) is True
    assert has_ccr_data(base.model_copy(update={"dive_mode": "scr"})) is True
    assert has_ccr_data(base.model_copy(update={"dive_mode": "oc_multi_gas"})) is False
    assert has_ccr_data(base.model_copy(update={"gas_mixtures": [GasMixture(oxygen=21.0, tank_role="diluent")]})) is True
    assert has_ccr_data(base.model_copy(update={"gas_mixtures": [GasMixture(oxygen=100.0, tank_role="oxygen")]})) is True
    assert has_ccr_data(base.model_copy(update={"gas_mixtures": [GasMixture(oxygen=50.0, tank_role="deco")]})) is False
    plain = [UnifiedSample(depth=10.0, time=60), UnifiedSample(depth=12.0, time=120, channels=SampleChannels(ppo2=0.42, ndl=1200))]
    assert has_ccr_data(base.model_copy(update={"samples": plain})) is False
    assert has_ccr_data(base.model_copy(update={"samples": plain + [UnifiedSample(depth=12.0, time=180, channels=SampleChannels(setpoint=1.2))]})) is True
    assert has_ccr_data(base.model_copy(update={"samples": plain + [UnifiedSample(depth=12.0, time=180, channels=SampleChannels(o2_cells={0: 1.2}))]})) is True
    assert "not yet tested with a real CCR dive" in CCR_NOTE


def test_o2_cells_are_a_channel_of_their_own_next_to_the_computers_po2():
    """A rebreather's cells (plans/gap-roadmap.md R18): one reading per
    cell by its 0-based index, written only when a sample has any, JSON
    keys coming back as numbers; the computer's own PO2 and the setpoint
    stay what they were."""
    channels = SampleChannels(ppo2=1.21, setpoint=1.2, o2_cells={0: 1.22, 1: 1.19, 2: 1.21})
    assert channels.model_dump() == {"ppo2": 1.21, "setpoint": 1.2, "o2_cells": {0: 1.22, 1: 1.19, 2: 1.21}}
    sample = UnifiedSample(depth=20.0, time=60, channels=channels)
    data = sample.model_dump(mode="json")
    assert data["channels"]["o2_cells"] == {"0": 1.22, "1": 1.19, "2": 1.21}
    back = UnifiedSample(**json.loads(json.dumps(data)))
    assert back == sample and back.channels.o2_cells == {0: 1.22, 1: 1.19, 2: 1.21}
    # a cell the computer skipped leaves a gap, not a 0; no cells is no key
    assert SampleChannels(o2_cells={0: 1.2, 2: 1.21}).model_dump() == {"o2_cells": {0: 1.2, 2: 1.21}}
    assert SampleChannels(ppo2=1.2, o2_cells={}).model_dump() == {"ppo2": 1.2}
    # a dive written before R18 reads and writes back unchanged
    before = computer_dive().model_dump(mode="json")
    assert "o2_cells" not in json.dumps(before)
    assert UnifiedDive(**before).model_dump(mode="json") == before


def test_zero_is_a_value_not_unset():
    """NDL 0, CNS 0 %, 'no stop' and tank 0 are things a computer logs."""
    channels = SampleChannels(ndl=0, cns=0.0, deco_stop_depth=0.0, deco_stop_time=0, heart_rate=0)
    assert channels.model_dump() == {"ndl": 0, "deco_stop_depth": 0.0, "deco_stop_time": 0, "cns": 0.0, "heart_rate": 0}
    assert DiveEvent(time=0, type=EVENT_GAS_SWITCH, tank=0, helium=0.0, value=0.0).model_dump() == \
        {"time": 0, "type": "gas_switch", "tank": 0, "helium": 0.0, "value": 0.0}
    dive = legacy_dive(gf_low=0, surface_interval=0, cns_start=0.0, exit_lat=0.0, exit_lng=0.0, bottom_time=0)
    data = dive.model_dump()
    assert [k for k in data if k in CARRIED_DIVE_FIELDS] == ["gf_low", "surface_interval", "bottom_time", "cns_start"]
    assert (data["exit_lat"], data["exit_lng"]) == (0.0, 0.0)      # the equator is a position (synced since R8)


def test_one_carried_field_adds_one_key():
    data = legacy_dive(computer_model="Perdix 2").model_dump(mode="json")
    assert list(data) == LEGACY_DIVE_KEYS + ["computer_model"] and data["computer_model"] == "Perdix 2"


def test_carried_fields_default_to_unset_and_validate_their_types():
    channels = SampleChannels()
    assert channels.pressures == {} and channels.o2_cells == {} and channels.model_dump() == {}
    assert all(getattr(channels, n) is None for n in SampleChannels.model_fields if n not in ("pressures", "o2_cells"))
    assert SampleChannels().pressures is not SampleChannels().pressures         # no shared default dict
    assert SampleChannels().o2_cells is not SampleChannels().o2_cells
    assert legacy_dive().events is not legacy_dive().events
    with pytest.raises(ValueError):
        SampleChannels(ndl="soon")
    with pytest.raises(ValueError):
        SampleChannels(pressures={"left": 200.0})                                # a tank is an index into gas_mixtures
    with pytest.raises(ValueError):
        SampleChannels(o2_cells={"left": 1.2})                                   # a cell is its 0-based index
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

@pytest.mark.parametrize("service_id", ["garmin", "divelogs", "subsurface", "uddf", "submersion", "shearwater", "ssi"])
def test_no_field_catalogue_names_a_carried_field(service_id):
    """What the boards, the engine and conflicts can see is the catalogue;
    the carried fields are in none of them (the three synced sparse fields
    may be, under their own name - R10)."""
    carried = set(CARRIED_DIVE_FIELDS) | {"channels"} | set(SampleChannels.model_fields)
    for spec in field_catalog_of(service_id):
        assert spec.unified not in carried, spec.key
        assert spec.name not in carried, spec.key


# ---------------------------------------------------------------- surface air consumption (R9)

def test_surface_air_consumption_sums_every_cylinder():
    """SAC = litres breathed / (minutes x atmospheres at the average depth):
    a twinset dive, 2 x 11.1 L, 162.57->78.04 and 160.69->106.38 bar over
    4129 s at 8.196 m, is 1541 L in 68.8 min at 1.82 ata = 12.3 L/min; one
    cylinder of it alone is the smaller half."""
    tanks = [(11.1, 162.57, 78.04), (11.1, 160.69, 106.38)]
    assert models.surface_air_consumption(tanks, 8.196, 4129.51) == pytest.approx(12.31, abs=0.01)
    assert models.surface_air_consumption(tanks[:1], 8.196, 4129.51) == pytest.approx(7.5, abs=0.05)
    # 150 bar x 12 L = 1800 L, over 60 min at 20 m (3 ata) = 10.0 L/min
    assert models.surface_air_consumption([(12.0, 200.0, 50.0)], 20.0, 3600) == pytest.approx(10.0)


def test_surface_air_consumption_needs_a_volume_a_drop_a_depth_and_a_time():
    full = (12.0, 200.0, 50.0)
    assert models.surface_air_consumption([(0, 200.0, 50.0)], 20.0, 3600) is None       # Divelogs' 0 = size never entered
    assert models.surface_air_consumption([(None, 200.0, 50.0)], 20.0, 3600) is None
    assert models.surface_air_consumption([(12.0, 200.0, 200.0)], 20.0, 3600) is None   # no drop
    assert models.surface_air_consumption([(12.0, 50.0, 200.0)], 20.0, 3600) is None    # a rise is no drop
    assert models.surface_air_consumption([], 20.0, 3600) is None
    assert models.surface_air_consumption([full], 0, 3600) is None                      # no average depth
    assert models.surface_air_consumption([full], 20.0, 0) is None                      # no duration
    assert models.surface_air_consumption([full], None, None) is None
    assert models.surface_air_consumption([("x", "y", "z")], 20.0, 3600) is None
    assert models.surface_air_consumption([("12", "200", "50")], "20", "3600") == pytest.approx(10.0)   # texts, as a cache holds them


def test_dive_sac_is_the_computers_else_computed_from_the_tanks():
    """`dive_sac`: the logged SAC wins whenever the dive holds one (even
    with tanks to compute from), else the computed one, else nothing; the
    source word says which. Nothing is written back into the dive."""
    logged = computer_dive()                                   # sac=11.2 with tanks to compute from
    assert models.dive_sac(logged) == (11.2, models.SAC_LOGGED)
    computed = legacy_dive(gas_mixtures=[GasMixture(tank_volume=12.0, start_pressure=200.0, end_pressure=50.0)],
                           avg_depth=20.0, duration=3600)
    value, source = models.dive_sac(computed)
    assert value == pytest.approx(10.0) and source == models.SAC_COMPUTED
    assert computed.sac is None and models.computed_sac(computed) == pytest.approx(10.0)
    assert models.dive_sac(legacy_dive(gas_mixtures=[])) == (None, "")
    assert models.dive_sac(legacy_dive(avg_depth=None)) == (None, "")
    assert models.computed_sac(legacy_dive()) == pytest.approx((162.57 - 78.04) * 11.1 / (4129 / 60.0 * 1.8196), abs=0.01)
    assert (models.SAC_LOGGED, models.SAC_COMPUTED) == ("logged", "computed")


# ---------------------------------------------------------------- the synced sparse fields (R10)

def test_air_temperature_and_suit_are_sparse_but_syncable():
    """``air_temp`` and ``suit`` (and ``water_type``) are left out of the
    JSON when unset like the carried fields, so every cache written before
    R10 reads and writes back unchanged, and come back from JSON as they
    went in; unlike the carried fields a catalogue may name them."""
    assert legacy_dive().model_dump() == {k: v for k, v in legacy_dive().model_dump().items() if k in LEGACY_DIVE_KEYS}
    dive = legacy_dive(air_temp=31.5, suit="drysuit", water_type="fresh")
    data = dive.model_dump(mode="json")
    assert list(data) == LEGACY_DIVE_KEYS + ["water_type", "air_temp", "suit"]
    assert UnifiedDive(**json.loads(json.dumps(data))) == dive
    assert legacy_dive(air_temp=0.0).model_dump()["air_temp"] == 0.0          # 0 °C is a value
    assert "suit" not in legacy_dive(suit=None).model_dump()
    assert UnifiedDive(**json.loads(LEGACY_DIVE_JSON)).air_temp is None
    with pytest.raises(ValueError):
        legacy_dive(air_temp="warm")
    for name in SYNCED_SPARSE_FIELDS:
        assert name not in CARRIED_DIVE_FIELDS
    named = {spec.unified for service in ("subsurface", "uddf", "shearwater", "ssi", "garmin") for spec in field_catalog_of(service)}
    assert {"water_type", "air_temp", "suit", "exit_gps"} <= named          # each named by at least one catalogue
    # the exit position is one catalogue field (a pair, like gps), never two
    assert not any(spec.unified in ("exit_lat", "exit_lng") for service in ("subsurface", "uddf", "garmin")
                   for spec in field_catalog_of(service))


def test_salinity_and_water_type_tables():
    assert models.SALINITY_BY_WATER_TYPE == {"fresh": 1000, "salt": 1030, "en13319": 1020}
    assert models.water_type_of_salinity(1030) == "salt" and models.water_type_of_salinity("1025 g/l".split()[0]) == "salt"
    assert models.water_type_of_salinity(1000.4) == "fresh" and models.water_type_of_salinity(1020) == "en13319"
    assert models.water_type_of_salinity(1015) is None and models.water_type_of_salinity(None) is None
    assert models.water_type_of_salinity("x") is None
    assert models.salinity_of("salt") == 1030 and models.salinity_of("salt", 1025.0) == 1025
    assert models.salinity_of("custom") is None and models.salinity_of(None, 1012.6) == 1013 and models.salinity_of(None) is None
