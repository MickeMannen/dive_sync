"""The Convert page's enrichment from the Garmin cache,
``src/core/convert/enrich.py`` (plans/convert.md I9, decision Q7; the
start-time fallback and the account preference of the I9 follow-up).

Synthetic cache entries are written into a temp DATA_DIR in the layout of
``layout.py`` (``garmin/<account>/data/<stem>.json``, the payload
``garmin_files.write_cached`` writes); the dives to fill are built here as
a FIT reader would hand them over. The owner's real cache is never read.
"""
import json
import logging
import os
from datetime import datetime, timedelta

from src.core import garmin_files, layout
from src.core.convert import enrich
from src.core.convert.enrich import (
    TIME_MATCH_SECONDS,
    CachedDive,
    Enrichment,
    cached_for,
    candidate_dates,
    date_of_name,
    enrich_dive,
    enrich_dives,
    enrich_from_cache,
    garmin_id_of,
    load_cached_dives,
    start_distance_seconds,
    time_key,
    wanted_ids,
)
from src.core.models import GasMixture, UnifiedDive


def cache_payload(activity_id, number=38, start="2026-08-29 09:56:11", location="House Reef", buddy="Kim",
                  notes="Easy drift", weight=4.0, visibility=15.0, tank_sizes=(12.0, 11.0), unit="kilogram", gmt=None):
    """One cached Garmin dive as Connect's listing + details payload;
    ``gmt`` is Connect's ``startTimeGMT`` (the UTC start) when given."""
    gases = [{"gasIndex": i, "oxygenContent": 21.0, "heliumContent": 0.0, "tankSize": size,
              "tankStartingPressure": 200, "tankEndingPressure": 60} for i, size in enumerate(tank_sizes)]
    info = {"buddy": buddy, "diveGases": gases}
    if weight is not None:
        info["weight"] = weight
        info["weightUnit"] = {"unitKey": unit}
    if visibility is not None:
        info["visibility"] = visibility
        info["visibilityUnit"] = {"unitKey": "meter"}
    summary = {"activityId": activity_id, "activityName": location, "startTimeLocal": start,
               "metadataDTO": {"diveNumber": number}}
    summary_dto = {"startTimeLocal": start, "duration": 2780, "maxDepth": 11.9}
    if gmt:
        summary["startTimeGMT"] = gmt
        summary_dto["startTimeGMT"] = gmt
    return {
        "summary": summary,
        "details": {"activityId": activity_id, "activityName": location, "description": notes,
                    "summaryDTO": summary_dto, "metadataDTO": {"diveNumber": number}, "diveInfo": info},
        "activityDetails": None, "tanksensor": None,
    }


def write_cache(base, account, payload, name=None):
    folder = layout.dives_dir("garmin", account, str(base))
    if name is None:
        return os.path.join(folder, garmin_files.write_cached(folder, payload))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "w") as f:
        json.dump(payload, f)
    return path


def fit_dive(activity_id="24449823373", tanks=2, **fields):
    """A dive as `read_fit` builds it: profile data, tanks without a volume,
    no site/buddy/notes/weight/visibility."""
    values = dict(date_time=datetime(2026, 8, 29, 9, 56, 11), duration=2780, max_depth=11.92, dive_number=38,
                  external_ids={"garmin": activity_id} if activity_id else {},
                  gas_mixtures=[GasMixture(oxygen=21.0, helium=0.0, start_pressure=200.0, end_pressure=60.0,
                                           tank_name=f"Tank {i + 1}") for i in range(tanks)])
    values.update(fields)
    return UnifiedDive(**values)


# --- the pure fill ------------------------------------------------------------

