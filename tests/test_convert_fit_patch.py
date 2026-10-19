"""The Garmin FIT patcher ``src/core/convert/fit_patch.py`` (plans/convert.md I14b).

Save as .fit rewrites only the fields an edit touched inside the opened
file's bytes. Pinned here against the anonymised fixtures of I2
(``tests/data/garmin_fit``, skipped while absent), the synthetic FIT of
``test_convert_fit`` and, read-only, the owner's three Descent X50i dives
in ``test_data/fit`` (gitignored; skipped when absent). Each patched file
is read back with `fit_reader` (the edited values, everything else equal),
its CRC checked, decoded by ``fitdecode`` and - when
``garmin_fit_sdk`` is installed - by Garmin's own decoder, and diffed byte
by byte against the original: every byte outside the patched spans, found
by an independent walk here, is identical.
"""
import glob
import re
import os
import struct
import zipfile
from datetime import datetime, timedelta

import fitdecode
import pytest

from src.core.convert import edit
from src.core.convert import fit_patch as module
from src.core.convert.fit_patch import FitPatchError, PatchReport, fit_crc, patch_fit, recompute_crcs
from src.core.convert.fit_reader import read_fit
from src.core.models import UnifiedDive
from tests.test_convert_fit import (ACTIVITY, DIVE_GAS, DIVE_SUMMARY, FILE_ID, SESSION, T0, _activity, _dive_gas,
                                    _dive_summary, _file_id, _gas_switch, _record, _session, build_fit,
                                    minimal_dive)

DATA = os.path.join(os.path.dirname(__file__), "data", "garmin_fit")
TWO_TANKS = os.path.join(DATA, "two_tanks.fit")
SINGLE_GAS = os.path.join(DATA, "single_gas.fit")
# The owner's own split dive: the watch's logs are named <n>_<date>_<time>_<activity id>.fit;
# anything else in the folder (a merged or edited file saved there while trying the app) is not one of them.
OWNER_FITS = sorted(
    p for p in glob.glob(os.path.join(os.path.dirname(os.path.dirname(__file__)), "test_data", "fit", "*.fit"))
    if re.match(r"\d+_\d{4}-\d{2}-\d{2}_\d{6}_\d+\.fit$", os.path.basename(p))
)
fixtures = pytest.mark.skipif(not os.path.isfile(TWO_TANKS), reason="Garmin FIT fixtures not present")

try:
    from garmin_fit_sdk import Decoder, Stream   # shipped since I12; imported by shipped code only in fit_writer.py
except ImportError:  # pragma: no cover
    Decoder = Stream = None


# --- helpers -------------------------------------------------------------------

def _walk_spans(data):
    """An independent walk: the byte spans of (message number, field number)
    for every data record and the CRC positions, so a test can say where a
    patch is allowed to differ without trusting the patcher's own report."""
    spans = {}
    pos, crcs = 0, []
    while pos < len(data):
        header_size = data[pos]
        end = pos + header_size + struct.unpack_from("<I", data, pos + 4)[0]
        if header_size == 14:
            crcs.append((pos + 12, 2))
        crcs.append((end, 2))
        definitions = {}
        p = pos + header_size
        while p < end:
            header = data[p]
            if header & 0x80:
                local, compressed = (header >> 5) & 0x03, True
            elif header & 0x40:
                local = header & 0x0F
                endian = ">" if data[p + 2] == 1 else "<"
                mesg = struct.unpack_from(endian + "H", data, p + 3)[0]
                count = data[p + 5]
                fields = [(data[p + 6 + 3 * i], data[p + 7 + 3 * i]) for i in range(count)]
                q = p + 6 + 3 * count
                dev = 0
                if header & 0x20:
                    n = data[q]
                    dev = sum(data[q + 2 + 3 * i] for i in range(n))
                    q += 1 + 3 * n
                definitions[local] = (mesg, fields, dev)
                p = q
                continue
            else:
                local, compressed = header & 0x0F, False
            mesg, fields, dev = definitions[local]
            offset = p + 1
            for num, size in fields:
                spans.setdefault((mesg, num), []).append((offset, size))
                offset += size
            if compressed:
                spans.setdefault(("header", mesg), []).append((p, 1))
            p = offset + dev
        pos = end + 2
    return spans, crcs


