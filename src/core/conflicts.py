"""Manual-conflict queue (rework.md Track C, step C4).

A link with conflict policy ``manual`` never overwrites: when both sides hold
a non-empty, different value the engine records a ``Conflict`` here instead.
Entries live in ``conflicts.json`` next to ``sync_state.json`` (in ``sync/``, layout.py). Every run
re-checks the matched pairs it saw and replaces their entries, so a conflict
that no longer exists disappears on the next run. Resolving one (from the
CLI or a UI) writes the chosen value to the losing side through the normal
adapter ``update_dive`` and removes the entry; that part lives on
``SyncEngine.resolve_conflict`` because it needs the adapters.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger("dive_sync.conflicts")

CONFLICTS_FILE = "conflicts.json"


class Conflict(BaseModel):
    id: str = Field(..., description="Stable short id derived from the link and the two dive ids")
    link_id: str
    source_service: str
    target_service: str
    source_external_id: Optional[str] = None
    target_external_id: Optional[str] = None
    source_key: str
    target_key: str
    source_type: str = Field("text", description="Catalogue type of the source value (text for a rendered template)")
    field_type: str = Field(..., description="Catalogue type of the target field")
    dive_ids: Dict[str, Optional[str]] = Field(default_factory=dict, description="{service_id: external id} for both dives")
    source_value: Any = None
    target_value: Any = None
    dive_time: str = Field("", description="Start time of the matched dive (source side), for display")
    seen_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @staticmethod
    def make_id(link_id: str, source_service: str, target_service: str,
                source_external_id: Optional[str], target_external_id: Optional[str]) -> str:
        raw = "|".join([link_id, source_service, target_service, str(source_external_id), str(target_external_id)])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]

    @property
    def pair_key(self) -> Tuple[Tuple[str, Optional[str]], ...]:
        """Identifies the matched pair independent of which way the link points."""
        return pair_key(self.dive_ids)

    @property
    def rule_key(self) -> Tuple[str, str, str, str, str]:
        """What ``rule_conflict_keys`` lists for the rule that queued this
        entry, so a board change can tell a stale entry from a live one."""
        return (self.link_id, self.source_service, self.target_service, self.source_key, self.target_key)


def pair_key(dive_ids: Dict[str, Optional[str]]) -> Tuple[Tuple[str, Optional[str]], ...]:
    return tuple(sorted(dive_ids.items()))


def rule_conflict_keys(rules: Optional[Dict[str, list]]) -> Set[Tuple[str, str, str, str, str]]:
    """Every conflict a board can still queue, as the (link id, source
    service, target service, source key, target key) tuple
    ``SyncEngine._conflict_for`` records. A plain rule queues one on its
    receiver when its policy is ``manual``; a composite with a reverse
    pattern queues the same tuple on a run towards its sources' side when
    its split policy is ``manual`` (``_conflict_for`` keeps the sources ->
    composite orientation for a split, so the tuple is the same either way)."""
    keys: Set[Tuple[str, str, str, str, str]] = set()
    for receiver, items in (rules or {}).items():
        for rule in items:
            split_manual = bool(rule.template and rule.reverse) and (rule.reverse_conflict or rule.conflict) == "manual"
            if rule.conflict == "manual" or split_manual:
                keys.add((rule.id, rule.sender_id, receiver, rule.source[0], rule.target))
    return keys


def prune_stale_conflicts(settings, state_dir: str, pair_ids: Optional[Iterable[str]] = None) -> int:
    """Drop every waiting conflict the saved boards no longer raise (owner
    request, 2026-09-27): after a rule is deleted, re-pointed or given a
    policy other than ``manual``, its queued entries would otherwise sit on
    the Conflicts page until a run that spans their dives happens to compare
    them again. Walks every ``conflicts*.json`` in the sync folder, since a
    pair keeps one file per direction and account combination; entries
    between services no saved pair joins are left alone (they run on the
    shipped defaults, which do not change). ``pair_ids`` limits the check to
    those pairs. Returns how many entries were dropped."""
    from src.core import layout
    from src.core.fields import links_to_rules

    keys_by_services: Dict[frozenset, Set[Tuple[str, str, str, str, str]]] = {}
    for pair in settings.sync_pairs:
        if pair_ids is not None and pair.id not in set(pair_ids):
            continue
        try:
            services = frozenset({pair.source_service, pair.target_service})
        except Exception:
            continue            # a disabled or unknown service: nothing of it is queued
        if pair.rules is not None:
            rules = pair.rules
        else:
            rules, _keys = links_to_rules(pair.effective_field_links(), pair.source_service, pair.target_service)
        keys_by_services.setdefault(services, set()).update(rule_conflict_keys(rules))

    folder = layout.sync_dir(state_dir)
    if not keys_by_services or not os.path.isdir(folder):
        return 0
    dropped = 0
    for name in sorted(os.listdir(folder)):
        if not (name.startswith("conflicts") and name.endswith(".json")):
            continue
        store = ConflictStore(os.path.join(folder, name))
        for services, keys in keys_by_services.items():
            for gone in store.prune(keys, set(services)):
                dropped += 1
                logger.info("Dropped conflict %s (%s -> %s, rule %s): the board no longer raises it",
                            gone.id, gone.source_key, gone.target_key, gone.link_id)
    return dropped


def conflicts_path_for(state_file: str) -> str:
    """``conflicts.json`` beside the engine's state file, sharing its suffix
    (``sync_state_<a>_<b>.json`` -> ``conflicts_<a>_<b>.json``)."""
    directory, name = os.path.split(state_file)
    suffix = name[len("sync_state"):-len(".json")] if name.startswith("sync_state") and name.endswith(".json") else ""
    return os.path.join(directory or ".", f"conflicts{suffix}.json")


def display_value(value: Any) -> str:
    """A recorded conflict value as a person reads it: text as is, numbers
    without a float tail, lists joined, tanks/profiles as a count."""
    if value is None or value == "" or value == []:
        return "(empty)"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, list):
        if value and isinstance(value[0], dict):
            return f"{len(value)} item(s)"
        if len(value) == 2 and all(isinstance(v, (int, float)) for v in value):
            return f"{value[0]:.6g}, {value[1]:.6g}"          # a position
        return ", ".join(str(v) for v in value)
    return str(value)


class ConflictStore:
    def __init__(self, path: str):
        self.path = path

    def load(self) -> List[Conflict]:
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r") as f:
                data = json.load(f)
            return [Conflict.model_validate(item) for item in data.get("conflicts", [])]
        except Exception as e:
            logger.warning("Conflict file %s could not be read (%s); treating it as empty.", self.path, e)
            return []

    def save(self, conflicts: Iterable[Conflict]) -> None:
        payload = {"conflicts": [c.model_dump(mode="json") for c in conflicts]}
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(payload, f, indent=2)

    def get(self, conflict_id: str) -> Optional[Conflict]:
        for c in self.load():
            if c.id == conflict_id or c.id.startswith(conflict_id):
                return c
        return None

    def remove(self, conflict_id: str) -> bool:
        current = self.load()
        remaining = [c for c in current if c.id != conflict_id]
        if len(remaining) == len(current):
            return False
        self.save(remaining)
        return True

    def prune(self, keys: Set[Tuple[str, str, str, str, str]], services: Set[str]) -> List[Conflict]:
        """Drop the stored entries between ``services`` whose rule key is not
        in ``keys`` (see ``rule_conflict_keys``); entries of other service
        pairs stay. Never creates the file. Returns what was dropped."""
        if not os.path.exists(self.path):
            return []
        kept, gone = [], []
        for c in self.load():
            stale = {c.source_service, c.target_service} == set(services) and c.rule_key not in keys
            (gone if stale else kept).append(c)
        if gone:
            self.save(kept)
        return gone

    def replace_for_pairs(self, seen_pairs: Set[Tuple[Tuple[str, Optional[str]], ...]],
                          new_conflicts: List[Conflict]) -> List[Conflict]:
        """Drop every stored entry belonging to a matched pair this run looked
        at, then add this run's conflicts. Entries for pairs outside the run's
        date window are kept. Returns the stored list."""
        kept = [c for c in self.load() if c.pair_key not in seen_pairs]
        merged = kept + list(new_conflicts)
        self.save(merged)
        return merged
