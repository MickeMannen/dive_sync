"""**This module WRITES to the Garmin test account.** It answers one open
question (rework.md G8 / the 2026-09-23 board discussion): are a dive's start
time, duration, max depth and average depth writable through
``PUT /activity-service/activity/{id}``?

They live in ``summaryDTO``, the same nested object as ``startLatitude`` /
``startLongitude`` and the three water temperatures, which D4 proved *are*
writable when sent as a minimal partial payload (docs/garmin_diving_api.md
section 4). The four fields above are still marked ``writable=False`` in
``GarminAdapter.field_catalog()`` - an assumption inherited from before that
finding, never tested. The water-temperature fields carried the same flag for
the same reason until D4 actually tried them.

Each field is probed on two dives when the account has both: one logged by a
device (``GARMIN_DEVICE``) and one entered by hand (``MANUAL_CONNECT``). A
manual dive has no recorded profile for Garmin to contradict, so it is the
case most likely to accept an edit - and the one the owner asked about.

Every value written is restored afterwards and the restore is verified. If a
restore fails the test says so loudly, naming the dive and the field, so the
account is never left quietly modified.

Gated twice on purpose: the usual ``DIVE_SYNC_LIVE_DATA_DIR`` *and*
``DIVE_SYNC_LIVE_WRITES=1``, because everything else in this package is
read-only.

    DIVE_SYNC_LIVE_DATA_DIR=~/.dive_sync_test DIVE_SYNC_LIVE_WRITES=1 \\
        ./run_tests.sh tests/live/test_live_garmin_writes.py -s

``-s`` matters: the findings are printed, not asserted. Expect a couple of
minutes, nearly all of it the one-second cooldown between API calls; progress
is printed as it goes.
"""
import os
import time
from typing import Any, Dict, List, Optional

import pytest

pytestmark = pytest.mark.live

WRITE_ENV = "DIVE_SYNC_LIVE_WRITES"
COOLDOWN = 1.0          # seconds between API calls, as the adapter uses
DEVICE, MANUAL = "GARMIN_DEVICE", "MANUAL_CONNECT"


def _require_write_opt_in():
    if os.environ.get(WRITE_ENV) != "1":
        pytest.skip(f"writes to the test account need {WRITE_ENV}=1")


# --------------------------------------------------------------------- helpers

def _get_activity(garmin, activity_id: str) -> Dict[str, Any]:
    time.sleep(COOLDOWN)
    return garmin.client.connectapi(f"/activity-service/activity/{activity_id}")


def _summary(activity: Dict[str, Any]) -> Dict[str, Any]:
    return activity.get("summaryDTO") or {}


def _put_summary(garmin, activity_id: str, summary: Dict[str, Any]):
    """A minimal partial ``summaryDTO`` PUT - never the whole echoed-back
    object (docs/garmin_diving_api.md section 4). Returns (ok, detail)."""
    payload = {"activityId": int(activity_id) if str(activity_id).isdigit() else activity_id,
               "summaryDTO": dict(summary)}
    time.sleep(COOLDOWN)
    try:
        garmin.client.client.put("connectapi", f"/activity-service/activity/{activity_id}",
                                 json=payload, api=True)
        return True, "204"
    except Exception as e:                      # the API answers 400 with a validation body
        return False, str(e)[:300]


def _diving_summaries(garmin) -> Dict[str, Dict[str, Any]]:
    """The diving service's own record per activity id. It carries
    ``activitySource`` (device vs manual) and the summary the dive detail
    shows, which is not necessarily what the activity service holds."""
    time.sleep(COOLDOWN)
    try:
        data = garmin.client.connectapi("/diving/v1/dive/summary?connectActivityId=0") or {}
    except Exception as e:
        print(f"  diving service unavailable: {str(e)[:200]}")
        return {}
    return {str(d.get("connectActivityId")): d for d in data.get("diveActivities", []) or []}


