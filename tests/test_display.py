"""Track J, J6: src/core/display.py - the one implementation of "which
recorded value is a quantity, and what does it read as in the chosen unit
system" shared by the desktop Conflicts page, the CLI and the web
dashboard. Catalogue-driven, never by field name."""
from src.core import display
from src.core.conflicts import Conflict
from src.core.fields import FieldSpec
from src.core.pairs import field_catalog_of


def _specs():
    return {f.key: f for f in field_catalog_of("garmin") + field_catalog_of("divelogs")}


def _conflict(name, src_val, tgt_val, field_type="number", source_type=None, target=None, source_key=None):
    return Conflict(id=Conflict.make_id(name, "garmin", "divelogs", "1", "2"), link_id=name,
                    source_service="garmin", target_service="divelogs", source_external_id="1", target_external_id="2",
                    source_key=source_key or f"garmin.{name}", target_key=f"divelogs.{target or name}",
                    source_type=source_type or field_type, field_type=field_type,
                    dive_ids={"garmin": "1", "divelogs": "2"}, source_value=src_val, target_value=tgt_val)


def test_quantity_kind_comes_from_the_catalogue():
    specs = _specs()
    assert display.quantity_kind(specs["garmin.max_depth"]) == "depth"
    assert display.quantity_kind(specs["divelogs.temp_min"]) == "temperature"
    assert display.quantity_kind(specs["garmin.weight"]) == "weight"          # own-unit quantity, by `unified`
    assert display.quantity_kind(specs["divelogs.visibility"]) == "depth"
    assert display.quantity_kind(specs["garmin.duration"]) == ""              # "s" is no kind
    assert display.quantity_kind(specs["garmin.buddy"]) == "" and display.quantity_kind(None) == ""
    # the unit decides, not the name
    assert display.quantity_kind(FieldSpec(key="x.anything", label="A", type="number", unit="bar")) == "pressure"
    assert display.quantity_kind(FieldSpec(key="x.anything", label="A", type="number", unit="L")) == "volume"
    assert display.quantity_kind(FieldSpec(key="x.max_depth", label="A", type="text", unit="m")) == ""


def test_field_spec_and_conflict_kinds():
    assert display.field_spec("garmin.max_depth").label == "Max depth"
    assert display.field_spec("garmin.no_such_field") is None
    assert display.field_spec("nosuchservice.x") is None and display.field_spec("") is None
    specs = _specs()
    assert display.conflict_kinds(specs["garmin.max_depth"], specs["divelogs.max_depth"]) == ("depth", "depth")
    # a side the catalogue does not know takes the other's kind
    assert display.conflict_kinds(None, specs["divelogs.max_depth"]) == ("depth", "depth")
    assert display.conflict_kinds(specs["garmin.buddy"], None) == ("", "")


def test_quantity_text_is_the_desktop_rule():
    assert display.quantity_text(30.2, "depth", "metric") == "30.2"          # metric in metric: brief, unchanged
    assert display.quantity_text(30.2, "depth", "imperial") == "99 ft"
    assert display.quantity_text(24.5, "temperature", "imperial") == "76 °F"
    assert display.quantity_text([4.0, "kilogram"], "weight", "imperial") == "8.8 lb"
    assert display.quantity_text([10.0, "pound"], "weight", "imperial") == "10.0 lb"
    assert display.quantity_text([10.0, "pound"], "weight", "metric") == "4.5 kg"
    assert display.quantity_text([80.0, "foot"], "depth", "metric") == "24.4 m"
    assert display.quantity_text([4.0, "kilogram"], "", "imperial") == "4.0, kilogram"   # no kind: brief
    assert display.quantity_text("n/a", "depth", "imperial") == "n/a"
    assert display.quantity_text(None, "depth", "imperial") == "(empty)"
    assert display._split_quantity(True) == (None, "") and display._split_quantity([1, 2, 3]) == (None, "")
    assert display._own_system("pound") == "imperial" and display._own_system("feet") == "imperial"
    assert display._own_system("kilogram") == "metric" and display._own_system(None) == "metric"


