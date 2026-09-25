"""Template rendering for composite field links (rework.md Track C, step C18).

A link with several sources renders its target from ``template``, a string
with ``{key}`` placeholders where ``key`` is a catalogue key
(``{divelogs.divesite}``) or, as a shorthand, the bare name of a field on the
link's own source service (``{dive_number}`` inside a link whose sources are
Divelogs fields). Standard Python format specs apply: ``{divelogs.dive_number:03d}``,
``{date_time:%Y-%m-%d}``. An empty source renders as an empty string. Datetimes
render in dive local time. A rendered text longer than the target's
``max_length`` is truncated and a warning is returned alongside the text.

A templated link is normally one-way (``to_target``/``off``): the composite
is a display convenience, not a fact to sync back. Setting ``reverse`` (a
regex with named groups matching source field names, C21) lifts that
restriction — ``reverse_parse`` splits an edited target back into its
sources when the pattern fully matches it, and the direction may then also
be ``bidirectional``/``to_source``. Only text-typed sources are supported,
since recovering a number or datetime from free text needs a format the
regex alone can't express reliably. ``reverse`` may also be ``"auto"``
(``AUTO_REVERSE``): the pattern is then derived from the template itself
(``auto_reverse_pattern``), so ``{location}, {divesite}`` splits on its
first ", " without the user writing a regex.

``validate_links`` is the full save-time check for a board: the structural
checks from ``fields.validate_field_links`` plus template validation (unknown
keys, keys outside the link's sources, unused sources, format spec vs field
type, non-text target), reverse-pattern validation (unknown or out-of-source
group names), and loop detection (a composite's target feeding back into one
of its own sources through other links).
"""
from __future__ import annotations

import re
from datetime import datetime
from string import Formatter
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from src.core.fields import (
    FieldLink,
    FieldSpec,
    get_field,
    is_empty,
    validate_field_links,
)
from src.core.models import GasMixture, UnifiedDive, UnifiedSample

_FORMATTER = Formatter()


class TemplateError(ValueError):
    pass


AUTO_REVERSE = "auto"


def _literal_pattern(literal: str) -> str:
    """A template literal as regex, tolerant of the spacing around it: an
    edited "Gozo ,Blue Hole" still splits on the ", " of the template."""
    parts = [re.escape(p) for p in literal.split()]
    if not parts:
        return r"\s+" if literal else ""
    return r"\s*" + r"\s*".join(parts) + r"\s*"


def auto_reverse_pattern(template: str) -> str:
    """The reverse pattern ``"auto"`` stands for: the template with every
    placeholder turned into a lazy named group and every literal matched
    as-is (spacing around it optional). Raises ``TemplateError`` when two
    placeholders are not separated by text - there is nothing to split on."""
    out, previous_was_field = [], False
    for literal, key, _spec, _conv in parse_template(template):
        if literal:
            out.append(_literal_pattern(literal))
        if key is None:
            continue
        if previous_was_field and not literal:
            raise TemplateError("an automatic split needs text between the fields, e.g. \"{a}, {b}\"")
        name = key.split(".", 1)[-1]
        if not re.fullmatch(r"[A-Za-z_]\w*", name):
            raise TemplateError(f"field {key} cannot be split automatically")
        out.append(f"(?P<{name}>.+?)")
        previous_was_field = True
    return "".join(out)


def reverse_pattern(link) -> Optional[str]:
    """The regex ``link.reverse`` means: itself, or the template-derived
    pattern for ``"auto"``. ``None`` without a reverse pattern."""
    if not link.reverse:
        return None
    if link.reverse.strip().lower() == AUTO_REVERSE:
        return auto_reverse_pattern(link.template or "")
    return link.reverse


def parse_template(template: str) -> List[Tuple[str, Optional[str], Optional[str], Optional[str]]]:
    """``(literal, key, format_spec, conversion)`` tuples, like ``Formatter.parse``.
    Raises ``TemplateError`` on unbalanced braces."""
    try:
        return list(_FORMATTER.parse(template))
    except ValueError as e:
        raise TemplateError(f"malformed template: {e}") from e


