# Configuration files

## `credentials.json`

Written by `setup_credentials.py` (see [Getting started](getting-started.md#configure-credentials)); the desktop app keeps the same accounts in the OS keychain instead.

### Single Account Structure (`credentials.json`):
```json
{
  "garmin": {
    "username": "user@domain.com",
    "password": "yourpassword"
  },
  "divelogs": {
    "username": "user_divelogs",
    "password": "yourpassword"
  },
  "subsurface": {
    "email": "you@example.com",
    "password": "yourpassword",
    "base_url": "https://cloud.subsurface-divelog.org/"
  }
}
```
The `subsurface` section is optional; an older file without it still loads. A Garmin account may also set `token_dir`, the folder for its login tokens; left out (the default), it is the account's own `garmin/<account>/tokens` folder.

### Multiple Accounts Structure (`credentials.json`):
You can configure a list of credentials. Each account gets its own folder (e.g. `garmin/user1@domain.com/`, see [Where your data is kept](configuration.md#where-your-data-is-kept)):
```json
{
  "garmin": [
    {
      "username": "user1@domain.com",
      "password": "pass1"
    },
    {
      "username": "user2@domain.com",
      "password": "pass2"
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

## `settings.json`

The sync configuration: pairs (`sync_pairs`, each with its direction, grace window and mapping board), scheduled jobs, the API cooldown and the options of each pair. The board's storage format is on the [Mapping](mapping.md#the-mapping-board-fields-rules-templates-and-conflicts) page (`settings_version` 2). The whole configuration, never credentials, travels between machines as a *sync profile*: export/import on the desktop app's Settings page, the web dashboard's Settings page, or the CLI (`--export-profile`, `--validate-profile`, `--import-profile`, see [Command line](cli.md)).

## Where your data is kept

| Runs as | Data folder |
| --- | --- |
| Desktop app, macOS | `~/Library/Application Support/org.christersson.dive_sync` |
| Desktop app, Windows | `%LOCALAPPDATA%\Christersson\DiveSync` |
| Desktop app, Linux | `~/.local/share/dive-sync` |
| Docker | the volume mounted at `/app/data` (`DATA_DIR`) |
| CLI | `DATA_DIR`; unset, settings in the working directory and caches in `./data` |

Inside it, everything one account owns sits in one folder:

```
settings.json, credentials.json    settings (the desktop app keeps passwords in the OS keychain)
sync/                              remembered dive pairs, conflicts, run history
backups/                           a snapshot of both sides before each sync run
garmin/<account>/data/             cached Garmin dives
garmin/<account>/fit/              downloaded .fit files
garmin/<account>/tokens/           Garmin login tokens
divelogs/<account>/data/           cached Divelogs dives
subsurface/<account>/data/         cached Subsurface dives
subsurface/<account>/cloud/        the Subsurface Cloud checkout
```

A side without accounts (a Submersion store, a local Subsurface folder) uses the account folder `default`. Version 0.3.0 changed this layout and the desktop app's folder: it starts fresh and leaves an earlier version's data where it was (see [`CHANGELOG.md`](../CHANGELOG.md)).
