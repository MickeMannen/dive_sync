# Garmin Connect diving API — what dive_sync found (2026-09-22)

Reference for anyone touching the Garmin adapter or wondering why gases never
reach Garmin. Everything below was established empirically against a Garmin
Connect **test** account with the session that `python-garminconnect` /
`garth` obtains (the Garmin Connect *web* OAuth consumer). Nothing here is
documented by Garmin; treat it as observed behaviour that may change.

## 1. Two services, two ids

| | Activity service | Diving service |
|---|---|---|
| Base path | `/activity-service/activity/{activityId}` | `/diving/v1/...` |
| Id | `activityId` (what dive_sync stores as `external_ids["garmin"]`) | its own `id`; every entry also carries `connectActivityId` = the activity id |
| Gases | `diveInfo.diveGases[]` — read-only mirror | `equipment.gases[]` — the source of truth |
| Sensors | none | `tankSensors[]` |

`GET /diving/v1/dive/summary?connectActivityId=<any>` returns **all** dives of
the account (`totalCount`, `diveActivities[]`); the parameter does not filter.
Each entry:

```json
{"id": 32795372, "connectActivityId": 24438065346, "name": "Phuket Single-Gas Dive",
 "diveType": "SINGLE_GAS", "number": 39, "activitySource": "GARMIN_DEVICE",
 "startTime": "2026-08-29T12:52:59+07:00", "timezone": "Asia/Bangkok",
 "totalTime": 4310.07, "bottomTime": 4129.51, "maxDepth": 13.82,
 "entryLoc": {"latitude": 7.6093, "longitude": 98.3789},
 "gases": [{"gasStatus": "BOTTOM_GAS", "gasType": "AIR", "percentOxygen": 21, "percentHelium": 0, "gasMode": "OPEN_CIRCUIT"},
           {"gasStatus": "BACKUP_ONLY", "gasType": "AIR", "percentOxygen": 21, "percentHelium": 0}],
 "surfaceInterval": 8008, "contentVisibility": "PRIVATE", "hasTSBData": false}
```

`activitySource` is `GARMIN_DEVICE` for computer dives and `MANUAL_CONNECT`
for dives created through the activity service (what dive_sync uploads).
`startTime` carries the UTC offset and `timezone` the IANA zone: a cleaner
time-zone source than `startTimeLocal`/`startTimeGMT` on the activity.

`GET /diving/v1/dive/detail/{id}` (diving-service id) returns, among others:

- `equipment.gases[]`:
  `gasId`, `gasStatus` (`BOTTOM_GAS`, `BACKUP_ONLY`, `DECO`), `gasType`
  (`AIR`, `NITROX`, `TRIMIX`), `percentOxygen`, `percentHelium`, `gasMode`
  (`OPEN_CIRCUIT`), `startPressure` + `startPressureUnit` (`BAR`),
  `endPressure` + unit, `tankType` (`ALUMINUM`, ...), `tankSize` +
  `tankSizeUnit` (`LITER`), `sacRateUnit`.
- `tankSensors[]`: `tankIndex`, `name` (the transmitter's serial or the name
  given in the app), `antChannelId`, `pressureUnit`, `ratedPressure`,
  `reservePressure`, `usedForGasRate`, `startingPressure`, `endingPressure`,
  `volumeUsed`, `partNumber`, `softwareVersion`.
- `config[]` (deco model, GF), `environment` (temperatures, water type,
  density), `performance` (CNS, N2, ppO2), `records` (the profile columns),
  `location`, `diveSiteSummary`, `weather`, `media`, `diveTags`.

`GET /diving/v1/dive/detail/tanksensor?connectActivityId=<activityId>` is the
per-activity sensor view dive_sync already reads (`tankSensors[]` with the
pressure `records`).

## 2. How sensors get onto a dive

A Descent records tank pressure from every paired transmitter, but a
transmitter only appears on a dive once it is **assigned to that dive in the
Garmin Connect mobile app** (dive → tanks → assign sensor). The web UI cannot
do this. Before the assignment the activity showed one gas with no pressures
and one sensor; after it, `diveInfo.diveGases` had two entries (index 0 with
`tankType`, `tankSize`, pressures; index 1 with `status: 2` and its own
pressures) and both `tankSensors` were listed. The gas index matches the
sensor's `tankIndex`, which is how `GarminAdapter._map_to_unified` pairs them.

Consequence for dive_sync: **reading multi-tank dives from Garmin works as
soon as the diver has assigned the sensors on the phone.** Nothing else is
needed on our side. The FIT file of the dive contains both sensors regardless
of the assignment (Subsurface and Submersion import them from the FIT file),
so `fetch_fit_file` remains the complete source if a user never assigns.

## 3. Writing gases: every door is closed

| Attempt | Result |
|---|---|
| `POST /activity-service/activity` with `diveInfo.diveGases` (creation) | dive created, gases dropped silently |
| `PUT /activity-service/activity/{id}` with `diveInfo.diveGases` (update) | 200, gases still `null` |
| `OPTIONS /diving/v1/dive/{detail,gases,tanksensor,list}` | `Allow: POST, PATCH, DELETE, OPTIONS` |
| `POST` on those paths with JSON or an arbitrary multipart part | 500 "not a multipart request" / 400 "Required part 'userfile' is not present" — these are **file uploads** (the FIT upload path of the app) |
| `PATCH` with `application/json`, `merge-patch+json`, form data, text | 415 Unsupported Media Type |
| `PATCH /diving/v1/dive/{id}` with `application/json-patch+json` (`[{"op":"replace","path":"/name",...}]`) | the route exists (the id is parsed) but answers **403 "Access is denied"** — for a manual dive *and* for a device dive, even for a harmless rename |

The 403 is a scope problem, not a data problem: the web session's OAuth
consumer is not allowed to mutate the diving service. The mobile app uses a
different consumer with that scope, which is exactly why it can assign
sensors and edit gases while the web cannot. Obtaining the mobile consumer's
credentials is outside what dive_sync can do legitimately.

**Decision (rework.md E4):** the Garmin adapter stays read-only for tanks and
gases. Dives uploaded to Garmin arrive without tanks; the README says so.

## 4. Things worth using later

- The diving-service summary is one request for the whole account with time
  zone, gas roles and dive numbers; `fetch_recent_dives` and C15 could use it
  instead of three activity calls per dive.
- `equipment.gases[].gasStatus` gives tank roles (`BOTTOM_GAS`, `DECO`,
  `BACKUP_ONLY`) for E5's role round-trip; dive_sync currently keeps only
  order.
- `activitySource` distinguishes dive_sync's own uploads
  (`MANUAL_CONNECT`) from device dives, useful for a "re-upload / clean up"
  tool.
