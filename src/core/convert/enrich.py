"""Fill the dives the Convert page reads from a Garmin FIT with what Garmin
Connect holds about them (plans/convert.md I9, decision Q7).

A FIT carries the profile, the tanks and the computer, but not what the
diver typed into Connect afterwards: the site, the buddy, the notes, the
weight, the visibility and the tank sizes. When the dive is in one of the
app's Garmin account caches (``garmin/<account>/data``, `layout.py`), those
fields are filled from the cached dive - only where the file left them
empty, and the tank volume only on a tank without one, pairing the cached
tanks by index. Nothing the FIT holds is overwritten: a tank size the watch
wrote (set on the computer for the SAC) stays the watch's.

The cached dive is found in two ways (`load_cached_dives`):

1. by the activity id the file's name carries (``external_ids['garmin']``,
   which `fit_reader.read_fit` takes from the app's own cache name or from
   the names Connect exports, ``<id>.zip`` / ``<id>_ACTIVITY.fit``);
2. when the name holds no id, or no cache has it (a renamed file, a file
   kept in the diver's own folders), by the **start time**: the cached dive
   that starts within `TIME_MATCH_SECONDS` of the file's dive, on the UTC
   instants when both carry one and on the naive local times otherwise -
   the rule `SyncEngine._start_distance_hours` matches dives by, with a
   tighter window than a sync's grace period, since the file and the cache
   come from the same watch clock. Only the cache files whose name carries
   the dive's date (or a neighbouring day, for the local-vs-UTC edge) are
   opened, so an open never parses a whole cache.

When several accounts hold the dive, an exact id match in any account wins
over a time match; among equals the account the app has selected (the
Garmin dives page's pick, passed in as ``preferred_account``) comes first,
then the accounts in name order.

The module is pure: `enrich_dives` works on the dives it is given and the
lookup `load_cached_dives` built; a cache file that cannot be read is a log
line, never a failure. Dives without a cached match pass through unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from src.core import garmin_files, layout
from src.core.models import UnifiedDive

logger = logging.getLogger("dive_sync.convert.enrich")

# The dive fields filled when the file left them empty, as the detail pane
# names them; the weight and visibility bring their unit along, and the
# tank volume is handled per tank (see `enrich_dive`).
FIELD_WORDS: Tuple[Tuple[str, str], ...] = (
    ("location", "site"), ("buddy", "buddy"), ("notes", "notes"), ("weight", "weight"), ("visibility", "visibility"),
)
TANK_VOLUME_WORD = "tank volume"

# How far apart two start times may be for the file's dive and a cached dive
# to be the same dive (the fallback when no activity id matches).
TIME_MATCH_SECONDS = 120
TIME_MATCH_WORD = "matched by start time"

# The date in the cache's own file name, ``<n>_<YYYY-MM-DD>_<HHMMSS>_<id>.json``
# (`garmin_files.dive_stem`; the date is the dive's local date).
_DATED_NAME = re.compile(r"^[^_]+_(\d{4}-\d{2}-\d{2})_\d{6}_\d+\.json$")


@dataclass(frozen=True)
class CachedDive:
    """One cached Garmin dive, the account folder it was found in, and
    whether it was found by the start time rather than the activity id."""
    account: str
    dive: UnifiedDive
    by_time: bool = False


@dataclass(frozen=True)
class Enrichment:
    """What one dive got from the cache: its index in the list given,
    the account folder, the fields filled (in the order of `FIELD_WORDS`)
    and whether the cached dive was matched by its start time."""
    index: int
    account: str
    fields: Tuple[str, ...]
    by_time: bool = False

    @property
    def summary(self) -> str:
        """``site, buddy, tank volume from the Garmin cache (me@example.org)``,
        with ``, matched by start time`` inside the brackets for a time match."""
        where = f"{self.account}, {TIME_MATCH_WORD}" if self.by_time else self.account
        return f"{', '.join(self.fields)} from the Garmin cache ({where})"


@dataclass
class EnrichResult:
    """`enrich_dives`'s outcome: the dives in the order given (copies where
    something was filled, the given objects where nothing was) and the
    enrichments made."""
    dives: List[UnifiedDive] = field(default_factory=list)
    enrichments: List[Enrichment] = field(default_factory=list)

    def enrichment_of(self, index: int) -> Optional[Enrichment]:
        for e in self.enrichments:
            if e.index == index:
                return e
        return None


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def garmin_id_of(dive: UnifiedDive) -> Optional[str]:
    """The activity id a dive carries, as the string the cache is keyed by."""
    value = (dive.external_ids or {}).get("garmin")
    text = str(value).strip() if value is not None else ""
    return text or None


def time_key(dive: UnifiedDive) -> str:
    """The key `load_cached_dives` files a time match under: the dive's
    start (UTC when it knows it, else local), so the lookup is by the dive
    and not by its place in a list."""
    start = dive.date_time_utc if dive.date_time_utc is not None else dive.date_time
    return "time:" + start.isoformat()


def wanted_ids(dives: Iterable[UnifiedDive]) -> Set[str]:
    """The activity ids worth looking up: those of the dives that carry one."""
    return {gid for gid in (garmin_id_of(d) for d in dives) if gid}


def start_distance_seconds(a: UnifiedDive, b: UnifiedDive) -> float:
    """Seconds between two start times: on the UTC instants when both dives
    carry one, otherwise on the naive local times (the rule of
    `SyncEngine._start_distance_hours`)."""
    if a.date_time_utc is not None and b.date_time_utc is not None:
        return abs((a.date_time_utc - b.date_time_utc).total_seconds())
    return abs((a.date_time - b.date_time).total_seconds())


def candidate_dates(dive: UnifiedDive) -> Set[str]:
    """The dates (``YYYY-MM-DD``) a cache file of this dive can be named by:
    the local and the UTC start date and the day either side, since the
    cache's name holds Garmin's local date and the file's zone may differ."""
    starts = [dive.date_time] + ([dive.date_time_utc] if dive.date_time_utc is not None else [])
    out: Set[str] = set()
    for start in starts:
        for days in (-1, 0, 1):
            out.add((start + timedelta(days=days)).strftime("%Y-%m-%d"))
    return out


