"""What the depth profile shows for one sample under the mouse
(plans/convert.md I19): the gas breathed at every moment of the dive, the
gas-switch markers, the deco ceiling band and one sample's data as
display-ready text lines in the chosen unit system.

No Qt here: ``ProfileChart.qml`` asks a controller slot for one sample's
``sample_details`` on hover (never a per-sample string array across the
bridge), and the controllers hand the chart ``gas_switch_markers`` and
``ceiling_series`` once per selected dive. Everything takes the SI
``UnifiedDive`` and converts through ``src/core/units.py``.

The active gas is read from the gas list and the ``gas_switch`` events:
the dive starts on the first gas of ``gas_mixtures`` unless a switch at or
before the first sample names another; every later switch changes it. A
switch names a gas by its tank index (``DiveEvent.tank``) or by its mix
(``oxygen`` / ``helium``); a mix not in the gas list keeps the name the
mix gives it (``"EAN50"``) rather than being dropped, and a switch naming
neither a listed tank nor a mix is ignored.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from src.core import units as core_units
from src.core.models import EVENT_GAS_SWITCH, SampleChannels, UnifiedDive, UnifiedSample

# A timeline entry's gas: the index into UnifiedDive.gas_mixtures, or the
# name of a mix the gas list does not hold.
Gas = Union[int, str]

# The tooltip's line order (ProfileChart.qml draws `lines`; the keys are for tests and callers).
DETAIL_ORDER = ("time", "depth", "temp", "ndl", "deco", "gas", "tanks", "cns", "po2", "setpoint", "cells", "heart_rate", "gas_time")


# --- gas names ----------------------------------------------------------------

def gas_name(mix: Any, helium: Optional[float] = None) -> str:
    """The name of a mix: ``Air`` (21/0, within half a percent), ``EAN32``
    (nitrox, the O2 percentage), ``O2`` (100 %), ``21/35`` (trimix, O2/He).
    ``mix`` is anything with ``oxygen`` / ``helium`` attributes (a
    ``GasMixture``, a ``DiveEvent``) or the oxygen percentage itself with
    ``helium`` as the second argument; a missing oxygen means air."""
    if hasattr(mix, "oxygen"):
        oxygen, helium = mix.oxygen, getattr(mix, "helium", None)
    else:
        oxygen = mix
    o2 = 21.0 if oxygen is None else float(oxygen)
    he = float(helium or 0.0)
    if he > 0:
        return f"{o2:g}/{he:g}"
    if o2 >= 99.5:
        return "O2"
    if abs(o2 - 21.0) <= 0.5:
        return "Air"
    return f"EAN{o2:g}"


def _same_mix(a: Any, b: Any) -> bool:
    return abs(float(a.oxygen or 0) - float(b.oxygen or 0)) <= 0.5 and abs(float(a.helium or 0) - float(b.helium or 0)) <= 0.5


def _switch_gas(dive: UnifiedDive, event: Any) -> Optional[Gas]:
    """What a gas-switch event names: a listed tank's index, else the index
    of the listed gas with the event's mix, else the mix's own name, else
    None (nothing to switch to)."""
    if event.tank is not None and 0 <= event.tank < len(dive.gas_mixtures):
        return int(event.tank)
    if event.oxygen is None and event.helium is None:
        return None
    for i, gm in enumerate(dive.gas_mixtures):
        if _same_mix(gm, event):
            return i
    return gas_name(event)


def tank_label(dive: UnifiedDive, index: int) -> str:
    """A tank's name on the page: its ``tank_name`` when the file has one,
    else ``Tank N`` (1-based)."""
    if 0 <= index < len(dive.gas_mixtures) and dive.gas_mixtures[index].tank_name:
        return dive.gas_mixtures[index].tank_name
    return f"Tank {index + 1}"


def gas_label(dive: UnifiedDive, gas: Optional[Gas], with_tank: bool = True) -> str:
    """The text for a timeline gas: the mix's name, followed by the tank in
    brackets (``Air (Tank 2)``) when ``with_tank`` and the dive has more
    than one tank, so two tanks of the same mix can be told apart."""
    if gas is None:
        return ""
    if isinstance(gas, str):
        return gas
    name = gas_name(dive.gas_mixtures[gas]) if 0 <= gas < len(dive.gas_mixtures) else f"Tank {gas + 1}"
    if with_tank and len(dive.gas_mixtures) > 1:
        return f"{name} ({tank_label(dive, gas)})"
    return name


# --- the active gas over time -----------------------------------------------

def _start_time(dive: UnifiedDive) -> int:
    return int(dive.samples[0].time or 0) if dive.samples else 0


def active_gas_timeline(dive: UnifiedDive) -> List[Tuple[int, Gas]]:
    """``[(time, gas), ...]`` in time order: the gas breathed from ``time``
    on. The first entry is the starting gas at the first sample's time (the
    first listed gas, or what a switch at or before the start names); every
    later switch adds an entry (two at the same second: the later one
    wins). A dive without gases and without switches has no timeline."""
    start = _start_time(dive)
    timeline: List[Tuple[int, Gas]] = []
    if dive.gas_mixtures:
        timeline.append((start, 0))
    switches = sorted((e for e in dive.events if e.type == EVENT_GAS_SWITCH), key=lambda e: e.time)
    for event in switches:
        gas = _switch_gas(dive, event)
        if gas is None:
            continue
        at = max(int(event.time), start)
        if timeline and timeline[-1][0] >= at:
            timeline[-1] = (at, gas)
        else:
            timeline.append((at, gas))
    return timeline


def active_gas_at(timeline: Sequence[Tuple[int, Gas]], t: int) -> Optional[Gas]:
    """The gas breathed at second ``t``: the last entry at or before ``t``
    (a switch applies from its own second), the starting gas before the
    first entry, None for an empty timeline."""
    if not timeline:
        return None
    i = bisect_right([time for time, _gas in timeline], t)
    return timeline[max(i - 1, 0)][1]


def gas_switch_markers(dive: UnifiedDive) -> List[Dict[str, Any]]:
    """The gas switches after the start as ``{time, label}`` for the chart's
    ticks: the label is the gas's name, or the tank's when another listed
    tank holds the same mix (two Air tanks: ``Tank 2``)."""
    timeline = active_gas_timeline(dive)
    markers = []
    for time, gas in timeline[1:]:
        label = gas_label(dive, gas, with_tank=False)
        if isinstance(gas, int) and sum(1 for gm in dive.gas_mixtures if gas_name(gm) == label) > 1:
            label = tank_label(dive, gas)
        markers.append({"time": time, "label": label})
    return markers


# --- the deco ceiling ---------------------------------------------------------

def ceiling_series(dive: UnifiedDive) -> List[Dict[str, Any]]:
    """``{time, depth}`` (metres) of the next deco stop at every sample that
    logs one, for the chart's shaded band from the surface down to it;
    empty when no sample of the dive has a stop deeper than 0, so a
    no-deco dive draws no band."""
    series = []
    deepest = 0.0
    for s in dive.samples:
        if s.channels is None or s.channels.deco_stop_depth is None:
            continue
        depth = float(s.channels.deco_stop_depth)
        deepest = max(deepest, depth)
        series.append({"time": int(s.time or 0), "depth": depth})
    return series if deepest > 0 else []


# --- one sample's details -----------------------------------------------------

def clock_text(seconds: int) -> str:
    """``m:ss`` (``46:20``), ``h:mm:ss`` from an hour on."""
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _minutes(seconds: Optional[int]) -> str:
    return f"{int(round(float(seconds) / 60.0))} min"


def _depth_text(metres: float, system: str) -> str:
    """A depth with the display rule, except that a whole number of metres
    (a stop depth) is shown without the ``.0``."""
    decimals = 0 if core_units.normalize(system) == core_units.METRIC and float(metres).is_integer() else None
    return core_units.format_with_label("depth", metres, system, decimals)


_HELD_CHANNELS = ("tts", "deco_stop_time", "cns", "ppo2", "setpoint", "gf99", "gas_time", "heart_rate")


def held_values(dive: UnifiedDive, index: int) -> Tuple[Optional[float], Dict[int, float], Optional[SampleChannels]]:
    """Sample ``index``'s temperature, tank pressures (per tank) and
    channels, each value it does not log taken from the last sample before
    it that does: a computer logs a tank pressure, the temperature or the
    NDL every few samples, so most samples miss some. NDL and the next
    deco stop are one reading: whichever was logged last wins, so a stop
    held from earlier never hides a newer NDL. A rebreather's O2 cells are
    held one by one, like the tank pressures. Nothing is invented before
    the first reading."""
    temp: Optional[float] = None
    pressures: Dict[int, float] = {}
    cells: Dict[int, float] = {}
    held: Dict[str, Any] = {}
    deco: Dict[str, Any] = {}
    for s in dive.samples[:index + 1]:
        if s.temp is not None:
            temp = s.temp
        ch = s.channels
        if ch is not None and ch.pressures:
            pressures.update(ch.pressures)
        elif s.pressure is not None:
            pressures[0] = s.pressure
        if ch is None:
            continue
        cells.update(ch.o2_cells)
        if ch.deco_stop_depth is not None or ch.ndl is not None:
            deco = {"deco_stop_depth": ch.deco_stop_depth, "ndl": ch.ndl}
        for name in _HELD_CHANNELS:
            value = getattr(ch, name)
            if value is not None:
                held[name] = value
    values = {**held, **{k: v for k, v in deco.items() if v is not None}}
    if cells:
        values["o2_cells"] = cells
    return temp, pressures, (SampleChannels(**values) if values else None)


def cells_text(cells: Dict[int, float]) -> str:
    """The O2 cells on one line, in cell order, ``Cells 1.22 / 1.19 / 1.21
    bar``; a cell between two others that has no reading is a dash."""
    top = max(cells)
    return "Cells " + " / ".join(f"{cells[i]:.2f}" if i in cells else "-" for i in range(top + 1)) + " bar"


def sample_details(dive: UnifiedDive, index: int, system: str = core_units.METRIC) -> Dict[str, Any]:
    """The tooltip for sample ``index`` of ``dive``: display-ready text per
    key, only the keys the sample has data for, plus ``lines`` (the texts in
    ``DETAIL_ORDER``, the tanks one line each). ``time`` (``m:ss``),
    ``depth`` and ``temp`` in ``system``; ``ndl`` (``NDL 14 min``) when the
    computer logs one and the sample is not in deco; ``deco`` (``Deco: stop
    6 m for 3 min, TTS 12 min``) when the next stop is deeper than 0;
    ``gas`` (the active gas); ``tanks`` (``Tank 1 162 bar`` per tank
    pressure the sample holds, named as on the page); ``cns``, ``po2``,
    ``setpoint``, ``cells`` (a rebreather's O2 cells, ``Cells 1.22 / 1.19 /
    1.21 bar``), ``heart_rate``, ``gas_time``. A value the sample does not
    log is the last one logged before it (`held_values`): computers log a
    tank pressure or the NDL every few samples, not at each. ``{}`` for an
    index outside the profile."""
    if not 0 <= index < len(dive.samples):
        return {}
    s = dive.samples[index]
    out: Dict[str, Any] = {"time": clock_text(s.time or 0), "depth": core_units.format_with_label("depth", s.depth, system)}
    temp, pressures, ch = held_values(dive, index)
    if temp is not None:
        out["temp"] = core_units.format_with_label("temperature", temp, system)
    if ch is not None:
        in_deco = (ch.deco_stop_depth or 0) > 0
        if in_deco:
            deco = f"Deco: stop {_depth_text(ch.deco_stop_depth, system)}"
            if ch.deco_stop_time is not None:
                deco += f" for {_minutes(ch.deco_stop_time)}"
            if ch.tts is not None:
                deco += f", TTS {_minutes(ch.tts)}"
            out["deco"] = deco
        elif ch.ndl is not None:
            out["ndl"] = f"NDL {_minutes(ch.ndl)}"
    gas = active_gas_at(active_gas_timeline(dive), int(s.time or 0))
    if gas is not None:
        out["gas"] = gas_label(dive, gas)
    if pressures:
        out["tanks"] = [f"{tank_label(dive, i)} {core_units.format_with_label('pressure', bar, system)}"
                        for i, bar in sorted(pressures.items())]
    if ch is not None:
        if ch.cns is not None:
            out["cns"] = f"CNS {ch.cns:g} %"
        if ch.ppo2 is not None:
            out["po2"] = f"PO2 {ch.ppo2:.2f} bar"
        if ch.setpoint is not None:
            out["setpoint"] = f"Setpoint {ch.setpoint:.2f} bar"
        if ch.o2_cells:
            out["cells"] = cells_text(ch.o2_cells)
        if ch.heart_rate is not None:
            out["heart_rate"] = f"HR {int(ch.heart_rate)} bpm"
        if ch.gas_time is not None:
            out["gas_time"] = f"Gas time {_minutes(ch.gas_time)}"
    out["lines"] = detail_lines(out)
    return out


def detail_lines(details: Dict[str, Any]) -> List[str]:
    """The tooltip's lines in ``DETAIL_ORDER``: one per key present, the
    tanks one line each."""
    lines: List[str] = []
    for key in DETAIL_ORDER:
        value = details.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, list):
            lines.extend(str(v) for v in value)
        else:
            lines.append(str(value))
    return lines


def dive_from_samples(samples: Sequence[Dict[str, Any]]) -> UnifiedDive:
    """A bare ``UnifiedDive`` around a cache's sample list (``{time, depth,
    temp}`` dicts, ``dive_cache.get_samples``' shape) so the dives pages'
    chart can use the same helpers; no gases, no events, so the tooltip
    there shows time, depth and temperature only."""
    kept = [UnifiedSample(depth=float(s.get("depth") or 0.0), temp=s.get("temp"), time=int(s.get("time") or 0),
                          pressure=s.get("pressure"))
            for s in samples if s.get("depth") is not None]
    return UnifiedDive(date_time=datetime(1970, 1, 1), duration=kept[-1].time if kept else 0,
                       max_depth=max((s.depth for s in kept), default=0.0), samples=kept)


__all__ = ["DETAIL_ORDER", "active_gas_at", "active_gas_timeline", "ceiling_series", "cells_text", "clock_text",
           "detail_lines", "dive_from_samples", "gas_label", "gas_name", "gas_switch_markers", "held_values", "sample_details",
           "tank_label"]