def _allowed(data, keys):
    """The byte offsets a patch of the fields ``keys`` may change."""
    spans, crcs = _walk_spans(data)
    offsets = set()
    for key in keys:
        for start, size in spans.get(key, []):
            offsets.update(range(start, start + size))
    for start, size in crcs:
        offsets.update(range(start, start + size))
    return offsets


def _assert_identical_outside(original, patched, keys):
    assert len(original) == len(patched)
    allowed = _allowed(original, keys)
    changed = {i for i, (a, b) in enumerate(zip(original, patched)) if a != b}
    assert changed and changed <= allowed, sorted(changed - allowed)[:10]


def _assert_well_formed(path):
    """A valid CRC, decodable by fitdecode (CRC checked) and by Garmin's SDK when installed."""
    data = open(path, "rb").read()
    assert struct.unpack_from("<H", data, len(data) - 2)[0] == fit_crc(data[:-2])
    with fitdecode.FitReader(path, check_crc=fitdecode.CrcCheck.RAISE, error_handling=fitdecode.ErrorHandling.RAISE) as reader:
        frames = sum(1 for _ in reader)
    assert frames > 0
    if Decoder is not None:
        messages, errors = Decoder(Stream.from_file(path)).read()
        assert errors == [] and messages
        return messages
    return None


def _same_but(a, b, *fields):
    """``b`` equals ``a`` in every field but ``fields``."""
    da, db = a.model_dump(), b.model_dump()
    for name in fields:
        da.pop(name, None)
        db.pop(name, None)
    assert da == db, {k: (da[k], db[k]) for k in da if da[k] != db[k]}


def _tanks(dive, index, **changes):
    rows = edit.tank_rows(dive)
    rows[index] = dict(rows[index], **changes)
    return rows


# --- the walker and the CRC ------------------------------------------------------

def test_fit_crc_and_recompute_crcs_match_the_files_own():
    data = bytearray(minimal_dive())
    assert fit_crc(bytes(data[:-2])) == struct.unpack_from("<H", data, len(data) - 2)[0]
    assert fit_crc(bytes(data[:12])) == struct.unpack_from("<H", data, 12)[0]
    data[-2:] = b"\0\0"
    data[12:14] = b"\0\0"           # a zero header CRC means "not computed" and is left so
    spans = recompute_crcs(data)
    assert spans == [(len(data) - 2, 2)] and bytes(data[-2:]) == struct.pack("<H", fit_crc(bytes(data[:-2])))
    assert bytes(data[12:14]) == b"\0\0"


@pytest.mark.parametrize("data", [b"", b"\x0e" + b"\0" * 20, b"\x0e\x10\x5c\x08\xff\xff\xff\xff.FIT\0\0"])
def test_patch_refuses_what_is_not_a_fit(data, tmp_path):
    dive = UnifiedDive(date_time=datetime(2024, 11, 8, 12, 33, 20), duration=600, max_depth=18.5)
    with pytest.raises(FitPatchError):
        patch_fit(data, tmp_path / "x.fit", dive, dive)
    assert not (tmp_path / "x.fit").exists()


# --- the synthetic dive ------------------------------------------------------------

