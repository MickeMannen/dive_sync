# Getting started

## Install

### Packaged app

Installers for macOS, Windows and Linux are attached to the [Releases](https://github.com/mickemannen/dive_sync/releases) page (built by hand from a tag, see [Building and releasing](building.md)). The unattended, scheduled sync runs as a Docker image instead, see [Docker](docker.md).

### From source
Ensure you have **Python 3.10+** installed. Clone the repository and install dependencies:
```bash
pip install -r requirements.txt
pip install -e .
```
To run the test suite or the fixture tools, install the development dependencies as well (`pip install -r requirements-dev.txt`; it pulls in `requirements.txt` too).

The desktop app, from the same checkout:
```bash
python -m desktop          # from a source checkout
briefcase dev              # the same, through Briefcase
briefcase build macOS app && briefcase package macOS app    # a DiveSync.app / installer
```
Pre-built installers are attached to the GitHub releases (built by `.github/workflows/desktop-build.yml`). The app keeps its settings and caches in its own data folder (shown on the **About** page) and credentials in the OS keychain.

## Configure credentials
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
* **Submersion**: disabled for now (see the note in the [README](../README.md)); its settings are kept for when it works.

The desktop app asks for the same credentials on its **Settings** page and keeps them in the OS keychain instead of a file.

The two shapes of `credentials.json` are in [Configuration files](configuration.md#credentialsjson).

## Where your data is kept

See [Configuration files](configuration.md#where-your-data-is-kept) for the data folder on each platform and what sits inside it. The desktop app shows its folder on the **About** page.
