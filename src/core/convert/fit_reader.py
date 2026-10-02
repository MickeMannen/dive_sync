"""Read a Garmin dive ``.fit`` into a `UnifiedDive` (plans/convert.md I3).

The decoder is `fitdecode` (MIT, pure Python). The reading rules are ported
from UWMedia's ``parsers/garmin.py`` (MIT, same owner): the local zone from
the ``activity`` message's ``local_timestamp`` against its ``timestamp``,
entry and exit positions from the ``session``, gases from ``dive_gas``, the
``tank_update`` readings merged onto the ``record`` samples, ``dive_alert``
events. Added here, from the messages UWMedia's telemetry reader skips:
``dive_summary`` (dive number, bottom time, surface interval, CNS),
``dive_settings`` (deco model, gradient factors, water type and density),
``tank_summary`` (start and end pressure per transmitter), the transmitter
profiles (message 147, named by libdivecomputer's ``garmin.c``: name and
volume) and the ``dive_gas_switched`` event.

Rules kept from the F16 reader (verified against Submersion's own derivation
of the same files, plans/subsurface-submersion.md F16):
- the average depth is the watch's own ``dive_summary.avg_depth`` (its
  bottom-time average, the number Garmin Connect shows), so a FIT-read dive
  agrees with the Connect-read one (owner, 2026-10-02, over F16's profile
  mean); the time-weighted profile mean only when the file has no summary;
- the water temperature (``temp_min``) is the coldest sample, not the first;
- a value the file does not hold stays None, never a default.

What the FIT does not hold (site, buddy, notes, weight, visibility) is left
unset; I9 fills it from the Garmin cache. Apnea files are refused like the
Garmin adapter refuses apnea activities (``garmin_files.SCUBA_ACTIVITY_TYPES``).
"""
from __future__ import annotations

import bisect
import io
import logging
import os
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Union

import fitdecode

from src.core import garmin_files
from src.core.models import (
    EVENT_ALERT,
    EVENT_GAS_SWITCH,
    KNOWN_WATER_TYPES,
    DiveEvent,
    GasMixture,
    SampleChannels,
    UnifiedDive,
    UnifiedSample,
)

logger = logging.getLogger("dive_sync.convert.fit")

# Connect exports a dive as ``<activity id>.zip`` holding ``<activity id>_ACTIVITY.fit``.
# An activity id has had 9+ digits since Connect began (today 11), so a short
# all-digit name (``515.fit``, a dive renamed by its number) is not one.
CONNECT_ID_MIN_DIGITS = 8
_CONNECT_NAME = re.compile(r"(\d{%d,})(?:_ACTIVITY)?" % CONNECT_ID_MIN_DIGITS, re.IGNORECASE)

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
SENSOR_PROFILE = 147          # the transmitter profile; not in Garmin's public profile
SENSOR_PROFILE_ID, SENSOR_PROFILE_NAME, SENSOR_PROFILE_TYPED_NAME, SENSOR_PROFILE_VOLUME = 0, 2, 91, 77

# session.sub_sport -> UnifiedDive.dive_mode (KNOWN_DIVE_MODES); apnea is
# refused. The numbers are Garmin's own for the values fitdecode's profile
# does not name yet (ccr_diving = 63, dynamic_apnea = 121), which it then
# hands over as plain integers.
DIVE_MODES: Dict[Any, str] = {
    "single_gas_diving": "oc_single_gas", 53: "oc_single_gas",
    "multi_gas_diving": "oc_multi_gas", 54: "oc_multi_gas",
    "gauge_diving": "gauge", 55: "gauge",
    "ccr_diving": "ccr", 63: "ccr",
}
APNEA_SUB_SPORTS = frozenset({"apnea_diving", 56, "apnea_hunting", 57, "dynamic_apnea", 121})
# dive_settings.model -> the name Subsurface and the UDDF files use.
DECO_MODELS = {"zhl_16c": "ZHL-16C"}
# The FIT events a dive keeps; the timer and lap events are the dive's own start and stop.
EVENT_TANK_POD_CONNECTED = "tank_pod_connected"
EVENT_TANK_POD_DISCONNECTED = "tank_pod_disconnected"


class FitReadError(ValueError):
    """The input is not a FIT file this reader can decode."""


class NotADiveError(FitReadError):
    """A FIT file that is not a scuba dive: another activity, a settings
    file, or an apnea dive."""


@dataclass
class _Tank:
    """One transmitter as the file names it (``sensor`` is its ANT id)."""
    sensor: int
    name: Optional[str] = None
    volume: Optional[float] = None
    start_pressure: Optional[float] = None
    end_pressure: Optional[float] = None


