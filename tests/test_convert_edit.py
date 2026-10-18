"""The Dive edit page's core, ``src/core/convert/edit.py`` (plans/convert.md
I14): shifting a dive's start and applying field edits, without Qt."""
from datetime import datetime, timedelta

import pytest

from src.core.convert import edit
from src.core.convert.edit import (
    EditError,
    apply_edits,
    format_utc_offset,
    locked_fields,
    parse_utc_offset,
    shift_dive,
    tank_rows,
    utc_offset_text,
    zone_for_offset,
    zone_offset,
)
from src.core.models import DiveEvent, GasMixture, SampleChannels, UnifiedDive, UnifiedSample


def recorded_dive(**kwargs) -> UnifiedDive:
    """A computer-logged dive at 09:56:11 UTC+7 with samples, channels, two
    tanks and events (the two-transmitter fixture's shape, smaller)."""
    fields = dict(
        date_time=datetime(2026, 8, 29, 9, 56, 11), date_time_utc=datetime(2026, 8, 29, 2, 56, 11), timezone="Etc/GMT-7",
        duration=2780, max_depth=11.92, avg_depth=5.894, temp_min=30.0, temp_max=31.0, temp_avg=30.0, dive_number=38,
        lat=-10.0, lng=-30.0, exit_lat=-10.00005, exit_lng=-29.9998, location="House Reef", buddy="Kim",
        computer_vendor="Garmin", computer_model="Descent X50i", computer_serial="1000000001", computer_firmware="7.05",
        gf_low=40, gf_high=85, deco_model="ZHL-16C", water_type="salt", water_density=1025.0, dive_mode="oc_single_gas",
        surface_interval=497326, bottom_time=2599, cns_start=0.0, cns_end=2.0,
        gas_mixtures=[GasMixture(oxygen=21.0, tank_volume=11.1, start_pressure=204.0, end_pressure=164.0, tank_name="T1", tank_role="backGas"),
                      GasMixture(oxygen=32.0, tank_volume=11.1, start_pressure=210.0, end_pressure=162.0, tank_name="T2", tank_role="stage")],
        samples=[UnifiedSample(time=0, depth=0.0, temp=31.0, pressure=204.0, channels=SampleChannels(pressures={0: 204.0, 1: 210.0}, ndl=5940)),
                 UnifiedSample(time=600, depth=11.92, temp=30.0, pressure=190.0),
                 UnifiedSample(time=2780, depth=0.0, temp=31.0, pressure=164.0)],
        events=[DiveEvent(time=0, type="gas_switch", tank=0), DiveEvent(time=1200, type="gas_switch", tank=1),
                DiveEvent(time=2000, type="alert", name="Safety stop begin")],
    )
    fields.update(kwargs)
    return UnifiedDive(**fields)


def hand_logged_dive(**kwargs) -> UnifiedDive:
    fields = dict(date_time=datetime(2025, 7, 4, 16, 30), duration=1200, max_depth=5.0, avg_depth=3.0)
    fields.update(kwargs)
    return UnifiedDive(**fields)


def instants(dive: UnifiedDive):
    """The wall-clock instant of every sample and event."""
    return ([dive.date_time + timedelta(seconds=s.time) for s in dive.samples],
            [dive.date_time + timedelta(seconds=e.time) for e in dive.events])


# ---------------------------------------------------------------- offsets

