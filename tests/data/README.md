# Test fixtures

Real exports from the owner's **test** accounts, anonymised with
`tests/tools/anonymize_fixtures.py` (see its docstring for exactly what is
replaced). Captured 2026-09-21.

| Directory | Source | Notes |
|---|---|---|
| `submersion/` | Backblaze B2 bucket Submersion syncs through (S3 provider), two devices | `ssv1.<device>.manifest.json` + `ssv1.<device>.base.000000000001.p0000` per device; schema 210, writer 218; 5 dives, two of them with two tanks. `importedFiles.bytes` and `diveDataSources.rawData` are emptied, so the base `checksum` fields do not match the file content. No changeset file yet |
| `subsurface_cloud/` | Subsurface Cloud git repository (working tree only) | Git storage format: `00-Subsurface`, `01-Divesites/Site-*`, `YYYY/MM/DD-...-hh=mm=ss/{Dive-N,Divecomputer}`; 8 dives, the 2026-08-29 ones with two cylinders and two pressure sensors |
| `garmin_fit/` | Two dive `.fit` files from the owner's Garmin **test** account (a Descent watch, product 4518, with Descent T2 transmitters; as cached by the desktop app), anonymised and thinned with `tests/tools/anonymize_fit.py` on 2026-10-02 | `single_gas.fit`: one transmitter, EAN32, 24.1 m, dive 28 of 2026-06-27 (local +8 h); `two_tanks.fit`: two transmitters on one gas (air), 11.9 m, dive 38 of 2026-08-29 (local +7 h; the same dive as the two-cylinder one in `subsurface_cloud/`). Neither has a mid-dive gas switch: no dive in the cache has one, so the only `dive_gas_switched` event is the watch's start-of-dive marker. See below |
| `ssrf/` | `synthetic.ssrf`: invented data (no real dive), written by `src/core/convert/ssrf_writer.py` from `tests/test_convert_ssrf.py`'s `synthetic_dive()` on 2026-10-02 | The byte-for-byte pin of the Subsurface `.ssrf` writer (plans/convert.md I6): a two-tank deco dive touching every field the writer knows. Regenerate deliberately when the writer's output changes, and read the diff |
| `garmin/`, `divelogs/` | not committed | `test_mock_sync.py` skips the tests that need `488.json` / `502.json` |

## Garmin dive FITs (`garmin_fit/`)

Made with

    python tests/tools/anonymize_fit.py <source.fit> tests/data/garmin_fit/<name>.fit
    python tests/tools/anonymize_fit.py --describe tests/data/garmin_fit/<name>.fit   # list what is in it

from a copy of the cached file (the source is only read). The tool's
docstring says exactly what it does; in short:

- **Kept as the watch wrote them:** times (UTC and the local offset), dive
  number, depths, temperatures, tank pressures, gases, deco and CNS data,
  dive settings (GF, model, water type), the watch and transmitter model and
  firmware, the start-of-dive gas switch, dive alerts and tank events.
- **Replaced:** serial numbers (`1000000001`), transmitter ids (`2000000001`,
  `2000000002`, consistently across tank updates, tank summaries, device
  info, the sensor profile and the events), transmitter names (`Tank 1`,
  `Tank 2`). Positions are moved so the entry point is 10.0 S 30.0 W (open
  ocean); the exit and the bounding box keep their distance from it.
- **Dropped:** the user profile, device and training settings, sport,
  battery, alarm and timestamp-correlation messages, unnamed messages and
  events, every field the FIT profile does not name (except the transmitter
  id in `device_info` field 24 and the known fields of the sensor profile,
  message 147), every other text.
- **Thinned, not cut short:** about one record every 6 seconds over the whole
  dive (plus the first, last, deepest, coldest, warmest record and the one at
  each event), tank updates on the same interval per transmitter with each
  transmitter's first and last kept, so the summary messages still agree with
  the records. About 26 KB per file.

Guarded by `tests/test_fixtures.py` (decode cleanly with garmin-fit-sdk, a
dev-only dependency from `requirements-dev.txt`, only
placeholder serials and ids, positions only around the made-up point, bounded
record count); the tool itself by `tests/test_anonymize_fit.py`.
