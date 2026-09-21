"""Field catalogue and field links (rework.md Track C, steps C1/C17/C19).

Every adapter declares the fields it can read and write as a list of
``FieldSpec`` (its *catalogue*). The user's mapping between two services is a
list of ``FieldLink``; each link joins one target field to one or more source
fields and carries a direction, a conflict policy and (for composites) a
template. ``SyncEngine`` drives its matched-pair loop and its "match key" tier
from these links, and both UIs edit the same list.

Catalogue keys are always ``<service_id>.<name>`` (``garmin.buddy``,
``divelogs.divesite``). The prefix is what tells a link which side of a pair
each end lives on, which matters because a link may read from either side
(the shipped composite for Garmin's activity name reads Divelogs fields).
For fields that map onto a ``UnifiedDive`` attribute ``name`` is that
attribute (``buddy``, ``gps``, ``tanks``); for service-specific scalars it is
the native API name (``activityName``, ``divesite``) and the value lives in
``UnifiedDive.service_fields``.

This module has no I/O and no knowledge of any particular service beyond the
shipped default link set for the Garmin/Divelogs pair.
"""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator

from src.core.models import GasMixture, UnifiedDive, UnifiedSample

FieldType = Literal["text", "number", "datetime", "gps", "list", "tanks", "samples"]
LinkDirection = Literal["bidirectional", "to_target", "to_source", "off"]
ConflictPolicy = Literal["source_wins", "target_wins", "prefer_non_empty", "prefer_source", "manual"]

# Field types a match key (FieldLink.match_order) may have.
MATCH_KEY_TYPES = ("number", "datetime")
# Structural types: one-way only, no templates, same type on both ends.
STRUCTURAL_TYPES = ("tanks", "samples")
# Types that may appear as a source inside a composite template.
TEMPLATE_SOURCE_TYPES = ("text", "number", "datetime", "list")


class FieldSpec(BaseModel):
    """One field an adapter can read and/or write."""
    key: str = Field(..., description="'<service_id>.<name>', e.g. 'garmin.activityName' or 'divelogs.buddy'")
    label: str = Field(..., description="Friendly name shown on the mapping board")
    type: FieldType
    readable: bool = True
    writable: bool = True
    unified: Optional[str] = Field(None, description="UnifiedDive attribute this field is, if any ('buddy', 'gps', 'tanks', ...)")
    max_length: Optional[int] = Field(None, description="Service-side length limit for text fields")
    unit: Optional[str] = Field(None, description="Unit shown next to number fields (values that carry their own unit, such as weight, leave this empty)")

    @field_validator("key")
    @classmethod
    def _key_has_service_prefix(cls, value: str) -> str:
        if "." not in value or not value.split(".", 1)[0] or not value.split(".", 1)[1]:
            raise ValueError(f"Field key {value!r} must be '<service_id>.<name>'")
        return value

    @property
    def service_id(self) -> str:
        return self.key.split(".", 1)[0]

    @property
    def name(self) -> str:
        return self.key.split(".", 1)[1]


