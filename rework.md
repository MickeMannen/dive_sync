# Dive Sync — Implementation Plan

This is the working plan for every change to the project. `features.md` is the inventory of what exists and the backlog of requests (B-numbers); this file says **how and in what order** those requests get built. Keep the two in sync: when a track here ships, flip the row in `features.md`.

Last verified against the code: 2026-09-21 (branch `feature_branch`, after phase 1 of the suggested order). Test baseline: `./run_tests.sh` → 91 passed, 4 skipped (the skips in `test_mock_sync.py` need real downloaded fixtures `488.json`/`502.json` under `data/`, which are not committed).

**Status legend**
- ✅ Done and verified
- 🚧 Partial / in progress
- 📝 Planned, not started
- ❌ Dropped or blocked (reason in notes)

---

## Architecture principle (unchanged)

Two deployables, one shared engine:

- **Docker** — unattended scheduled sync plus a read-only status page with schedule and credential forms. No dive editing.
- **Desktop app** — manual sync plus all dive editing. No built-in scheduler.

Both import `src/core` (`adapter.py`, `sync_engine.py`, `models.py`, `scheduler.py`, `dive_cache.py`, `mapping/`, `services/`) unchanged. That layer knows nothing about FastAPI or any GUI toolkit, and every new capability that affects sync behaviour goes there first so both deployables get it from one implementation. `src/core/scheduler.py` must stay free of FastAPI imports.

Docker (server/NAS) and the desktop app (laptop) may sync the same account independently with no shared filesystem. No cross-process lock exists or is attempted; each keeps its own cache and `sync_state.json`, and both honour `api_cooldown_seconds`. The user manages timing.

---

## Track A — Docker: scheduled-sync engine + status page

| # | Step | Status | Notes |
|---|------|--------|-------|
| A1 | Extract `scheduler_loop()` / `run_sync_thread()` / `get_next_scheduled_run()` into `src/core/scheduler.py` | ✅ | No FastAPI dependency; `web/app.py` only wires `lifespan` |
| A2 | Trim `src/web/app.py` to status, log stream, credentials, settings and cron-job endpoints | ✅ | Dive/cache/delete routes removed |
| A3 | Remove dive-editing UI and cache explorer from `src/web/static/` | ✅ | Logic ported to `src/core/dive_cache.py` (B4 in Track B) |
| A4 | `docker_run.py` / `Dockerfile` boot one process: scheduler + status server | ✅ | Unchanged from before the rework |
| A5 | Update deployment docs for the new scope | 🚧 | `README.md` done. `DOCKERHUB.md` still advertises the removed spreadsheet editor and telemetry graphs (features.md B10) |
| A6 | Document the concurrent Docker/desktop runner limitation in README | 📝 | Called for in the original plan, never written. Add under README "Limitations" |
| A7 | Per-account last-sync status on the status page | 📝 | `scheduler.last_sync_results` is one global dict (features.md B9). Depends on A8 |
| A8 | Account selection for scheduled jobs and the credentials form | 📝 | `run_sync_thread` calls `SyncEngine()` with no account override, so a multi-account `credentials.json` makes every scheduled run fail; `POST /api/credentials` overwrites an account list with a single account (features.md B8). Add optional `garmin_username` / `divelogs_username` to `CronJobModel` and pass them through |
| A9 | Mapping board and conflict list on the status page | 📝 | Track C, step C6 |
| A11 | Failure alerts: optional `notify_url` (ntfy / Gotify / generic webhook) in settings and profile; POST a short message when a scheduled or manual run fails; status page field with a Test button | 📝 | Decided 2026-09-20 |
| A10 | README security section: no built-in auth, run behind a reverse proxy / VPN / Tailscale; note that `DIVE_SYNC_HOST=0.0.0.0` binds all interfaces | 📝 | Decided 2026-09-20 |

## Track B — Desktop app v1 (Toga, macOS)

All shipped. Condensed history; the hard-won details are in "Lessons and constraints" below.

