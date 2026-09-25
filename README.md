# Dive Sync 🤿✨ 

# Solution is still in Beta!!!!!
## I have mainly tested Garmin sync to Divelogs and not the other direction - be careful and keep a backup!

> **Note:** Submersion sync doesn't work as of now and is disabled: it isn't offered in the status page, the desktop app, `setup_credentials.py` or the CLI. The Submersion sections below describe how it is meant to work once it is re-enabled (`SUBMERSION_ENABLED` in `src/core/config.py`).


Dive Sync is a dive log synchronization engine that seamlessly matches and syncs your dive history between **Garmin Connect** and **Divelogs.org**. 

I started this project when i got back to diving and had to import all dives from logbooks to Garmin and to Divelogs.
I managed to make the import but the stability of the code wasn't good enough for release. Because of lack of time I didnt continue but with Gemini I saw the opportunity to finalize the project.

There are most likely a lot of bugs so please use it carefully, I will use the docker container myself and fix issues as I see them.

It features two-way syncing (one direction per run), detailed telemetry parsing (depth/temperature graphs and gas mixture sensors), and multi-account support.

**Project direction**: the Docker deployment is a scheduled-sync engine with a status page (schedule, credentials, mapping board — no dive editing there). Interactive dive editing lives in the native desktop app (`python -m desktop`; Briefcase + PySide6/Qt Quick, macOS first, Windows and Linux planned) — see [rework.md](rework.md) for the plan and status.

<p align="left">
  <a href="https://skillicons.dev">
    <img src="https://skillicons.dev/icons?i=python,fastapi,js,html,css,docker,bash,git,gemini,claude" alt="Tech Stack" />
  </a>
</p>

---

## 🚀 Key Features

* **Two-Way Syncing, One Direction Per Run**: Every run has exactly one receiving service (to Divelogs, to Garmin, to Submersion, ...). To keep two services in step both ways, schedule one job per direction — the second run sees what the first wrote, so nothing needs a tiebreak and no single run ever writes both sides.
* **Telemetry & Gas Mapping**: Maps complex dive metrics, temperature profiles, start/end tank pressures, gas mixtures (Nitrox/Trimix), and telemetry graph coordinates.
* **Field-Level Mapping Board**: A drag-and-drop editor (status page and desktop app) of what each service accepts from the other, field by field, with a policy per rule, composite templates, and a queue for conflicts that need a manual pick.
* **Scheduled Sync**: Configure one or more cron-like jobs (hourly/daily/weekly/custom interval, per-job direction and filters) that run unattended.
* **Status Page**: A read-only web page showing whether a sync is running, the last result, the next scheduled run, and a live log tail — plus a small form for credentials and schedule configuration.
* **Multi-Account Support**: Configure multiple Garmin and Divelogs credentials. Run the sync globally or target specific accounts using selection arguments.
* **Docker Ready**: Package and run the scheduler with custom port routing and unified volume mapping to persist settings, credentials, session tokens, and data caches.

---

## 🛠️ Installation & Setup

### 1. Prerequisites
Ensure you have **Python 3.10+** installed. Clone the repository and install dependencies:
```bash
pip install -r requirements.txt
pip install -e .
```

### 2. Configure Credentials
Run the interactive setup tool to provision and verify your accounts:
```bash
python setup_credentials.py                      # all services, one section each
python setup_credentials.py --services garmin    # just one
```
Each section verifies the login read-only before saving and leaves the other sections untouched. Credentials go to `credentials.json` in `DATA_DIR` (the current directory when unset), so a second set of accounts for testing is simply a second directory:
```bash
DATA_DIR=~/.dive_sync_test python setup_credentials.py
DATA_DIR=~/.dive_sync_test python sync.py --dry-run
```
Supported services:
* **Garmin Connect** and **Divelogs.org**: username and password.
* **Subsurface Cloud**: the email and password from Subsurface's cloud storage preferences. Verified with one read-only request against the cloud git server.
* **Submersion**: the S3-compatible bucket Submersion syncs through. Backblaze B2 is the recommended hosted option: create a bucket, then an application key restricted to it; the endpoint is `https://s3.<region>.backblazeb2.com`. Point Submersion's sync provider at the same bucket. Google Drive cannot be used (see [docs/submersion_sync_format.md](docs/submersion_sync_format.md)). If the library has end-to-end encryption turned on in Submersion, also set a `passphrase` — otherwise leave it blank.

Subsurface Cloud is fully supported; Submersion sync doesn't work as of now and is disabled (see "Other services" below).

