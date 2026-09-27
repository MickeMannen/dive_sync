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


def _manual_board():
    """A Garmin <-> Divelogs board with two ways to queue a conflict: a plain
    manual rule (buddy) and a composite whose split is manual (site)."""
    from src.core.fields import SyncRule
    return {
        "divelogs": [
            SyncRule(id="buddy", target="divelogs.buddy", source=["garmin.buddy"], conflict="manual"),
            SyncRule(id="site", target="divelogs.divesite", source=["garmin.locationName", "garmin.notes"],
                     template="{garmin.locationName} / {garmin.notes}", conflict="source_wins",
                     reverse="(?P<locationName>.+) / (?P<notes>.+)", reverse_conflict="manual"),
            SyncRule(id="notes", target="divelogs.notes", source=["garmin.notes"], conflict="prefer_non_empty"),
        ],
        "garmin": [SyncRule(id="temp", target="garmin.temp_min", source=["divelogs.temp_min"], conflict="manual")],
    }


def test_rule_conflict_keys_follow_the_manual_policies():
    from src.core.conflicts import rule_conflict_keys
    from src.core.fields import SyncRule
    board = _manual_board()
    assert rule_conflict_keys(board) == {
        ("buddy", "garmin", "divelogs", "garmin.buddy", "divelogs.buddy"),
        ("site", "garmin", "divelogs", "garmin.locationName", "divelogs.divesite"),   # the split, sources -> composite
        ("temp", "divelogs", "garmin", "divelogs.temp_min", "garmin.temp_min"),
    }
    # a composite without a reverse pattern never splits, so its reverse policy is moot
    board["divelogs"][1] = SyncRule(id="site", target="divelogs.divesite", source=["garmin.locationName", "garmin.notes"],
                                    template="{garmin.locationName} / {garmin.notes}", conflict="source_wins")
    assert {k[0] for k in rule_conflict_keys(board)} == {"buddy", "temp"}
    assert rule_conflict_keys(None) == set() and rule_conflict_keys({}) == set()
    # what the engine records for a split matches the key (rule_key of a stored entry)
    split = _conflict(link="site", source_key="garmin.locationName", target_key="divelogs.divesite")
    assert split.rule_key == ("site", "garmin", "divelogs", "garmin.locationName", "divelogs.divesite")


def test_prune_drops_only_stale_entries_between_the_pairs_services(tmp_path):
    from src.core.conflicts import ConflictStore, rule_conflict_keys
    store = ConflictStore(str(tmp_path / "conflicts.json"))
    live = _conflict()                                                     # buddy, still manual
    split = _conflict(link="site", source_key="garmin.locationName", target_key="divelogs.divesite")
    stale = _conflict(link="notes", source_key="garmin.notes", target_key="divelogs.notes")   # prefer_non_empty now
    moved = _conflict(link="buddy", source_key="garmin.notes")             # same id, re-pointed to another field
    other = _conflict(link="buddy", source_service="garmin", target_service="subsurface",
                      dive_ids={"garmin": "1", "subsurface": "s"})         # not this pair's business
    store.save([live, split, stale, moved, other])
    gone = store.prune(rule_conflict_keys(_manual_board()), {"garmin", "divelogs"})
    assert sorted(c.link_id for c in gone) == ["buddy", "notes"] and moved in gone
    assert [c.id for c in store.load()] == [live.id, split.id, other.id]
    # a missing file is never created
    assert ConflictStore(str(tmp_path / "none.json")).prune(set(), {"garmin", "divelogs"}) == []
    assert not (tmp_path / "none.json").exists()


def test_prune_stale_conflicts_walks_every_file_of_the_saved_pairs(tmp_path):
    """Owner request 2026-09-27: changing a rule away from manual clears its
    queued conflicts at once, not on the next run that spans their dives."""
    from src.core import layout
    from src.core.config import SettingsModel, SyncPairModel
    from src.core.conflicts import ConflictStore, prune_stale_conflicts
    settings = SettingsModel(sync_pairs=[SyncPairModel(id="garmin_divelogs", source="garmin", target="divelogs",
                                                       rules=_manual_board())])
    state_dir = str(tmp_path)
    folder = layout.sync_dir(state_dir)
    # one file per account combination and direction, like the engine keeps them
    a = ConflictStore(os.path.join(folder, "conflicts_u_v.json"))
    b = ConflictStore(os.path.join(folder, "conflicts_divelogs-v_garmin-u.json"))
    other = ConflictStore(os.path.join(folder, "conflicts_garmin_subsurface.json"))
    a.save([_conflict(), _conflict(link="notes", source_key="garmin.notes", target_key="divelogs.notes")])
    b.save([_conflict(link="temp", source_service="divelogs", target_service="garmin",
                      source_key="divelogs.temp_min", target_key="garmin.temp_min")])
    foreign = _conflict(source_service="garmin", target_service="subsurface", dive_ids={"garmin": "1", "subsurface": "s"})
    other.save([foreign])
    (tmp_path / "sync" / "sync_state_u_v.json").write_text("{}")          # not a conflicts file: ignored

    assert prune_stale_conflicts(settings, state_dir) == 1                 # notes went
    assert [c.link_id for c in a.load()] == ["buddy"] and [c.link_id for c in b.load()] == ["temp"]
    assert [c.id for c in other.load()] == [foreign.id]                    # no saved pair joins these: untouched

    # buddy goes to source_wins and the split to prefer_source: both queues empty out
    pair = settings.sync_pairs[0]
    pair.rules["divelogs"][0].conflict = "source_wins"
    pair.rules["divelogs"][1].reverse_conflict = "prefer_source"
    pair.rules["garmin"][0].conflict = "prefer_non_empty"
    assert prune_stale_conflicts(settings, state_dir, pair_ids=["garmin_divelogs"]) == 2
    assert a.load() == [] and b.load() == [] and os.path.exists(a.path)
    assert prune_stale_conflicts(settings, state_dir, pair_ids=["some_other_pair"]) == 0
    assert prune_stale_conflicts(settings, str(tmp_path / "nowhere")) == 0

    # a pair on the shipped defaults (rules=None) is checked against those
    settings.sync_pairs[0].rules = None
    a.save([_conflict(), _conflict(link="bogus", source_key="garmin.notes", target_key="divelogs.notes")])
    assert prune_stale_conflicts(settings, state_dir) == 1
    assert [c.link_id for c in a.load()] == ["buddy"]                      # buddy is manual by default
