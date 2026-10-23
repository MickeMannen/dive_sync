# Command line

The synchronization command-line interface is accessed via `sync.py`.

```bash
# Run a dry-run sync (simulate transfers without modifying remote data)
python sync.py --dry-run

# Run the sync in the saved direction (settings.json: "directionality", e.g. to_divelogs)
python sync.py

# Sync the other way in a second run - a run only ever writes one side
python sync.py --direction to_garmin

# Download all raw history JSONs into local data caches (defaults to `./data`)
python sync.py --save-raw-data

# Execute sync targeting a specific user pair (for multi-account configurations)
python sync.py --garmin user1@domain.com --divelogs user1_divelogs

# Back up your entire synced history to flat local JSON files
python sync.py --backup --garmin-path my_garmin.json --divelogs-path my_divelogs.json
```

### Options Overview:
* `--date-from YYYY-MM-DD`: Sync only dives occurring on or after this date.
* `--date-to YYYY-MM-DD`: Sync only dives occurring on or before this date.
* `--full-sync`: Force a full sync instead of an incremental check.
* `--no-garmin-cache`: Fetch every Garmin dive from Garmin Connect. By default a sync reads a Garmin dive from the local refresh cache when Garmin's activity list shows it unchanged (and caches what it does fetch); that list does not reveal an edit to only notes, buddy, weight or visibility, which is what this flag is for.
* `--direction`: Which side this run writes: `to_garmin`, `to_divelogs`, `to_<service>`, `to_target` or `to_source`. Without it the pair's saved direction is used. `bidirectional` is no longer accepted — run each direction separately. **Deletes follow the direction**: with *propagate deletes* on, a dive gone from the sending side is deleted on the receiving side; a dive gone from the receiving side is left alone by that run (the opposite run removes it from the sender). A sync started from the desktop app's Sync page never deletes unless *Mirror* is ticked.
* `--overwrite`: With `--save-raw-data`, fetch every Garmin dive again instead of reusing the unchanged ones (other services are always downloaded in full).
* `--garmin-fit-upload` (beta): The new dives this run creates on Garmin Connect are uploaded as `.fit` files (profile, tanks and gases arrive; name, notes, buddy, weight, visibility and location are set on the activity right after) instead of manual activities with the summary only. Creating dives on Garmin must be on for the pair (`create_on_garmin`). Without the flag the setting `garmin_fit_upload` (per pair in `sync_pairs`, or the top-level default; off) applies. With `--dry-run` the log lists each dive that would be uploaded and what its `.fit` cannot carry. A dive without a profile still goes as a manual activity; Connect's "duplicate activity" answer counts as skipped.
* `--ssi-account EMAIL`: Which MySSI account to run as when several are in `credentials.json` (the same as naming it in the spec, `ssi:<email>`; also applies to `--pair`). Optional with one account.
* After a run the summary block lists what was uploaded and updated per side, the conflicts left for `--list-conflicts`, and under **Failed** the writes the receiver refused or could not do (an update it did not take, a dive it could not add) - those are never counted as done; the log above says why.
* `--units metric|imperial`: How this run shows depths, temperatures, pressures, weights and visibility in `--list-conflicts`, `--test-mapping` and the log lines about a rule's two values: metric (m, bar, °C, L, kg; the default) or imperial (ft, psi, °F, cuft, lb, rounded like a dive computer). For this run only, nothing is stored; the data, the files and the services stay metric whichever you pick.

### Other services: UDDF files, Subsurface and the Shearwater app

> **Shearwater: desktop app only, still being tested.** The Shearwater app keeps its database on the computer it runs on, so the Shearwater sync is a feature of the DiveSync desktop app on that computer; the Docker image and its web dashboard cannot reach it, and the dashboard has no Shearwater settings. Reading dives, profiles and writing dive details back have been verified against the author's own account; more testing is ongoing, so keep a backup of the app's data (DiveSync copies the database to `backups/shearwater/` before every write) and use a second Shearwater account for trying it out.