def resolve_key(key: str, link: FieldLink, catalog: Dict[str, FieldSpec]) -> Optional[str]:
    """Turn a placeholder key into a catalogue key: as written if it is one,
    otherwise ``<source service>.<key>`` when that exists."""
    if key in catalog:
        return key
    if "." not in key and link.source:
        service = link.source[0].split(".", 1)[0]
        candidate = f"{service}.{key}"
        if candidate in catalog:
            return candidate
    return None


def template_keys(link: FieldLink, catalog: Dict[str, FieldSpec]) -> List[Tuple[str, Optional[str], str]]:
    """``(key as written, resolved catalogue key or None, format_spec)`` per placeholder."""
    out = []
    for _literal, key, spec, _conv in parse_template(link.template or ""):
        if key is None:
            continue
        out.append((key, resolve_key(key, link, catalog), spec or ""))
    return out


def _spec_kind(spec: str) -> str:
    """'datetime' for strftime-style specs, 'number' for numeric presentation
    types, 'text' otherwise."""
    if "%" in spec:
        return "datetime"
    if spec and spec[-1] in "bcdoxXneEfFgG":
        return "number"
    return "text"


def _format_value(value: Any, field_type: str, spec: str, separator: str) -> str:
    if value is None or is_empty(field_type, value):
        return ""
    if field_type == "list":
        value = separator.join(str(v) for v in value if v is not None)
        return format(value, spec) if spec else value
    if isinstance(value, tuple):  # weight / visibility carry their unit
        value = value[0]
    if field_type == "number" and spec and spec[-1] in "bcdoxXn":
        try:
            value = int(float(value))
        except (TypeError, ValueError):
            return str(value)
    if field_type == "datetime" and not isinstance(value, datetime):
        return str(value)
    try:
        return format(value, spec) if spec else str(value)
    except (TypeError, ValueError):
        return str(value)


def tidy_joined_text(text: str, separator: str = ", ") -> str:
    """Composite output with empty placeholders leaves dangling separators
    ("Västra Hamnen, " or ", Zenobia"); drop the empty segments. Only the
    link's own separator is treated this way."""
    token = separator.strip() or separator
    if not token or token not in text:
        return text.strip()
    segments = [seg.strip() for seg in text.split(token)]
    return separator.join(seg for seg in segments if seg)


def render(link: FieldLink, dive_by_service: Dict[str, UnifiedDive],
           catalog: Dict[str, FieldSpec]) -> Tuple[str, List[str]]:
    """Render ``link.template`` (or, for a single untemplated source, the plain
    value as text). Returns ``(text, warnings)``; warnings currently only
    report truncation to the target's ``max_length``."""
    warnings: List[str] = []
    if not link.template:
        spec = catalog[link.source[0]]
        dive = dive_by_service.get(spec.service_id)
        text = _format_value(get_field(dive, spec), spec.type, "", link.separator) if dive else ""
    else:
        parts: List[str] = []
        for literal, key, fmt, _conv in parse_template(link.template):
            parts.append(literal)
            if key is None:
                continue
            resolved = resolve_key(key, link, catalog)
            if resolved is None:
                raise TemplateError(f"Link '{link.id}': unknown field {{{key}}} in template")
            spec = catalog[resolved]
            dive = dive_by_service.get(spec.service_id)
            parts.append(_format_value(get_field(dive, spec), spec.type, fmt or "", link.separator) if dive else "")
        text = "".join(parts)
        if link.is_composite:
            text = tidy_joined_text(text, link.separator)

    target = catalog.get(link.target)
    if target and target.max_length and len(text) > target.max_length:
        warnings.append(
            f"Link '{link.id}': rendered text ({len(text)} chars) truncated to {target.max_length} for {target.key}"
        )
        text = text[: target.max_length]
    return text, warnings


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_template(link: FieldLink, catalog: Dict[str, FieldSpec]) -> List[str]:
    """Problems with one link's template; empty when it has none or it is fine."""
    if not link.template:
        return []
    problems: List[str] = []
    prefix = f"Link '{link.id}':"
    target = catalog.get(link.target)
    if target is not None and target.type != "text":
        problems.append(f"{prefix} a templated link must target a text field, not {target.type}")
    if link.direction not in ("to_target", "off") and not link.reverse:
        problems.append(f"{prefix} a templated link can only write its target (direction 'to_target' or 'off') "
                         f"unless it has a 'reverse' pattern")
    try:
        keys = template_keys(link, catalog)
    except TemplateError as e:
        return problems + [f"{prefix} {e}"]
    if not keys:
        problems.append(f"{prefix} template has no {{field}} placeholder")
    used: Set[str] = set()
    for written, resolved, spec in keys:
        if resolved is None:
            problems.append(f"{prefix} unknown field {{{written}}} in template")
            continue
        if resolved not in link.source:
            problems.append(f"{prefix} template uses {{{written}}} which is not one of the link's source fields")
            continue
        used.add(resolved)
        kind = _spec_kind(spec)
        ftype = catalog[resolved].type
        if kind == "datetime" and ftype != "datetime":
            problems.append(f"{prefix} format '{spec}' on {{{written}}} needs a datetime field, {resolved} is {ftype}")
        elif kind == "number" and ftype != "number":
            problems.append(f"{prefix} format '{spec}' on {{{written}}} needs a number field, {resolved} is {ftype}")
    unused = [s for s in link.source if s not in used]
    if unused:
        problems.append(f"{prefix} source field(s) {', '.join(unused)} are not used in the template")
    return problems