@dataclass
class _Gas:
    index: int
    oxygen: float
    helium: float


@dataclass
class _Messages:
    """The file's data messages by name (the transmitter profile under 147)."""
    by_name: Dict[Any, List[fitdecode.FitDataMessage]] = field(default_factory=dict)

    def all(self, name: Any) -> List[fitdecode.FitDataMessage]:
        return self.by_name.get(name, [])

    def first(self, name: Any) -> Optional[fitdecode.FitDataMessage]:
        items = self.all(name)
        return items[0] if items else None


def _value(message: Optional[fitdecode.FitDataMessage], name: Any, raw: bool = False) -> Any:
    """A field's value, None when the message or the field is missing or
    invalid (fitdecode gives None for a field the message carries unset)."""
    if message is None:
        return None
    try:
        return message.get_value(name, fallback=None, raw_value=raw)
    except (KeyError, IndexError, ValueError):
        return None


def _float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out  # NaN (bottom_depth=nan in the fixtures) is unset


def _int(value: Any) -> Optional[int]:
    out = _float(value)
    return int(round(out)) if out is not None else None


def _degrees(semicircles: Any) -> Optional[float]:
    value = _int(semicircles)
    return value * 180.0 / 2 ** 31 if value is not None else None


def _fit_seconds(message: Optional[fitdecode.FitDataMessage], name: str) -> Optional[int]:
    """A timestamp field as FIT seconds (since 1989-12-31 UTC)."""
    return _int(_value(message, name, raw=True))


def _naive_utc(fit_seconds: int) -> datetime:
    return (FIT_EPOCH + timedelta(seconds=fit_seconds)).replace(tzinfo=None)


def zone_name(offset_seconds: int) -> Optional[str]:
    """The IANA name of a fixed offset, for whole hours: ``Etc/GMT-7`` is
    UTC+7 (the Etc zones carry the inverted sign of the POSIX convention).
    A FIT holds the offset, not the zone, so this is the most that can be
    said; a half-hour offset has no Etc zone and gives None."""
    hours, rest = divmod(offset_seconds, 3600)
    if rest or not -12 <= hours <= 14:
        return None
    if hours == 0:
        return "Etc/UTC"
    return f"Etc/GMT{'-' if hours > 0 else '+'}{abs(hours)}"


def decode(data: bytes) -> _Messages:
    """Every data message of ``data``, or FitReadError when it is not a FIT
    file (wrong header, CRC, truncated, undecodable)."""
    messages = _Messages()
    try:
        with fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.RAISE,
                                 error_handling=fitdecode.ErrorHandling.RAISE) as reader:
            for frame in reader:
                if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                    continue
                key: Any = frame.global_mesg_num if frame.global_mesg_num == SENSOR_PROFILE else frame.name
                messages.by_name.setdefault(key, []).append(frame)
    except (fitdecode.FitError, ValueError, struct.error, IndexError, KeyError, EOFError) as e:
        raise FitReadError(f"not a FIT file this reader can decode: {e}") from e
    if not messages.by_name:
        raise FitReadError("not a FIT file this reader can decode: no data messages")
    return messages


def _check_dive(messages: _Messages) -> Optional[str]:
    """Refuse what is not a scuba dive; the dive mode of what is."""
    file_type = _value(messages.first("file_id"), "type")
    if file_type is not None and file_type != "activity":
        raise NotADiveError(f"not a dive: the file is a Garmin '{file_type}' file, not an activity")
    session = messages.first("session") or messages.first("lap")
    if session is None:
        raise FitReadError("not a dive FIT: the file has no session")
    sport = _value(session, "sport")
    sub_sport = _value(session, "sub_sport")
    if sport != "diving":
        raise NotADiveError(f"not a dive: the activity is '{sport}'")
    if sub_sport in APNEA_SUB_SPORTS:
        raise NotADiveError(f"apnea dives are not read (the activity is '{sub_sport}')")
    return DIVE_MODES.get(sub_sport)


