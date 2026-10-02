"""The Convert page's format registry ``src/core/convert/formats.py``
(plans/convert.md I4): detection, the read and write entry points, the
file-dialog filters and the per-dive output file names.

Detection and the FIT round trips run on the anonymised FIT fixtures of I2
(skipped while they are not on disk); the UDDF cases use the hand-written
export of ``test_uddf``; the Subsurface XML and the zips are built here.
"""
import io
import os
import zipfile
from datetime import datetime

import pytest

from src.core.convert import formats
from src.core.convert.fit_reader import FitReadError, NotADiveError
from src.core.convert.formats import (
    FORMATS,
    FileFormat,
    ReadResult,
    UnsupportedFileError,
    detect_format,
    get_format,
    open_name_filters,
    output_file_name,
    output_paths,
    read_file,
    read_files,
    safe_file_name,
    save_name_filters,
    write_each,
    write_file,
)
from src.core.models import DiveEvent, GasMixture, SampleChannels, UnifiedDive, UnifiedSample
from src.core.services.uddf import read_uddf
from tests.test_convert_fit import build_fit, minimal_dive, _file_id, _record, _session
from tests.test_uddf import SAMPLE as UDDF_SAMPLE

DATA = os.path.join(os.path.dirname(__file__), "data", "garmin_fit")
SINGLE_GAS = os.path.join(DATA, "single_gas.fit")
TWO_TANKS = os.path.join(DATA, "two_tanks.fit")
fixtures = pytest.mark.skipif(not os.path.isfile(TWO_TANKS), reason="Garmin FIT fixtures not present")

SSRF_SAMPLE = """<?xml version="1.0"?>
<divelog program="subsurface" version="3">
  <settings><divecomputerid model="Descent" serial="1" /></settings>
  <dives><dive number="1" date="2026-06-22" time="09:30:00" duration="52:00 min"></dive></dives>
</divelog>
"""