def _parse_stamp(value: Any):
    from datetime import datetime
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _close(a: Any, b: Any, tolerance: float = 0.01) -> bool:
    """Compare two summaryDTO values. Timestamps are compared as *instants*,
    never as strings: Garmin echoes a value back with its own fractional-second
    spelling ('...:00.0' for a sent '...:00.000000'), and a plain string
    compare reads that as "did not persist" - which on 2026-09-23 made the
    probe report start time as ignored when it had in fact been written, and
    therefore skip its restore."""
    if a is None or b is None:
        return a is None and b is None
    stamp_a, stamp_b = _parse_stamp(a), _parse_stamp(b)
    if stamp_a and stamp_b:
        return stamp_a == stamp_b
    try:
        return abs(float(a) - float(b)) <= tolerance
    except (TypeError, ValueError):
        return str(a) == str(b)


# The four fields under test, with the change to attempt. ``keys`` holds the
# summaryDTO keys sent together: start time is tried both ways because
# startLatitude/startLongitude taught us a lone key can return 204 and
# silently not persist (docs/garmin_diving_api.md section 4).
def _shift_time(value: str, seconds: int) -> str:
    from datetime import datetime, timedelta
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return (datetime.strptime(value, fmt) + timedelta(seconds=seconds)).strftime(fmt)
        except ValueError:
            continue
    raise AssertionError(f"unparsed Garmin timestamp {value!r}")


PROBES = [
    {"name": "duration", "keys": ["duration"], "change": lambda v: (v or 0) + 60},
    {"name": "maxDepth", "keys": ["maxDepth"], "change": lambda v: round((v or 0) + 0.5, 2)},
    {"name": "averageDepth", "keys": ["averageDepth"], "change": lambda v: round((v or 0) + 0.5, 2)},
    {"name": "startTimeLocal (alone)", "keys": ["startTimeLocal"],
     "change": lambda v: _shift_time(v, 60)},
    {"name": "startTimeLocal+GMT (together)", "keys": ["startTimeLocal", "startTimeGMT"],
     "change": lambda v: _shift_time(v, 60)},
]


def _probe_field(garmin, activity_id: str, probe: Dict[str, Any], results: List[Dict[str, Any]],
                 source: str, before: Dict[str, Any]) -> None:
    """Write one field, read back, restore, verify the restore. ``before`` is
    the dive's summary as last confirmed - the previous probe restored and
    verified it, so re-reading it per field would only buy API calls."""
    name, keys = probe["name"], probe["keys"]
    originals = {k: before.get(k) for k in keys}
    if any(v is None for v in originals.values()):
        results.append({"dive": activity_id, "source": source, "field": name,
                        "outcome": "skipped", "detail": f"not present: {originals}"})
        return

    try:
        wanted = {k: probe["change"](originals[k]) for k in keys}
    except AssertionError as e:
        results.append({"dive": activity_id, "source": source, "field": name,
                        "outcome": "skipped", "detail": str(e)})
        return

    accepted, detail = _put_summary(garmin, activity_id, wanted)
    after = _summary(_get_activity(garmin, activity_id))
    persisted = all(_close(after.get(k), wanted[k]) for k in keys)
    if not accepted:
        outcome = "rejected"
    elif persisted:
        outcome = "WRITABLE"
    else:
        outcome = "silently ignored"
        detail = f"sent {wanted}, read back {({k: after.get(k) for k in keys})}"

    restored = True
    if accepted:                                 # a 204 may have persisted even if the read-back
                                                 # disagrees - restore anyway, never leave it to luck
        ok, restore_detail = _put_summary(garmin, activity_id, originals)
        back = _summary(_get_activity(garmin, activity_id))
        restored = ok and all(_close(back.get(k), originals[k]) for k in keys)
        if not restored:
            detail = f"{detail} | RESTORE FAILED ({restore_detail}); left as {({k: back.get(k) for k in keys})}"

    results.append({"dive": activity_id, "source": source, "field": name,
                    "outcome": outcome, "detail": detail, "restored": restored,
                    "original": originals})


