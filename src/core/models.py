from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field, SerializerFunctionWrapHandler, model_serializer


def recorded_water_temp(value: Any) -> Optional[float]:
    """A water temperature as recorded, or None when there is none. 0 °C
    counts as none: Garmin stores 0 on a hand-logged dive whose temperature
    was never entered, and a real 0 °C dive is rare enough not to guess it."""
    if value in (None, ""):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if value == 0.0 else value

class GasMixture(BaseModel):
    oxygen: float = Field(21.0, description="Oxygen percentage (0-100)")
    helium: float = Field(0.0, description="Helium percentage (0-100)")
    start_pressure: Optional[float] = Field(None, description="Starting pressure in bar")
    end_pressure: Optional[float] = Field(None, description="Ending pressure in bar")
    tank_volume: Optional[float] = Field(None, description="Tank volume in liters")
    tank_name: Optional[str] = Field(None, description="Custom name of the tank/cylinder (e.g. a transmitter name or user label)")
    tank_role: Optional[str] = Field(
        None,
        description=(
            "Role of this tank in a multi-tank setup, e.g. 'backGas', 'stage', 'deco', 'bailout', "
            "'sidemountLeft', 'sidemountRight', 'diluent', 'oxygen', 'not_used'. Services model this "
            "differently (or not at all); see each adapter's mapping. Order in gas_mixtures is the "
            "primary source of tank order across all services."
        ),
    )

# ---------------------------------------------------------------------------
# Carried, not synced (plans/convert.md I1)
#
# A dive computer's own file (a Garmin .fit, the Shearwater log) holds more
# than the services exchange. The models below and the UnifiedDive fields
# listed in CARRIED_DIVE_FIELDS hold that extra data so a file conversion
# loses nothing. They are no part of a sync: no field catalogue names them,
# nothing compares them, and they are left out of the JSON whenever they are
# unset, so caches, backups and conflicts written for a dive without them
# are byte for byte what they were before these fields existed.
#
# The per-sample channels sit in one optional object (UnifiedSample.channels)
# rather than as eleven more fields of UnifiedSample: a sync holds every
# sample of every dive in memory, and eleven unset fields on each would grow
# that by half (measured: 560 -> 840 bytes a sample) for data no sync uses.
# ---------------------------------------------------------------------------

EVENT_GAS_SWITCH = "gas_switch"
EVENT_ALERT = "alert"
EVENT_MODE_CHANGE = "mode_change"
EVENT_SETPOINT_CHANGE = "setpoint_change"
EVENT_BOOKMARK = "bookmark"
KNOWN_EVENT_TYPES: Tuple[str, ...] = (EVENT_GAS_SWITCH, EVENT_ALERT, EVENT_MODE_CHANGE, EVENT_SETPOINT_CHANGE, EVENT_BOOKMARK)

# UnifiedDive.dive_mode / .water_type: the words a reader uses when it
# recognises the computer's own; another value is kept as it was read.
KNOWN_DIVE_MODES: Tuple[str, ...] = ("oc_single_gas", "oc_multi_gas", "gauge", "ccr", "scr", "apnea")
KNOWN_WATER_TYPES: Tuple[str, ...] = ("fresh", "salt", "en13319", "custom")


