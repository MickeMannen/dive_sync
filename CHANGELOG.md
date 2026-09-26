# Changelog

All notable user-facing changes to DiveSync are recorded here. See `README.md`
for current features and setup, and `rework.md`/`features.md` for the full
development history and decision log.

## 0.1.0 - Unreleased

First packaged release.

- Two-way dive log sync between Garmin Connect and Divelogs.org - one
  receiving service per run, so a two-way sync is two scheduled runs - with
  a field-level mapping board of receiver rules - what each service accepts
  from the other, with a policy per rule, composite/templated fields and
  optional reverse parsing - edited as two receiver panels on the status
  page and in the desktop app. Deletes follow the run's direction. Boards
  are stored per pair (`settings_version` 2); the Garmin <-> Divelogs pair
  is the explicit `garmin_divelogs` entry of `sync_pairs`. Older settings
  files and version-1 sync profiles are upgraded on load.
- Desktop app: several accounts per service for Garmin Connect,
  Divelogs.org and Subsurface Cloud (e.g. a live and a test account on one
  computer). Each dives page and the Sync page pick their account, and
  remember it; cached dives and each pair's sync history are kept per
  account. The web UI and Docker still use one Subsurface Cloud account.
- Additional sync targets: UDDF files, Subsurface (local git checkout and
  Subsurface Cloud), and Submersion (including end-to-end encrypted
  libraries). Submersion syncs metadata only - dive site, dive name, buddy,
  notes, weight, visibility, GPS and its own extra fields: a dive's profile,
  cylinders, tank pressures and gas switches come from the `.fit` file you
  import in the Submersion app, and a dive a dive computer recorded is left
  for that import to create (hand-logged Garmin dives are created for you).
- Deletion propagation, automatic pre-sync backups, timezone-aware dive
  matching, and a conflict queue for fields that need a manual pick.
- Multi-account support for Garmin and Divelogs.
- FastAPI-based status page (scheduling, mapping board, accounts, conflicts,
  sync history) and a PySide6/Qt Quick desktop app with an editable dive
  table, credentials stored in the OS keychain, and profile export/import.
- Docker deployment for unattended scheduled sync.
- Garmin FIT files: the Garmin dive list (desktop and a new "Garmin dives"
  page on the status page) shows whether each dive's original `.fit` has
  been downloaded, and downloads it for one dive or every dive still
  missing one. FIT files go to `data/garmin_fit/<account>/`, named like the
  dive's cached JSON: `<dive #>_<date>_<time>_<activity id>`. Existing
  `<dive #>.json` cache files are renamed on the next refresh. Hand-logged
  dives are marked "manual" and skipped: Garmin has no dive-computer file
  for them, only a generated summary.
- Syncs reuse the Garmin cache: a dive Garmin's activity list shows
  unchanged is read from the local cache instead of being downloaded again
  (three API calls each), and dives a sync does download are cached. On by
  default ("Use cached Garmin dives" in the desktop app and on the status
  page, `--no-garmin-cache` on the CLI); turn it off, or run a Full
  refresh, to pick up an edit to only notes, buddy, weight or visibility.
