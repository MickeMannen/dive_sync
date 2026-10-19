# Changelog

All notable user-facing changes to DiveSync are recorded here. See `README.md`
for current features and setup, and `rework.md`/`features.md` for the full
development history and decision log.

## Unreleased

- UDDF: files exported by the Shearwater app read right. The PO2 no longer
  shows as 0.00 bar (the app writes bar where UDDF uses pascal), each
  transmitter's pressure curve stays on its tank, and the app's unused tank
  slots no longer appear as extra "Air 0 bar" tanks. When DiveSync writes
  into such a file it is saved in standard UDDF units.
- UDDF: a rebreather file that lists its gases but no tanks (APD's
  DiveSight, some other logbooks) now gets a gas list, as Subsurface imports
  it: the mixes as diluents, plus a bailout tank for a gas switched to on
  open circuit, and the gas switches point at those tanks. An APD
  Inspiration's three O2 cells are read too.
- Shearwater app: a rebreather dive's tanks are right. The app stores 21 %
  for every tank of a CCR dive; DiveSync now reads a transmitter named O2 as
  the oxygen tank (100 %) and one named D1, D2 or DIL as a diluent with the
  mix breathed on the loop. Open-circuit dives are unchanged.
- Dive edit: a Garmin Descent CCR dive shows its setpoint. The watch's low
  and high setpoint and its switches between them are read (a setpoint line
  in the profile tooltip, setpoint changes among the events), carried into
  UDDF and Subsurface files, and kept in a `.fit` written from the dive.
- Divelogs: the air temperature is read and written. It shows as the
  optional **Air temp** column on the Divelogs Dives page and is offered on
  the mapping board, so a rule can copy it between Divelogs and Subsurface,
  UDDF or the Shearwater app. No rule is drawn for it by default, so the
  first sync after upgrading changes nothing.
- Web dashboard: the alerts webhook can also report successful runs. Next
  to the webhook URL, choose **Send** when a run fails (the default, as
  before), when a run changes something or fails, or after every run; the
  message names the job, the direction and how many dives were added,
  updated, skipped and failed, and says when it was a dry run. Handy for
  ntfy or a home-automation trigger. In `settings.json` it is `notify_on`.
- Garmin (beta, off by default): new dives can reach Garmin Connect with
  their profile, tanks and gases. Tick **New Garmin dives as .fit (beta)** on
  the Sync page or in a web dashboard job (or pass `--garmin-fit-upload`):
  a dive the run creates on Garmin is uploaded as a `.fit` file instead of a
  summary-only manual activity, its name, notes, buddy, weight and location
  are set right after, and the log names what a `.fit` cannot carry. A dry
  run lists what would be uploaded; Connect's "duplicate activity" answer
  counts as skipped. Creating dives on Garmin must still be on for the pair.
  Try it on one dive first.
- Sync: dives logged on clocks in different time zones are matched instead
  of uploaded twice. Start times are compared in UTC where both services
  know the offset (Garmin, `.fit`, UDDF times with an offset); otherwise a
  pair whose local times are a whole number of quarter hours apart (up to
  14 hours) is matched when the max depth and duration agree closely and no
  other dive fits as well. The sync log names each dive matched this way.
  Dives already linked by earlier runs are never moved, and two dives
  starting equally close to one dive now go by depth and duration instead
  of list order.
- Subsurface (git and Cloud): a dive keeps every tank's pressure curve and
  the other values its dive computer logged per sample. Reading a Subsurface
  log now brings each cylinder's pressure (sidemount and stage tanks
  included), NDL, time to surface, deco stop, CNS, PO2 or a rebreather's
  setpoint and O2 cells, and heart rate - shown in the profile tooltip and
  carried to UDDF and `.fit` - and a new dive uploaded to Subsurface is
  written with all of them, each pressure on its own cylinder, instead of
  the first tank's pressure only. A sync still compares profiles on depth,
  temperature and time, so linked dives are not updated because of this.
- Exit position for drift dives: where you surfaced is kept beside the
  entry position. Garmin's end position is read (read-only), Subsurface
  (git and Cloud) reads and writes it as the dive computer's `GPS2` extra
  data, as Subsurface's own Garmin import does, and UDDF files keep it in
  DiveSync's own block of the dive. It is on the mapping board of those
  services with no shipped rule, so nothing changes on your next run until
  you draw one. UDDF saved from the Dive edit page no longer lists the exit
  position among what it leaves out.
- Water type, air temperature and suit are fields of the dive. They are
  read and written where a service holds them - Subsurface (git and Cloud:
  the salinity, `airtemp` and `suit`), UDDF files (`airtemperature` and a
  named suit under the owner's equipment), `.ssrf` files (`temperature air`
  beside the water temperature), the Shearwater app's air temperature (its
  water type is the computer's own setting, read only) and MySSI's water
  type (the air temperature read only) - and appear on the mapping board
  of those services, so a rule can sync them; none is in the shipped
  boards, so nothing changes on your next run until you draw one. The Dive
  edit page gets an **Air temp** and a **Suit** field, and the Subsurface,
  UDDF, Shearwater and MySSI dives pages three optional columns for them.
- SAC on every page: the dives pages and the Dive edit page show the dive
  computer's own surface air consumption when the dive holds one (a Garmin
  `.fit`, or a file that carries it) and otherwise the one computed from the
  tanks' sizes and pressures, the average depth and the duration, in L/min
  or cuft/min as the Units setting says. On the Dive edit page the SAC is a
  field of its own (the computed value sits in its placeholder, so it is
  never stored by mistake) with RMV and OTU read-only beside it, and an
  edited SAC goes into the `.fit` written by **Save as .fit**.