Besides Garmin and Divelogs, a sync can target a UDDF 3.2 file (which both Subsurface and Submersion import and export) or a Subsurface git-storage directory (what Subsurface Cloud and Subsurface's git repositories contain), and it can read the Shearwater app's database (the app is Shearwater's "Shearwater Cloud" desktop program; DiveSync works on its local file and never talks to the cloud) (`dive_data.db`) as a source of dives - Perdix and other Shearwater computer downloads with their gases and the dive profile decoded from the computer's own log (every sample as logged: depth, temperature, the pressure of every paired transmitter under its own tank, NDL, time to surface, deco stop, CNS, ppO2, the setpoint and the calibrated O2 cells on a rebreather dive - untested: no CCR dive has been through the decoder yet - and GF99, plus the gas switches, the computer's model and serial, gradient factors, dive mode and water setting); on a rebreather dive, where the app stores 21 % for every tank, a transmitter named O2 is read as the oxygen tank and one named D1, D2 or DIL as a diluent with the mix breathed on the loop). The profile's extra channels and events are carried, not synced: they go into a UDDF file, a `.fit` saved from the Dive edit page and the profile tooltip of the dives pages, while a sync compares profiles on depth, temperature and time only; Subsurface (git and Cloud) holds them all - every tank's pressure under its own cylinder and the NDL, time to surface, deco stop, CNS, PO2 or setpoint, O2 cells and heart rate per sample - so a dive uploaded there keeps them and a Subsurface dive read by DiveSync brings its own along (to a UDDF file, a `.fit`, the profile tooltip); an existing Subsurface profile is never rewritten by a sync. Divelogs' profile has no pressure slot. A log that cannot be decoded leaves that dive without a profile and logs a warning. Dives are never created or deleted in that database (a Mirror run, or a pair with propagate deletes on, logs each dive it would have deleted there and leaves it, counted as skipped), but metadata (site, buddy, notes, dive number, weight, entry position, air temperature and the app's own fields such as environment, weather, platform or dress) is written back into it the way the app writes it, so the app uploads the change to Shearwater Cloud on its next sync. Close the app before such a run: DiveSync refuses to write while it is open, and copies the database to `backups/shearwater/` first. The values of the app's dropdown fields must be one of its options. The app keeps one database per Shearwater account, and DiveSync treats them like accounts: in the desktop app, Settings → Shearwater app → *Detect* lists every account the app has on this Mac; keep the ones to sync, and the Sync page picks one per run (with nothing saved, the app's active account is used). For the CLI, `credentials.json` (`"shearwater": [{"database": "...", "account": "..."}]`) takes a path to a copy or a mounted file. Once a database is known, "Shearwater Cloud" is offered as a Source or Target on the Sync page and gets a mapping board with every other configured service; the CLI names a file as `shearwater:<path>`. Name the two sides with `--source` and `--target`, or configure a pair once in `settings.json` and run it by id:

```bash
python sync.py --source garmin --target uddf:garmin_export.uddf --full-sync     # file under DATA_DIR
python sync.py --source divelogs --target subsurface:/path/to/subsurface-checkout
python sync.py --source "shearwater:/path/to/dive_data.db" --target subsurface-cloud   # close the Shearwater app first
python sync.py --pair to-subsurface                                             # a pair from sync_pairs
```

```json
"sync_pairs": [
  {"id": "to-subsurface", "source": "garmin", "target": "subsurface:subsurface-repo", "directionality": "to_subsurface"}
]
```

Each pair has its own direction, grace window and mapping board (defaults are generated per pair). The Garmin ↔ Divelogs pair is itself an entry in `sync_pairs`, always the first one, with the fixed id `garmin_divelogs` — `python sync.py` with no `--pair`, the Docker default job and the boards' first entry all mean that pair. Settings files written before this (a top-level `directionality` / `field_links`) are upgraded on first load; the file then says `"settings_version": 2`.

**Subsurface Cloud** needs no checkout of your own: with the account configured by `setup_credentials.py --services subsurface`, the spec `subsurface-cloud` clones the repository under `DATA_DIR/subsurface/<account>/cloud/`, refreshes it from the cloud at the start of every run, and commits and pushes once at the end. It never force-pushes; if Subsurface pushed in between, the run's changes are replayed on top of the new state.

```bash
python sync.py --source garmin --target subsurface-cloud --full-sync
```

**MySSI** (beta) is a sync side like the others once an account is in `credentials.json` (`"ssi": [{"email": "...", "password": "..."}]`, written by `setup_credentials.py --services ssi`, which checks the login; the token MySSI hands out is kept in the file by the first run so the next one skips the login). The spec is `ssi`, or `ssi:<email>` to pick one of several accounts (`--ssi-account <email>` is the same pick). MySSI has no public API or file import: DiveSync uses the MySSI app's own connection, which SSI can change or close at any time, and the whole feature is not fully tested yet. A run towards MySSI **adds new dives only** (a change to a dive already there is logged per dive, not sent, until updating has been tested), **skips a dive with two or more tanks** with a line saying why, and **never deletes on MySSI**, not even with *propagate deletes*; the board has no dive-number rule (MySSI numbers its own dives), so dives are matched by the sync history, the dive-computer reference DiveSync sent and the start time. Garmin → MySSI is the exercised path.

```bash
python sync.py --source garmin --target ssi --dry-run                 # one account configured
python sync.py --source garmin --target ssi:me@example.com --full-sync   # or --ssi-account me@example.com
```

### Mapping, conflicts and profiles

Which field feeds which, in what direction, and who wins a conflict is defined by the *mapping board* — see [Mapping](mapping.md) for what that actually means. Edit it visually in the desktop app or on the web dashboard, or from the CLI:

```bash
# Show the board as a table (and any problems with it)
python sync.py --show-mapping

# Rehearse the board read-only against the newest 10 dives per side
python sync.py --test-mapping

# Links with conflict policy "manual" queue conflicts instead of overwriting
python sync.py --list-conflicts
python sync.py --resolve <conflict-id> source     # or: target

# Move the whole sync configuration (never credentials) between machines
python sync.py --export-profile dive_sync_profile.json
python sync.py --validate-profile dive_sync_profile.json
python sync.py --import-profile dive_sync_profile.json     # shows a summary, asks before applying (--yes skips)
```
