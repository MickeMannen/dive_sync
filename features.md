# Dive Sync — Feature Inventory

Living list of what the project does today, what is partial, and what is planned.
Use it as the backlog: add a row when you want a new feature or change, flip the status when it ships.

Verified against the code on 2026-09-20 (branch `feature_branch`, commit `4b234a0`).

**Status legend**
- ✅ Done and working
- 🚧 Partial / works with caveats
- 📝 Planned / not started
- ❌ Known gap or limitation

---

## 1. Core sync engine (`src/core/`)

| # | Feature | Status | Notes / where |
|---|---------|--------|---------------|
| 1.1 | Unified dive model (`UnifiedDive`, `GasMixture`, `UnifiedSample`) with metric units | ✅ | `models.py` — date/time, duration, max/avg depth, temps, gases, location, notes, dive number, weight, visibility, buddy, GPS, profile samples, optional FIT payload |
| 1.2 | Adapter interface (`BaseDiveAdapter`: login / fetch / add / update / delete, plus `service_id` and `field_catalog()`) | ✅ | `adapter.py`; catalogue models and link validation in `fields.py` |
| 1.3 | Garmin Connect adapter | ✅ | `services/garmin.py` — via `python-garminconnect`; token cache on disk per account; 429 rate-limit and MFA errors surfaced clearly |
| 1.4 | Divelogs.org adapter | ✅ | `services/divelogs.py` — bearer-token REST API; honours the user's imperial/metric preference when writing |
| 1.5 | Local mock adapters (offline sync from cached JSON) | ✅ | `services/mock_adapters.py`, CLI `--mock-data-dir` |
| 1.6 | Declarative JSONPath field mappings | ✅ | `mapping/garmin_to_divelogs.json`, `mapping/divelogs_to_garmin.json` |
| 1.7 | Three-tier dive matching: external-ID link → match keys from the mapping board (none on Garmin↔Divelogs by default; a hit must be within 24 h) → start times within the grace window, in UTC when both sides know their zone | ✅ | `sync_engine.py:match_dives`; tier 2 iterates `field_links` with `match_order` set (rework.md C19, guard decided 2026-09-21) |
| 1.8 | Direction control: one receiver per run (`to_garmin` / `to_divelogs` / `to_<service>` / `to_target` / `to_source`) | ✅ | settings + CLI/API/desktop override. `bidirectional` removed as a run mode 2026-09-23 (rework.md G0): a two-way sync is two directed runs; old configs migrate on load. Deletes follow the direction |
| 1.9 | Upload unmatched dives to the other service | ✅ | Both directions; Divelogs "already exists" responses are skipped, not errored |
| 1.10 | Remember matched and uploaded pairs across runs | ✅ | Neither Garmin nor Divelogs has a field for a foreign id (the old "cross-link" wrote nothing); since 2026-09-21 pairs are kept in `sync_state.json` (`links`) and count as tier-1 matches. Adapters that can store ids set `stores_external_ids` |
| 1.11 | Field-level update of matched dives, driven by the pair's receiver rules (`sync_pairs[].rules`, rework.md G1/G2 2026-09-23): the run's receiver applies its own rules, policy read from its side, `target_wins` = never overwrite | ✅ | `sync_engine.py:_apply_link`; default board = buddy, notes, weight, visibility, GPS and site name bidirectional with `manual` (blanks filled for free, a real conflict queued in the Conflicts view rather than silently kept - flipped from the interim `prefer_non_empty` 2026-09-21, rework.md C12), tanks (`prefer_source`) and profiles (`prefer_non_empty`) to Divelogs, activity name built for new Garmin dives. Old Garmin-wins board: `fields.legacy_field_links()`. Edit on the mapping board (status page or desktop, B11) or directly in `settings.json` (`field_links`) / `POST /api/settings` |
| 1.12 | GPS coordinates sync | ✅ | A `gps` rule on each receiver, fill-blanks policy, half-metre tolerance on comparison |
| 1.13 | Depth/temperature profile samples sync | 🚧 | Default link `samples`, Garmin→Divelogs only (Garmin cannot write samples). Since 2026-09-21 profiles are resampled onto Divelogs' fixed `samplerate` grid (Garmin records irregular intervals) and compared on that grid | **Never written to Submersion** (rework.md F17, 2026-09-24): its profile is derived by the app's own `.fit` import, so `submersion.samples` is read-only - dive_sync only reads it, to feed another service. A dive dive_sync creates itself still gets the profile it came with.
| 1.14 | Gas mixtures / tank pressures / tank volume sync, with tank order and role | 🚧 | Default link `tanks`, Garmin→Divelogs only (Garmin's gas API is read-only, E4). Order preserved via list order everywhere; role (`backGas`/`stage`/`deco`/...) is a new `GasMixture.tank_role` field, round-trips through Submersion (native support) and partially through Subsurface (`diluent`/`oxygen`/`bailout`/`not used` only); Divelogs and Garmin have no role concept. Verified live 2026-09-22 including a real multi-tank Divelogs write. `sync_gases` off disables every tanks link | **Not written to Submersion on a matched dive** (rework.md F17, 2026-09-24): the cylinders, tank-pressure series and gas switches all come out of the diver's own `.fit` import there, so `submersion.tanks` is read-only; role still round-trips on read, and a dive dive_sync creates gets the gas it came with.
| 1.15 | Garmin tank-sensor telemetry fetch | ✅ | `/diving/v1/dive/detail/tanksensor` per activity; 404 tolerated |
| 1.16 | Garmin update pushes only changed fields (partial PUT) | ✅ | Location, notes, dive number, buddy, weight, visibility, GPS. Samples/gases are never pushed to Garmin |
| 1.17 | Incremental sync ("only new" since last run, minus 1-day grace) | ✅ | State in `sync_state.json` (also holds the known pairs); `--full-sync` forces full |
| 1.18 | Date-range filters (`date_from` / `date_to`) | ✅ | Settings or CLI override |
| 1.19 | Dry-run mode (no remote writes, no state update) | ✅ | CLI `--dry-run`, status page toggle, desktop switch |
| 1.20 | API cooldown between requests (`api_cooldown_seconds`) | ✅ | Applied in both adapters; Garmin login 429 is logged with a wait hint. Default halved to 0.5 s on 2026-09-23 (rework.md E16) |
| 1.21 | Multi-account credentials (list of Garmin and/or Divelogs accounts) | ✅ | `CredentialsModel` plus CLI `--garmin`/`--divelogs`, and since 2026-09-21 (rework.md A8/E7) also the scheduler (`CronJobModel` account fields), the status page (repeatable account rows, cron/trigger dropdowns) and the desktop app (`SettingsPage.qml`/`SyncPage.qml`). Desktop keychain storage: one item per service holding every account as a JSON blob (2026-09-22, found necessary during D7/D9's hands-on session - storing one keychain item per account per field produced 15-20 separate macOS access prompts per launch) |
| 1.22 | Per-account sync state and data directories | ✅ | `sync_state_<garmin>_<divelogs>.json`, `data/garmin/<user>/`, `data/divelogs/<user>/` |
| 1.23 | FIT file download from Garmin (`GarminAdapter.download_fit`, `dive_cache.download_garmin_fits`) | ✅ | 2026-09-24: on demand from the Garmin dive list (desktop: Download FIT / Download missing FITs buttons, FIT column; web: Garmin dives page with per-dive download and Save link). Saved to `data/garmin_fit/<account>/`, outside the JSON cache so a Full refresh cannot wipe them. Cached JSON and FIT share one name, `<dive #>_<YYYY-MM-DD>_<HHMMSS>_<activity id>` (`src/core/garmin_files.py`); a refresh renames old `<dive #>.json` files and keeps FIT names in step when a dive is renumbered or retimed. Hand-logged dives (`isManualActivity`) show `manual` in the FIT column and are never downloaded: Connect answers for them with a FIT it generates from the typed-in summary (checked 2026-09-24: `garmin_product: connect`, 524 bytes, no records/profile). `sync_fit` still does nothing |
| 1.24 | Full-history backup to flat JSON (`--backup`) | ✅ | `sync_engine.py:backup` |
| 1.25 | Raw data download / local cache (`download_and_save_raw_data`) | ✅ | Per-service scoping (`include_garmin` / `include_divelogs`), optional overwrite. Populates the files the dive editor reads |
| 1.26 | Local dive cache read/list/update/delete + remote push (`dive_cache.py`) | ✅ | Display formatting (duration in minutes, depth 2 dp, ISO `T` normalised), field edits written back into raw Garmin/Divelogs JSON shapes |
| 1.27 | Version check against GitHub latest release | ✅ | `version.py`, cached 1 h, `APP_VERSION` env |
| 1.28 | Deletion propagation: a dive deleted on one side deletes the linked dive on the other, per pair, off by default | ✅ | rework.md C13, 2026-09-22. Full-sync only (an incremental run can't reliably tell "deleted" from "outside this run's date window"); when off, just a log line, and the orphaned dive naturally re-uploads via the existing unmatched-dive path. `SettingsModel.propagate_deletes` / `SyncPairModel.propagate_deletes`; UI on both mapping boards' header strip and the status page's default settings |
| 1.29 | Automatic pre-write backup: every non-dry-run sync snapshots what it just fetched (both sides, `UnifiedDive` JSON) before writing anything | ✅ | rework.md C14, 2026-09-22. `<state-file-dir>/backups/<timestamp>/<service>.json`; keeps the last `SettingsModel.backup_retention_count` runs (default 10); best-effort, never fails the sync it's protecting |
| 1.30 | `create_on_garmin` switch, per pair, off by default: a new dive found only on the other side is not created on Garmin unless enabled; matched-dive field updates unaffected | ✅ | rework.md C16, 2026-09-22. `SettingsModel.create_on_garmin` / `SyncPairModel.create_on_garmin`; a blocked dive is reported in the sync results as skipped, not silently dropped, and stays eligible next run. UI on both mapping boards' header strip and the status page's default settings |

## 2. Scheduler (`src/core/scheduler.py`)

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 2.1 | Background asyncio watcher, checks settings every minute | ✅ | No FastAPI dependency; reusable from any entry point |
| 2.2 | Cron-like jobs: hourly / daily / weekly / custom-minute interval | ✅ | `CronJobModel` — per-job direction, only_new, sync_gases, account selection (`garmin_username`/`divelogs_username`, rework.md A8), enabled, optional `field_links`. Per-job values are now passed as explicit `run_sync` overrides (they used to be lost to the settings reload). `sync_fit` removed from here 2026-09-21 (E6) - it never affected a normal sync run |
| 2.3 | Legacy daily time slots (`schedule: [{hour, minute}]`) | ✅ | Still honoured alongside cron jobs |
| 2.4 | Skip a job when a sync is already running | ✅ | Single global `is_sync_running` flag |
| 2.5 | Next-run estimate for status display | ✅ | `get_next_scheduled_run` |
| 2.6 | Background raw-data download runner (`run_download_thread`) | ✅ | Mirrors `run_sync_thread`; used by desktop app |
| 2.7 | Scheduled jobs for a specific account in multi-account setups | ❌ | Jobs always call `SyncEngine()` with no account override |

## 3. CLI (`sync.py`, `setup_credentials.py`)

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 3.1 | `python sync.py` incremental sync in the saved direction, with results summary | ✅ | `--direction` rejects `bidirectional` since 2026-09-23 (G0) |
| 3.2 | Flags: `--dry-run`, `--direction`, `--date-from/--date-to`, `--full-sync`, `--garmin`, `--divelogs`, `-v` | ✅ | |
| 3.10 | Other services: `--source SPEC --target SPEC` (`garmin`, `divelogs`, `uddf:<file>`, `subsurface:<dir>`, `subsurface-cloud`) or `--pair <id>` from `sync_pairs` in settings; cron jobs can name a pair | ✅ | 2026-09-22, rework.md F3/F6 |
| 3.8 | Mapping-board flags: `--show-mapping`, `--test-mapping` (read-only, newest 10 dives per side), `--list-conflicts`, `--resolve <id> source\|target` | ✅ | 2026-09-21, rework.md C5 |
| 3.9 | Profile flags: `--export-profile`, `--validate-profile`, `--import-profile [--yes]` | ✅ | 2026-09-21, rework.md C9 |
| 3.3 | `--backup` with `--garmin-path` / `--divelogs-path` | ✅ | |
| 3.4 | `--save-raw-data [dir]` and `--overwrite` | ✅ | |
| 3.5 | `--mock-data-dir [dir]` offline sync | ✅ | |
| 3.6 | Interactive credential setup with live login verification, one section per service (Garmin, Divelogs, Subsurface Cloud, Submersion S3 store), writing to `DATA_DIR` | ✅ | `setup_credentials.py`; `--services` to limit, skipped sections keep their stored values. README fixed 2026-09-21 |
| 3.7 | `DATA_DIR` env var relocates settings, credentials, tokens, caches | ✅ | Used by Docker and desktop |

## 4. Docker status page (`src/web/`, `docker_run.py`, `Dockerfile`)

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 4.1 | Single-process container: uvicorn + scheduler loop, graceful SIGTERM | ✅ | `docker_run.py` |
| 4.2 | Status view: running flag, last result, next scheduled run | ✅ | `GET /api/status` |
| 4.3 | Manual "Sync now" with dry-run toggle (409 if already running) | ✅ | `POST /api/sync/trigger` accepts direction/filter overrides |
| 4.4 | Live log tail via Server-Sent Events | ✅ | `GET /api/logs/stream` |
| 4.5 | Credentials form: save + test Garmin/Divelogs/Subsurface Cloud/Submersion login | 🚧 | `POST /api/credentials` writes a single Garmin/Divelogs account and overwrites any multi-account list; Subsurface Cloud and Submersion (store type, S3 fields or folder path) each have their own form fieldset wired to save/test (rework.md F13, done 2026-09-22) |
| 4.6 | Default sync settings form (direction, grace window, cooldown, only_new, sync_gases) | ✅ | `GET/POST /api/settings`; the payload also carries `field_links` and `sync_pairs` (validated on save, kept when omitted). No `sync_fit` control since 2026-09-21 (E6) - the stored value passes through unchanged on save |
| 4.12 | Mapping board: per pair, two receiver panels ("what X takes from Y") with drag-and-drop from the sender's fields onto the receiver's, a rule editor (policy as seen by the receiver, template with live preview, reverse pattern + split policy), an ordered match-key strip, Save/Cancel/Reset, apply-to-all prompt, read-only Test mapping on the newest 10 dives naming the receiver per row | ✅ | 2026-09-22 (links, rework.md C6/C20); rebuilt around receiver rules 2026-09-23 (rework.md G5); click-to-connect (arm a sender field, click the receiver) and edge auto-scroll added 2026-09-23 (rework.md G10) for field lists too long to drag across |
| 4.13 | Conflicts table with resolve buttons; sync pairs table; profile export/import with diff summary | ✅ | 2026-09-22, rework.md C6/C10/F5 |
| 4.7 | Scheduled jobs table: add / edit / remove cron jobs | ✅ | |
| 4.8 | Version + update-available banner | ✅ | `GET /api/version` |
| 4.9 | Dive editing / cache explorer in the web UI | ❌ | Removed on purpose (moved to desktop app) |
| 4.10 | Per-account last-sync status | ❌ | Only one global `last_sync_results` |
| 4.11 | Multi-arch Docker Hub image (amd64/arm64) built on GitHub Release | ✅ | `.github/workflows/docker-release.yml`, syncs `DOCKERHUB.md` |

## 5. Desktop app (`desktop/`, Briefcase + PySide6 / Qt Quick)

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 5.1 | Sidebar shell with sections: Sync, Garmin Dives, Divelogs Dives, Mapping, Conflicts, Settings | ✅ | QML `main.qml`, window sized from the screen, light/dark theme tokens (2026-09-22) |
| 5.2 | Sync section: pair picker, direction, dry-run / only-new / gases / FIT switches, "Sync now", progress bar, live log | ✅ | Reuses `scheduler.run_sync_thread` on a `QThread`; a Garmin refresh shows a real progress bar and the dive being fetched since 2026-09-23 (rework.md E14). A refresh skips dives already cached and unchanged (rework.md E15); Full refresh re-fetches everything. Dives deleted or renumbered on the service are removed from the local cache (rework.md E18). Since 2026-09-24 a **sync** reuses that cache too ("Use cached Garmin dives", on by default; `SyncFilters.use_garmin_cache`, web trigger + default settings, CLI `--no-garmin-cache`): `GarminAdapter.cache_dir` makes `fetch_dives` read an unchanged, complete cached dive instead of three API calls, and cache what it fetches. Same blind spot as Refresh: notes/buddy/weight/visibility-only edits on Garmin need the switch off or a Full refresh |
| 5.3 | "Download dives" (raw cache refresh) with overwrite switch | ✅ | |
| 5.4 | Dive editor tables (Date, Time, Dive #, Location, Max Depth, Duration, Buddy, Weight, Visibility) | ✅ | Shared `DiveEditorSection` for both services |
| 5.5 | Per-tab Refresh that downloads only that service's dives, with live status line | ✅ | |
| 5.6 | Column chooser and sort column/direction, persisted per service | ✅ | `desktop_prefs.json` in app data dir |
| 5.7 | Detail form: edit dive #, date, time, duration, max depth, location, notes, weight, visibility, buddy; avg depth and water temp read-only | ✅ | Save writes local cache then pushes to remote |
| 5.8 | Delete dive (confirm dialog, local + remote) | ✅ | |
| 5.9 | Edits/deletes blocked while a sync or download runs | ✅ | |
| 5.10 | Keychain-backed credentials (`keyring`) for Garmin, Divelogs and Subsurface Cloud with Test buttons and first-run landing on Settings | ✅ | Password fields never prefilled |
| 5.11 | Credentials and Garmin token materialised to disk only for the duration of an operation | ✅ | `begin_operation` / `end_operation`; Qt `aboutToQuit` cleanup |
| 5.18 | Mapping board (receiver panels, match-key strip, rule editor - same model as the status page), conflicts view and profile export/import in the desktop app | ✅ | 2026-09-22, rework.md C7/C11/D6; board rebuilt around receiver rules 2026-09-23 (rework.md G6); click-to-connect 2026-09-23 (rework.md G10) |
| 5.12 | Private per-user data dir via `platformdirs` | ✅ | |
| 5.13 | Telemetry graphs (depth / temperature / gas) | 📝 | Phase 2 in `rework.md` |
| 5.14 | Windows / Linux Briefcase targets | 📝 | macOS only today |
| 5.15 | Code signing / notarisation | 📝 | |
| 5.16 | Built-in scheduler in the desktop app | ❌ | By design; scheduling stays in Docker |
| 5.17 | Multi-account selection in the desktop app | ❌ | Single account only |

## 6. Testing and tooling

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 6.1 | Pytest suite (`./run_tests.sh`): sync logic, mock sync, field catalogue/links, templates, conflicts, profiles, link-driven engine, CLI, scheduler, dive cache, web API, desktop credentials and preferences | 🚧 | 248 tests across 23 files (2026-09-22); real anonymised Submersion and Subsurface Cloud fixtures under `tests/data/` (rework.md F9, F4); ~10 skip unless real downloaded fixtures or a live `DIVE_SYNC_LIVE_DATA_DIR` exist. `test_link_engine.py` replays the pre-Track-C loop as a reference over 400 seeded cases |
| 6.2 | Live-API integration tests against real Garmin/Divelogs (and Subsurface Cloud, Submersion store) | 🚧 | `tests/live/`, opt-in via `DIVE_SYNC_LIVE_DATA_DIR=<test data dir>`; read-only so far (rework.md E9) |

---

## 7. Known limitations (from README, rework.md and code review)

- Beta quality. Garmin→Divelogs is well exercised; Divelogs→Garmin much less so. Keep backups.
- Divelogs→Garmin gas/tank data is not written on matched-dive updates.
- Profile samples flow only Garmin→Divelogs.
- A real conflict (both sides non-empty and different) on the default mapping board's scalar fields is queued in the Conflicts view for a manual pick, not applied automatically - see rework.md C12.
- Docker and desktop syncing the same account from different machines have no shared lock; back-to-back runs can hit Garmin rate limits.
- `sync_fit` only affects `--backup`; no UI sets it any more (rework.md E6) - use `POST /api/settings` or hand-edit `settings.json`.
- Gases and tanks cannot be written to Garmin Connect through its API (creation or update); see rework.md E4 for the 2026-09-22 investigation.
- Start time, duration, max depth and average depth **are** writable on Garmin (probed live 2026-09-23, rework.md G8) and are mappable on the board; the earlier lock was an untested assumption. Only `date_time_utc` stays read-only, because Garmin recomputes `startTimeGMT` from the local time itself.

---

## 8. Backlog / ideas

Seeded from `todo_txt` and `rework.md`. Add new requests here; move them into the tables above once scoped.

| # | Request | Priority | Status | Notes |
|---|---------|----------|--------|-------|
| B1 | Telemetry graphs in the desktop dive editor | | ✅ | 2026-09-21 (rework.md E1): depth/temperature profile in `DivesPage.qml`, drawn on a QML `Canvas` rather than QtCharts (crashes this environment - see E1 note) |
| B2 | Windows and Linux desktop builds | | ✅ | rework.md E2: Briefcase config + CI workflow added 2026-09-21, actually run and fixed 2026-09-22 (all three platforms build+package clean) |
| B3 | macOS signing and notarisation | | ✅ | rework.md E3; done 2026-09-22 once the owner had an Apple Developer account and Developer ID certificate |
| B4 | Sync gas mixtures / tank data Divelogs→Garmin | | 📝 | todo_txt: "check how to handle dive gases" |
| B5 | Multi-tank data to Divelogs | | 📝 | todo_txt: "multi tank to divelogs - tank data" |
| B6 | More verification / test coverage, especially Divelogs→Garmin | | 📝 | todo_txt: "add more verification" |
| B7 | Sync with Subsurface (Subsurface Cloud git storage; `.ssrf` XML optional) | | ✅ | Git-storage adapter (rework.md F4) and Subsurface Cloud clone/commit/push (F6) done 2026-09-22: `subsurface:<dir>` for a checkout, `subsurface-cloud` for the account in `credentials.json`. Desktop UI for it is Track D |
| B8 | Account selection for scheduled jobs, status page and desktop app | | ✅ | 2026-09-21 (rework.md A8/E7): `CronJobModel`/manual triggers carry optional `garmin_username`/`divelogs_username`; status page and desktop credentials forms take repeatable account rows instead of one account each; cron rows, the status-page "Sync now" trigger and desktop `SyncPage.qml` all show an always-visible account dropdown. Closes gaps 1.21 / 2.7 / 4.5 / 5.17 |
| B9 | Per-account last-sync status on the status page | | ✅ | 2026-09-21 (rework.md A7): `scheduler.last_sync_results` keyed by job id; status page shows one row per job. Closes 4.10 |
| B10 | Fix README `setup.py` reference and refresh `DOCKERHUB.md` feature list | | ✅ | 2026-09-22 |
| B11 | Per-field sync rules defined by **linking fields** on a drag-and-drop mapping board (same board on the status page and in the desktop app): each link has a direction (bidirectional / to_target / to_source / off) and a conflict policy (source_wins / target_wins / prefer_non_empty / prefer_source / manual); adapters publish a field catalogue; manual conflicts queued to `conflicts.json` and resolved in either UI | | ✅ | Engine, templates, conflict queue, CLI, the status-page board with Test mapping, the desktop board (`MappingPage.qml`) and README docs (rework.md C6/C7/C8/C20) all done 2026-09-22. Touches 1.11–1.14, 4.6, 5.2. Superseded on the board by receiver rules, see B19 (2026-09-23) |
| B13 | Sync with Submersion by joining its cloud changeset log as a peer device (S3-compatible bucket, Dropbox or iCloud folder; Google Drive is not reachable by third parties). UDDF file exchange as fallback | | 🚧 | Codec, store/merge and peer adapter done 2026-09-22 (rework.md F10–F12): `submersion` spec, reads/writes an S3 or folder store, byte-verified against a real export. UI config done 2026-09-22 (F13: status page + desktop). End-to-end encrypted libraries supported too, done 2026-09-22 (rework.md E11): a passphrase unlocks the cloud keyslot file, verified byte-for-byte against Submersion's own crypto test vectors. **Scope settled 2026-09-24 (rework.md F17): metadata only.** The depth profile, cylinders, tank pressures, gas switches and data sources belong to the `.fit` import the diver runs in the Submersion app; dive_sync syncs the dive row, its site, buddies and the B20 extras onto the dive that import created, and only creates dives that will never be imported (hand-logged Garmin ones). Pending: confirm a real device consumes our base, incremental changesets, heartbeat |
| B14 | Generalise `SyncEngine` from a fixed Garmin/Divelogs pair to any two adapters with a `service_id` | | ✅ | 2026-09-21, rework.md F1. `SyncEngine(source_adapter=, target_adapter=)`; results keyed by service id; `engine.garmin`/`engine.divelogs` kept as aliases. Backup and raw download stay Garmin/Divelogs-only until F5 |
| B15 | Portable sync profile: export/import the full sync configuration (rules, filters, pairs, cron jobs, never credentials) as a versioned JSON file from the desktop app, the status page and the CLI, so config authored on one machine can be loaded on the other | | 🚧 | Core + CLI (rework.md C9) and status page (C10, 2026-09-22) done; desktop (C11) pending |
| B16 | Composite fields: a target that exists on one side only (Garmin activity name) is built from several source fields with a user-defined `{key}` template, with live preview; optional reverse parsing to make it bidirectional | | ✅ | Renderer, validation, loop detection and `POST /api/fields/preview` done 2026-09-21 (rework.md C18); board editor done with C6/C7; reverse parsing (`reverse` regex, text sources only) done 2026-09-22 (rework.md C21). The shipped `site_to_garmin` composite is off by default. Superseded on the board by receiver rules, see B19 (2026-09-23) |
| B17 | Mapping board extras: ordered match keys chosen on the board, read-only 'Test mapping' against the newest 10 live dives, apply-to-all prompt after saving, reset to defaults | | ✅ | 2026-09-22 on the status page (rework.md C19/C20/C6); the desktop board follows Track D |
| B18 | Conditional links (`when` on a FieldLink, e.g. only for dive type training) | | 📝 | Later; field reserved in the model on 2026-09-21, no UI planned yet |
| B19 | Receiver-centric sync rules: each pair holds "what service X accepts from Y" and "what Y accepts from X" (rule = receiver field ← sender field(s), policy), replacing per-link `direction`; match keys become a pair-level list; boards become two receiver columns with locks on unwritable fields | | ✅ | Planned 2026-09-23 (rework.md Track G). Keeps today's five policy names; a run has exactly one receiver (`bidirectional` dropped as a run mode, G0 ✅ 2026-09-23 - a two-way sync is two directed runs; deletes follow the direction), so no tiebreak is needed. G1 ✅ 2026-09-23: `SyncRule` / `sync_pairs[].rules` / `match_keys` stored (settings v2), `garmin_divelogs` is the explicit default pair, lossless link↔rule converters keep both boards working, profile v2. G2/G3 ✅ 2026-09-23: the engine applies the receiver's rules natively (`target_wins` = never overwrite, also on uploads), `--show-mapping` / `--test-mapping` are receiver-centric. G5/G6 ✅ 2026-09-23: both boards rebuilt as receiver panels with a match-key strip. G7 ✅ 2026-09-23: docs in rules vocabulary, the API's link views retired (a version-1 `field_links` payload is still accepted). Owner's hands-on pass over both boards still open |
| B20 | Submersion's fuller record: the dive row's boat, operator, entry/exit times and methods, dive mode/type, altitude, air temperature, and the site row's region/country/island/city/notes - mappable like any other field | | ✅ | 2026-09-23 (rework.md G9), 22 fields from the owner's pass over `docs/submersion_field_inventory.md`. The site hierarchy is what gives Divelogs' `location` a Submersion counterpart; sparse until filled in the Submersion app |
| B12 | Migrate desktop app from Toga to PySide6 / Qt Quick (QML) | | 🚧 | Started 2026-09-22: Qt shell, controllers and all six pages (Sync, Garmin/Divelogs dives, Mapping, Conflicts, Settings) in `desktop/`, Toga removed. Editable tanks/GPS/water temperature done 2026-09-23 (rework.md D4) - also fixed two real Garmin write bugs found along the way (a GPS write that silently failed on real dives, and lat/lng needing to be sent together). Open: a hands-on pass with the packaged app (D9), which needs the owner |

**Adding an entry:** give it the next `B` number, one line describing the user-visible outcome, and a note on which existing feature rows it touches.
