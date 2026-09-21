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
| 1.8 | Direction control: bidirectional / to_garmin / to_divelogs | ✅ | settings + CLI/API/desktop override |
| 1.9 | Upload unmatched dives to the other service | ✅ | Both directions; Divelogs "already exists" responses are skipped, not errored |
| 1.10 | Remember matched and uploaded pairs across runs | ✅ | Neither Garmin nor Divelogs has a field for a foreign id (the old "cross-link" wrote nothing); since 2026-09-21 pairs are kept in `sync_state.json` (`links`) and count as tier-1 matches. Adapters that can store ids set `stores_external_ids` |
| 1.11 | Field-level update of matched dives, driven by `field_links` in settings (direction + conflict policy per link) | ✅ | `sync_engine.py:_apply_link`; default board (2026-09-21) = buddy, notes, weight, visibility, GPS and site name bidirectional with `prefer_non_empty` (blanks filled, real conflicts logged, nothing wiped), tanks and profiles to Divelogs, activity name built for new Garmin dives. Old Garmin-wins board: `fields.legacy_field_links()`. Edit `settings.json` (`field_links`) or `POST /api/settings` until the board UI ships (B11) |
| 1.12 | GPS coordinates sync | ✅ | One bidirectional `gps` link, fill-blanks policy, half-metre tolerance on comparison |
| 1.13 | Depth/temperature profile samples sync | 🚧 | Default link `samples`, Garmin→Divelogs only (Garmin cannot write samples). Since 2026-09-21 profiles are resampled onto Divelogs' fixed `samplerate` grid (Garmin records irregular intervals) and compared on that grid |
| 1.14 | Gas mixtures / tank pressures / tank volume sync, with tank order and role | 🚧 | Default link `tanks`, Garmin→Divelogs only (Garmin's gas API is read-only, E4). Order preserved via list order everywhere; role (`backGas`/`stage`/`deco`/...) is a new `GasMixture.tank_role` field, round-trips through Submersion (native support) and partially through Subsurface (`diluent`/`oxygen`/`bailout`/`not used` only); Divelogs and Garmin have no role concept. Verified live 2026-09-22 including a real multi-tank Divelogs write. `sync_gases` off disables every tanks link |
| 1.15 | Garmin tank-sensor telemetry fetch | ✅ | `/diving/v1/dive/detail/tanksensor` per activity; 404 tolerated |
| 1.16 | Garmin update pushes only changed fields (partial PUT) | ✅ | Location, notes, dive number, buddy, weight, visibility, GPS. Samples/gases are never pushed to Garmin |
| 1.17 | Incremental sync ("only new" since last run, minus 1-day grace) | ✅ | State in `sync_state.json` (also holds the known pairs); `--full-sync` forces full |
| 1.18 | Date-range filters (`date_from` / `date_to`) | ✅ | Settings or CLI override |
| 1.19 | Dry-run mode (no remote writes, no state update) | ✅ | CLI `--dry-run`, status page toggle, desktop switch |
| 1.20 | API cooldown between requests (`api_cooldown_seconds`) | ✅ | Applied in both adapters; Garmin login 429 is logged with a wait hint |
| 1.21 | Multi-account credentials (list of Garmin and/or Divelogs accounts) | 🚧 | Supported by `CredentialsModel`; account selection only via CLI `--garmin` / `--divelogs`. Scheduler, status page and desktop app use the single configured account and raise if more than one is present |
| 1.22 | Per-account sync state and data directories | ✅ | `sync_state_<garmin>_<divelogs>.json`, `data/garmin/<user>/`, `data/divelogs/<user>/` |
| 1.23 | FIT file download from Garmin (`fetch_fit_file`) | 🚧 | Only used by `--backup` when `sync_fit` is on. `sync_fit` has no effect on a normal sync run |
| 1.24 | Full-history backup to flat JSON (`--backup`) | ✅ | `sync_engine.py:backup` |
| 1.25 | Raw data download / local cache (`download_and_save_raw_data`) | ✅ | Per-service scoping (`include_garmin` / `include_divelogs`), optional overwrite. Populates the files the dive editor reads |
| 1.26 | Local dive cache read/list/update/delete + remote push (`dive_cache.py`) | ✅ | Display formatting (duration in minutes, depth 2 dp, ISO `T` normalised), field edits written back into raw Garmin/Divelogs JSON shapes |
| 1.27 | Version check against GitHub latest release | ✅ | `version.py`, cached 1 h, `APP_VERSION` env |

## 2. Scheduler (`src/core/scheduler.py`)

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 2.1 | Background asyncio watcher, checks settings every minute | ✅ | No FastAPI dependency; reusable from any entry point |
| 2.2 | Cron-like jobs: hourly / daily / weekly / custom-minute interval | ✅ | `CronJobModel` — per-job direction, only_new, sync_gases, sync_fit, enabled, optional `field_links`. Per-job values are now passed as explicit `run_sync` overrides (they used to be lost to the settings reload) |
| 2.3 | Legacy daily time slots (`schedule: [{hour, minute}]`) | ✅ | Still honoured alongside cron jobs |
| 2.4 | Skip a job when a sync is already running | ✅ | Single global `is_sync_running` flag |
| 2.5 | Next-run estimate for status display | ✅ | `get_next_scheduled_run` |
| 2.6 | Background raw-data download runner (`run_download_thread`) | ✅ | Mirrors `run_sync_thread`; used by desktop app |
| 2.7 | Scheduled jobs for a specific account in multi-account setups | ❌ | Jobs always call `SyncEngine()` with no account override |

## 3. CLI (`sync.py`, `setup_credentials.py`)

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 3.1 | `python sync.py` bidirectional incremental sync with results summary | ✅ | |
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
| 4.6 | Default sync settings form (direction, grace window, cooldown, only_new, sync_gases, sync_fit) | ✅ | `GET/POST /api/settings`; the payload also carries `field_links` and `sync_pairs` (validated on save, kept when omitted) |
| 4.12 | Mapping board: per-pair drag-and-drop field linking, link editor (direction, conflict, match key, template with live preview), Save/Cancel/Reset, apply-to-all prompt, read-only Test mapping on the newest 10 dives | ✅ | 2026-09-22, rework.md C6/C20 |
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
| 5.2 | Sync section: pair picker, direction, dry-run / only-new / gases / FIT switches, "Sync now", progress bar, live log | ✅ | Reuses `scheduler.run_sync_thread` on a `QThread` |
| 5.3 | "Download dives" (raw cache refresh) with overwrite switch | ✅ | |
| 5.4 | Dive editor tables (Date, Time, Dive #, Location, Max Depth, Duration, Buddy, Weight, Visibility) | ✅ | Shared `DiveEditorSection` for both services |
| 5.5 | Per-tab Refresh that downloads only that service's dives, with live status line | ✅ | |
| 5.6 | Column chooser and sort column/direction, persisted per service | ✅ | `desktop_prefs.json` in app data dir |
| 5.7 | Detail form: edit dive #, date, time, duration, max depth, location, notes, weight, visibility, buddy; avg depth and water temp read-only | ✅ | Save writes local cache then pushes to remote |
| 5.8 | Delete dive (confirm dialog, local + remote) | ✅ | |
| 5.9 | Edits/deletes blocked while a sync or download runs | ✅ | |
| 5.10 | Keychain-backed credentials (`keyring`) for Garmin, Divelogs and Subsurface Cloud with Test buttons and first-run landing on Settings | ✅ | Password fields never prefilled |
| 5.11 | Credentials and Garmin token materialised to disk only for the duration of an operation | ✅ | `begin_operation` / `end_operation`; Qt `aboutToQuit` cleanup |
| 5.18 | Mapping board, conflicts view and profile export/import in the desktop app | ✅ | 2026-09-22, rework.md C7/C11/D6 |
| 5.12 | Private per-user data dir via `platformdirs` | ✅ | |
| 5.13 | Telemetry graphs (depth / temperature / gas) | 📝 | Phase 2 in `rework.md` |
| 5.14 | Windows / Linux Briefcase targets | 📝 | macOS only today |
| 5.15 | Code signing / notarisation | 📝 | |
| 5.16 | Built-in scheduler in the desktop app | ❌ | By design; scheduling stays in Docker |
| 5.17 | Multi-account selection in the desktop app | ❌ | Single account only |

## 6. Testing and tooling

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 6.1 | Pytest suite (`./run_tests.sh`): sync logic, mock sync, field catalogue/links, templates, conflicts, profiles, link-driven engine, CLI, scheduler, dive cache, web API, desktop credentials and preferences | 🚧 | 200 tests across 24 files; real anonymised Submersion and Subsurface Cloud fixtures under `tests/data/` (rework.md F9, F4); 4 in `test_mock_sync.py` skip unless real downloaded fixtures `488.json`/`502.json` exist under `data/`. `test_link_engine.py` replays the pre-Track-C loop as a reference over 400 seeded cases |
| 6.2 | Live-API integration tests against real Garmin/Divelogs (and Subsurface Cloud, Submersion store) | 🚧 | `tests/live/`, opt-in via `DIVE_SYNC_LIVE_DATA_DIR=<test data dir>`; read-only so far (rework.md E9) |

---

## 7. Known limitations (from README, rework.md and code review)

- Beta quality. Garmin→Divelogs is well exercised; Divelogs→Garmin much less so. Keep backups.
- Divelogs→Garmin gas/tank data is not written on matched-dive updates.
- Profile samples flow only Garmin→Divelogs.
- In bidirectional mode Garmin wins every field conflict (buddy, notes, weight, visibility) on the **default** mapping board, and site names are not compared on matched dives. Per-link direction and conflict policy exist in `settings.json` (`field_links`) since 2026-09-21, but there is no UI to edit them yet (B11).
- Docker and desktop syncing the same account from different machines have no shared lock; back-to-back runs can hit Garmin rate limits.
- Multi-account works only from the CLI. Scheduler, status page and desktop app assume one account each.
- `sync_fit` only affects `--backup`.
- Gases and tanks cannot be written to Garmin Connect through its API (creation or update); see rework.md E4 for the 2026-09-22 investigation.

---

## 8. Backlog / ideas

Seeded from `todo_txt` and `rework.md`. Add new requests here; move them into the tables above once scoped.

| # | Request | Priority | Status | Notes |
|---|---------|----------|--------|-------|
| B1 | Telemetry graphs in the desktop dive editor | | 📝 | rework.md phase 2 |
| B2 | Windows and Linux desktop builds | | 📝 | rework.md phase 2 |
| B3 | macOS signing and notarisation | | 📝 | rework.md phase 2 |
| B4 | Sync gas mixtures / tank data Divelogs→Garmin | | 📝 | todo_txt: "check how to handle dive gases" |
| B5 | Multi-tank data to Divelogs | | 📝 | todo_txt: "multi tank to divelogs - tank data" |
| B6 | More verification / test coverage, especially Divelogs→Garmin | | 📝 | todo_txt: "add more verification" |
| B7 | Sync with Subsurface (Subsurface Cloud git storage; `.ssrf` XML optional) | | ✅ | Git-storage adapter (rework.md F4) and Subsurface Cloud clone/commit/push (F6) done 2026-09-22: `subsurface:<dir>` for a checkout, `subsurface-cloud` for the account in `credentials.json`. Desktop UI for it is Track D |
| B8 | Account selection for scheduled jobs, status page and desktop app | | 📝 | Closes gaps 1.21 / 2.7 / 4.5 / 5.17 |
| B9 | Per-account last-sync status on the status page | | 📝 | Closes 4.10 |
| B10 | Fix README `setup.py` reference and refresh `DOCKERHUB.md` feature list | | ✅ | 2026-09-22 |
| B11 | Per-field sync rules defined by **linking fields** on a drag-and-drop mapping board (same board on the status page and in the desktop app): each link has a direction (bidirectional / to_target / to_source / off) and a conflict policy (source_wins / target_wins / prefer_non_empty / manual); adapters publish a field catalogue; manual conflicts queued to `conflicts.json` and resolved in either UI | | 🚧 | Engine, templates, conflict queue, CLI (2026-09-21) and the status-page board with Test mapping (2026-09-22, rework.md C6/C20) done. Pending: desktop board (C7, after Track D), docs (C8). Touches 1.11–1.14, 4.6, 5.2 |
| B13 | Sync with Submersion by joining its cloud changeset log as a peer device (S3-compatible bucket, Dropbox or iCloud folder; Google Drive is not reachable by third parties). UDDF file exchange as fallback | | 🚧 | Codec, store/merge and peer adapter done 2026-09-22 (rework.md F10–F12): `submersion` spec, reads/writes an S3 or folder store, byte-verified against a real export. UI config done 2026-09-22 (F13: status page + desktop). Pending: confirm a real device consumes our base, incremental changesets, heartbeat |
| B14 | Generalise `SyncEngine` from a fixed Garmin/Divelogs pair to any two adapters with a `service_id` | | ✅ | 2026-09-21, rework.md F1. `SyncEngine(source_adapter=, target_adapter=)`; results keyed by service id; `engine.garmin`/`engine.divelogs` kept as aliases. Backup and raw download stay Garmin/Divelogs-only until F5 |
| B15 | Portable sync profile: export/import the full sync configuration (rules, filters, pairs, cron jobs, never credentials) as a versioned JSON file from the desktop app, the status page and the CLI, so config authored on one machine can be loaded on the other | | 🚧 | Core + CLI (rework.md C9) and status page (C10, 2026-09-22) done; desktop (C11) pending |
| B16 | Composite fields: a target that exists on one side only (Garmin activity name) is built from several source fields with a user-defined `{key}` template, with live preview; reverse parsing later | | 🚧 | Renderer, validation, loop detection and `POST /api/fields/preview` done 2026-09-21 (rework.md C18); the shipped `site_to_garmin` composite is off by default; board editor (C6/C7) and reverse parsing (C21) pending |
| B17 | Mapping board extras: ordered match keys chosen on the board, read-only 'Test mapping' against the newest 10 live dives, apply-to-all prompt after saving, reset to defaults | | ✅ | 2026-09-22 on the status page (rework.md C19/C20/C6); the desktop board follows Track D |
| B18 | Conditional links (`when` on a FieldLink, e.g. only for dive type training) | | 📝 | Later; field reserved in the model on 2026-09-21, no UI planned yet |
| B12 | Migrate desktop app from Toga to PySide6 / Qt Quick (QML) | | 🚧 | Started 2026-09-22: Qt shell, controllers and all six pages (Sync, Garmin/Divelogs dives, Mapping, Conflicts, Settings) in `desktop/`, Toga removed. Open: editable tanks/GPS/water temperature (rework.md D4), a hands-on pass with the packaged app |

**Adding an entry:** give it the next `B` number, one line describing the user-visible outcome, and a note on which existing feature rows it touches.
