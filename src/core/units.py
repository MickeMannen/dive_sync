"""Unit systems for everything the app shows (rework.md Track J, plans/units.md).

Every value in the data model stays SI: metres, bar, °C, litres, kilograms
(the adapters write SI, the sync engine compares SI, the caches and file
writers store SI). Only the display converts, here. This module reads no
setting and imports no Qt: every function takes the unit system as an
explicit ``units`` argument (default metric), and the front ends pass the
one they hold (the desktop preference, the CLI's ``--units``, the web
container's own setting).

Display rounding follows the dive computer (owner, 2026-10-18): imperial
shows whole feet, whole psi and whole °F, pounds and cubic feet with one
decimal; metric keeps the app's current decimals (0.1 m, whole bar, 0.1 °C,
litres and kilograms with one decimal). Only the ``format_*`` helpers round;
the conversions themselves are exact in both directions, so a value typed in
imperial converts back to SI at full precision before it is stored.

Copied from UWMedia's ``utils/units.py`` (same author) minus the overlay-only
parts (stop depths, per-brand display rules, layout overrides, the settings
reading); weight added since this app edits lead weight.
"""
from typing import Any, Optional

METRIC = "metric"
IMPERIAL = "imperial"
UNIT_SYSTEMS = (METRIC, IMPERIAL)
UNIT_LABELS = {METRIC: "Metric (m, bar, °C, L, kg)", IMPERIAL: "Imperial (ft, psi, °F, cuft, lb)"}

FT_PER_M = 3.280839895
PSI_PER_BAR = 14.5037738
CUFT_PER_L = 0.0353146667
LB_PER_KG = 2.2046226218

# The kinds `unit_label`, `convert` and `convert_back` know, in the order the
# front ends list them.
KINDS = ("depth", "pressure", "temperature", "volume", "weight", "sac")


def normalize(value: Any) -> str:
    """The unit system a setting / flag value names: ``"imperial"`` for the
    imperial spellings (``imperial``, ``ft``, ``feet``, ``psi``, ``us``),
    ``"metric"`` for anything else (including ``None`` and the unknown)."""
    value = str(value or "").strip().lower()
    return IMPERIAL if value in (IMPERIAL, "ft", "feet", "psi", "us") else METRIC


def _imperial(units: Optional[str]) -> bool:
    return normalize(units) == IMPERIAL


# --- conversions (SI in, display units out) ---------------------------------

def depth(metres: float, units: str = METRIC) -> float:
    """Metres, or feet in imperial."""
    return metres * FT_PER_M if _imperial(units) else metres


def pressure(bar: float, units: str = METRIC) -> float:
    """Bar, or psi in imperial."""
    return bar * PSI_PER_BAR if _imperial(units) else bar


def temperature(celsius: float, units: str = METRIC) -> float:
    """°C, or °F in imperial."""
    return celsius * 1.8 + 32.0 if _imperial(units) else celsius


def volume(litres: float, units: str = METRIC) -> float:
    """Litres (a cylinder's water volume), or cubic feet in imperial."""
    return litres * CUFT_PER_L if _imperial(units) else litres


def weight(kg: float, units: str = METRIC) -> float:
    """Kilograms (lead weight), or pounds in imperial."""
    return kg * LB_PER_KG if _imperial(units) else kg


def sac(litres_per_min: float, units: str = METRIC) -> float:
    """Volumetric SAC: L/min, or cuft/min in imperial (the app's SAC is
    volumetric, ``dive_cache._format_sac``)."""
    return litres_per_min * CUFT_PER_L if _imperial(units) else litres_per_min


volume_rate = sac   # UWMedia's name for the same thing


# --- conversions back (display units in, SI out) ----------------------------

def depth_to_metres(value: float, units: str = METRIC) -> float:
    return value / FT_PER_M if _imperial(units) else value


def pressure_to_bar(value: float, units: str = METRIC) -> float:
    return value / PSI_PER_BAR if _imperial(units) else value


def temperature_to_celsius(value: float, units: str = METRIC) -> float:
    return (value - 32.0) / 1.8 if _imperial(units) else value


def volume_to_litres(value: float, units: str = METRIC) -> float:
    return value / CUFT_PER_L if _imperial(units) else value


def weight_to_kg(value: float, units: str = METRIC) -> float:
    return value / LB_PER_KG if _imperial(units) else value


def sac_to_litres_per_min(value: float, units: str = METRIC) -> float:
    return value / CUFT_PER_L if _imperial(units) else value


volume_rate_to_litres_per_min = sac_to_litres_per_min