def test_fills_only_what_the_file_left_empty():
    cached = UnifiedDive(date_time=datetime(2026, 8, 29, 9, 56), duration=2780, max_depth=11.9,
                         location="House Reef", buddy="Kim", notes="Easy drift", weight=4.0, weight_unit="kilogram",
                         visibility=15.0, visibility_unit="meter",
                         gas_mixtures=[GasMixture(oxygen=21.0, tank_volume=12.0), GasMixture(oxygen=21.0, tank_volume=11.0)])
    dive = fit_dive()
    out, fields = enrich_dive(dive, cached)
    assert fields == ("site", "buddy", "notes", "weight", "visibility", "tank volume")
    assert (out.location, out.buddy, out.notes) == ("House Reef", "Kim", "Easy drift")
    assert (out.weight, out.weight_unit, out.visibility, out.visibility_unit) == (4.0, "kilogram", 15.0, "meter")
    assert [t.tank_volume for t in out.gas_mixtures] == [12.0, 11.0]
    # the profile data and the pressures are the file's, untouched
    assert out.max_depth == 11.92 and out.gas_mixtures[0].start_pressure == 200.0 and out.gas_mixtures[1].tank_name == "Tank 2"
    # the dive as read is kept as it was (a copy was filled)
    assert out is not dive and dive.location is None and dive.gas_mixtures[0].tank_volume is None

    # what the file holds is never overwritten; blanks count as empty
    dive = fit_dive(location="Reef as named by the watch", buddy=" ", weight=2.0, weight_unit="pound",
                    gas_mixtures=[GasMixture(oxygen=21.0, tank_volume=10.0), GasMixture(oxygen=21.0)])
    out, fields = enrich_dive(dive, cached)
    assert fields == ("buddy", "notes", "visibility", "tank volume")
    assert out.location == "Reef as named by the watch" and out.buddy == "Kim"
    assert (out.weight, out.weight_unit) == (2.0, "pound")
    assert [t.tank_volume for t in out.gas_mixtures] == [10.0, 11.0]


def test_tanks_pair_by_index_and_blank_cache_values_fill_nothing():
    cached = UnifiedDive(date_time=datetime(2026, 8, 29, 9, 56), duration=2780, max_depth=11.9,
                         location="", buddy=None, gas_mixtures=[GasMixture(oxygen=21.0, tank_volume=None)])
    # a cache without values: the dive is returned as the same object
    dive = fit_dive(tanks=2)
    out, fields = enrich_dive(dive, cached)
    assert out is dive and fields == ()
    # one cached tank for two in the file: the second keeps no volume; a 0 volume is no volume
    cached.gas_mixtures = [GasMixture(oxygen=21.0, tank_volume=12.0), GasMixture(oxygen=21.0, tank_volume=0.0)]
    out, fields = enrich_dive(dive, cached)
    assert fields == ("tank volume",) and [t.tank_volume for t in out.gas_mixtures] == [12.0, None]
    # a file without tanks gets none from the cache
    out, fields = enrich_dive(fit_dive(tanks=0), cached)
    assert fields == () and out.gas_mixtures == []


def test_enrich_dives_matches_by_activity_id_and_passes_the_rest_through():
    cached = {"1": CachedDive("me@example.org", UnifiedDive(date_time=datetime(2026, 1, 1), duration=1, max_depth=1.0, buddy="Kim"))}
    a = fit_dive("1")
    b = fit_dive("2")                       # not cached
    c = fit_dive(None)                      # a UDDF dive: no Garmin id
    d = fit_dive("1", buddy="Already")      # cached but nothing to fill
    result = enrich_dives([a, b, c, d], cached)
    assert result.dives[0].buddy == "Kim" and result.dives[0] is not a
    assert result.dives[1] is b and result.dives[2] is c and result.dives[3] is d
    assert result.enrichments == [Enrichment(index=0, account="me@example.org", fields=("buddy",))]
    assert result.enrichment_of(0).summary == "buddy from the Garmin cache (me@example.org)"
    assert result.enrichment_of(1) is None
    assert enrich_dives([], cached).dives == [] and enrich_dives([a], {}).dives == [a]
    # an entry filed under the dive's start time is found for a dive without an id, or with an unknown one
    by_time = CachedDive("test@example.org", cached["1"].dive, by_time=True)
    cached[time_key(c)] = by_time
    assert cached_for(c, cached) is by_time and cached_for(b, cached) is by_time and cached_for(a, cached) is cached["1"]
    result = enrich_dives([b, c], cached)
    assert [e.index for e in result.enrichments] == [0, 1] and all(e.by_time for e in result.enrichments)
    assert result.enrichment_of(1).summary == "buddy from the Garmin cache (test@example.org, matched by start time)"
    assert cached_for(fit_dive(None, date_time=datetime(2030, 1, 1)), cached) is None


