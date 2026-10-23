# Changelog

All notable user-facing changes to DiveSync are recorded here. See `README.md`
for current features and setup, and `rework.md` (local only) for the plan,
the backlog and the decision log.

## 0.5.1 - 2026-10-23

- Submersion: support removed. It was switched off and is no longer
  planned, so its settings, credentials and code are gone. A settings or
  credentials file that still mentions Submersion keeps loading: a sync
  pair or scheduled job with a Submersion side is dropped (the log says so
  once) and the rest of the file is kept as it was.
- Desktop app and Docker image: smaller downloads, as the installers no
  longer carry the S3 and encryption libraries only Submersion used.

## 0.5.0 - 2026-10-20

- MySSI (beta): **MySSI is a sync service** like the others - a source and a
  target on the desktop Sync page (with an account picker and a beta note),
  on the command line (`--source garmin --target ssi`, `--ssi-account`) and
  on the web dashboard (Sync now, scheduled jobs, Download dives). It has a
  mapping board and a place on the Conflicts page.
- MySSI (beta): a sync towards MySSI **adds new dives only** until updating
  a MySSI dive has been tested: a change to a dive already there is logged,
  not sent; a dive with two or more tanks is skipped with a reason; and **no
  sync run deletes on MySSI** (not Mirror, not propagate deletes), because a
  MySSI delete is permanent.
- MySSI (beta): on the Settings page the **MySSI** card is an account list
  like Divelogs' (any number of logins, kept in the OS keychain); the login
  saved by the previous version is moved into it. The CLI and Docker take
  the same accounts in `credentials.json` (`setup_credentials.py --services
  ssi`).
- MySSI (beta): a **MySSI Dives** page: your MySSI logbook with the depth
  profile, main tank and dive site, an account picker, Refresh and Delete.
  The editor is read-only until updates are tested.
- MySSI (beta): the Dive edit page no longer has MySSI buttons: MySSI is
  synced from the Sync page and browsed on its own Dives page.
- MySSI (beta): MySSI has no public API; DiveSync uses the MySSI app's own
  connection, which SSI can change or close at any time.
- Sync: dives logged on clocks in different time zones are matched instead
  of uploaded twice. Start times are compared in UTC where both services
  know the offset (Garmin, `.fit`, UDDF times with an offset); otherwise a
  pair whose times are a whole time-zone shift apart (up to 14 hours) is
  matched when max depth and duration agree and no other dive fits as well.
  The log names each dive matched this way; dives already linked never move.
- Sync: an update or new dive the target refused is no longer reported as
  done: it is listed as **failed** in the CLI summary, the web dashboard's
  results and run history, and the desktop Sync page's status line, and the
  log says why. A dive the target already has counts as skipped.
- Sync: a run that fails part way no longer forgets the dives it already
  created: each pair is remembered the moment its upload succeeds, so the
  next run does not create them again.
- Sync: Mirror (or propagate deletes) towards the Shearwater app no longer
  logs a false "Failed to delete" warning per dive; the app's dives are
  never deleted, and the run counts them as skipped.
- Garmin (beta, off by default): new dives can reach Garmin Connect with
  their profile, tanks and gases: tick **New Garmin dives as .fit (beta)**
  on the Sync page or in a web dashboard job (or pass
  `--garmin-fit-upload`). A dive the run creates on Garmin is uploaded as a
  `.fit` instead of a summary-only manual activity, its name, notes, buddy,
  weight and location are set right after, and the log names what a `.fit`
  cannot carry. A dry run lists what would be uploaded. Creating dives on
  Garmin must still be on for the pair. Try it on one dive first.
- Dive fields: **Water type, air temperature and suit** are fields of the
  dive: read and written for Subsurface (git, Cloud, `.ssrf`), UDDF, the
  Shearwater app's air temperature, Divelogs' air temperature and MySSI's
  water type (read only). Dive edit gets **Air temp** and **Suit** fields;
  the dives pages get optional columns.
- Dive fields: **Exit position** for drift dives, beside the entry position:
  read from Garmin, read and written for Subsurface (`GPS2`, as Subsurface's
  own Garmin import) and UDDF.
- Dive fields: **SAC** on the dives pages and the Dive edit page: the dive
  computer's own value, otherwise computed from the tanks, average depth and
  duration, in L/min or cuft/min. On Dive edit it is a field (an edited SAC
  goes into **Save as .fit**), with RMV and OTU beside it.
- Dive fields: none of the new fields has a shipped rule, so the first sync
  after upgrading changes nothing; draw a rule on the mapping board to sync
  them.
- Profiles: **Shearwater app:** a dive keeps everything its computer logged
  per sample - every paired transmitter's pressure on its own tank (so both
  tanks of a sidemount dive show), NDL, time to surface, deco stop, CNS,
  ppO2, GF99 and the setpoint - plus its gas switches and the computer's
  model, serial, gradient factors, dive mode and water setting. Before, only
  the first transmitter's pressure survived.
- Profiles: **Subsurface (git and Cloud):** every cylinder's pressure curve
  and the other per-sample values (NDL, TTS, deco stop, CNS, PO2 or
  setpoint, O2 cells, heart rate) are read, and written to a new dive
  uploaded there.