#### Single Account Structure (`credentials.json`):
```json
{
  "garmin": {
    "username": "user@domain.com",
    "password": "yourpassword",
    "token_dir": "tokens/garmin"
  },
  "divelogs": {
    "username": "user_divelogs",
    "password": "yourpassword"
  },
  "subsurface": {
    "email": "you@example.com",
    "password": "yourpassword",
    "base_url": "https://cloud.subsurface-divelog.org/"
  },
  "submersion": {
    "store_type": "s3",
    "endpoint_url": "s3.eu-central-003.backblazeb2.com",
    "bucket": "my-submersion-sync",
    "access_key_id": "…",
    "secret_access_key": "…",
    "passphrase": ""
  }
}
```
The `subsurface` and `submersion` sections are optional; an older file without them still loads.

The `submersion` fields are the ones Submersion's own sync settings ask for. An
`endpoint_url` without a scheme is read as `https://`; give it an explicit
`http://` only for a self-hosted store without TLS. Three more settings sit
behind **Advanced** in both UIs and can be left out of the file entirely:
`region` (otherwise read out of the endpoint — B2, S3, R2, Wasabi, Spaces,
Scaleway; a self-hosted store needs it set by hand), `prefix` (defaults to
`submersion-sync/`, what Submersion writes under) and `path_style` (off;
some self-hosted stores need it on).

#### Multiple Accounts Structure (`credentials.json`):
You can configure a list of credentials. The sync engine will segregate data directories per user (e.g. `./data/garmin/user@domain.com/`):
```json
{
  "garmin": [
    {
      "username": "user1@domain.com",
      "password": "pass1",
      "token_dir": "tokens/garmin"
    },
    {
      "username": "user2@domain.com",
      "password": "pass2",
      "token_dir": "tokens/garmin"
    }
  ],
  "divelogs": [
    {
      "username": "user1_divelogs",
      "password": "pass1"
    },
    {
      "username": "user2_divelogs",
      "password": "pass2"
    }
  ]
}
```

---

## 💻 CLI Usage

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
* `--direction`: Which side this run writes: `to_garmin`, `to_divelogs`, `to_<service>`, `to_target` or `to_source`. Without it the pair's saved direction is used. `bidirectional` is no longer accepted — run each direction separately. **Deletes follow the direction**: with *propagate deletes* on, a dive gone from the sending side is deleted on the receiving side; a dive gone from the receiving side is left alone by that run (the opposite run removes it from the sender).
* `--overwrite`: With `--save-raw-data`, fetch every Garmin dive again instead of reusing the unchanged ones (other services are always downloaded in full).

### Other services: UDDF files and Subsurface