def reverse_keys(link: FieldLink, catalog: Dict[str, FieldSpec]) -> List[Tuple[str, Optional[str]]]:
    """``(group name, resolved catalogue key or None)`` for every named group
    in ``link.reverse``. Empty when the link has no reverse pattern."""
    if not link.reverse:
        return []
    try:
        pattern = re.compile(reverse_pattern(link))
    except re.error as e:
        raise TemplateError(f"Link '{link.id}': malformed reverse pattern: {e}") from e
    return [(name, resolve_key(name, link, catalog)) for name in pattern.groupindex]


def reverse_parse(link: FieldLink, text: Any, catalog: Dict[str, FieldSpec]) -> Optional[Dict[str, str]]:
    """Split ``text`` (the target field's current value) back into the link's
    sources using ``link.reverse``. Only text-typed sources are supported, so
    every recovered value is the raw captured string. Returns ``None`` when
    there is no reverse pattern, the value is not text, or the pattern does
    not match the whole value (a target edited into some unrelated shape is
    left alone rather than guessed at)."""
    if not link.reverse or not isinstance(text, str):
        return None
    try:
        pattern = re.compile(reverse_pattern(link))
    except (re.error, TemplateError):
        return None
    match = pattern.fullmatch(text.strip())
    if not match:
        return None
    out: Dict[str, str] = {}
    for name, value in match.groupdict().items():
        if value is None:
            continue
        resolved = resolve_key(name, link, catalog)
        if resolved is None or resolved not in link.source:
            continue
        out[resolved] = value.strip()
    return out or None


def validate_reverse(link: FieldLink, catalog: Dict[str, FieldSpec]) -> List[str]:
    """Problems with one link's reverse pattern; empty when it has none or it
    is fine."""
    if not link.reverse:
        return []
    prefix = f"Link '{link.id}':"
    try:
        keys = reverse_keys(link, catalog)
    except TemplateError as e:
        return [f"{prefix} {e}"]
    if not keys:
        return [f"{prefix} reverse pattern has no named group, e.g. (?P<{link.source[0].split('.', 1)[-1]}>...)"]
    problems: List[str] = []
    for written, resolved in keys:
        if resolved is None:
            problems.append(f"{prefix} unknown field ({written}) in reverse pattern")
        elif resolved not in link.source:
            problems.append(f"{prefix} reverse pattern group ({written}) is not one of the link's source fields")
    return problems