def test_id_helpers():
    assert garmin_id_of(fit_dive("12")) == "12" and garmin_id_of(fit_dive(None)) is None
    assert garmin_id_of(fit_dive(" ")) is None
    assert wanted_ids([fit_dive("1"), fit_dive(None), fit_dive("1"), fit_dive("3")]) == {"1", "3"}


def test_time_helpers():
    """The time key is the UTC start when known, else the local one; the
    distance rule is SyncEngine's (UTC when both sides know it); the
    candidate dates cover the local and UTC day and their neighbours; the
    date comes from the cache's own file-name scheme only."""
    local, utc = datetime(2026, 8, 30, 0, 30), datetime(2026, 8, 29, 17, 30)
    assert time_key(fit_dive(date_time=local)) == "time:2026-08-30T00:30:00"
    assert time_key(fit_dive(date_time=local, date_time_utc=utc)) == "time:2026-08-29T17:30:00"
    a, b = fit_dive(date_time=local, date_time_utc=utc), fit_dive(date_time=local + timedelta(hours=7), date_time_utc=utc + timedelta(seconds=90))
    assert start_distance_seconds(a, b) == 90.0                            # UTC wins over the local clocks
    assert start_distance_seconds(fit_dive(date_time=local), b) == 7 * 3600  # one side without UTC: naive local
    assert candidate_dates(fit_dive(date_time=local)) == {"2026-08-29", "2026-08-30", "2026-08-31"}
    assert candidate_dates(fit_dive(date_time=local, date_time_utc=utc)) == {"2026-08-28", "2026-08-29", "2026-08-30", "2026-08-31"}
    assert date_of_name("38_2026-08-29_095611_24449823373.json") == "2026-08-29"
    assert date_of_name("nonum_2026-08-29_095611_24449823373.json") == "2026-08-29"
    assert date_of_name("5.json") is None and date_of_name("2026-08-29.json") is None
    assert TIME_MATCH_SECONDS == 120


# --- the cache ----------------------------------------------------------------

def test_load_cached_dives_reads_every_account_by_file_name(tmp_path):
    base = str(tmp_path)
    write_cache(tmp_path, "a@example.org", cache_payload(101, number=5, start="2026-06-01 10:00:00", location="Site A"))
    write_cache(tmp_path, "b@example.org", cache_payload(202, number=6, start="2026-06-02 10:00:00", location="Site B", tank_sizes=(15.0,)))
    write_cache(tmp_path, "b@example.org", cache_payload(303, number=7, start="2026-06-03 10:00:00", location="Not wanted"))
    found = load_cached_dives([fit_dive("101"), fit_dive("202"), fit_dive("999", date_time=datetime(2030, 1, 1))], base)
    assert set(found) == {"101", "202"}
    assert found["101"].account == "a@example.org" and found["101"].dive.location == "Site A"
    assert found["202"].account == "b@example.org" and found["202"].dive.gas_mixtures[0].tank_volume == 15.0
    assert found["202"].dive.buddy == "Kim" and found["202"].dive.weight == 4.0 and found["202"].dive.weight_unit == "kilogram"
    assert found["202"].dive.external_ids == {"garmin": "202"} and found["202"].dive.dive_number == 6
    assert not any(c.by_time for c in found.values())
    # ids are matched as strings whatever the dive holds; nothing wanted reads nothing
    assert set(load_cached_dives([fit_dive(" 101 ")], base)) == {"101"}
    assert load_cached_dives([], base) == {} and load_cached_dives([fit_dive("101")], str(tmp_path / "nowhere")) == {}