- Profiles: a sync still compares profiles on depth, temperature and time,
  so linked dives are not re-sent because profiles now carry more.
- Profiles: the depth-profile tooltip shows the last reading of a value a
  sample did not log (a tank pressure, the temperature, the NDL), so it no
  longer blinks in and out; a long tooltip wraps into two columns.
- Dive edit: a Garmin `.fit` keeps more of what the watch wrote: SAC, RMV,
  OTU and a rebreather's diluent tank role are read and written back.
- Dive edit: half-hour and quarter-hour time zones (India, Nepal,
  Newfoundland, Adelaide) are kept: a `.fit` from such a dive reads as
  `+05:30` and is saved back the same way.
- Dive edit: UDDF files exported by the Shearwater app read right: PO2 no
  longer shows as 0.00 bar, each transmitter's pressure stays on its tank,
  and unused tank slots no longer appear as extra tanks. When DiveSync
  writes into such a file it is saved in standard UDDF units.
- Dive edit: a UDDF that lists its gases but no tanks (APD's DiveSight and
  others) now gets a gas list as Subsurface imports it, and an APD
  Inspiration's O2 cells are read.
- Rebreathers (untested): O2 cells are kept per sample beside the computer's
  PO2 and the setpoint: read and written in Subsurface `.ssrf`
  (`sensor1`..`sensor3`) and UDDF (`measuredpo2`), read from the Shearwater
  app's database (cells still at the factory calibration are left out, as
  Subsurface does), and shown in the profile tooltip as `Cells 1.22 / 1.19 /
  1.21 bar`. A `.fit` cannot hold them, and **Save as .fit** says so.
- Rebreathers (untested): a Shearwater CCR dive's tanks are right: a
  transmitter named O2 is the oxygen tank (100 %), D1, D2 or DIL a diluent
  with the loop's mix.
- Rebreathers (untested): a Garmin Descent CCR dive shows its low/high
  setpoints and the switches between them, carried into UDDF, Subsurface and
  `.fit` files.
- Rebreathers (untested): **None of this has been checked against a real CCR
  dive yet.** The Dive edit page and the dives pages show a note on a dive
  with rebreather data; compare the values with your dive computer's own log
  before relying on them.
- Web dashboard: the alerts webhook can also report successful runs: next to
  the webhook URL choose **Send** when a run fails (the default, as before),
  when a run changes something or fails, or after every run. The message
  names the job, the direction and the counts, and says when it was a dry
  run. In `settings.json` it is `notify_on`.

## 0.4.5 - 2026-10-19

- Dive edit page: merging a split dive no longer stops at "dive 9 has no dive
  computer serial". The serial is now also read from the file's device
  record when the file header leaves it out, and when a log still has none
  the Merge dialog asks which serial the merged file gets (from the serials
  of the logs being merged) and writes it into the file, so Garmin Connect
  knows which watch it came from; with no serial in any log the merge goes
  ahead and says that Connect may not accept the file. Logs from two
  different computers are still refused.
- Dive edit page: when a merge is refused or fails, a **Generate debug
  report…** button (next to the reason in the Merge dialog, and next to the
  error under the list) saves a text file you can send to the developer: the
  app, system and library versions, the merge that was asked for, why it was
  refused or how it failed, and per log the file name, size and checksum, the
  dive computer and its serial, times, sample count, gases, tanks and event
  counts, plus the app's recent log lines. It holds no GPS positions, site or
  location names, notes, buddy, full file paths, passwords or tokens, and
  says so at the top.

## 0.4.4 - 2026-10-18

- Dive edit page: the depth profile shows a sample's data under the mouse -
  a line and a dot on the profile and a tooltip with the time, depth and
  temperature, the NDL (or, in deco, the next stop and the time to surface),
  the gas breathed (Air, EAN32, O2, 18/45) and each tank's pressure by tank
  name, CNS, PO2, setpoint, heart rate and gas time remaining - only the
  lines the dive computer logged, in the chosen units.
- Dive edit page: a click pins the tooltip (click again or Esc unpins), and
  the Left/Right arrow keys step through the samples (Shift for ten at a
  time).
- Dive edit page: a gas switch is marked on the profile with a tick and the
  gas name, and a deco ceiling shades the profile from the surface down to
  the next stop.
- Dives pages (Garmin, Divelogs, Subsurface): the profile chart hovers too,
  with the time, depth and temperature the downloaded dives hold.

## 0.4.3 - 2026-10-18

Two big additions: the app can show everything in metric or imperial, and the Convert page has grown into **Dive edit** - edit any dive from a file, write it back as a Garmin `.fit`, and join the logs of a split dive.

**Units**
- Desktop app: a **Units** card on the Settings page picks Metric (m, bar,
  °C, L, kg) or Imperial (ft, psi, °F, cuft, lb); the choice applies at once
  and is remembered on this computer. Your dives, files and the services
  stay metric whichever you pick.