def test_synthetic_number_gf_and_position_patch(tmp_path):
    """Dive number and max depth in dive_summary, a position into the
    session; a field the file has no slot for (GF: no dive_settings) and the
    texts no FIT holds are reported, not written."""
    source = minimal_dive(start_lat=100_000_000)
    original = read_fit(source)
    edited = edit.apply_edits(original, {"dive_number": 77, "lat": "-33.5", "gf_low": "30", "location": "Jetty",
                                         "notes": "x", "weight": "4", "visibility": "12", "buddy": "Kim"})
    out = tmp_path / "patched.fit"
    report = patch_fit(source, out, original, edited)
    assert isinstance(report, PatchReport) and report.changed and report.shifted_seconds == 0
    assert report.patched == ["dive number", "latitude"]
    assert report.not_carried == ["site", "buddy", "notes", "weight", "visibility", "GF low (the file has no field for it)"]
    back = read_fit(str(out))
    assert back.dive_number == 77 and back.lat == pytest.approx(-33.5, abs=1e-7) and back.gf_low is None
    _same_but(original, back, "dive_number", "lat")
    _assert_identical_outside(source, out.read_bytes(), [(DIVE_SUMMARY, 10), (SESSION, 3)])
    _assert_well_formed(out)
    # a dry run (dest None) makes the same report and writes nothing
    assert patch_fit(source, None, original, edited).not_carried == report.not_carried
    assert sorted(os.listdir(tmp_path)) == ["patched.fit"]


def test_synthetic_shift_moves_every_timestamp_and_the_local_clock(tmp_path):
    """A one-hour later start plus a zone change: every timestamp (file_id,
    session start and end, activity, records, events, dive_summary) moves by
    the UTC delta and activity.local_timestamp follows the new offset."""
    source = minimal_dive()
    original = read_fit(source)
    assert original.timezone == "UTC+01:00" and original.date_time == original.date_time_utc + timedelta(hours=1)
    new_local = original.date_time + timedelta(hours=1)
    edited = edit.apply_edits(original, {"time": new_local.strftime("%H:%M:%S"), "utc_offset": "+03:00"})
    assert edited.date_time_utc == original.date_time_utc - timedelta(hours=1)
    out = tmp_path / "shifted.fit"
    report = patch_fit(source, out, original, edited)
    assert report.shifted_seconds == -3600 and report.patched == ["start time (-3600 s on every timestamp)", "time zone"]
    assert report.not_carried == []
    back = read_fit(str(out))
    assert back.date_time == new_local and back.date_time_utc == edited.date_time_utc and back.timezone == "UTC+03:00"
    assert [s.time for s in back.samples] == [s.time for s in original.samples] and back.duration == original.duration
    assert back.events == original.events
    _same_but(original, back, "date_time", "date_time_utc", "timezone")
    _assert_identical_outside(source, out.read_bytes(), [(FILE_ID, 4), (SESSION, 253), (SESSION, 2), (20, 253), (21, 253),
                                                         (ACTIVITY, 253), (ACTIVITY, 5), (DIVE_SUMMARY, 253)])
    _assert_well_formed(out)
    # the raw activity timestamps say the same
    with fitdecode.FitReader(str(out)) as reader:
        activity = next(f for f in reader if f.frame_type == fitdecode.FIT_FRAME_DATA and f.name == "activity")
    assert activity.get_value("timestamp", raw_value=True) == T0 - 3600
    assert activity.get_value("local_timestamp", raw_value=True) == T0 - 3600 + 3 * 3600


def test_synthetic_zone_change_alone_moves_the_utc_instant(tmp_path):
    """The same wall clock in another zone: the timestamps shift by the
    zone difference and the local clock stays what it was."""
    source = minimal_dive()
    original = read_fit(source)
    edited = edit.apply_edits(original, {"utc_offset": "-02:00"})
    out = tmp_path / "zone.fit"
    report = patch_fit(source, out, original, edited)
    assert report.shifted_seconds == 3 * 3600 and "time zone" in report.patched
    back = read_fit(str(out))
    assert back.date_time == original.date_time and back.date_time_utc == original.date_time_utc + timedelta(hours=3)
    assert back.timezone == "UTC-02:00"
    _same_but(original, back, "date_time_utc", "timezone")