def test_load_cached_dives_opens_old_style_names_only_while_an_id_is_missing(tmp_path):
    base = str(tmp_path)
    write_cache(tmp_path, "a@example.org", cache_payload(101, location="Named"), name="5.json")        # the pre-E18 scheme
    write_cache(tmp_path, "a@example.org", cache_payload(202, location="Also named"), name="6.json")
    found = load_cached_dives([fit_dive("101")], base)
    assert set(found) == {"101"} and found["101"].dive.location == "Named"
    # a file named the old way is never a time match (its name holds no date)
    assert load_cached_dives([fit_dive(None)], base) == {}
    # the first account in name order wins when two hold the same id, unless one is preferred
    write_cache(tmp_path, "b@example.org", cache_payload(101, location="Second copy"))
    assert load_cached_dives([fit_dive("101")], base)["101"].account == "a@example.org"
    assert load_cached_dives([fit_dive("101")], base, preferred_account="b@example.org")["101"].account == "b@example.org"


def test_broken_cache_entries_are_a_log_line(tmp_path, caplog):
    base = str(tmp_path)
    folder = layout.dives_dir("garmin", "a@example.org", base)
    os.makedirs(folder)
    with open(os.path.join(folder, "1_2026-06-01_100000_101.json"), "w") as f:
        f.write("{not json")
    with open(os.path.join(folder, "2_2026-06-02_100000_102.json"), "w") as f:
        json.dump({"summary": {"activityId": 102, "startTimeLocal": "not a date"}, "details": {"summaryDTO": {"maxDepth": "deep"}}}, f)
    with open(os.path.join(folder, "notes.json"), "w") as f:
        json.dump([1, 2, 3], f)
    write_cache(tmp_path, "a@example.org", cache_payload(103, location="Fine"))
    elsewhere = datetime(2026, 6, 1, 10)      # the broken entries' day: their dives find no time match either
    with caplog.at_level(logging.WARNING, logger="dive_sync.convert.enrich"):
        found = load_cached_dives([fit_dive("101", date_time=elsewhere), fit_dive("102", date_time=elsewhere), fit_dive("103")], base)
    assert set(found) == {"103"}
    messages = [r.getMessage() for r in caplog.records]
    assert any("101.json could not be read" in m for m in messages)
    assert any("102.json could not be mapped" in m for m in messages)
    # the whole flow never raises on a broken cache
    result = enrich_from_cache([fit_dive("101", date_time=elsewhere), fit_dive("103")], base)
    assert result.dives[0].location is None and result.dives[1].location == "Fine"
    assert [e.index for e in result.enrichments] == [1]


def test_enrich_from_cache_end_to_end_uses_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    write_cache(tmp_path, "me@example.org", cache_payload(24449823373, tank_sizes=(12.0, 12.0), notes=None, visibility=None))
    dive = fit_dive("24449823373", tanks=2)
    other_day = fit_dive(None, date_time=datetime(2026, 2, 1, 10))
    result = enrich_from_cache([dive, other_day])
    out = result.dives[0]
    assert out.location == "House Reef" and out.buddy == "Kim" and out.notes is None and out.visibility is None
    assert out.weight == 4.0 and [t.tank_volume for t in out.gas_mixtures] == [12.0, 12.0]
    assert result.enrichments == [Enrichment(0, "me@example.org", ("site", "buddy", "weight", "tank volume"))]
    assert result.dives[1] is other_day and other_day.location is None
    assert enrich.TANK_VOLUME_WORD == "tank volume"
    # the same dive renamed (no id) is found by its start time, through DATA_DIR too
    result = enrich_from_cache([fit_dive(None, tanks=2)])
    assert result.dives[0].location == "House Reef"
    assert result.enrichments == [Enrichment(0, "me@example.org", ("site", "buddy", "weight", "tank volume"), by_time=True)]


# --- the start-time fallback (I9 follow-up) ----------------------------------

