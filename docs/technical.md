# How it works (architecture)

This document provides a comprehensive overview of the `dive_sync` project. It is intended to onboard developers and AI coding assistants to the codebase; user-facing docs are the other pages in [`docs/`](README.md).

---

## 🎯 Project Overview
`dive_sync` is a two-way dive log synchronization tool (one direction per run) that connects **Garmin Connect**, **Divelogs.org**, **Subsurface** (cloud, git checkout, UDDF), the **Shearwater app** and, in beta, **MySSI**. It allows users to:
1. Fetch dive logs from both platforms.
2. Normalize logs into a unified format.
3. Match identical dives across services.
4. Synchronize data differences (gases, weights, buddies, notes, GPS coordinates) and upload new dives.
5. Run scheduled or CLI-triggered sync jobs, with a read-only status page for monitoring plus schedule/credential configuration.
6. Edit and convert dive-computer files offline in the desktop app's Dive edit page (Garmin `.fit`, UDDF or Subsurface `.ssrf` in; every field editable, a start-time change moving the profile; UDDF or `.ssrf` out, a Garmin dive back as `.fit` with its own file patched in place or any other dive as a new `.fit` written with Garmin's FIT SDK, and a dive the computer split into several `.fit` logs merged into one), with no account involved (rework.md Track I).

---