# --------------------------------------------------------------------- the probe

def test_probe_whether_garmin_accepts_summary_field_writes(request):
    """WRITES AND RESTORES on the Garmin test account. Reports, per field and
    per dive kind, whether the value was rejected, silently ignored, or really
    written - the three outcomes that decide whether the mapping board should
    keep locking these fields.

    ``live_engine`` is pulled in only after the write gate passes: a
    session-scoped fixture named in the signature would otherwise log in to
    Garmin before the skip could fire."""
    _require_write_opt_in()
    live_engine = request.getfixturevalue("live_engine")
    garmin = live_engine.source

    # One listing call. Never fetch_recent_dives(): that pulls details,
    # telemetry and tank sensors per dive (three calls each, with the
    # adapter's cooldown between them) for data this probe never looks at.
    # The diving service already lists every dive with its activitySource.
    print("\nlisting dives (one call)...", flush=True)
    diving = _diving_summaries(garmin)
    by_source: Dict[str, str] = {}
    newest_first = sorted(diving.values(), key=lambda d: str(d.get("startTime") or ""), reverse=True)
    for entry in newest_first:
        source = entry.get("activitySource") or "UNKNOWN"
        by_source.setdefault(source, str(entry.get("connectActivityId")))
    if not by_source:                                   # diving service unavailable: plain activity listing
        print("  diving service gave nothing; falling back to the activity listing", flush=True)
        activities = garmin._list_dive_activities()
        assert activities, "no dive activities on the Garmin test account"
        by_source["UNKNOWN"] = str(activities[0].get("activityId"))

    print(f"dive kinds found: { {k: v for k, v in by_source.items()} }", flush=True)
    if MANUAL not in by_source:
        print(f"  NOTE: no {MANUAL} dive on the account - the hand-entered case (the one you "
              f"asked about) is NOT tested. Add a dive by hand on Garmin Connect and rerun.", flush=True)

    targets = [(src, by_source[src]) for src in (MANUAL, DEVICE, "UNKNOWN") if src in by_source]
    calls = len(targets) * len(PROBES) * 4
    print(f"probing {len(PROBES)} fields on {len(targets)} dive(s): up to ~{calls} API calls, "
          f"~{int(calls * COOLDOWN)}s of cooldown\n", flush=True)

    results: List[Dict[str, Any]] = []
    for source, activity_id in targets:
        print(f"probing {source} dive {activity_id}", flush=True)
        baseline = _summary(_get_activity(garmin, activity_id))
        for probe in PROBES:
            _probe_field(garmin, activity_id, probe, results, source, baseline)
            last = results[-1]
            print(f"  {last['field']:<32} {last['outcome']:<18} {last.get('detail', '')}", flush=True)

        # Does the diving service (what the app's dive detail shows) follow?
        if source == DEVICE and any(r["outcome"] == "WRITABLE" for r in results if r["dive"] == activity_id):
            fresh = _diving_summaries(garmin).get(activity_id, {})
            print(f"  diving-service record after the writes: "
                  f"totalTime={fresh.get('totalTime')}, maxDepth={fresh.get('maxDepth')}, "
                  f"startTime={fresh.get('startTime')}")

    print("\n--- summary (field x dive kind) ---")
    for r in results:
        print(f"{r['source']:<16} {r['field']:<32} {r['outcome']}")

    writable = sorted({r["field"] for r in results if r["outcome"] == "WRITABLE"})
    print(f"\nwritable through a minimal summaryDTO PUT: {writable or 'none'}")
    if writable:
        print("-> unlock the matching FieldSpec(s) in GarminAdapter.field_catalog() "
              "and record the finding in docs/garmin_diving_api.md")

    # The probe reports; it only fails when it left the account dirty or
    # learned nothing at all.
    dirty = [r for r in results if not r.get("restored", True)]
    assert not dirty, f"values left changed on the test account: {dirty}"
    assert any(r["outcome"] != "skipped" for r in results), "every field was absent; nothing was probed"
