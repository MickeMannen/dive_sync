# Dive Sync 🤿✨

Dive Sync is a robust dive log synchronization engine and web dashboard that seamlessly matches and syncs your dive history between **Garmin Connect** and **Divelogs.org**. 

I started this project when i got back to diving and had to import all dives from logbooks to Garmin and to Divelogs.
I managed to make the import but the stability of the code wasn't good enough for release. Because of lack of time I didnt continue but with Gemini I saw the opportunity to finalize the project.

There are most likely a lot of bugs so please use it carefully, I will use the docker container myself and fix issues as I see them.

It features bidirectional syncing, detailed telemetry parsing (depth/temperature graphs and gas mixture sensors), an interactive web-based spreadsheet editor, and multi-account support.

<p align="left">
  <a href="https://skillicons.dev">
    <img src="https://skillicons.dev/icons?i=python,fastapi,js,html,css,docker,bash,git,gemini,claude" alt="Tech Stack" />
  </a>
</p>

---

## 🚀 Key Features

* **Bidirectional Syncing**: Synchronizes dive logs in both directions, or unidirectionally (to Garmin or to Divelogs).
* **Telemetry & Gas Mapping**: Maps complex dive metrics, temperature profiles, start/end tank pressures, gas mixtures (Nitrox/Trimix), and telemetry graph coordinates.
* **Interactive Spreadsheet Editor**: Easily view, sort, and edit dive metadata (buddy, weights, visibility, location, notes, dates, and Garmin dive numbers) directly from the Web UI.
* **Non-Blocking Upstream Sync**: Edits made in the spreadsheet editor are instantly saved to the local cache and pushed asynchronously to remote Garmin/Divelogs servers in the background.
* **Multi-Account Support**: Configure multiple Garmin and Divelogs credentials. Run the sync globally or target specific accounts using selection arguments.
* **Docker Ready**: Package and run the dashboard with custom port routing and unified volume mapping to persist settings, credentials, session tokens, and data caches.

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
python setup.py
```
This utility authenticates with Garmin and Divelogs, validates your password, and saves them to `credentials.json`.

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
  }
}
```

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
- **Dashboard UI**: available at `http://localhost:8080`
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

## 🖥️ Web Dashboard

Start the web dashboard locally without Docker:
```bash
python docker_run.py
```
Open `http://localhost:8000` in your web browser. 

The dashboard provides:
1. **Dives Explorer Tab**: View and click on individual dives. Displays telemetry details, depth/temp profiles, and raw JSON metrics. Add new dives or trigger a sync.
2. **Spreadsheet Editor Tab**: Edit locations, notes, buddies, weights, visibility, and Garmin dive numbers in an Excel-like grid. Upstream syncing is done asynchronously.
3. **Download Raw Data**: Sync and download raw JSON payloads locally from both Garmin and Divelogs APIs.

---

## 🧪 Testing

Run all unit, mock, and API tests to verify execution logic:
```bash
./run_tests.sh
```

---

## 📄 License
This project is open-source. See license files for details.