def _zip_of(entries, path):
    """A zip at ``path`` holding ``entries`` (name -> bytes)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    path.write_bytes(buffer.getvalue())
    return str(path)


def _dive(when=datetime(2026, 9, 30, 14, 32, 5), number=452, **kwargs):
    base = dict(date_time=when, duration=2500, max_depth=18.4, dive_number=number)
    base.update(kwargs)
    return UnifiedDive(**base)


# --- the registry ---------------------------------------------------------------

def test_registry_entries():
    ids = [f.id for f in FORMATS]
    assert ids == ["fit", "uddf", "ssrf"]
    fit, uddf, ssrf = (get_format(i) for i in ids)
    assert (fit.can_read, fit.can_write, fit.extension) == (True, False, ".fit")
    assert (uddf.can_read, uddf.can_write, uddf.extension) == (True, True, ".uddf")
    assert (ssrf.can_read, ssrf.can_write, ssrf.extension) == (True, True, ".ssrf")
    assert [f.id for f in formats.readable_formats()] == ["fit", "uddf", "ssrf"]
    assert [f.id for f in formats.writable_formats()] == ["uddf", "ssrf"]
    for f in FORMATS:
        assert all(ext.startswith(".") and ext == ext.lower() for ext in f.extensions)
        assert set(f.ambiguous) <= set(f.extensions) and f.extension not in f.ambiguous
    with pytest.raises(UnsupportedFileError, match="unknown file format 'csv'"):
        get_format("csv")


def test_name_filters():
    assert open_name_filters() == ["Dive files (*.fit *.zip *.uddf *.xml *.ssrf)", "Garmin FIT (*.fit *.zip)",
                                   "UDDF (*.uddf *.xml)", "Subsurface (*.ssrf *.xml)", "All files (*)"]
    assert save_name_filters() == ["UDDF (*.uddf)", "Subsurface (*.ssrf)"]
    assert save_name_filters("uddf") == ["UDDF (*.uddf)"]
    assert FileFormat("x", "Example", (".a", ".b"), True, True).name_filter == "Example (*.a *.b)"


# --- detection ------------------------------------------------------------------

@fixtures
def test_detect_fit_by_extension_and_by_content(tmp_path):
    assert detect_format(SINGLE_GAS).id == "fit"
    unnamed = tmp_path / "activity"           # no extension: the header's ".FIT" decides
    unnamed.write_bytes(open(SINGLE_GAS, "rb").read())
    assert detect_format(unnamed).id == "fit"
    assert detect_format(str(tmp_path / "activity")).id == "fit"


def test_detect_synthetic_fit_and_zip(tmp_path):
    raw = minimal_dive()
    bare = tmp_path / "dive.FIT"               # upper-case extension
    bare.write_bytes(raw)
    assert detect_format(bare).id == "fit"
    connect = _zip_of({"24449823352_ACTIVITY.fit": raw}, tmp_path / "export.zip")
    assert detect_format(connect).id == "fit"
    renamed = _zip_of({"a.fit": raw}, tmp_path / "export.bin")   # zip content under any extension
    assert detect_format(renamed).id == "fit"


def test_detect_uddf_and_subsurface_xml(tmp_path):
    uddf = tmp_path / "log.uddf"
    uddf.write_text(UDDF_SAMPLE, encoding="utf-8")
    assert detect_format(uddf).id == "uddf"
    as_xml = tmp_path / "log.xml"              # ambiguous extension: the root element decides
    as_xml.write_text(UDDF_SAMPLE, encoding="utf-8")
    assert detect_format(as_xml).id == "uddf"
    ssrf = tmp_path / "log.ssrf"
    ssrf.write_text(SSRF_SAMPLE, encoding="utf-8")
    assert detect_format(ssrf).id == "ssrf"
    ssrf_xml = tmp_path / "subsurface.xml"
    ssrf_xml.write_text(SSRF_SAMPLE, encoding="utf-8")
    assert detect_format(ssrf_xml).id == "ssrf"
    no_ext = tmp_path / "divelog"
    no_ext.write_text(SSRF_SAMPLE, encoding="utf-8")
    assert detect_format(no_ext).id == "ssrf"


@pytest.mark.parametrize("name, content, words", [
    ("notes.txt", b"just some text", "not a Garmin FIT, UDDF or Subsurface file"),
    ("other.xml", b"<?xml version='1.0'?><gpx version='1.1'></gpx>", "not a Garmin FIT"),
    ("empty.zip", b"", "the zip holds no .fit file"),
    ("photo.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 20, "not a Garmin FIT"),
])
def test_unknown_files_are_rejected(tmp_path, name, content, words):
    path = tmp_path / name
    path.write_bytes(content)
    with pytest.raises(UnsupportedFileError, match=words) as info:
        detect_format(path)
    assert name in str(info.value)


def test_broken_uddf_is_detected_by_its_root_and_fails_on_read(tmp_path):
    """Detection reads up to the first start tag; the parse error is read_file's."""
    import xml.etree.ElementTree as ET
    path = tmp_path / "broken.xml"
    path.write_bytes(b"<uddf><broken")
    assert detect_format(path).id == "uddf"
    with pytest.raises(ET.ParseError):
        read_file(path)


def test_zip_without_fit_is_rejected_and_missing_file_raises(tmp_path):
    path = _zip_of({"readme.txt": b"hello"}, tmp_path / "nothing.zip")
    with pytest.raises(UnsupportedFileError, match="holds no .fit"):
        detect_format(path)
    with pytest.raises(FileNotFoundError):
        detect_format(tmp_path / "missing.fit")


# --- reading --------------------------------------------------------------------

@fixtures
def test_read_fit_fixture():
    result = read_file(SINGLE_GAS)
    assert isinstance(result, ReadResult)
    assert [d.dive_number for d in result.dives] == [28] and result.warnings == []
    assert result.files == [(SINGLE_GAS, "fit")] and result.format_id == "fit"


@fixtures
def test_read_connect_zip_and_zip_of_several(tmp_path):
    """Connect's export holds one FIT: read like the bare file, the zip's
    name giving the activity id. A zip of several gives every dive, with
    the entries that are not dives as warnings."""
    single, two = open(SINGLE_GAS, "rb").read(), open(TWO_TANKS, "rb").read()
    connect = _zip_of({"24449823352_ACTIVITY.fit": single}, tmp_path / "28_2026-06-27_1124_24449823352.zip")
    result = read_file(connect)
    assert len(result.dives) == 1 and result.dives[0].external_ids == {"garmin": "24449823352"}
    assert result.files == [(connect, "fit")]
    running = build_fit([_file_id(), _session(sport=1, sub_sport=0), _record(0, 1.0)])
    many = _zip_of({"b_two.fit": two, "a_single.fit": single, "c_run.fit": running, "notes.txt": b"x"},
                   tmp_path / "many.zip")
    result = read_file(many)
    assert [d.dive_number for d in result.dives] == [28, 38]    # entry name order
    assert all(d.external_ids == {} for d in result.dives)
    assert len(result.warnings) == 1 and "c_run.fit" in result.warnings[0] and "running" in result.warnings[0]