def test_synthetic_half_hour_zone_is_patched_and_read_back(tmp_path):
    """A half-hour zone (India, +05:30) typed on the page: the activity's
    local timestamp moves by the exact 4.5 h against the file's +01:00,
    and the patched file reads back with the zone kept as UTC+05:30 (R7)."""
    source = minimal_dive()
    original = read_fit(source)
    edited = edit.apply_edits(original, {"utc_offset": "+05:30"})
    assert edited.timezone == "UTC+05:30" and edited.date_time_utc == original.date_time_utc - timedelta(hours=4, minutes=30)
    out = tmp_path / "india.fit"
    report = patch_fit(source, out, original, edited)
    assert report.shifted_seconds == -(4 * 3600 + 1800) and "time zone" in report.patched
    back = read_fit(str(out))
    assert (back.date_time, back.date_time_utc, back.timezone) == (original.date_time, edited.date_time_utc, "UTC+05:30")
    _same_but(original, back, "date_time_utc", "timezone")
    with fitdecode.FitReader(str(out)) as reader:
        activity = next(f for f in reader if f.frame_type == fitdecode.FIT_FRAME_DATA and f.name == "activity")
    assert activity.get_value("local_timestamp", raw_value=True) - activity.get_value("timestamp", raw_value=True) == 5 * 3600 + 1800


def test_synthetic_gas_change_and_a_tank_the_file_has_no_record_for(tmp_path):
    """O2/He into the dive_gas record; a second tank added on the page has
    no record and is reported; a tank name or role is never in a FIT."""
    source = minimal_dive()
    original = read_fit(source)
    assert len(original.gas_mixtures) == 1 and original.gas_mixtures[0].oxygen == 32.0
    rows = _tanks(original, 0, oxygen="36", helium="10", tank_name="Deco", tank_role="deco")
    rows.append({"tank_name": "Stage", "oxygen": "50", "helium": "0", "tank_volume": "", "start_pressure": "", "end_pressure": "", "tank_role": ""})
    edited = edit.apply_edits(original, {"tanks": rows})
    out = tmp_path / "gas.fit"
    report = patch_fit(source, out, original, edited)
    assert report.patched == ["tank 1 O2", "tank 1 He"]
    assert report.not_carried == ["tank 1 name", "tank 1 role", "tank 2 (added on the page: the file has no record for it)"]
    back = read_fit(str(out))
    assert back.gas_mixtures[0].oxygen == 36.0 and back.gas_mixtures[0].helium == 10.0 and len(back.gas_mixtures) == 1
    assert back.events[0].oxygen == 36.0                                 # the gas switch names the gas it switched to
    _same_but(original, back, "gas_mixtures", "events")
    _assert_identical_outside(source, out.read_bytes(), [(DIVE_GAS, 0), (DIVE_GAS, 1)])
    _assert_well_formed(out)
    # pressures with no transmitter record: reported, nothing else written
    edited = edit.apply_edits(original, {"tanks": _tanks(original, 0, start_pressure="200")})
    report = patch_fit(source, tmp_path / "p.fit", original, edited)
    assert report.patched == [] and report.not_carried == ["tank 1 start pressure (the file has no transmitter record for this tank)"]
    assert (tmp_path / "p.fit").read_bytes() == source


def test_synthetic_profile_temperatures_and_cleared_values(tmp_path):
    """Min and max water temperature of a dive with a profile come from the
    samples and are reported; the average goes into the session when it
    has the field (the synthetic one has none); clearing a value writes the
    field's own unset mark, which the reader gives back as None."""
    source = minimal_dive()
    original = read_fit(source)
    edited = edit.apply_edits(original, {"temp_min": "20", "temp_max": "25", "temp_avg": "22", "dive_number": ""})
    report = patch_fit(source, tmp_path / "t.fit", original, edited)
    assert report.patched == ["dive number"]
    assert report.not_carried == ["water temp avg (the file has no field for it)", "water temp max (the profile samples give it)",
                                  "water temp min (the profile samples give it)"]
    back = read_fit(str(tmp_path / "t.fit"))
    assert back.dive_number is None
    _same_but(original, back, "dive_number")