class FieldLink(BaseModel):
    """One user-defined link between a target field and its source field(s)."""
    id: str = Field(..., description="Unique id of the link within its board")
    source: List[str] = Field(..., description="One catalogue key, or several for a composite")
    target: str = Field(..., description="Catalogue key of the field this link writes")
    direction: LinkDirection = "bidirectional"
    conflict: ConflictPolicy = "source_wins"
    # ("prefer_source": mirror whichever side is named "source" whenever it
    #  has a value, but never blank the other side from an empty source;
    #  falls back to the non-source side's value when source is empty.
    #  Added 2026-09-22 for the default `tanks` link: a manual Garmin dive
    #  with no gas API data must not erase real tank data already recorded
    #  on the target, but a Garmin dive that *does* have tanks should always
    #  replace stale target data rather than only filling a blank field.)
    template: Optional[str] = Field(None, description="'{key}' template; required when len(source) > 1")
    reverse: Optional[str] = Field(None, description="Parse pattern for a composite (regex with named groups); later item C21")
    match_order: Optional[int] = Field(None, description="Set: this link is also a match key, tried in this order")
    separator: str = Field(", ", description="list <-> text links: join / split token")
    when: Optional[str] = Field(None, description="Reserved for conditional links")

    @field_validator("id")
    @classmethod
    def _id_not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Link id must not be blank")
        return value

    @model_validator(mode="after")
    def _structure(self) -> "FieldLink":
        if not self.source:
            raise ValueError(f"Link '{self.id}': at least one source field is required")
        if len(set(self.source)) != len(self.source):
            raise ValueError(f"Link '{self.id}': duplicate source fields")
        if self.target in self.source:
            raise ValueError(f"Link '{self.id}': target {self.target!r} is also a source")
        if self.is_composite:
            if not self.template:
                raise ValueError(f"Link '{self.id}': a composite link needs a template")
            if self.direction not in ("to_target", "off"):
                raise ValueError(f"Link '{self.id}': a composite link is one-way towards its target (direction 'to_target' or 'off')")
            if self.match_order is not None:
                raise ValueError(f"Link '{self.id}': a composite link cannot be a match key")
        return self

    @property
    def is_composite(self) -> bool:
        return len(self.source) > 1


# ---------------------------------------------------------------------------
# Value access on UnifiedDive
# ---------------------------------------------------------------------------

# Unified "fields" that span more than one UnifiedDive attribute. Their value
# is a tuple so a link compares/copies the parts together, as the old
# matched-pair loop did (weight always travels with its unit, GPS as a pair).
_TUPLE_UNIFIED: Dict[str, Tuple[str, ...]] = {
    "gps": ("lat", "lng"),
    "weight": ("weight", "weight_unit"),
    "visibility": ("visibility", "visibility_unit"),
}
_UNIFIED_ALIASES: Dict[str, str] = {
    "tanks": "gas_mixtures",
}


def get_field(dive: UnifiedDive, spec: FieldSpec) -> Any:
    """Read the value of ``spec`` from ``dive`` (see module docstring for the shapes)."""
    if spec.unified:
        parts = _TUPLE_UNIFIED.get(spec.unified)
        if parts:
            return tuple(getattr(dive, p) for p in parts)
        return getattr(dive, _UNIFIED_ALIASES.get(spec.unified, spec.unified))
    return dive.service_fields.get(spec.name)


def set_field(dive: UnifiedDive, spec: FieldSpec, value: Any) -> None:
    """Write ``value`` (in the shape ``get_field`` returns) to ``spec`` on ``dive``."""
    if spec.unified:
        parts = _TUPLE_UNIFIED.get(spec.unified)
        if parts:
            values = tuple(value) if value is not None else (None,) * len(parts)
            for attr, val in zip(parts, values):
                setattr(dive, attr, val)
            return
        setattr(dive, _UNIFIED_ALIASES.get(spec.unified, spec.unified), value)
        return
    dive.service_fields[spec.name] = value


def normalize_text(value: Any) -> str:
    """Text comparison rule inherited from the old loop: None and the literal
    string 'None' count as empty, surrounding whitespace is ignored."""
    if value is None or value == "None":
        return ""
    return str(value).strip()


PRESSURE_TOLERANCE = 0.5   # bar; Divelogs stores pressures via a psi round trip (193 -> 192.91)
VOLUME_TOLERANCE = 0.05    # litres
DEPTH_TOLERANCE = 0.01     # metres; Divelogs samples are rounded to 2 decimals
TEMP_TOLERANCE = 0.1       # degrees


def _close(a: Any, b: Any, tolerance: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) <= tolerance
    except (TypeError, ValueError):
        return a == b