def test_utc_offset_parsing_and_formatting():
    assert parse_utc_offset("+07:00") == timedelta(hours=7) and parse_utc_offset("-03:30") == timedelta(hours=-3, minutes=-30)
    assert parse_utc_offset("+7") == timedelta(hours=7) and parse_utc_offset("UTC+8") == timedelta(hours=8)
    assert parse_utc_offset("-0330") == timedelta(hours=-3, minutes=-30) and parse_utc_offset("Z") == timedelta(0)
    assert parse_utc_offset("") is None and parse_utc_offset(None) is None and parse_utc_offset("   ") is None
    for bad in ("+25:00", "+07:75", "Asia/Kuala_Lumpur", "seven", "7"):
        with pytest.raises(EditError, match="Time zone"):
            parse_utc_offset(bad)
    assert format_utc_offset(timedelta(hours=7)) == "+07:00" and format_utc_offset(timedelta(hours=-3, minutes=-30)) == "-03:30"
    assert format_utc_offset(timedelta(0)) == "+00:00" and format_utc_offset(None) == ""
    # the timezone value for an edited offset: the FIT reader's Etc spelling, a half hour as UTC+hh:mm
    assert zone_for_offset(timedelta(hours=7)) == "Etc/GMT-7" and zone_for_offset(timedelta(hours=-5)) == "Etc/GMT+5"
    assert zone_for_offset(timedelta(0)) == "Etc/UTC" and zone_for_offset(timedelta(hours=5, minutes=30)) == "UTC+05:30"
    assert zone_for_offset(None) is None
    # and back: every spelling the model may hold
    assert zone_offset("Etc/GMT-7") == timedelta(hours=7) and zone_offset("Etc/GMT+5") == timedelta(hours=-5)
    assert zone_offset("Etc/UTC") == timedelta(0) and zone_offset("UTC+05:30") == timedelta(hours=5, minutes=30)
    assert zone_offset("Asia/Kuala_Lumpur", datetime(2026, 8, 29, 10)) == timedelta(hours=8)
    assert zone_offset("Europe/Stockholm", datetime(2026, 7, 1, 12)) == timedelta(hours=2)        # summer time
    assert zone_offset("Europe/Stockholm", datetime(2026, 1, 1, 12)) == timedelta(hours=1)
    assert zone_offset(None) is None and zone_offset("") is None and zone_offset("Not/AZone") is None
    # a dive's offset: the two clocks win over the name; the name alone; nothing
    assert utc_offset_text(recorded_dive()) == "+07:00"
    assert utc_offset_text(recorded_dive(date_time_utc=None)) == "+07:00"
    assert utc_offset_text(recorded_dive(date_time_utc=None, timezone="Asia/Kuala_Lumpur")) == "+08:00"
    assert utc_offset_text(hand_logged_dive()) == ""


# ---------------------------------------------------------------- shift_dive

def test_shift_dive_moves_every_sample_and_event_by_the_delta():
    dive = recorded_dive()
    before_samples, before_events = instants(dive)
    shifted = shift_dive(dive, datetime(2026, 8, 29, 11, 26, 11))         # +1 h 30 min
    delta = timedelta(hours=1, minutes=30)
    after_samples, after_events = instants(shifted)
    assert after_samples == [t + delta for t in before_samples]
    assert after_events == [t + delta for t in before_events]
    # the offsets (the model's clock) and the duration are what they were
    assert [s.time for s in shifted.samples] == [0, 600, 2780] and [e.time for e in shifted.events] == [0, 1200, 2000]
    assert shifted.duration == 2780 and shifted.bottom_time == 2599
    # the UTC clock follows, the zone stays
    assert shifted.date_time == datetime(2026, 8, 29, 11, 26, 11) and shifted.date_time_utc == datetime(2026, 8, 29, 4, 26, 11)
    assert shifted.timezone == "Etc/GMT-7"
    # everything else is carried, and the dive given is untouched
    assert shifted.samples[0].channels.pressures == {0: 204.0, 1: 210.0} and shifted.events[2].name == "Safety stop begin"
    assert shifted.gas_mixtures == dive.gas_mixtures and shifted.max_depth == 11.92
    assert dive.date_time == datetime(2026, 8, 29, 9, 56, 11) and dive.date_time_utc == datetime(2026, 8, 29, 2, 56, 11)
    assert shifted.samples is not dive.samples and shifted.gas_mixtures[0] is not dive.gas_mixtures[0]
    # backwards across midnight
    back = shift_dive(dive, datetime(2026, 8, 28, 23, 0, 0))
    assert back.date_time_utc == datetime(2026, 8, 28, 16, 0, 0) and instants(back)[0][-1] == datetime(2026, 8, 28, 23, 46, 20)


