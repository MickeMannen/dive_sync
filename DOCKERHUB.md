# Dive Sync 🤿✨

**Dive Sync** keeps dive logs in sync between **Garmin Connect**, **Divelogs.org** and **Subsurface Cloud** (UDDF files too). This image runs the scheduled-sync engine with a small status page: schedule, credentials, live log. Dive editing lives in the separate desktop app, not here.

# THIS IS STILL IN BETA - KEEP BACKUPS

GitHub Repository: [github.com/MickeMannen/dive_sync](https://github.com/MickeMannen/dive_sync)

---

## 🚀 Quick Start

```bash
docker run -d \
  --name dive_sync \
  -p 127.0.0.1:8080:8000 \
  -v /path/to/local/data:/app/data \
  mickemannen/dive_sync:latest
```

Then open `http://localhost:8080`, enter your credentials on the status page (or run `setup_credentials.py` with `DATA_DIR` pointing at the mounted folder) and add a schedule.

---

## 🐳 Docker Compose

```yaml
services:
  dive_sync:
    image: mickemannen/dive_sync:latest
    container_name: dive_sync
    ports:
      - "127.0.0.1:8080:8000"
    volumes:
      - ./data:/app/data
    environment:
      - DIVE_SYNC_PORT=8000
      - DIVE_SYNC_HOST=0.0.0.0
    restart: unless-stopped
```

```bash
docker compose up -d
```

---

## ⚡ What it does

- **Matches** dives across services by remembered pairs, then by start time (time-zone aware where the service provides it).
- **Syncs fields** according to a mapping board: which field feeds which, in which direction, and what happens on a conflict (fill blanks, one side wins, or ask). Defaults never overwrite a real value with another one.
- **Uploads** new dives in either direction, including profiles and tanks where the target can take them (Garmin Connect accepts no gas data from outside).
- **Services**: Garmin Connect, Divelogs.org, Subsurface Cloud (clone, commit, push), UDDF files; Submersion is in progress.
- **Scheduling**: hourly, daily, weekly or every N minutes; dry-run mode; manual "Sync now" from the page; optional per-job sync pair and board.
- **Portable profile**: export/import the whole sync configuration (never credentials) to move it between the desktop app and Docker.

---

## 📁 Volume Persistence (`/app/data`)

Map a host folder to `/app/data`. It holds:
- `credentials.json` (plain text: passwords and store keys, keep it private)
- `settings.json` (direction, filters, schedule, mapping board, sync pairs)
- `sync_state.json` / `conflicts.json` (remembered pairs, last run, conflicts waiting for you)
- `tokens/` (Garmin session tokens; grant API access without the password)
- `subsurface_cloud/` (the clone of your Subsurface Cloud repository)
- `garmin/` & `divelogs/` (raw dive caches when you download them)

---

## ⚙️ Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `DIVE_SYNC_PORT` | `8000` | Port of the status page inside the container |
| `DIVE_SYNC_HOST` | `0.0.0.0` | Bind address inside the container |
| `DATA_DIR` | `/app/data` | Persistent configuration and caches |

---

## 🔒 Security Notice

The status page has **no login**. Publish the port only on a private network or behind an authenticating reverse proxy, VPN or Tailscale (the examples above bind it to localhost on the host). Treat `/app/data` as secret: it contains your passwords and Garmin tokens.
