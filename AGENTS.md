# Guidelines for AI Coding Agents (AGENTS.md)

Welcome, AI Agent! This file outlines the instructions, constraints, and development workflow for the `dive_sync` project. Please follow these guidelines for all code edits, refactorings, and tool usage.

---

## 🎯 Project Overview
`dive_sync` is a bidirectional dive log synchronization system connecting Garmin Connect and Divelogs.org. It normalizes data via Pydantic models and matching matrices, and serves a FastAPI web UI with local cached dive files.

---

## 🛠️ Preferred Coding Patterns & Style
> [!TIP]
> Edit or override these preferences to match your custom coding style!

*   **Language & Standards**: Python 3.10+ with clear static type hinting.
*   **Documentation**:
    *   Preserve all existing docstrings and comments.
    *   When adding new functions or classes, write descriptive docstrings.
*   **Data Models**:
    *   Always use Pydantic models (specifically `UnifiedDive`, `GasMixture`, `UnifiedSample` in [src/core/models.py](file:///Users/mikael/development/dive_sync/src/core/models.py)) for normalized domain data.
*   **API Client Design**:
    *   Always extend the [BaseDiveAdapter](file:///Users/mikael/development/dive_sync/src/core/adapter.py) interface when creating new service clients or modifying existing ones.
    *   Do not instantiate adapters directly outside of their service boundaries.
*   **Data Translation**:
    *   Do not hardcode field translation logic. Use the declarative JSONPath mappings defined in [src/core/mapping/](file:///Users/mikael/development/dive_sync/src/core/mapping).
*   **Testing & Verification**:
    *   Always write unit or integration test cases in the `tests/` directory for all new functions, endpoints, or adapters added.
    *   Before concluding a task, run the test suite using `./run_tests.sh` to ensure all tests pass and no regressions are introduced.

---

## ⚠️ Critical Constraints
*   **API Cooldowns & Rate Limits**: 
    *   Always respect Garmin Connect login thresholds. Rate limiting (HTTP 429) is severe. Ensure cooldowns (`api_cooldown_seconds`) are respected during synchronization.
*   **Deletion Safety**:
    *   When deleting dives from the local cache explorer, ensure that BOTH the local JSON file is removed and the corresponding API delete request is sent in a background thread to the remote service (Garmin or Divelogs).
*   **File Paths & Local Storage**:
    *   Save all temporary files, scratch scripts, or data back-ups in the `.gemini/` app directory or [data/](file:///Users/mikael/development/dive_sync/data/) directory. Do not clutter the workspace root.

---

## 🚀 Commands Reference

### Run FastAPI Server
```bash
uvicorn src.web.app:app --reload
```

### Run CLI Sync Engine
```bash
python sync.py
```

### Run All Integration & Unit Tests
```bash
./run_tests.sh
```

---

## 📝 Editing this File
Feel free to add your own constraints, specify library version preferences, or add details about new adapters (e.g. Subsurface or MacDive) as you extend the application.
