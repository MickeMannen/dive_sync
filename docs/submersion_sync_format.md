# Submersion sync format — reference for dive_sync

Read from the Submersion source (github.com/submersion-app/submersion, commit `2debe84`, 2026-09-20). Nothing here is documented by the Submersion project outside its code; re-verify against the source when Submersion ships a new release. Status marks in the plan (`rework.md` Track F) point here.

Schema: `currentSchemaVersion = 221`, `minimumCompatibleSchemaVersion = 210`. Sync wire format: `syncFormatVersion = 2`, manifest `formatVersion = 1`.

---

## 1. Which cloud providers a third party can reach

| Provider | How Submersion stores it | Can dive_sync read/write it? |
|---|---|---|
| **Google Drive** | Drive `appDataFolder`, OAuth scope `drive.appdata` only. Folder is hidden from the user and scoped to Submersion's Google Cloud project | **No.** Only an OAuth client from the same Google project can list or read `appDataFolder`. Reusing Submersion's client ID is against Google's terms and can be revoked at any time |
| **S3-compatible** (AWS, MinIO, R2, B2, NAS) | Flat objects under `prefix` (default `submersion-sync/`) in a bucket; config = endpoint, region, bucket, prefix, path-style flag, access key, secret | **Yes.** Any S3 client (`boto3`) with the same credentials. Best fit for Docker. Self-host with Garage or SeaweedFS on the NAS (MinIO community edition was archived in April 2026, avoid it for new setups), or use a free hosted tier: Backblaze B2 (10 GB), Cloudflare R2 (10 GB, card required), Tebi (25 GB). Sync data is a few MB, so any of these is far above what is needed |
| **Dropbox** | "App folder" access, visible to the user as `Apps/Submersion/` | **Yes**, with the user's own Dropbox token, or by reading the local Dropbox folder on a desktop |
| **iCloud** | Container `iCloud.app.submersion`, Documents directory | **Desktop app on a Mac only**, by reading the container folder on disk (path to confirm on a real machine, expected under `~/Library/Mobile Documents/`) |

**Consequence:** the user must set Submersion's sync provider to S3, Dropbox or iCloud. Google Drive cannot be used as the shared store.

## 2. Files in the sync folder

Everything lives flat in one folder (`Submersion Sync` on Drive/iCloud, the key prefix on S3, the app folder on Dropbox). Device ids are UUIDs, so `.` is the field separator; sequence numbers are zero-padded so lexical order equals numeric order.

| File | Purpose |
|---|---|
| `ssv1.<deviceId>.manifest.json` | Per-device commit point, rewritten on every publish. The only mutable file per device |
| `ssv1.<deviceId>.base.<seq:12>.p<part:4>` | Full snapshot of that device's library, byte-sliced into 8 MiB parts, reassembled then decoded as one payload |
| `ssv1.<deviceId>.cs.<seq:12>.json` | Incremental changeset since the previous seq |
| `ssv1.<deviceId>.retired.json` | Marker that a device's log was retired by a peer |
| `submersion_library_epoch.json` | Which library generation is current (written on a "replace" restore). Payloads carrying an older `epochId` are ignored |

Files are plain JSON unless end-to-end encryption is on, in which case every file is an `SBE1` envelope (section 6).

### Manifest JSON

```json
{
  "formatVersion": 1,
  "schemaVersion": 210,          // compatibility floor; readers with a LOWER schema hold this peer
  "writerSchemaVersion": 221,    // diagnostics only
  "deviceName": "…", "deviceId": "<uuid>", "provider": "s3",
  "baseSeq": 12, "basePartCount": 3, "baseBytes": 123456,
  "baseChecksum": "…", "basePartChecksums": ["…"],
  "headSeq": 17,                 // newest changeset seq
  "publishedHlcHigh": "<hlc>",   // watermark: everything up to this HLC is published
  "epochId": "<uuid or null>", "uploadNonce": "…",
  "appliedPeerHlc": {"<peerDeviceId>": "<hlc>"},   // what this device has applied from each peer
  "updatedAt": 1758300000000     // unix ms
}
```

Liveness rules that a dive_sync "device" must respect: a manifest older than 7 days triggers a heartbeat rewrite by the app; a manifest older than **365 days** is retired by any live peer (files deleted, marker written). So an unattended dive_sync peer must republish its manifest at least occasionally, and on start-up must check for its own `.retired.json` and re-join with a fresh base.

