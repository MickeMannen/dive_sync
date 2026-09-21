"""Submersion peer-device adapter (rework.md F12).

dive_sync joins the shared sync store as one more Submersion device: it reads
every device's base and changesets, merges them by HLC into an in-memory
``Library`` (F11), maps that to ``UnifiedDive``, and publishes its own
writes as this device's base under its own ``deviceId`` and HLC clock. This
version publishes a full base each time it writes (simple and correct for a
few-MB library); incremental changesets are a later optimisation.

Storage is an S3 bucket or a local folder behind ``SyncStore``. End-to-end
encrypted stores are refused with a clear message. On start-up the adapter
checks for its own ``.retired.json`` and re-joins with a fresh device id.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.core.adapter import BaseDiveAdapter
from src.core.config import SubmersionCredentials
from src.core.fields import FieldSpec
from src.core.models import UnifiedDive
from src.core.services.submersion import codec, store as st
from src.core.services.submersion.hlc import HlcClock, hlc_key
from src.core.services.submersion.library import SERVICE_ID, Library, dive_to_unified

logger = logging.getLogger("dive_sync.submersion")

DEVICE_NAME = "dive_sync"


def _now_ms() -> int:
    return int(time.time() * 1000)


class SubmersionAdapter(BaseDiveAdapter):
    """One configured Submersion store (one sync "pair")."""

    service_id = SERVICE_ID
    display_name = "Submersion"
    stores_external_ids = True

    @classmethod
    def field_catalog(cls) -> List[FieldSpec]:
        return [
            FieldSpec(key="submersion.date_time", label="Start time", type="datetime", unified="date_time", writable=False),
            FieldSpec(key="submersion.duration", label="Duration", type="number", unified="duration", unit="s"),
            FieldSpec(key="submersion.max_depth", label="Max depth", type="number", unified="max_depth", unit="m"),
            FieldSpec(key="submersion.avg_depth", label="Average depth", type="number", unified="avg_depth", unit="m"),
            FieldSpec(key="submersion.temp_min", label="Water temperature", type="number", unified="temp_min", unit="°C"),
            FieldSpec(key="submersion.dive_number", label="Dive number", type="number", unified="dive_number"),
            FieldSpec(key="submersion.location", label="Dive site", type="text", unified="location"),
            FieldSpec(key="submersion.notes", label="Notes", type="text", unified="notes"),
            FieldSpec(key="submersion.buddy", label="Buddy", type="list", unified="buddy"),
            FieldSpec(key="submersion.weight", label="Weight", type="number", unified="weight"),
            FieldSpec(key="submersion.visibility", label="Visibility", type="number", unified="visibility"),
            FieldSpec(key="submersion.gps", label="Site position", type="gps", unified="gps"),
            FieldSpec(key="submersion.tanks", label="Cylinders", type="tanks", unified="tanks"),
            FieldSpec(key="submersion.samples", label="Dive profile", type="samples", unified="samples"),
        ]

    def __init__(self, config: SubmersionCredentials, device_state_dir: Optional[str] = None,
                 device_id: Optional[str] = None, site_match_radius_m: float = 200.0):
        self.config = config
        self.site_match_radius_m = site_match_radius_m
        self.store = st.open_store(config)
        state_dir = device_state_dir or os.path.join(os.environ.get("DATA_DIR", "."), "submersion")
        self._identity_path = os.path.join(state_dir, "device.json")
        self.device_id = device_id or self._load_or_make_identity()
        self.clock = HlcClock(self.device_id, os.path.join(state_dir, f"hlc_{self.device_id}.json"))
        self.library = Library()
        self._data_sources_by_dive: Dict[str, List[Dict[str, Any]]] = {}
        self._applied_peer_hlc: Dict[str, str] = {}
        self._epoch_id: Optional[str] = None
        # writes staged this run; add/update/delete mutate the library and record here
        self._dirty = False
        self._loaded = False

    # -- identity ---------------------------------------------------------

    def _load_or_make_identity(self) -> str:
        import json
        if os.path.exists(self._identity_path):
            try:
                with open(self._identity_path) as f:
                    return json.load(f)["device_id"]
            except Exception:
                pass
        device_id = str(uuid.uuid4())
        os.makedirs(os.path.dirname(self._identity_path) or ".", exist_ok=True)
        with open(self._identity_path, "w") as f:
            json.dump({"device_id": device_id}, f)
        return device_id

    def _reset_identity(self) -> None:
        import json
        self.device_id = str(uuid.uuid4())
        with open(self._identity_path, "w") as f:
            json.dump({"device_id": self.device_id}, f)
        self.clock = HlcClock(self.device_id, os.path.join(os.path.dirname(self._identity_path), f"hlc_{self.device_id}.json"))

    # -- reading ----------------------------------------------------------

    def login(self) -> bool:
        try:
            ok, message = st.check_store_access(self.config)
        except Exception as e:
            logger.error("Submersion store not reachable: %s", e)
            return False
        if not ok:
            logger.error("Submersion store check failed: %s", message)
            return False
        return True

    def _load_library(self) -> None:
        self.library = Library()
        self._data_sources_by_dive = {}
        self._applied_peer_hlc = {}
        devices = st.list_devices(self.store)
        # If a peer retired our device, re-join under a new id with a fresh base.
        if self.device_id in devices and devices[self.device_id]["retired"]:
            logger.warning("Submersion: this device was retired by a peer; re-joining with a new device id.")
            self._reset_identity()
        self._epoch_id = st.read_epoch(self.store)
        for device_id, entry in devices.items():
            peer = st.read_peer(self.store, device_id, entry)
            if peer is None:
                continue
            for payload in peer["payloads"]:
                if self._epoch_id and payload.get("epochId") and payload["epochId"] != self._epoch_id:
                    continue
                self.library.apply_payload(payload)
                # collect diveDataSources for garmin-id resolution
                for source in payload.get("data", {}).get("diveDataSources", []) or []:
                    self._data_sources_by_dive.setdefault(source.get("diveId"), []).append(source)
                self.clock.observe(payload.get("toHlc"))
            if device_id != self.device_id:
                self._applied_peer_hlc[device_id] = peer["manifest"].get("publishedHlcHigh") or ""
        self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load_library()

    def fetch_dives(self, date_from: Optional[datetime] = None, date_to: Optional[datetime] = None) -> List[UnifiedDive]:
        self._load_library()   # always fresh, like the file adapters
        out = []
        for dive in self.library.dives():
            unified = dive_to_unified(self.library, dive, self._data_sources_by_dive)
            if unified.date_time is None:
                continue
            if date_from and unified.date_time < date_from:
                continue
            if date_to and unified.date_time > date_to:
                continue
            out.append(unified)
        return out

    # -- writing ----------------------------------------------------------

    def _site_for(self, unified: UnifiedDive) -> Optional[str]:
        """Resolve or create a dive_sites row; returns its id."""
        from src.core.site_matcher import find_site
        if not unified.location and unified.lat is None:
            return None
        sites = list(self.library._alive("diveSites").values())
        hit = find_site(sites, unified.location, unified.lat, unified.lng,
                        get_name=lambda s: s.get("name"), get_gps=lambda s: (s.get("latitude"), s.get("longitude")),
                        radius_m=self.site_match_radius_m)
        if hit is not None:
            return hit["id"]
        site_id = str(uuid.uuid4())
        self._put_row("diveSites", {
            "id": site_id, "diverId": self.library.default_diver_id(),
            "name": unified.location or f"{unified.lat:.6f}, {unified.lng:.6f}",
            "latitude": unified.lat, "longitude": unified.lng, "isShared": False,
        })
        return site_id

    def _put_row(self, table: str, row: Dict[str, Any]) -> Dict[str, Any]:
        now = _now_ms()
        row.setdefault("createdAt", now)
        row["updatedAt"] = now
        row["hlc"] = str(self.clock.tick())
        self.library.tables[table][row["id"]] = row
        self._dirty = True
        return row

    def _write_dive_rows(self, unified: UnifiedDive, dive_id: str, existing: Optional[Dict[str, Any]]) -> None:
        site_id = self._site_for(unified)
        diver_id = (existing or {}).get("diverId") or self.library.default_diver_id()
        row = dict(existing or {})
        row.update({
            "id": dive_id,
            "diverId": diver_id,
            "diveNumber": unified.dive_number,
            "diveDateTime": int(unified.date_time.replace(tzinfo=timezone.utc).timestamp() * 1000) if unified.date_time else None,
            "runtime": unified.duration or None,
            "bottomTime": row.get("bottomTime") or (unified.duration or None),
            "maxDepth": unified.max_depth or None,
            "avgDepth": unified.avg_depth,
            "waterTemp": unified.temp_min,
            "notes": unified.notes or "",
            "siteId": site_id,
            "diveType": row.get("diveType") or "recreational",
            "diveMode": row.get("diveMode") or "oc",
        })
        if unified.weight is not None:
            kg = unified.weight / 2.20462 if (unified.weight_unit or "").lower().startswith("p") else unified.weight
            row["weightAmount"] = round(kg, 3)
        if unified.visibility is not None:
            row["visibilityMeters"] = unified.visibility
        garmin_id = unified.external_ids.get("garmin")
        if garmin_id:
            row["importSource"] = "garmin"
            row["importId"] = str(garmin_id)
        self._put_row("dives", row)

        # Submersion shows a dive's provenance from a diveDataSources row, not
        # from importSource/importId. Give a dive_sync-created Garmin dive one
        # (prefix "garmin-" is what marks the provider) so it presents as a
        # Garmin dive; never add a second one to a dive that already has a
        # Garmin source (the matched dives keep their original FIT source).
        if garmin_id and not self._has_garmin_source(dive_id):
            self._put_row("diveDataSources", {
                "id": str(uuid.uuid4()), "diveId": dive_id,
                "sourceUuid": f"garmin-connect-{garmin_id}",
                "sourceFormat": "garmin_connect",
                "isPrimary": not self._has_any_source(dive_id),
            })

        # Replace tanks, but keep an existing tank's role/name when the
        # incoming UnifiedDive doesn't carry one for that slot (a source
        # service with no role concept, e.g. Divelogs or Garmin, must not
        # blank out a role Submersion (or a peer) already recorded).
        existing_tanks = self.library.children("diveTanks", dive_id)
        for tank in existing_tanks:
            self._delete_row("diveTanks", tank["id"])
        for order, gas in enumerate(unified.gas_mixtures):
            previous = existing_tanks[order] if order < len(existing_tanks) else {}
            role = gas.tank_role or previous.get("tankRole") or ("backGas" if order == 0 else None)
            name = gas.tank_name if gas.tank_name is not None else previous.get("tankName")
            self._put_row("diveTanks", {
                "id": str(uuid.uuid4()), "diveId": dive_id, "tankOrder": order,
                "volume": gas.tank_volume, "startPressure": gas.start_pressure, "endPressure": gas.end_pressure,
                "o2Percent": gas.oxygen, "hePercent": gas.helium or 0.0,
                "tankRole": role, "tankName": name,
            })

        # replace buddies
        for link in self.library.children("diveBuddies", dive_id):
            self._delete_row("diveBuddies", link["id"])
        for name in [b.strip() for b in (unified.buddy or "").split(",") if b.strip()]:
            buddy_id = self._buddy_id(name)
            self._put_row("diveBuddies", {"id": str(uuid.uuid4()), "diveId": dive_id, "buddyId": buddy_id, "role": "buddy"})

        # replace the primary profile series
        for series in self.library.children("diveProfileSeries", dive_id):
            self._delete_row("diveProfileSeries", series["id"])
        if unified.samples:
            import base64
            samples = [(int(s.time or 0), s.depth, s.temp) for s in unified.samples]
            blob = base64.b64encode(codec.encode_profile(samples)).decode("ascii")
            summary = codec.profile_summary(samples)
            self._put_row("diveProfileSeries", dict(summary, id=str(uuid.uuid4()), diveId=dive_id, isPrimary=True,
                                                    samples=blob))

    def _has_garmin_source(self, dive_id: str) -> bool:
        return any((r.get("sourceUuid") or "").startswith("garmin-")
                   for r in self.library.children("diveDataSources", dive_id))

    def _has_any_source(self, dive_id: str) -> bool:
        return bool(self.library.children("diveDataSources", dive_id))

    def _buddy_id(self, name: str) -> str:
        for rid, row in self.library._alive("buddies").items():
            if (row.get("name") or "").lower() == name.lower():
                return rid
        buddy_id = str(uuid.uuid4())
        self._put_row("buddies", {"id": buddy_id, "name": name})
        return buddy_id

    # Submersion groups payload deletions by table; keep the table with each.
    _SUBMERSION_TABLE = {"dives": "dives", "diveSites": "dive_sites", "diveTanks": "dive_tanks",
                         "buddies": "buddies", "diveBuddies": "dive_buddies", "tags": "tags", "diveTags": "dive_tags",
                         "diveProfileSeries": "dive_profile_series", "tankPressureSeries": "tank_pressure_series", "diveDataSources": "dive_data_sources"}

    def _delete_row(self, table: str, row_id: str) -> None:
        self.library.tables[table].pop(row_id, None)
        self.library.tombstones[row_id] = {"id": row_id, "entityType": self._SUBMERSION_TABLE.get(table, table),
                                           "deletedAt": _now_ms(), "hlc": str(self.clock.tick())}
        self._dirty = True

    def add_dive(self, dive: UnifiedDive) -> Optional[str]:
        self._ensure_loaded()
        dive_id = str(uuid.uuid4())
        self._write_dive_rows(dive, dive_id, None)
        logger.info("Submersion: staged new dive %s", dive_id)
        return dive_id

    def update_dive(self, external_id: str, dive: UnifiedDive) -> bool:
        self._ensure_loaded()
        existing = self.library.tables["dives"].get(external_id)
        if existing is None:
            logger.error("Submersion: no dive %s to update", external_id)
            return False
        self._write_dive_rows(dive, external_id, existing)
        logger.info("Submersion: staged update of dive %s", external_id)
        return True

    def delete_dive(self, external_id: str) -> bool:
        self._ensure_loaded()
        if external_id not in self.library.tables["dives"]:
            return False
        for table in ("diveTanks", "diveBuddies", "diveTags", "diveProfileSeries", "tankPressureSeries", "diveDataSources"):
            for child in self.library.children(table, external_id):
                self._delete_row(table, child["id"])
        self._delete_row("dives", external_id)
        logger.info("Submersion: staged delete of dive %s", external_id)
        return True

    def finish(self) -> None:
        """Publish this device's base (all rows it now owns) and manifest, once
        per run that wrote anything (called by SyncEngine after a non-dry run)."""
        if not self._dirty:
            return
        data = {name: list(rows.values()) for name, rows in self.library.tables.items() if rows}
        deletions: Dict[str, List[Dict[str, Any]]] = {}
        for record in self.library.tombstones.values():
            deletions.setdefault(record.get("entityType") or "rows", []).append(record)
        base_seq = 1
        to_hlc = str(self.clock.current)
        payload = st.build_payload(self.device_id, data, deletions, seq=base_seq, to_hlc=to_hlc,
                                   since_hlc=None, epoch_id=self._epoch_id, now_ms=_now_ms(), base_seq=None)
        st.publish_base(self.store, self.device_id, DEVICE_NAME, payload, self._applied_peer_hlc,
                        self.config.store_type, _now_ms())
        logger.info("Submersion: published base seq %d for device %s", base_seq, self.device_id)
        self._dirty = False
