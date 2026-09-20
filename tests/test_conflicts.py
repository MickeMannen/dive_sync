import json
import os
from datetime import datetime

from src.core.conflicts import Conflict, ConflictStore, conflicts_path_for, pair_key
from src.core.fields import deserialize_value, serialize_value
from src.core.models import GasMixture, UnifiedSample


def _conflict(link="buddy", a="1", b="2", **kw):
    base = dict(link_id=link, source_service="garmin", target_service="divelogs", source_external_id=a,
                target_external_id=b, source_key="garmin.buddy", target_key="divelogs.buddy", field_type="text",
                source_value="A", target_value="B", dive_time="2026-06-22 12:00:00",
                dive_ids={"garmin": a, "divelogs": b})
    base.update(kw)
    return Conflict(id=Conflict.make_id(link, "garmin", "divelogs", a, b), **base)


def test_conflicts_path_mirrors_state_file():
    assert conflicts_path_for("/x/sync_state.json") == "/x/conflicts.json"
    assert conflicts_path_for("/x/sync_state_a_b.json") == "/x/conflicts_a_b.json"
    assert conflicts_path_for("sync_state.json") == os.path.join(".", "conflicts.json")


def test_store_round_trip_replace_and_remove(tmp_path):
    store = ConflictStore(str(tmp_path / "conflicts.json"))
    assert store.load() == []
    c1, c2, c3 = _conflict(), _conflict(link="notes"), _conflict(a="9", b="8")
    store.save([c1, c2, c3])
    assert [c.id for c in store.load()] == [c1.id, c2.id, c3.id]
    assert store.get(c2.id[:6]).link_id == "notes"

    # A run that saw pair (1,2) and found only the notes conflict again:
    # buddy is dropped, notes refreshed, (9,8) untouched.
    c2b = _conflict(link="notes", source_value="A2")
    merged = store.replace_for_pairs({pair_key({"garmin": "1", "divelogs": "2"})}, [c2b])
    assert sorted(c.id for c in merged) == sorted([c2.id, c3.id])
    assert store.get(c2.id).source_value == "A2"

    assert store.remove(c3.id) and not store.remove(c3.id)
    assert [c.id for c in store.load()] == [c2.id]
    raw = json.load(open(store.path))
    assert raw["conflicts"][0]["link_id"] == "notes"


def test_store_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "conflicts.json"
    path.write_text("{not json")
    assert ConflictStore(str(path)).load() == []


def test_value_serialisation_round_trip():
    tanks = [GasMixture(oxygen=32.0, start_pressure=200.0)]
    assert deserialize_value("tanks", serialize_value("tanks", tanks)) == tanks
    samples = [UnifiedSample(depth=1.0, time=0)]
    assert deserialize_value("samples", serialize_value("samples", samples)) == samples
    assert deserialize_value("gps", serialize_value("gps", (1.0, 2.0))) == (1.0, 2.0)
    assert deserialize_value("number", serialize_value("number", (5.0, "kilogram"))) == (5.0, "kilogram")
    assert deserialize_value("number", serialize_value("number", 7)) == 7
    dt = datetime(2026, 6, 22, 12)
    assert deserialize_value("datetime", serialize_value("datetime", dt)) == dt
    assert serialize_value("text", None) is None and deserialize_value("text", None) is None
    json.dumps(serialize_value("tanks", tanks))  # must be JSON-safe