def are_gas_mixtures_different(list1: List[GasMixture], list2: List[GasMixture]) -> bool:
    """Tank-list comparison inherited from the old loop (tank names ignored),
    with tolerances for the services' storage rounding so an unchanged tank
    is not rewritten on every run."""
    if len(list1) != len(list2):
        return True
    for gm1, gm2 in zip(list1, list2):
        if gm1.oxygen != gm2.oxygen or gm1.helium != gm2.helium:
            return True
        if not _close(gm1.start_pressure, gm2.start_pressure, PRESSURE_TOLERANCE):
            return True
        if not _close(gm1.end_pressure, gm2.end_pressure, PRESSURE_TOLERANCE):
            return True
        if not _close(gm1.tank_volume, gm2.tank_volume, VOLUME_TOLERANCE):
            return True
    return False


def resample_profile(samples: List[UnifiedSample]) -> Tuple[int, List[UnifiedSample]]:
    """Return ``(samplerate, samples on a uniform grid)``.

    The rate is the most common spacing of the timed samples (at least 1 s).
    Depth and temperature are linearly interpolated at every grid point from
    the first to the last timed sample. Samples without times are passed
    through unchanged at rate 1, as before."""
    timed = [s for s in samples if s.time is not None]
    if len(timed) < 2:
        return 1, list(samples)
    timed.sort(key=lambda s: s.time)
    diffs = [b.time - a.time for a, b in zip(timed, timed[1:]) if b.time > a.time]
    if not diffs:
        return 1, list(samples)
    from collections import Counter
    samplerate = max(1, int(Counter(diffs).most_common(1)[0][0]))
    if all(d == samplerate for d in diffs):
        return samplerate, timed

    grid: List[UnifiedSample] = []
    idx = 0
    t = timed[0].time
    last = timed[-1].time
    while t <= last:
        while idx + 1 < len(timed) and timed[idx + 1].time <= t:
            idx += 1
        a = timed[idx]
        b = timed[idx + 1] if idx + 1 < len(timed) else a
        if b.time == a.time or t <= a.time:
            depth, temp = a.depth, a.temp
        else:
            f = (t - a.time) / (b.time - a.time)
            depth = a.depth + (b.depth - a.depth) * f
            temp = None
            if a.temp is not None and b.temp is not None:
                temp = a.temp + (b.temp - a.temp) * f
            elif a.temp is not None:
                temp = a.temp
        grid.append(UnifiedSample(depth=round(depth, 3), temp=None if temp is None else round(temp, 2), time=t))
        t += samplerate
    return samplerate, grid


def are_samples_different(list1: List[UnifiedSample], list2: List[UnifiedSample]) -> bool:
    """Profile comparison on a common footing: both lists are resampled onto
    their uniform grid first (Garmin records irregular intervals, Divelogs
    stores a fixed rate), then compared sample by sample with depths within
    1 cm and temperatures within 0.1 degree (Divelogs rounds both)."""
    if not list1 or not list2:
        return bool(list1) != bool(list2)
    _, list1 = resample_profile(list1)
    _, list2 = resample_profile(list2)
    if len(list1) != len(list2):
        return True
    for s1, s2 in zip(list1, list2):
        if s1.time != s2.time:
            return True
        if not _close(s1.depth, s2.depth, DEPTH_TOLERANCE):
            return True
        if not _close(s1.temp, s2.temp, TEMP_TOLERANCE):
            return True
    return False


GPS_TOLERANCE = 5e-6  # degrees (~0.5 m); Divelogs stores six decimals, Garmin full floats


def _coord_equal(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) <= GPS_TOLERANCE
    except (TypeError, ValueError):
        return a == b


def values_equal(field_type: str, a: Any, b: Any) -> bool:
    if field_type == "text":
        return normalize_text(a) == normalize_text(b)
    if field_type == "gps":
        if a is None or b is None:
            return a == b
        return _coord_equal(a[0], b[0]) and _coord_equal(a[1], b[1])
    if field_type == "tanks":
        return not are_gas_mixtures_different(list(a or []), list(b or []))
    if field_type == "samples":
        return not are_samples_different(list(a or []), list(b or []))
    if field_type == "list":
        return list(a or []) == list(b or [])
    return a == b