- Rebreather dives: the O2 cells are kept. Each cell's measured PO2 is now
  a value of its own beside the computer's PO2 and the setpoint, so a
  Subsurface `.ssrf` with `sensor1`..`sensor3` reads with its cells and is
  written back with them, a UDDF file carries them as `measuredpo2` per
  `o2sensor` (and reads other apps' files that do), and the profile tooltip
  shows a `Cells 1.22 / 1.19 / 1.21 bar` line that holds each cell's last
  reading. A Garmin `.fit` has no field for them, so **Save as .fit** names
  "O2 cell readings" among what it leaves out. A dive read from the
  Shearwater app's database now keeps its computer's three O2 cells too
  (each cell's millivolts times the calibration stored in the log, bar per
  cell; cells still at the factory calibration are left out, as Subsurface
  does, and the computer's own PPO2 stays).
- **Rebreather (CCR) support is untested.** None of the above - the cells,
  the setpoint, a diluent tank, the Shearwater cell readings - has been
  through a real CCR dive yet (no such dive was available). The Dive edit
  page and the Subsurface, UDDF, Shearwater and MySSI dives pages show a
  muted note on a dive that holds rebreather data saying so; compare the
  values with your dive computer's own log before relying on them.
- Shearwater app: a dive read from the app's database now keeps everything
  its computer logged per sample - the pressure of every paired transmitter
  (each under its own tank, so a sidemount dive with two transmitters shows
  both), NDL, time to surface, deco stop, CNS, ppO2, the setpoint on a
  rebreather dive and GF99 - plus its gas switches and the computer's
  model, serial, gradient factors, dive mode and water setting. Before,
  only the first transmitter's pressure survived. The data reaches a UDDF
  file, a `.fit` saved from the Dive edit page and the profile tooltip of
  the dives pages; a sync still compares profiles on depth, temperature and
  time, so nothing is re-sent because of it.
- Dive edit: a Garmin `.fit` keeps more of what the watch wrote. The
  surface air consumption (SAC), RMV and oxygen toxicity units (OTU) of its
  dive summary are now read, and a rebreather's diluent tank keeps that
  role, so a dive that goes through the app and out again as a new `.fit`
  (**Save as .fit** for a dive that did not come from one) still carries
  them.
- Dive edit: a dive logged in a half-hour or quarter-hour time zone (India,
  Nepal, Newfoundland, Adelaide) keeps its zone. A Garmin `.fit` from such a
  dive used to lose the zone (the local time was right, the zone blank);
  now it reads as `+05:30`, is saved back the same way, and a zone typed on
  the page is kept at the same precision.

- Dive edit and dives pages: the depth-profile tooltip shows the last
  reading of a value the dive computer did not log at that sample (a tank
  pressure, the temperature, the NDL), so it no longer blinks in and out
  along the profile; a long tooltip wraps into two columns.

- Sync: a run that fails part way (a dropped connection, a service error on
  a later dive) no longer forgets the dives it had already created on the
  other side. Each pair is remembered the moment its upload succeeds, so
  the next run carries on from there instead of creating those dives again
  on Garmin or Divelogs, which keep no foreign id.
- Sync: an update the target refused or could not do is no longer reported
  as updated, and a new dive the target did not add no longer disappears
  from the summary as if nothing happened. Such writes are listed as
  **failed** - in the CLI's summary block, the web dashboard's job results
  and run history (with the service, id and reason), and the desktop Sync
  page's status line ("Sync complete. 1 dive failed — check the log.") -
  and the log says why. A dive the target already has (Divelogs' "already
  exists", MySSI's duplicate check) is counted as skipped, not failed.
- Shearwater: a Mirror run (or a pair with propagate deletes on) towards the
  Shearwater app no longer logs a false "Failed to delete" warning for every
  dive the other side does not have; dives are never deleted from the app's
  database, so the run now says what it would have deleted and counts it as
  skipped, like MySSI.
- MySSI (beta): **MySSI is a sync service** like the others - a source and a
  target on the desktop app's Sync page (with its own account picker and a
  note under the pair saying what the beta means), on the command line
  (`--source garmin --target ssi`, `ssi:<email>` or `--ssi-account` to pick an
  account) and on the web dashboard (Sync now, scheduled jobs with a MySSI
  account per job, Download dives). It gets a mapping board and a place on
  the Conflicts page like every other pair. A sync towards MySSI **adds new
  dives only** until updating a MySSI dive has been tested: a change to a
  dive already there is logged per dive, not sent; a dive with two or more
  tanks is skipped with a line saying why; and **no sync run deletes on
  MySSI** - not Mirror, not a pair with propagate deletes on - it logs what
  it would have deleted and counts it as skipped (a MySSI delete is
  permanent and the dive number may not come back); only the MySSI Dives
  page deletes there. MySSI has no public API, so the
  connection is the MySSI app's own, which SSI can change or close at any
  time; the whole feature is beta and not fully tested.
- Settings page: the **MySSI** card is an account list like Divelogs' -
  any number of MySSI logins with Test, Save and Remove, kept in the OS
  keychain, the beta note under the title. The one login saved by the
  previous version was moved into the list. The CLI and the Docker image
  take the same accounts in `credentials.json` (`"ssi"`, written by
  `setup_credentials.py --services ssi`), and the web dashboard's Settings
  page edits them.
- A **MySSI Dives** page (after Subsurface Dives): your MySSI logbook in the
  same table as the other Dives pages, with the depth profile, the main tank
  and the dive site, an account picker, Refresh and Delete (the confirmation
  says that a MySSI delete is permanent). Editing a MySSI dive comes after
  the update test; until then the editor is read-only and Save says so.
- Dive edit page: the **Open MySSI** and **Send to SSI…** buttons of the
  previous version are gone - MySSI is synced from the Sync page and browsed
  on its own Dives page instead.

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
