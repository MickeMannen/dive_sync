"""Track J, J1: the unit systems module (src/core/units.py). Pure functions:
SI in, display units out, the inverses, labels and the rounding rule."""
import math

import pytest

from src.core import units
from src.core.units import IMPERIAL, METRIC


# --- systems and normalisation -------------------------------------------------

def test_systems_and_constants():
    assert units.UNIT_SYSTEMS == (METRIC, IMPERIAL)
    assert set(units.UNIT_LABELS) == {METRIC, IMPERIAL}
    assert units.FT_PER_M == 3.280839895
    assert units.PSI_PER_BAR == 14.5037738
    assert units.CUFT_PER_L == 0.0353146667
    assert units.LB_PER_KG == 2.2046226218
    assert units.KINDS == ("depth", "pressure", "temperature", "volume", "weight", "sac")


@pytest.mark.parametrize("value", ["imperial", "IMPERIAL", " Imperial ", "ft", "feet", "psi", "us"])
def test_normalize_imperial_spellings(value):
    assert units.normalize(value) == IMPERIAL


@pytest.mark.parametrize("value", ["metric", "METRIC", "m", "bar", "si", "", None, 0, "bogus"])
def test_normalize_everything_else_is_metric(value):
    # like the template: an unknown string is normalised to metric, not rejected
    assert units.normalize(value) == METRIC


def test_conversions_accept_unnormalised_spellings():
    assert units.depth(10.0, "Imperial") == pytest.approx(32.8084, abs=1e-4)
    assert units.depth(10.0, "ft") == pytest.approx(32.8084, abs=1e-4)
    assert units.depth(10.0, "bogus") == 10.0
    assert units.depth(10.0, None) == 10.0
    assert units.depth_label("FT") == "ft"


# --- conversions out (SI -> display) ---------------------------------------------

def test_known_values_imperial():
    assert units.depth(10.0, IMPERIAL) == pytest.approx(32.8, abs=0.05)
    assert units.pressure(200.0, IMPERIAL) == pytest.approx(2901, abs=0.5)
    assert units.temperature(20.0, IMPERIAL) == pytest.approx(68.0)
    assert units.volume(12.0, IMPERIAL) == pytest.approx(0.4, abs=0.05)
    assert units.weight(4.0, IMPERIAL) == pytest.approx(8.8, abs=0.05)
    assert units.sac(20.0, IMPERIAL) == pytest.approx(0.706, abs=0.001)
    assert units.volume_rate is units.sac


def test_metric_is_identity():
    for fn in (units.depth, units.pressure, units.temperature, units.volume, units.weight, units.sac):
        assert fn(12.345) == 12.345
        assert fn(12.345, METRIC) == 12.345
        assert fn(0.0, METRIC) == 0.0


def test_conversions_never_round():
    assert units.depth(10.0, IMPERIAL) == 10.0 * units.FT_PER_M
    assert units.pressure(200.0, IMPERIAL) == 200.0 * units.PSI_PER_BAR
    assert units.temperature(20.5, IMPERIAL) == 20.5 * 1.8 + 32.0
    assert units.volume(12.0, IMPERIAL) == 12.0 * units.CUFT_PER_L
    assert units.weight(4.0, IMPERIAL) == 4.0 * units.LB_PER_KG
    assert units.sac(20.0, IMPERIAL) == 20.0 * units.CUFT_PER_L


def test_temperature_fixed_points():
    assert units.temperature(0.0, IMPERIAL) == 32.0
    assert units.temperature(100.0, IMPERIAL) == 212.0
    assert units.temperature(-40.0, IMPERIAL) == -40.0
    assert units.temperature_to_celsius(32.0, IMPERIAL) == 0.0
    assert units.temperature_to_celsius(212.0, IMPERIAL) == 100.0


# --- conversions back (display -> SI) ------------------------------------------

def test_known_values_back():
    assert units.depth_to_metres(32.8084, IMPERIAL) == pytest.approx(10.0, abs=1e-4)
    assert units.pressure_to_bar(2900.75, IMPERIAL) == pytest.approx(200.0, abs=1e-3)
    assert units.temperature_to_celsius(68.0, IMPERIAL) == pytest.approx(20.0)
    assert units.volume_to_litres(0.4238, IMPERIAL) == pytest.approx(12.0, abs=1e-3)
    assert units.weight_to_kg(8.8185, IMPERIAL) == pytest.approx(4.0, abs=1e-3)
    assert units.sac_to_litres_per_min(0.7063, IMPERIAL) == pytest.approx(20.0, abs=1e-3)
    assert units.volume_rate_to_litres_per_min is units.sac_to_litres_per_min


