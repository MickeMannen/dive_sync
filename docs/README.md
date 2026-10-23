# DiveSync documentation

DiveSync keeps dive logs in sync between Garmin Connect, Divelogs.org, Subsurface, the Shearwater app and MySSI (beta), edits dives on each service and converts dive-computer files. It comes as a desktop app, a Docker image with a web dashboard and a command line, all on the same sync engine.

## Start here

- [Getting started](getting-started.md) - install, credentials, where your files live.
- [Docker](docker.md) - the unattended, scheduled sync with its web dashboard.

## The desktop app, page by page

[Desktop app](desktop-app.md) runs it and shows every page; one page per entry in the sidebar:

| Page | What it's for |
|---|---|
| [Sync](sync.md) | Pick a source and a target and sync now; download dives for the Dives pages. |
| [Dives](dives.md) | Garmin, Divelogs, Subsurface and MySSI dives in a table; edit, stage and save (MySSI: browse and delete until the update test). |
| [Mapping](mapping.md) | The mapping board: fields, rules, policies, templates, splits and matching. |
| [Conflicts](conflicts.md) | Queued conflicts with the two values side by side. |
| [Dive edit](convert.md) | Dive-computer files in, every field of a dive editable, UDDF, Subsurface or Garmin `.fit` files out (a dive from a `.fit` patched into a copy, any other written anew); merge a split dive; no account involved. |
| [Settings](settings.md) | Accounts (kept in the OS keychain), metric or imperial units, the sync profile, About. |

## Reference

- [Command line](cli.md) - every option of `sync.py` (including `--units`), other services (UDDF, Subsurface, the Shearwater app, MySSI), mapping and conflicts from the CLI.
- [Configuration files](configuration.md) - `credentials.json`, `settings.json`, the sync profile, where your data is kept.
- [Web dashboard](web-dashboard.md) - the Docker image's pages and its own Units choice.
- [Advanced](advanced.md) - the Garmin dive cache, limitations.
- [Building and releasing](building.md) - the desktop app from source, the release workflow, Docker Hub.
- [How it works](technical.md) - architecture, directory layout, dive normalization and matching, developer guidelines, tests.

## Service notes

- [Garmin Connect diving API](garmin_diving_api.md) - what the API accepts and refuses (why gases never reach Garmin).
