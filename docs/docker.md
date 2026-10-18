# Docker

The application is fully containerized. You can run the web dashboard server in background mode, mount a local volume for configurations/session persistence, and select a port:

## Pull the image from Docker Hub
```bash
docker pull mickemannen/dive_sync:latest
```

## Run the container
Map a local host directory (for config, caches, and token persistence) and route the dashboard to your chosen port (e.g. `8080`):
```bash
docker run -d \
  --name dive_sync \
  -p 8080:8000 \
  -v /absolute/path/to/local/dir:/app/data \
  -e TZ=Europe/Stockholm \
  mickemannen/dive_sync:latest
```

The container exposes:
- **Web dashboard**: available at `http://localhost:8080` — sync status, live log, mapping board, accounts and schedule (see [Web dashboard](web-dashboard.md))
- **Volume Mount**: `/app/data/` (contains `settings.json`, `credentials.json`, `sync/`, `backups/` and one folder per service - see [Where your data is kept](configuration.md#where-your-data-is-kept))

### Or with Docker Compose
Save this as `docker-compose.yml`; `./data` next to it becomes the data folder:
```yaml
services:
  dive_sync:
    image: mickemannen/dive_sync:latest
    container_name: dive_sync
    ports:
      - "127.0.0.1:8080:8000"   # localhost only - the dashboard has no login
    volumes:
      - ./data:/app/data
    environment:
      - DIVE_SYNC_PORT=8000
      - DIVE_SYNC_HOST=0.0.0.0
      - TZ=Europe/Stockholm     # your time zone; scheduled jobs run on this clock
    restart: unless-stopped
```
```bash
docker compose up -d          # start (and after editing the file)
docker compose pull && docker compose up -d   # update to the newest image
docker compose logs -f        # follow the log
```

| Variable | Default | Description |
| --- | --- | --- |
| `DIVE_SYNC_PORT` | `8000` | Port of the dashboard inside the container |
| `DIVE_SYNC_HOST` | `0.0.0.0` | Bind address inside the container |
| `DATA_DIR` | `/app/data` | Settings, credentials, tokens and caches |
| `TZ` | UTC | Time zone of the container's clock, e.g. `Europe/Stockholm` |

**Time zone.** Scheduled jobs run on the container's clock, and a container runs on UTC unless `TZ` is set: without it, a job set to 06:00 runs at 06:00 UTC (08:00 in Swedish summer time). Set `TZ` to your [time zone name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) and check it with `docker exec dive_sync date`, which should print your local time.

To reach the dashboard from other machines, drop the `127.0.0.1:` prefix only on a private network or behind a VPN or an authenticating reverse proxy (see [Security](#security)).

The dashboard itself is described in [Web dashboard](web-dashboard.md); building and publishing the image in [Building and releasing](building.md).

## Security

The web dashboard has **no authentication**. Anyone who can reach its port can read the log, change the schedule and save credentials. Run it on a private network only, or behind something that authenticates for it: a reverse proxy with a login (Caddy, nginx, Authelia), a VPN, or Tailscale. Note that the default `DIVE_SYNC_HOST=0.0.0.0` binds every interface of the host; use `127.0.0.1` when a proxy on the same machine fronts it.

`credentials.json` holds passwords in clear text, and the Garmin token cache under `garmin/<account>/tokens/` grants API access without the password. Keep `DATA_DIR` private (mode 700) and out of version control. The desktop app keeps credentials in the OS keychain instead and writes them to disk only for the duration of an operation.