def detect_loops(links: Iterable[FieldLink]) -> List[str]:
    """A composite's target must not feed, through other links, back into one
    of its own sources. Returns one message per loop naming both links."""
    links = [l for l in links if l.direction != "off"]
    # edge: from field -> (to field, link id)
    edges: Dict[str, List[Tuple[str, str]]] = {}
    for link in links:
        if link.direction in ("bidirectional", "to_target"):
            for s in link.source:
                edges.setdefault(s, []).append((link.target, link.id))
        if link.direction in ("bidirectional", "to_source"):
            edges.setdefault(link.target, []).append((link.source[0], link.id))

    problems: List[str] = []
    for comp in links:
        if not comp.is_composite:
            continue
        sources = set(comp.source)
        seen: Set[str] = set()
        stack: List[Tuple[str, Optional[str]]] = [(comp.target, None)]
        found: Optional[str] = None
        while stack and found is None:
            node, via = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            for nxt, link_id in edges.get(node, []):
                if link_id == comp.id:
                    continue
                if nxt in sources:
                    found = link_id
                    break
                stack.append((nxt, link_id))
        if found:
            problems.append(
                f"Link '{comp.id}' writes {comp.target}, which link '{found}' feeds back into one of its sources"
            )
    return problems


def validate_links(links: List[FieldLink], catalog: Dict[str, FieldSpec]) -> List[str]:
    """Everything Save must check: structure, templates, loops."""
    problems = validate_field_links(links, catalog)
    bad = {p.split("'", 2)[1] for p in problems if p.startswith("Link '")}
    for link in links:
        if link.id in bad:
            continue
        problems.extend(validate_template(link, catalog))
        problems.extend(validate_reverse(link, catalog))
    problems.extend(detect_loops([l for l in links if l.id not in bad]))
    return problems


# ---------------------------------------------------------------------------
# Preview support
# ---------------------------------------------------------------------------

def example_dive(service_id: str) -> UnifiedDive:
    """A plausible dive for template previews when no real dive is at hand.
    Service-specific fields are filled for every catalogue the engine knows;
    unknown keys are simply absent."""
    service_fields = {
        "garmin": {"activityName": "Wreck of the Zenobia", "locationName": "Larnaca"},
        "divelogs": {"location": "Larnaca", "divesite": "Zenobia"},
    }.get(service_id, {})
    return UnifiedDive(
        date_time=datetime(2026, 6, 22, 9, 30, 0),
        duration=52 * 60,
        max_depth=31.4,
        avg_depth=18.2,
        temp_min=21.0,
        external_ids={service_id: "123456"},
        gas_mixtures=[GasMixture(oxygen=32.0, start_pressure=210.0, end_pressure=60.0, tank_volume=12.0)],
        location="Larnaca, Zenobia",
        notes="Great visibility on the wreck.",
        dive_number=148,
        weight=6.0,
        weight_unit="kilogram",
        visibility=25.0,
        visibility_unit="meter",
        buddy="Anna",
        lat=34.887,
        lng=33.657,
        samples=[UnifiedSample(depth=0.0, temp=24.0, time=0), UnifiedSample(depth=31.4, temp=21.0, time=900)],
        service_fields=service_fields,
    )


def preview(link: FieldLink, catalog: Dict[str, FieldSpec],
            dive_by_service: Optional[Dict[str, UnifiedDive]] = None) -> Dict[str, Any]:
    """Validation problems plus a rendered sample for one link, for the UIs'
    live preview. Uses ``dive_by_service`` when given, else example dives."""
    problems = validate_template(link, catalog) + validate_reverse(link, catalog)
    unknown = [k for k in link.source + [link.target] if k not in catalog]
    if unknown:
        problems.insert(0, f"Link '{link.id}': unknown field(s) {', '.join(unknown)}")
    if problems:
        return {"ok": False, "problems": problems, "text": None, "warnings": []}
    dives = dict(dive_by_service or {})
    for key in link.source:
        service = key.split(".", 1)[0]
        dives.setdefault(service, example_dive(service))
    text, warnings = render(link, dives, catalog)
    out = {"ok": True, "problems": [], "text": text, "warnings": warnings}
    if link.reverse:
        # Self-check: parsing the link's own rendered text should recover
        # every source the reverse pattern names, proving it actually
        # inverts the template rather than just looking plausible.
        out["reverse_sample"] = reverse_parse(link, text, catalog)
    return out
