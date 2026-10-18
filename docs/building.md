# Building and releasing

## Desktop app

```bash
python -m desktop          # from a source checkout
briefcase dev              # the same, through Briefcase
briefcase build macOS app && briefcase package macOS app    # a DiveSync.app / installer
```
Pre-built installers are attached to the GitHub releases (built by `.github/workflows/desktop-build.yml`). The app keeps its settings and caches in its own data folder (shown on the **About** page) and credentials in the OS keychain.

## Releases (GitHub Actions)
Nothing is built or published automatically. To release:
1. Create a GitHub Release with a `vX.Y.Z` tag, e.g. `v0.1.0` (draft or published). The tag is the version: the build writes it into `pyproject.toml` on the runner (the desktop app shows it on its About page), so there is nothing to bump by hand. Plain numbers only — Windows installers reject tags like `v0.1.0-beta`.
2. In the **Actions** tab, run **Build and attach release artifacts** (`.github/workflows/release-build.yml`): pick the release's tag in the **Use workflow from** dropdown (a branch is refused) and tick the platforms to build: macOS (signed and notarized), Windows and Linux installers are attached to the release.
3. Tick **push_docker** there (or run `.github/workflows/docker-release.yml` on its own) to build the multi-architecture image (`linux/amd64`, `linux/arm64`) and push it to Docker Hub; the tag is baked in as `APP_VERSION`, which the web dashboard's About page shows.

`.github/workflows/desktop-build.yml` builds the desktop app for a quick check (ad-hoc signed) without a release.

### Required GitHub secrets
Set the following secrets in your GitHub repository (**Settings ➔ Secrets and variables ➔ Actions**):
- `DOCKERHUB_USERNAME`: Your Docker Hub username (`mickemannen`)
- `DOCKERHUB_TOKEN`: A Personal Access Token (PAT) generated in Docker Hub (**Account Settings ➔ Security ➔ Personal access tokens**)

### Updating the Docker Hub overview page
The GitHub workflow automatically syncs [`DOCKERHUB.md`](../DOCKERHUB.md) to your Docker Hub repository description overview page (`hub.docker.com/r/mickemannen/dive_sync`) every time it pushes an image.