def is_empty(field_type: str, value: Any) -> bool:
    if value is None:
        return True
    if field_type == "text":
        return normalize_text(value) == ""
    if field_type == "gps":
        return value[0] is None or value[1] is None
    if field_type == "number":
        if isinstance(value, tuple):
            return value[0] is None
        return False
    if field_type in ("list", "tanks", "samples"):
        return len(value) == 0
    return False


def copy_value(field_type: str, value: Any) -> Any:
    """Value to write to the other side. Tanks lose their name on the way,
    exactly as the old Garmin->Divelogs gas copy did (Garmin tank names are
    sensor names, not meaningful on the other service). tank_role travels
    through unchanged (E5): it is real multi-tank information a target
    adapter may itself be unable to store (e.g. Divelogs has no slot for
    it), in which case the adapter's own write path drops it, but the
    engine must not discard it pre-emptively for pairs that do model it
    (Submersion <-> Subsurface)."""
    if value is None:
        return None
    if field_type == "tanks":
        return [
            GasMixture(
                oxygen=gm.oxygen,
                helium=gm.helium,
                start_pressure=gm.start_pressure,
                end_pressure=gm.end_pressure,
                tank_volume=gm.tank_volume,
                tank_role=gm.tank_role,
            )
            for gm in value
        ]
    if field_type == "samples":
        return [s.model_copy() for s in value]
    if field_type == "list":
        return list(value)
    return copy.copy(value)


def convert_value(value: Any, from_type: str, to_type: str, separator: str = ", ") -> Any:
    """Cross-type conversion for the one allowed mixed link, list <-> text."""
    if from_type == to_type or value is None:
        return value
    if from_type == "list" and to_type == "text":
        return separator.join(str(v) for v in value if v is not None and str(v) != "")
    if from_type == "text" and to_type == "list":
        token = separator.strip() or separator
        return [part.strip() for part in str(value).split(token) if part.strip()]
    raise ValueError(f"Cannot convert a {from_type} value to {to_type}")


def match_key_equal(field_type: str, a: Any, b: Any) -> bool:
    """Tier-2 matching rule for a match-key link. Numbers must parse to the same
    positive integer (the dive-number rule the old ladder used); datetimes must
    be identical."""
    if a is None or b is None:
        return False
    if field_type == "number":
        try:
            ia = int(float(str(a).strip()))
            ib = int(float(str(b).strip()))
        except (TypeError, ValueError):
            return False
        return ia == ib and ia > 0
    if field_type == "datetime":
        return isinstance(a, datetime) and isinstance(b, datetime) and a == b
    return False


# ---------------------------------------------------------------------------
# Catalogue helpers and link validation
# ---------------------------------------------------------------------------

def build_catalog(*catalogs: Iterable[FieldSpec]) -> Dict[str, FieldSpec]:
    """Merge adapter catalogues into one key -> spec dict."""
    merged: Dict[str, FieldSpec] = {}
    for catalog in catalogs:
        for spec in catalog:
            merged[spec.key] = spec
    return merged


def types_compatible(source_type: str, target_type: str) -> bool:
    if source_type == target_type:
        return True
    return {source_type, target_type} == {"list", "text"}


