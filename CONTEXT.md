# Dive Sync Project Context & Architecture

This document provides a comprehensive overview of the `dive_sync` project. It is intended to onboard developers and AI coding assistants to the codebase.

---

## 🎯 Project Overview
`dive_sync` is a bidirectional dive log synchronization tool that connects **Garmin Connect** and **Divelogs.org**. It allows users to:
1. Fetch dive logs from both platforms.
2. Normalize logs into a unified format.
3. Match identical dives across services.
4. Synchronize data differences (gases, weights, buddies, notes, GPS coordinates) and upload new dives.
5. Manage cached files and run manual or scheduled sync jobs via a web interface or CLI.

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
│   │   ├── config.py          # Settings and credentials management
│   │   ├── mapping_helper.py  # Mapping Engine that applies JSONPath rules
│   │   ├── models.py          # Unified Dive schemas (Pydantic)
│   │   └── sync_engine.py     # Dive matching and synchronization engine
│   └── web/                   # Web API & UI
│       ├── static/            # Static assets (HTML, CSS, JS)
│       └── app.py             # FastAPI Server & SSE logging setup
├── tests/                     # Test suite
│   ├── data/                  # Static test fixtures
│   ├── test_sync.py           # Sync logic and engine tests
│   ├── test_mock_sync.py      # Dry-run and offline sync tests
│   └── test_web_api.py        # Web API endpoint integration tests
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
1. **Tier 1: Explicit External ID Links**: Compares the cross-referenced IDs stored in the `external_ids` dictionary (`garmin` activity ID vs `divelogs` dive ID).
2. **Tier 2: Dive Number Match**: Compares the dive sequence numbers if present.
3. **Tier 3: Naive Timestamp Match**: Matches dives if their start times fall within the configured `grace_window_minutes` (default is 15 minutes).

### 3. Deletion Flow
When a dive is deleted through the Local Cache Explorer UI:
1. The frontend sends a `DELETE` request to `/api/dives?service={service}&filename={filename}`.
2. The backend reads the cached file to find the remote ID.
3. The backend spawns a background thread to call the remote client's `delete_dive(external_id)` method.
4. The local JSON cache file is removed from the filesystem.

---

## 🚀 Key Commands

### Running the Web Server
Starts the FastAPI server with hot-reloading at `http://127.0.0.1:8000`:
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
* **Abstract Base Client**: Do not import `GarminAdapter` or `DivelogsAdapter` directly outside the service layers or background runners. Always interact through the `BaseDiveAdapter` interface.
* **Declarative Mapping**: Avoid hardcoding field translations. If a new API field needs to be mapped, update the mapping definitions in `divelogs_to_garmin.json` or `garmin_to_divelogs.json`.
* **Testing Changes**: When adding new API routes or synchronization rules, always write corresponding test cases in the `tests/` directory and ensure `./run_tests.sh` executes successfully.
