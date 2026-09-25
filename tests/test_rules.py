"""rework.md G1: receiver-centric sync rules.

A board is stored as ``SyncRule`` lists keyed by the receiving service
(``sync_pairs[].rules``) plus the pair's ordered ``match_keys``; the once
implicit Garmin <-> Divelogs pair is the explicit ``garmin_divelogs`` entry.
``FieldLink`` stays the executed/edited form until G2/G5/G6, so the two
converters must be lossless for every board the old model could express -
that is what keeps the legacy-equivalence test in test_link_engine.py
meaningful on migrated data.
"""
import json
import random

import pytest
from fastapi.testclient import TestClient

from src.core.config import (DEFAULT_PAIR_ID, PROFILE_VERSION, SETTINGS_VERSION, ConfigManager, ProfileError,
                             SettingsModel, SyncFilters, SyncPairModel, export_profile, import_profile)
from src.core.fields import (FieldLink, SyncRule, common_default_links, default_field_links, legacy_field_links,
                             links_to_rules, pre_c12_default_field_links, rules_to_links,
                             submersion_default_links)
from src.core.templates import validate_links
from src.web.app import app
from tests.test_profile import _catalog


# ---------------------------------------------------------------- the model

def test_sync_rule_validation():
    rule = SyncRule(id="buddy", target="divelogs.buddy", source=["garmin.buddy"], conflict="manual")
    assert rule.receiver_id == "divelogs" and rule.sender_id == "garmin" and not rule.is_composite
    with pytest.raises(ValueError, match="at least one source"):
        SyncRule(id="x", target="divelogs.buddy", source=[])
    with pytest.raises(ValueError, match="needs a template"):
        SyncRule(id="x", target="garmin.activityName", source=["divelogs.location", "divelogs.divesite"])
    with pytest.raises(ValueError, match="also a source"):
        SyncRule(id="x", target="divelogs.buddy", source=["divelogs.buddy"])
    with pytest.raises(ValueError, match="'reverse' only applies"):
        SyncRule(id="x", target="divelogs.buddy", source=["garmin.buddy"], reverse="(?P<buddy>.+)")
    with pytest.raises(ValueError, match="needs a 'reverse'"):
        SyncRule(id="x", target="garmin.activityName", source=["divelogs.location", "divelogs.divesite"],
                 template="{divelogs.location}", reverse_conflict="manual")


# ---------------------------------------------------------------- links -> rules (the migration table)

def _rule(rules, receiver, rule_id):
    return next(r for r in rules[receiver] if r.id == rule_id)


def test_bidirectional_link_becomes_a_rule_on_each_receiver():
    rules, keys = links_to_rules([FieldLink(id="buddy", source=["garmin.buddy"], target="divelogs.buddy",
                                            conflict="manual")], "garmin", "divelogs")
    assert keys == []
    d, g = _rule(rules, "divelogs", "buddy"), _rule(rules, "garmin", "buddy")
    assert (d.target, d.source, d.conflict) == ("divelogs.buddy", ["garmin.buddy"], "manual")
    assert (g.target, g.source, g.conflict) == ("garmin.buddy", ["divelogs.buddy"], "manual")


def test_bidirectional_target_wins_is_never_overwrite_me_plus_source_wins():
    rules, _ = links_to_rules([FieldLink(id="buddy", source=["garmin.buddy"], target="divelogs.buddy",
                                         conflict="target_wins")], "garmin", "divelogs")
    assert _rule(rules, "divelogs", "buddy").conflict == "target_wins"   # Divelogs owns this field
    assert _rule(rules, "garmin", "buddy").conflict == "source_wins"     # Garmin takes Divelogs' value


def test_one_way_links_become_one_rule():
    rules, _ = links_to_rules([
        FieldLink(id="tanks", source=["garmin.tanks"], target="divelogs.tanks", direction="to_target", conflict="prefer_source"),
        FieldLink(id="gps_fill", source=["divelogs.gps"], target="garmin.gps", direction="to_target", conflict="prefer_non_empty"),
        FieldLink(id="notes", source=["garmin.notes"], target="divelogs.notes", direction="to_source", conflict="manual"),
        # a one-way target_wins always copied the other side over: kept as source_wins
        FieldLink(id="w", source=["garmin.weight"], target="divelogs.weight", direction="to_target", conflict="target_wins"),
    ], "garmin", "divelogs")
    assert [r.id for r in rules["divelogs"]] == ["tanks", "w"]
    assert _rule(rules, "divelogs", "w").conflict == "source_wins"
    assert [(r.id, r.target, r.source) for r in rules["garmin"]] == [
        ("gps_fill", "garmin.gps", ["divelogs.gps"]), ("notes", "garmin.notes", ["divelogs.notes"])]


