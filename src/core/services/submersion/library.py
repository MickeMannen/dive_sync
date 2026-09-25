"""Merge a set of Submersion payloads (bases + changesets from every device)
into one in-memory library view, and map its dives to ``UnifiedDive``
(rework.md F11/F12).

Merge rule (format section 4): per row the highest HLC wins, tombstones win
over older edits, a child whose parent dive is gone is dropped. dive_sync
does not itself keep pending local edits, so the "unpublished local edit is
not overwritten" case does not arise here.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.core.models import GasMixture, UnifiedDive, UnifiedSample
from src.core.services.submersion import codec
from src.core.services.submersion.hlc import hlc_key

SERVICE_ID = "submersion"
# Tables dive_sync reads or writes; other tables in a payload are ignored.
TABLES = ("divers", "dives", "diveSites", "diveTanks", "buddies", "diveBuddies", "tags", "diveTags",
          "diveProfileSeries", "tankPressureSeries", "diveDataSources")


def _row_key(row: Dict[str, Any]) -> Tuple[int, int, str]:
    return hlc_key(row.get("hlc")) if row.get("hlc") else (0, int(row.get("updatedAt") or 0), "")


class Library:
    """The merged current state: one dict per table keyed by row id, plus the
    set of tombstoned ids."""

    def __init__(self):
        self.tables: Dict[str, Dict[str, Dict[str, Any]]] = {name: {} for name in TABLES}
        self.tombstones: Dict[str, Dict[str, Any]] = {}   # id -> deletion record

    def apply_payload(self, payload: Dict[str, Any]) -> None:
        data = payload.get("data") or {}
        for name in TABLES:
            bucket = self.tables[name]
            for row in data.get(name, []) or []:
                rid = row.get("id")
                if rid is None:
                    continue
                existing = bucket.get(rid)
                if existing is None or _row_key(row) >= _row_key(existing):
                    bucket[rid] = row
        for table, records in (payload.get("deletions") or {}).items():
            for record in records or []:
                rid = record.get("id") or record.get("recordId")
                if rid is None:
                    continue
                prior = self.tombstones.get(rid)
                if prior is None or hlc_key(record.get("hlc")) >= hlc_key(prior.get("hlc")):
                    # keep the source table so we can republish deletions in
                    # Submersion's per-table shape
                    self.tombstones[rid] = dict(record, entityType=record.get("entityType") or table)

    def _alive(self, name: str) -> Dict[str, Dict[str, Any]]:
        out = {}
        for rid, row in self.tables[name].items():
            tomb = self.tombstones.get(rid)
            if tomb and hlc_key(tomb.get("hlc")) >= _row_key(row):
                continue
            out[rid] = row
        return out

    def default_diver_id(self) -> Optional[str]:
        divers = self._alive("divers")
        for rid, row in divers.items():
            if row.get("isDefault"):
                return rid
        return next(iter(divers), None)

    def dives(self) -> List[Dict[str, Any]]:
        dives = list(self._alive("dives").values())
        dives.sort(key=lambda d: d.get("diveDateTime") or 0)
        return dives

    def children(self, table: str, dive_id: str) -> List[Dict[str, Any]]:
        rows = [r for r in self._alive(table).values() if r.get("diveId") == dive_id]
        rows.sort(key=lambda r: (r.get("tankOrder") if r.get("tankOrder") is not None else 0, r.get("createdAt") or 0))
        return rows


# ---------------------------------------------------------------------------
# Mapping to UnifiedDive
# ---------------------------------------------------------------------------

def _ms_to_dt(ms: Optional[int]):
    from datetime import datetime, timezone
    if not ms:
        return None
    # UnifiedDive.date_time is timezone-naive UTC (matches how Garmin/Divelogs are stored)
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).replace(tzinfo=None)


def garmin_id_of(dive: Dict[str, Any], data_sources: Iterable[Dict[str, Any]]) -> Optional[str]:
    """Garmin activity id: from importSource/importId when set, else parsed
    from a diveDataSources ``sourceUuid`` of the form ``garmin-<serial>-<ms>``
    (what Submersion's own FIT import writes; see F9)."""
    if (dive.get("importSource") or "").lower() == "garmin" and dive.get("importId"):
        return str(dive["importId"])
    for source in data_sources:
        uuid = source.get("sourceUuid") or ""
        if uuid.startswith("garmin-"):
            parts = uuid.split("-")
            if len(parts) >= 3 and parts[-1].isdigit():
                return None  # a Submersion FIT import stores the epoch-ms, not the Garmin activity id
    return None



# ---------------------------------------------------------------------------
# Extra fields (2026-09-23, from the owner's pass over
# docs/submersion_field_inventory.md)
# ---------------------------------------------------------------------------
# Submersion stores far more per dive and per site than UnifiedDive models.
# Everything here has no UnifiedDive attribute, so it travels in
# ``service_fields`` under the name in the first column, exactly as Garmin's
# activityName and Divelogs' divesite do. Declared once and consumed three
# times: the field catalogue, the read in dive_to_unified, and the write in
# SubmersionAdapter._write_dive_rows.
#
#   (service_fields name, row key, board label, type, unit)
#
# ``site_*`` entries live on the dive's diveSites row, not the dive row. The
# site's own name and coordinates are not here: they already travel as the
# unified ``location`` and ``gps`` fields.
DIVE_EXTRA_FIELDS = [
    ("dive_name", "name", "Dive name", "text", None),
    ("bottomTime", "bottomTime", "Bottom time", "number", "s"),
    ("entryTime", "entryTime", "Entry time", "datetime", None),
    ("exitTime", "exitTime", "Exit time", "datetime", None),
    ("diveType", "diveType", "Dive type", "text", None),
    ("diveMode", "diveMode", "Dive mode", "text", None),
    ("waterType", "waterType", "Water type", "text", None),
    ("altitude", "altitude", "Altitude", "number", "m"),
    ("airTemp", "airTemp", "Air temperature", "number", "°C"),
    ("entryMethod", "entryMethod", "Entry method", "text", None),
    ("exitLatitude", "exitLatitude", "Exit latitude", "number", None),
    ("exitLongitude", "exitLongitude", "Exit longitude", "number", None),
    ("boatName", "boatName", "Boat name", "text", None),
    ("diveOperator", "diveOperator", "Dive operator", "text", None),
]
SITE_EXTRA_FIELDS = [
    ("site_region", "region", "Site region", "text", None),
    ("site_country", "country", "Site country", "text", None),
    ("site_island", "island", "Site island", "text", None),
    ("site_city", "city", "Site city", "text", None),
    ("site_notes", "notes", "Site notes", "text", None),
    ("site_waterType", "waterType", "Site water type", "text", None),
    ("site_entryMethod", "entryMethod", "Site entry method", "text", None),
    ("site_exitMethod", "exitMethod", "Site exit method", "text", None),
]
EXTRA_FIELDS = DIVE_EXTRA_FIELDS + SITE_EXTRA_FIELDS


def _extra_value(row: Dict[str, Any], row_key: str, field_type: str) -> Any:
    value = row.get(row_key)
    if field_type == "datetime":
        return _ms_to_dt(value)
    if field_type == "text":
        return value or None
    return value


def read_extra_fields(dive: Dict[str, Any], site: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The ``service_fields`` a dive contributes beyond the unified ones."""
    out: Dict[str, Any] = {}
    for name, row_key, _label, field_type, _unit in DIVE_EXTRA_FIELDS:
        out[name] = _extra_value(dive, row_key, field_type)
    for name, row_key, _label, field_type, _unit in SITE_EXTRA_FIELDS:
        out[name] = _extra_value(site or {}, row_key, field_type)
    return out


def dive_to_unified(library: Library, dive: Dict[str, Any], data_sources_by_dive: Dict[str, List[Dict[str, Any]]]) -> UnifiedDive:
    dive_id = dive["id"]
    site = library._alive("diveSites").get(dive.get("siteId") or "")
    tanks = [
        GasMixture(
            oxygen=t.get("o2Percent") if t.get("o2Percent") is not None else 21.0,
            helium=t.get("hePercent") or 0.0,
            start_pressure=t.get("startPressure"),
            end_pressure=t.get("endPressure"),
            tank_volume=t.get("volume"),
            tank_name=t.get("tankName") or t.get("presetName"),
            tank_role=t.get("tankRole"),
        )
        for t in library.children("diveTanks", dive_id)
    ]
    buddy_names = []
    buddies = library._alive("buddies")
    for link in library.children("diveBuddies", dive_id):
        b = buddies.get(link.get("buddyId") or "")
        if b and b.get("name"):
            buddy_names.append(b["name"])
    samples: List[UnifiedSample] = []
    for series in library.children("diveProfileSeries", dive_id):
        if not series.get("isPrimary", True):
            continue
        blob = series.get("samples")
        if blob:
            import base64
            for time_s, depth, temp in codec.decode_profile(base64.b64decode(blob)):
                if depth is not None:
                    samples.append(UnifiedSample(depth=depth, temp=temp, time=time_s))
        break
    external_ids = {SERVICE_ID: dive_id}
    garmin_id = garmin_id_of(dive, data_sources_by_dive.get(dive_id, []))
    if garmin_id:
        external_ids["garmin"] = garmin_id
    duration = dive.get("runtime") or dive.get("bottomTime") or 0
    return UnifiedDive(
        date_time=_ms_to_dt(dive.get("diveDateTime")) or _ms_to_dt(dive.get("entryTime")),
        duration=int(duration),
        max_depth=dive.get("maxDepth") or (max((s.depth for s in samples), default=0.0)),
        avg_depth=dive.get("avgDepth"),
        temp_min=dive.get("waterTemp"),
        external_ids=external_ids,
        gas_mixtures=tanks,
        location=site.get("name") if site else dive.get("name"),
        notes=dive.get("notes") or None,
        dive_number=dive.get("diveNumber"),
        weight=dive.get("weightAmount"),
        weight_unit="kilogram" if dive.get("weightAmount") is not None else None,
        visibility=dive.get("visibilityMeters"),
        visibility_unit="meter" if dive.get("visibilityMeters") is not None else None,
        buddy=", ".join(buddy_names) or dive.get("buddy") or None,
        lat=(site.get("latitude") if site else None) if site and site.get("latitude") is not None else dive.get("entryLatitude"),
        lng=(site.get("longitude") if site else None) if site and site.get("longitude") is not None else dive.get("entryLongitude"),
        samples=samples,
        service_fields=read_extra_fields(dive, site),
    )
