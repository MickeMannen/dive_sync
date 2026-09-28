# CLAUDE.md

This file is the entry point for Claude Code sessions in this repo. It points to the existing docs rather than duplicating them — read those first, keep this file thin.

## What this project is
`dive_sync` is a bidirectional dive log sync engine between **Garmin Connect**, **Divelogs.org** and **Subsurface** (Subsurface Cloud, a git checkout, or a UDDF file), with three front ends: a **desktop app** (DiveSync, PySide6/Qt Quick in `desktop/`, for manual syncs, browsing and editing dives, the mapping board and conflicts), a **FastAPI web dashboard** shipped in the Docker image for unattended scheduled syncs, and a **CLI** (`sync.py`). Beta quality; author has tested Garmin→Divelogs more than the reverse direction. See [README.md](README.md) for features, install, CLI usage, Docker deployment and the desktop app.

## Where to look
- [CONTEXT.md](CONTEXT.md) — architecture, directory layout, core concepts (dive normalization via `UnifiedDive`, three-tier dive matching in `SyncEngine`, deletion flow). Read this before touching `src/core/`.
- [AGENTS.md](AGENTS.md) — coding conventions and constraints for AI agents working in this repo (Pydantic models, `BaseDiveAdapter` interface, declarative JSONPath mappings in `src/core/mapping/`, API rate-limit/cooldown rules, test requirements). Follow these when writing or editing code here.
- [README.md](README.md) — user-facing docs: features, credentials setup, CLI flags, Docker deployment, known limitations.

## Quick commands
```bash
uvicorn src.web.app:app --reload   # web dashboard, hot reload
python sync.py                      # CLI sync (see README for flags: --dry-run, --direction, --full-sync, etc.)
./run_tests.sh                      # full test suite — run before calling any change done
```

## Desktop app: identity and file-system paths
The desktop app (`desktop/`, PySide6/Qt Quick, packaged with Briefcase) is cross-platform: macOS today, Windows and Linux builds in CI. Its identity and data folder are defined in one place, `desktop/paths.py`; keep them consistent with `pyproject.toml` (`[tool.briefcase]`) and `desktop/app.py`.

**Identity**
- Domain: `christersson.org` — Vendor / organization: `Christersson`
- App name: `DiveSync` — slug: `dive_sync` — Linux folder: `dive-sync`
- Bundle / app id (reverse-DNS, macOS only): `org.christersson.dive_sync`

**Runtime data** — one private per-user data dir, resolved by `desktop/paths.py` via `platformdirs` and exported as `DATA_DIR` for `src/core` (`src/core/layout.py` lays it out):
- macOS: `~/Library/Application Support/org.christersson.dive_sync/`
- Windows: `%LOCALAPPDATA%\Christersson\DiveSync\` (Local on purpose, not Roaming: the dive caches are large)
- Linux: `$XDG_DATA_HOME/dive-sync/` (default `~/.local/share/dive-sync/`)
- Contents: `settings.json`, `desktop_prefs.json`, per-service/per-account dive caches, FIT files, sync state and backups. Credentials and the Garmin token live in the OS keychain (`keyring`, see `desktop/credentials.py`), not on disk, except materialised for the duration of an operation.

**Rules**
1. Never hardcode a platform path as a raw string. Resolve through `desktop/paths.py` / `platformdirs` (or `QStandardPaths` on the QML side); in `src/core`, derive from `DATA_DIR` via `src/core/layout.py`.
2. Create directories recursively (`os.makedirs(..., exist_ok=True)`) before reading or writing.
3. Respect the Linux `$XDG_*` variables and their defaults (`platformdirs` does; don't bypass it).
4. Never write logs, config, caches or databases into the install location (the `.app` bundle, `%LOCALAPPDATA%\Programs\`, `%ProgramFiles%\`, `~/.local/bin`) — binaries and read-only assets only.
5. Changing any identity constant changes the data folder and keychain service name for every user; treat it as a migration (see `old_data_dir()` and rework.md E21).

## Notes for this migration
This project's docs (CONTEXT.md, AGENTS.md) were originally written while developing with Gemini; they're tool-agnostic and still accurate against the current code (verified against `src/` on 2026-08-16). No Claude-specific rework was needed — just this pointer file so Claude Code picks up context automatically each session.