def test_off_links_leave_no_rule_but_keep_their_match_key():
    rules, keys = links_to_rules([
        FieldLink(id="dive_number", source=["garmin.dive_number"], target="divelogs.dive_number", direction="off", match_order=2),
        FieldLink(id="when", source=["divelogs.date_time"], target="garmin.date_time", direction="off", match_order=1),
        # the "never overwrite me" marker survives as a target_wins rule
        FieldLink(id="title", source=["divelogs.divesite"], target="garmin.activityName", direction="off", conflict="target_wins"),
    ], "garmin", "divelogs")
    assert "divelogs" not in rules and [r.id for r in rules["garmin"]] == ["title"]
    assert _rule(rules, "garmin", "title").conflict == "target_wins"
    # ordered, and always written source-side first
    assert keys == [["garmin.date_time", "divelogs.date_time"], ["garmin.dive_number", "divelogs.dive_number"]]


def test_composites_live_on_their_targets_receiver():
    template, reverse = "{divelogs.divesite} ({divelogs.location})", r"(?P<divesite>.+) \((?P<location>.+)\)"
    sources = ["divelogs.location", "divelogs.divesite"]
    rules, _ = links_to_rules([
        FieldLink(id="one_way", source=sources, target="garmin.activityName", direction="to_target", conflict="manual",
                  template=template, reverse=reverse),
        FieldLink(id="both", source=sources, target="garmin.activityName", direction="bidirectional", conflict="manual",
                  template=template, reverse=reverse),
        FieldLink(id="owned", source=sources, target="garmin.activityName", direction="bidirectional", conflict="target_wins",
                  template=template, reverse=reverse),
        FieldLink(id="split_only", source=sources, target="garmin.activityName", direction="to_source", conflict="prefer_non_empty",
                  template=template, reverse=reverse),
    ], "garmin", "divelogs")
    assert list(rules) == ["garmin"]
    one_way, both, owned, split_only = rules["garmin"]
    assert one_way.reverse is None and one_way.conflict == "manual"              # an inert pattern is dropped
    assert (both.conflict, both.reverse, both.reverse_conflict) == ("manual", reverse, None)
    assert (owned.conflict, owned.reverse_conflict) == ("target_wins", "source_wins")
    assert (split_only.conflict, split_only.reverse_conflict) == ("target_wins", "prefer_non_empty")


# ---------------------------------------------------------------- rules -> links and back (lossless)

def _boards():
    return {
        "default": ("garmin", "divelogs", default_field_links()),
        "legacy": ("garmin", "divelogs", legacy_field_links()),
        "pre_c12": ("garmin", "divelogs", pre_c12_default_field_links()),
        "common": ("garmin", "submersion", common_default_links("garmin", "submersion")),
        "common_divelogs": ("divelogs", "subsurface", common_default_links("divelogs", "subsurface", match_on_dive_number=False)),
    }


@pytest.mark.parametrize("name", list(_boards()))
def test_shipped_boards_round_trip_exactly(name):
    source, target, board = _boards()[name]
    rules, keys = links_to_rules(board, source, target)
    back = rules_to_links(rules, keys, source, target)
    assert {l.id: l for l in back} == {l.id: l for l in board}
    assert links_to_rules(back, source, target) == (rules, keys)     # stable under a second round trip


def _random_board(rng, source="garmin", target="divelogs"):
    """Boards over the fields both catalogues share, in every direction and
    policy the link model allows, plus the shipped composite and the odd
    match key."""
    fields = ["buddy", "notes", "weight", "visibility", "gps", "dive_number"]
    links = []
    used = set()
    for name in rng.sample(fields, rng.randint(1, len(fields))):
        flip = rng.random() < 0.3
        src, tgt = (f"{target}.{name}", f"{source}.{name}") if flip else (f"{source}.{name}", f"{target}.{name}")
        direction = rng.choice(["bidirectional", "to_target", "to_source", "off"])
        conflict = rng.choice(["source_wins", "target_wins", "prefer_non_empty", "prefer_source", "manual"])
        match = rng.choice([None, None, 1, 2]) if name == "dive_number" else None
        links.append(FieldLink(id=name, source=[src], target=tgt, direction=direction, conflict=conflict, match_order=match))
        used.add(name)
    if rng.random() < 0.5:
        direction = rng.choice(["to_target", "bidirectional", "to_source", "off"])
        links.append(FieldLink(id="activity_name", source=[f"{target}.location", f"{target}.divesite"],
                               target=f"{source}.activityName", direction=direction,
                               conflict=rng.choice(["manual", "target_wins", "source_wins"]),
                               template="{divelogs.location}, {divelogs.divesite}",
                               reverse=None if direction == "to_target" and rng.random() < 0.5 else r"(?P<location>.+), (?P<divesite>.+)"))
    return links


