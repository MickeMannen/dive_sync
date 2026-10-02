# Changelog

All notable user-facing changes to DiveSync are recorded here. See `README.md`
for current features and setup, and `rework.md`/`features.md` for the full
development history and decision log.

## Unreleased

- Convert page: a Garmin `.fit` file (or Connect's export zip) whose dive is
  in the app's Garmin cache now gets its site, buddy, notes, weight,
  visibility and tank sizes from there - what you typed into Garmin Connect
  but the watch's file does not hold. The dive is found by the activity id
  in the file's name (the app's own cache names and the names Connect
  exports, `<id>.zip` / `<id>_ACTIVITY.fit`), or else by its start time, so
  a renamed file or one kept in your own folders is filled too; with
  several Garmin accounts, the one the Garmin page shows is tried first.
  Only empty fields are filled; nothing in the file is overwritten, and a
  tank size the watch holds stays the watch's. The dive's pane says what was
  filled in, from which account, and when it was matched by time. A "Fill
  from Garmin cache" checkbox at the top of the page (on by default,
  remembered) switches it off to see the file alone.
- Convert page: the dive list is now a working list. Open adds the files'
  dives to it (a dive already there - same file, or same start and number -
  is skipped and counted), **Remove** (or Delete/Backspace on the list)
  takes the selected dives off it, **Clear** empties it. The files on disk
  are never touched.

## 0.4.0 - 2026-10-02

Beta: the Convert page is new and has had less testing than the sync; keep
the original files.

- Desktop app: a new **Convert** page (between Conflicts and Settings) converts
  dive-computer files offline, without any account: open one or several
  Garmin `.fit` files (or Garmin Connect's "export original" zip), UDDF files
  or Subsurface `.ssrf` files, see each dive's summary, tanks, depth profile
  and which extra channels the file holds (tank pressures, NDL, CNS, PO2,
  heart rate, ...), and save the selected dives as UDDF or Subsurface `.ssrf`.
  One dive is saved through a Save-as dialog; several give one file per dive
  (named `<date> <time> dive <number>`) in a folder you pick. The page says
  what the chosen format cannot hold. The dialogs open in Documents the
  first time and then in the last folder used.
- Dives pages: the depth profile is drawn by the same component as on the
  Convert page; it looks and behaves as before.
- UDDF files: a dive's lead weight is now written (as `leadquantity` under
  the equipment used), so a sync to a UDDF file no longer loses it. Dives
  without a weight are written exactly as before.
- UDDF files: dives from Subsurface, the Shearwater app or Submersion now
  carry their tank-pressure profile (one reading per sample, on the first
  tank), so a logbook that imports the file draws the pressure graph.
- Web dashboard: the Shearwater settings are gone. The Shearwater app is a
  desktop-app feature, since the Docker image cannot reach the app's
  database.

## 0.3.6 - 2026-09-30

- Conflicts page (desktop and web): a "Clear all" button forgets every
  waiting conflict after a confirmation. Nothing is written to any
  service; a later sync that finds the same difference lists it again.
- Positions: two services' GPS positions of one dive within 200 m of each
  other count as the same position (no update, no conflict). Before, a
  difference of a few metres raised a conflict on nearly every dive.
- Shearwater app: the dive number is the computer's own and is never
  written from another service (a first live run had renumbered two dives).
- Mapping board: a rule now says what to write: the whole value, only the
  text before or after a separator (e.g. the area or the site out of
  "Tenggol Island, Sawadi Wreck"), text built from a template, or what a
  custom pattern picks. The editor shows the result on a sample dive, and a
  value without that part is left alone. The board's help is shorter and
  explains combining, splitting and this in one place.
- **Shearwater app (desktop app only, more testing ongoing)**: the Shearwater
  app keeps its database on the computer it runs on, so this is a feature of
  the DiveSync desktop app there, not of the Docker image. Verified against the
  author's own account so far; keep a backup of the app's data (DiveSync
  copies the database before every write) and use a second Shearwater account
  to try it out.
