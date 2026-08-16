# CLAUDE.md

This file is the entry point for Claude Code sessions in this repo. It points to the existing docs rather than duplicating them — read those first, keep this file thin.

## What this project is
`dive_sync` is a bidirectional dive log sync engine + FastAPI web dashboard between **Garmin Connect** and **Divelogs.org**. Beta quality; author has tested Garmin→Divelogs more than the reverse direction. See [README.md](README.md) for features, install, CLI usage, and Docker deployment.

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

## Notes for this migration
This project's docs (CONTEXT.md, AGENTS.md) were originally written while developing with Gemini; they're tool-agnostic and still accurate against the current code (verified against `src/` on 2026-08-16). No Claude-specific rework was needed — just this pointer file so Claude Code picks up context automatically each session.