def test_random_boards_round_trip_and_are_stable():
    """Every board the old model can write converts to rules and back to a
    board the engine treats the same way: same field pairs, same writable
    sides, same policies except the documented normalisations (a one-way
    target_wins reads source_wins; a two-way target_wins composite reads
    to_source with source_wins; an inert reverse pattern goes)."""
    rng = random.Random(20260923)
    for _ in range(300):
        board = _random_board(rng)
        rules, keys = links_to_rules(board, "garmin", "divelogs")
        back = rules_to_links(rules, keys, "garmin", "divelogs")
        # the same rules come back (a receiver's list may be reordered when
        # the link view interleaves the two receivers differently)
        rules2, keys2 = links_to_rules(back, "garmin", "divelogs")
        as_sets = lambda r: {receiver: {rule.id: rule for rule in items} for receiver, items in r.items()}
        assert (as_sets(rules2), keys2) == (as_sets(rules), keys), board

        def writes(links):
            out = set()
            for l in links:
                if l.direction in ("bidirectional", "to_target"):
                    if l.template and l.direction == "bidirectional" and l.conflict == "target_wins":
                        pass    # never writes its target under the receiver reading (documented normalisation)
                    else:
                        out.add((l.target, tuple(l.source), l.conflict if l.conflict != "target_wins" or l.direction != "to_target" else "source_wins"))
                if l.direction in ("bidirectional", "to_source") and (not l.template or l.reverse):
                    # target_wins on a two-way link (the source receiver's half) or on a
                    # one-way to_source link both copied the other side over: source_wins
                    policy = "source_wins" if l.conflict == "target_wins" else l.conflict
                    out.add((l.source[0] if not l.template else "split:" + l.target, (l.target,), policy))
            return out
        assert writes(back) == writes(board), board
        # match keys keep their order (the numbers themselves are renumbered 1..n on the pair)
        def keyed(links):
            return [frozenset((l.source[0], l.target)) for l in sorted((l for l in links if l.match_order), key=lambda l: l.match_order)]
        assert keyed(back) == keyed(board)


# ---------------------------------------------------------------- settings: the explicit default pair

def test_settings_always_carry_the_garmin_divelogs_pair_first():
    s = SettingsModel()
    assert [p.id for p in s.sync_pairs] == [DEFAULT_PAIR_ID] and s.settings_version == SETTINGS_VERSION
    assert s.default_pair().rules is not None and s.default_pair().directionality == "to_divelogs"
    s = SettingsModel(sync_pairs=[SyncPairModel(id="p", source="garmin", target="uddf:x.uddf")])
    assert [p.id for p in s.sync_pairs] == [DEFAULT_PAIR_ID, "p"]
    assert s.pair_for("garmin", "uddf").id == "p" and s.pair_for("uddf", "garmin") is None


def test_top_level_keys_are_views_over_the_default_pair():
    s = SettingsModel(directionality="to_garmin",
                      field_links=[FieldLink(id="buddy", source=["garmin.buddy"], target="divelogs.buddy", conflict="manual")])
    assert s.directionality == "to_garmin" and s.default_pair().directionality == "to_garmin"
    assert [l.id for l in s.field_links] == ["buddy"] and set(s.default_pair().rules) == {"divelogs", "garmin"}
    s.directionality = "to_divelogs"
    s.field_links = default_field_links()
    assert s.default_pair().directionality == "to_divelogs" and len(s.field_links) == 9
    # a pair's link view follows the same rule; None = the shipped defaults
    pair = SyncPairModel(id="p", source="garmin", target="submersion")
    assert pair.rules is None and pair.field_links is None
    # Submersion's shipped board is the metadata-only one (rework.md F17)
    assert [l.id for l in pair.effective_field_links()] == [l.id for l in submersion_default_links("garmin", "submersion")]
    uddf = SyncPairModel(id="u", source="garmin", target="uddf:out.uddf")
    # the shipped board for the pair: the common links UDDF can hold (it has no weight field)
    from src.core.pairs import default_links_for
    assert [l.id for l in uddf.effective_field_links()] == [l.id for l in default_links_for("garmin", "uddf")]
    pair.field_links = [FieldLink(id="notes", source=["garmin.notes"], target="submersion.notes", direction="to_target")]
    assert list(pair.rules) == ["submersion"] and pair.field_links[0].direction == "to_target"
    # nothing legacy is ever written
    dumped = s.model_dump()
    assert "field_links" not in dumped and "directionality" not in dumped and "field_links" not in dumped["sync_pairs"][0]