_CONVERT = {
    "depth": depth, "pressure": pressure, "temperature": temperature,
    "volume": volume, "weight": weight, "sac": sac,
}
_CONVERT_BACK = {
    "depth": depth_to_metres, "pressure": pressure_to_bar, "temperature": temperature_to_celsius,
    "volume": volume_to_litres, "weight": weight_to_kg, "sac": sac_to_litres_per_min,
}


def convert(kind: str, value: Optional[float], units: str = METRIC) -> Optional[float]:
    """SI in, display units out, by kind (one of ``KINDS``); ``None`` stays
    ``None``. Raises ``KeyError`` for an unknown kind."""
    return None if value is None else _CONVERT[kind](value, units)


def convert_back(kind: str, value: Optional[float], units: str = METRIC) -> Optional[float]:
    """Display units in, SI out, by kind; the inverse of ``convert`` at full
    precision (what a typed value goes through before it is stored)."""
    return None if value is None else _CONVERT_BACK[kind](value, units)


# --- labels -------------------------------------------------------------------

def depth_label(units: str = METRIC) -> str:
    return "ft" if _imperial(units) else "m"


def pressure_label(units: str = METRIC) -> str:
    return "psi" if _imperial(units) else "bar"


def temperature_label(units: str = METRIC) -> str:
    return "°F" if _imperial(units) else "°C"


def volume_label(units: str = METRIC) -> str:
    return "cuft" if _imperial(units) else "L"


def weight_label(units: str = METRIC) -> str:
    return "lb" if _imperial(units) else "kg"


def sac_label(units: str = METRIC) -> str:
    return "cuft/min" if _imperial(units) else "L/min"


volume_rate_label = sac_label

_LABELS = {
    "depth": depth_label, "pressure": pressure_label, "temperature": temperature_label,
    "volume": volume_label, "weight": weight_label, "sac": sac_label,
}


def unit_label(kind: str, units: str = METRIC) -> str:
    """The unit label for a kind (depth, pressure, temperature, volume,
    weight, sac) in ``units``; ``""`` for an unknown kind."""
    label = _LABELS.get(kind)
    return label(units) if label else ""


# --- formatting (the rounding rule; None formats as "") ----------------------

# Decimals per kind and system: the display rule from plans/units.md.
DECIMALS = {
    "depth": {METRIC: 1, IMPERIAL: 0},
    "pressure": {METRIC: 0, IMPERIAL: 0},
    "temperature": {METRIC: 1, IMPERIAL: 0},
    "volume": {METRIC: 1, IMPERIAL: 1},
    "weight": {METRIC: 1, IMPERIAL: 1},
    "sac": {METRIC: 1, IMPERIAL: 1},
}


def decimals(kind: str, units: str = METRIC) -> int:
    """How many decimals a kind is shown with in ``units``."""
    return DECIMALS[kind][normalize(units)]


def format_value(kind: str, value: Optional[float], units: str = METRIC, decimals: Optional[int] = None) -> str:
    """``value`` (SI) converted to ``units`` and rounded per the display rule
    (or to ``decimals`` when given), as text without the unit label;
    ``None`` (and ``""``) formats as ``""``, like the rest of the app."""
    if value is None or value == "":
        return ""
    d = DECIMALS[kind][normalize(units)] if decimals is None else decimals
    return f"{_CONVERT[kind](float(value), units):.{d}f}"


def format_depth(metres: Optional[float], units: str = METRIC, decimals: Optional[int] = None) -> str:
    return format_value("depth", metres, units, decimals)


def format_pressure(bar: Optional[float], units: str = METRIC, decimals: Optional[int] = None) -> str:
    return format_value("pressure", bar, units, decimals)


def format_temperature(celsius: Optional[float], units: str = METRIC, decimals: Optional[int] = None) -> str:
    return format_value("temperature", celsius, units, decimals)


def format_volume(litres: Optional[float], units: str = METRIC, decimals: Optional[int] = None) -> str:
    return format_value("volume", litres, units, decimals)


def format_weight(kg: Optional[float], units: str = METRIC, decimals: Optional[int] = None) -> str:
    return format_value("weight", kg, units, decimals)


def format_sac(litres_per_min: Optional[float], units: str = METRIC, decimals: Optional[int] = None) -> str:
    return format_value("sac", litres_per_min, units, decimals)


def format_with_label(kind: str, value: Optional[float], units: str = METRIC, decimals: Optional[int] = None) -> str:
    """``format_value`` followed by the unit label (``"32 ft"``, ``"10.0 m"``);
    ``""`` for ``None``."""
    text = format_value(kind, value, units, decimals)
    return f"{text} {unit_label(kind, units)}" if text else ""