def test_converted_text_only_changes_imperial_number_quantities():
    # metric: never anything (the CLI and the web print as they always have)
    assert display.converted_text(30.2, "number", "depth", "metric") is None
    assert display.converted_text([10.0, "pound"], "number", "weight", "metric") is None
    # imperial: a number of a kind converts, with its label
    assert display.converted_text(30.2, "number", "depth", "imperial") == "99 ft"
    assert display.converted_text(30.5, "number", "depth", "imperial") == "100 ft"
    assert display.converted_text(200.0, "number", "pressure", "imperial") == "2901 psi"
    assert display.converted_text([4.0, "kilogram"], "number", "weight", "imperial") == "8.8 lb"
    # ... and nothing else does
    assert display.converted_text(2700, "number", "", "imperial") is None            # a duration: no kind
    assert display.converted_text("30.2 m", "text", "depth", "imperial") is None     # a rendered template
    assert display.converted_text(None, "number", "depth", "imperial") is None
    assert display.converted_text("", "number", "depth", "imperial") is None
    assert display.converted_text("ft", "number", "depth", "us") is None             # not a number


def test_conflict_texts_for_a_conflict_and_a_dict():
    depth = _conflict("max_depth", 30.2, 30.5)
    assert display.conflict_texts(depth, "metric") == (None, None)
    assert display.conflict_texts(depth, "imperial") == ("99 ft", "100 ft")
    # the same through an engine-style catalogue dict
    assert display.conflict_texts(depth, "imperial", _specs()) == ("99 ft", "100 ft")
    # a run's results / the history hold the conflict as a dict
    assert display.conflict_texts(depth.model_dump(mode="json"), "imperial") == ("99 ft", "100 ft")
    temp = _conflict("temp_min", 24.5, 26.0)
    assert display.conflict_texts(temp, "imperial") == ("76 °F", "79 °F")
    weight = _conflict("weight", [4.0, "kilogram"], [10.0, "pound"])
    assert display.conflict_texts(weight, "imperial") == ("8.8 lb", "10.0 lb")
    vis = _conflict("visibility", [25.0, "meter"], [80.0, "foot"])
    assert display.conflict_texts(vis, "imperial") == ("82 ft", "80 ft")
    # a duration, a text and a rendered template (text source, unknown key) never convert
    assert display.conflict_texts(_conflict("duration", 2700, 2760), "imperial") == (None, None)
    assert display.conflict_texts(_conflict("buddy", "Anna", "Bob", field_type="text"), "imperial") == (None, None)
    rendered = _conflict("max_depth", "max 30.2 m", 30.5, source_type="text", source_key="garmin.max_depth:tpl")
    assert display.conflict_texts(rendered, "imperial") == (None, "100 ft")
    # the catalogue decides the kind: a key no catalogue has, number-typed, takes its partner's kind
    assert display.conflict_texts(_conflict("max_depth", 30.2, 30.5, source_key="garmin.unknown"), "imperial") == ("99 ft", "100 ft")


def test_row_texts_for_test_mapping_rows():
    row = {"source_key": "garmin.max_depth", "target_key": "divelogs.max_depth", "source_value": 30.2, "target_value": 30.5}
    assert display.row_texts(row, "metric") == (None, None)
    assert display.row_texts(row, "imperial") == ("99 ft", "100 ft")
    assert display.row_texts(row, "imperial", _specs()) == ("99 ft", "100 ft")
    # the type is the catalogue's: a rendered template's source (no entry) is text
    tpl = {"source_key": "garmin.max_depth:tpl", "target_key": "divelogs.notes", "source_value": "max 30.2 m", "target_value": "x"}
    assert display.row_texts(tpl, "imperial") == (None, None)
    buddy = {"source_key": "garmin.buddy", "target_key": "divelogs.buddy", "source_value": "A", "target_value": "B"}
    assert display.row_texts(buddy, "imperial") == (None, None)
    assert display.row_texts({}, "imperial") == (None, None)
