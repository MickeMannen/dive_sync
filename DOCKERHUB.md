# Dive Sync 🤿✨

**Dive Sync** is an automated dive log synchronization engine and web dashboard that seamlessly matches and syncs dive logs between **Garmin Connect** and **Divelogs.org**.

GitHub Repository: [github.com/MickeMannen/dive_sync](https://github.com/MickeMannen/dive_sync)

---

## 🚀 Quick Start

Run the container using `docker run`:

```bash
docker run -d \
  --name dive_sync \
  -p 8080:8000 \
  -v /path/to/local/data:/app/data \
  mickemannen/dive_sync:latest
```

Once running, open your browser and navigate to:
👉 `http://localhost:8080`

---

## 🐳 Docker Compose

You can also run Dive Sync using Docker Compose (`docker-compose.yml`):

```yaml
version: '3.8'

services:
  dive_sync:
    image: mickemannen/dive_sync:latest
    container_name: dive_sync
    ports:
      - "8080:8000"
    volumes:
      - ./data:/app/data
    environment:
      - DIVE_SYNC_PORT=8000
      - DIVE_SYNC_HOST=0.0.0.0
    restart: unless-stopped
```

Start the service with:
```bash
docker compose up -d
```

---

## ⚡ Features

- **Bidirectional Synchronization**: Sync dive logs between Garmin Connect and Divelogs.org (or unidirectionally in either direction).
- **Rich Telemetry Parsing**: Parses depth/temperature graphs, tank pressures, gas mixtures (Nitrox/Trimix), and native FIT files.
- **Web Dashboard & Spreadsheet Editor**: View telemetry graphs, edit metadata (buddy, visibility, weight, notes, locations) in a grid, and save changes upstream asynchronously.
- **Multi-Account Support**: Manage multiple Garmin and Divelogs user profiles simultaneously.
- **Automated Scheduling & Dry-Run Mode**: Schedule background syncs at specific daily times or simulate sync runs safely.

---

## 📁 Volume Persistence (`/app/data`)

Map a host folder to `/app/data` inside the container. This preserves:
- `credentials.json` (encrypted/configured API credentials)
- `settings.json` (sync preferences, schedules, and filters)
- `tokens/` (Garmin OAuth authentication tokens)
- `garmin/` & `divelogs/` (local dive log caches & backups)

---

## ⚙️ Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `DIVE_SYNC_PORT` | `8000` | Port for the FastAPI web server |
| `DIVE_SYNC_HOST` | `0.0.0.0` | Binding host address |
| `DATA_DIR` | `/app/data` | Directory path for persistent configuration & caches |

---

## 🔒 Security Notice

Please treat your `credentials.json` and session token files securely. Do not commit `/app/data` contents to public repositories.
