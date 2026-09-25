from datetime import datetime

import pytest
from pydantic import ValidationError

from src.core.fields import FieldLink, FieldSpec, build_catalog, default_field_links
from src.core.models import UnifiedDive
from src.core.services.divelogs import DivelogsAdapter
from src.core.services.garmin import GarminAdapter
from src.core.templates import (
    TemplateError,
    detect_loops,
    example_dive,
    preview,
    render,
    reverse_keys,
    reverse_parse,
    validate_links,
    validate_reverse,
    validate_template,
)


def _catalog(extra=()):
    cat = build_catalog(GarminAdapter.field_catalog(), DivelogsAdapter.field_catalog())
    for spec in extra:
        cat[spec.key] = spec
    return cat


def _divelogs_dive(**kw):
    base = dict(date_time=datetime(2026, 6, 22, 9, 30), duration=100, max_depth=10.0, dive_number=7,
                service_fields={"location": "Larnaca", "divesite": "Zenobia"})
    base.update(kw)
    return UnifiedDive(**base)


SITE = FieldLink(id="site", source=["divelogs.location", "divelogs.divesite"], target="garmin.activityName",
                 direction="to_target", template="{divelogs.divesite} ({divelogs.location})")


def test_render_composite_and_format_specs():
    cat = _catalog()
    dives = {"divelogs": _divelogs_dive()}
    text, warnings = render(SITE, dives, cat)
    assert text == "Zenobia (Larnaca)" and warnings == []

    link = FieldLink(id="n", source=["divelogs.dive_number", "divelogs.date_time", "divelogs.divesite"],
                     target="garmin.activityName", direction="to_target",
                     template="#{dive_number:03d} {date_time:%Y-%m-%d} {divesite}")
    text, _ = render(link, dives, cat)
    assert text == "#007 2026-06-22 Zenobia"  # bare keys resolve on the source service


def test_render_empty_source_is_empty_string():
    cat = _catalog()
    dives = {"divelogs": _divelogs_dive(service_fields={"location": None, "divesite": "Zenobia"})}
    assert render(SITE, dives, cat)[0] == "Zenobia ()"
    # dangling ", " separators around empty placeholders are dropped
    joined = SITE.model_copy(update={"template": "{divelogs.location}, {divelogs.divesite}"})
    assert render(joined, dives, cat)[0] == "Zenobia"
    dives = {"divelogs": _divelogs_dive(service_fields={"location": "Larnaca", "divesite": ""})}
    assert render(joined, dives, cat)[0] == "Larnaca"
    dives = {"divelogs": _divelogs_dive(service_fields={"location": "Larnaca", "divesite": "Zenobia"})}
    assert render(joined, dives, cat)[0] == "Larnaca, Zenobia"
    dives = {"divelogs": _divelogs_dive(dive_number=None, service_fields={})}
    link = FieldLink(id="n", source=["divelogs.dive_number", "divelogs.divesite"], target="garmin.activityName",
                     direction="to_target", template="#{dive_number:03d}/{divesite}")
    assert render(link, dives, cat)[0] == "#/"


def test_render_single_source_template_and_plain_copy():
    cat = _catalog()
    dives = {"garmin": UnifiedDive(date_time=datetime(2026, 1, 1), duration=1, max_depth=1.0, notes="hello",
                                   weight=5.0, weight_unit="kilogram")}
    link = FieldLink(id="t", source=["garmin.notes"], target="divelogs.notes", template="Dive: {notes}")
    assert render(link, dives, cat)[0] == "Dive: hello"
    link = FieldLink(id="w", source=["garmin.weight"], target="divelogs.notes", template="{weight:.1f} kg")
    assert render(link, dives, cat)[0] == "5.0 kg"
    link = FieldLink(id="p", source=["garmin.notes"], target="divelogs.notes")
    assert render(link, dives, cat)[0] == "hello"


def test_render_truncates_to_max_length_with_warning():
    cat = _catalog([FieldSpec(key="garmin.activityName", label="Activity name", type="text", max_length=8)])
    text, warnings = render(SITE, {"divelogs": _divelogs_dive()}, cat)
    assert text == "Zenobia " and len(warnings) == 1 and "truncated to 8" in warnings[0]


def test_render_unknown_key_raises():
    with pytest.raises(TemplateError, match="unknown field"):
        render(FieldLink(id="x", source=["divelogs.location", "divelogs.divesite"], target="garmin.activityName",
                         direction="to_target", template="{nope} {divelogs.divesite}"),
               {"divelogs": _divelogs_dive()}, _catalog())