def test_zip_whose_fits_are_no_dives_is_rejected(tmp_path):
    running = build_fit([_file_id(), _session(sport=1, sub_sport=0), _record(0, 1.0)])
    path = _zip_of({"a.fit": running, "b.fit": b"garbage"}, tmp_path / "runs.zip")
    with pytest.raises(UnsupportedFileError, match="none of its 2 .fit files is a dive"):
        read_file(path)


def test_read_fit_errors_pass_through(tmp_path):
    bad = tmp_path / "bad.fit"
    bad.write_bytes(b"not a fit")
    with pytest.raises(FitReadError):
        read_file(bad)
    apnea = tmp_path / "apnea.fit"
    apnea.write_bytes(build_fit([_file_id(), _session(sub_sport=56), _record(0, 1.0)]))
    with pytest.raises(NotADiveError):
        read_file(apnea)


def test_read_uddf(tmp_path):
    path = tmp_path / "export.uddf"
    path.write_text(UDDF_SAMPLE, encoding="utf-8")
    result = read_file(path)
    assert [d.dive_number for d in result.dives] == [148, None] and result.format_id == "uddf"
    assert result.dives[0].date_time == datetime(2026, 6, 22, 9, 30) and result.warnings == []
    empty = tmp_path / "empty.uddf"
    empty.write_text('<uddf xmlns="http://www.streit.cc/uddf/3.2/" version="3.2.0"><profiledata/></uddf>')
    result = read_file(empty)
    assert result.dives == [] and result.warnings == ["empty.uddf: the file holds no dive"]


def test_read_ssrf(tmp_path):
    """The Subsurface reader of I6b behind the registry (its own tests are in test_convert_ssrf_reader)."""
    path = tmp_path / "log.ssrf"
    path.write_text(SSRF_SAMPLE, encoding="utf-8")
    result = read_file(path)
    assert [d.dive_number for d in result.dives] == [1] and result.format_id == "ssrf" and result.warnings == []
    assert result.dives[0].date_time == datetime(2026, 6, 22, 9, 30) and result.dives[0].duration == 3120
    empty = tmp_path / "empty.ssrf"
    empty.write_text("<divelog program='subsurface' version='3'><dives/></divelog>")
    result = read_file(empty)
    assert result.dives == [] and result.warnings == ["empty.ssrf: the file holds no dive"]


def test_read_files_merges_in_order_and_warns_per_failed_file(tmp_path):
    fit = tmp_path / "dive.fit"
    fit.write_bytes(minimal_dive())
    uddf = tmp_path / "log.uddf"
    uddf.write_text(UDDF_SAMPLE, encoding="utf-8")
    text = tmp_path / "notes.txt"
    text.write_text("x")
    result = read_files([uddf, fit, text, tmp_path / "missing.fit"])
    assert [d.dive_number for d in result.dives] == [148, None, 12]
    assert result.files == [(str(uddf), "uddf"), (str(fit), "fit")] and result.format_id is None
    assert len(result.warnings) == 2
    assert result.warnings[0].startswith("notes.txt: ") and result.warnings[1] == "missing.fit: file not found"
    # one path: the error is raised, as read_file does
    with pytest.raises(UnsupportedFileError):
        read_files([text])
    # several paths, none readable
    with pytest.raises(UnsupportedFileError, match="none of the files could be read"):
        read_files([text, tmp_path / "missing.fit"])
    assert read_files([]).dives == [] and read_files([]).format_id is None


# --- writing --------------------------------------------------------------------

def test_write_uddf_and_read_back(tmp_path):
    path = tmp_path / "out" / "dives.uddf"     # the parent folder is created
    dives = [_dive(), _dive(when=datetime(2026, 10, 1, 8, 0), number=453)]
    assert write_file(dives, "uddf", path) == []
    _root, again = read_uddf(str(path))
    assert [(d.dive_number, d.date_time, d.duration, d.max_depth) for d in again] == [
        (452, datetime(2026, 9, 30, 14, 32, 5), 2500, 18.4), (453, datetime(2026, 10, 1, 8, 0), 2500, 18.4)]