- Dives pages (Garmin, Divelogs, Subsurface): the table, the dive form and
  the tank table show depths, water temperature, SAC, tank sizes and
  pressures, weight and visibility in the chosen units, rounded like a dive
  computer in imperial (whole ft, psi and °F), and you type edits in those
  units too; a field you did not touch is saved exactly as it was. A dive
  logged in pounds or feet is shown in the chosen system as well, and keeps
  its own unit on the service when saved.
- Dive edit page: the dive list, the dive's details and its tank table follow
  the Units setting too, and the depth profile chart on the Dive edit and
  Dives pages labels its axis in the chosen unit. The files you save stay
  metric, whichever units are shown.
- Conflicts page: a conflict over a depth, temperature, weight or visibility
  shows both values in the chosen units (a weight logged in pounds reads in
  kilograms under metric, and the other way round); text conflicts are as
  before. Keeping a side still writes the service's own value, unconverted.
- CLI: a new `--units metric|imperial` flag shows depths, temperatures,
  pressures, weights and visibility in the chosen units for that run, in
  `--list-conflicts`, `--test-mapping` and the log lines about a rule's two
  values ("garmin.max_depth=99 ft"). Without the flag everything prints as
  before; the data and the services stay metric.
- Web dashboard: a **Units** select next to the API cooldown on the Settings
  page (Metric / Imperial) applies to the Conflicts page, the Mapping page's
  test table, a run's conflicts in the history and the live log of the runs
  started here. The choice is kept by the dashboard alone - it is not part
  of the sync profile export, so importing a profile never changes it.

**Dive edit (was Convert)**
- The page is now called **Dive edit** and the dive card is an editor. Click
  a dive and change any of its fields - date, time and time zone (the whole
  profile moves along), depths and times on a hand-logged dive, water
  temperatures, site and positions, buddy, weight, visibility, notes, the
  dive computer and its settings, dive mode, water type, and the tank table
  (add, remove, edit) - in the units you chose; Save as UDDF / Subsurface
  writes the dive with its edits. The files you opened are never changed.
- A new **Open folder…** button adds every dive file directly in a folder
  you pick (Garmin `.fit` or Connect zip, UDDF, Subsurface) to the list in
  one go; subfolders are not searched, dives already listed are skipped and
  counted, and a file that is no dive is named under the list.
- A dive with edits you have not saved yet carries a dot in the list and
  "edited" on its card, and a **Revert** button puts it back as it was read
  from the file (or as you last saved it); with several dives selected,
  "Revert N" reverts the edited ones among them. Remove, Clear and closing
  the app ask first when such a dive would be lost; saving a dive (as UDDF,
  Subsurface or .fit) clears its mark.
- **Save as .fit…** for every dive. A dive opened from a Garmin `.fit` (or a
  Connect zip) is written into a copy of the watch's own file - only the
  changed values are touched, every other byte stays as the watch wrote it -
  named `<original name> (edited).fit` beside the original, which is never
  overwritten. Start time and time zone, dive number, bottom time, surface
  interval, CNS, entry and exit position, gases, tank pressures and sizes,
  gradient factors, water type and density, dive mode and the average water
  temperature go in. A dive from a UDDF or Subsurface file, a hand-logged
  dive or one from another dive computer is written as a new Garmin `.fit`
  from scratch, with its profile and channels, gases and tanks, settings,
  summary, positions and time zone, named `<date> <time> dive <number>.fit`.
  Either way, what no `.fit` holds (site, buddy, notes, weight, visibility,
  a non-Garmin computer's name, a tank added on the page…) is listed under
  the buttons before you save, with the advice to save as UDDF or Subsurface
  for those. Garmin Connect knows a dive by its start time and device:
  delete the original activity there before uploading an edited file.
- **Merge…** joins a dive the computer split into several Garmin `.fit`
  logs into one. Select the logs (Cmd/Ctrl-click) and click Merge… under the
  list - on as soon as two or more dives are selected, with a tooltip and a
  line under the list saying what it does; the dialog shows the logs in
  start order and says in red why they cannot be merged (a dive from another
  file type, another dive computer, more than 2 hours apart). Choose to add
  the surface time (kept in the dive, so photos and videos still line up) or
  to remove it (one continuous dive that ends earlier), and Save puts the
  merged `.fit` beside the first log and adds it to the list. The originals
  are never changed. Garmin Connect treats the merged log as the first dive:
  delete the original activities there before uploading it.

**Other**
- Installers and Docker image: Garmin's FIT SDK (`garmin-fit-sdk`, under
  Garmin's FIT Protocol License), which writes the `.fit` files, is now part
  of every build and credited on the About pages.
- Documentation: the README is now a short overview and the user docs live
  in `docs/`, one page per app page plus the command line, configuration,
  Docker, web dashboard and building.

## 0.4.2 - 2026-10-04

- Convert page: a new **Open Cached** button next to Open files… opens the
  `.fit` files this app has downloaded from Garmin Connect for your Garmin
  account, a folder that is hard to find by hand. Save dialogs still open
  in your own folder afterwards. The button stays off, with a tip saying
  why, until the Garmin page's Download dives has cached something.

## 0.4.1 - 2026-10-02

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