### Payload JSON (base and changeset share one shape)

```json
{
  "version": 2, "exportedAt": <unix ms>, "deviceId": "<uuid>",
  "lastSyncTimestamp": null, "checksum": "<sha256 of the JSON-encoded data object>",
  "data": { "divers": [...], "dives": [...], "diveTanks": [...], "diveSites": [...],
            "buddies": [...], "diveBuddies": [...], "tags": [...], "diveTags": [...],
            "diveProfileSeries": [...], "tankPressureSeries": [...], "diveProfileEvents": [...],
            "...": "one list per table, ~80 keys; unknown keys are ignored on read" },
  "deletions": { "dives": [{"id": "...", "deletedAt": <ms>, "hlc": "<hlc>"}], "...": [] },
  "uploadNonce": "…", "epochId": "…", "seq": 17, "baseSeq": 12,
  "sinceHlc": "<hlc or null>", "toHlc": "<hlc>"
}
```

Each list entry is a **full row** serialised by Drift's generated `toJson`: keys are the Dart column names in camelCase (`diveDateTime`, `maxDepth`, `siteId`), BLOB columns are base64 strings, booleans are JSON booleans. The checksum is SHA-256 over the `data` object exactly as serialised.

## 3. Hybrid logical clock (`hlc` column)

String `"<physicalMs:15 digits>:<counter:6 digits>:<deviceId>"`, e.g. `000001758300000000:000003:8f1c…`. Ordering is (physicalMs, counter, deviceId). Every row a device writes gets a fresh HLC; on receipt of a remote HLC a device advances its own clock past it. dive_sync must keep one persistent clock per configured Submersion pair and stamp every row it writes.

## 4. Merge rules (what Submersion does with our rows)

- Per row: the copy with the **higher HLC wins**. Rows without HLC fall back to `updatedAt`.
- A row the local device has edited but not yet published (`sync_records.syncStatus = pending`) is not overwritten by a remote copy; the conflict is recorded in `sync_records.conflictData`.
- Tombstones win over older edits; a remote edit strictly newer than a deletion revives the row.
- Child rows (`diveTanks`, `diveBuddies`, `diveTags`, series, events) are "parent-gated": a child whose parent dive is tombstoned is dropped; since v210 children carry their own HLC and a stale child copy cannot overwrite a newer local one.
- A device whose manifest declares `schemaVersion` above the reader's own schema is held (not applied) until the reader upgrades. dive_sync should publish `schemaVersion: 210` (the current floor) and only use columns that exist at 210.

## 5. Tables dive_sync needs

Only the columns relevant to `UnifiedDive`. Every table has `id` (TEXT UUID primary key), `createdAt`, `updatedAt` (unix ms) and `hlc`.

**`dives`**

| Column | Type | Notes |
|---|---|---|
| `diverId` | text? | FK `divers.id`. dive_sync uses the default diver (`divers.isDefault`) |
| `diveNumber` | int? | |
| `name` | text? | user-given dive name, null = falls back to site name |
| `diveDateTime` | int | "Unix timestamp" — **verify seconds vs ms against a real export** |
| `entryTime`, `exitTime` | int? | same unit |
| `bottomTime`, `runtime` | int? | seconds |
| `maxDepth`, `avgDepth` | real? | metres |
| `waterTemp`, `airTemp` | real? | °C |
| `visibility` | text? | free text; `visibilityMeters` real? is the numeric form |
| `buddy` | text? | legacy single string; the structured form is `dive_buddies` |
| `notes` | text | default `''` |
| `siteId` | text? | FK `dive_sites.id` |
| `weightAmount` | real? | kg; `weightType` text? |
| `diveType` | text | default `recreational`; `diveMode` `oc`/`ccr`/`scr` |
| `entryLatitude`, `entryLongitude` | real? | GPS from computer; site GPS lives on `dive_sites` |
| `importSource`, `importId` | text? | e.g. `'garmin'` + source id. **This is where dive_sync stores the Garmin activity id** for Tier 1 matching |
| `computerId`, `diveComputerModel/Serial/Firmware` | | optional |

**`dive_sites`**: `name` (text, required), `description`, `latitude`, `longitude`, `country`, `region`, `city`, `notes`, `isShared`.