def test_write_uddf_says_what_it_drops(tmp_path):
    """Since I5 the UDDF writer carries the channels, events, computer and
    weight; the warnings name only what UDDF 3.2 has no slot for."""
    rich = _dive(events=[DiveEvent(time=0, type="gas_switch", tank=0), DiveEvent(time=5, type="tank_pod_connected", tank=0),
                         DiveEvent(time=9, type="tank_pod_disconnected", tank=0)],
                 computer_model="Descent", weight=4.0, water_type="salt", water_density=1025.0, exit_lat=1.0, exit_lng=2.0,
                 bottom_time=2000, cns_start=0.0, cns_end=3.0, dive_mode="gauge", gas_mixtures=[GasMixture()],
                 samples=[UnifiedSample(depth=1.0, time=0, channels=SampleChannels(pressures={0: 200.0}, ndl=99, tts=40))])
    warnings = write_file([rich], "uddf", tmp_path / "rich.uddf")
    assert warnings == [
        "UDDF: the time to surface is not written",
        "UDDF: the tank pod connected, tank pod disconnected events are not written",
        "UDDF: the water type, water density, exit position, bottom time, start and end CNS, gauge dive mode are not written",
    ]
    # the file holds what the writer carries since I5
    _root, back = read_uddf(str(tmp_path / "rich.uddf"))
    assert back[0].weight == 4.0 and back[0].computer_model == "Descent" and back[0].samples[0].channels.pressures == {0: 200.0}
    assert back[0].samples[0].channels.ndl == 99 and [e.type for e in back[0].events] == ["gas_switch"]
    # one missing field reads in the singular; a dive with a slot for everything gives no warning
    assert write_file([_dive(bottom_time=100)], "uddf", tmp_path / "one.uddf") == ["UDDF: the bottom time is not written"]
    plain = _dive(weight=2.0, computer_model="Descent", computer_serial="1", gf_low=40, gf_high=85, surface_interval=600, dive_mode="ccr",
                  events=[DiveEvent(time=0, type="gas_switch", tank=0), DiveEvent(time=10, type="alert", name="x"),
                          DiveEvent(time=20, type="mode_change", name="oc_single_gas"), DiveEvent(time=30, type="setpoint_change", value=1.0),
                          DiveEvent(time=40, type="bookmark")],
                  samples=[UnifiedSample(depth=1.0, time=0, pressure=200.0,
                                         channels=SampleChannels(pressures={0: 200.0}, ndl=99, cns=1.0, ppo2=0.3, heart_rate=70, gf99=10.0, gas_time=100,
                                                                 deco_stop_depth=3.0, deco_stop_time=60, setpoint=0.7))])
    assert write_file([plain], "uddf", tmp_path / "plain.uddf") == []
    assert write_file([_dive(samples=[UnifiedSample(depth=1.0, time=0, pressure=200.0)])], "uddf", tmp_path / "bare.uddf") == []


def test_describe_drops_matches_what_the_writer_reports(tmp_path):
    """The Convert page shows what a target will drop before the write; the
    preview is the writer's own list (I8)."""
    rich = _dive(bottom_time=100, water_type="salt",
                 samples=[UnifiedSample(depth=1.0, time=0, channels=SampleChannels(pressures={0: 200.0}, tts=40, gf99=12.0))])
    for format_id in ("uddf", "ssrf"):
        assert formats.describe_drops([rich], format_id) == write_file([rich], format_id, tmp_path / f"d.{format_id}")
    assert formats.describe_drops([rich], "uddf")[0] == "UDDF: the time to surface is not written"
    assert formats.describe_drops([], "uddf") == []
    with pytest.raises(UnsupportedFileError, match="read, not written"):
        formats.describe_drops([rich], "fit")


def test_write_refusals(tmp_path):
    with pytest.raises(UnsupportedFileError, match="Garmin FIT files are read, not written"):
        write_file([_dive()], "fit", tmp_path / "x.fit")
    with pytest.raises(UnsupportedFileError):
        write_file([_dive()], "csv", tmp_path / "x.csv")
    with pytest.raises(ValueError, match="no dives"):
        write_file([], "ssrf", tmp_path / "x.ssrf")


