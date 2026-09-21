# Test fixtures

Real exports from the owner's **test** accounts, anonymised with
`tests/tools/anonymize_fixtures.py` (see its docstring for exactly what is
replaced). Captured 2026-09-21.

| Directory | Source | Notes |
|---|---|---|
| `submersion/` | Backblaze B2 bucket Submersion syncs through (S3 provider), two devices | `ssv1.<device>.manifest.json` + `ssv1.<device>.base.000000000001.p0000` per device; schema 210, writer 218; 5 dives, two of them with two tanks. `importedFiles.bytes` and `diveDataSources.rawData` are emptied, so the base `checksum` fields do not match the file content. No changeset file yet |
| `subsurface_cloud/` | Subsurface Cloud git repository (working tree only) | Git storage format: `00-Subsurface`, `01-Divesites/Site-*`, `YYYY/MM/DD-...-hh=mm=ss/{Dive-N,Divecomputer}`; 8 dives, the 2026-08-29 ones with two cylinders and two pressure sensors |
| `garmin/`, `divelogs/` | not committed | `test_mock_sync.py` skips the tests that need `488.json` / `502.json` |