def test_synthetic_removed_tank_and_unknown_words(tmp_path):
    source = build_fit([_file_id(), _session(), _activity(), _record(0, 1.0), _record(60, 10.0),
                        _dive_gas(0, 32), _dive_gas(1, 50), _gas_switch(0, 0), _dive_summary()])
    original = read_fit(source)
    assert len(original.gas_mixtures) == 2
    edited = original.model_copy(deep=True)
    edited.gas_mixtures = edited.gas_mixtures[:1]
    edited.water_type = "brackish"      # not a FIT water type; the editor's own list would refuse it anyway
    report = patch_fit(source, None, original, edited)
    assert report.not_carried == ["water type ('brackish' is not a value a FIT knows)", "tank 2 removed (a FIT keeps its transmitter records)"]


def test_a_zip_of_several_fits_patches_the_right_member(tmp_path):
    a = minimal_dive()
    b = build_fit([_file_id(), _session(timer=900.0), _activity(), _record(0, 1.0), _record(60, 12.0),
                   _dive_gas(0, 21), _dive_summary(number=13)])
    archive = tmp_path / "two.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.fit", a)
        zf.writestr("b.fit", b)
    original = read_fit(b, name="b.fit")
    edited = edit.apply_edits(original, {"dive_number": "14"})
    out = tmp_path / "b (edited).fit"
    report = patch_fit(str(archive), out, original, edited)
    assert report.source_member == "b.fit" and report.patched == ["dive number"]
    back = read_fit(str(out))
    assert back.dive_number == 14 and back.duration == 900
    assert out.read_bytes()[:14] == b[:14]           # a plain .fit comes out, not a zip
    with pytest.raises(FitPatchError, match="holds no .fit"):
        empty = tmp_path / "empty.zip"
        with zipfile.ZipFile(empty, "w") as zf:
            zf.writestr("readme.txt", "x")
        patch_fit(str(empty), None, original, edited)


# --- the fixtures --------------------------------------------------------------------

@fixtures
def test_two_tanks_number_gf_and_end_pressure(tmp_path):
    """(a) A max-depth-free edit: dive number, both gradient factors and
    the first tank's end pressure; every other byte identical."""
    source = TWO_TANKS
    data = open(source, "rb").read()
    original = read_fit(source)
    edited = edit.apply_edits(original, {"dive_number": "39", "gf_low": "35", "gf_high": "80",
                                         "tanks": _tanks(original, 0, end_pressure="160")})
    out = tmp_path / "two_tanks (edited).fit"
    report = patch_fit(source, out, original, edited)
    assert report.patched == ["dive number", "GF low", "GF high", "tank 1 end pressure"] and report.not_carried == []
    assert report.shifted_seconds == 0
    back = read_fit(str(out))
    assert back.dive_number == 39 and back.gf_low == 35 and back.gf_high == 80
    assert back.gas_mixtures[0].end_pressure == pytest.approx(160.0) and back.gas_mixtures[1] == original.gas_mixtures[1]
    assert back.samples == original.samples and back.events == original.events
    _same_but(original, back, "dive_number", "gf_low", "gf_high", "gas_mixtures")
    _assert_identical_outside(data, out.read_bytes(), [(268, 10), (258, 2), (258, 3), (323, 2)])
    messages = _assert_well_formed(out)
    if messages is not None:
        assert messages["dive_summary_mesgs"][0]["dive_number"] == 39
        assert messages["dive_settings_mesgs"][0]["gf_low"] == 35
    assert open(source, "rb").read() == data                          # the original is untouched