def test_write_file_ssrf(tmp_path):
    """The Subsurface writer of I6 behind the registry (its own tests are in test_convert_ssrf)."""
    path = tmp_path / "x.ssrf"
    assert write_file([_dive()], "ssrf", path) == []
    text = path.read_text(encoding="utf-8")
    assert text.startswith("<divelog program='subsurface' version='3'>\n")
    assert "<dive number='452' date='2026-09-30' time='14:32:05' duration='41:40 min'>" in text
    rich = _dive(samples=[UnifiedSample(depth=1.0, time=0, channels=SampleChannels(gf99=10.0))])
    assert write_file([rich], "ssrf", path) == ["Subsurface: the GF99 channel is not written"]
    with pytest.raises(ValueError, match="no dives"):
        write_file([], "uddf", tmp_path / "x.uddf")
    assert not os.path.exists(tmp_path / "x.uddf")


@fixtures
def test_fit_to_uddf_round_trip():
    """FIT -> UDDF -> read_uddf keeps the basic fields: start, duration,
    depths, dive number, the coldest temperature, the tanks and the profile;
    and since I5 both transmitters' pressures on every sample, the NDL, CNS,
    PO2 and gas-time channels, the gas switch and alerts, the computer, the
    gradient factors and the surface interval. What UDDF has no slot for is
    said in the warnings."""
    import tempfile
    dive = read_file(TWO_TANKS).dives[0]
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "two.uddf")
        warnings = write_file([dive], "uddf", path)
        _root, back = read_uddf(path)
    assert warnings == [
        "UDDF: the time to surface is not written",
        "UDDF: the tank pod connected, tank pod disconnected events are not written",
        "UDDF: the water type, water density, exit position, bottom time, start and end CNS are not written",
    ]
    assert len(back) == 1
    again = back[0]
    assert again.date_time == dive.date_time == datetime(2026, 8, 29, 9, 56, 11)
    assert again.duration == dive.duration == 2780
    assert again.max_depth == pytest.approx(dive.max_depth, abs=0.001)
    assert again.avg_depth == pytest.approx(dive.avg_depth, abs=0.001)
    assert again.dive_number == 38 and again.temp_min == dive.temp_min == 30.0
    assert [(g.oxygen, g.helium, g.start_pressure, g.end_pressure, g.tank_volume) for g in again.gas_mixtures] == [
        (21.0, 0.0, 204.29, 164.37, 11.1), (21.0, 0.0, 209.66, 161.75, 11.1)]
    assert len(again.samples) == len(dive.samples) == 331
    assert [(s.time, s.depth) for s in again.samples[:3]] == [(s.time, round(s.depth, 3)) for s in dive.samples[:3]]
    assert again.samples[-1].time == 2720
    assert again.external_ids == {"uddf": "dive_sync-20260829T095611"}
    # I5: the carried fields
    assert (again.computer_vendor, again.computer_model, again.computer_serial, again.computer_firmware) == (
        "Garmin", "Descent X50i", "1000000001", "7.05")
    assert (again.gf_low, again.gf_high, again.deco_model) == (40, 85, "ZHL-16C")
    assert again.surface_interval == 497326 and again.dive_mode == "oc_single_gas" and again.weight is None
    for mine, theirs in zip(dive.samples, again.samples):
        assert (theirs.channels.pressures if theirs.channels else {}) == (mine.channels.pressures if mine.channels else {})
        assert theirs.pressure == mine.pressure
        for name in ("ndl", "cns", "ppo2", "gas_time", "heart_rate"):
            assert getattr(theirs.channels, name, None) == getattr(mine.channels, name, None), (mine.time, name)
        assert theirs.channels is None or theirs.channels.tts is None
    assert sum(len(s.channels.pressures) for s in again.samples if s.channels) == 494
    kept = [e for e in dive.events if not e.type.startswith("tank_pod")]
    assert [(e.type, e.name, e.tank) for e in again.events] == [(e.type, e.name, e.tank) for e in kept]
    assert again.events[0].model_dump() == {"time": 0, "type": "gas_switch", "tank": 0, "oxygen": 21.0, "helium": 0.0}
    # an event keeps its second (the fixture has a sample at each one); the two after the last sample land on it
    assert [e.time for e in again.events] == [min(e.time, 2720) for e in kept]
    assert again.water_type is None and again.bottom_time is None


# --- output file names ----------------------------------------------------------