UTC_START = "2026-08-29 02:56:11"      # the 09:56 local dive in Asia/Bangkok


def _fit_at(utc, offset_hours=7, activity_id=None, **fields):
    """A FIT-read dive: UTC start and the local time the watch wrote."""
    return fit_dive(activity_id, date_time=utc + timedelta(hours=offset_hours), date_time_utc=utc, **fields)


def test_time_match_finds_the_dive_a_renamed_or_unknown_file_holds(tmp_path):
    """No id in the name, or an id no cache has: the cached dive starting
    within two minutes (on the UTC instants) is the match, the closest when
    several are near; three minutes off is no match."""
    base = str(tmp_path)
    utc = datetime(2026, 8, 29, 2, 56, 11)
    write_cache(tmp_path, "me@example.org", cache_payload(24449823373, location="House Reef", gmt=UTC_START))
    write_cache(tmp_path, "me@example.org", cache_payload(24449823374, number=39, start="2026-08-29 09:58:30",
                                                          gmt="2026-08-29 02:58:30", location="Next door"))
    write_cache(tmp_path, "me@example.org", cache_payload(24449823375, number=40, start="2026-08-29 14:00:00",
                                                          gmt="2026-08-29 07:00:00", location="Afternoon"))
    renamed = _fit_at(utc)                                          # 515.fit: no id
    unknown = _fit_at(utc, activity_id="99999999999")               # an export of another account's copy
    near = _fit_at(utc + timedelta(seconds=100))                    # 02:57:51: 100 s from the first, 39 s from the second
    far = _fit_at(utc + timedelta(minutes=6))                       # 03:02:11: more than two minutes from any
    found = load_cached_dives([renamed, unknown, near, far], base)
    assert set(found) == {time_key(renamed), time_key(near)}
    assert found[time_key(renamed)].dive.location == "House Reef" and found[time_key(renamed)].by_time is True
    assert found[time_key(near)].dive.location == "Next door"
    result = enrich_dives([renamed, unknown, near, far], found)
    assert [d.location for d in result.dives] == ["House Reef", "House Reef", "Next door", None]
    assert result.dives[3] is far and far.location is None
    assert [(e.index, e.by_time) for e in result.enrichments] == [(0, True), (1, True), (2, True)]
    assert result.enrichments[0].summary.endswith("from the Garmin cache (me@example.org, matched by start time)")
    assert renamed.location is None                                 # the dives as read are kept
    # a dive whose id is in the cache is taken by the id, never by the time
    by_id = _fit_at(utc, activity_id="24449823374")                 # the id of the 09:58 dive, at 09:56
    found = load_cached_dives([by_id], base)
    assert set(found) == {"24449823374"} and found["24449823374"].by_time is False


def test_time_match_compares_local_clocks_when_no_utc_is_known(tmp_path):
    """A cache entry without startTimeGMT, or a file without a zone: the
    naive local times are compared, as the sync engine does."""
    base = str(tmp_path)
    write_cache(tmp_path, "me@example.org", cache_payload(24449823373, location="House Reef"))     # no GMT
    local_only = fit_dive(None, date_time=datetime(2026, 8, 29, 9, 56, 40))
    assert load_cached_dives([local_only], base)[time_key(local_only)].dive.location == "House Reef"
    with_utc = _fit_at(datetime(2026, 8, 29, 2, 56, 11))            # local 09:56:11: the cache has no UTC to compare
    assert set(load_cached_dives([with_utc], base)) == {time_key(with_utc)}