def test_shift_dive_zone_change_without_a_wall_clock_change():
    dive = recorded_dive()
    moved = shift_dive(dive, dive.date_time, "Etc/GMT-8")
    assert moved.date_time == dive.date_time and moved.timezone == "Etc/GMT-8"
    assert moved.date_time_utc == datetime(2026, 8, 29, 1, 56, 11)        # one hour earlier in UTC
    assert instants(moved) == instants(dive)
    # zone and wall clock together
    both = shift_dive(dive, datetime(2026, 8, 29, 10, 0, 0), "UTC+05:30")
    assert both.timezone == "UTC+05:30" and both.date_time_utc == datetime(2026, 8, 29, 4, 30, 0)
    # the zone cleared: the UTC clock keeps the old offset by moving with the wall clock
    cleared = shift_dive(dive, datetime(2026, 8, 29, 10, 56, 11), None)
    assert cleared.timezone is None and cleared.date_time_utc == datetime(2026, 8, 29, 3, 56, 11)
    # a UTC clock but no zone name: the clocks' own offset is kept
    unnamed = shift_dive(recorded_dive(timezone=None), datetime(2026, 8, 29, 12, 0, 0))
    assert unnamed.date_time_utc == datetime(2026, 8, 29, 5, 0, 0) and unnamed.timezone is None
    # an IANA zone: the offset at the dive's date
    named = shift_dive(recorded_dive(timezone="Asia/Kuala_Lumpur", date_time_utc=None), datetime(2026, 8, 29, 10, 0, 0))
    assert named.date_time_utc == datetime(2026, 8, 29, 2, 0, 0)
    # a hand-logged dive without any clock but the local one: nothing to recompute
    plain = shift_dive(hand_logged_dive(), datetime(2025, 7, 4, 17, 0))
    assert plain.date_time == datetime(2025, 7, 4, 17, 0) and plain.date_time_utc is None and plain.timezone is None
    given_zone = shift_dive(hand_logged_dive(), datetime(2025, 7, 4, 17, 0), "Etc/GMT-2")
    assert given_zone.date_time_utc == datetime(2025, 7, 4, 15, 0) and given_zone.timezone == "Etc/GMT-2"
    # an aware datetime is taken as the wall clock
    from datetime import timezone as tz
    aware = shift_dive(dive, datetime(2026, 8, 29, 12, 0, tzinfo=tz.utc))
    assert aware.date_time == datetime(2026, 8, 29, 12, 0)


# ---------------------------------------------------------------- apply_edits

