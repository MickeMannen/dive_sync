# Settings and About

**Settings** — any number of accounts for Garmin Connect, Divelogs.org and Subsurface Cloud (with a login test), and export/import of the whole sync configuration as a profile.

**About** — version, update check, license, data folder.

<p align="center"><img src="../media/app_settings.png" alt="Settings page" width="800"></p>

**Units** — a card on the Settings page, after the account cards, with two choices: Metric (m, bar, °C, L, kg) or Imperial (ft, psi, °F, cuft, lb). The switch applies at once, with no Save, and is remembered on this computer (it is not part of the exported sync profile). It only changes what is shown: your dives, the caches, the files the app writes and the services stay metric.

The Shearwater app card (desktop only) is described under [Other services](cli.md#other-services-uddf-files-subsurface-and-the-shearwater-app). Credentials never leave the OS keychain except for the duration of an operation; the CLI and the Docker image use `credentials.json` instead, see [Configuration files](configuration.md).