@fixtures
def test_two_tanks_start_shift_and_zone(tmp_path):
    """(b) One hour later in the next zone east: the UTC instant is the same
    and every timestamp with it... no: +1 h wall clock and +1 h zone cancel
    out, so the timestamps stay and only the local clock moves; a plain
    hour later in the same zone shifts every timestamp by 3600 s."""
    source = TWO_TANKS
    data = open(source, "rb").read()
    original = read_fit(source)
    later = original.date_time + timedelta(hours=1)
    # the same instant, named in the next zone
    edited = edit.apply_edits(original, {"time": later.strftime("%H:%M:%S"), "utc_offset": "+08:00"})
    out = tmp_path / "zone.fit"
    report = patch_fit(source, out, original, edited)
    assert report.shifted_seconds == 0 and report.patched == ["time zone"]
    back = read_fit(str(out))
    assert back.date_time == later and back.date_time_utc == original.date_time_utc and back.timezone == "UTC+08:00"
    _same_but(original, back, "date_time", "timezone")
    _assert_identical_outside(data, out.read_bytes(), [(34, 5)])
    # an hour later in the same zone
    edited = edit.apply_edits(original, {"time": later.strftime("%H:%M:%S")})
    out = tmp_path / "shift.fit"
    report = patch_fit(source, out, original, edited)
    assert report.shifted_seconds == 3600 and report.patched == ["start time (+3600 s on every timestamp)"]
    back = read_fit(str(out))
    assert back.date_time == later and back.date_time_utc == original.date_time_utc + timedelta(hours=1)
    assert back.timezone == original.timezone == "UTC+07:00"
    assert [s.time for s in back.samples] == [s.time for s in original.samples] and back.events == original.events
    _same_but(original, back, "date_time", "date_time_utc")
    spans, _crcs = _walk_spans(data)
    timestamp_keys = [key for key in spans if key[1] == 253] + [(0, 4), (18, 2), (19, 2), (34, 5)]
    _assert_identical_outside(data, out.read_bytes(), timestamp_keys)
    messages = _assert_well_formed(out)
    if messages is not None:
        assert messages["session_mesgs"][0]["start_time"].replace(tzinfo=None) == datetime(2026, 8, 29, 3, 56, 11)


@fixtures
def test_two_tanks_gas_change(tmp_path):
    """(c) O2 of the one gas record: both tanks show it (the reader pairs the
    extra transmitter with the first gas) and so does the gas switch event."""
    source = TWO_TANKS
    data = open(source, "rb").read()
    original = read_fit(source)
    edited = edit.apply_edits(original, {"tanks": _tanks(original, 0, oxygen="32")})
    out = tmp_path / "gas.fit"
    report = patch_fit(source, out, original, edited)
    assert report.patched == ["tank 1 O2"] and report.not_carried == []
    back = read_fit(str(out))
    assert [g.oxygen for g in back.gas_mixtures] == [32.0, 32.0]
    _same_but(original, back, "gas_mixtures", "events")
    assert [e.model_dump(exclude={"oxygen"}) for e in back.events] == [e.model_dump(exclude={"oxygen"}) for e in original.events]
    _assert_identical_outside(data, out.read_bytes(), [(259, 1)])
    _assert_well_formed(out)
    # the second tank's O2 cannot be told apart from the first's in this file
    edited = edit.apply_edits(original, {"tanks": _tanks(original, 1, oxygen="36")})
    report = patch_fit(source, None, original, edited)
    assert report.not_carried == ["tank 2 O2 (the file has 1 gas record; this tank shows the first gas)"]


