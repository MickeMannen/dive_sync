# Dive Sync 🤿✨

**Dive Sync** keeps dive logs in sync between **Garmin Connect**, **Divelogs.org** and **Subsurface Cloud** (UDDF files too). This image runs the sync on a schedule, with a web dashboard: Sync now, scheduled jobs, mapping board, conflicts, settings and a live log. Dive editing and Mirror (making a target an exact copy) live in the separate desktop app, not here; a sync from this image never deletes dives.

# THIS IS STILL IN BETA - KEEP BACKUPS

GitHub Repository: [github.com/MickeMannen/dive_sync](https://github.com/MickeMannen/dive_sync)

---

## 🚀 Quick Start

```bash
docker run -d \
  --name dive_sync \
  -p 127.0.0.1:8080:8000 \
  -v /path/to/local/data:/app/data \
  -e TZ=Europe/Stockholm \
  mickemannen/dive_sync:latest
```

Then open `http://localhost:8080`, enter your credentials under **Settings** (or run `setup_credentials.py` with `DATA_DIR` pointing at the mounted folder) and add a job under **Sync → Scheduled jobs** with **+ Add job**, then **Save schedule**.

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
      - TZ=Europe/Stockholm     # your time zone; scheduled jobs run on this clock
    restart: unless-stopped
```

```bash
docker compose up -d                           # start (and after editing the file)
docker compose pull && docker compose up -d    # update to the newest image
docker compose logs -f                         # follow the log
```

---

## ⚡ What it does

- **Matches** dives across services by remembered pairs, then by start time (time-zone aware where the service provides it).
- **Syncs fields** according to a mapping board: which field feeds which, in which direction, and what happens on a conflict (fill blanks, one side wins, or ask). Defaults never overwrite a real value with another one.
- **Uploads** new dives in either direction, including profiles and tanks where the target can take them (Garmin Connect accepts no gas data from outside).
- **Services**: Garmin Connect, Divelogs.org, Subsurface Cloud (clone, commit, push), UDDF files. Submersion sync doesn't work yet and is disabled.
- **Scheduling**: jobs from any source to any target, hourly, daily, weekly or every N minutes; a job writes its target only, so add one each way for both directions. Dry-run mode and a manual "Sync now" from the page.
- **Garmin cache**: *Use cached Garmin dives* re-reads only the dives Garmin's activity list shows as new or changed, saving three slow API calls per dive. That list doesn't include notes, buddies, weight or visibility, so an edit to only those is picked up by a run with the option off - e.g. a weekly job next to a frequent cached one.
- **Portable profile**: export/import the whole sync configuration (never credentials) to move it between the desktop app and Docker.

---

## 📁 Volume Persistence (`/app/data`)

Map a host folder to `/app/data`. It holds:
- `credentials.json` (plain text: passwords and store keys, keep it private)
- `settings.json` (direction, filters, schedule, mapping board, sync pairs)
- `sync/` (remembered pairs, last run, conflicts waiting for you, run history)
- `backups/` (a snapshot of both sides before each sync run)
- `garmin/<account>/` with `data/` (dive cache), `fit/` (downloaded `.fit` files) and `tokens/` (Garmin session tokens; grant API access without the password)
- `divelogs/<account>/data/` and `subsurface/<account>/data/` (dive caches); `subsurface/<account>/cloud/` (the clone of your Subsurface Cloud repository)

**Upgrading from before 0.3.0:** the layout changed. The container starts fresh in the same volume: the dive caches are downloaded again, and the remembered dive pairs start empty, so the first sync re-matches dives by time. The old `garmin/<account>/*.json`, `garmin_fit/`, `tokens/`, `subsurface_cloud/` and root-level `sync_state*.json` / `conflicts*.json` can be deleted once the new version runs.

---

## ⚙️ Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `DIVE_SYNC_PORT` | `8000` | Port of the dashboard inside the container |
| `DIVE_SYNC_HOST` | `0.0.0.0` | Bind address inside the container |
| `DATA_DIR` | `/app/data` | Persistent configuration and caches |
| `TZ` | UTC | Time zone of the container's clock, e.g. `Europe/Stockholm` |

**Time zone.** Scheduled jobs run on the container's clock, which is UTC unless `TZ` is set: without it, a job set to 06:00 runs at 06:00 UTC (08:00 in Swedish summer time). Set `TZ` to your [time zone name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) and check it with `docker exec dive_sync date`.

---

## 🔒 Security Notice

The dashboard has **no login**. Publish the port only on a private network or behind an authenticating reverse proxy, VPN or Tailscale (the examples above bind it to localhost on the host). Treat `/app/data` as secret: it contains your passwords and Garmin tokens.