def test_apply_edits_numbers_and_text():
    dive = hand_logged_dive()
    out = apply_edits(dive, {"max_depth": "30.48", "avg_depth": 12, "duration": "2700", "temp_min": "27.5", "temp_max": 29,
                             "dive_number": "41", "surface_interval": 3600, "bottom_time": "2500.4", "water_density": "1020",
                             "lat": "5.123456", "lng": "-100.5", "exit_lat": "", "weight": 4.5, "visibility": "15",
                             "gf_low": "35", "gf_high": 80, "cns_start": 0, "cns_end": "3.5",
                             "location": "  Reef  ", "buddy": "Kim", "notes": "two\nlines", "computer_vendor": "Shearwater",
                             "computer_model": "Perdix 2", "computer_serial": "123", "computer_firmware": "v95", "deco_model": "VPM-B"})
    assert (out.max_depth, out.avg_depth, out.duration) == (30.48, 12.0, 2700)
    assert (out.temp_min, out.temp_max, out.dive_number, out.surface_interval, out.bottom_time) == (27.5, 29.0, 41, 3600, 2500)
    assert (out.water_density, out.lat, out.lng, out.exit_lat, out.exit_lng) == (1020.0, 5.123456, -100.5, None, None)
    assert (out.weight, out.weight_unit, out.visibility, out.visibility_unit) == (4.5, "kilogram", 15.0, "meter")
    assert (out.gf_low, out.gf_high, out.cns_start, out.cns_end) == (35, 80, 0.0, 3.5)
    assert (out.location, out.buddy, out.notes) == ("Reef", "Kim", "two\nlines")
    assert (out.computer_vendor, out.computer_model, out.computer_serial, out.computer_firmware, out.deco_model) == (
        "Shearwater", "Perdix 2", "123", "v95", "VPM-B")
    assert dive.max_depth == 5.0 and dive.location is None                  # the dive given is untouched
    # a key left out is untouched; blank clears an optional field and its unit
    again = apply_edits(out, {"buddy": "", "weight": "", "visibility": None, "gf_low": " ", "avg_depth": ""})
    assert again.buddy is None and again.weight is None and again.weight_unit is None
    assert again.visibility is None and again.visibility_unit is None and again.gf_low is None and again.avg_depth is None
    assert again.max_depth == 30.48 and again.location == "Reef"
    # the dive's own weight unit is kept (the page converts into it)
    pounds = apply_edits(hand_logged_dive(weight=8.0, weight_unit="pound"), {"weight": "10"})
    assert (pounds.weight, pounds.weight_unit) == (10.0, "pound")
    # errors name the field
    with pytest.raises(EditError, match="Max depth: not a number: 'deep'"):
        apply_edits(dive, {"max_depth": "deep"})
    with pytest.raises(EditError, match="Max depth: a value is needed"):
        apply_edits(dive, {"max_depth": ""})
    with pytest.raises(EditError, match="Latitude: 91 is above 90"):
        apply_edits(dive, {"lat": "91"})
    with pytest.raises(EditError, match="GF high: 120 is above 100"):
        apply_edits(dive, {"gf_high": 120})
    with pytest.raises(EditError, match="Duration: -5 is below 0"):
        apply_edits(dive, {"duration": "-5"})
    with pytest.raises(EditError, match="Water temp: the minimum 30 is above the maximum 20"):
        apply_edits(dive, {"temp_min": "30", "temp_max": "20"})
    with pytest.raises(EditError, match="GF low 90 is above GF high 80"):
        apply_edits(dive, {"gf_low": 90, "gf_high": 80})
    with pytest.raises(EditError, match="Not an editable field: samples"):
        apply_edits(dive, {"samples": []})
    with pytest.raises(EditError, match="not a number"):
        apply_edits(dive, {"max_depth": float("nan")})


def test_apply_edits_choices():
    dive = recorded_dive()
    out = apply_edits(dive, {"dive_mode": "ccr", "water_type": "fresh"})
    assert (out.dive_mode, out.water_type) == ("ccr", "fresh")
    cleared = apply_edits(dive, {"dive_mode": "", "water_type": None})
    assert cleared.dive_mode is None and cleared.water_type is None
    with pytest.raises(EditError, match="Dive mode: 'freedive' is not one of oc_single_gas, oc_multi_gas, gauge, ccr, scr, apnea"):
        apply_edits(dive, {"dive_mode": "freedive"})
    with pytest.raises(EditError, match="Water type: 'brackish' is not one of fresh, salt, en13319, custom"):
        apply_edits(dive, {"water_type": "brackish"})
    assert dive.dive_mode == "oc_single_gas"


def test_apply_edits_tanks_add_remove_edit():
    dive = recorded_dive()
    rows = tank_rows(dive)
    assert rows[1] == {"tank_name": "T2", "oxygen": 32.0, "helium": 0.0, "tank_volume": 11.1,
                       "start_pressure": 210.0, "end_pressure": 162.0, "tank_role": "stage"}
    # edit one cell of one tank: the rest of that tank and the other tank are untouched
    rows[0]["start_pressure"] = "206.84"
    out = apply_edits(dive, {"tanks": rows})
    assert out.gas_mixtures[0] == GasMixture(oxygen=21.0, tank_volume=11.1, start_pressure=206.84, end_pressure=164.0, tank_name="T1", tank_role="backGas")
    assert out.gas_mixtures[1] == dive.gas_mixtures[1]
    # add a tank: blanks are air / no helium / nothing
    added = apply_edits(dive, {"tanks": rows + [{"tank_name": "deco", "oxygen": "50", "helium": "", "tank_volume": "", "start_pressure": "", "end_pressure": "", "tank_role": ""}]})
    assert len(added.gas_mixtures) == 3
    assert added.gas_mixtures[2] == GasMixture(oxygen=50.0, helium=0.0, tank_name="deco")
    # a row holding only some keys keeps the old tank's others
    partial = apply_edits(dive, {"tanks": [{"tank_name": "renamed"}, {"helium": 10}]})
    assert partial.gas_mixtures[0].tank_name == "renamed" and partial.gas_mixtures[0].start_pressure == 204.0
    assert partial.gas_mixtures[1].helium == 10.0 and partial.gas_mixtures[1].oxygen == 32.0 and partial.gas_mixtures[1].tank_role == "stage"
    # remove the second tank; remove all
    one = apply_edits(dive, {"tanks": rows[:1]})
    assert len(one.gas_mixtures) == 1 and one.gas_mixtures[0].tank_name == "T1"
    assert apply_edits(dive, {"tanks": []}).gas_mixtures == [] and apply_edits(dive, {"tanks": None}).gas_mixtures == []
    assert len(dive.gas_mixtures) == 2
    # errors
    with pytest.raises(EditError, match="Tank 2 O2: not a number: 'x'"):
        apply_edits(dive, {"tanks": [rows[0], dict(rows[1], oxygen="x")]})
    with pytest.raises(EditError, match="Tank 1: O2 60 % and He 50 % add up to more than 100 %"):
        apply_edits(dive, {"tanks": [dict(rows[0], oxygen=60, helium=50)]})
    with pytest.raises(EditError, match="Tank 1 Start pressure: -1 is below 0"):
        apply_edits(dive, {"tanks": [dict(rows[0], start_pressure="-1")]})


