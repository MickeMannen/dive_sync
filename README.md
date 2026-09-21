# Dive Sync 🤿✨ 

# Solution is still in Beta!!!!!
## I have mainly tested Garmin sync to Divelogs and not the other direction - be careful and keep a backup!


Dive Sync is a dive log synchronization engine that seamlessly matches and syncs your dive history between **Garmin Connect** and **Divelogs.org**. 

I started this project when i got back to diving and had to import all dives from logbooks to Garmin and to Divelogs.
I managed to make the import but the stability of the code wasn't good enough for release. Because of lack of time I didnt continue but with Gemini I saw the opportunity to finalize the project.

There are most likely a lot of bugs so please use it carefully, I will use the docker container myself and fix issues as I see them.

It features bidirectional syncing, detailed telemetry parsing (depth/temperature graphs and gas mixture sensors), and multi-account support.

**Project direction**: the Docker deployment is a scheduled-sync engine with a status page (schedule, credentials, mapping board — no dive editing there). Interactive dive editing lives in the native desktop app (`python -m desktop`; Briefcase + PySide6/Qt Quick, macOS first, Windows and Linux planned) — see [rework.md](rework.md) for the plan and status.

<p align="left">
  <a href="https://skillicons.dev">
    <img src="https://skillicons.dev/icons?i=python,fastapi,js,html,css,docker,bash,git,gemini,claude" alt="Tech Stack" />
  </a>
</p>

---

## 🚀 Key Features

* **Bidirectional Syncing**: Synchronizes dive logs in both directions, or unidirectionally (to Garmin or to Divelogs).
* **Telemetry & Gas Mapping**: Maps complex dive metrics, temperature profiles, start/end tank pressures, gas mixtures (Nitrox/Trimix), and telemetry graph coordinates.
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
* **Submersion**: the S3-compatible bucket Submersion syncs through. Backblaze B2 is the recommended hosted option: create a bucket, then an application key restricted to it; the endpoint is `https://s3.<region>.backblazeb2.com`. Point Submersion's sync provider at the same bucket. Google Drive cannot be used (see [docs/submersion_sync_format.md](docs/submersion_sync_format.md)). Verified with one read-only listing.

Subsurface Cloud is fully supported (see "Other services" below). The Submersion adapter is still in progress (rework.md Track F); its store credentials are stored and checked now so the setup is ready when it lands.

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
    "endpoint_url": "https://s3.eu-central-003.backblazeb2.com",
    "region": "eu-central-003",
    "bucket": "my-submersion-sync",
    "prefix": "submersion-sync/",
    "access_key_id": "…",
    "secret_access_key": "…"
  }
}
```
The `subsurface` and `submersion` sections are optional; an older file without them still loads.

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

# Run standard bidirectional sync
python sync.py

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
* `--direction`: Choose sync flow direction (`bidirectional`, `to_garmin`, `to_divelogs`).
* `--overwrite`: Clear local mock caches before running a raw data download.

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

Each pair has its own direction, grace window and mapping board (defaults are generated per pair).

**Subsurface Cloud** needs no checkout of your own: with the account configured by `setup_credentials.py --services subsurface`, the spec `subsurface-cloud` clones the repository under `DATA_DIR/subsurface_cloud/`, refreshes it from the cloud at the start of every run, and commits and pushes once at the end. It never force-pushes; if Subsurface pushed in between, the run's changes are replayed on top of the new state.

```bash
python sync.py --source garmin --target subsurface-cloud --full-sync
```

### Mapping, conflicts and profiles

Which field feeds which, in what direction and who wins a conflict is defined by the *mapping board*: the `field_links` list in `settings.json`. The shipped defaults fill blanks on either side and never overwrite a real value with another (differences are logged); tanks and profiles go to Divelogs only, Garmin's location name and Divelogs' dive site are the same field, and new Garmin dives are titled "location, dive site". A graphical editor is coming; until then these commands work with the board:

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
3. **Mapping board**: one board per sync pair. The two columns list what each service can read and write (locked fields cannot be written). Drag a field onto a field on the other side to link them; click a line or a row to set the direction, the conflict policy (fill blanks, source wins, target wins, ask me), the match-key order and, for text targets, a `{field}` template with a live preview. Dropping a further field onto a linked target builds a composite. Save asks whether the changed mapping should be applied to all matched dives on the next run; Cancel and Reset to defaults are there too; **Test mapping** fetches the newest 10 dives from both services and shows what every link would do, without writing anything.
4. **Conflicts**: what links with the "ask me" policy have queued, with a button per side to resolve.
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
- **Submersion** is not synced yet; Google Drive as its store is not reachable by third parties at all (see `docs/submersion_sync_format.md`).

---

## 🔒 Security

The status page has **no authentication**. Anyone who can reach its port can read the log, change the schedule and save credentials. Run it on a private network only, or behind something that authenticates for it: a reverse proxy with a login (Caddy, nginx, Authelia), a VPN, or Tailscale. Note that the default `DIVE_SYNC_HOST=0.0.0.0` binds every interface of the host; use `127.0.0.1` when a proxy on the same machine fronts it.

`credentials.json` holds passwords and the Submersion store keys in clear text, and the Garmin token cache under `tokens/` grants API access without the password. Keep `DATA_DIR` private (mode 700) and out of version control. The desktop app keeps credentials in the OS keychain instead and writes them to disk only for the duration of an operation.

---

## 📄 License
MIT, see [LICENSE](LICENSE). The planned Qt desktop build uses PySide6, which is LGPL and links dynamically; that is compatible with the MIT licence of this project.