def test_back_is_identity_in_metric():
    for fn in (units.depth_to_metres, units.pressure_to_bar, units.temperature_to_celsius,
               units.volume_to_litres, units.weight_to_kg, units.sac_to_litres_per_min):
        assert fn(7.25) == 7.25
        assert fn(7.25, METRIC) == 7.25


PAIRS = [
    (units.depth, units.depth_to_metres),
    (units.pressure, units.pressure_to_bar),
    (units.temperature, units.temperature_to_celsius),
    (units.volume, units.volume_to_litres),
    (units.weight, units.weight_to_kg),
    (units.sac, units.sac_to_litres_per_min),
]


@pytest.mark.parametrize("out, back", PAIRS, ids=[p[0].__name__ for p in PAIRS])
@pytest.mark.parametrize("value", [0.0, 0.1, 3.0, 18.7, 42.123456789, 207.0, 3000.5, -5.5])
def test_round_trip_at_full_precision(out, back, value):
    """Typed values are stored unrounded: out and back is the original to
    floating-point precision in both systems."""
    for system in (METRIC, IMPERIAL):
        assert back(out(value, system), system) == pytest.approx(value, rel=1e-12, abs=1e-12)
        assert out(back(value, system), system) == pytest.approx(value, rel=1e-12, abs=1e-12)


@pytest.mark.parametrize("kind", units.KINDS)
def test_convert_by_kind_matches_the_named_functions(kind):
    named_out = {"depth": units.depth, "pressure": units.pressure, "temperature": units.temperature,
                 "volume": units.volume, "weight": units.weight, "sac": units.sac}[kind]
    named_back = {"depth": units.depth_to_metres, "pressure": units.pressure_to_bar,
                  "temperature": units.temperature_to_celsius, "volume": units.volume_to_litres,
                  "weight": units.weight_to_kg, "sac": units.sac_to_litres_per_min}[kind]
    assert units.convert(kind, 12.5, IMPERIAL) == named_out(12.5, IMPERIAL)
    assert units.convert(kind, 12.5, METRIC) == 12.5
    assert units.convert_back(kind, 12.5, IMPERIAL) == named_back(12.5, IMPERIAL)
    assert units.convert_back(kind, units.convert(kind, 12.5, IMPERIAL), IMPERIAL) == pytest.approx(12.5, rel=1e-12)


def test_convert_by_kind_none_and_unknown_kind():
    assert units.convert("depth", None, IMPERIAL) is None
    assert units.convert_back("depth", None, IMPERIAL) is None
    with pytest.raises(KeyError):
        units.convert("altitude", 1.0, IMPERIAL)
    with pytest.raises(KeyError):
        units.convert_back("altitude", 1.0, IMPERIAL)


# --- labels -----------------------------------------------------------------------

def test_labels():
    assert (units.depth_label(METRIC), units.depth_label(IMPERIAL)) == ("m", "ft")
    assert (units.pressure_label(METRIC), units.pressure_label(IMPERIAL)) == ("bar", "psi")
    assert (units.temperature_label(METRIC), units.temperature_label(IMPERIAL)) == ("°C", "°F")
    assert (units.volume_label(METRIC), units.volume_label(IMPERIAL)) == ("L", "cuft")
    assert (units.weight_label(METRIC), units.weight_label(IMPERIAL)) == ("kg", "lb")
    assert (units.sac_label(METRIC), units.sac_label(IMPERIAL)) == ("L/min", "cuft/min")
    assert units.volume_rate_label is units.sac_label
    # metric is the default
    assert units.depth_label() == "m"
    assert units.pressure_label() == "bar"


@pytest.mark.parametrize("kind, metric, imperial", [
    ("depth", "m", "ft"),
    ("pressure", "bar", "psi"),
    ("temperature", "°C", "°F"),
    ("volume", "L", "cuft"),
    ("weight", "kg", "lb"),
    ("sac", "L/min", "cuft/min"),
])
def test_unit_label_by_kind(kind, metric, imperial):
    assert units.unit_label(kind) == metric
    assert units.unit_label(kind, METRIC) == metric
    assert units.unit_label(kind, IMPERIAL) == imperial


def test_unit_label_unknown_kind_is_blank():
    assert units.unit_label("altitude", IMPERIAL) == ""
    assert units.unit_label("", METRIC) == ""


# --- formatting: the rounding rule ------------------------------------------------

def test_decimals_rule():
    assert units.decimals("depth", METRIC) == 1 and units.decimals("depth", IMPERIAL) == 0
    assert units.decimals("pressure", METRIC) == 0 and units.decimals("pressure", IMPERIAL) == 0
    assert units.decimals("temperature", METRIC) == 1 and units.decimals("temperature", IMPERIAL) == 0
    assert units.decimals("volume", METRIC) == 1 and units.decimals("volume", IMPERIAL) == 1
    assert units.decimals("weight", METRIC) == 1 and units.decimals("weight", IMPERIAL) == 1
    assert units.decimals("sac", METRIC) == 1 and units.decimals("sac", IMPERIAL) == 1


