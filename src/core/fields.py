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
ConflictPolicy = Literal["source_wins", "target_wins", "prefer_non_empty", "manual"]

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


def are_gas_mixtures_different(list1: List[GasMixture], list2: List[GasMixture]) -> bool:
    """Tank-list comparison inherited from the old loop (tank names ignored)."""
    if len(list1) != len(list2):
        return True
    for gm1, gm2 in zip(list1, list2):
        if gm1.oxygen != gm2.oxygen or gm1.helium != gm2.helium:
            return True
        if gm1.start_pressure != gm2.start_pressure or gm1.end_pressure != gm2.end_pressure:
            return True
        if gm1.tank_volume != gm2.tank_volume:
            return True
    return False


def values_equal(field_type: str, a: Any, b: Any) -> bool:
    if field_type == "text":
        return normalize_text(a) == normalize_text(b)
    if field_type == "tanks":
        return not are_gas_mixtures_different(list(a or []), list(b or []))
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
    sensor names, not meaningful on the other service; E5 revisits this)."""
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

def common_default_links(source_id: str, target_id: str) -> List[FieldLink]:
    """The shipped links every pair starts with, expressed on the unified
    fields both services have. Reproduces the pre-Track-C matched-pair loop:
    the source side wins every text/number conflict, GPS goes source->target
    and only fills a blank source from the target, tanks and samples go one
    way to the target, and the dive-number link is the tier-2 match key."""
    s, t = source_id, target_id
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
    """Default board for the Garmin -> Divelogs pair. Site names are shown as
    links but stay off on matched dives, which is what the old loop did (it
    never compared locations); uploads of new dives still use
    ``UnifiedDive.location`` until the template renderer (C18) takes over."""
    links = common_default_links("garmin", "divelogs")
    links.extend([
        FieldLink(
            id="site_to_divelogs",
            source=["garmin.activityName"],
            target="divelogs.divesite",
            direction="off",
        ),
        FieldLink(
            id="site_to_garmin",
            source=["divelogs.location", "divelogs.divesite"],
            target="garmin.activityName",
            direction="off",
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
