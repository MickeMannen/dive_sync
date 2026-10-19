# Advanced

## Garmin dive cache

Garmin Connect is slow: each dive takes three API calls, with a cooldown between calls. **Use cached Garmin dives** (on by default; desktop app, web dashboard, scheduled jobs; `--no-garmin-cache` turns it off on the CLI) makes a sync skip dives it already has. The cache is refreshed as part of every sync, not in a separate step:

1. The sync fetches Garmin's activity list, a few calls in total.
2. It compares each dive in the list with the copy in the cache: name, location, dive number, duration, depths, temperature, times and a few more fields.
   - **Unchanged**: the cached copy is used, with no further calls to Garmin.
   - **New or changed**: the full dive is fetched (three calls) and the cache is updated.

New dives and most edits are always picked up. **The catch:** the activity list doesn't include notes, buddies, weight or visibility, so an edit to *only* those fields on Garmin leaves the dive looking unchanged, and the old cached copy is used. Such an edit is picked up by a sync with the option off, or by a **Full refresh** on the desktop app's Garmin dives page.

For scheduled jobs, a good setup is a frequent job with the cache on, plus a weekly job with it off to catch those edits. The cache lives in `garmin/<account>/data/` in the data folder.

## Limitations

- **Beta quality.** Garmin → Divelogs and Garmin → Subsurface are the exercised paths; Divelogs → Garmin and Subsurface → Garmin much less so. Keep backups (`--backup`, and Subsurface Cloud keeps its git history).
- **Gases and tanks reach Garmin Connect only in a `.fit` upload.** Garmin's activity API accepts no gas or tank data, on creation or update; gases on Garmin come from the dive computer, from tank sensors assigned in the Garmin Connect mobile app, and - opt-in and in beta - from a new dive uploaded as a `.fit` file (*New Garmin dives as .fit*, `--garmin-fit-upload`, `garmin_fit_upload`). Without it dives uploaded to Garmin arrive without tanks, and a dive already on Garmin never gets its tanks from a sync. Reading works: once a second transmitter is assigned to a dive in the mobile app, both tanks are visible to dive_sync. Details of the investigation: [garmin_diving_api.md](garmin_diving_api.md).
- **Divelogs numbers dives itself** from date and time, so dive numbers are never synced or matched on that pair.
- **Profiles and tanks flow one way to Divelogs** (Garmin cannot take them back). Divelogs stores profiles at a fixed sample rate; irregular Garmin profiles are resampled on the way.
- **Two runners, one account.** Docker (scheduled) and the desktop app (manual) may sync the same accounts from different machines. There is no shared lock and each keeps its own sync state, conflicts and Garmin token cache; both honour `api_cooldown_seconds`, but back-to-back runs from two machines can still hit Garmin's login rate limit (HTTP 429, wait 10–15 minutes). Let one runner finish before starting the other.
- **Pairs are remembered locally.** Garmin and Divelogs cannot store each other's id, so the matched pairs live in `sync/sync_state.json` next to your settings. Deleting that file makes the next run re-match by time (fine) and forget which dives it uploaded (uploads are not repeated because they now match by time, unless the times were shifted). Each pair is written the moment its upload succeeds, so a run that stops or fails part way (a dropped connection, a service error on a later dive) keeps the dives it already created linked, and the next run carries on from there instead of creating them again.
- **Multi-account** works only from the CLI (`--garmin`, `--divelogs`); the scheduler, web dashboard and desktop app assume one account per service.
- **Subsurface keeps a recorded dive's profile as recorded.** Its duration and depths follow from the dive computer's profile and are only written on hand-logged dives; Subsurface has no visibility in metres (only a 0–5 rating), so visibility is not synced there.
- **A water temperature of 0 °C counts as "not recorded".** Garmin stores 0 on a hand-logged dive whose temperature was never entered; a genuine 0 °C dive would be treated the same.
- **Mirror deletes for real.** Mirroring to Garmin removes device dives that cannot be restored from here (the automatic pre-sync backup keeps the dive data, not the original file). Run it as a dry run first, and download the FIT files beforehand if you want them.
- **MySSI is beta and add-only.** It has no public API: DiveSync uses the MySSI app's own connection, which SSI can change or close at any time. A sync adds new dives to MySSI only (changes to a dive already there are logged, not sent, until updating has been tested), skips a dive with two or more tanks, and never deletes on MySSI - not Mirror, not propagate deletes (a MySSI delete is permanent and its number may not come back; the MySSI Dives page can delete, and says so). Helium is not sent (a trimix tank goes as nitrox of its oxygen), nor buddy, events or the time zone. Garmin → MySSI is the exercised path.
- **Rebreather (CCR) support is untested.** The O2 cells, the setpoint, a diluent tank and the Shearwater app's cell readings are read and written where a format has a slot (Subsurface `.ssrf`, UDDF, the profile tooltip, the Shearwater log), but no real CCR dive has been through any of it yet; the Dive edit page and the dives pages say so on such a dive. Compare the values with your dive computer's own log before relying on them.
- **Submersion sync is disabled** until it works reliably.
