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
| 1.7 | Three-tier dive matching: external-ID link → match keys from the mapping board (default: dive number) → timestamp within grace window (default 15 min) | ✅ | `sync_engine.py:match_dives`; tier 2 iterates `field_links` with `match_order` set (rework.md C19) |
| 1.8 | Direction control: bidirectional / to_garmin / to_divelogs | ✅ | settings + CLI/API/desktop override |
| 1.9 | Upload unmatched dives to the other service | ✅ | Both directions; Divelogs "already exists" responses are skipped, not errored |
| 1.10 | Cross-link matched dives by writing each service's ID into the other | ✅ | Garmin ID stored on Divelogs dive and vice-versa |
| 1.11 | Field-level update of matched dives, driven by `field_links` in settings (direction + conflict policy per link) | ✅ | `sync_engine.py:_apply_link`; default board = buddy, notes, weight (+unit), visibility (+unit) bidirectional with Garmin winning conflicts, exactly the old behaviour. Edit `settings.json` (`field_links`) or `POST /api/settings` until the board UI ships (B11) |
| 1.12 | GPS coordinates sync | 🚧 | Default links `gps` (Garmin→Divelogs always) and `gps_fill` (Divelogs→Garmin only when Garmin has no coordinates); both editable |
| 1.13 | Depth/temperature profile samples sync | 🚧 | Default link `samples`, Garmin→Divelogs only (Garmin cannot write samples) |
| 1.14 | Gas mixtures / tank pressures / tank volume sync | 🚧 | Default link `tanks`, Garmin→Divelogs only. Divelogs→Garmin gas/tank updates are not supported (Garmin's gas structures are restricted). `sync_gases` off disables every tanks link |
| 1.15 | Garmin tank-sensor telemetry fetch | ✅ | `/diving/v1/dive/detail/tanksensor` per activity; 404 tolerated |
| 1.16 | Garmin update pushes only changed fields (partial PUT) | ✅ | Location, notes, dive number, buddy, weight, visibility, GPS. Samples/gases are never pushed to Garmin |
| 1.17 | Incremental sync ("only new" since last run, minus 1-day grace) | ✅ | State in `sync_state.json`; `--full-sync` forces full |
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
| 3.3 | `--backup` with `--garmin-path` / `--divelogs-path` | ✅ | |
| 3.4 | `--save-raw-data [dir]` and `--overwrite` | ✅ | |
| 3.5 | `--mock-data-dir [dir]` offline sync | ✅ | |
| 3.6 | Interactive credential setup with live login verification | ✅ | `setup_credentials.py` (README still calls it `setup.py`) |
| 3.7 | `DATA_DIR` env var relocates settings, credentials, tokens, caches | ✅ | Used by Docker and desktop |

## 4. Docker status page (`src/web/`, `docker_run.py`, `Dockerfile`)

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 4.1 | Single-process container: uvicorn + scheduler loop, graceful SIGTERM | ✅ | `docker_run.py` |
| 4.2 | Status view: running flag, last result, next scheduled run | ✅ | `GET /api/status` |
| 4.3 | Manual "Sync now" with dry-run toggle (409 if already running) | ✅ | `POST /api/sync/trigger` accepts direction/filter overrides |
| 4.4 | Live log tail via Server-Sent Events | ✅ | `GET /api/logs/stream` |
| 4.5 | Credentials form: save + test Garmin/Divelogs login | 🚧 | `POST /api/credentials` writes a single account and overwrites any multi-account list |
| 4.6 | Default sync settings form (direction, grace window, cooldown, only_new, sync_gases, sync_fit) | ✅ | `GET/POST /api/settings`; the payload also carries `field_links` (validated on save, kept when omitted). `GET /api/fields` serves the catalogues per pair for the coming board |
| 4.7 | Scheduled jobs table: add / edit / remove cron jobs | ✅ | |
| 4.8 | Version + update-available banner | ✅ | `GET /api/version` |
| 4.9 | Dive editing / cache explorer in the web UI | ❌ | Removed on purpose (moved to desktop app) |
| 4.10 | Per-account last-sync status | ❌ | Only one global `last_sync_results` |
| 4.11 | Multi-arch Docker Hub image (amd64/arm64) built on GitHub Release | ✅ | `.github/workflows/docker-release.yml`, syncs `DOCKERHUB.md` |

## 5. Desktop app (`desktop/`, Briefcase + Toga)

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 5.1 | Sidebar shell with sections: Sync, Garmin Dives, Divelogs Dives, Settings | ✅ | Fixed-size window sized from the screen |
| 5.2 | Sync section: direction, dry-run / only-new / gases / FIT switches, "Sync now", progress bar, live log | ✅ | Reuses `scheduler.run_sync_thread` |
| 5.3 | "Download dives" (raw cache refresh) with overwrite switch | ✅ | |
| 5.4 | Dive editor tables (Date, Time, Dive #, Location, Max Depth, Duration, Buddy, Weight, Visibility) | ✅ | Shared `DiveEditorSection` for both services |
| 5.5 | Per-tab Refresh that downloads only that service's dives, with live status line | ✅ | |
| 5.6 | Column chooser and sort column/direction, persisted per service | ✅ | `desktop_prefs.json` in app data dir |
| 5.7 | Detail form: edit dive #, date, time, duration, max depth, location, notes, weight, visibility, buddy; avg depth and water temp read-only | ✅ | Save writes local cache then pushes to remote |
| 5.8 | Delete dive (confirm dialog, local + remote) | ✅ | |
| 5.9 | Edits/deletes blocked while a sync or download runs | ✅ | |
| 5.10 | Keychain-backed credentials (`keyring`) with Test buttons and first-run landing on Settings | ✅ | Password fields never prefilled |
| 5.11 | Credentials and Garmin token materialised to disk only for the duration of an operation | ✅ | `begin_operation` / `end_operation`; `atexit` safety net |
| 5.12 | Private per-user data dir via `platformdirs` | ✅ | |
| 5.13 | Telemetry graphs (depth / temperature / gas) | 📝 | Phase 2 in `rework.md` |
| 5.14 | Windows / Linux Briefcase targets | 📝 | macOS only today |
| 5.15 | Code signing / notarisation | 📝 | |
| 5.16 | Built-in scheduler in the desktop app | ❌ | By design; scheduling stays in Docker |
| 5.17 | Multi-account selection in the desktop app | ❌ | Single account only |

## 6. Testing and tooling

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 6.1 | Pytest suite (`./run_tests.sh`): sync logic, mock sync, field catalogue/links, link-driven engine, scheduler, dive cache, web API, desktop credentials and preferences | 🚧 | 95 tests across 9 files; 4 in `test_mock_sync.py` skip unless real downloaded fixtures `488.json`/`502.json` exist under `data/`. `test_link_engine.py` replays the pre-Track-C loop as a reference over 400 seeded cases |
| 6.2 | Live-API integration tests against real Garmin/Divelogs | ❌ | All tests are mocked/offline |

---

## 7. Known limitations (from README, rework.md and code review)

- Beta quality. Garmin→Divelogs is well exercised; Divelogs→Garmin much less so. Keep backups.
- Divelogs→Garmin gas/tank data is not written on matched-dive updates.
- Profile samples flow only Garmin→Divelogs.
- In bidirectional mode Garmin wins every field conflict (buddy, notes, weight, visibility) on the **default** mapping board, and site names are not compared on matched dives. Per-link direction and conflict policy exist in `settings.json` (`field_links`) since 2026-09-21, but there is no UI to edit them yet (B11).
- Docker and desktop syncing the same account from different machines have no shared lock; back-to-back runs can hit Garmin rate limits.
- Multi-account works only from the CLI. Scheduler, status page and desktop app assume one account each.
- `sync_fit` only affects `--backup`.
- Docs drift: README refers to `setup.py` (file is `setup_credentials.py`); `DOCKERHUB.md` still advertises the removed web spreadsheet editor and telemetry graphs.

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
| B7 | Sync with Subsurface (native `.ssrf` XML and Subsurface Cloud git storage) | | 📝 | todo_txt: "garmin to subsurface"; plan in rework.md Track F |
| B8 | Account selection for scheduled jobs, status page and desktop app | | 📝 | Closes gaps 1.21 / 2.7 / 4.5 / 5.17 |
| B9 | Per-account last-sync status on the status page | | 📝 | Closes 4.10 |
| B10 | Fix README `setup.py` reference and refresh `DOCKERHUB.md` feature list | | 📝 | Docs only |
| B11 | Per-field sync rules defined by **linking fields** on a drag-and-drop mapping board (same board on the status page and in the desktop app): each link has a direction (bidirectional / to_target / to_source / off) and a conflict policy (source_wins / target_wins / prefer_non_empty / manual); adapters publish a field catalogue; manual conflicts queued to `conflicts.json` and resolved in either UI | | 🚧 | Engine side done 2026-09-21 (rework.md C1, C2, C3, C19, most of C17): catalogue, links in settings, table-driven loop, policies, match keys, `GET /api/fields`. Pending: templates (C18), conflict queue (C4), CLI (C5), board UI (C6/C7), docs (C8). Touches 1.11–1.14, 4.6, 5.2 |
| B13 | Sync with Submersion by joining its cloud changeset log as a peer device (S3-compatible bucket, Dropbox or iCloud folder; Google Drive is not reachable by third parties). UDDF file exchange as fallback | | 📝 | rework.md Track F steps F9–F13; format reference in `docs/submersion_sync_format.md` |
| B14 | Generalise `SyncEngine` from a fixed Garmin/Divelogs pair to any two adapters with a `service_id` | | ✅ | 2026-09-21, rework.md F1. `SyncEngine(source_adapter=, target_adapter=)`; results keyed by service id; `engine.garmin`/`engine.divelogs` kept as aliases. Backup and raw download stay Garmin/Divelogs-only until F5 |
| B15 | Portable sync profile: export/import the full sync configuration (rules, filters, pairs, cron jobs, never credentials) as a versioned JSON file from the desktop app, the status page and the CLI, so config authored on one machine can be loaded on the other | | 📝 | rework.md C9–C11 |
| B16 | Composite fields: a target that exists on one side only (Garmin activity name) is built from several source fields with a user-defined `{key}` template, with live preview; reverse parsing later | | 📝 | rework.md C18, C21; the Divelogs location/divesite comma join becomes a visible default composite link |
| B17 | Mapping board extras: ordered match keys chosen on the board, read-only 'Test mapping' against the newest 10 live dives, apply-to-all prompt after saving, reset to defaults | | 🚧 | Match keys (rework.md C19) done in the engine 2026-09-21; Test mapping (C20), apply-to-all and reset are board work |
| B18 | Conditional links (`when` on a FieldLink, e.g. only for dive type training) | | 📝 | Later; field reserved in the model on 2026-09-21, no UI planned yet |
| B12 | Migrate desktop app from Toga to PySide6 / Qt Quick (QML) | | 📝 | Rewrite `desktop/sections/` and `async_utils.py` only; `src/core`, credentials, paths, preferences unchanged. Briefcase supports PySide6. Unlocks QtCharts for B1 and removes the toga-cocoa quit-hook workaround (5.11) |

**Adding an entry:** give it the next `B` number, one line describing the user-visible outcome, and a note on which existing feature rows it touches.
