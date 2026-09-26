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


def pair_key(dive_ids: Dict[str, Optional[str]]) -> Tuple[Tuple[str, Optional[str]], ...]:
    return tuple(sorted(dive_ids.items()))


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

    def replace_for_pairs(self, seen_pairs: Set[Tuple[Tuple[str, Optional[str]], ...]],
                          new_conflicts: List[Conflict]) -> List[Conflict]:
        """Drop every stored entry belonging to a matched pair this run looked
        at, then add this run's conflicts. Entries for pairs outside the run's
        date window are kept. Returns the stored list."""
        kept = [c for c in self.load() if c.pair_key not in seen_pairs]
        merged = kept + list(new_conflicts)
        self.save(merged)
        return merged
