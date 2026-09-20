# Dive Sync Project Context & Architecture

This document provides a comprehensive overview of the `dive_sync` project. It is intended to onboard developers and AI coding assistants to the codebase.

---

## 🎯 Project Overview
`dive_sync` is a bidirectional dive log synchronization tool that connects **Garmin Connect** and **Divelogs.org**. It allows users to:
1. Fetch dive logs from both platforms.
2. Normalize logs into a unified format.
3. Match identical dives across services.
4. Synchronize data differences (gases, weights, buddies, notes, GPS coordinates) and upload new dives.
5. Run scheduled or CLI-triggered sync jobs, with a read-only status page for monitoring plus schedule/credential configuration.

---

## 💻 Tech Stack
* **Language**: Python 3.10+
* **Backend Framework**: FastAPI (with Uvicorn)
* **Libraries**: `python-garminconnect` (for Garmin Connect API integration), `requests` (for Divelogs.org API), `pydantic` (for data validation)
* **Frontend**: HTML5, Vanilla JavaScript, CSS (via TailwindCSS CDN)
* **Testing**: Pytest

---

## 📁 Directory Structure
```
├── src/
│   ├── core/                  # Core domain logic
│   │   ├── services/          # API Adapters (Garmin, Divelogs, Mocks)
│   │   ├── mapping/           # Declarative JSONPath mapping files
│   │   ├── adapter.py         # BaseDiveAdapter abstract base class
│   │   ├── config.py          # Settings and credentials management (field_links live here)
│   │   ├── fields.py          # Field catalogue (FieldSpec), field links (FieldLink), value access, defaults, validation
│   │   ├── mapping_helper.py  # Mapping Engine that applies JSONPath rules
│   │   ├── models.py          # Unified Dive schemas (Pydantic)
│   │   ├── sync_engine.py     # Dive matching and link-driven synchronization engine (any source/target adapter pair)
│   │   └── scheduler.py       # Background schedule watcher + sync runner (used by web/app.py's lifespan)
│   └── web/                   # Status page (Docker-facing) — no dive editing
│       ├── static/            # Static assets (HTML, CSS, JS) for the status page
│       └── app.py             # FastAPI app: status/logs, credentials, schedule config; SSE logging setup
├── tests/                     # Test suite
│   ├── data/                  # Static test fixtures
│   ├── test_sync.py           # Sync logic and engine tests
│   ├── test_mock_sync.py      # Dry-run and offline sync tests
│   ├── test_scheduler.py      # Scheduler extraction unit tests
│   └── test_web_api.py        # Status page API endpoint integration tests
├── data/                      # Local JSON cache directory (auto-created)
│   ├── garmin/                # Cached Garmin JSON dives
│   └── divelogs/              # Cached Divelogs JSON dives
├── sync.py                    # Sync Engine Command Line Interface (CLI)
└── settings.json              # Local application settings
```

---

## ⚙️ Core Concepts & Workflows

### 1. Dive Normalization
All service-specific dive logs are mapped into the `UnifiedDive` model (defined in [models.py](file:///Users/mikael/development/dive_sync/src/core/models.py)) which defines standard units (metric: meters, Celsius, kilograms, bar).
* The **GarminAdapter** maps Garmin Connect nested JSON payloads to the unified model.
* The **DivelogsAdapter** maps Divelogs.org JSON payloads to the unified model.
* The mappings are augmented declaratively using JSONPath configuration files in [src/core/mapping/](file:///Users/mikael/development/dive_sync/src/core/mapping).

### 2. Dive Matching Logic
Dives are linked in [SyncEngine.match_dives](file:///Users/mikael/development/dive_sync/src/core/sync_engine.py) using a three-tier system:
1. **Tier 1: Explicit External ID Links**: Compares the cross-referenced IDs stored in the `external_ids` dictionary (each side's `service_id` is the key, e.g. `garmin` activity ID vs `divelogs` dive ID).
2. **Tier 2: Match keys from the mapping board**: The `field_links` flagged with `match_order` (number or datetime fields only) are tried in order. The default board flags the dive-number link, which reproduces the old "same positive dive number" rule.
3. **Tier 3: Naive Timestamp Match**: Matches dives if their start times fall within the configured `grace_window_minutes` (default is 15 minutes).

### 2b. Field catalogue and field links (what happens on a matched pair)
Every adapter declares a `service_id` and a `field_catalog()` of `FieldSpec`s (key `<service_id>.<name>`, type, writability). The user's mapping is `SettingsModel.field_links`, a list of `FieldLink`s (source field(s) → target field, direction, conflict policy, optional template). `SyncEngine._apply_link` runs every active link on every matched pair: read both ends, compare, and if they differ decide who wins from the global `directionality` (which sides may be written) and the link's `conflict` policy. The shipped default board (`fields.default_field_links()`) reproduces the pre-Track-C behaviour exactly. Fields with no `UnifiedDive` attribute (Garmin `activityName`/`locationName`, Divelogs `location`/`divesite`) live in `UnifiedDive.service_fields`. Design and open steps: [rework.md](rework.md) Track C.

### 3. Scheduling
`src/core/scheduler.py` holds `scheduler_loop()` (an asyncio loop started from `web/app.py`'s FastAPI `lifespan`), `run_sync_thread()`, and `get_next_scheduled_run()`. It reads `SettingsModel.schedule`/`cron_jobs` (via `ConfigManager.load_settings()`) every minute and spawns a background thread calling `SyncEngine.run_sync()` when a job is due. This module has no FastAPI dependency, so it can be reused by non-web entry points.

### 4. Dive Editing (planned)
The web dashboard's dive-editing UI (spreadsheet-style metadata edits, per-dive delete with remote push via `adapter.update_dive()`/`adapter.delete_dive()`) has been removed from `src/web/` as part of the desktop-app migration (see [rework.md](rework.md)) — it is not currently available anywhere. It will be rebuilt as a native Toga desktop app calling `BaseDiveAdapter` methods directly.

---

## 🚀 Key Commands

### Running the Status Page
Starts the FastAPI status page with hot-reloading at `http://127.0.0.1:8000`:
```bash
uvicorn src.web.app:app --reload
```

### Running the CLI Sync Engine
```bash
# Perform bidirectional incremental sync
python sync.py

# Perform a dry-run sync without updating remote services
python sync.py --dry-run

# Download all raw data from both services to local cache
python sync.py --save-raw-data
```

### Running the Tests
```bash
./run_tests.sh
```

---

## 💡 Developer Guidelines
* **Abstract Base Client**: Do not import `GarminAdapter` or `DivelogsAdapter` directly outside the service layers or background runners. Always interact through the `BaseDiveAdapter` interface. `SyncEngine` itself only knows a `source` and a `target` adapter; `engine.garmin` / `engine.divelogs` are aliases.
* **Field catalogue first**: a field the engine should compare or copy must be declared in the adapter's `field_catalog()` and reachable through `fields.get_field` / `set_field`; never add a hardcoded field comparison to `run_sync`.
* **Declarative Mapping**: Avoid hardcoding field translations. If a new API field needs to be mapped, update the mapping definitions in `divelogs_to_garmin.json` or `garmin_to_divelogs.json`.
* **Testing Changes**: When adding new API routes or synchronization rules, always write corresponding test cases in the `tests/` directory and ensure `./run_tests.sh` executes successfully.