def test_v1_settings_file_with_pairs_migrates_to_v2_once(tmp_path, caplog):
    path = str(tmp_path / "settings.json")
    v1 = {
        "directionality": "to_garmin",
        "field_links": [l.model_dump() for l in legacy_field_links()],
        "sync_pairs": [{"id": "g2s", "source": "garmin", "target": "submersion", "directionality": "to_submersion",
                        "field_links": [{"id": "notes", "source": ["garmin.notes"], "target": "submersion.notes",
                                         "direction": "to_target", "conflict": "prefer_source"}]}],
    }
    json.dump(v1, open(path, "w"))
    import logging
    with caplog.at_level(logging.INFO, logger="dive_sync.config"):
        loaded = ConfigManager.load_settings(path)
    assert sum("upgraded to version 2" in r.getMessage() for r in caplog.records) == 1
    assert [p.id for p in loaded.sync_pairs] == [DEFAULT_PAIR_ID, "g2s"]
    assert loaded.directionality == "to_garmin"
    assert {l.id: l for l in loaded.field_links} == {l.id: l for l in legacy_field_links()}
    g2s = loaded.sync_pairs[1]
    assert list(g2s.rules) == ["submersion"] and g2s.rules["submersion"][0].conflict == "prefer_source"
    on_disk = json.load(open(path))
    assert on_disk["settings_version"] == 2 and "field_links" not in on_disk
    assert "field_links" not in on_disk["sync_pairs"][1] and "rules" in on_disk["sync_pairs"][1]
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="dive_sync.config"):
        assert ConfigManager.load_settings(path) == loaded
    assert not [r for r in caplog.records if "upgraded" in r.getMessage()]


def test_v2_file_without_the_default_pair_gets_it_back_with_defaults(tmp_path):
    path = str(tmp_path / "settings.json")
    json.dump({"settings_version": 2, "sync_pairs": [{"id": "g2s", "source": "garmin", "target": "submersion"}]}, open(path, "w"))
    loaded = ConfigManager.load_settings(path)
    assert [p.id for p in loaded.sync_pairs] == [DEFAULT_PAIR_ID, "g2s"]
    assert {l.id for l in loaded.field_links} == {l.id for l in default_field_links()}   # not the pre-C12 board


# ---------------------------------------------------------------- profiles

def test_profile_v2_round_trips_and_v1_is_folded_in(tmp_path):
    settings = SettingsModel(sync_pairs=[SyncPairModel(id="g2s", source="garmin", target="submersion",
                                                       directionality="to_submersion")])
    settings.field_links = [FieldLink(id="buddy", source=["garmin.buddy"], target="divelogs.buddy", conflict="manual")]
    profile = export_profile(settings)
    assert profile["dive_sync_profile"] == PROFILE_VERSION == 2
    new, summary = import_profile(profile, SettingsModel(), _catalog())
    assert new == settings
    assert any("added g2s" in line for line in summary.changes)
    # a version-1 profile (top-level keys) lands on the garmin_divelogs pair and keeps the other pairs
    v1 = {"dive_sync_profile": 1, "directionality": "to_garmin",
          "field_links": [{"id": "notes", "source": ["garmin.notes"], "target": "divelogs.notes"}]}
    new2, summary2 = import_profile(v1, settings, _catalog())
    assert new2.directionality == "to_garmin" and [l.id for l in new2.field_links] == ["notes"]
    assert [p.id for p in new2.sync_pairs] == [DEFAULT_PAIR_ID, "g2s"]
    assert summary2.sections == ["sync_pairs"] and summary2.ignored_keys == []
    with pytest.raises(ProfileError, match="newer Dive Sync"):
        import_profile({"dive_sync_profile": PROFILE_VERSION + 1}, settings)


# ---------------------------------------------------------------- the boards keep working (G4 compatibility read)

def _isolated(tmp_path, monkeypatch):
    from tests.test_web_api import _isolated_settings
    _isolated_settings(tmp_path, monkeypatch)