**`dive_tanks`** (one row per cylinder, FK `diveId`): `volume` (L), `workingPressure`, `startPressure`, `endPressure` (bar), `o2Percent` (default 21), `hePercent` (default 0), `tankOrder`, `tankRole` (`backGas`, `stage`, `deco`, `bailout`), `tankName`, `presetName`, `transmitterSerial`.

**`buddies`**: `name`, `email`, `phone`, `notes`, `photo` (blob). **`dive_buddies`**: `diveId`, `buddyId`, `role` (default `buddy`). Garmin/Divelogs have a single buddy string: split on `,` outbound and look up or create `buddies` rows by name; join inbound.

**`tags`**: `name`, `color`, `appliesToDives`. **`dive_tags`**: `diveId`, `tagId`.

**`dive_profile_series`** (FK `diveId`, one primary row per dive): `sampleCount`, `startTimestamp`, `endTimestamp`, `maxDepth`, `firstDepth`, `lastDepth`, `codecVersion` (1), `samples` (blob, section 7), `isPrimary`.

**`tank_pressure_series`** (FK `diveId`, `tankId`): same shape, separate codec for pressure.

**`dive_profile_events`**: `timestamp` (s from start), `eventType`, `severity`, `description`, `depth`, `value`, `tankId`, `source`.

**`deletion_log`** / payload `deletions`: `entityType`, `recordId`, `deletedAt`, `hlc`, `originHlc`.

## 6. Encryption envelope (only if the user enables E2E)

`"SBE1"(4) | libraryKeyId(16, UUID) | flags(1, bit0 = gzip) | nonce(12) | AES-256-GCM ciphertext | tag(16)`. AAD is the UTF-8 logical filename. The data key is derived from the user's passphrase via `crypto/keyslots.dart` (Argon2id key slots, plus an EFF-wordlist recovery code).

**Implemented 2026-09-22 (rework.md E11)**, ported to `src/core/services/submersion/crypto.py` from `lib/core/services/sync/crypto/keyslots.dart` and `sync_envelope.dart` (commit `2debe84`), verified byte-for-byte against the project's own committed known-answer vectors (`scripts/generate_crypto_test_vectors.py` / `test/fixtures/crypto/crypto_vectors.json`, vendored into `tests/data/submersion/crypto/`):

- The cloud's plaintext `submersion_keyslots.json` (`{version, libraryKeyId, slots: [{type, salt, kdf, nonce, wrapped}]}`) holds one or more wrapped copies of a 32-byte master library key (MLK), one per unlock method (`type: "passphrase"` or `"recovery"`). Each slot wraps the MLK under AES-256-GCM keyed by an Argon2id KEK derived from that method's secret + the slot's own salt/KDF params.
- The actual per-file data key is `HKDF-SHA256(IKM=MLK, salt=b"", info=b"sbe:v1:data")` - the MLK itself never touches file content directly.
- dive_sync only ever *joins* an already-encrypted library (never enables encryption, rotates keys, or issues a recovery code - those stay Submersion-app-only actions): given the keyslot file and a passphrase in `SubmersionCredentials.passphrase`, it recovers the MLK, derives the data key, and seals/opens every `ssv1.*` file (and `submersion_library_epoch.json`) through it. The keyslot file itself is always plaintext.
- **Scope restriction, deliberate**: dive_sync does not generate recovery codes or offer a recovery-code UI, since its only job is to sync as an existing library member, not to be the device someone recovers access through.
- Dependencies: `argon2-cffi` (Argon2id) and `cryptography` (AES-256-GCM, HKDF) - the same two Python packages Submersion's own KAT-vector generator script uses, so an exact match was expected and verified, not assumed.

## 7. Profile sample blob (`ProfileSeriesCodec` v1)

zlib (level 6) over: `version byte (1)` | `sampleCount varint` | one column block per field in table order. Each block starts with a presence mode (all-null / all-present / bitmap of ceil(n/8) bytes LSB-first) then the values. Field kinds: `deltaInt` (zigzag varint delta from previous), `float64` (little-endian), `runLengthString`.

Field table v1 in order: `timestamp` (deltaInt, s), `depth`, `pressure`, `temperature`, `heart_rate` (deltaInt), `ascent_rate`, `ceiling`, `ndl` (deltaInt), `setpoint`, `pp_o2`, `o2_sensor1..6`, `cns`, `tts`, `rbt`, `deco_type` (deltaInt), `heart_rate_source` (runLengthString), `heading`, `o2_sensor_mv1..6` (deltaInt).