def _without_unset(data: Dict[str, Any], keep: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """``data`` without the keys whose value is None or an empty list/dict
    (0 and False are values); the keys in ``keep`` always stay."""
    return {k: v for k, v in data.items() if k in keep or not (v is None or v == [] or v == {})}


class SampleChannels(BaseModel):
    """What a dive computer logs at one sample beyond depth, temperature and
    one tank pressure. Every channel is optional: None means the computer
    logged none, never a default. The names line up with the Shearwater log
    decoder's ``ShearwaterSample`` (services/shearwater_log.py) and the
    Subsurface ``Sample.pressures``; the units are this module's (metres,
    bar, seconds, percent), so a reader converts (Shearwater logs minutes
    and a CNS fraction)."""
    pressures: Dict[int, float] = Field(
        default_factory=dict,
        description=(
            "Tank pressure in bar per tank at this sample, keyed by the tank's 0-based index in "
            "UnifiedDive.gas_mixtures (the reader maps a transmitter to its tank). Holds every tank with a "
            "reading, including the one whose value is also in UnifiedSample.pressure"
        ),
    )
    ndl: Optional[int] = Field(None, description="No-decompression limit left, in seconds")
    tts: Optional[int] = Field(None, description="Time to surface, in seconds")
    deco_stop_depth: Optional[float] = Field(None, description="Depth of the next stop in meters; 0 when the computer says no stop is needed")
    deco_stop_time: Optional[int] = Field(None, description="Time to spend at the next stop, in seconds")
    cns: Optional[float] = Field(None, description="CNS oxygen toxicity as a percentage (12.0 = 12 %)")
    ppo2: Optional[float] = Field(None, description="Oxygen partial pressure in bar (open circuit: of the gas breathed; a rebreather: of the loop)")
    setpoint: Optional[float] = Field(None, description="Rebreather PO2 setpoint in bar")
    gf99: Optional[float] = Field(None, description="Current gradient factor of the leading tissue as a percentage (Shearwater's GF99)")
    gas_time: Optional[int] = Field(None, description="Remaining gas time in seconds (Garmin's air time remaining, Shearwater's GTR)")
    heart_rate: Optional[int] = Field(None, description="Heart rate in beats per minute")

    @model_serializer(mode="wrap")
    def _omit_unset(self, handler: SerializerFunctionWrapHandler) -> Dict[str, Any]:
        """Only the channels that hold something: most samples of most
        computers have a few of them, and a profile has thousands of samples."""
        return _without_unset(handler(self))


class UnifiedSample(BaseModel):
    depth: float = Field(..., description="Depth in meters")
    temp: Optional[float] = Field(None, description="Temperature in Celsius")
    time: Optional[int] = Field(None, description="Time in seconds from start of dive")
    pressure: Optional[float] = Field(None, description="Tank pressure in bar at this sample, when the computer logged one (a transmitter); None otherwise")
    channels: Optional[SampleChannels] = Field(
        None,
        description=(
            "Everything else the computer logged at this sample (NDL, TTS, deco stop, CNS, PO2, heart rate, "
            "pressure per tank), for the file conversions (plans/convert.md I1). Carried, not synced: profiles "
            "are compared on depth, temperature and time only, and a sample without channels serialises "
            "exactly as it did before this field existed"
        ),
    )

    @model_serializer(mode="wrap")
    def _omit_unset_channels(self, handler: SerializerFunctionWrapHandler) -> Dict[str, Any]:
        """The four profile keys as before; ``channels`` only when it holds something."""
        data = handler(self)
        if "channels" in data and not data["channels"]:
            del data["channels"]
        return data


class DiveEvent(BaseModel):
    """Something that happened at one moment of a dive: a gas switch, an
    alert of the computer, a change of dive mode or setpoint (plans/convert.md
    I1). The shape follows what Subsurface keeps per event (a time, a kind, a
    name, a value, and for a gas change the cylinder and the mix), so a list
    of these can be written as its ``event`` lines, as UDDF ``switchmix`` /
    ``alarm`` waypoint elements, or read from a FIT ``event`` message.

    ``type`` is one of ``KNOWN_EVENT_TYPES`` where the reader recognised the
    event; anything else is kept as the reader named it."""
    time: int = Field(..., description="Seconds from the start of the dive, the same clock as UnifiedSample.time")
    type: str = Field(..., description="Kind of event: 'gas_switch', 'alert', 'mode_change', 'setpoint_change', 'bookmark' (KNOWN_EVENT_TYPES), or the reader's own word for another kind")
    name: Optional[str] = Field(None, description="The computer's own name for the event, e.g. a Garmin alert 'Safety stop begin' or 'ndl_reached'. For a mode_change: the mode switched to, in UnifiedDive.dive_mode's vocabulary")
    tank: Optional[int] = Field(None, description="gas_switch: 0-based index into UnifiedDive.gas_mixtures of the gas/tank switched to, when the reader could tell which entry it is")
    oxygen: Optional[float] = Field(None, description="gas_switch: oxygen percentage (0-100) of the mix switched to, for computers that log the mix rather than a tank")
    helium: Optional[float] = Field(None, description="gas_switch: helium percentage (0-100) of the mix switched to")
    value: Optional[float] = Field(None, description="Numeric payload of the event when it has one: the new setpoint in bar for a setpoint_change, otherwise the computer's raw value")

    @model_serializer(mode="wrap")
    def _omit_unset(self, handler: SerializerFunctionWrapHandler) -> Dict[str, Any]:
        """Only the keys that hold something (time and type always)."""
        return _without_unset(handler(self), keep=("time", "type"))


class UnifiedDive(BaseModel):
    date_time: datetime = Field(..., description="Local start date and time of the dive (timezone-naive)")
    date_time_utc: Optional[datetime] = Field(None, description="Start instant in UTC (timezone-naive) when the service provides it; used for matching and the {date_time_utc} template key")
    timezone: Optional[str] = Field(None, description="IANA zone of the local start time when the service provides it, e.g. 'Asia/Kuala_Lumpur'")
    duration: int = Field(..., description="Duration of the dive in seconds")
    max_depth: float = Field(..., description="Maximum depth in meters")
    avg_depth: Optional[float] = Field(None, description="Average depth in meters")
    temp_min: Optional[float] = Field(None, description="Minimum temperature in Celsius")
    temp_max: Optional[float] = Field(None, description="Maximum temperature in Celsius")
    temp_avg: Optional[float] = Field(None, description="Average temperature in Celsius")
    external_ids: Dict[str, str] = Field(default_factory=dict, description="Service-specific primary keys, e.g., {'garmin': '12345', 'divelogs': '67890'}")
    gas_mixtures: List[GasMixture] = Field(default_factory=list, description="Gas mixtures used per dive")
    location: Optional[str] = Field(None, description="Location/Dive site name")
    notes: Optional[str] = Field(None, description="Notes/Description of the dive")
    dive_number: Optional[int] = Field(None, description="Dive number sequence")
    weight: Optional[float] = Field(None, description="Lead weight value")
    weight_unit: Optional[str] = Field(None, description="Weight unit, e.g. 'kilogram' or 'pound'")
    visibility: Optional[float] = Field(None, description="Visibility value")
    visibility_unit: Optional[str] = Field(None, description="Visibility unit, e.g. 'meter' or 'foot'")
    buddy: Optional[str] = Field(None, description="Dive buddy name")
    lat: Optional[float] = Field(None, description="Latitude coordinate")
    lng: Optional[float] = Field(None, description="Longitude coordinate")
    samples: List[UnifiedSample] = Field(default_factory=list, description="Time-series dive profile samples")
    device_logged: Optional[bool] = Field(
        None,
        description=(
            "True when a dive computer recorded this dive, False when it was entered by hand, None when the "
            "service does not say. Provenance rather than dive data: it decides whether a receiver that gets its "
            "computer data elsewhere (Submersion, whose profile comes from the diver's own .fit import) should "
            "have the dive created for it at all (rework.md F17)"
        ),
    )
    service_fields: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Service-specific scalars that have no unified attribute, keyed by their native name "
            "(Garmin: activityName, locationName; Divelogs: location, divesite). Filled by the adapter's "
            "to_unified mapping, pushed back by its update_dive, addressed on the mapping board as "
            "<service_id>.<name>."
        ),
    )

    # --- Carried, not synced (CARRIED_DIVE_FIELDS; plans/convert.md I1) ---
    events: List[DiveEvent] = Field(default_factory=list, description="Gas switches, alerts and other events of the dive, in time order")
    computer_vendor: Optional[str] = Field(None, description="Maker of the dive computer that recorded the dive, e.g. 'Garmin', 'Shearwater'")
    computer_model: Optional[str] = Field(None, description="Model of the dive computer without the maker, e.g. 'Descent Mk3i', 'Perdix 2'")
    computer_serial: Optional[str] = Field(None, description="Serial number of the dive computer, as text")
    computer_firmware: Optional[str] = Field(None, description="Firmware version of the dive computer, as it names it")
    gf_low: Optional[int] = Field(None, description="Gradient factor low setting, in percent")
    gf_high: Optional[int] = Field(None, description="Gradient factor high setting, in percent")
    deco_model: Optional[str] = Field(None, description="Decompression model the computer ran, as it names it, e.g. 'ZHL-16C', 'VPM-B'")
    water_type: Optional[str] = Field(None, description="Water setting of the computer: 'fresh', 'salt', 'en13319' or 'custom' (KNOWN_WATER_TYPES)")
    water_density: Optional[float] = Field(None, description="Water density in kg/m3 the computer converted pressure to depth with, e.g. 1025.0")
    dive_mode: Optional[str] = Field(None, description="How the dive was dived: 'oc_single_gas', 'oc_multi_gas', 'gauge', 'ccr', 'scr' or 'apnea' (KNOWN_DIVE_MODES)")
    exit_lat: Optional[float] = Field(None, description="Latitude where the diver surfaced (lat/lng are the entry)")
    exit_lng: Optional[float] = Field(None, description="Longitude where the diver surfaced")
    surface_interval: Optional[int] = Field(None, description="Time at the surface since the previous dive, in seconds")
    bottom_time: Optional[int] = Field(None, description="Bottom time in seconds as the computer counted it (duration is the whole dive)")
    cns_start: Optional[float] = Field(None, description="CNS oxygen toxicity at the start of the dive, as a percentage")
    cns_end: Optional[float] = Field(None, description="CNS oxygen toxicity at the end of the dive, as a percentage")

    @model_serializer(mode="wrap")
    def _omit_unset_carried(self, handler: SerializerFunctionWrapHandler) -> Dict[str, Any]:
        """Every key the dive had before I1, always; a carried field only
        when it holds something."""
        data = handler(self)
        for name in CARRIED_DIVE_FIELDS:
            if name in data and (data[name] is None or data[name] == []):
                del data[name]
        return data


# The UnifiedDive fields that are carried, not synced: left out of the JSON
# when unset, and never named by a field catalogue (tests/test_models.py
# holds both to it, and makes a new UnifiedDive field choose a side).
CARRIED_DIVE_FIELDS: Tuple[str, ...] = (
    "events", "computer_vendor", "computer_model", "computer_serial", "computer_firmware",
    "gf_low", "gf_high", "deco_model", "water_type", "water_density", "dive_mode",
    "exit_lat", "exit_lng", "surface_interval", "bottom_time", "cns_start", "cns_end",
)