def test_settings_api_serves_rules_and_keeps_the_default_pair(tmp_path, monkeypatch, submersion_enabled):
    _isolated(tmp_path, monkeypatch)
    client = TestClient(app)
    data = client.get("/api/settings").json()
    assert data["settings_version"] == 2 and data["directionality"] == "to_divelogs"
    assert "field_links" not in data                                   # rework.md G7: rules only
    assert data["sync_pairs"][0]["id"] == DEFAULT_PAIR_ID and "field_links" not in data["sync_pairs"][0]
    assert {r: len(v) for r, v in data["sync_pairs"][0]["rules"].items()} == {"divelogs": 8, "garmin": 7}

    # the status page posts the pairs table without garmin_divelogs and the default board at top level
    payload = {"directionality": "to_garmin", "sync_filters": data["sync_filters"], "grace_window_minutes": 15,
               "api_cooldown_seconds": 1.0, "schedule": [], "cron_jobs": [],
               "field_links": [{"id": "buddy", "source": ["garmin.buddy"], "target": "divelogs.buddy", "conflict": "manual"}],
               "sync_pairs": [{"id": "g2s", "source": "garmin", "target": "submersion", "directionality": "to_submersion",
                               "enabled": True, "field_links": [{"id": "notes", "source": ["garmin.notes"],
                                                                 "target": "submersion.notes", "direction": "to_target"}]}]}
    res = client.post("/api/settings", json=payload)
    assert res.status_code == 200, res.text
    data = client.get("/api/settings").json()
    assert [p["id"] for p in data["sync_pairs"]] == [DEFAULT_PAIR_ID, "g2s"]
    assert data["directionality"] == "to_garmin"
    assert {r: [x["id"] for x in v] for r, v in data["sync_pairs"][0]["rules"].items()} == {"divelogs": ["buddy"], "garmin": ["buddy"]}
    assert data["sync_pairs"][1]["rules"] == {"submersion": [{"id": "notes", "target": "submersion.notes", "source": ["garmin.notes"],
                                                              "conflict": "source_wins", "template": None, "reverse": None,
                                                              "reverse_conflict": None, "separator": ", ", "when": None}]}

    # posting the pairs table back unchanged changes nothing
    again = dict(payload, sync_pairs=data["sync_pairs"][1:])
    again.pop("field_links")
    assert client.post("/api/settings", json=again).status_code == 200
    assert client.get("/api/settings").json()["sync_pairs"] == data["sync_pairs"]

    # the fields endpoint lists the default pair first under its real id
    pairs = client.get("/api/fields").json()["pairs"]
    assert [p["id"] for p in pairs] == [DEFAULT_PAIR_ID, "g2s"]


def test_link_view_of_a_v2_board_still_validates_against_the_catalogue():
    settings = SettingsModel()
    assert validate_links(settings.field_links, _catalog()) == []
    assert validate_links(rules_to_links(*links_to_rules(legacy_field_links(), "garmin", "divelogs"),
                                         "garmin", "divelogs"), _catalog()) == []


# ---------------------------------------------------------------- the engine runs rules natively (G2)

from datetime import datetime  # noqa: E402

from tests.test_link_engine import _dive, _engine, _pair  # noqa: E402


def _rules_engine(tmp_path, g, d, rules, direction="to_divelogs", match_keys=None, **settings):
    pair = SyncPairModel(id="garmin_divelogs", source="garmin", target="divelogs", directionality=direction,
                         rules=rules, match_keys=match_keys or [])
    return _engine(tmp_path, g, d, sync_pairs=[pair], **settings)


def test_target_wins_rule_never_overwrites_the_receiver(tmp_path):
    """The receiver reading of target_wins (rework.md Track G): a documented
    no-op. Before G2 the same board copied Garmin's value over on a run
    towards Divelogs (the single-writable-side rule)."""
    rules = {"divelogs": [SyncRule(id="buddy", target="divelogs.buddy", source=["garmin.buddy"], conflict="target_wins")],
             "garmin": [SyncRule(id="buddy", target="garmin.buddy", source=["divelogs.buddy"], conflict="source_wins")]}
    g, d = _pair({"buddy": "A"}, {"buddy": "B"})
    engine = _rules_engine(tmp_path, [g], [d], rules)
    assert [e.rule.id for e in engine.active_rules()] == []           # nothing to do towards Divelogs
    res = engine.run_sync(dry_run=False)
    assert (g.buddy, d.buddy) == ("A", "B") and not res["updated_on_divelogs"]
    res = engine.run_sync(dry_run=False, direction_override="to_garmin")
    assert (g.buddy, d.buddy) == ("B", "B") and len(res["updated_on_garmin"]) == 1
    # the same rule on an upload: a new dive is not filled from a never-overwrite rule either
    engine2 = _rules_engine(tmp_path, [_dive(external_ids={"garmin": "7"}, buddy="Z")], [], rules)
    engine2.run_sync(dry_run=False)
    assert engine2.target.added[0].buddy == "Z"       # the unified attribute travels with the dive itself
    only = {"divelogs": [SyncRule(id="site", target="divelogs.divesite", source=["garmin.locationName"], conflict="target_wins")]}
    engine3 = _rules_engine(tmp_path, [_dive(external_ids={"garmin": "8"}, service_fields={"locationName": "Reef"})], [], only)
    engine3.run_sync(dry_run=False)
    assert engine3.target.added[0].service_fields.get("divesite") is None