def validate_field_links(links: List[FieldLink], catalog: Dict[str, FieldSpec]) -> List[str]:
    """Return a list of human-readable problems; empty means the board is valid.

    Checks: unique ids, known fields, type compatibility (same type, or
    list <-> text), structural fields one-way and un-templated, composite
    targets are text, match keys are single number/datetime fields, and no
    link writes a field its service cannot write. Template *contents* are
    checked by the renderer (C18)."""
    errors: List[str] = []
    seen_ids = set()
    for link in links:
        if link.id in seen_ids:
            errors.append(f"Link '{link.id}': duplicate link id")
        seen_ids.add(link.id)

        unknown = [k for k in link.source + [link.target] if k not in catalog]
        if unknown:
            errors.append(f"Link '{link.id}': unknown field(s) {', '.join(unknown)}")
            continue

        target = catalog[link.target]
        sources = [catalog[k] for k in link.source]

        if link.is_composite:
            if target.type != "text":
                errors.append(f"Link '{link.id}': a composite link must target a text field, not {target.type}")
            bad = [s.key for s in sources if s.type not in TEMPLATE_SOURCE_TYPES]
            if bad:
                errors.append(f"Link '{link.id}': field(s) {', '.join(bad)} cannot be used inside a template")
        else:
            src = sources[0]
            if not types_compatible(src.type, target.type):
                errors.append(f"Link '{link.id}': cannot link {src.key} ({src.type}) to {target.key} ({target.type})")
            if src.type in STRUCTURAL_TYPES or target.type in STRUCTURAL_TYPES:
                if link.direction == "bidirectional":
                    errors.append(f"Link '{link.id}': {src.type} links are one-way (to_target or to_source)")
                if link.template:
                    errors.append(f"Link '{link.id}': {src.type} links cannot have a template")
            if link.match_order is not None:
                if src.type not in MATCH_KEY_TYPES or target.type not in MATCH_KEY_TYPES:
                    errors.append(f"Link '{link.id}': only number or datetime fields can be match keys")

        writes_target = link.direction in ("bidirectional", "to_target")
        writes_source = link.direction in ("bidirectional", "to_source")
        if writes_target and not target.writable:
            errors.append(f"Link '{link.id}': {target.key} cannot be written by its service")
        if writes_source and not sources[0].writable:
            errors.append(f"Link '{link.id}': {sources[0].key} cannot be written by its service")
        unreadable = [s.key for s in sources if not s.readable]
        if unreadable:
            errors.append(f"Link '{link.id}': field(s) {', '.join(unreadable)} cannot be read")
    return errors


# ---------------------------------------------------------------------------
# Default link sets
# ---------------------------------------------------------------------------

MATCH_KEY_MAX_HOURS = 24  # a match-key hit only counts when the dives start within a day of each other


def common_default_links(source_id: str, target_id: str, match_on_dive_number: bool = True) -> List[FieldLink]:
    """The shipped links every pair starts with, on the unified fields both
    services have. Scalar fields use ``prefer_non_empty`` (decided
    2026-09-21 after the live baseline): a blank side is filled from the
    other, a real conflict is left alone and logged, nothing is ever wiped.
    ``tanks`` is ``prefer_source`` (decided 2026-09-22): it is a one-way
    structural link already (``to_target``), so this mirrors whichever side
    is the dive computer / source of truth for gas data (Garmin in every
    pair configured so far) whenever that side has tank data, which fixes
    the case where a target already had *some* (stale) tanks and
    ``prefer_non_empty`` refused to update them. A source with no tank data
    at all (a manually created Garmin dive) still never blanks a target
    that has real tank data. ``samples`` stays fill-only for now. The dive-number
    link is a match key only where both services let the user set the
    number."""
    s, t = source_id, target_id
    fill = "prefer_non_empty"
    links = [
        FieldLink(id="buddy", source=[f"{s}.buddy"], target=f"{t}.buddy", conflict=fill),
        FieldLink(id="notes", source=[f"{s}.notes"], target=f"{t}.notes", conflict=fill),
        FieldLink(id="weight", source=[f"{s}.weight"], target=f"{t}.weight", conflict=fill),
        FieldLink(id="visibility", source=[f"{s}.visibility"], target=f"{t}.visibility", conflict=fill),
        FieldLink(id="gps", source=[f"{s}.gps"], target=f"{t}.gps", conflict=fill),
        FieldLink(id="samples", source=[f"{s}.samples"], target=f"{t}.samples", direction="to_target", conflict=fill),
        FieldLink(id="tanks", source=[f"{s}.tanks"], target=f"{t}.tanks", direction="to_target", conflict="prefer_source"),
    ]
    if match_on_dive_number:
        links.append(FieldLink(id="dive_number", source=[f"{s}.dive_number"], target=f"{t}.dive_number",
                               direction="off", match_order=1))
    return links


