# Changelog

All notable user-facing changes to DiveSync are recorded here. See `README.md`
for current features and setup, and `rework.md`/`features.md` for the full
development history and decision log.

## 0.1.0 - Unreleased

First packaged release.

- Bidirectional dive log sync between Garmin Connect and Divelogs.org, with
  a field-level mapping board (per-field direction, conflict policy,
  composite/templated fields, optional reverse parsing).
- Additional sync targets: UDDF files, Subsurface (local git checkout and
  Subsurface Cloud), and Submersion (including end-to-end encrypted
  libraries).
- Deletion propagation, automatic pre-sync backups, timezone-aware dive
  matching, and a conflict queue for fields that need a manual pick.
- Multi-account support for Garmin and Divelogs.
- FastAPI-based status page (scheduling, mapping board, accounts, conflicts,
  sync history) and a PySide6/Qt Quick desktop app with an editable dive
  table, credentials stored in the OS keychain, and profile export/import.
- Docker deployment for unattended scheduled sync.