@fixtures
def test_two_tanks_volume_position_water_and_mode(tmp_path):
    """The transmitter profile's volume (tenths of a litre), exit position,
    water type and density, surface interval, CNS, dive mode."""
    source = TWO_TANKS
    original = read_fit(source)
    assert original.gas_mixtures[1].tank_volume is not None
    edited = edit.apply_edits(original, {"exit_lat": "5.5", "exit_lng": "100.25", "water_type": "fresh", "water_density": "1000",
                                         "surface_interval": "3600", "cns_start": "3", "cns_end": "12", "dive_mode": "oc_multi_gas",
                                         "bottom_time": "1500", "temp_avg": "27",
                                         "tanks": _tanks(original, 1, tank_volume="12")})
    out = tmp_path / "more.fit"
    report = patch_fit(source, out, original, edited)
    assert report.not_carried == []
    assert set(report.patched) == {"exit latitude", "exit longitude", "water type", "water density", "surface interval", "CNS start",
                                   "CNS end", "dive mode", "bottom time", "water temp avg", "tank 2 volume"}
    back = read_fit(str(out))
    assert back.exit_lat == pytest.approx(5.5, abs=1e-7) and back.exit_lng == pytest.approx(100.25, abs=1e-7)
    assert back.water_type == "fresh" and back.water_density == pytest.approx(1000.0) and back.surface_interval == 3600
    assert back.cns_start == 3.0 and back.cns_end == 12.0 and back.dive_mode == "oc_multi_gas" and back.bottom_time == 1500
    assert back.temp_avg == 27.0 and back.gas_mixtures[1].tank_volume == 12.0 and back.gas_mixtures[0].tank_volume == original.gas_mixtures[0].tank_volume
    _same_but(original, back, "exit_lat", "exit_lng", "water_type", "water_density", "surface_interval", "cns_start", "cns_end",
              "dive_mode", "bottom_time", "temp_avg", "gas_mixtures")
    _assert_well_formed(out)


@pytest.mark.skipif(not os.path.isfile(SINGLE_GAS), reason="Garmin FIT fixtures not present")
def test_single_gas_unchanged_dive_copies_the_file(tmp_path):
    """No edit: the copy is the file, byte for byte (the CRC recomputes to itself)."""
    original = read_fit(SINGLE_GAS)
    out = tmp_path / "same.fit"
    report = patch_fit(SINGLE_GAS, out, original, original.model_copy(deep=True))
    assert not report.changed and report.not_carried == [] and report.patched == []
    assert out.read_bytes() == open(SINGLE_GAS, "rb").read()


@pytest.mark.skipif(not OWNER_FITS, reason="the owner's test_data/fit dives are not present")
@pytest.mark.parametrize("path", OWNER_FITS, ids=[os.path.basename(p) for p in OWNER_FITS])
def test_owner_dives_round_trip_read_only(path, tmp_path):
    """One read-only round trip per Descent X50i dive: a number, GF, end
    pressure and start-shift patch into a tmp path reads back with the
    edits and nothing else changed; the file on disk is never touched."""
    data = open(path, "rb").read()
    original = read_fit(path)
    later = original.date_time + timedelta(minutes=30)
    edited = edit.apply_edits(original, {"dive_number": str((original.dive_number or 0) + 1000), "gf_low": "33",
                                         "time": later.strftime("%H:%M:%S"),
                                         "tanks": _tanks(original, 0, end_pressure="77")})
    out = tmp_path / (os.path.splitext(os.path.basename(path))[0] + " (edited).fit")
    report = patch_fit(path, out, original, edited)
    assert report.shifted_seconds == 1800 and report.not_carried == []
    assert set(report.patched) == {"start time (+1800 s on every timestamp)", "dive number", "GF low", "tank 1 end pressure"}
    back = read_fit(str(out), name=os.path.basename(path))           # the cache name gives the activity id
    assert back.dive_number == (original.dive_number or 0) + 1000 and back.gf_low == 33 and back.date_time == later
    assert back.gas_mixtures[0].end_pressure == pytest.approx(77.0)
    assert [s.time for s in back.samples] == [s.time for s in original.samples] and back.events == original.events
    _same_but(original, back, "dive_number", "gf_low", "date_time", "date_time_utc", "gas_mixtures")
    spans, _crcs = _walk_spans(data)
    keys = [key for key in spans if key[1] == 253] + [(0, 4), (18, 2), (19, 2), (34, 5), (268, 10), (258, 2), (323, 2)]
    _assert_identical_outside(data, out.read_bytes(), keys)
    _assert_well_formed(out)
    assert open(path, "rb").read() == data