## 💻 Tech Stack
* **Language**: Python 3.10+
* **Backend Framework**: FastAPI (with Uvicorn)
* **Libraries**: `python-garminconnect` (for Garmin Connect API integration), `requests` (for Divelogs.org API), `pydantic` (for data validation), `fitdecode` (reads Garmin `.fit` files on the Dive edit page; MIT), `garmin-fit-sdk` (Garmin's own FIT SDK, under its FIT Protocol License; its encoder writes a `.fit` from a dive that did not come from one, `src/core/convert/fit_writer.py`, the one shipped module that imports it)
* **Frontend**: HTML5, Vanilla JavaScript, CSS (via TailwindCSS CDN)
* **Testing**: Pytest

---

## 📁 Directory Structure
```
├── src/
│   ├── core/                  # Core domain logic
│   │   ├── services/          # Adapters: Garmin, Divelogs, UDDF, Subsurface (git storage + cloud), Shearwater app, MySSI, mocks
│   │   │   └── ssi.py         # MySSI client (the MySSI app's own backend, unofficial - UNOFFICIAL_NOTE / BETA_NOTE) + SsiAdapter, a sync side since plans/myssi-sync.md (spec ssi[:email], credentials.json "ssi" list / the keychain): add-only (accepts_updates False until the update test, accepts_mirror_deletes False for good, cannot_add_reason refuses a dive with two or more tanks); one get_divelog reads the whole logbook, writes keep a 2 s spacing
│   │   ├── convert/           # The Dive edit page's core (rework.md Track I; the page was "Convert" until I14), pure - no adapter, settings or credentials: fit_reader.py (Garmin .fit on fitdecode), formats.py (registry: detection by extension/content, read/write entry points, dialog filters, "<date> <time> dive <n>.<ext>" names, per-format drop lines), ssrf_writer.py / ssrf_reader.py (Subsurface XML), enrich.py (fill a FIT's dive from the cached Garmin dive with the same activity id, or the same start time when the name holds no id; the selected account first), edit.py (shift_dive: a new start moves every sample and event with it; apply_edits: validated field edits, the tank table, the zone as a UTC offset), fit_patch.py (Save as .fit: the opened .fit patched field by field at the byte level - walker and timestamp shift ported from UWMedia's fit_time.py, CRCs recomputed, every other byte kept; no encoder, no Garmin SDK), fit_merge.py (Merge: two or more Garmin .fit logs of one split dive joined into one .fit at the byte level - the first log's messages kept, the later logs' samples, events and tank readings appended, summaries patched, gaps filled or closed; the dive computer serial from file_id or, failing that, the creator device_info, a log without one merging with the serial the caller chooses written into the first log's file_id and creator device_info, I20; UWMedia's fit_merge.py ported, no encoder, no SDK), fit_writer.py (Save as .fit for a dive that did not come from a .fit: a Garmin dive activity encoded from scratch with garmin_fit_sdk's Encoder - file_id, device_info, dive_settings, dive_gas, records with tank readings, events, tank summaries and transmitter profiles, dive_summary, lap, session, activity with the local timestamp; UWMedia's fit_writer.py ported; the one shipped module importing the SDK)
│   │   ├── pairs.py           # Service specs (garmin, divelogs, subsurface-cloud, uddf:<file>, subsurface:<dir>, shearwater[:<file>], ssi[:<email>]) -> adapters and engines; sync_pairs, default boards, the Download / endpoint lists
│   │   ├── site_matcher.py    # Dive-site resolution by name / nearest within 200 m
│   │   ├── mapping/           # Declarative JSONPath mapping files
│   │   ├── adapter.py         # BaseDiveAdapter abstract base class
│   │   ├── config.py          # Settings and credentials management (sync_pairs[].rules, the garmin_divelogs default pair, settings v2 migration, sync profile export/import)
│   │   ├── fields.py          # Field catalogue (FieldSpec), field links (FieldLink), value access, defaults, validation
│   │   ├── templates.py       # {key} templates for composite links: render, validate, loop detection, preview
│   │   ├── conflicts.py       # conflicts.json queue for links with the 'manual' policy
│   │   ├── layout.py          # The data folder layout: <service>/<account>/data|fit|tokens, sync/, backups/
│   │   ├── mapping_helper.py  # Mapping Engine that applies JSONPath rules
│   │   ├── units.py           # Metric/imperial conversion, labels and rounding for display only (Track J); pure, reads no settings
│   │   ├── display.py         # Which catalogue fields are quantities and how a value is shown in a unit system; shared by desktop, CLI and web
│   │   ├── models.py          # Unified Dive schemas (Pydantic)
│   │   ├── sync_engine.py     # Dive matching and link-driven synchronization engine (any source/target adapter pair)
│   │   └── scheduler.py       # Background schedule watcher + sync runner (used by web/app.py's lifespan)
│   └── web/                   # Status page (Docker-facing) — no dive editing
│       ├── static/            # Static assets (HTML, CSS, JS) for the status page
│       ├── prefs.py           # web_prefs.json: the dashboard's own units choice (app-local)
│       └── app.py             # FastAPI app: status/logs, credentials, schedule config; SSE logging setup
├── tests/                     # Test suite
│   ├── data/                  # Static test fixtures
│   ├── test_sync.py           # Sync logic and engine tests
│   ├── test_mock_sync.py      # Dry-run and offline sync tests
│   ├── test_scheduler.py      # Scheduler extraction unit tests
│   ├── test_convert_*.py      # One module per src/core/convert file (fit, fit_patch, formats, ssrf, ssrf_reader, enrich, edit); test_ssi.py for services/ssi.py; the page itself is in test_desktop_qt.py
│   ├── test_web_api.py        # Status page API endpoint integration tests
│   └── test_cli.py            # sync.py flags as subprocesses against a scratch DATA_DIR and the offline mock adapters
├── data/                      # DATA_DIR when unset (auto-created); layout.py
│   ├── garmin/<account>/      # data/ (cached dive JSON), fit/ (.fit files), tokens/ (login tokens)
│   ├── divelogs/<account>/    # data/ (cached dive JSON)
│   ├── subsurface/<account>/  # data/ (cached dives), cloud/ (Subsurface Cloud checkout)
│   └── ssi/<account>/         # data/ (cached MySSI dives; the account is its email)
├── sync.py                    # Sync Engine Command Line Interface (CLI)
└── settings.json              # Local application settings
```

---

## ⚙️ Core Concepts & Workflows

### 1. Dive Normalization
All service-specific dive logs are mapped into the `UnifiedDive` model (defined in [models.py](../src/core/models.py)) which defines standard units (metric: meters, Celsius, kilograms, bar). Everything stored, synced or written to a file stays in those units; a metric/imperial choice converts only what is shown (`src/core/units.py`, `src/core/display.py`), and is app-local: `desktop_prefs.json` for the desktop app, `web_prefs.json` for the web dashboard, `--units` for one CLI run - never in `SettingsModel` or the sync profile.
* The **GarminAdapter** maps Garmin Connect nested JSON payloads to the unified model. A new dive is posted as a manual activity (summary only), or - with `garmin_fit_upload` on for the run (settings, pair, job, `--garmin-fit-upload`, the Sync page's box; beta, plans/gap-roadmap.md R13) and a profile on the dive - written with `convert/fit_writer.encode_fit` and sent through garminconnect's `upload_activity` (Connect's upload service); the new id is read from the answer's `detailedImportResult.successes`, or, while Connect is still processing (202), found among the newest diving activities by start time; then `update_dive` sets what the file cannot hold (name, notes, buddy, weight, visibility, location). Every request keeps `cooldown_seconds` apart.
* The **DivelogsAdapter** maps Divelogs.org JSON payloads to the unified model. Every add and update sends the whole dive (`_map_from_unified`); the dive's `airtemp` is `air_temp` (R10b; 0 = none on both read and write, °F on an imperial account like `depthtemp`), and `surfacetemp` is still neither read nor sent (its entry in `garmin_to_divelogs.json` has no handler).
* The mappings are augmented declaratively using JSONPath configuration files in [src/core/mapping/](../src/core/mapping).

### 2. Dive Matching Logic
Dives are linked in [SyncEngine.match_dives](../src/core/sync_engine.py) using a tiered system (tiers 1-4 below; the order they run in is 1, 3, 2, 4):
1. **Tier 1: Known pairs**: a dive that carries the other service's id in `external_ids` (adapters with `stores_external_ids`), or a pair remembered in `sync_state.json` (`links`) from an earlier match or upload. Garmin and Divelogs have no field for a foreign id, so for them the state file is the only link store. A link made by an upload is written to the file the moment the upload succeeds (`SyncEngine._save_links_now`, check.md B5), so a run that breaks off afterwards - an exception from the next dive, a stopped or killed process - keeps the pairs it made and the next run does not create those dives again; `last_sync_time` is written only by a run that finishes (or kept where it was by `keep_stopped_run` after a Stop), so the dives not reached still count as new.
2. **Tier 2: Match keys from the mapping board**: the pair's ordered `match_keys` (number or datetime fields only; shown as `match_order` on the link view) are tried in order. The generic default board for pairs where both services let the user number dives flags the dive number, which reproduces the old "same positive dive number" rule.
3. **Tier 3: Naive Timestamp Match**: Matches dives if their start times fall within the configured `grace_window_minutes` (default is 15 minutes): on the UTC instants when both dives carry `date_time_utc` (Garmin, FIT, a UDDF datetime with a non-zero offset), on the naive local times otherwise. Within a tier the closest start times pair first; candidates whose distances fall in the same minute go by `profile_mismatch` (max depth and duration, each in units of its tolerance) before list order. Tier 3 runs before tier 2.
4. **Tier 4: Time-zone shift** (`SyncEngine._pair_across_zone_shift`, plans/gap-roadmap.md R11): of the dives still unmatched, a pair whose start times differ by a whole number of quarter hours (30 min to 14 h, within 3 min - `zone_shift_minutes`) and whose max depth and duration agree (`profiles_agree`: 0.5 m or 3 %, 90 s or 3 %, both values known on both sides). A candidate is paired only when its shift is shared by strictly more candidates than the shift of any rival candidate of either dive (a trip's dives all off by the same hours pair with their twins; an even tie pairs nothing); each match and each dive left out is logged. The constants are at the top of `sync_engine.py`.


### 2b. Field catalogue, rules and links (what happens on a matched pair)
Every adapter declares a `service_id` and a `field_catalog()` of `FieldSpec`s (key `<service_id>.<name>`, type, writability). The user's mapping is stored per pair as **receiver rules** (`SyncPairModel.rules`, a `SyncRule` list per receiving service: receiver field ← sender field(s), policy, optional template/reverse; rework.md Track G) plus the pair's ordered `match_keys`. The Garmin ↔ Divelogs pair is the explicit `garmin_divelogs` entry, always `sync_pairs[0]`; `SettingsModel.directionality` / `.field_links` and `SyncPairModel.field_links` are compatibility views over it. `SyncEngine` resolves its pair from settings (`pair_id`, else by the two service ids, else a transient default pair) and applies the rules natively (G2): `active_rules()` is the run's receiver's list (minus `target_wins`, which means never-overwrite) plus the reverse splits of the other side's composites, and `_apply_rule` reads every policy from the receiver's side (`source_wins` always takes the sender's value, `prefer_source` when the sender has one, `prefer_non_empty`/`manual` only fill a blank, `manual` queues a real disagreement). Both boards edit the rules directly (G5/G6). The **link view** (`fields.rules_to_links`, a list of `FieldLink`s: source field(s) → target field, direction, conflict policy) remains as the form save-time validation runs on (`templates.validate_links`), as what a version-1 settings file, profile or API payload is converted from (`fields.links_to_rules`), and for a cron job's optional per-job board; the two are lossless for every board the link model can express (`tests/test_rules.py`). The shipped default board (`fields.default_field_links()`) reproduces the pre-Track-C behaviour exactly. Fields with no `UnifiedDive` attribute (Garmin `activityName`/`locationName`, Divelogs `location`/`divesite`) live in `UnifiedDive.service_fields`. A link with several sources renders its target from a `{key}` template (`templates.py`); links also run on uploads of new dives (`SyncEngine.prepare_upload`). A link with policy `manual` records real conflicts to `conflicts.json` (`conflicts.py`) instead of overwriting; `SyncEngine.resolve_conflict` pushes the chosen side. `SyncEngine.test_mapping` rehearses a board read-only on the newest dives. The whole configuration travels as a versioned *sync profile* (`config.export_profile` / `import_profile`). Design and open steps: `rework.md` Track C.

### 2c. What a receiver can take (adapter flags)
`BaseDiveAdapter` carries class flags the engine reads with `getattr(..., True)`, so an adapter that takes everything declares nothing: `accepts_new_dives` (False: the engine logs and counts the new dives it would have uploaded instead of erroring per dive), `accepts_updates` (False: on a matched pair whose receiver is this adapter the rules still run - so the run reports what differs - but nothing is written; one log line per differing dive, counted under `skipped` as `receiver_takes_no_updates`, and a `manual` disagreement queues no conflict for that receiver, since a resolution would be the update it cannot take), `accepts_mirror_deletes` (False: no sync run deletes on this side - a Mirror run logs each unmatched dive it would have deleted and leaves it, counted as `mirror_never_deletes_here`, and the propagate-deletes path, which Mirror turns on, logs each linked dive gone from the sender, keeps the link and the dive and counts it as `sync_never_deletes_here`; the name dates from when only Mirror deleted), the hook `as_stored(field_type, value)` (the sender's value as this service would store and read it back; the engine compares that form, so what the service cannot hold is not reported as a change on every run - MySSI drops tank names and puts a profile on its own time grid; default: unchanged), the hook `cannot_add_reason(dive)` (a string refuses one upload with its reason, counted as `receiver_cannot_add_dive`; the engine never calls `add_dive` for it), the hook `upload_details(dive)` (how `add_dive` would send this dive, asked on the prepared dive before every upload and in a dry run too: an optional `note` logged under the upload line, the other keys put on the dive's `uploaded_to_<svc>` entry; Garmin's `.fit` upload, plans/gap-roadmap.md R13, says `upload_as: "fit"` and `fit_drops`; default `{}`), and the attribute `add_skipped_reason` (set by `add_dive` when it returned None because the service already has the dive - Divelogs' "already exists" 400, MySSI's duplicate check, Garmin's "duplicate activity" answer to a `.fit` upload; the engine clears it before each call and counts a None with a reason as `receiver_already_has_dive`, a None without one as `failed`). MySSI (`SsiAdapter`) is the first user: add-only until the owner's live update test (plans/myssi-sync.md K6/K7 flip `accepts_updates`), never deleted on by a sync run (a MySSI delete is permanent; only the MySSI Dives page deletes there), and a dive with two or more tanks refused until the second tank's keys are known (plans/convert.md I7b). Its catalogue marks the fields `build_payload` fills on a new dive writable, so the board maps them and `prepare_upload` renders them; the dive number and site stay read-only. MySSI stores no foreign id, but the dive-computer reference DiveSync sends (`garmin:<activity id>`) is read back into `external_ids`, which is tier 1 of the matching without any engine change. The desktop Dives page reads the same flags (`DivesController.acceptsUpdates`) to make the MySSI editor read-only.

`run_sync` returns one dict per run: `uploaded_to_<svc>`, `updated_on_<svc>`, `deleted_on_<svc>` (what was done, or would be in a dry run, each entry with `dry_run`), `conflicts`, `skipped` (`{"reason", "time", ...}`; reasons: `create_on_garmin_off`, `receiver_takes_no_new_dives`, `receiver_cannot_add_dive` (with `detail`), `receiver_already_has_dive` (with `detail`), `receiver_takes_no_updates`, `mirror_never_deletes_here`, `sync_never_deletes_here`) and `failed` (`{"action": "update"|"add", "service", "id", "time", "reason"}`: `update_dive` returned False, or `add_dive` returned None without a skip reason - the write was tried and not taken, so it is never listed as done; one warning per entry). `run_history.count_results` counts every non-empty bucket for the dashboard's listings, the CLI's `print_sync_summary` prints them, and the desktop Sync page's status line names the failed count.

### 3. Scheduling
`src/core/scheduler.py` holds `scheduler_loop()` (an asyncio loop started from `web/app.py`'s FastAPI `lifespan`), `run_sync_thread()`, and `get_next_scheduled_run()`. It reads `SettingsModel.schedule`/`cron_jobs` (via `ConfigManager.load_settings()`) every minute and spawns a background thread calling `SyncEngine.run_sync()` when a job is due. This module has no FastAPI dependency, so it can be reused by non-web entry points. `run_sync_thread` posts to `notify_url` (`src/core/notify.py`): a failure alert when the run raises, and a one-line summary of its counts (`notify.run_summary`) after a finished run when `notify_on` is `always`, or `changes` and the run wrote, failed a write or raised a conflict (never for a dry run) - each best-effort, never failing the run.

### 4. Dive editing
Dive editing lives in the desktop app only (`desktop/controllers/dives.py` and `DivesPage.qml`, one editor for every service): edits and deletes are staged and pushed through `adapter.update_dive()` / `adapter.delete_dive()`. The Dive edit page (`desktop/controllers/convert.py`, `ConvertPage.qml`) edits dives read from files in memory, through `src/core/convert/edit.py`, and writes them with Save as; the two editors share the no-drift unit helpers in `desktop/controllers/editing.py`. The web dashboard's former editing UI was removed in the desktop-app migration (`rework.md` Track B/D) and the dashboard syncs only.

---

## 🚀 Key Commands

### Running the Status Page
Starts the FastAPI status page with hot-reloading at `http://127.0.0.1:8000`:
```bash
uvicorn src.web.app:app --reload
```

### Running the CLI Sync Engine
```bash
# Perform an incremental sync in the saved direction (one receiver per run; two-way = two runs)
python sync.py
python sync.py --direction to_garmin

# Perform a dry-run sync without updating remote services
python sync.py --dry-run

# Download all raw data from both services to local cache
python sync.py --save-raw-data
```

### Running the tests
```bash
pip install -r requirements-dev.txt   # once; the test-only dependencies
./run_tests.sh
```
Garmin's own `garmin-fit-sdk` is a regular dependency since the Dive edit page writes `.fit` files with its encoder (plans/convert.md I12; the owner accepted its FIT Protocol License on 2026-10-18), so the tests that cross-check the Garmin FIT fixtures with Garmin's decoder run in every install (their `importorskip` guards stay, harmlessly). FIT files are still read with `fitdecode` (MIT), and the patcher and the merge are byte-level: `src/core/convert/fit_writer.py` is the only shipped module that imports the SDK (`tests/test_convert_fit_writer.py` holds it to that). Note that the SDK's wheel installs a top-level package named `tests` into site-packages; `tests/__init__.py` explains why the repo's folder still wins.


---

## 💡 Developer Guidelines
* **Abstract Base Client**: Do not import `GarminAdapter` or `DivelogsAdapter` directly outside the service layers or background runners. Always interact through the `BaseDiveAdapter` interface. `SyncEngine` itself only knows a `source` and a `target` adapter; `engine.garmin` / `engine.divelogs` are aliases.
* **Field catalogue first**: a field the engine should compare or copy must be declared in the adapter's `field_catalog()` and reachable through `fields.get_field` / `set_field`; never add a hardcoded field comparison to `run_sync`.
* **Declarative Mapping**: Avoid hardcoding field translations. If a new API field needs to be mapped, update the mapping definitions in `divelogs_to_garmin.json` or `garmin_to_divelogs.json`.
* **Testing Changes**: When adding new API routes or synchronization rules, always write corresponding test cases in the `tests/` directory and ensure `./run_tests.sh` executes successfully.