def test_apply_edits_shifts_through_date_time_and_zone():
    dive = recorded_dive()
    out = apply_edits(dive, {"date": "2026-08-30", "time": "10:00", "utc_offset": "+08:00", "buddy": "Sam"})
    assert out.date_time == datetime(2026, 8, 30, 10, 0, 0) and out.timezone == "Etc/GMT-8"
    assert out.date_time_utc == datetime(2026, 8, 30, 2, 0, 0) and out.buddy == "Sam"
    assert [s.time for s in out.samples] == [0, 600, 2780] and out.duration == 2780
    # the time alone, with seconds; the date alone; the zone alone
    assert apply_edits(dive, {"time": "09:00:30"}).date_time == datetime(2026, 8, 29, 9, 0, 30)
    assert apply_edits(dive, {"date": "2026-09-01"}).date_time == datetime(2026, 9, 1, 9, 56, 11)
    zone = apply_edits(dive, {"utc_offset": "-03:30"})
    assert zone.date_time == dive.date_time and zone.timezone == "UTC-03:30" and zone.date_time_utc == datetime(2026, 8, 29, 13, 26, 11)
    blank_zone = apply_edits(dive, {"utc_offset": ""})
    assert blank_zone.timezone is None and blank_zone.date_time_utc == dive.date_time_utc
    with pytest.raises(EditError, match="Date: use YYYY-MM-DD, not '30/08/2026'"):
        apply_edits(dive, {"date": "30/08/2026"})
    with pytest.raises(EditError, match="Time: use HH:MM or HH:MM:SS, not '10 am'"):
        apply_edits(dive, {"time": "10 am"})
    with pytest.raises(EditError, match="Time zone"):
        apply_edits(dive, {"utc_offset": "Bangkok"})


def test_profile_fields_are_locked_on_a_recorded_dive():
    assert locked_fields(recorded_dive()) == ("duration", "max_depth", "avg_depth") and locked_fields(hand_logged_dive()) == ()
    with pytest.raises(EditError, match="max depth: set by the dive profile, not editable"):
        apply_edits(recorded_dive(), {"max_depth": "20"})
    with pytest.raises(EditError, match="duration, avg depth: set by the dive profile"):
        apply_edits(recorded_dive(), {"duration": "100", "avg_depth": "5", "buddy": "x"})
    # the same keys on a hand-logged dive are fine; the other fields of a recorded dive too
    assert apply_edits(hand_logged_dive(), {"duration": "100", "max_depth": "20", "avg_depth": "5"}).duration == 100
    assert apply_edits(recorded_dive(), {"temp_min": "29", "bottom_time": "2000"}).bottom_time == 2000
    assert set(edit.PROFILE_FIELDS) <= set(edit.EDITABLE_FIELDS) and "tanks" in edit.EDITABLE_FIELDS