def legacy_field_links() -> List[FieldLink]:
    """The board that reproduces the pre-Track-C loop exactly (Garmin wins
    every difference, empty values included; GPS overwrite one way and
    fill-only the other; dive number as tier-2 match key). Kept for the
    equivalence test and for anyone who wants the old behaviour back."""
    s, t = "garmin", "divelogs"
    return [
        FieldLink(id="buddy", source=[f"{s}.buddy"], target=f"{t}.buddy"),
        FieldLink(id="notes", source=[f"{s}.notes"], target=f"{t}.notes"),
        FieldLink(id="weight", source=[f"{s}.weight"], target=f"{t}.weight"),
        FieldLink(id="visibility", source=[f"{s}.visibility"], target=f"{t}.visibility"),
        FieldLink(id="gps", source=[f"{s}.gps"], target=f"{t}.gps", direction="to_target"),
        FieldLink(id="gps_fill", source=[f"{t}.gps"], target=f"{s}.gps", direction="to_target", conflict="prefer_non_empty"),
        FieldLink(id="samples", source=[f"{s}.samples"], target=f"{t}.samples", direction="to_target"),
        FieldLink(id="tanks", source=[f"{s}.tanks"], target=f"{t}.tanks", direction="to_target"),
        FieldLink(id="dive_number", source=[f"{s}.dive_number"], target=f"{t}.dive_number", direction="off", match_order=1),
    ]


def default_field_links() -> List[FieldLink]:
    """Default board for the Garmin -> Divelogs pair (decided 2026-09-21).

    Divelogs numbers dives itself from date and time, so dive numbers are
    neither a match key nor synced on this pair. Site names: Garmin's
    ``locationName`` and Divelogs' ``divesite`` are the same thing and sync
    both ways; Garmin's ``activityName`` (the title) is built from the
    Divelogs region and site for new dives (``prefer_non_empty`` means an
    existing title is never replaced). Divelogs' ``location`` (region) has no
    Garmin counterpart and is left alone."""
    links = common_default_links("garmin", "divelogs", match_on_dive_number=False)
    links.extend([
        FieldLink(
            id="site",
            source=["garmin.locationName"],
            target="divelogs.divesite",
            conflict="prefer_non_empty",
        ),
        FieldLink(
            id="activity_name",
            source=["divelogs.location", "divelogs.divesite"],
            target="garmin.activityName",
            direction="to_target",
            conflict="prefer_non_empty",
            template="{divelogs.location}, {divelogs.divesite}",
        ),
    ])
    return links


# ---------------------------------------------------------------------------
# JSON round-trip of field values (conflicts.json, API responses)
# ---------------------------------------------------------------------------

def serialize_value(field_type: str, value: Any) -> Any:
    """JSON-safe form of a value in the shape ``get_field`` returns."""
    if value is None:
        return None
    if field_type in ("tanks", "samples"):
        return [item.model_dump(mode="json") for item in value]
    if field_type == "datetime":
        return value.isoformat() if isinstance(value, datetime) else value
    if isinstance(value, tuple):
        return list(value)
    return value


def deserialize_value(field_type: str, raw: Any) -> Any:
    """Inverse of ``serialize_value``."""
    if raw is None:
        return None
    if field_type == "tanks":
        return [GasMixture.model_validate(item) for item in raw]
    if field_type == "samples":
        return [UnifiedSample.model_validate(item) for item in raw]
    if field_type == "datetime":
        return datetime.fromisoformat(raw) if isinstance(raw, str) else raw
    if field_type in ("gps", "number") and isinstance(raw, list):
        return tuple(raw)
    return raw