- New source: the Shearwater app's database (`shearwater:<path to dive_data.db>`; the
  app is Shearwater's "Shearwater Cloud" desktop program - DiveSync works on its
  local file and never talks to the cloud, the app does that itself).
  Its dives are read with time, duration, depths, temperatures, dive number,
  gases with pressures, the metadata typed into the app (site, buddy,
  notes, environment, weather and the other dropdowns) and the dive
  profile decoded from the computer's own log, so they can be created on
  Subsurface, Divelogs or a UDDF file with their profile. Every sample the
  computer logged is carried (depth, temperature, time, and the
  transmitter's tank pressure per sample, which Subsurface's profile
  format holds and Divelogs' does not), including the minute the computer
  keeps logging at the surface; a log that cannot be decoded, or that
  disagrees with the dive's recorded depth and time, gives the dive
  without a profile and one warning, never a failed load.
  Metadata from the other services (site, buddy, notes, dive number,
  weight, entry position and the app's own dropdown fields) is written into
  the database exactly as the app writes an edit, so the app uploads it to
  Shearwater Cloud on its next sync; dives are never added or deleted
  there (a run says once which dives the other side has that Shearwater
  does not, and leaves them alone). Writes are refused while the app is open, and the database is
  copied to `backups/shearwater/` first. Several Shearwater accounts work like
  several Garmin or Subsurface accounts: the desktop app's Settings →
  Shearwater app lists them (Detect finds every account the app has on
  this Mac; a path field takes a copy), the Sync page picks one per run,
  and each pair keeps its links per account. With nothing saved, the app's
  active account is used. The web dashboard's credentials form takes one
  path. A known database appears on the Sync
  page as a side and gets a mapping board with each configured service.
- UDDF file: creating more than one dive in a single run failed after the
  first one (the file's namespace was applied to the in-memory document).
- Sync page, desktop app and web dashboard: "Download dives" now has one
  tick per configured service, so one, several or all caches can be
  refreshed in one go. The web dashboard gets the control for the first
  time (it had no way to refresh a cache); it uses the Sync page's account
  picks, shows the download's progress and says how it ended. A download
  always fetches every dive again, Garmin's included; "Use cached Garmin
  dives" now only applies to syncing.

## 0.3.5 - 2026-09-29

- Conflicts page: a pick on a Subsurface dive that the same sync run had
  renumbered (Garmin's dive number changed, so `Dive-7` became `Dive-8`)
  failed with "was not found on subsurface; it may have been deleted".
  The dive is now found by its start-time folder, and later runs replace
  the pair's waiting conflicts instead of adding a duplicate per renumber.
- Conflicts page: saving picks reports progress ("n of m done"), locks the
  other buttons until the save and the reload after it have finished, and
  refuses a second concurrent save. Each dive a save writes to is fetched
  once, and Garmin is asked for that one dive instead of paging the history.

## 0.3.3 - 2026-09-27

- Conflicts page: more fixes to the conflict logic.
- Desktop app: fixed the app version not being able to contact Subsurface.

## 0.3.2 - 2026-09-27

- Conflicts page: green toggle picks, one Save button per receiving service.

## 0.3.1 - 2026-09-27

- Conflicts page rework, desktop dive list fixes, Gitea test builds.

## 0.3.0 - 2026-09-26

**Breaking: new data folder and layout. This version starts fresh** - it
does not read the data of earlier versions, and never touches it. Enter
your logins again and download your dives again; delete the old folder
once this version works for you (the desktop app logs where it is).

- New data folder, named after the app's identity `org.christersson.dive_sync`:
  - macOS: `~/Library/Application Support/org.christersson.dive_sync`
    (was `~/Library/Application Support/DiveSync`); the app's bundle id is
    now `org.christersson.dive_sync`
  - Windows: `%LOCALAPPDATA%\Christersson\DiveSync`
  - Linux: `~/.local/share/dive-sync`
  - Docker and the CLI: `DATA_DIR`, as before
- Everything one account owns is in one folder: `<service>/<account>/data`
  holds its cached dives, and Garmin accounts also have `fit/` (downloaded
  `.fit` files, was `garmin_fit/<account>/`) and `tokens/` (login tokens,
  was `tokens/garmin/`). A Subsurface Cloud checkout is in
  `subsurface/<account>/cloud` (was `subsurface_cloud/<account>`). A side
  without accounts uses the account folder `default`.
- Sync state, conflicts and run history are in `sync/`
  (`sync_state*.json`, `conflicts*.json`, `sync_history.jsonl`); backups
  stay in `backups/`. Docker users: the remembered dive pairs start empty,
  and the first sync re-matches dives by time.
- Garmin `token_dir` is blank by default, meaning the account's own
  `tokens` folder; the old default `tokens/garmin` is read the same way.
- Desktop app: passwords are stored in the keychain under
  `org.christersson.dive_sync` (was `DiveSync`).
- Garmin dives page: a Dive computer column and a read-only field naming
  the watch that recorded each dive (e.g. Descent X50i, Descent Mk3(i)
  51mm; "Hand-logged" for a dive typed in on Connect), read from the
  dive's `.fit` file.
- Garmin: only scuba dives are downloaded and synced (single gas, multi
  gas, CCR, gauge, and dives logged by hand). Apnea dives and spearfishing
  are left on Garmin; one downloaded by an earlier version is removed from
  the cache on the next download.
- Conflicts page: saving a mapping board drops the waiting conflicts its
  rules no longer raise (a rule deleted, re-pointed, or given a policy other
  than manual), instead of leaving them until a run happens to compare those
  dives again. A sync run does the same for a board edited by hand.
- Subsurface Cloud in the desktop app: fixed "certificate verify failed"
  on every clone, fetch and push. The bundled Python has no CA store of
  its own, so the app now uses the same certificate bundle as its Garmin
  and Divelogs connections.

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