def test_format_depth():
    assert units.format_depth(10.0) == "10.0"
    assert units.format_depth(18.74, METRIC) == "18.7"
    assert units.format_depth(18.75, METRIC) == "18.8"
    assert units.format_depth(10.0, IMPERIAL) == "33"          # 32.8 ft, whole feet
    assert units.format_depth(18.74, IMPERIAL) == "61"         # 61.48 ft
    assert units.format_depth(0.0, IMPERIAL) == "0"


def test_format_pressure():
    assert units.format_pressure(200.0) == "200"
    assert units.format_pressure(207.4, METRIC) == "207"
    assert units.format_pressure(200.0, IMPERIAL) == "2901"    # 2900.75 psi
    assert units.format_pressure(50.0, IMPERIAL) == "725"      # 725.19 psi


def test_format_temperature():
    assert units.format_temperature(20.0) == "20.0"
    assert units.format_temperature(17.26, METRIC) == "17.3"
    assert units.format_temperature(20.0, IMPERIAL) == "68"
    assert units.format_temperature(17.26, IMPERIAL) == "63"   # 63.07 °F
    assert units.format_temperature(-1.0, IMPERIAL) == "30"    # 30.2 °F


def test_format_volume():
    assert units.format_volume(12.0) == "12.0"
    assert units.format_volume(11.1, METRIC) == "11.1"
    assert units.format_volume(12.0, IMPERIAL) == "0.4"        # 0.42 cuft
    assert units.format_volume(80.0, IMPERIAL) == "2.8"        # 2.83 cuft


def test_format_weight():
    assert units.format_weight(4.0) == "4.0"
    assert units.format_weight(4.26, METRIC) == "4.3"
    assert units.format_weight(4.0, IMPERIAL) == "8.8"         # 8.82 lb
    assert units.format_weight(6.0, IMPERIAL) == "13.2"        # 13.23 lb


def test_format_sac():
    assert units.format_sac(14.23) == "14.2"
    assert units.format_sac(20.0, METRIC) == "20.0"
    assert units.format_sac(20.0, IMPERIAL) == "0.7"           # 0.706 cuft/min
    assert units.format_sac(40.0, IMPERIAL) == "1.4"           # 1.41 cuft/min


def test_format_decimals_override():
    assert units.format_depth(10.0, IMPERIAL, decimals=1) == "32.8"
    assert units.format_depth(10.0, METRIC, decimals=0) == "10"
    assert units.format_pressure(200.0, IMPERIAL, decimals=2) == "2900.75"


@pytest.mark.parametrize("fn", [units.format_depth, units.format_pressure, units.format_temperature,
                                units.format_volume, units.format_weight, units.format_sac])
def test_format_none_is_blank(fn):
    assert fn(None) == ""
    assert fn(None, METRIC) == ""
    assert fn(None, IMPERIAL) == ""
    assert fn("") == ""


def test_format_accepts_ints_and_numeric_strings():
    assert units.format_depth(10, IMPERIAL) == "33"
    assert units.format_pressure("200", IMPERIAL) == "2901"
    assert units.format_weight(4, METRIC) == "4.0"


def test_format_value_by_kind_and_unknown_kind():
    assert units.format_value("depth", 10.0, IMPERIAL) == "33"
    assert units.format_value("depth", None, IMPERIAL) == ""
    with pytest.raises(KeyError):
        units.format_value("altitude", 1.0, IMPERIAL)
    with pytest.raises(KeyError):
        units.decimals("altitude", IMPERIAL)


def test_format_with_label():
    assert units.format_with_label("depth", 10.0, IMPERIAL) == "33 ft"
    assert units.format_with_label("depth", 10.0, METRIC) == "10.0 m"
    assert units.format_with_label("pressure", 200.0, IMPERIAL) == "2901 psi"
    assert units.format_with_label("temperature", 20.0) == "20.0 °C"
    assert units.format_with_label("weight", 4.0, IMPERIAL) == "8.8 lb"
    assert units.format_with_label("depth", None, IMPERIAL) == ""


def test_module_is_pure():
    """No settings read, no Qt: nothing of the template's setting plumbing
    came over, and importing the module pulls in neither PySide6 nor desktop."""
    import sys
    assert not any(hasattr(units, attr) for attr in ("current_units", "set_units", "reload_units",
                                                     "units_override", "set_units_override", "resolve_units"))
    assert "PySide6" not in getattr(units, "__dict__", {})
    source = open(units.__file__, encoding="utf-8").read()
    assert "PySide6" not in source and "desktop" not in source.replace("desktop preference", "")
    assert "load_settings" not in source and "ConfigManager" not in source
    assert not math.isnan(units.depth(1.0, IMPERIAL))