def _tanks(messages: _Messages) -> List[_Tank]:
    """The transmitters that logged on this dive, in the order of their
    ``tank_summary`` messages (then of first reading), with the name and
    volume of their profile and the start and end pressure of the summary
    (else of the first and last reading)."""
    tanks: Dict[int, _Tank] = {}
    for summary in messages.all("tank_summary"):
        sensor = _int(_value(summary, "sensor"))
        if sensor is None or sensor in tanks:
            continue
        tanks[sensor] = _Tank(sensor, start_pressure=_float(_value(summary, "start_pressure")),
                              end_pressure=_float(_value(summary, "end_pressure")))
    first_last: Dict[int, List[float]] = {}
    for update in messages.all("tank_update"):
        sensor, pressure = _int(_value(update, "sensor")), _float(_value(update, "pressure"))
        if sensor is None or pressure is None:
            continue
        tanks.setdefault(sensor, _Tank(sensor))
        first_last.setdefault(sensor, [pressure, pressure])[1] = pressure
    for sensor, (first, last) in first_last.items():
        tank = tanks[sensor]
        if tank.start_pressure is None:
            tank.start_pressure = first
        if tank.end_pressure is None:
            tank.end_pressure = last
    for profile in messages.all(SENSOR_PROFILE):
        sensor = _int(_value(profile, SENSOR_PROFILE_ID))
        tank = tanks.get(sensor) if sensor is not None else None
        if tank is None:
            continue  # paired with the watch but not on this dive
        name = _value(profile, SENSOR_PROFILE_TYPED_NAME) or _value(profile, SENSOR_PROFILE_NAME)
        tank.name = str(name).strip() or None if name is not None else None
        volume = _float(_value(profile, SENSOR_PROFILE_VOLUME))
        if volume is not None and 0 < volume <= 1000:
            tank.volume = round(volume / 10.0, 2)  # 111 -> 11.1 L (an AL80), as libdivecomputer reads it
    return list(tanks.values())


def _gases(messages: _Messages) -> List[_Gas]:
    """The gases of the dive in ``dive_gas`` order, without the disabled
    ones; ``index`` is the message index the gas switch events refer to."""
    out = []
    for gas in messages.all("dive_gas"):
        if _value(gas, "status") == "disabled":
            continue
        index = _int(_value(gas, "message_index"))
        oxygen, helium = _float(_value(gas, "oxygen_content")), _float(_value(gas, "helium_content"))
        out.append(_Gas(index if index is not None else len(out), oxygen if oxygen is not None else 21.0,
                        helium if helium is not None else 0.0))
    return out


def _gas_mixtures(gases: Sequence[_Gas], tanks: Sequence[_Tank]) -> List[GasMixture]:
    """One entry per tank, pairing transmitter i with gas i as Garmin Connect
    does (``tankIndex`` = ``gasIndex``). More transmitters than gases: the
    extra tanks hold the first gas (a single-gas dive with two transmitters).
    More gases than transmitters: the extra gases have no pressures."""
    out = []
    for i in range(max(len(gases), len(tanks))):
        gas = gases[i] if i < len(gases) else (gases[0] if gases else None)
        tank = tanks[i] if i < len(tanks) else None
        out.append(GasMixture(
            oxygen=gas.oxygen if gas else 21.0,
            helium=gas.helium if gas else 0.0,
            start_pressure=tank.start_pressure if tank else None,
            end_pressure=tank.end_pressure if tank else None,
            tank_volume=tank.volume if tank else None,
            tank_name=tank.name if tank else None,
        ))
    return out


def _channels(record: fitdecode.FitDataMessage) -> SampleChannels:
    cns = _float(_value(record, "cns_load"))
    return SampleChannels(
        ndl=_int(_value(record, "ndl_time")),
        tts=_int(_value(record, "time_to_surface")),
        deco_stop_depth=_float(_value(record, "next_stop_depth")),
        deco_stop_time=_int(_value(record, "next_stop_time")),
        cns=cns,
        ppo2=_float(_value(record, "po2")),
        gas_time=_int(_value(record, "air_time_remaining")),
        heart_rate=_int(_value(record, "heart_rate")),
    )


def _samples(messages: _Messages, start: int, tanks: Sequence[_Tank]) -> List[UnifiedSample]:
    """The ``record`` messages as samples, each tank reading attached to the
    sample of its second (else the nearest earlier one; the first when it
    came before any record). ``pressure`` is the first tank's reading."""
    samples: List[UnifiedSample] = []
    times: List[int] = []
    for record in messages.all("record"):
        stamp, depth = _fit_seconds(record, "timestamp"), _float(_value(record, "depth"))
        if stamp is None or depth is None:
            continue
        samples.append(UnifiedSample(depth=depth, temp=_float(_value(record, "temperature")),
                                     time=max(0, stamp - start), channels=_channels(record)))
        times.append(stamp)
    if not samples:
        return samples
    tank_index = {tank.sensor: i for i, tank in enumerate(tanks)}
    for update in messages.all("tank_update"):
        stamp, sensor = _fit_seconds(update, "timestamp"), _int(_value(update, "sensor"))
        pressure = _float(_value(update, "pressure"))
        if stamp is None or sensor not in tank_index or pressure is None:
            continue
        at = max(0, bisect.bisect_right(times, stamp) - 1)
        sample = samples[at]
        assert sample.channels is not None
        sample.channels.pressures[tank_index[sensor]] = pressure
        if tank_index[sensor] == 0:
            sample.pressure = pressure
    return samples