def date_of_name(name: str) -> Optional[str]:
    """The date in a cache file's name, None for a file named the old way."""
    m = _DATED_NAME.match(name)
    return m.group(1) if m else None


def _read_payload(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:  # a broken cache entry is a log line, never a failed open
        logger.warning("Garmin cache entry %s could not be read: %s", path, e)
        return None
    return payload if isinstance(payload, dict) else None


def _map_payload(path: str, payload: dict) -> Optional[UnifiedDive]:
    from src.core.services.garmin import GarminAdapter  # lazy: the sync client is not needed to enrich
    try:
        return GarminAdapter.from_cached_payload(payload)
    except Exception as e:
        logger.warning("Garmin cache entry %s could not be mapped: %s", path, e)
        return None


class _Account:
    """One account's cache folder: its file names, listed once, and the
    dives parsed from them, each file read at most once per lookup."""

    def __init__(self, name: str, directory: str):
        self.name = name
        self.directory = directory
        self.names: List[str] = []
        self.by_date: Dict[str, List[str]] = {}
        self._dives: Dict[str, Optional[UnifiedDive]] = {}
        try:
            self.names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
        except OSError as e:
            logger.warning("Garmin cache folder %s could not be listed: %s", directory, e)
        for name in self.names:
            date = date_of_name(name)
            if date:
                self.by_date.setdefault(date, []).append(name)

    def dive(self, name: str) -> Optional[UnifiedDive]:
        if name not in self._dives:
            path = os.path.join(self.directory, name)
            payload = _read_payload(path)
            self._dives[name] = _map_payload(path, payload) if payload is not None else None
        return self._dives[name]

    def payload_id(self, name: str) -> Optional[str]:
        """The activity id inside a file named the old way, None when it has none."""
        payload = _read_payload(os.path.join(self.directory, name))
        if payload is None:
            return None
        value = (payload.get("summary") or {}).get("activityId")
        text = str(value).strip() if value is not None else ""
        if text and name not in self._dives:
            self._dives[name] = _map_payload(os.path.join(self.directory, name), payload)
        return text or None


def _accounts(base_dir: Optional[str], preferred_account: Optional[str]) -> List[_Account]:
    """Every Garmin account cache, the preferred account's first and the
    rest in name order."""
    preferred = layout.account_dir_name(preferred_account) if preferred_account else None
    found = [_Account(name, directory) for name, directory in layout.all_dives_dirs("garmin", base_dir)]
    return sorted(found, key=lambda a: (a.name != preferred, a.name))


def load_cached_dives(dives: Iterable[UnifiedDive], base_dir: Optional[str] = None,
                      preferred_account: Optional[str] = None) -> Dict[str, CachedDive]:
    """The cached Garmin dives of ``dives``, keyed so `enrich_dives` finds
    them again: by the activity id for a dive found through the id it
    carries, by `time_key` for one found through its start time.

    Ids are looked for in every account first (a file named the old way is
    opened only while some id is still missing); the dives still without a
    match are then looked for by start time, among the cache files named by
    the dive's date or a neighbouring day (`candidate_dates`), the closest
    start within `TIME_MATCH_SECONDS` winning. The first account that has a
    dive wins, ``preferred_account`` (a username or folder name) before the
    rest in name order."""
    dives = list(dives)
    found: Dict[str, CachedDive] = {}
    if not dives:
        return found
    accounts = _accounts(base_dir, preferred_account)
    wanted = wanted_ids(dives)

    # pass 1: by activity id, every account before any time match
    for account in accounts:
        if not wanted - set(found):
            break
        unnamed: List[str] = []
        for name in account.names:
            activity_id = garmin_files.activity_id_of(name)
            if activity_id is None:
                unnamed.append(name)
            elif activity_id in wanted and activity_id not in found:
                dive = account.dive(name)
                if dive is not None:
                    found[activity_id] = CachedDive(account=account.name, dive=dive)
        for name in unnamed:
            if wanted <= set(found):
                break
            activity_id = account.payload_id(name)
            if activity_id and activity_id in wanted and activity_id not in found:
                dive = account.dive(name)
                if dive is not None:
                    found[activity_id] = CachedDive(account=account.name, dive=dive)

    # pass 2: by start time, for the dives without an id match
    pending: Dict[str, UnifiedDive] = {}
    for dive in dives:
        gid = garmin_id_of(dive)
        if gid is None or gid not in found:
            pending.setdefault(time_key(dive), dive)
    for account in accounts:
        if not pending:
            break
        for key, dive in list(pending.items()):
            best: Optional[Tuple[float, UnifiedDive]] = None
            for date in sorted(candidate_dates(dive)):
                for name in account.by_date.get(date, []):
                    cached = account.dive(name)
                    if cached is None:
                        continue
                    distance = start_distance_seconds(dive, cached)
                    if distance <= TIME_MATCH_SECONDS and (best is None or distance < best[0]):
                        best = (distance, cached)
            if best is not None:
                found[key] = CachedDive(account=account.name, dive=best[1], by_time=True)
                del pending[key]

    if found:
        by_time = sum(1 for c in found.values() if c.by_time)
        logger.info("Garmin cache: %d of %d dive(s) found (%s)%s.", len(found), len(dives),
                    ", ".join(sorted({c.account for c in found.values()})),
                    f", {by_time} by start time" if by_time else "")
    return found


def enrich_dive(dive: UnifiedDive, cached: UnifiedDive) -> Tuple[UnifiedDive, Tuple[str, ...]]:
    """One dive filled from its cached twin: ``(dive, fields filled)``. The
    given dive is returned untouched when nothing was filled; otherwise a
    deep copy, the original kept as read."""
    filled: List[str] = []
    changes: Dict[str, object] = {}
    for attr, word in FIELD_WORDS:
        value = getattr(cached, attr)
        if _blank(getattr(dive, attr)) and not _blank(value):
            changes[attr] = value
            if attr == "weight":
                changes["weight_unit"] = cached.weight_unit
            elif attr == "visibility":
                changes["visibility_unit"] = cached.visibility_unit
            filled.append(word)
    volumes: Dict[int, float] = {}
    for i, tank in enumerate(dive.gas_mixtures):
        if tank.tank_volume is not None or i >= len(cached.gas_mixtures):
            continue
        volume = cached.gas_mixtures[i].tank_volume
        if volume is not None and volume > 0:
            volumes[i] = float(volume)
    if volumes:
        filled.append(TANK_VOLUME_WORD)
    if not filled:
        return dive, ()
    out = dive.model_copy(deep=True, update=changes)
    for i, volume in volumes.items():
        out.gas_mixtures[i].tank_volume = volume
    return out, tuple(filled)


def cached_for(dive: UnifiedDive, cached: Dict[str, CachedDive]) -> Optional[CachedDive]:
    """The lookup's entry for one dive: by the activity id it carries, else
    by its start time (`time_key`); None without either."""
    gid = garmin_id_of(dive)
    entry = cached.get(gid) if gid else None
    return entry if entry is not None else cached.get(time_key(dive))


def enrich_dives(dives: Sequence[UnifiedDive], cached: Dict[str, CachedDive]) -> EnrichResult:
    """Every dive of ``dives`` with an entry in ``cached`` (`cached_for`)
    filled from it (`enrich_dive`); the others pass through as the same objects."""
    result = EnrichResult()
    for index, dive in enumerate(dives):
        entry = cached_for(dive, cached)
        if entry is None:
            result.dives.append(dive)
            continue
        enriched, fields = enrich_dive(dive, entry.dive)
        result.dives.append(enriched)
        if fields:
            result.enrichments.append(Enrichment(index=index, account=entry.account, fields=fields, by_time=entry.by_time))
    return result


def enrich_from_cache(dives: Sequence[UnifiedDive], base_dir: Optional[str] = None,
                      preferred_account: Optional[str] = None) -> EnrichResult:
    """`load_cached_dives` for the dives, then `enrich_dives`."""
    return enrich_dives(dives, load_cached_dives(dives, base_dir, preferred_account))
