# Guidelines for AI Coding Agents (AGENTS.md)

Welcome, AI Agent! This file outlines the instructions, constraints, and development workflow for the `dive_sync` project. Please follow these guidelines for all code edits, refactorings, and tool usage.

---

## 🎯 Project Overview
`dive_sync` is a two-way dive log synchronization system (each run writes one side, rework.md G0) connecting Garmin Connect and Divelogs.org. It normalizes data via Pydantic models and matching matrices, and serves a FastAPI web UI with local cached dive files.

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
*   **Field catalogue and links** ([src/core/fields.py](file:///Users/mikael/development/dive_sync/src/core/fields.py)):
    *   Every adapter sets `service_id` / `display_name` and returns its readable/writable fields from `field_catalog()` (keys are `<service_id>.<name>`). A new synced field is added to the catalogue (and, if it has no `UnifiedDive` attribute, to `service_fields` in `to_unified` / `update_dive`), never as a special case in `SyncEngine.run_sync`.
    *   What happens on matched dives is defined by the pair's receiver rules (`SyncPairModel.rules`, rework.md Track G), applied by `SyncEngine.active_rules` / `_apply_rule` for the run's one receiver and edited as rules by both boards. The link view (`SyncPairModel.field_links` / `SettingsModel.field_links`, derived by `fields.rules_to_links`) is only for validation, version-1 input and per-job boards - do not build new features on it. Any change to the converters must keep `tests/test_rules.py` lossless, and `fields.default_field_links()` must keep reproducing the previous behaviour (guarded by `tests/test_link_engine.py::test_link_loop_reproduces_legacy_loop`). A policy is always read from the receiver's side; `target_wins` is a no-op by design.
    *   The Garmin ↔ Divelogs pair is `sync_pairs[0]` with id `garmin_divelogs` (`config.DEFAULT_PAIR_ID`); never add a second code path for it. Settings files are version 2 (`settings_version`); legacy top-level `directionality` / `field_links` and per-pair `field_links` are accepted on input and folded in by the models' validators, never written.
    *   Per-run changes go in as `SyncEngine.run_sync(...)` override arguments; `run_sync` reloads `settings.json` first, so mutating `engine.settings` beforehand does nothing.
*   **Testing & Verification**:
    *   Every function, endpoint, adapter method or controller slot has unit tests in `tests/`, in the module that covers its file (`src/core/services/shearwater.py` → `tests/test_shearwater.py`, `src/web/app.py` → `tests/test_web_api.py`, `desktop/controllers/*.py` → `tests/test_desktop_qt.py`). A change to a function is a change to its tests in the same commit.
    *   Test against mock data, never live services: the in-memory fakes (`tests/test_link_engine.py` `FakeGarmin`/`FakeDivelogs`/`RecordingAdapter`), the fixture files under `tests/data/` (a Subsurface checkout, an anonymised Shearwater database, Submersion samples), `monkeypatch` for the network, the keychain (`fake_keyring`) and the clock. New fixture data is anonymised (no real names, sites, emails or free text) and small.
    *   While implementing one part, run the test modules that cover it (`python -m pytest tests/test_x.py -q`) after each step; before calling the change done, and always before preparing a release, run the whole suite with `./run_tests.sh`.

---

## 🤖 Agent workflow

Work moves through three roles, each on the model suited to it. A role runs as a subagent (`.claude/agents/<role>.md`, which fixes the model and the tools; see "Triggering a role" below) or as the session model when the owner starts the session on it.

| Role | Model | Does |
|---|---|---|
| Research | Opus 5.5 | Investigates before code is written: reads the code, the docs and external formats, answers "what is this file / API / behaviour", proposes the design and the questions for the owner. Writes into `rework.md` (plan, decisions log) and `features.md` (backlog), never into `src/`. |
| Implementation | Fable 5.1 | Writes the code **and the tests that pin it** (a test is the specification of the code next to it, so it is written by the same model, one part at a time), running that part's test modules as it goes. Updates `CHANGELOG.md` (Unreleased), README and the plan's step status. |
| Verification | Sonnet 5 | Runs the applicable test modules after an implementation step, or the full suite before a release; triages failures into "the test is wrong" / "the code is wrong" with the failing assertion and log lines quoted; reviews a step's diff against these guidelines; adds boilerplate cases and fixture data. Never rewrites the code it is checking. |

Triggering a role (Claude Code): the three roles are project subagents in `.claude/agents/` (`research.md`, `implement.md`, `verify.md`), each with its model and tools in the frontmatter, and they inherit CLAUDE.md and this file.
- By name in the prompt: "use the research agent to investigate the Shearwater database", "implement H3 with the implement agent", "verify agent: run the suite". `@research`, `@implement`, `@verify` also work.
- Automatically: the session's Claude picks a role whose `description` matches the task (the descriptions name the trigger words: investigate/plan, implement/fix/proceed with, run the tests/verify).
- For a whole session on one role: `claude --agent implement`.
- The model in a role's frontmatter wins over the session model; a per-invocation `model` in the prompt ("with sonnet") overrides it once. The session's own model cannot switch between steps - only subagents can.

Rules of the road:
1. Research first when the task touches a service format, an external API or an unknown part of the codebase; skip it for a bug with a known cause.
2. Implementation is done per step of the plan (rework.md step ids), each step ending with its test modules green, so a step can be committed alone.
3. Testing runs the applicable modules after each implementation step and the full suite before a release (`CLAUDE.md` "Changelog and releases"); a release is never prepared on a red or unrun suite.
4. The owner does the commits, the releases and the hands-on checks against live accounts; the agents do not push, tag or run a sync against a live service.

---

## ⚠️ Critical Constraints
*   **API Cooldowns & Rate Limits**: 
    *   Always respect Garmin Connect login thresholds. Rate limiting (HTTP 429) is severe. Ensure cooldowns (`api_cooldown_seconds`) are respected during synchronization.
*   **Deletion Safety**:
    *   `src/web/` (the Docker-facing status page) no longer has any dive-editing or deletion UI — that's being rebuilt as a native desktop app (see [rework.md](rework.md)). If/when dive deletion is reintroduced anywhere, ensure that BOTH the local JSON file is removed and the corresponding API delete request is sent in a background thread to the remote service (Garmin or Divelogs), as the old web implementation did.
*   **Scheduler vs. web coupling**:
    *   `src/core/scheduler.py` (the background schedule watcher and sync runner) must stay free of FastAPI imports — `src/web/app.py` only wires its `lifespan` to `scheduler.scheduler_loop()`. Keep it this way so non-web entry points can reuse it.
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