| # | Step | Status | Notes |
|---|------|--------|-------|
| B1 | Briefcase/Toga skeleton: sidebar + section swap, `pyproject.toml` `[tool.briefcase.app.desktop]` | ✅ | Validated `briefcase dev` and a packaged `DiveSync.app` |
| B2 | Sync section: direction, dry-run / only-new / gases / FIT switches, "Sync now", progress bar, streamed log | ✅ | Reuses `scheduler.run_sync_thread`; `desktop/logging_bridge.py` feeds a queue from the `dive_sync` logger |
| B3 | "Download dives" (raw cache) with overwrite switch; per-tab Refresh scoped to one service | ✅ | `scheduler.run_download_thread`, `download_and_save_raw_data(include_garmin=, include_divelogs=)` |
| B4 | `src/core/dive_cache.py`: list / read / update / delete cache files + remote push | ✅ | Shared by both services; 19 unit tests |
| B5 | Dive editor: table + detail form (dive #, date, time, duration, max depth, location, notes, weight, visibility, buddy), Save and Delete with confirm | ✅ | One `DiveEditorSection` class, `sections/garmin.py` and `sections/divelogs.py` are thin wrappers. Edits blocked while a sync or download runs |
| B6 | Column chooser and sort column/direction persisted per service | ✅ | `desktop/preferences.py` → `desktop_prefs.json`; not in the original plan, added on request |
| B7 | Keychain credentials (`keyring`), Settings section with Test buttons, first-run lands on Settings | ✅ | `desktop/credentials.py`, `desktop/paths.py` (platformdirs data dir) |
| B8 | Credentials and Garmin token file exist on disk only during an operation | ✅ | `begin_operation()` / `end_operation()` reference count; see lessons |
| B9 | Date/time split, Garmin float formatting, ISO `T` normalisation | ✅ | `dive_cache._normalize_date_time` |

Drift from the original plan, for the record: section files are `sync.py`, `garmin.py`, `divelogs.py`, `settings.py`, `dive_editor.py` (not `sync_section.py` / `garmin_editor.py` / `divelogs_editor.py`), and editing is a detail form under the table rather than inline cell editing.

---

## Track C — Per-field sync rules and conflict handling (features.md B11)

### Problem

Sync direction is a single global switch and the per-field behaviour is hardcoded in the matched-pair loop of `SyncEngine.run_sync`. The resulting policy is not visible to the user and not what most would expect:

- In bidirectional mode Garmin silently wins every field conflict for buddy, notes, weight and visibility. The Garmin→Divelogs pass runs first and overwrites the Divelogs values, so the reverse pass sees no difference.
- GPS goes Divelogs→Garmin only when Garmin has no coordinates.
- Profile samples and gas mixtures go Garmin→Divelogs only. Divelogs→Garmin gas/tank updates are not supported.
- Location is never compared on matched dives.
- The field mapping itself is fixed in code. Garmin `activityName` and `locationName` are collapsed into one `location` string, and Divelogs `location` + `divesite` are joined with a comma on read and split on the first comma on write (`services/divelogs.py`, `services/garmin.py`). Fields that exist on one side only (Garmin's activity name) cannot be built from anything else, and the user cannot see or change which field feeds which.

### Design

One implementation in the engine, two thin editors.

**Field catalogue** (`src/core/fields.py`). Every adapter declares the fields it can read and write, so the UI never hardcodes a field list and a new adapter (Track F) gets a mapping editor for free:

```python
class FieldSpec(BaseModel):
    key: str                      # "garmin.activityName", "divelogs.divesite", "gps"
    label: str                    # shown in the UI
    type: Literal["text", "number", "datetime", "gps", "list", "tanks", "samples"]
    readable: bool = True
    writable: bool = True
    unified: Optional[str] = None # UnifiedDive attribute this is, if any
    max_length: Optional[int] = None  # text fields with a service-side limit
```

`BaseDiveAdapter.field_catalog() -> List[FieldSpec]`. Common fields keep their `UnifiedDive` attribute (`buddy`, `notes`, `weight`, `gps`, `tanks`, `samples`, ...). Service-specific scalars that have no unified attribute (Garmin `activityName`, `locationName`; Divelogs `location`, `divesite`) go into a new `UnifiedDive.service_fields: Dict[str, Any]`, filled by `to_unified` and read by `update_dive`. The JSONPath maps in `src/core/mapping/` keep doing raw-payload extraction; the catalogue is the declared list of what they produce and what `update_dive` can push. v1 catalogue = every field synced today plus `rating`, water type / dive type (Divelogs fields, Garmin `waterType`/`diveType` enums) and tags (Divelogs free text; Garmin has none, so a text-only source or composite target).

**Field links** (`src/core/config.py`). The user's mapping is a list of links, each joining one target field to one or more source fields:

```python
class FieldLink(BaseModel):
    id: str
    source: List[str]             # one catalogue key, or several for a composite
    target: str
    direction: Literal["bidirectional", "to_target", "to_source", "off"] = "bidirectional"
    conflict: Literal["source_wins", "target_wins", "prefer_non_empty", "manual"] = "source_wins"
    template: Optional[str] = None   # required when len(source) > 1
    reverse: Optional[str] = None    # optional parse pattern for a composite (C21, later)
    match_order: Optional[int] = None  # set: this link is also a match key, run in this order
    separator: str = ", "            # list <-> text links: join / split token
    when: Optional[str] = None       # reserved for conditional links (later, no migration needed)

class SettingsModel(BaseModel):
    ...
    field_links: List[FieldLink] = Field(default_factory=default_field_links)
```

Policies are named `source_wins` / `target_wins` rather than `garmin_wins` / `divelogs_wins` so the same model works for every pair after F1. The default list reproduces today's mapping and today's behaviour exactly (Garmin side wins; `tanks` and `samples` `to_target` towards Divelogs; the location links `off` on matched dives), so upgrading changes nothing until the user edits the board. The comma join/split of Divelogs `location`/`divesite` becomes an explicit composite link the user can see, change or delete. `CronJobModel` gets an optional `field_links` override, same pattern as its existing `directionality` override; with F1 the list lives on each `sync_pair`. A link between incompatible types is refused at save time, except `list` ↔ `text`, which joins with the link's `separator` one way and splits on it the other way (Garmin's single buddy text ↔ Submersion's buddy list). `tanks` and `samples` links are structural: one-way only, no template. Text links always replace the target; there is no append mode, a user who wants a footer includes `{notes}` in the template. Every adapter ships a full default link set for its pairs (depth, time, site name, notes, buddy, tanks) so a new pair syncs out of the box and the defaults double as the Track F adapter tests.

**Composite fields and templates** (`src/core/templates.py`). A link with several sources renders its target from a template using `{key}` placeholders with the catalogue key and standard Python format specs: `{divelogs.divenumber:03d}`, `{date_time:%Y-%m-%d}`. Example: Garmin `activityName` ⇐ `"{divelogs.divesite} ({divelogs.location}) #{divelogs.divenumber}"`. An empty source renders as an empty string (decided 2026-09-21; no optional-segment syntax). Datetime placeholders render in dive local time; a `{date_time_utc}` key is added with C15. A rendered text longer than the target's `max_length` is truncated, the preview shows a warning and the sync log notes it once per dive. Templates are validated against the catalogue when saved (unknown key, wrong type for the format spec, non-text target) and the UI shows a live preview rendered from the newest dive in the latest backup snapshot (C14), or from a built-in example dive when there is none. There is no filter language and no per-link transform list: a single-source link is a plain copy, and anything else (`Dive: {notes}`) is a one-source template. Validation also refuses a board where a composite's target feeds, through another link, back into one of its own sources; the error names both links. A composite target that already holds a different value follows the link's normal conflict policy, no special case. A composite link is one-way towards its target; the reverse direction needs a parse pattern (`reverse`, a regex with named groups matching catalogue keys) and is a later item (C21).

**Mapping board (both UIs).** One board per sync pair, chosen from a pair picker at the top; under it a header strip with the pair's options (grace window, `create_on_garmin`, `propagate_deletes`, the ordered match-key list) so everything about one pair is in one place, while credentials, global cooldown, backups and alerts stay in Settings. Below that, two columns, left service and right service, each listing its catalogue with a friendly label (native key such as `garmin.activityName` on hover and in templates), a type icon, the unit for numbers, and a lock badge on fields the service cannot write. Drag a field from one column and drop it on a field in the other to create a link. Drop a further field onto an existing link's target to turn it into a composite, which opens the template editor pre-filled with the fields separated by a space. Links draw as lines with arrowheads for direction; click a link to edit direction, conflict policy and template, or delete it. Dropping onto a locked field is refused with a short message and the direction menu hides impossible options. Fields with no link are not synced, which is the visible replacement for today's implicit "off". The template editor is a text box with a field picker that inserts `{key}` at the cursor, plus the live preview. Edits stay local until Save; Cancel reloads the saved board, a dot on Save marks unsaved edits, and leaving the page or section warns. A "Reset to defaults" button restores the shipped links after a confirm; named layouts are not stored, that is what profile export/import is for. Saving a board that changes existing links asks "Apply to all matched dives on the next run?" and, on yes, sets a one-shot full-compare flag in sync state; default is no. A "Test mapping" button fetches the newest 10 dives per side live from the services (normal cooldown, progress shown), renders every link with the unsaved board and shows a per-dive table of source, target, result and conflicts; it never writes. Unit conversion is not a link option: numbers keep following the Divelogs profile's metric/imperial flag. Status page: HTML5 drag-and-drop plus one SVG overlay for the lines, plain JavaScript in `static/script.js` to match the rest of the page, no framework. Desktop: QML `Drag`/`DropArea` with `Shape` paths, built after Track D. Below about 700 px the columns stack, links are listed as rows with a delete button, drag-and-drop still works between the stacked lists and the SVG lines are hidden. The status page uses the backup snapshot and Test mapping results only behind the scenes; it does not grow a dive list. Both edit the same `field_links` list (status page through `GET/POST /api/settings`, desktop through `settings.json`) and the list travels in the sync profile, so a board laid out in the app can be imported on the Docker page and back.

**Matching from the board.** External IDs stay tier 1 and the timestamp window stays the last tier. Between them, instead of the hardcoded dive-number tier, the engine tries the links flagged as match keys in `match_order` (number or datetime types only; the default board flags the dive-number link). Removing every match flag leaves external IDs and timestamp, which is today's behaviour minus dive number.

**Conflict semantics.** A conflict is a matched pair where both sides have a non-empty value for the field and they differ. `prefer_non_empty` only ever fills a blank side. `manual` skips the field and records `{pair ids, field, garmin_value, divelogs_value, seen_at}` to `conflicts.json` next to `sync_state.json`; a later run re-checks and drops entries that no longer conflict. Resolving a conflict from a UI writes the chosen value to the losing side through the normal adapter `update_dive` and removes the entry.

**Format options.** Do not invent new transforms beyond templates. The Divelogs "location, divesite" comma join/split is expressed as a default composite link instead of a switch; unit handling stays as it is and follows the Divelogs profile's metric/imperial flag. A "newest wins" policy is deliberately excluded until it is confirmed that the Divelogs API exposes a modification timestamp.

**Existing gates stay.** The global `directionality` still gates which passes run at all, and `sync_gases` remains as a convenience alias for "the `tanks` link is not `off`" so old settings files and cron jobs keep working.

**Portable sync profile.** Docker and the desktop app run on different machines with separate `settings.json` files, so the rules must travel as a file. Define a *sync profile*: a versioned JSON document (`{"dive_sync_profile": 1, ...}`) containing `directionality`, `sync_filters`, `grace_window_minutes`, `api_cooldown_seconds`, `field_links` (including templates), format options, `sync_pairs` (Track F) and `cron_jobs`. Never credentials, tokens, sync state or conflicts. Export/import live in `src/core/config.py` (`export_profile()` / `import_profile()`), validated through the same Pydantic models, with a `version` field so an older build refuses a newer profile with a clear message instead of silently dropping keys. Both UIs expose Export and Import; the status page also accepts a profile via `POST /api/settings/import` (file upload) and serves one from `GET /api/settings/export` (download). Cron jobs are included even though the desktop app has no scheduler, so a user can author the whole Docker configuration on the desktop and upload it. Import replaces the listed sections wholesale (no merge) and shows a diff summary before applying. Links that reference a field this build's catalogue does not know are skipped and listed by name in the summary; the rest of the profile still imports. A newer profile major version is still refused outright.

### Steps

| # | Step | Status | Notes |
|---|------|--------|-------|
| C1 | `FieldSpec`, `FieldLink` and `field_links` on `SettingsModel` and `CronJobModel`; `default_field_links()` equal to current behaviour (including the location composite); type-compatibility validation; migration test that an old `settings.json` loads unchanged | ✅ | 2026-09-21. `src/core/fields.py` (models, value access, `validate_field_links()`, defaults), `config.py` re-exports `FieldLink`; tests in `tests/test_fields.py`. Keys are always `<service_id>.<name>` (see decisions). The GPS rule needs two default links (`gps` overwrite towards Divelogs, `gps_fill` fill-blank-only towards Garmin) to stay exact. `POST /api/settings` validates the board and keeps the saved one when the form omits it |
| C17 | `field_catalog()` on `BaseDiveAdapter`, implemented by Garmin, Divelogs and both mock adapters, with labels, units, `max_length` and writability; `UnifiedDive.service_fields` filled by `to_unified` and pushed by `update_dive` (Garmin `activityName`/`locationName`, Divelogs `location`/`divesite` stop being collapsed into `location`); new v1 fields rating, water/dive type, tags; `GET /api/fields` returns the catalogues per pair | 🚧 | 2026-09-21: catalogues (17 Garmin, 15 Divelogs fields), `service_id`/`display_name` on every adapter, `service_fields` filled and pushed (Divelogs `location`/`divesite` now round-trip exactly instead of via the comma split), `GET /api/fields` done. **Still open:** (1) `location` stays filled by the old collapse so uploads of new dives keep working until C18 renders the composite link; (2) rating / water type / dive type / tags: neither raw payload carries them (checked against a real Garmin and Divelogs dive on 2026-09-21), needs an API-docs check before adding; `max_length` is unknown for every text field and left empty |
| C18 | Template renderer in `src/core/templates.py`: `{key}` placeholders with format specs, validation against the catalogue, `preview(link, dive)`; truncation to `max_length` with warning; loop detection across links; tests for empty sources, format specs, unknown keys, non-text targets, truncation, loops | 📝 | Decided 2026-09-21. Composite links are already accepted and structurally validated (template required, one-way, text target); `SyncEngine.active_links()` skips them with a warning until this ships. Also to do here: switch uploads of new dives from `UnifiedDive.location` to the rendered composite and drop the collapse in both `to_unified` maps |
| C19 | Match keys from the board: engine tier 2 iterates links with `match_order` set (number/datetime only) instead of the hardcoded dive-number tier; default board flags the dive-number link; tests that the old ladder is reproduced by the default | ✅ | 2026-09-21. `SyncEngine.match_key_links()`, `fields.match_key_equal()` (numbers: same positive integer, i.e. the old dive-number rule; datetimes: identical). A match link may be drawn from either side. Tests in `tests/test_link_engine.py` |
| C20 | Test mapping: `POST /api/mapping/test` takes an unsaved board, fetches the newest 10 dives per side with the normal cooldown, returns per-dive source/target/result/conflict rows; strictly read-only; the same core function serves the QML board | 📝 | Decided 2026-09-21; needs credentials, so it runs inside the same process as the sync |
| C2 | Refactor the matched-pair loop in `run_sync` into a table-driven loop over `field_links`: render the source (single get or template), read the target, compare, apply the policy, write through `set_field` | ✅ | 2026-09-21. `SyncEngine._apply_link()`; existing tests pass unchanged. `tests/test_link_engine.py::test_link_loop_reproduces_legacy_loop` replays the old loop (kept verbatim as a reference in the test) against the new engine on the default board over 400 seeded field/direction combinations. One accepted deviation: a Garmin coordinate with only one of lat/lng set is no longer overwritten by an *empty* Divelogs pair. Also checked offline against the owner's local cache in all three directions: identical plans |
| C3 | Implement `target_wins`, `prefer_non_empty` and `off`; add dry-run output that names the rule applied per field | ✅ | 2026-09-21, fell out of C2: all four policies are dispatched in `_apply_link()` (`manual` only skips and logs a warning until C4 records it). Every field log line ends with `[link <id>, <policy>]`, which is what a dry run shows. Per-policy tests in `tests/test_link_engine.py` rather than `test_mock_sync.py` |
| C4 | `manual` policy: `conflicts.json` writer/reader in `src/core/conflicts.py`, engine skips the field and records it; `resolve_conflict()` pushes the chosen side | 📝 | Keep it in `src/core` so both UIs and the CLI reuse it. The engine half (skip + warning log) exists; only the recording and resolution are missing |
| C5 | CLI: `--list-conflicts`, `--resolve <id> source|target`, `--show-mapping` (links per pair as a table), `--validate-profile <file>` (same checks as Save), `--test-mapping` (the 10-dive read-only table); `run_sync_thread` passes `field_links` overrides from cron jobs | 🚧 | Scheduler half done 2026-09-21: `run_sync_thread` passes every cron-job value (direction, filters, `field_links`) as explicit `run_sync(...)` arguments. This also fixes a pre-existing bug: `run_sync` re-reads `settings.json` first thing, so the old code's edits to `engine.settings` were discarded and scheduled jobs always ran with the global direction/filters. CLI flags still to do |
| C6 | Status page: mapping board (two catalogue columns, drag-and-drop linking, SVG link lines, link editor for direction/conflict/template with live preview, composite by dropping a second field), pair picker and options header strip, match-key list, Save/Cancel with dirty indicator, Reset to defaults, apply-to-all prompt, Test mapping table, plus a conflicts table with pick-source / pick-target buttons | 📝 | Decided 2026-09-21. `GET/POST /api/settings` extended; new `GET /api/fields`, `POST /api/fields/preview`, `POST /api/mapping/test`, `GET /api/conflicts`, `POST /api/conflicts/{id}/resolve` |
| C7 | Desktop: the same mapping board in QML (`Drag`/`DropArea`, `Shape` lines, same header strip, link editor, preview and Test mapping) as its own sidebar section "Mapping", and a Conflicts view | 📝 | Decided 2026-09-21. Build in QML after Track D, not in Toga, to avoid writing it twice |
| C8 | Docs: README section explaining the board, links, templates, defaults and the conflict queue, with the activity-name example | 📝 | |
| C9 | Sync profile export/import in `src/core/config.py` with version check and round-trip tests; CLI `--export-profile <path>` / `--import-profile <path>` | 📝 | Everything in `SettingsModel` except nothing secret; credentials stay out by construction because they live in a different model |
| C10 | Status page: Export (download) and Import (upload, with diff summary and confirm) buttons; `GET /api/settings/export`, `POST /api/settings/import` | 📝 | |
| C11 | Desktop: Export / Import in Settings using native file dialogs | 📝 | Build in QML after Track D (with C7) |
| C13 | Deletion propagation, per pair, off by default: record tombstones (service, external id, seen-deleted-at) in sync state; when `propagate_deletes` is on, delete the linked dive on the other side and drop the tombstone; when off, log that the dive will be re-uploaded | 📝 | Decided 2026-09-20; depends on F1 pair model |
| C14 | Automatic pre-write backup: after fetch and before any add/update/delete, write `DATA_DIR/backups/<timestamp>/<service>.json` per side; keep last N (default 10); skipped in dry-run | 📝 | Decided 2026-09-20 |
| C15 | Timezone-aware matching: normalise both sides to UTC when the service provides an offset (Garmin `startTimeGMT`), keep naive-local as fallback, per-pair `grace_window_minutes`, cross-timezone fixtures | 📝 | Decided 2026-09-20; owner to supply one real mismatched pair as the test case |
| C16 | Per-pair `create_on_garmin` switch, default off: new dives found only on Divelogs (or other sources) are not created on Garmin unless enabled; matched-dive updates unaffected | 📝 | Decided 2026-09-20; flip default once proven |
| C21 | Reverse parsing for composite links: optional `reverse` regex with named groups so a composite target can be split back into its sources, making the link bidirectional | 📝 | Decided 2026-09-21 as a later item; v1 composites are one-way |
| C12 | Flip the default conflict policy from `source_wins` to `manual` once the conflict queue has been used for real; migration writes the explicit old default into existing `settings.json` so nothing changes silently | 📝 | Decided 2026-09-20; after C6/C7 are verified |

---

## Track D — Desktop app v2: migrate Toga → PySide6 / Qt Quick (features.md B12)

### Why

- Telemetry graphs are planned (Track E) and Toga has no charting; PySide6 ships QtCharts.
- `QAbstractTableModel` replaces the native `NSTableColumn` width hack the Toga editor needs.
- Qt's `QCoreApplication.aboutToQuit` fires reliably, so the credential materialisation workaround from B8 can be simplified to "materialise at operation start, clear on operation end and on quit".
- Same GUI/backend pattern was already used successfully in the previous project.

### Scope

Rewrite only `desktop/app.py`, `desktop/sections/*`, `desktop/async_utils.py`, `desktop/theme.py`, `desktop/widgets.py`. Unchanged: all of `src/core`, `desktop/credentials.py`, `desktop/paths.py`, `desktop/preferences.py`, `desktop/logging_bridge.py` (its queue is polled by a `QTimer` instead of an asyncio loop).

Packaging stays on Briefcase, which supports PySide6 as a GUI toolkit; the `[tool.briefcase.app.desktop]` block changes `requires` from `toga` to `PySide6` (or `PySide6-Essentials` + `PySide6-Addons` for QtCharts). Expect bundles in the 150–300 MB range versus Toga's. PySide6 is LGPL; the pip wheels link dynamically, which is compatible with this project's MIT licence.

### Steps

| # | Step | Status | Notes |
|---|------|--------|-------|
| D1 | Skeleton: `QGuiApplication` + `QQmlApplicationEngine`, `main.qml` with sidebar and section `StackLayout`, dark/light theme tokens in QML | 📝 | Validate `briefcase dev` and a packaged macOS build before writing any section |
| D2 | Backend bridge objects: `SyncController`, `DiveTableModel(QAbstractTableModel)`, `SettingsController`, exposed via `setContextProperty`; long jobs on `QThread` with signals for log lines, progress and completion | 📝 | Replaces `async_utils.run_polled_job` |
| D3 | Sync section in QML (parity with B2/B3) | 📝 | |
| D4 | Dive editor in QML: `TableView` bound to `DiveTableModel`, column chooser and sort bound to `preferences.py`, detail form, Save/Delete (parity with B5/B6/B9) plus new fields: tanks sub-table (O2/He, start/end pressure, volume), GPS lat/lng, rating, water temperature | 📝 | Decided 2026-09-20. `UnifiedDive` needs a `rating` field; `dive_cache.update_dive_fields` and both `update_dive` implementations must accept the new fields |
| D5 | Settings section in QML: credentials with Test buttons (parity with B7), plus profile Export / Import (C11); the Track C mapping board (C7) is its own sidebar section | 📝 | |
| D6 | Conflicts view (C7) | 📝 | |
| D7 | Wire `aboutToQuit` to `credentials.clear_local_cache()` and `clear_garmin_token_file()`; keep the operation counter | 📝 | Verify empirically on Cmd+Q, unlike Toga |
| D8 | Remove Toga code and the `toga` requirement; update `tests/` that import desktop modules | 📝 | `test_desktop_credentials.py` and `test_desktop_preferences.py` should not need changes |
| D9 | Live verification against fake keyring and a scratch `DATA_DIR`, as in Track B | 📝 | Never against real credentials or the real app-data dir |

---

## Track E — Phase 2 and remaining gaps

| # | Step | Status | Notes |
|---|------|--------|-------|
| E1 | Depth/temperature telemetry graphs in the desktop dive editor (QtCharts) | 📝 | features.md B1; after D4. Data is already in the raw Garmin cache (`activityDetails.metrics`) and Divelogs `sampledata` |
| E2 | Windows and Linux Briefcase targets, with CI runners for all three platforms | 📝 | features.md B2; decided 2026-09-20 to do during Track D, not after |
| E3 | macOS code signing and notarisation | 📝 | features.md B3 |
| E4 | Gas mixtures / tank data Divelogs→Garmin on matched-dive updates | 📝 | features.md B4. Blocked on understanding Garmin's `diveGases` write API; investigate before scheduling |
| E5 | Multi-tank round-trip through every adapter: tank order and role (`backGas`/`stage`/`deco`) preserved Garmin ↔ Divelogs ↔ Submersion ↔ Subsurface; Divelogs `tanks[]` verified with more than one entry | 📝 | features.md B5. **Raised priority 2026-09-20**: owner dives multi-tank regularly; schedule right after Track C |
| E6 | Remove the `sync_fit` switch from status page, desktop and cron-job UI; keep `SyncFilters.sync_fit` for `--backup` only | 📝 | Decided 2026-09-20 |
| E7 | Desktop account selection for multi-account credentials | 📝 | features.md B8, desktop half of A8 |
| E8 | Garmin → Subsurface export | ❌ | Superseded by Track F, which covers Subsurface and Submersion in both directions |
| E9 | More verification of Divelogs→Garmin; consider an opt-in live integration test behind an env flag | 📝 | features.md B6 |
| E10 | Docs drift: README `setup.py` → `setup_credentials.py`, refresh `DOCKERHUB.md` | 📝 | features.md B10; overlaps A5 |
| E13 | Add `LICENSE` (MIT) and fix the README licence line; note PySide6 LGPL compatibility | 📝 | Decided 2026-09-20 |
| E12 | Single release workflow: one version in `pyproject.toml` read by `version.py`; a `v*` tag builds the Docker image and macOS/Windows/Linux Briefcase bundles and attaches them to the GitHub Release | 📝 | Decided 2026-09-20; needs macOS and Windows CI runners |
| E11 | Submersion end-to-end encryption support: port `crypto/keyslots.dart` key derivation, passphrase in credentials, `SBE1` open/seal | 📝 | Owner does not use it; for other users. After F12 |

---

## Track F — Sync with Subsurface and Submersion (features.md B7, B13)

### What the two targets actually are (checked 2026-09-20)

| | Subsurface | Submersion |
|---|---|---|
| Project | github.com/subsurface/subsurface, C++/Qt, GPL-2.0, mature | github.com/submersion-app/submersion, Flutter/Dart, GPL-3.0, newer (v1.5, v2.0 in progress) |
| Native store | `.ssrf` XML, or a git repo of plain-text `key value` files (`YYYY/MM/DD-Www-hh=mm=ss/Dive-N`, `Divecomputer`) | Drift-managed SQLite, optional at-rest encryption, backup as `.sqlite` |
| Cloud | "Subsurface Cloud" is a git remote over HTTPS with email/password; per-user branch is the email. Read-only web view. No REST API | No server, no account. Per-device JSON changeset log (`ssv1.*` files) in the user's own iCloud / Google Drive / Dropbox / S3, optionally AES-256-GCM encrypted. Format read from source and written up in `docs/submersion_sync_format.md`. **Google Drive is unreachable for third parties** (hidden `appDataFolder`, scoped to Submersion's OAuth project); S3, Dropbox and iCloud-on-Mac are reachable |
| Imports | Subsurface XML, UDDF, Garmin FIT, many computer/app formats | Subsurface XML, UDDF, Garmin FIT, CSV presets (incl. Garmin Connect), DAN DL7, Shearwater, MacDive |
| Exports | Subsurface XML, UDDF, CSV, KML, HTML, divelogs.de upload | UDDF 3.2, CSV, Excel, KML, GPX, PDF, SQLite backup |
| Programmatic access | `subsurface-cli` (read-only JSON over a git repo), `subsurface-downloader`; no Python package | None. No CLI, no API |
| Own Garmin link | Imports `.FIT` from a Garmin folder | Optional Garmin Connect account connection, FIT import |

Neither has a writable API. **UDDF is the one format both read and write**, and Subsurface XML is readable by both. Writing straight into Submersion's SQLite would bypass its changeset log and never reach the user's other devices, so that route is rejected. Submersion's **cloud changeset log**, however, is a documented-enough peer protocol (see `docs/submersion_sync_format.md`): dive_sync can join as one more device, which gives true unattended two-way sync with every Submersion device the user owns.

### Design

Three adapters behind `BaseDiveAdapter`, all file-based, in order of value:

1. **`UddfAdapter`** (`services/uddf.py`) — reads and writes a UDDF 3.2 file. This alone gives an exchange path with both apps: dive_sync writes `export.uddf`, the user imports it; the user exports UDDF, dive_sync reads it. Units are SI in UDDF (metres, seconds, **kelvin**, **pascal**), so the adapter converts to the metric `UnifiedDive` (Celsius, bar). Samples map to `<waypoint>`, tanks to `<tankdata>` + `<mix>`.
2. **`SubsurfaceAdapter`** (`services/subsurface.py`) — reads and writes native `.ssrf` XML directly, which preserves more than UDDF (tags, extradata, dive sites, multiple dive computers). External IDs are stored as `<extradata key="garmin_id" value="…"/>` on the dive computer element so Tier 1 matching works on re-runs.
3. **`SubsurfaceCloudAdapter`** — wraps `SubsurfaceAdapter` around a clone of the user's cloud git repo (git-over-HTTPS with the Subsurface email/password, branch = email) using `dulwich` (pure Python, no git binary in the Docker image). Pull, apply, commit, push. This is the only fully unattended path to either app and belongs in Docker. The exact cloud clone URL format needs to be confirmed from `core/git-access.cpp` before this step is scheduled.

4. **`SubmersionAdapter`** (`services/submersion/`) — acts as a **peer device** in Submersion's changeset log. Reads every device's manifest, base and changesets from the shared store, merges them by HLC into an in-memory view of `dives` + `dive_sites` + `dive_tanks` + `dive_buddies` + `dive_profile_series`, and maps that to `UnifiedDive`. Writes go out as this device's own base/changesets under its own `deviceId`, stamped with its own HLC clock, with `schemaVersion: 210` in the manifest. Storage backends behind one small `SyncStore` interface: **S3-compatible** first (Docker on the NAS with Garage or SeaweedFS, or a free hosted tier such as Backblaze B2 or Cloudflare R2; `boto3`), then a **local folder** backend (Dropbox or iCloud folder on the desktop machine). Google Drive is not supportable and the docs must say so. E2E encryption is not supported in v1 (requires porting the passphrase key-slot derivation); the adapter refuses `SBE1` files with a clear message.

   Why a peer rather than a UDDF drop folder: Submersion has no automation to import a dropped file, so the UDDF route always needs a manual step on a phone. The peer route is fully unattended and keeps Garmin/Divelogs/Submersion consistent on the same schedule as everything else. The UDDF adapter (item 1) stays as the fallback for users who keep Google Drive.

**Engine generalisation.** `SyncEngine` is hardcoded to exactly Garmin and Divelogs (attribute names, result keys, `garmin_dir_name`, `external_ids["garmin"]`). It has to become pairwise: `SyncEngine(source_adapter, target_adapter)` with each adapter declaring its `service_id` (`garmin`, `divelogs`, `subsurface`, `uddf`) used for `external_ids`, cache dirs and result keys. A sync job then names a pair. This is the same refactor Track C2 touches, so **C2 must be written against generic "side A / side B" from the start**, otherwise it is done twice.

**Matching.** Subsurface and Submersion dives carry no Garmin or Divelogs IDs, so first runs match on dive number and timestamp only (Tiers 2 and 3). Subsurface XML supports `extradata` for storing IDs afterwards. Submersion has `dives.importSource` / `dives.importId`, which its own Garmin import already fills with the Garmin activity id, so Tier 1 works immediately for dives Submersion pulled from Garmin itself and dive_sync writes the same fields for dives it creates; the Divelogs id needs a `dive_custom_fields` row or a `divelogs:<id>` tag (decide in F9). UDDF has no clean slot, so the UDDF adapter falls back to dive number + timestamp on every run and must therefore be strict about `grace_window_minutes`.

**Format decisions to expose (extends Track C format options).** Subsurface tags and Submersion buddies are lists; Garmin/Divelogs have a single buddy string. Rule: join with `, ` outbound, split inbound. Location vs dive site: Subsurface has dive sites with GPS as separate objects; map `UnifiedDive.location` to the site name and `lat`/`lng` to the site coordinates.

### Where to host the Submersion sync store (checked 2026-09-20)

Sync data is a few MB, so every option below is far above what is needed. Google Drive is excluded (see §1 of `docs/submersion_sync_format.md`). Re-check free tiers before deciding; they change.

| Option | Free allowance | Card needed | Verdict |
|---|---|---|---|
| **Backblaze B2** (hosted) | 10 GB storage, perpetual; egress free up to 3× stored | No | **Recommended.** Mature S3 API, works identically from phone, Mac and Docker |
| Cloudflare R2 (hosted) | 10 GB storage, 1M writes / 10M reads per month, zero egress, perpetual | Yes, not charged within limits | Good second choice; Submersion lists it explicitly |
| Tebi (hosted, EU) | 25 GB in two copies, 250 GB transfer per month | No | Fine, smaller company with less track record |
| Scaleway (hosted, EU) | 90-day trial only now | Yes | Skip; the old permanent 75 GB tier is gone |
| AWS S3 | 5 GB for 12 months only | Yes | Skip |
| Garage (self-hosted on the NAS) | Free, AGPL | — | Lightweight, built for small home deployments, one container next to dive_sync. Phone must reach the NAS from outside (VPN or reverse proxy) |
| SeaweedFS (self-hosted) | Free, Apache-2.0 | — | Heavier than Garage, same networking caveat |
| MinIO community edition | — | — | **Do not use for new setups.** Repository archived April 2026, no further releases |
| Dropbox Basic | 2 GB | No | Zero-setup for phone + Mac (desktop app reads the local folder). Docker side would need a Dropbox API token rather than a folder, so weaker for unattended sync |

Sources: Cloudflare R2 pricing docs and community thread on the card requirement; Backblaze B2 pricing and developer pages; Scaleway Object Storage FAQ and the LowEndSpirit thread on the free-tier change; Tebi FAQ; MinIO GitHub issue #21714 (maintenance mode) and the 2026 write-ups on alternatives.

### Steps

| # | Step | Status | Notes |
|---|------|--------|-------|
| F1 | Generalise `SyncEngine` to two adapters with `service_id`; keep `garmin`/`divelogs` behaviour identical; results keyed by service id | ✅ | 2026-09-21. `SyncEngine(source_adapter=, target_adapter=)`; `engine.source`/`engine.target`, `source_id`/`target_id`; result keys are `uploaded_to_<id>` / `updated_on_<id>` with per-item `<id>_id`, `new_<id>_id`, `linked_<id>` (identical to the old keys for Garmin/Divelogs, no shim needed) plus pair-neutral `source_id`/`new_id`/`linked_id`. `engine.garmin` / `engine.divelogs` stay as aliases. `directionality` accepts `to_<service_id>` and the pair-neutral `to_source`/`to_target`. `backup()` and `download_and_save_raw_data()` remain Garmin/Divelogs-specific (F5). Non-Garmin pair test: `test_engine_with_a_non_garmin_pair` |
| F2 | `UddfAdapter` read/write with unit conversion and sample/tank mapping; fixtures from a real Subsurface export and a real Submersion export | 📝 | `services/uddf.py`, `tests/test_uddf.py`. Needs `lxml` or stdlib `xml.etree` (prefer stdlib) |
| F3 | CLI: `--target uddf:<path>` / `--source uddf:<path>` job form; settings `sync_pairs` list | 📝 | `sync.py`, `config.py` |
| F4 | `SubsurfaceAdapter` for `.ssrf`, with `extradata` external IDs and dive-site mapping | 📝 | `services/subsurface.py`; fixture `.ssrf` from a real export |
| F5 | Cron jobs and the status page can target a UDDF/`.ssrf` path under `DATA_DIR`; desktop Sync section gets a service pair selector | 📝 | After Track D for the desktop half |
| F6 | `SubsurfaceCloudAdapter` via `dulwich`: clone/pull, write, commit, push; credentials stored like the other services (file for Docker, keychain for desktop) | 📝 | Confirm clone URL and branch naming from `core/git-access.cpp` first. Respect that Subsurface Cloud is a shared git history: always pull before write, never force-push |
| F7 | UDDF fallback for Submersion-on-Google-Drive users: write UDDF into a configured folder on each run; user imports in the app | 📝 | Kept only as the fallback; F9–F13 is the real path |
| F8 | Docs: supported pairs matrix, what each format loses, ID-storage caveat for UDDF, "Google Drive not supported for Submersion" | 📝 | |
| F9 | Submersion format spike with a **real export**: settle the open questions in `docs/submersion_sync_format.md` §9 (timestamp unit, default diver, Divelogs-id slot), capture anonymised fixture files (one manifest, one base, one changeset, one profile blob) | 📝 | Needs the user to point Submersion at an S3 bucket (Garage/SeaweedFS on the NAS, or B2/R2/Tebi hosted) or Dropbox and share the resulting `ssv1.*` files |
| F10 | `services/submersion/codec.py`: Python port of the profile series codec (varint, zigzag, presence bitmaps, zlib) with byte-exact round-trip tests against the F9 fixtures; JSON row ↔ `UnifiedDive` mapping per §8 | 📝 | Pure functions, no I/O |
| F11 | `services/submersion/store.py`: `SyncStore` interface with `S3Store` (`boto3`, list by prefix `ssv1.`, get/put/delete) and `FolderStore` (local directory). Manifest/base/changeset readers, HLC merge into an in-memory library view, own HLC clock persisted next to `sync_state.json` | 📝 | Refuse `SBE1` envelopes with a clear error |
| F12 | `SubmersionAdapter(BaseDiveAdapter)`: `fetch_dives` from the merged view; `add/update/delete` append to this device's changeset log (own `deviceId`, `seq`, `publishedHlcHigh`, `appliedPeerHlc`, `epochId` from the epoch marker, `schemaVersion: 210`), rewriting the manifest last; base publish on first run; heartbeat republish so the peer is never retired; detect own `.retired.json` and re-join | 📝 | Verify end-to-end against a real Submersion device consuming our changesets before calling it done |
| F14 | `src/core/site_matcher.py`: resolve a `UnifiedDive` location + GPS to an existing site by exact name, else nearest site within `site_match_radius_m` (default 200), else create; used by F4 and F12 | 📝 | Decided 2026-09-20 |
| F13 | Settings/UI: Submersion pair config (store type, S3 endpoint/bucket/prefix/keys or folder path) in the profile, status page and desktop; credentials stored like the other services | 📝 | After C6 / Track D |

Out of scope for this track: writing Submersion's SQLite directly, reading end-to-end-encrypted Submersion files, Google Drive as a Submersion store, and importing from dive computers (both apps already do that better).

---

## Decisions log

Answers from the project owner that shape the plan. Newest at the bottom.

| Date | Question | Decision | Effect on plan |
|---|---|---|---|
| 2026-09-20 | Which pairs to sync | All three, in priority order: Garmin ↔ Divelogs, Submersion ↔ Garmin/Divelogs, Subsurface ↔ others. Two-way, not export-only | Track F stays before Track D in the order; Submersion (F9–F13) ahead of Subsurface Cloud (F6) |
| 2026-09-20 | Submersion sync store | Backblaze B2 | F11 `S3Store` targets B2 first; test bucket to be created by the owner |
| 2026-09-20 | Submersion E2E encryption | Owner does not use it, but other users might | v1 refuses encrypted stores with a clear message; add E11 "Submersion E2E passphrase support" as a later item |
| 2026-09-20 | Multiple accounts | One account per service for end users; a second account per service is needed **for testing** | A7/A8/E7 stay but drop to low priority; instead keep CLI `--garmin/--divelogs` selection working and make sure tests/fixtures can target a dedicated test account without touching the real one |
| 2026-09-20 | Audience | Public release intended (GitHub releases, Docker Hub, packaged desktop app) | Docs, first-run flow, platform builds, signing and E11 encryption gain priority; every user-facing string and error must be understandable without reading code |
| 2026-09-20 | Default conflict policy | Ship Track C with **garmin_wins** as default (today's behaviour); once the conflict queue is proven, switch the default to **manual** | C3 ships with garmin_wins (renamed `source_wins` on 2026-09-21); add C12 "flip default to manual" after C6/C7 are verified in real use; profile export must carry the explicit policy so the flip does not surprise existing installs |
| 2026-09-20 | Subsurface storage | Subsurface Cloud (git) | F6 is the Subsurface deliverable; F4 (`.ssrf` adapter) is still needed as its parser/writer, so F4 → F6 in sequence with no standalone local-file UI |
| 2026-09-20 | `sync_fit` flag | Drop it from the UIs, keep the backup behaviour | E6 becomes "remove the switch from status page, desktop and `CronJobModel` UI; keep `SyncFilters.sync_fit` for `--backup`" |
| 2026-09-20 | Desktop release timing | Wait for the Qt version; no public Toga build | Docker + CLI are the public surface until Track D ships; E3 signing applies to the Qt build only |
| 2026-09-20 | Desktop platforms | macOS, Windows and Linux all matter | E2 moves up: set up Briefcase targets and CI runners for all three during Track D, not after |
| 2026-09-20 | Test fixtures | Owner will provide real exports (Submersion B2 folder, Subsurface Cloud clone or `.ssrf`, UDDF from each), anonymised before commit | F2/F4/F9 can use byte-exact fixtures; add an anonymisation script under `tests/tools/` so re-exports are repeatable |
| 2026-09-20 | "Newest wins" policy | Only if the Divelogs API exposes an edit timestamp | C1 includes a check of the Divelogs dive payload for a modification timestamp; add `newest_wins` only if found |
| 2026-09-20 | Status page authentication | None built in; README documents putting it behind a reverse proxy, VPN or Tailscale | Add A10 "security section in README + `DIVE_SYNC_HOST` default note"; no auth code in `web/app.py` |
| 2026-09-20 | Versioning and releases | One version number, one git tag builds the Docker image and all three desktop bundles | Add E12 "single release workflow": `version.py` and `pyproject.toml` read the same version; `docker-release.yml` gains macOS/Windows/Linux Briefcase jobs |
| 2026-09-20 | Desktop editor fields to add | Gas mixtures/tanks, GPS coordinates, rating and water temperature (not tags/dive type) | D4 scope grows: tank sub-table in the detail form, lat/lng fields, rating and water temp scalars; `dive_cache.update_dive_fields` and both adapters' `update_dive` need the new fields (rating has no `UnifiedDive` field yet: add it) |
| 2026-09-20 | Languages | English only, no i18n framework | Nothing to add |
| 2026-09-20 | Deletion propagation | Propagate deletes with a per-pair setting, **off by default** | Add C13: track deleted external IDs in sync state (tombstones per pair) so a delete on one side can be applied on the other only when `propagate_deletes` is on; without it, document that a deleted dive is re-uploaded on the next full sync |
| 2026-09-20 | Automatic backup | Yes: JSON snapshot of every fetched dive list per run, before any remote write | Add C14: `DATA_DIR/backups/<timestamp>/<service>.json`, keep the last 10 (setting), skipped in dry-run; CLI `--restore-backup` is a later item |
| 2026-09-20 | Failure alerts | Optional webhook URL (ntfy / Gotify / generic POST) on failed runs | Add A11: `notify_url` in settings + profile; `run_sync_thread` POSTs a short text on exception or non-empty error list; status page field to set/test it |
| 2026-09-20 | Time zones | Owner dives across time zones and has seen wrong matches | Add C15: timezone normalisation in matching (compare in UTC where the service gives an offset, Garmin `startTimeGMT` vs `startTimeLocal`), a per-pair `grace_window_minutes` override, and cross-timezone fixtures in `test_sync.py`. Needs a concrete failing example from the owner |
| 2026-09-20 | Time zone example | Owner will look up a concrete mismatched pair (Garmin activity id + Divelogs dive id) | C15 waits for that pair as its failing test |
| 2026-09-20 | Sync frequency | Daily is the normal cadence; never hit HTTP 429 | Keep 1 s cooldown and daily default job; no backoff work needed now |
| 2026-09-20 | Dive site matching (Submersion/Subsurface) | Match by exact name, then nearest existing site within ~200 m by GPS, otherwise create a new site | Add to F4/F12: shared `site_matcher.py` in `src/core` used by both adapters; 200 m radius as a setting |
| 2026-09-20 | Out of scope | Photos/media, trips, equipment, certifications, dive plans stay out entirely | dive_sync syncs dives, sites, tanks, buddies, notes, GPS and profiles only; state this in README |
| 2026-09-20 | New-dive upload Divelogs → Garmin | Off by default for the public beta, opt-in per pair | Add C16: per-pair `create_on_garmin` switch (default false); bidirectional still updates matched dives both ways. Flip the default once the path is proven with real data |
| 2026-09-20 | Licence | MIT | Add E13: `LICENSE` file (MIT, 2026, Mikael Christersson), README licence line fixed; note PySide6 LGPL compatibility in README |
| 2026-09-20 | Multi-tank | Owner dives twinset/sidemount/stages regularly | E5 moves up to sit right after Track C; tank order and role (`backGas`/`stage`/`deco`) must round-trip through every adapter and the D4 tanks editor |
| 2026-09-21 | How the user defines the mapping | By linking fields with drag and drop, in both the Docker status page and the desktop app | Track C changes from a fixed rules grid to a field catalogue per adapter (C17) and a list of `FieldLink`s edited on a mapping board (C6 web, C7 QML); policies renamed `source_wins`/`target_wins`; the board's link list is what the profile exports |
| 2026-09-21 | Fields that exist on one side only (Garmin activity name) | Must be composable from several fields of the other side in a user-defined format | Composite links with a `{key}` template (C18, `src/core/templates.py`), live preview in both UIs; one-way in v1, reverse parsing later (C21); the Divelogs location/divesite comma split becomes a visible default composite link |
| 2026-09-21 | Composite target already differs | Follow the link's normal conflict policy | No special case in C2/C3 |
| 2026-09-21 | Empty source inside a template | Render as empty string | C18 has no optional-segment syntax |
| 2026-09-21 | Template editing | Text box plus a field picker that inserts `{key}`, with live preview | Same editor on web (C6) and QML (C7) |
| 2026-09-21 | Unit conversion | Stays automatic from the Divelogs profile flag; no per-link option | Board shows the unit next to number fields only |
| 2026-09-21 | Board scope | One board per sync pair, chosen with a pair picker | Links live on `sync_pairs[i].field_links` after F1 |
| 2026-09-21 | Field labels | Friendly label on the board, native key on hover and in templates | `FieldSpec.label` + `key` |
| 2026-09-21 | Presets | Reset-to-defaults button only; no named layouts | Profile files are the way to keep several layouts |
| 2026-09-21 | Re-applying a changed mapping | Ask after saving; default no; yes sets a one-shot full-compare flag | C6/C7 prompt, flag in sync state read by `run_sync` |
| 2026-09-21 | Match keys | The board defines an ordered list of match links (number/datetime) that replace the dive-number tier; external IDs first and timestamp last stay fixed | Add C19; `FieldLink.match_order` |
| 2026-09-21 | Length limits | Truncate to the field's `max_length`, warn in the preview, note once per dive in the log | `FieldSpec.max_length`; C18 |
| 2026-09-21 | Test mapping | Live fetch of the newest 10 dives per side with normal cooldown; strictly read-only | Add C20 `POST /api/mapping/test`; no "apply now" button |
| 2026-09-21 | Desktop placement | Own sidebar section "Mapping" | C7/D5 updated; Settings keeps credentials and profile export/import |
| 2026-09-21 | Link loops | Refuse to save; error names both links | C18 validation |
| 2026-09-21 | Transforms on single links | None; templates are the only mechanism | Keeps C18 small |
| 2026-09-21 | Read-only fields | Shown with a lock badge; drop refused; impossible directions hidden | `FieldSpec.writable`/`readable` drive the board |
| 2026-09-21 | Catalogue v1 extras | Rating, water type / dive type, tags | C17 scope; rating also needed by D4 |
| 2026-09-21 | Editing model | Save/Cancel with dirty indicator and leave warning; no autosave, no undo stack | C6/C7 |
| 2026-09-21 | Template timezone | Dive local time; `{date_time_utc}` added with C15 | C18 |
| 2026-09-21 | Pair options placement | Header strip on the board (grace window, create_on_garmin, propagate_deletes, match keys); credentials, cooldown, backups, alerts stay in Settings | C6/C7, D5 |
| 2026-09-21 | Profile import with unknown fields | Import the rest, list skipped links by name; newer major version still refused | C9/C10/C11 |
| 2026-09-21 | CLI and the board | `--show-mapping`, `--validate-profile`, `--test-mapping` share the core functions with the UIs | C5 extended |
| 2026-09-21 | Web dive list | None; backup snapshot and Test mapping stay behind the scenes | features.md 4.9 stays ❌ by design |
| 2026-09-21 | Append mode for text links | No; replace only, include `{notes}` in the template for footers | Keeps writes idempotent |
| 2026-09-21 | Conditional links | Out of v1/v2; `FieldLink.when` reserved so it can be added without a migration | features.md B18 later item |
| 2026-09-21 | Defaults for Submersion/Subsurface pairs | Full default link set per adapter | F4/F12 ship `default_field_links()`; used as adapter tests |
| 2026-09-21 | List fields to text | Join with a per-link separator (default ", "), split back the other way | `FieldSpec.type = "list"`, `FieldLink.separator` |
| 2026-09-21 | Narrow layout | Stack columns, links as a row list, drag still works, lines hidden | C6/C7 |
| 2026-09-21 | Catalogue key format (phase 1 implementation choice) | Every key is `<service_id>.<name>`, also for unified fields (`garmin.buddy`, `divelogs.buddy`, `garmin.gps`); the plan's bare `gps` example is not used | A link's ends must say which side of the pair they are on, because a link may read from either side (the shipped activity-name composite reads Divelogs fields). Templates use the same keys |
| 2026-09-21 | What `directionality` means for a link (phase 1 implementation choice) | The global direction decides which sides may be written at all. With one writable side the *other* side is always the origin (what `to_garmin` did before); with both writable the link's `conflict` policy picks the winner. `source_wins`/`target_wins` copy on any difference, including an empty value over a filled one (today's Garmin-wins behaviour); `prefer_non_empty`/`manual` only fill blanks | C2/C3. Consequence: on a one-way link `source_wins` and `target_wins` behave the same; the board (C6/C7) should show one "overwrite" choice there |
| 2026-09-21 | GPS default (phase 1 implementation choice) | Two default links: `gps` (Garmin → Divelogs, overwrite) and `gps_fill` (Divelogs → Garmin, `prefer_non_empty`) | Only way to keep the old asymmetric rule ("Divelogs → Garmin only when Garmin has no coordinates") exact. Owner may collapse it to one bidirectional link on the board later |
| 2026-09-21 | Site-name collapse in `to_unified` (phase 1 implementation choice) | Kept for now: Garmin `location` = activity/location name, Divelogs `location` = "location, divesite"; the native fields travel in `service_fields` alongside | Uploads of new dives still go through `UnifiedDive.location`; C18 switches them to the rendered composite and removes the collapse. Divelogs updates already push `location`/`divesite` natively (no more lossy comma split on round-trips) |
| 2026-09-21 | Catalogue v1 extras (rating, water/dive type, tags) | Deferred | Neither the real Garmin nor the real Divelogs raw payload carries them; needs an API-docs check (C17 note) |
| 2026-09-21 | "Newest wins" feasibility (the C1 check) | Divelogs dive payloads carry `created` and `modified` timestamps; Garmin `metadataDTO` has `lastUpdateDate` | `newest_wins` can be added as a fifth conflict policy after C4; not part of phase 1 |

## Suggested order

1. ✅ **C1 + C17 + F1 + C2 + C19 together** (2026-09-21, plus C3) — field catalogue and link model, engine generalised to two adapters, matched-pair loop and match tier made table-driven over links. One refactor, tested offline, no behaviour change. Leftovers folded into phase 2: C17's catalogue extras and the `location` un-collapse (with C18), C5's CLI flags.
2. **C3–C5, C18, C9** — the new conflict policies, conflicts queue, template renderer, CLI and scheduler wiring, and the profile export/import so links can move between machines from day one.
3. **E5** — multi-tank round-trip, right after the engine work since the owner dives multi-tank.
4. **F2–F4, F9** — UDDF adapter, pair-based CLI jobs, native `.ssrf` adapter, and the Submersion format spike with real fixtures (needs the user's S3 bucket or Dropbox setup, so start it early).
5. **A6, E10, A5** — small doc fixes, folded into the C8/F8 docs pass.
6. **C6, C20, C10, F5 (Docker half)** — status page mapping board with Test mapping, conflicts table, profile export/import, file-target jobs.
7. **Track D** — Qt migration, with C7, C11, D5, D6 and the F5 pair selector built directly in QML.
8. **F10–F12** — Submersion codec, store and peer adapter, verified against a real device.
9. **F6, F7, F13** — Subsurface Cloud git adapter, UDDF fallback, Submersion UI config.
10. **A8, A7, E7** — account selection across scheduler, status page and desktop.
11. **Track E** — graphs first (E1), then platform builds (E2, E3), then the data-model items (E4, E5, E6).

---

## Lessons and constraints (keep these)

- **Briefcase copies each `sources` entry by its basename.** A nested `"src/core"` entry flattens to `core/` and breaks every `from src.core.X import ...`. List the whole `"src"` tree.
- **toga-cocoa 0.5.6 never routes Cmd+Q or the Quit menu through `App.on_exit`, `request_exit` or even `atexit`.** `NSApplication`'s default `terminate:` ends the process first. This is why credentials are materialised per operation with a reference counter rather than once at startup. Track D removes the toga half of this problem but keep the counter.
- **The Garmin token cache is as sensitive as the password.** garth's token grants API access without it. On desktop it lives in the keychain and is written to disk only for the duration of an operation; whatever garth refreshed is read back before the file is deleted. CLI/Docker keep the plain `tokens/garmin/` directory.
- **Tests must never touch the real keychain or real app-data directory.** Two tests once ran without the `fake_keyring` fixture and moved the user's real Garmin token into the keychain. Every desktop test takes `fake_keyring` and a `tmp_path` `DATA_DIR`; live GUI verification uses a module-level in-memory `keyring` fake and a scratch data dir.
- **A normal sync never populates the dive cache.** Only `download_and_save_raw_data` writes the per-dive JSON files the editor reads. Any UI that lists dives needs a download/refresh path.
- **Garmin `startTimeLocal` is ISO with a `T` separator and sometimes fractional seconds.** Normalise once in `dive_cache._normalize_date_time`, not in the UI.
- **`DATA_DIR` must be set before `src.core.config` is imported.** `SETTINGS_FILE` and `CREDENTIALS_FILE` are computed at import time; `desktop/__main__.py` calls `paths.configure_environment()` first.
- **Toga `Table` rows need the accessibility `select` action in automated tests**, not `click`; `ConfirmDialog` buttons live under the window's sheet. Irrelevant after Track D but recorded in case Toga is revisited.
- **Google Drive `appDataFolder` is invisible to any other app.** Submersion stores its sync log there with the `drive.appdata` scope, scoped to its own Google Cloud project. No third-party OAuth client can list it. Any "sync through the user's Google Drive" idea for Submersion is dead on arrival; S3, Dropbox and iCloud-on-Mac are the reachable stores.
- **Respect Garmin login rate limits.** HTTP 429 on login is severe; both entry points go through the same cooldown-aware adapter and nothing else may log in on its own.
- **`SyncEngine.run_sync()` re-reads `settings.json` before every run.** Anything a caller wants to change for one run (direction, filters, `field_links`) must be passed as a `run_sync(...)` override argument; editing `engine.settings` beforehand is silently undone by the reload. The scheduler learned this the hard way (C5 note).
- **The link loop must stay behaviour-identical on the default board.** `tests/test_link_engine.py::test_link_loop_reproduces_legacy_loop` keeps a verbatim copy of the pre-Track-C loop as the reference; if a change to `_apply_link()` or the defaults is intended to alter behaviour, update that reference deliberately.

---

## Completed milestone log

Condensed from the original plan; dates are not recorded in git history for individual steps.

1. ✅ Scheduler extracted, Docker web app trimmed to status + schedule (A1–A4).
2. ✅ Briefcase/Toga skeleton validated with a packaged macOS build (B1).
3. ✅ Sync section with streamed log and progress (B2).
4. ✅ `dive_cache.py` and the shared dive editor with Save/Delete (B4, B5).
5. ✅ Keychain credentials, per-operation materialisation, first-run flow (B7, B8).
   - 5a ✅ Garmin token into keychain; Docker credential path confirmed as `DATA_DIR` only.
   - 5b ✅ Fixed "Refresh shows nothing": added Download dives (B3).
   - 5c ✅ Refresh downloads its own service's dives with a live status line (B3).
   - 5d ✅ Date/Time split; Garmin float formatting (B9).
   - 5e ✅ ISO `T` normalisation at the cache layer (B9).
   - 5f ✅ Column chooser and persisted sort (B6).