def test_policies_are_read_from_the_receiver(tmp_path):
    def run(policy, g_buddy, d_buddy, direction="to_divelogs"):
        rules = {"divelogs": [SyncRule(id="b", target="divelogs.buddy", source=["garmin.buddy"], conflict=policy)],
                 "garmin": [SyncRule(id="b", target="garmin.buddy", source=["divelogs.buddy"], conflict=policy)]}
        g, d = _pair({"buddy": g_buddy}, {"buddy": d_buddy})
        res = _rules_engine(tmp_path, [g], [d], rules, direction).run_sync(dry_run=False)
        return g.buddy, d.buddy, bool(res["updated_on_divelogs"]), bool(res["updated_on_garmin"]), len(res["conflicts"])

    assert run("source_wins", "A", "B") == ("A", "A", True, False, 0)
    assert run("source_wins", None, "B") == (None, None, True, False, 0)          # even an empty sender value
    assert run("prefer_source", "A", "B") == ("A", "A", True, False, 0)
    assert run("prefer_source", None, "B") == (None, "B", False, False, 0)        # never blanked
    assert run("prefer_non_empty", "A", "B") == ("A", "B", False, False, 0)      # fill only
    assert run("prefer_non_empty", "A", None) == ("A", "A", True, False, 0)
    assert run("manual", "A", "B") == ("A", "B", False, False, 1)                 # queued
    assert run("manual", "A", "", ) == ("A", "A", True, False, 0)
    assert run("manual", "A", "B", "to_garmin") == ("A", "B", False, False, 1)    # symmetric rule, other receiver
    assert run("source_wins", "A", "B", "to_garmin") == ("B", "B", False, True, 0)


def test_split_uses_its_own_policy_and_reports_the_receiver(tmp_path):
    rule = SyncRule(id="title", target="garmin.activityName", source=["divelogs.location", "divelogs.divesite"],
                    conflict="target_wins", template="{divelogs.divesite} ({divelogs.location})",
                    reverse=r"(?P<divesite>.+) \((?P<location>.+)\)", reverse_conflict="source_wins")
    rules = {"garmin": [rule]}
    g, d = _pair({"service_fields": {"activityName": "Wreck Alpha (Malmo)"}},
                 {"service_fields": {"location": "Larnaca", "divesite": "Zenobia"}})
    engine = _rules_engine(tmp_path, [g], [d], rules)
    active = engine.active_rules()
    assert [(e.rule.id, e.split, e.receiver, e.policy) for e in active] == [("title", True, "divelogs", "source_wins")]
    out = engine.test_mapping()
    row = out["rows"][0]
    assert out["receiver"] == "divelogs" and row["receiver"] == "divelogs" and row["split"] is True
    assert row["source_key"] == "garmin.activityName" and row["target_key"] == "divelogs.location, divelogs.divesite"
    assert row["result"] == "write:divelogs.divesite,divelogs.location"      # in the pattern's group order
    res = engine.run_sync(dry_run=False)
    assert d.service_fields["location"] == "Malmo" and d.service_fields["divesite"] == "Wreck Alpha"
    assert len(res["updated_on_divelogs"]) == 1
    # towards Garmin the composite is target_wins: never overwrite the title
    g.service_fields["activityName"] = "totally different"
    assert engine.active_rules("garmin") == []
    # a fill-only split policy leaves a real difference alone and, with manual, queues it as sources -> title
    rule2 = rule.model_copy(update={"reverse_conflict": "manual"})
    g2, d2 = _pair({"service_fields": {"activityName": "Wreck Alpha (Malmo)"}},
                   {"service_fields": {"location": "Larnaca", "divesite": "Zenobia"}})
    res = _rules_engine(tmp_path, [g2], [d2], {"garmin": [rule2]}).run_sync(dry_run=False)
    assert d2.service_fields["location"] == "Larnaca" and len(res["conflicts"]) == 1
    conflict = res["conflicts"][0]
    assert (conflict["source_service"], conflict["target_service"]) == ("divelogs", "garmin")
    assert conflict["source_key"] == "divelogs.location" and conflict["target_key"] == "garmin.activityName"
    assert conflict["source_value"] == "Zenobia (Larnaca)" and conflict["target_value"] == "Wreck Alpha (Malmo)"