def _average_depth(samples: Sequence[UnifiedSample]) -> Optional[float]:
    """Time-weighted (trapezoid) mean depth over the whole profile."""
    if not samples:
        return None
    if len(samples) == 1 or samples[-1].time == samples[0].time:
        return round(samples[0].depth, 3)
    area = 0.0
    for a, b in zip(samples, samples[1:]):
        area += (a.depth + b.depth) / 2.0 * ((b.time or 0) - (a.time or 0))
    return round(area / ((samples[-1].time or 0) - (samples[0].time or 0)), 3)


def _events(messages: _Messages, start: int, gases: Sequence[_Gas], tanks: Sequence[_Tank]) -> List[DiveEvent]:
    """Gas switches, alerts and transmitter connections, in time order."""
    gas_entry = {gas.index: i for i, gas in enumerate(gases)}
    tank_entry = {tank.sensor: i for i, tank in enumerate(tanks)}
    out: List[DiveEvent] = []
    for event in messages.all("event"):
        kind, stamp = _value(event, "event"), _fit_seconds(event, "timestamp")
        if stamp is None:
            continue
        time = max(0, stamp - start)
        data, raw = _value(event, "data"), _value(event, "data", raw=True)
        if kind == "dive_gas_switched":
            entry = gas_entry.get(_int(raw)) if raw is not None else None
            gas = gases[entry] if entry is not None else None
            out.append(DiveEvent(time=time, type=EVENT_GAS_SWITCH, tank=entry,
                                 oxygen=gas.oxygen if gas else None, helium=gas.helium if gas else None))
        elif kind == "dive_alert":
            out.append(DiveEvent(time=time, type=EVENT_ALERT, name=str(data) if data is not None else None,
                                 value=_float(raw)))
        elif kind in (EVENT_TANK_POD_CONNECTED, EVENT_TANK_POD_DISCONNECTED):
            out.append(DiveEvent(time=time, type=str(kind), tank=tank_entry.get(_int(raw))))
    out.sort(key=lambda e: e.time)
    return out


def _firmware(messages: _Messages) -> Optional[str]:
    """The watch's firmware from its own device_info (the 'creator'), else
    from file_creator (an integer, hundredths)."""
    for info in messages.all("device_info"):
        index = _value(info, "device_index")
        if index in ("creator", 0):
            version = _float(_value(info, "software_version"))
            if version is not None:
                return f"{version:.2f}"
    version = _float(_value(messages.first("file_creator"), "software_version"))
    return f"{version / 100.0:.2f}" if version is not None else None


def _model(messages: _Messages) -> Optional[str]:
    """The watch's model from file_id's product number (raw: fitdecode names
    the products it knows, e.g. 'descent_mk3i', and leaves the newer ones as
    numbers), through the same table the Garmin cache uses."""
    file_id = messages.first("file_id")
    product = _int(_value(file_id, "garmin_product", raw=True))
    if product is None:
        product = _int(_value(file_id, "product", raw=True))
    return garmin_files.FIT_PRODUCT_NAMES.get(product) if product is not None else None


def activity_id_from_name(*names: Optional[str]) -> Optional[str]:
    """The Garmin activity id the first of ``names`` that carries one gives:
    the app's own cache name ``<n>_<date>_<time>_<id>``
    (`garmin_files.activity_id_of`), Connect's export ``<id>.zip`` or its
    member ``<id>_ACTIVITY.fit``. None when no name holds one (a dive
    renamed by its number, ``515.fit``, holds none: see `CONNECT_ID_MIN_DIGITS`)."""
    for name in names:
        if not name:
            continue
        activity_id = garmin_files.activity_id_of(name)
        if activity_id:
            return activity_id
        stem = os.path.splitext(os.path.basename(name))[0]
        m = _CONNECT_NAME.fullmatch(stem)
        if m:
            return m.group(1)
    return None