def test_output_file_name():
    assert output_file_name(_dive(), "uddf") == "2026-09-30 1432 dive 452.uddf"
    assert output_file_name(_dive(), "ssrf") == "2026-09-30 1432 dive 452.ssrf"
    assert output_file_name(_dive(number=None), "uddf") == "2026-09-30 1432.uddf"
    assert output_file_name(_dive(when=datetime(2026, 1, 5, 0, 7), number=1), "uddf") == "2026-01-05 0007 dive 1.uddf"
    with pytest.raises(UnsupportedFileError):
        output_file_name(_dive(), "csv")


@pytest.mark.parametrize("name, expected", [
    ("2026-09-30 1432 dive 452.uddf", "2026-09-30 1432 dive 452.uddf"),
    ('a<b>c:d"e/f\\g|h?i*j.uddf', "a_b_c_d_e_f_g_h_i_j.uddf"),
    ("tab\there\x00.uddf", "tab_here_.uddf"),
    ("  dots and spaces... .uddf", "dots and spaces.uddf"),
    ("...", "dive"),
    ("", "dive"),
    (".uddf", "uddf"),
    ("CON.uddf", "CON_.uddf"),
    ("lpt1.ssrf", "lpt1_.ssrf"),
    ("x" * 200 + ".uddf", "x" * 120 + ".uddf"),
    ("two   spaces.uddf", "two spaces.uddf"),
])
def test_safe_file_name(name, expected):
    assert safe_file_name(name) == expected


def test_output_paths_collisions_within_a_batch_and_on_disk(tmp_path):
    same = [_dive(), _dive(), _dive(number=None), _dive(number=None), _dive(when=datetime(2026, 9, 30, 14, 33))]
    paths = output_paths(same, "uddf", tmp_path)
    assert [os.path.basename(p) for p in paths] == [
        "2026-09-30 1432 dive 452.uddf", "2026-09-30 1432 dive 452 (2).uddf",
        "2026-09-30 1432.uddf", "2026-09-30 1432 (2).uddf", "2026-09-30 1433 dive 452.uddf"]
    assert all(os.path.dirname(p) == str(tmp_path) for p in paths)
    # a file already in the folder is not overwritten unless asked
    (tmp_path / "2026-09-30 1432 dive 452.uddf").write_text("keep me")
    (tmp_path / "2026-09-30 1432 dive 452 (2).uddf").write_text("keep me too")
    assert [os.path.basename(p) for p in output_paths([_dive(), _dive()], "uddf", tmp_path)] == [
        "2026-09-30 1432 dive 452 (3).uddf", "2026-09-30 1432 dive 452 (4).uddf"]
    assert [os.path.basename(p) for p in output_paths([_dive()], "uddf", tmp_path, overwrite=True)] == [
        "2026-09-30 1432 dive 452.uddf"]
    assert output_paths([], "uddf", tmp_path) == []


def test_write_each_writes_one_file_per_dive(tmp_path):
    folder = tmp_path / "out"
    dives = [_dive(), _dive(when=datetime(2026, 10, 1, 8, 0), number=None), _dive()]
    result = write_each(dives, "uddf", folder)
    assert [os.path.basename(p) for p in result.paths] == [
        "2026-09-30 1432 dive 452.uddf", "2026-10-01 0800.uddf", "2026-09-30 1432 dive 452 (2).uddf"]
    assert sorted(os.listdir(folder)) == sorted(os.path.basename(p) for p in result.paths)
    for path, dive in zip(result.paths, dives):
        _root, back = read_uddf(path)
        assert len(back) == 1 and back[0].dive_number == dive.dive_number and back[0].date_time == dive.date_time
    assert result.warnings == []
    # warnings are collected once across the files
    rich = [_dive(bottom_time=3000), _dive(when=datetime(2026, 10, 2, 9, 0), bottom_time=2000)]
    assert write_each(rich, "uddf", folder).warnings == ["UDDF: the bottom time is not written"]
    # a second run into the same folder does not overwrite the first
    assert os.path.basename(write_each([_dive()], "uddf", folder).paths[0]) == "2026-09-30 1432 dive 452 (4).uddf"
    assert os.path.basename(write_each([_dive()], "uddf", folder, overwrite=True).paths[0]) == "2026-09-30 1432 dive 452.uddf"
    ssrf = write_each([_dive(), _dive(when=datetime(2026, 10, 2, 9, 0))], "ssrf", folder)
    assert [os.path.basename(p) for p in ssrf.paths] == ["2026-09-30 1432 dive 452.ssrf", "2026-10-02 0900 dive 452.ssrf"]
    assert ssrf.warnings == []