def test_split_applies_to_new_dives_too(tmp_path):
    """A dive only Garmin has is uploaded to Divelogs with the activity name
    taken apart by the split, like a matched dive would be."""
    split = SyncRule(id="activity_name", target="garmin.activityName", source=["divelogs.location", "divelogs.divesite"],
                     conflict="prefer_non_empty", template="{divelogs.location}, {divelogs.divesite}",
                     reverse="auto", reverse_conflict="prefer_source")
    site = SyncRule(id="site", target="divelogs.divesite", source=["garmin.locationName"], conflict="prefer_non_empty")

    def upload(rules, name="Gozo, Blue Hole"):
        g = _dive(external_ids={"garmin": "1"}, location=name,
                  service_fields={"activityName": name, "locationName": "Blue Hole North"})
        engine = _rules_engine(tmp_path, [g], [], rules)
        engine.run_sync(dry_run=False)
        return engine.target.added[0].service_fields

    fields = upload({"garmin": [split]})
    assert (fields["location"], fields["divesite"]) == ("Gozo", "Blue Hole")
    # prefer_source: the split wins over a plain rule filling the same field
    fields = upload({"garmin": [split], "divelogs": [site]})
    assert (fields["location"], fields["divesite"]) == ("Gozo", "Blue Hole")
    # a fill-only split leaves what that rule wrote and fills the rest
    fill_only = split.model_copy(update={"reverse_conflict": "prefer_non_empty"})
    fields = upload({"garmin": [fill_only], "divelogs": [site]})
    assert (fields["location"], fields["divesite"]) == ("Gozo", "Blue Hole North")
    # a name the pattern does not describe is left to the other rules
    fields = upload({"garmin": [split], "divelogs": [site]}, name="Night dive")
    assert "location" not in fields and fields["divesite"] == "Blue Hole North"


def test_rules_override_and_pair_match_keys(tmp_path):
    g = [_dive(external_ids={"garmin": "1"}, dive_number=3, buddy="Anna")]
    d = [_dive(date_time=datetime(2026, 6, 22, 20), external_ids={"divelogs": "2"}, dive_number=3, buddy=None)]
    rules = {"divelogs": [SyncRule(id="buddy", target="divelogs.buddy", source=["garmin.buddy"], conflict="prefer_non_empty")]}
    engine = _rules_engine(tmp_path, g, d, rules, match_keys=[["garmin.dive_number", "divelogs.dive_number"]])
    assert [(a.key, b.key) for a, b in engine.match_key_specs()] == [("garmin.dive_number", "divelogs.dive_number")]
    res = engine.run_sync(dry_run=False)
    assert res["matched_count"] == 1 and d[0].buddy == "Anna"
    # a per-run rules override replaces the board for that run only
    d[0].buddy = None
    res = engine.run_sync(dry_run=False, rules_override={"divelogs": []})
    assert res["matched_count"] == 1 and d[0].buddy is None and not res["updated_on_divelogs"]
    assert len(engine.pair.rules["divelogs"]) == 1        # the saved board itself is untouched
    engine.run_sync(dry_run=True)
    assert len(engine.rules["divelogs"]) == 1             # and the next run is back on it
    # a bad match key is skipped with a warning, not fatal
    engine.match_keys = [["garmin.buddy", "divelogs.buddy"], ["garmin.nope", "divelogs.dive_number"], ["garmin.dive_number"]]
    assert engine.match_key_specs() == []


def test_invalid_rules_are_skipped_not_fatal(tmp_path):
    rules = {"divelogs": [SyncRule(id="bad", target="divelogs.max_depth", source=["garmin.buddy"]),
                          SyncRule(id="buddy", target="divelogs.buddy", source=["garmin.buddy"], conflict="source_wins")]}
    g, d = _pair({"buddy": "A"}, {"buddy": "B"})
    engine = _rules_engine(tmp_path, [g], [d], rules)
    assert [e.rule.id for e in engine.active_rules()] == ["buddy"]
    engine.run_sync(dry_run=False)
    assert d.buddy == "A"