def test_message_and_field_numbers_are_the_profiles():
    """The numbers the patcher writes to are fitdecode's profile's (so a
    profile update cannot silently move a field)."""
    from fitdecode import profile
    for attr, (_label, slots) in module.FIELD_SLOTS.items():
        for slot in slots:
            message = profile.MESSAGE_TYPES[slot.mesg]
            assert slot.num in message.fields, (attr, slot)
            field = message.fields[slot.num]
            if slot.scale != 1.0:
                assert field.scale == slot.scale, (attr, field.name)
    assert profile.MESSAGE_TYPES[268].name == "dive_summary" and profile.MESSAGE_TYPES[323].name == "tank_summary"
    assert profile.MESSAGE_TYPES[259].fields[1].name == "oxygen_content" and profile.MESSAGE_TYPES[259].fields[0].name == "helium_content"
    assert profile.MESSAGE_TYPES[323].fields[1].name == "start_pressure" and profile.MESSAGE_TYPES[323].fields[2].name == "end_pressure"
    assert profile.MESSAGE_TYPES[34].fields[5].name == "local_timestamp"
    assert module.WATER_TYPES == {name: value for value, name in profile.FIELD_TYPES["water_type"].enum.items()}


def test_synthetic_sac_patch(tmp_path):
    """R9: an edited SAC is written into dive_summary.avg_volume_sac (L/min
    in hundredths) when the file has the field; a file without it reports
    the SAC as not carried. The pressure SAC and RMV stay as logged."""
    with_sac = build_fit([_file_id(), _session(), _activity(), _record(0, 0.5, 24), _record(60, 18.0, 22),
                          _record(120, 1.0, 23), _dive_gas(0, 32), _gas_switch(0, 0),
                          _dive_summary(sac=11.2, rmv=22.5, pressure_sac=1.01)])
    original = read_fit(with_sac)
    assert original.sac == 11.2 and original.rmv == 22.5
    edited = edit.apply_edits(original, {"sac": "14.26"})
    out = tmp_path / "sac.fit"
    report = patch_fit(with_sac, out, original, edited)
    assert report.patched == ["SAC"] and report.not_carried == []
    back = read_fit(str(out))
    assert back.sac == pytest.approx(14.26) and back.rmv == 22.5
    _same_but(original, back, "sac")
    _assert_identical_outside(with_sac, out.read_bytes(), [(DIVE_SUMMARY, 13)])
    _assert_well_formed(out)
    # cleared: the field is set to invalid and reads back as none
    cleared = patch_fit(with_sac, tmp_path / "none.fit", original, edit.apply_edits(original, {"sac": ""}))
    assert cleared.patched == ["SAC"] and read_fit(str(tmp_path / "none.fit")).sac is None
    # a file whose summary has no SAC field
    source = minimal_dive()
    original = read_fit(source)
    report = patch_fit(source, tmp_path / "x.fit", original, edit.apply_edits(original, {"sac": "14"}))
    assert report.patched == [] and report.not_carried == ["SAC (the file has no field for it)"]


def test_air_temperature_and_suit_edits_are_reported_not_written(tmp_path):
    """R10: an edited air temperature or suit is named as not carried; a
    FIT has no field for either."""
    source = minimal_dive()
    original = read_fit(source)
    edited = edit.apply_edits(original, {"air_temp": "28.5", "suit": "drysuit"})
    report = patch_fit(source, None, original, edited)
    assert report.patched == [] and report.not_carried == ["air temperature", "suit"]