def test_validate_template_problems():
    cat = _catalog()
    def problems(**kw):
        base = dict(id="x", source=["divelogs.location", "divelogs.divesite"], target="garmin.activityName",
                    direction="to_target")
        base.update(kw)
        return "\n".join(validate_template(FieldLink(**base), cat))
    assert problems(template="{divelogs.divesite} ({divelogs.location})") == ""
    assert "unknown field {nope}" in problems(template="{nope} {divelogs.location} {divelogs.divesite}")
    assert "not one of the link's source fields" in problems(template="{divelogs.notes} {divelogs.location} {divelogs.divesite}")
    assert "are not used in the template" in problems(template="{divelogs.divesite}")
    assert "needs a datetime field" in problems(template="{divelogs.divesite:%Y} {divelogs.location}")
    assert "needs a number field" in problems(template="{divelogs.divesite:03d} {divelogs.location}")
    assert "malformed" in problems(template="{divelogs.divesite {divelogs.location}")
    assert "no {field} placeholder" in problems(source=["divelogs.divesite"], template="fixed text")
    assert "must target a text field" in problems(source=["divelogs.dive_number"], target="garmin.dive_number",
                                                  template="{dive_number}")
    assert "can only write its target" in problems(source=["divelogs.notes"], target="garmin.notes",
                                                   direction="bidirectional", template="Dive: {notes}")


def test_detect_loops_names_both_links():
    comp = FieldLink(id="site", source=["divelogs.location", "divelogs.divesite"], target="garmin.activityName",
                     direction="to_target", template="{divelogs.location} {divelogs.divesite}")
    back = FieldLink(id="back", source=["garmin.activityName"], target="divelogs.divesite", direction="to_target")
    msgs = detect_loops([comp, back])
    assert len(msgs) == 1 and "'site'" in msgs[0] and "'back'" in msgs[0]
    # via two hops and a bidirectional link
    hop = FieldLink(id="hop", source=["garmin.activityName"], target="divelogs.notes", direction="to_target")
    hop2 = FieldLink(id="hop2", source=["divelogs.location"], target="divelogs.notes", direction="bidirectional")
    msgs = detect_loops([comp, hop, hop2])
    assert len(msgs) == 1 and "'hop2'" in msgs[0]
    # off links do not count
    assert detect_loops([comp, back.model_copy(update={"direction": "off"})]) == []
    assert detect_loops([comp, hop]) == []


def test_validate_links_is_the_full_check():
    cat = _catalog()
    assert validate_links(default_field_links(), cat) == []
    comp = FieldLink(id="site", source=["divelogs.location", "divelogs.divesite"], target="garmin.activityName",
                     direction="to_target", template="{divelogs.location} {nope}")
    back = FieldLink(id="back", source=["garmin.activityName"], target="divelogs.divesite", direction="to_target")
    bad = FieldLink(id="bad", source=["garmin.buddy"], target="divelogs.max_depth")
    text = "\n".join(validate_links([comp, back, bad], cat))
    assert "unknown field {nope}" in text and "feeds back" in text and "cannot link garmin.buddy" in text


REVERSIBLE_SITE = SITE.model_copy(update={
    "direction": "bidirectional",
    "reverse": r"(?P<divesite>.+) \((?P<location>.+)\)",
})


def test_composite_direction_needs_a_reverse_pattern():
    with pytest.raises(ValidationError, match="one-way"):
        FieldLink(**{**SITE.model_dump(), "direction": "bidirectional"})
    # a reverse pattern lifts the restriction
    FieldLink(**{**SITE.model_dump(), "direction": "bidirectional",
                "reverse": r"(?P<divesite>.+) \((?P<location>.+)\)"})
    FieldLink(**{**SITE.model_dump(), "direction": "to_source", "reverse": r"(?P<divesite>.+)"})


def test_reverse_keys_resolve_group_names():
    cat = _catalog()
    assert reverse_keys(REVERSIBLE_SITE, cat) == [
        ("divesite", "divelogs.divesite"), ("location", "divelogs.location"),
    ]
    unknown = REVERSIBLE_SITE.model_copy(update={"reverse": r"(?P<nope>.+)"})
    assert reverse_keys(unknown, cat) == [("nope", None)]
    assert reverse_keys(SITE, cat) == []  # no reverse pattern


def test_reverse_parse_splits_composite_back_into_sources():
    cat = _catalog()
    assert reverse_parse(REVERSIBLE_SITE, "Zenobia (Larnaca)", cat) == {
        "divelogs.divesite": "Zenobia", "divelogs.location": "Larnaca",
    }
    # a target that no longer matches the pattern shape can't be guessed at
    assert reverse_parse(REVERSIBLE_SITE, "Just some renamed dive", cat) is None
    assert reverse_parse(REVERSIBLE_SITE, "", cat) is None
    assert reverse_parse(REVERSIBLE_SITE, None, cat) is None
    assert reverse_parse(SITE, "Zenobia (Larnaca)", cat) is None  # no reverse pattern
    # a partial pattern only recovers the source(s) it names
    one_field = REVERSIBLE_SITE.model_copy(update={"reverse": r"(?P<divesite>.+) \(.+\)"})
    assert reverse_parse(one_field, "Zenobia (Larnaca)", cat) == {"divelogs.divesite": "Zenobia"}
    # a group that resolves outside the link's own sources is ignored
    outside = REVERSIBLE_SITE.model_copy(update={"reverse": r"(?P<notes>.+) \((?P<location>.+)\)"})
    assert reverse_parse(outside, "Zenobia (Larnaca)", cat) == {"divelogs.location": "Larnaca"}