def test_time_match_looks_at_the_neighbouring_days_for_the_date_edge(tmp_path):
    """The cache's file name holds Garmin's local date; a file whose zone is
    unknown (local = UTC) starts on the day before - the match is still made."""
    base = str(tmp_path)
    # a night dive at 00:30 on the 30th in Bangkok is 17:30 UTC on the 29th
    write_cache(tmp_path, "me@example.org", cache_payload(24449823380, number=41, start="2026-08-30 00:30:00",
                                                          gmt="2026-08-29 17:30:00", location="Night reef"))
    no_zone = fit_dive(None, date_time=datetime(2026, 8, 29, 17, 30, 5), date_time_utc=datetime(2026, 8, 29, 17, 30, 5))
    found = load_cached_dives([no_zone], base)
    assert found[time_key(no_zone)].dive.location == "Night reef"
    # and the other way round: the file knows its zone, the cache's name is a day later than UTC
    bangkok = _fit_at(datetime(2026, 8, 29, 17, 30, 5))
    assert load_cached_dives([bangkok], base)[time_key(bangkok)].dive.location == "Night reef"


def test_time_match_opens_only_the_files_of_the_dive_days(tmp_path, monkeypatch):
    """The prefilter on the file name's date: a cache of many dives on other
    days is listed, not parsed."""
    base = str(tmp_path)
    for i in range(6):
        day = datetime(2026, 7, 1 + i, 10)
        write_cache(tmp_path, "me@example.org", cache_payload(500 + i, number=i, start=day.strftime("%Y-%m-%d %H:%M:%S"),
                                                              gmt=(day - timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S")))
    write_cache(tmp_path, "me@example.org", cache_payload(24449823373, gmt=UTC_START))
    opened = []
    real = enrich._read_payload
    monkeypatch.setattr(enrich, "_read_payload", lambda path: opened.append(os.path.basename(path)) or real(path))
    found = load_cached_dives([_fit_at(datetime(2026, 8, 29, 2, 56, 11))], base)
    assert set(found) == {"time:2026-08-29T02:56:11"} and opened == ["38_2026-08-29_095611_24449823373.json"]
    # a dive on a day without an entry opens nothing
    opened.clear()
    assert load_cached_dives([_fit_at(datetime(2026, 9, 15, 2, 0))], base) == {} and opened == []


def test_accounts_are_tried_preferred_first_but_an_id_match_anywhere_wins(tmp_path):
    """The owner's case: a test account holding copies of the live account's
    dives under other ids, with the same start times."""
    base = str(tmp_path)
    utc = datetime(2026, 8, 29, 2, 56, 11)
    write_cache(tmp_path, "live@example.org", cache_payload(18314668175, location="Live copy", gmt=UTC_START))
    write_cache(tmp_path, "test@example.org", cache_payload(24449823373, location="Test copy", gmt=UTC_START))
    renamed = _fit_at(utc)
    # no preference: name order
    assert load_cached_dives([renamed], base)[time_key(renamed)].account == "live@example.org"
    # the selected account first (given as the username; the folder name is derived)
    assert load_cached_dives([renamed], base, preferred_account="test@example.org")[time_key(renamed)].account == "test@example.org"
    assert load_cached_dives([renamed], base, preferred_account="live@example.org")[time_key(renamed)].account == "live@example.org"
    assert load_cached_dives([renamed], base, preferred_account="nobody@example.org")[time_key(renamed)].account == "live@example.org"
    # an exact id match in the other account beats a time match in the preferred one
    exported = _fit_at(utc, activity_id="24449823373")
    found = load_cached_dives([exported], base, preferred_account="live@example.org")
    assert set(found) == {"24449823373"} and found["24449823373"].account == "test@example.org"
    assert enrich_dives([exported], found).enrichments[0].summary == "site, buddy, notes, weight, visibility, tank volume from the Garmin cache (test@example.org)"


def test_no_match_leaves_the_dive_as_read(tmp_path):
    base = str(tmp_path)
    write_cache(tmp_path, "me@example.org", cache_payload(24449823373, gmt=UTC_START))
    dive = _fit_at(datetime(2026, 8, 29, 4, 0), activity_id="1")    # same day, an hour later, another id
    assert load_cached_dives([dive], base) == {}
    result = enrich_from_cache([dive], base)
    assert result.dives == [dive] and result.dives[0] is dive and result.enrichments == []
    assert dive.location is None and dive.buddy is None and dive.gas_mixtures[0].tank_volume is None