# ---------------------------------------------------------------- status page board (G5)

def test_fields_endpoint_ships_default_rules_and_mapping_test_takes_rules(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    client = TestClient(app)
    pair = client.get("/api/fields").json()["pairs"][0]
    assert pair["id"] == DEFAULT_PAIR_ID
    assert {r: len(v) for r, v in pair["default_rules"].items()} == {"divelogs": 8, "garmin": 7}
    assert pair["default_match_keys"] == [] and "default_links" not in pair

    import src.web.app as web
    seen = {}

    class Engine:
        def test_mapping(self, links=None, limit=10, rules=None, match_keys=None):
            seen.update(links=links, rules=rules, match_keys=match_keys, limit=limit)
            return {"ok": True, "problems": [], "rows": [], "receiver": "divelogs"}
    monkeypatch.setattr(web, "_engine_for_pair_id", lambda pair_id: Engine())
    body = {"pair": DEFAULT_PAIR_ID, "limit": 5,
            "rules": {"divelogs": [{"id": "buddy", "target": "divelogs.buddy", "source": ["garmin.buddy"], "conflict": "manual"}]},
            "match_keys": [["garmin.dive_number", "divelogs.dive_number"]]}
    res = client.post("/api/mapping/test", json=body)
    assert res.status_code == 200 and res.json()["receiver"] == "divelogs"
    assert seen["links"] is None and seen["rules"]["divelogs"][0].id == "buddy"
    assert seen["match_keys"] == [["garmin.dive_number", "divelogs.dive_number"]] and seen["limit"] == 5


def test_board_saves_rules_through_sync_pairs(tmp_path, monkeypatch, submersion_enabled):
    """The rebuilt status-page board posts the pair entry with rules and
    match_keys (no top-level field_links) - also for the garmin_divelogs
    pair - and every pair's board is validated against its own catalogue."""
    _isolated(tmp_path, monkeypatch)
    client = TestClient(app)
    data = client.get("/api/settings").json()
    default = dict(data["sync_pairs"][0])
    default["rules"] = {"divelogs": [{"id": "buddy", "target": "divelogs.buddy", "source": ["garmin.buddy"], "conflict": "target_wins"}],
                        "garmin": [{"id": "title", "target": "garmin.activityName", "source": ["divelogs.location", "divelogs.divesite"],
                                    "conflict": "manual", "template": "{divelogs.divesite} ({divelogs.location})",
                                    "reverse": r"(?P<divesite>.+) \((?P<location>.+)\)", "reverse_conflict": "source_wins"}]}
    default["match_keys"] = [["garmin.dive_number", "divelogs.dive_number"]]
    default["directionality"] = "to_garmin"
    payload = {"directionality": "to_garmin", "sync_filters": data["sync_filters"], "grace_window_minutes": 15,
               "api_cooldown_seconds": 1.0, "schedule": [], "cron_jobs": [], "sync_pairs": [default]}
    res = client.post("/api/settings", json=payload)
    assert res.status_code == 200, res.text
    saved = client.get("/api/settings").json()["sync_pairs"][0]
    assert saved["directionality"] == "to_garmin" and saved["match_keys"] == [["garmin.dive_number", "divelogs.dive_number"]]
    assert saved["rules"]["divelogs"][0]["conflict"] == "target_wins"
    assert saved["rules"]["garmin"][0]["reverse_conflict"] == "source_wins"
    # the never-overwrite rule and the match key survive the link view validation runs on
    links = {l.id: l for l in ConfigManager.load_settings().default_pair().field_links}
    assert links["buddy"].direction == "off" and links["buddy"].conflict == "target_wins"
    assert links["dive_number"].match_order == 1 and links["title"].direction == "bidirectional"

    # a rule that its catalogue rejects is refused, naming the pair
    default["rules"] = {"divelogs": [{"id": "bad", "target": "divelogs.max_depth", "source": ["garmin.buddy"]}]}
    res = client.post("/api/settings", json=payload)
    assert res.status_code == 400 and "Pair 'garmin_divelogs'" in res.text and "cannot link" in res.text
    # ... and so is one on another pair
    other = {"id": "g2s", "source": "garmin", "target": "submersion", "directionality": "to_submersion", "enabled": True,
             "rules": {"submersion": [{"id": "x", "target": "submersion.buddy", "source": ["garmin.tanks"]}]}}
    payload["sync_pairs"] = [other]
    res = client.post("/api/settings", json=payload)
    assert res.status_code == 400 and "Pair 'g2s'" in res.text
