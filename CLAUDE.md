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

## Changelog and releases
The owner does the coding, testing and the release builds; Claude keeps the record. Version numbers are the git tags (`v0.3.3`); the release workflow stamps the tag into the builds, so `pyproject.toml` is not bumped per release.

**After every round of work** (before the round is called done): add the round's user-facing changes as bullets under `## Unreleased` at the top of [CHANGELOG.md](CHANGELOG.md), in the file's existing style: what a user notices and why it matters, one to three lines each, no implementation detail, no bullet for docs-only or internal changes. Create the `## Unreleased` section if it is missing. Group by feature area as the existing entries do ("Conflicts page:", "Garmin:", "Desktop app:").

**When the owner says "prepare a release"** (or "prepare for release 0.3.4"): do these steps, on `main`, and stop before anything the owner did not ask for.
1. Pick the version: the one the owner named, otherwise the next patch number after the newest tag (`git tag --sort=-v:refname | head -1`). Say which one you chose.
2. Rename `## Unreleased` in CHANGELOG.md to `## <version> - <today, YYYY-MM-DD>`. Tidy the bullets so they read as release notes; do not add things that are not in the tree. Show the section to the owner before going on.
3. Commit that change on `main` with the message `Release <version>`, then tag it `v<version>` and push branch and tag to both remotes: `git push github main v<version>` and `git push gitea main v<version>`.
4. Create the GitHub release from that section, with the tag as the release and the notes taken verbatim from the file:
   ```bash
   awk '/^## <version> /{f=1;next} /^## /{f=0} f' CHANGELOG.md > /tmp/notes.md
   gh release create v<version> --title "DiveSync <version>" --notes-file /tmp/notes.md --repo mickemannen/dive_sync
   ```
5. Report the release URL and stop. The owner then runs the "Build and attach release artifacts" workflow from the tag on GitHub, which builds the installers and attaches them to the release; Claude does not trigger that workflow.

Tagging and creating the release are public and hard to undo, so if the tree is dirty, tests have not been run this session, or the version already exists as a tag, say so and stop instead of proceeding.

## Notes for this migration
This project's docs (CONTEXT.md, AGENTS.md) were originally written while developing with Gemini; they're tool-agnostic and still accurate against the current code (verified against `src/` on 2026-08-16). No Claude-specific rework was needed — just this pointer file so Claude Code picks up context automatically each session.
