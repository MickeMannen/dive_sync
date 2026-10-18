"""What the dive editors share (plans/units.md J3, plans/convert.md I14):
how a stored SI number is shown in the chosen unit system and how a typed
one comes back - without drift. The Dives pages (`controllers/dives.py`)
and the Dive edit page (`controllers/convert.py`) both edit through these;
neither page's own staging lives here.

The no-drift rule: a form shows a stored value rounded for the system
(``30.48 m`` as ``"100"`` ft). When the diver leaves the field as it was
shown, the stored value must survive exactly, so a typed value is first
compared with the text the form showed on arrival (`js_number` of the
display) and only a text that differs is converted back, at full
precision (`si_number`).
"""
from __future__ import annotations

from typing import Any, Optional

from src.core import units as core_units


def optional_float(text: Any) -> Optional[float]:
    """``float`` of a field's text; None for blank or no number."""
    text = ("" if text is None else str(text)).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def js_number(value: Any) -> str:
    """A value as the form's text field shows it: QML's ``String()`` of a
    number (``"12"`` for 12.0, ``"28.5"``), text as it is, ``""`` for None.
    What a typed value is compared against to tell an edit from an
    untouched field."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else repr(float(value))
    return "" if value is None else str(value)


def display_number(kind: str, value: Any, system: str) -> str:
    """A stored SI number (or its text, ``"30.20"``) as the page shows it:
    as it is in metric, converted and rounded per the display rule in
    imperial; blank and unparseable values stay what they are."""
    if system == core_units.METRIC or value in (None, ""):
        return js_number(value)
    try:
        return core_units.format_value(kind, float(value), system)
    except (TypeError, ValueError):
        return str(value)


def si_number(kind: str, typed: Any, shown: str, original: Any, system: str) -> Any:
    """What a typed number stands for in SI: the stored value itself while
    the field still shows what it showed on arrival (so the rounded display
    never overwrites the stored value), else the typed value converted back
    at full precision. Blank and unparseable text pass through as typed, and
    so does every value in metric (no conversion to do)."""
    typed_text = ("" if typed is None else str(typed)).strip()
    if typed_text == shown.strip():
        return original if original not in (None, "") else typed_text
    value = optional_float(typed_text)
    if value is None or system == core_units.METRIC:
        return typed_text
    return core_units.convert_back(kind, value, system)