def test_validate_reverse_problems():
    cat = _catalog()
    def problems(**kw):
        base = dict(id="x", source=["divelogs.location", "divelogs.divesite"], target="garmin.activityName",
                    direction="to_target", template="{divelogs.divesite} ({divelogs.location})")
        base.update(kw)
        return "\n".join(validate_reverse(FieldLink(**base), cat))
    assert problems(reverse=r"(?P<divesite>.+) \((?P<location>.+)\)") == ""
    assert problems(reverse=None) == ""
    assert "malformed reverse pattern" in problems(reverse=r"(?P<divesite>.+ \(")
    assert "has no named group" in problems(reverse=r".+ \(.+\)")
    assert "unknown field (nope)" in problems(reverse=r"(?P<nope>.+)")
    assert "not one of the link's source fields" in problems(reverse=r"(?P<notes>.+)")


def test_validate_field_links_requires_text_sources_for_reverse():
    cat = _catalog()
    link = FieldLink(id="x", source=["divelogs.dive_number", "divelogs.divesite"], target="garmin.activityName",
                     direction="bidirectional", template="#{dive_number} {divesite}",
                     reverse=r"#(?P<dive_number>\d+) (?P<divesite>.+)")
    text = "\n".join(validate_links([link], cat))
    assert "reverse parsing only supports text source fields" in text and "divelogs.dive_number" in text


def test_validate_links_includes_reverse_checks():
    cat = _catalog()
    assert validate_links([REVERSIBLE_SITE], cat) == []
    bad = REVERSIBLE_SITE.model_copy(update={"reverse": r"(?P<nope>.+)"})
    assert "unknown field (nope)" in "\n".join(validate_links([bad], cat))


def test_preview_uses_example_dives_or_given_ones():
    cat = _catalog()
    out = preview(SITE, cat)
    assert out["ok"] and out["text"] == "Zenobia (Larnaca)"
    out = preview(SITE, cat, {"divelogs": _divelogs_dive(service_fields={"location": "Malmö", "divesite": "Ön"})})
    assert out["text"] == "Ön (Malmö)"
    out = preview(SITE.model_copy(update={"template": "{nope}"}), cat)
    assert not out["ok"] and out["text"] is None and out["problems"]
    assert example_dive("garmin").service_fields["activityName"]
    assert example_dive("other").service_fields == {}


def test_preview_includes_reverse_self_check_when_reverse_is_set():
    cat = _catalog()
    out = preview(REVERSIBLE_SITE, cat)
    assert out["ok"] and out["text"] == "Zenobia (Larnaca)"
    assert out["reverse_sample"] == {"divelogs.divesite": "Zenobia", "divelogs.location": "Larnaca"}
    assert "reverse_sample" not in preview(SITE, cat)
    # a reverse pattern that doesn't actually invert the template shows as None, not a crash
    mismatched = REVERSIBLE_SITE.model_copy(update={"reverse": r"nope (?P<location>.+)"})
    assert preview(mismatched, cat)["reverse_sample"] is None


def test_auto_reverse_is_derived_from_the_template():
    cat = _catalog()
    link = FieldLink(id="x", source=["divelogs.location", "divelogs.divesite"], target="garmin.activityName",
                     direction="bidirectional", template="{divelogs.location}, {divelogs.divesite}", reverse="auto")
    assert validate_reverse(link, cat) == []
    assert reverse_parse(link, "Gozo, Blue Hole", cat) == {"divelogs.location": "Gozo", "divelogs.divesite": "Blue Hole"}
    # spacing around the separator is forgiven; the first separator splits
    assert reverse_parse(link, " Gozo ,Blue Hole ", cat) == {"divelogs.location": "Gozo", "divelogs.divesite": "Blue Hole"}
    assert reverse_parse(link, "Gozo, Blue Hole, North", cat)["divelogs.divesite"] == "Blue Hole, North"
    assert reverse_parse(link, "No separator here", cat) is None
    # two fields with nothing between them cannot be split
    glued = link.model_copy(update={"template": "{divelogs.location}{divelogs.divesite}"})
    assert "text between the fields" in "\n".join(validate_reverse(glued, cat))
    assert reverse_parse(glued, "GozoBlue", cat) is None
    assert preview(link, cat)["reverse_sample"]      # the self-check round-trips
