"""What the front ends show for a recorded value in the chosen unit system
(rework.md Track J, plans/units.md J5/J6).

A conflict, a test-mapping row or a run's conflict record holds both values
as the services hold them: SI numbers (metres, °C, bar, litres) and a
weight or visibility as ``[number, unit]`` in the dive's own unit
(``[4.0, "kilogram"]``, ``[10.0, "pound"]``). Only what is shown converts,
through src/core/units.py; nothing here writes anything back, and the
decision a conflict gets is a side, never a value.

The kind of a field (depth, temperature, pressure, volume, weight) is found
from the field catalogue (``FieldSpec.unit``, else the own-unit quantity the
spec's ``unified`` names), never from the field's name. One implementation
for the desktop Conflicts page (``desktop/controllers/conflicts.py``), the
CLI (``sync.py --units``) and the web dashboard (``src/web/app.py``).
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from src.core import units as core_units
from src.core.conflicts import display_value as brief

# The units kind of a catalogue number field, by the unit the catalogue
# gives it ("s" for a duration is no kind: seconds stay seconds) ...
_UNIT_KINDS = {"m": "depth", "°C": "temperature", "bar": "pressure", "L": "volume", "kg": "weight"}
# ... or, for the two quantities whose values carry their own unit and whose
# catalogue entry therefore has none, by the UnifiedDive quantity it is.
_UNIFIED_KINDS = {"weight": "weight", "visibility": "depth"}
# How the services spell an imperial weight / visibility unit (``weight_unit``
# "pound", ``visibility_unit`` "foot" / "feet"; the short forms for safety).
_IMPERIAL_UNIT_PREFIXES = ("pound", "lb", "foot", "feet", "ft")


def quantity_kind(spec) -> str:
    """The src/core/units.py kind (depth, temperature, pressure, volume,
    weight) of a catalogue field, found from the catalogue, not the name:
    its ``unit`` first, else the own-unit quantity its ``unified`` names.
    ``""`` for anything that is no quantity - text, dates, GPS, a duration,
    tanks and profiles (shown as a count), or no catalogue entry at all."""
    if spec is None or spec.type != "number":
        return ""
    return _UNIT_KINDS.get(spec.unit or "") or _UNIFIED_KINDS.get(spec.unified or "") or ""


def field_spec(key: str):
    """The catalogue entry of a ``<service>.<name>`` key; None for a key no
    catalogue has (an unknown service, or a rendered template's source)."""
    from src.core.pairs import field_catalog_of
    service, _, _name = key.partition(".")
    try:
        return next((f for f in field_catalog_of(service) if f.key == key), None)
    except ValueError:
        return None


def conflict_kinds(source_spec, target_spec) -> Tuple[str, str]:
    """The kinds of a linked pair of fields: a linked pair of number fields
    is the same quantity on both sides, so a side the catalogue does not
    know takes the other's kind. ``("", "")`` when neither is a quantity."""
    source_kind = quantity_kind(source_spec) or quantity_kind(target_spec)
    target_kind = quantity_kind(target_spec) or quantity_kind(source_spec)
    return source_kind, target_kind


def _own_system(unit: Any) -> str:
    """The system a recorded weight / visibility unit belongs to; none
    (a bare SI number) is metric."""
    return core_units.IMPERIAL if str(unit or "").lower().startswith(_IMPERIAL_UNIT_PREFIXES) else core_units.METRIC


def _split_quantity(value: Any):
    """``[4.0, "kilogram"]`` -> (4.0, "metric"), ``30.2`` / ``"30.2"`` ->
    (30.2, "metric"); (None, "") when there is no number to convert."""
    unit = None
    if isinstance(value, (list, tuple)):
        if len(value) != 2:
            return None, ""
        value, unit = value
    if isinstance(value, bool) or value in (None, ""):
        return None, ""
    try:
        return float(value), _own_system(unit)
    except (TypeError, ValueError):
        return None, ""


def quantity_text(value: Any, kind: str, system: str) -> str:
    """A recorded value of ``kind`` as the desktop page shows it in
    ``system``: today's text (``brief``) when it is a metric value shown in
    metric, so nothing changes for a metric user; otherwise converted from
    the unit it is recorded in (SI, or the dive's own weight / visibility
    unit) through src/core/units.py, rounded per the display rule and
    labelled (``"99 ft"``, ``"8.8 lb"``; a pound weight shown in imperial
    reads ``"10.0 lb"``, in metric ``"4.5 kg"``). Values that are no
    number, and every value of no kind, are ``brief`` as before."""
    number, own = _split_quantity(value) if kind else (None, "")
    if number is None or own == system == core_units.METRIC:
        return brief(value)
    return core_units.format_with_label(kind, core_units.convert_back(kind, number, own), system)


def converted_text(value: Any, field_type: str, kind: str, system: str) -> Optional[str]:
    """The text the CLI and the web dashboard show instead of their usual
    rendering of a recorded value, or None when they show it as they always
    have: only a number-typed value of a quantity ``kind`` under imperial
    converts (``"99 ft"``, ``"76 °F"``, ``"10.0 lb"``). Metric is left
    entirely to the caller, so a metric run prints byte for byte what it
    printed before Track J (a weight logged in pounds stays ``10.0, pound``
    there, unlike the desktop page's ``quantity_text``)."""
    if core_units.normalize(system) != core_units.IMPERIAL or field_type != "number" or not kind:
        return None
    number, _own = _split_quantity(value)
    if number is None:
        return None
    return quantity_text(value, kind, core_units.IMPERIAL)


def conflict_texts(conflict, system: str, catalog: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], Optional[str]]:
    """``converted_text`` for both sides of a ``Conflict`` (or a dict with
    the same keys, as a run's results and the history hold them): the kinds
    come from ``catalog`` (key -> FieldSpec, an engine's) when given, else
    from the services' own catalogues. ``(None, None)`` when nothing
    converts, which is always the case in metric."""
    if core_units.normalize(system) != core_units.IMPERIAL:
        return None, None
    get = conflict.get if isinstance(conflict, dict) else lambda k, d=None: getattr(conflict, k, d)
    source_key, target_key = get("source_key") or "", get("target_key") or ""
    lookup = catalog.get if catalog is not None else field_spec
    source_kind, target_kind = conflict_kinds(lookup(source_key), lookup(target_key))
    return (converted_text(get("source_value"), get("source_type") or "text", source_kind, system),
            converted_text(get("target_value"), get("field_type") or "text", target_kind, system))


def row_texts(row: Dict[str, Any], system: str, catalog: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], Optional[str]]:
    """``converted_text`` for a test-mapping row (``SyncEngine.test_mapping``:
    ``source_key`` / ``target_key`` / ``source_value`` / ``target_value``):
    the type is the catalogue's, so a side the catalogue does not know (a
    rendered template) never converts. ``(None, None)`` in metric."""
    if core_units.normalize(system) != core_units.IMPERIAL:
        return None, None
    lookup = catalog.get if catalog is not None else field_spec
    source_spec, target_spec = lookup(row.get("source_key") or ""), lookup(row.get("target_key") or "")
    source_kind, target_kind = conflict_kinds(source_spec, target_spec)
    return (converted_text(row.get("source_value"), source_spec.type if source_spec is not None else "text", source_kind, system),
            converted_text(row.get("target_value"), target_spec.type if target_spec is not None else "text", target_kind, system))