dive_sync only needs `timestamp`, `depth`, `temperature` (and optionally `pressure`) to round-trip `UnifiedSample`. A Python port of `byte_io.dart` + this table is roughly 150 lines and must have byte-exact fixtures generated from a real Submersion export.

## 8. Mapping to `UnifiedDive`

| UnifiedDive | Submersion |
|---|---|
| `date_time` | `dives.diveDateTime` (unit to verify) |
| `duration` | `dives.runtime` if set, else `bottomTime` |
| `max_depth`, `avg_depth` | `dives.maxDepth`, `dives.avgDepth` |
| `temp_min` | `dives.waterTemp` (single value; min/max/avg from the series) |
| `location`, `lat`, `lng` | `dive_sites.name`, `latitude`, `longitude` via `siteId` |
| `notes` | `dives.notes` |
| `dive_number` | `dives.diveNumber` |
| `weight`, `weight_unit` | `dives.weightAmount` (always kg) |
| `visibility`, `visibility_unit` | `dives.visibilityMeters` (always m); `visibility` text left alone |
| `buddy` | `dive_buddies` → `buddies.name`, joined with `, ` |
| `gas_mixtures[]` | `dive_tanks` rows ordered by `tankOrder` |
| `samples[]` | `dive_profile_series.samples` decoded |
| `external_ids["garmin"]` | `dives.importSource = "garmin"`, `dives.importId = <activityId>` |
| `external_ids["divelogs"]` | no native slot; store as a `dive_custom_fields` row or a tag `divelogs:<id>` (decide in F9) |
| `external_ids["submersion"]` | `dives.id` |

## 9. Open questions to settle with a real export

**Answered 2026-09-21 from a real S3 export** (two devices, schema 210, writer 218; fixtures in `tests/data/submersion/`, captured with `tests/tools/anonymize_fixtures.py`):

1. `diveDateTime`, `entryTime`, `exitTime`, `createdAt`, `updatedAt`, `exportedAt` are **milliseconds** since the epoch. Profile `startTimestamp` / `endTimestamp` and `gasSwitches.timestamp` are seconds from the dive start.
2. A `divers` row exists (`isDefault: true`) and every dive, site, tag and piece of equipment carries its `diverId`; write rows with the default diver's id.
4. The second device's manifest has `appliedPeerHlc` populated with the first device's `publishedHlcHigh` after it read that base, so peers do report what they applied; whether GC stalls without it is still unobserved (no deletions were exchanged yet).
5. The export carries far more tables than section 5 lists (about 100, most empty): `diverSettings`, `diveEquipment`, `diveWeights`, `qualityFindings`, `connectedAccounts`, `diveDataSources`, `importedFiles` (the raw FIT files, base64, one per import), `gasSwitches`, `diveSafetyReviews`, `tankPressureSeries` (one per tank), `diveProfileSeries` (`samples` is base64 of a zlib stream, `codecVersion: 1`, 3002 samples in 19.7 KB), and so on. A dive imported from a FIT file also gets `diveDataSources` with `sourceUuid = garmin-<serial>-<ms>` and `importSource`/`importId` empty, so the Garmin id slot is `sourceUuid`, not `importId`, for FIT-imported dives.
6. Multi-tank works out of the box on the Submersion side: the two 2026-08-29 Garmin FIT dives arrive with two `diveTanks` rows each (`tankOrder` 0/1, `tankRole` `backGas`), two `tankPressureSeries` and `gasSwitches` rows. Garmin Connect's own API returned only one tank sensor for the same dives, so the FIT file is the complete source for multi-tank data on Garmin.

Still open: 3 (iCloud path) and how a changeset file looks (the bucket only holds `base` snapshots until a device edits something; ask the owner to change a dive on the phone and re-capture).

Original questions:

1. Unit of `diveDateTime` / `entryTime` (seconds or milliseconds).
2. Whether `divers` must contain a row before dives referencing `diverId` are accepted, and how the app picks the default diver for rows with `diverId = null`.
3. Exact iCloud container path on macOS.
4. Whether `appliedPeerHlc` must be populated for tombstone GC to progress, or whether an absent entry merely delays GC.
5. Behaviour when a foreign device publishes with `schemaVersion: 210` but rows lacking columns added after 210 (should be fine: missing keys read as null/defaults).