Besides Garmin and Divelogs, a sync can target a UDDF 3.2 file (which both Subsurface and Submersion import and export) or a Subsurface git-storage directory (what Subsurface Cloud and Subsurface's git repositories contain). Name the two sides with `--source` and `--target`, or configure a pair once in `settings.json` and run it by id:

```bash
python sync.py --source garmin --target uddf:garmin_export.uddf --full-sync     # file under DATA_DIR
python sync.py --source divelogs --target subsurface:/path/to/subsurface-checkout
python sync.py --pair to-subsurface                                             # a pair from sync_pairs
```

```json
"sync_pairs": [
  {"id": "to-subsurface", "source": "garmin", "target": "subsurface:subsurface-repo", "directionality": "to_subsurface"}
]
```

Each pair has its own direction, grace window and mapping board (defaults are generated per pair). The Garmin ↔ Divelogs pair is itself an entry in `sync_pairs`, always the first one, with the fixed id `garmin_divelogs` — `python sync.py` with no `--pair`, the Docker default job and the boards' first entry all mean that pair. Settings files written before this (a top-level `directionality` / `field_links`) are upgraded on first load; the file then says `"settings_version": 2`.

**Subsurface Cloud** needs no checkout of your own: with the account configured by `setup_credentials.py --services subsurface`, the spec `subsurface-cloud` clones the repository under `DATA_DIR/subsurface_cloud/`, refreshes it from the cloud at the start of every run, and commits and pushes once at the end. It never force-pushes; if Subsurface pushed in between, the run's changes are replayed on top of the new state.

```bash
python sync.py --source garmin --target subsurface-cloud --full-sync
```

**Submersion** *(disabled for now — Submersion sync doesn't work yet)* joins the library's sync store as one more device (S3 bucket or a local synced folder, configured under `submersion` in `credentials.json`), and syncs **metadata only**:

```bash
python sync.py --source garmin --target submersion --direction to_submersion
```

Submersion builds a dive's depth profile, cylinders, tank pressures and gas switches from the `.fit` file you import in the app itself, and those tables reference one another. dive_sync does not write them: **import each dive computer dive's `.fit` in Submersion first** (export it from Garmin Connect, or from your watch), then let a sync fill in the dive site, dive name, buddy, notes, weight, visibility, GPS and the extra Submersion fields on the dive that import created. A dive you entered by hand on Garmin Connect has no `.fit` to import, so dive_sync creates that one for you, gas and all. To have dive_sync create recorded dives too (profile from Garmin's API, no tank pressures), set `"create_device_dives_on_submersion": true` in `settings.json` or tick the box on the status page.

### Mapping, conflicts and profiles

Which field feeds which, in what direction, and who wins a conflict is defined by the *mapping board* — see the "🗺️ Mapping Board" section below for what that actually means. Edit it visually on the status page or in the desktop app, or from the CLI:

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

---

## 🗺️ Mapping Board (fields, rules, templates and conflicts)

Every synced field is controlled by the *mapping board*, identical whether you edit it on the status page, in the desktop app, or by hand in `settings.json`. There's one board per sync pair — the Garmin ↔ Divelogs pair (`garmin_divelogs`) and one more for each pair you add (a UDDF file, a Subsurface checkout, Subsurface Cloud, Submersion) — and a board is two lists of **rules**, one per receiving side: *what Divelogs takes from Garmin* and *what Garmin takes from Divelogs*. A sync run writes exactly one side, and applies that side's list.

**Field catalogue.** Each service publishes what it can read and write (`GET /api/fields`) — a read-only field (e.g. Divelogs numbers its own dives) can never be written by a rule; the board UIs show these as locked.

**A rule** says what one field of the receiving side *accepts*: `receiver field ← sender field(s) [+ template], policy`. It has:
- **A policy**, read from the receiver's side — what happens when the sender's value differs from mine:
  - `manual` *(the default for most fields)* — a blank field is filled for free; a genuine disagreement is queued in the **Conflicts** view for you to pick a winner. Nothing is ever silently overwritten.
  - `prefer_non_empty` — the same "fill me when blank, never overwrite a real value" behaviour as `manual`, except a real disagreement is just logged, not queued. Used for `samples` (a depth/temperature profile "conflict" is hundreds of points — nothing to usefully arbitrate one at a time).
  - `prefer_source` — take the sender's value whenever it has one, even over a different value already here — but an *empty* sender never blanks a field that already has data. Used for `tanks`, so the dive computer stays the source of truth for gas data even after the other side has stale readings.
  - `source_wins` — always take the sender's value, blank or not. Available if you want stricter behaviour than `manual`.
  - `target_wins` — "never overwrite me": the receiving side owns this field and the rule is a deliberate no-op (new dives are not filled from it either). Useful to state the intent on the board rather than leave the field without a rule.
- **A template** (composite rules), optionally with a **reverse pattern** — see below.
- Fields without a rule are not synced. A field that syncs both ways is simply the same rule on both receivers.

**Templates (composite rules).** A rule's field can be rendered from a small text template combining several of the sender's fields instead of copying one field verbatim. The shipped `activity_name` rule is the example: Garmin's activity title is built from Divelogs' region and dive site —
```
template: "{divelogs.location}, {divelogs.divesite}"
```
so a Divelogs dive with location `Larnaca` and dive site `Zenobia` uploads to Garmin titled `Larnaca, Zenobia`. In either board UI, you build one by dropping a *second* field onto a field that already has a rule; the editor shows a live preview as you edit the template.

**Reverse parsing (splitting a composite back).** A composite rule can also work backwards by adding a `reverse` regex with named groups matching its source field names, e.g. `(?P<divesite>.+) \((?P<location>.+)\)` for a template of `{divesite} ({location})`. On a run *towards the sources' side*, whenever the composite's field has been hand-edited to something the template no longer reproduces, the pattern is matched against its whole current value and, if it fully matches, splits it back into the named fields. That split has its own policy (`reverse_conflict`; blank = the rule's own policy), read from the sources' side. A value that no longer fits the pattern at all is left alone rather than guessed at. Only text-typed source fields are supported. Both board UIs show a live self-check next to the preview — "reverse -> {...}" — proving the pattern actually inverts the template on the example dive.

**Match keys.** Before falling back to start times, the engine can pair dives on a field that both sides carry (a dive number, a timestamp): the pair's ordered `match_keys` list, edited in the strip under the board. A hit only counts within a day of each other.

**Shipped defaults (Garmin ↔ Divelogs).** Buddy, notes, weight, visibility, GPS, and site name are accepted on both sides with `manual`; tanks and depth/temperature profiles are accepted by Divelogs only (Garmin's gas API can't be written, and can't record samples at all); Garmin's activity title is built from Divelogs' location/dive site for new dives (see the template above). A pair beyond Garmin ↔ Divelogs starts from a smaller, generic default board covering the fields both sides actually have. If you want the old pre-board behaviour (Garmin always wins every difference), that board is still available as `fields.legacy_field_links()`.

**The conflict queue.** Any `manual`-policy rule where both sides hold a value and they differ gets recorded (`conflicts.json`) instead of being resolved automatically. Resolve one from the status page or desktop app's **Conflicts** view (pick which side wins; the other side gets updated), or from the CLI (`--list-conflicts`, `--resolve <id> source|target`). An unresolved conflict is re-evaluated — and re-queued if it's still unresolved — on the next run that revisits that dive.

**Editing the board.** Visually: each receiving side is a panel, "Y → X" (sender on the left, receiver on the right) (the side the current direction writes comes first, badged); drag a field from the sender's list on the left onto a field on the right to add a rule; drag the same text field onto a second field to **split** it (e.g. Garmin's *Activity name* "Gozo, Blue Hole" onto Divelogs' *Location* and then *Dive site*: the split is cut at the text between the fields in its template, applies to matched and newly uploaded dives, and shows as "⇠ split of …" in the panel it writes); click a rule to change its policy, template or split; **Save** asks whether to re-apply the changed mapping to every already-matched dive on the next run. By hand: `sync_pairs[].rules` in `settings.json` (see below), or `POST /api/settings` — the API also still accepts a version-1 board as `field_links` (top level for the `garmin_divelogs` pair, or on a pair) and stores it as rules.

**How a board is stored (`settings_version` 2).** Under the receiver's id in the pair's `rules`, plus the pair's `match_keys`:
```json
"sync_pairs": [{
  "id": "garmin_divelogs", "source": "garmin", "target": "divelogs", "directionality": "to_divelogs",
  "rules": {
    "divelogs": [{"id": "buddy", "target": "divelogs.buddy", "source": ["garmin.buddy"], "conflict": "manual"}],
    "garmin":   [{"id": "buddy", "target": "garmin.buddy",   "source": ["divelogs.buddy"], "conflict": "manual"},
                 {"id": "activity_name", "target": "garmin.activityName",
                  "source": ["divelogs.location", "divelogs.divesite"], "conflict": "manual",
                  "template": "{divelogs.location}, {divelogs.divesite}"}]
  },
  "match_keys": []
}]
```
`python sync.py --show-mapping` prints a board as "what X takes from Y" per side, and `--test-mapping` names the receiver on every row. Settings files and sync profiles written before version 2 (a top-level `field_links` list with a direction per link) are upgraded on load; a profile exported by this version is version 2 and older builds refuse it.

---

## 🐳 Docker Deployment

The application is fully containerized. You can run the web dashboard server in background mode, mount a local volume for configurations/session persistence, and select a port:

### 1. Pull Pre-built Image from Docker Hub
```bash
docker pull mickemannen/dive_sync:latest
```

### 2. Run the Container
Map a local host directory (for config, caches, and token persistence) and route the dashboard to your chosen port (e.g. `8080`):
```bash
docker run -d \
  --name dive_sync \
  -p 8080:8000 \
  -v /absolute/path/to/local/dir:/app/data \
  mickemannen/dive_sync:latest
```

The container exposes:
- **Status Page**: available at `http://localhost:8080` — sync status, live log tail, credentials, and schedule configuration
- **Volume Mount**: `/app/data/` (contains `settings.json`, `credentials.json`, `tokens/`, `garmin/`, and `divelogs/`)

### 3. Automated Docker Hub Builds on Release (GitHub Actions)
An automated GitHub Actions workflow (`.github/workflows/docker-release.yml`) builds and publishes updated multi-architecture images (`linux/amd64`, `linux/arm64`) to Docker Hub whenever a new GitHub Release is published or a version tag (e.g., `v1.0.0`) is pushed.

#### Required GitHub Secrets:
Set the following secrets in your GitHub repository (**Settings ➔ Secrets and variables ➔ Actions**):
- `DOCKERHUB_USERNAME`: Your Docker Hub username (`mickemannen`)
- `DOCKERHUB_TOKEN`: A Personal Access Token (PAT) generated in Docker Hub (**Account Settings ➔ Security ➔ Personal access tokens**)

#### Updating the Docker Hub Overview Page:
The GitHub workflow automatically syncs [`DOCKERHUB.md`](DOCKERHUB.md) to your Docker Hub repository description overview page (`hub.docker.com/r/mickemannen/dive_sync`) on every release.

---

## 🖥️ Status Page

Start the status page locally without Docker:
```bash
python docker_run.py
```
Open `http://localhost:8000` in your web browser.

The status page provides:
1. **Status**: whether a sync is currently running, the last result, the next scheduled run, and a manual "Sync now" trigger (with an optional dry-run toggle).
2. **Live log**: a streamed tail of the scheduler's log output.
3. **Mapping board**: one board per sync pair — see the "🗺️ Mapping Board" section above for what a rule, its policy and its template mean.
4. **Conflicts**: what rules with the `manual` policy have queued, with a button per side to resolve.
5. **Credentials**: set/test Garmin, Divelogs.org and Subsurface Cloud credentials without editing `credentials.json` by hand.
6. **Sync pairs** and **scheduled jobs**: pairs beyond Garmin ↔ Divelogs (UDDF file, Subsurface checkout, Subsurface Cloud), and cron-like jobs that can name a pair.
7. **Sync profile**: export the whole configuration as a file, or import one after reviewing the diff.

There is no dive-editing UI here — that capability belongs to the desktop app (see [rework.md](rework.md)).

---

## 🧪 Testing

Run all unit, mock, and API tests to verify execution logic:
```bash
./run_tests.sh
```

---

## ⚠️ Limitations

- **Beta quality.** Garmin → Divelogs and Garmin → Subsurface are the exercised paths; Divelogs → Garmin and Subsurface → Garmin much less so. Keep backups (`--backup`, and Subsurface Cloud keeps its git history).
- **Gases and tanks never reach Garmin Connect.** Garmin's activity API accepts no gas or tank data, on creation or update; gases on Garmin come only from the dive computer and from tank sensors assigned in the Garmin Connect mobile app. Dives uploaded to Garmin therefore arrive without tanks. Reading works: once a second transmitter is assigned to a dive in the mobile app, both tanks are visible to dive_sync. Details of the investigation: [docs/garmin_diving_api.md](docs/garmin_diving_api.md).
- **Divelogs numbers dives itself** from date and time, so dive numbers are never synced or matched on that pair.
- **Profiles and tanks flow one way to Divelogs** (Garmin cannot take them back). Divelogs stores profiles at a fixed sample rate; irregular Garmin profiles are resampled on the way.
- **Two runners, one account.** Docker (scheduled) and the desktop app (manual) may sync the same accounts from different machines. There is no shared lock and each keeps its own `sync_state.json`, `conflicts.json` and Garmin token cache; both honour `api_cooldown_seconds`, but back-to-back runs from two machines can still hit Garmin's login rate limit (HTTP 429, wait 10–15 minutes). Let one runner finish before starting the other.
- **Pairs are remembered locally.** Garmin and Divelogs cannot store each other's id, so the matched pairs live in `sync_state.json` next to your settings. Deleting that file makes the next run re-match by time (fine) and forget which dives it uploaded (uploads are not repeated because they now match by time, unless the times were shifted).
- **Multi-account** works only from the CLI (`--garmin`, `--divelogs`); the scheduler, status page and desktop app assume one account per service.
- **Submersion takes metadata only.** The profile, cylinders, tank pressures and gas switches come from the `.fit` file you import in the Submersion app; dive_sync syncs the soft fields onto the dive that import created, and leaves a recorded dive uncreated until you import it (see "Other services" above).
- **Submersion**: Google Drive as its store is not reachable by third parties at all, so the library's sync provider must be S3, Dropbox or iCloud (see `docs/submersion_sync_format.md`); Dropbox/iCloud need the desktop app pointed at the local synced folder, not the CLI/Docker deployment. dive_sync publishes a full base snapshot on every write rather than incremental changesets, and there is no 7-day heartbeat scheduler yet, so an unattended dive_sync-only peer can go quiet long enough for another device to consider it stale.

---

## 🔒 Security

The status page has **no authentication**. Anyone who can reach its port can read the log, change the schedule and save credentials. Run it on a private network only, or behind something that authenticates for it: a reverse proxy with a login (Caddy, nginx, Authelia), a VPN, or Tailscale. Note that the default `DIVE_SYNC_HOST=0.0.0.0` binds every interface of the host; use `127.0.0.1` when a proxy on the same machine fronts it.

`credentials.json` holds passwords and the Submersion store keys in clear text, and the Garmin token cache under `tokens/` grants API access without the password. Keep `DATA_DIR` private (mode 700) and out of version control. The desktop app keeps credentials in the OS keychain instead and writes them to disk only for the duration of an operation.

---

## 📄 License
MIT, see [LICENSE](LICENSE). The planned Qt desktop build uses PySide6, which is LGPL and links dynamically; that is compatible with the MIT licence of this project.