def to_unified(messages: _Messages, name: Optional[str] = None, member: Optional[str] = None) -> UnifiedDive:
    """The dive in ``messages``; ``name`` is the file's name and ``member``
    the name of the entry inside Connect's zip, either of which gives the
    Garmin activity id when it is a cache or Connect name (`activity_id_from_name`)."""
    dive_mode = _check_dive(messages)
    session, activity = messages.first("session"), messages.first("activity")
    summary = next((s for s in messages.all("dive_summary") if _value(s, "dive_number") is not None),
                   messages.first("dive_summary"))
    settings, file_id = messages.first("dive_settings"), messages.first("file_id")

    start = _fit_seconds(session, "start_time")
    if start is None:
        start = _fit_seconds(activity, "timestamp")
    records = messages.all("record")
    if start is None and records:
        start = _fit_seconds(records[0], "timestamp")
    if start is None:
        raise FitReadError("not a dive FIT: no start time")
    offset = None
    local, utc = _fit_seconds(activity, "local_timestamp"), _fit_seconds(activity, "timestamp")
    if local is not None and utc is not None:
        offset = int(round((local - utc) / 60.0)) * 60
    date_time_utc = _naive_utc(start)
    date_time = date_time_utc + timedelta(seconds=offset or 0)

    tanks, gases = _tanks(messages), _gases(messages)
    samples = _samples(messages, start, tanks)
    duration = _int(_value(session, "total_timer_time"))
    if duration is None:
        duration = _int(_value(session, "total_elapsed_time"))
    if duration is None:
        duration = _int(_value(activity, "total_timer_time"))
    if duration is None:
        duration = samples[-1].time if samples else 0
    temps = [s.temp for s in samples if s.temp is not None]
    max_depth = _float(_value(summary, "max_depth"))
    if max_depth is None:
        max_depth = max((s.depth for s in samples), default=0.0)

    external_ids = {}
    activity_id = activity_id_from_name(name, member)
    if activity_id:
        external_ids["garmin"] = activity_id

    vendor = _value(file_id, "manufacturer")
    serial = _int(_value(file_id, "serial_number"))
    water_type = _value(settings, "water_type")
    model = _value(settings, "model")
    return UnifiedDive(
        date_time=date_time,
        date_time_utc=date_time_utc,
        timezone=zone_name(offset) if offset is not None else None,
        duration=duration,
        max_depth=max_depth,
        avg_depth=_float(_value(summary, "avg_depth")) or _average_depth(samples),
        temp_min=min(temps) if temps else _float(_value(session, "min_temperature")),
        temp_max=max(temps) if temps else _float(_value(session, "max_temperature")),
        temp_avg=_float(_value(session, "avg_temperature")),
        external_ids=external_ids,
        gas_mixtures=_gas_mixtures(gases, tanks),
        dive_number=_int(_value(summary, "dive_number")),
        lat=_degrees(_value(session, "start_position_lat")),
        lng=_degrees(_value(session, "start_position_long")),
        samples=samples,
        device_logged=True,
        events=_events(messages, start, gases, tanks),
        computer_vendor=str(vendor).capitalize() if isinstance(vendor, str) else None,
        computer_model=_model(messages),
        computer_serial=str(serial) if serial else None,
        computer_firmware=_firmware(messages),
        gf_low=_int(_value(settings, "gf_low")),
        gf_high=_int(_value(settings, "gf_high")),
        deco_model=DECO_MODELS.get(str(model), str(model)) if model is not None else None,
        water_type=str(water_type) if water_type in KNOWN_WATER_TYPES else (str(water_type) if water_type is not None else None),
        water_density=_float(_value(settings, "water_density")),
        dive_mode=dive_mode,
        exit_lat=_degrees(_value(session, "end_position_lat")),
        exit_lng=_degrees(_value(session, "end_position_long")),
        surface_interval=_int(_value(summary, "surface_interval")),
        bottom_time=_int(_value(summary, "bottom_time")),
        cns_start=_float(_value(summary, "start_cns")),
        cns_end=_float(_value(summary, "end_cns")),
    )


def read_fit(source: Union[str, "os.PathLike[str]", bytes], name: Optional[str] = None) -> UnifiedDive:
    """The dive in a Garmin ``.fit`` file, given as a path or as its bytes;
    Connect's "export original" zip around one is unwrapped. Raises
    `NotADiveError` for a file that is not a scuba dive and `FitReadError`
    for one that cannot be decoded."""
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
    else:
        path = os.fspath(source)
        name = name or os.path.basename(path)
        with open(path, "rb") as f:
            data = f.read()
    try:
        data, member = garmin_files.extract_fit_named(data)
    except Exception as e:  # a zip without a FIT, or not a zip at all
        raise FitReadError(f"not a FIT file this reader can decode: {e}") from e
    dive = to_unified(decode(data), name, member)
    logger.debug("Read dive %s of %s: %d samples, %d tanks, %d events",
                 dive.dive_number, name or "<bytes>", len(dive.samples), len(dive.gas_mixtures), len(dive.events))
    return dive
