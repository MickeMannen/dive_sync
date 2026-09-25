"""Submersion peer-device adapter (rework.md F12).

dive_sync joins the shared sync store as one more Submersion device: it reads
every device's base and changesets, merges them by HLC into an in-memory
``Library`` (F11), maps that to ``UnifiedDive``, and publishes its own
writes as this device's base under its own ``deviceId`` and HLC clock. This
version publishes a full base each time it writes (simple and correct for a
few-MB library); incremental changesets are a later optimisation.

Storage is an S3 bucket or a local folder behind ``SyncStore``. An end-to-end
encrypted store (rework.md E11) is unlocked in ``login()`` from the cloud
keyslot file and the passphrase in ``SubmersionCredentials.passphrase``; with
no passphrase configured (or a wrong one), login fails with a clear message
rather than reading garbage. On start-up the adapter checks for its own
``.retired.json`` and re-joins with a fresh device id.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.core.adapter import BaseDiveAdapter
from src.core.config import SubmersionCredentials
from src.core.fields import FieldSpec
from src.core.models import UnifiedDive
from src.core.services.submersion import codec, crypto, store as st
from src.core.services.submersion.hlc import HlcClock, hlc_key
from src.core.services.submersion.library import EXTRA_FIELDS, DIVE_EXTRA_FIELDS, SITE_EXTRA_FIELDS, SERVICE_ID, Library, dive_to_unified

logger = logging.getLogger("dive_sync.submersion")

DEVICE_NAME = "dive_sync"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _buddy_names(value: Any) -> List[str]:
    """The buddy names to write as diveBuddies rows.

    UnifiedDive.buddy is the comma-joined string every service uses, but a
    mapping board can hand over a real list (a list-typed source field, or a
    board saved while this adapter still declared its own buddy field as a
    list), so both are accepted rather than crashing on whichever arrives."""
    if value is None:
        return []
    parts = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return [str(part).strip() for part in parts if str(part).strip()]


class SubmersionAdapter(BaseDiveAdapter):
    """One configured Submersion store (one sync "pair")."""

    service_id = SERVICE_ID
    display_name = "Submersion"
    stores_external_ids = True

    @classmethod
    def field_catalog(cls) -> List[FieldSpec]:
        return [
            # Writable since 2026-09-23 (rework.md G9): the owner needs to enter
            # historical dives that no computer ever recorded, and a start time
            # is the one thing such a dive cannot be without. _write_dive_rows
            # has always set diveDateTime on creation; this only lets a rule
            # correct it on a dive that is already matched.
            FieldSpec(key="submersion.date_time", label="Start time", type="datetime", unified="date_time"),
            FieldSpec(key="submersion.duration", label="Duration", type="number", unified="duration", unit="s"),
            FieldSpec(key="submersion.max_depth", label="Max depth", type="number", unified="max_depth", unit="m"),
            FieldSpec(key="submersion.avg_depth", label="Average depth", type="number", unified="avg_depth", unit="m"),
            FieldSpec(key="submersion.temp_min", label="Water temperature", type="number", unified="temp_min", unit="°C"),
            FieldSpec(key="submersion.dive_number", label="Dive number", type="number", unified="dive_number"),
            FieldSpec(key="submersion.location", label="Dive site", type="text", unified="location"),
            FieldSpec(key="submersion.notes", label="Notes", type="text", unified="notes"),
            # Submersion stores one diveBuddies row per buddy, but the value
            # this adapter reads and writes is the comma-joined string
            # UnifiedDive.buddy holds (dive_to_unified joins the names,
            # _write_dive_rows splits them again). Declaring it a list made
            # the link engine convert across the two types, which broke both
            # directions: a text source arrived as ["Guy"] and crashed the
            # split, and reading it back joined the string character by
            # character ("Guy" -> "G, u, y").
            FieldSpec(key="submersion.buddy", label="Buddy", type="text", unified="buddy"),
            FieldSpec(key="submersion.weight", label="Weight", type="number", unified="weight"),
            FieldSpec(key="submersion.visibility", label="Visibility", type="number", unified="visibility"),
            FieldSpec(key="submersion.gps", label="Site position", type="gps", unified="gps"),
            # Read-only since 2026-09-24 (rework.md F17): both are derived by
            # Submersion's own FIT import from the file the diver hands it, and
            # a rule that overwrote them destroyed coordinated rows (the tank
            # pressure series and gas switches hanging off the same data
            # source) and re-encoded the app's profile through a codec that
            # knows 3 of its 24 columns. dive_sync still *reads* both, so a
            # Submersion dive can feed a profile or gas to another service.
            FieldSpec(key="submersion.tanks", label="Cylinders", type="tanks", unified="tanks", writable=False),
            FieldSpec(key="submersion.samples", label="Dive profile", type="samples", unified="samples", writable=False),
        ] + [
            # Everything Submersion stores that UnifiedDive has no attribute
            # for, declared once in library.EXTRA_FIELDS and carried in
            # service_fields (2026-09-23, rework.md G9). The site_* ones live
            # on the dive's diveSites row: 'site_region' and friends are what
            # finally give Divelogs' own location field something to sync
            # with, which the site name alone never could.
            FieldSpec(key=f"submersion.{name}", label=label, type=field_type, unit=unit)
            for name, _row_key, label, field_type, unit in EXTRA_FIELDS
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
        try:
            ok, message = self._resolve_encryption()
        except Exception as e:
            logger.error("Submersion encryption check failed: %s", e)
            return False
        if not ok:
            logger.error("Submersion: %s", message)
            return False
        if message:
            logger.info("Submersion: %s", message)
        return True

    def _resolve_encryption(self) -> Tuple[bool, str]:
        """Fetch the cloud keyslot file, if any, and unlock it with the
        configured passphrase (rework.md E11). Sets ``self.store.encryption``
        so every read/write of an ``ssv1.*`` file seals/opens correctly.
        Returns ``(ok, message)``; an empty message on success means "nothing
        to do" (the store isn't encrypted) so ``login`` doesn't log noise on
        every plaintext-store run."""
        if not self.store.exists(crypto.KeyslotFile.CLOUD_FILE_NAME):
            self.store.encryption = None
            return True, ""
        try:
            keyslot_file = crypto.KeyslotFile.from_json_bytes(self.store.get(crypto.KeyslotFile.CLOUD_FILE_NAME))
        except Exception as e:
            return False, f"the keyslot file ({crypto.KeyslotFile.CLOUD_FILE_NAME}) is unreadable: {e}"
        if not self.config.passphrase:
            return False, ("this store is end-to-end encrypted; set a passphrase in its credentials to sync with it "
                           "(Submersion's own Settings > Sync > End-to-end encryption screen shows it once, when "
                           "first enabled)")
        mlk = crypto.try_unwrap(keyslot_file, self.config.passphrase)
        if mlk is None:
            return False, "the configured passphrase does not unlock this store's encrypted library"
        self.store.encryption = st.SubmersionEncryption(
            data_key=crypto.derive_data_key(mlk), library_key_id=keyslot_file.library_key_id)
        return True, f"unlocked encrypted library {keyslot_file.library_key_id}"

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

    @staticmethod
    def _apply_extras(row: Dict[str, Any], unified: UnifiedDive, fields) -> bool:
        """Copy service_fields values onto a stored row. Returns True when
        anything changed. A key the dive does not carry at all is left alone;
        an explicit None does clear the stored value, so a rule can empty a
        field on purpose."""
        changed = False
        for name, row_key, _label, field_type, _unit in fields:
            if name not in unified.service_fields:
                continue
            value = unified.service_fields[name]
            if field_type == "datetime" and isinstance(value, datetime):
                value = int(value.replace(tzinfo=timezone.utc).timestamp() * 1000)
            if row.get(row_key) != value:
                row[row_key] = value
                changed = True
        return changed

    def _write_site_extras(self, site_id: Optional[str], unified: UnifiedDive) -> None:
        """Site-level rules (site_region, site_country, ...) write to the
        dive's diveSites row, which is shared by every dive at that site."""
        if not site_id:
            return
        site = self.library._alive("diveSites").get(site_id)
        if site is None:
            return
        row = dict(site)
        if self._apply_extras(row, unified, SITE_EXTRA_FIELDS):
            self._put_row("diveSites", row)

    def _put_row(self, table: str, row: Dict[str, Any]) -> Dict[str, Any]:
        now = _now_ms()
        row.setdefault("createdAt", now)
        row["updatedAt"] = now
        row["hlc"] = str(self.clock.tick())
        self.library.tables[table][row["id"]] = row
        self._dirty = True
        return row

    def _write_dive_rows(self, unified: UnifiedDive, dive_id: str, existing: Optional[Dict[str, Any]]) -> None:
        """Write one dive.

        **A dive that already exists here gets its metadata only** (rework.md
        F17, decided 2026-09-24): the diver imports each device-logged dive's
        ``.fit`` in the Submersion app itself, and that import - not dive_sync
        - owns the depth profile, the tank rows, the tank-pressure series, the
        gas switches and the ``diveDataSources`` row they all hang off. Writing
        those from Garmin's API replaced coordinated rows the app had derived
        from the file, and re-encoded its profile through a codec that only
        knows 3 of the series' 24 columns, so it lost data every run. A dive
        *created* here is a different case - a hand-logged dive no FIT import
        will ever arrive for - and still gets the tanks and profile it came
        with, once, at creation.
        """
        creating = existing is None
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
            # A dive whose incoming copy carries neither a site name nor GPS
            # keeps the site it already has: no sender can name one, so this
            # would only ever be a blanking, never a correction.
            "siteId": site_id or row.get("siteId"),
            "diveType": row.get("diveType") or "recreational",
            "diveMode": row.get("diveMode") or "oc",
        })
        # The extras a rule may have written into service_fields. Only keys
        # the incoming dive actually carries are applied, so a sender with no
        # concept of (say) a boat name never blanks one Submersion already has.
        self._apply_extras(row, unified, DIVE_EXTRA_FIELDS)
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
        self._write_site_extras(site_id, unified)

        # replace buddies
        for link in self.library.children("diveBuddies", dive_id):
            self._delete_row("diveBuddies", link["id"])
        for name in _buddy_names(unified.buddy):
            buddy_id = self._buddy_id(name)
            self._put_row("diveBuddies", {"id": str(uuid.uuid4()), "diveId": dive_id, "buddyId": buddy_id, "role": "buddy"})

        if not creating:
            # Everything below belongs to the app's own FIT import; see the
            # docstring. A matched dive is metadata only.
            return

        # The tanks the new dive came with. Nothing to preserve here (the dive
        # is new), and no role to inherit - a sender without a role concept
        # (Garmin, Divelogs) leaves the first cylinder as back gas.
        for order, gas in enumerate(unified.gas_mixtures):
            self._put_row("diveTanks", {
                "id": str(uuid.uuid4()), "diveId": dive_id, "tankOrder": order,
                "volume": gas.tank_volume, "startPressure": gas.start_pressure, "endPressure": gas.end_pressure,
                "o2Percent": gas.oxygen, "hePercent": gas.helium or 0.0,
                "tankRole": gas.tank_role or ("backGas" if order == 0 else None), "tankName": gas.tank_name,
            })

        # the new dive's profile, when the sender has one
        if unified.samples:
            import base64
            samples = [(int(s.time or 0), s.depth, s.temp) for s in unified.samples]
            blob = base64.b64encode(codec.encode_profile(samples)).decode("ascii")
            summary = codec.profile_summary(samples)
            self._put_row("diveProfileSeries", dict(summary, id=str(uuid.uuid4()), diveId=dive_id, isPrimary=True,
                                                    samples=blob))

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
