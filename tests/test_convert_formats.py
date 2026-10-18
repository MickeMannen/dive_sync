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
    list_dive_files,
    open_name_filters,
    readable_extensions,
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
from tests.test_convert_fit import build_fit, minimal_dive, _dive_summary, _file_id, _record, _session
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
    assert (fit.can_read, fit.can_write, fit.extension, fit.patches_source) == (True, True, ".fit", True)   # written by patching (I14b)
    assert (uddf.can_read, uddf.can_write, uddf.extension, uddf.patches_source) == (True, True, ".uddf", False)
    assert (ssrf.can_read, ssrf.can_write, ssrf.extension, ssrf.patches_source) == (True, True, ".ssrf", False)
    assert [f.id for f in formats.readable_formats()] == ["fit", "uddf", "ssrf"]
    assert [f.id for f in formats.writable_formats()] == ["uddf", "ssrf", "fit"]          # the patch writer last
    for f in FORMATS:
        assert all(ext.startswith(".") and ext == ext.lower() for ext in f.extensions)
        assert set(f.ambiguous) <= set(f.extensions) and f.extension not in f.ambiguous
    with pytest.raises(UnsupportedFileError, match="unknown file format 'csv'"):
        get_format("csv")


def test_name_filters():
    assert open_name_filters() == ["Dive files (*.fit *.zip *.uddf *.xml *.ssrf)", "Garmin FIT (*.fit *.zip)",
                                   "UDDF (*.uddf *.xml)", "Subsurface (*.ssrf *.xml)", "All files (*)"]
    assert save_name_filters() == ["UDDF (*.uddf)", "Subsurface (*.ssrf)", "Garmin FIT (*.fit)"]
    assert save_name_filters("uddf") == ["UDDF (*.uddf)"]
    assert save_name_filters("fit") == ["Garmin FIT (*.fit)"]                 # never *.zip: a plain .fit comes out
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
    assert result.files == [(SINGLE_GAS, "fit")] and result.format_id == "fit" and result.sources == [SINGLE_GAS]


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
    # the export as Connect names it, and a zip of several Connect members: each entry's name gives its id
    export = _zip_of({"22569827629_ACTIVITY.fit": single}, tmp_path / "22569827629.zip")
    assert read_file(export).dives[0].external_ids == {"garmin": "22569827629"}
    both = _zip_of({"22569827629_ACTIVITY.fit": single, "22569827630_ACTIVITY.fit": two}, tmp_path / "day.zip")
    assert [d.external_ids["garmin"] for d in read_file(both).dives] == ["22569827629", "22569827630"]
    running = build_fit([_file_id(), _session(sport=1, sub_sport=0), _record(0, 1.0)])
    many = _zip_of({"b_two.fit": two, "a_single.fit": single, "c_run.fit": running, "notes.txt": b"x"},
                   tmp_path / "many.zip")
    result = read_file(many)
    assert [d.dive_number for d in result.dives] == [28, 38]    # entry name order
    assert result.sources == [many, many] and result.files == [(many, "fit")]
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
    assert result.sources == [str(uddf), str(uddf), str(fit)]       # the path each dive came from
    assert len(result.warnings) == 2
    assert result.warnings[0].startswith("notes.txt: ") and result.warnings[1] == "missing.fit: file not found"
    # one path: the error is raised, as read_file does
    with pytest.raises(UnsupportedFileError):
        read_files([text])
    # several paths, none readable
    with pytest.raises(UnsupportedFileError, match="none of the files could be read"):
        read_files([text, tmp_path / "missing.fit"])
    assert read_files([]).dives == [] and read_files([]).format_id is None


def test_list_dive_files_takes_the_top_level_by_extension(tmp_path):
    """I13, Open folder…: the files directly in the folder whose extension a
    readable format claims, case-insensitively, in name order; hidden files
    and subfolders are left alone; a .zip or .xml is listed by its extension
    alone (read_files says what it really is). The ambiguous extensions are
    in the list once each."""
    import shutil
    assert readable_extensions() == [".fit", ".zip", ".uddf", ".xml", ".ssrf"]      # registry order, each once
    folder = tmp_path / "logs"
    folder.mkdir()
    (folder / "b.uddf").write_text(UDDF_SAMPLE, encoding="utf-8")
    (folder / "a.ssrf").write_text(SSRF_SAMPLE, encoding="utf-8")
    (folder / "DIVE.FIT").write_bytes(minimal_dive())                 # upper case: taken
    (folder / "Zed.xml").write_text("<notes/>", encoding="utf-8")       # by extension only
    _zip_of({"readme.txt": b"hello"}, folder / "nothing.zip")           # by extension only
    (folder / "notes.txt").write_text("x")                              # not a dive extension
    (folder / "README").write_text("x")                                 # no extension
    (folder / ".hidden.ssrf").write_text(SSRF_SAMPLE, encoding="utf-8") # dot-file: skipped
    (folder / "fit").mkdir()                                            # a folder named like an extension
    (folder / "more").mkdir()
    shutil.copy(folder / "a.ssrf", folder / "more" / "deeper.ssrf")    # a subfolder: not searched
    files = list_dive_files(folder)
    assert all(isinstance(f, type(folder)) for f in files)
    assert [f.name for f in files] == ["a.ssrf", "b.uddf", "DIVE.FIT", "nothing.zip", "Zed.xml"]
    assert all(f.parent == folder for f in files)
    assert list_dive_files(str(folder)) == files                         # a plain path works too
    # what the listing hands on reads the way Open files… does: the two by-extension-only files are warnings
    result = read_files(files)
    assert [os.path.basename(path) for path, _fmt in result.files] == ["a.ssrf", "b.uddf", "DIVE.FIT"]
    assert sorted(w.split(":")[0] for w in result.warnings) == ["Zed.xml", "nothing.zip"]
    # empty, and not a folder
    empty = tmp_path / "empty"
    empty.mkdir()
    assert list_dive_files(empty) == []
    with pytest.raises(NotADirectoryError):
        list_dive_files(tmp_path / "missing")
    with pytest.raises(NotADirectoryError):
        list_dive_files(folder / "a.ssrf")


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
    assert formats.describe_drops([], "fit") == []
    # a dive with no .fit behind it is encoded (I12): the encoder's drops, the same line the write reports
    assert formats.describe_drops([rich], "fit") == ["Garmin FIT: not in a .fit, save as UDDF or Subsurface for these: GF99 channel"]
    assert formats.describe_drops([rich], "fit") == write_file([rich], "fit", tmp_path / "d.fit")
    assert formats.describe_drops([_dive()], "fit", sources=[""]) == []


def test_write_refusals(tmp_path):
    with pytest.raises(ValueError, match="a .fit holds one dive"):
        write_file([_dive(), _dive()], "fit", tmp_path / "x.fit", sources=["a.fit", "b.fit"])
    with pytest.raises(ValueError, match="a .fit holds one dive"):
        write_file([_dive(), _dive()], "fit", tmp_path / "x.fit")
    assert not (tmp_path / "x.fit").exists()
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
    # the patch writer names the copy after the file it patches; a Connect zip's name with .fit
    assert output_file_name(_dive(), "fit", "/dives/38_2026-08-29_095611_12345678901.fit") == "38_2026-08-29_095611_12345678901 (edited).fit"
    assert output_file_name(_dive(), "fit", "/dives/12345678901.zip") == "12345678901 (edited).fit"
    assert output_file_name(_dive(), "fit") == "2026-09-30 1432 dive 452.fit"      # no source known
    assert output_file_name(_dive(), "uddf", "/dives/x.fit") == "2026-09-30 1432 dive 452.uddf"   # whole-file writers ignore it
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


# --- Save as .fit: the patch writer behind the registry (I14b; the patcher's own tests are in test_convert_fit_patch) ---

def test_cannot_write_reason_for_the_fit_target(tmp_path):
    """Save as .fit is on for any dive (I12): dives opened from a .fit (or
    a Connect zip holding one) are patched, the others encoded; only a
    selection mixing the two kinds is refused, with the button's tip."""
    fit = tmp_path / "d.fit"
    fit.write_bytes(minimal_dive())
    archive = _zip_of({"12345678901_ACTIVITY.fit": minimal_dive()}, tmp_path / "12345678901.zip")
    (tmp_path / "s.ssrf").write_text(SSRF_SAMPLE)
    ssrf = str(tmp_path / "s.ssrf")
    assert formats.source_is("fit", str(fit)) and formats.source_is("fit", archive) and not formats.source_is("fit", ssrf)
    assert not formats.source_is("fit", "") and not formats.source_is("fit", None) and formats.source_is("ssrf", ssrf)
    assert formats.source_is("fit", str(tmp_path / "missing.fit")) and formats.source_is("fit", "/x/12345678901.zip")   # a missing file: by its name
    assert not formats.source_is("fit", str(tmp_path / "missing.ssrf"))
    assert formats.cannot_write_reason("fit", [str(fit)]) == ""
    assert formats.cannot_write_reason("fit", [str(fit), archive]) == ""
    assert formats.cannot_write_reason("fit", [ssrf]) == "" and formats.cannot_write_reason("fit", [ssrf, ""]) == ""
    assert formats.cannot_write_reason("fit", []) == "" and formats.cannot_write_reason("fit", [""]) == ""
    assert formats.cannot_write_reason("uddf", [ssrf]) == "" and formats.cannot_write_reason("ssrf", []) == ""
    mixed = "Save as .fit patches a dive opened from a .fit file and writes any other dive from scratch: select one kind at a time"
    assert formats.cannot_write_reason("fit", [str(fit), ssrf]) == mixed
    assert formats.cannot_write_reason("fit", [str(fit), ""]) == mixed
    with pytest.raises(UnsupportedFileError):
        formats.cannot_write_reason("csv", [str(fit)])


def test_write_file_fit_patches_a_copy_and_never_the_source(tmp_path):
    """The patch writer: the edited values in a new file, the source byte
    for byte as it was, the source path refused as the target, the
    not-in-a-FIT fields in the warnings as one line; originals read again
    from the source when the caller has none."""
    from src.core.convert import edit
    source = tmp_path / "dive.fit"
    source.write_bytes(minimal_dive())
    original = read_file(source).dives[0]
    edited = edit.apply_edits(original, {"dive_number": "99", "location": "Jetty", "buddy": "Kim", "notes": "n", "weight": "3", "visibility": "10"})
    drops = formats.describe_drops([edited], "fit", sources=[str(source)], originals=[original])
    assert drops == ["Garmin FIT: not in a .fit, save as UDDF or Subsurface for these: site, buddy, notes, weight, visibility"]
    assert formats.describe_drops([edited], "fit", sources=[str(source)]) == drops          # originals re-read
    out = tmp_path / "out" / "dive (edited).fit"
    assert write_file([edited], "fit", out, sources=[str(source)], originals=[original]) == drops
    back = read_file(out)
    assert back.format_id == "fit" and back.dives[0].dive_number == 99 and back.dives[0].location is None
    assert source.read_bytes() == minimal_dive()
    with pytest.raises(ValueError, match="is the file the dive was opened from"):
        write_file([edited], "fit", source, sources=[str(source)], originals=[original])
    with pytest.raises(ValueError, match="is the file the dive was opened from"):
        write_file([edited], "fit", str(tmp_path / "." / "dive.fit"), sources=[str(source)])
    assert source.read_bytes() == minimal_dive()
    # an unedited dive gives a copy with no warning
    assert write_file([original], "fit", tmp_path / "copy.fit", sources=[str(source)], originals=[original]) == []
    assert (tmp_path / "copy.fit").read_bytes() == minimal_dive()


def test_write_each_fit_names_the_copies_after_their_files(tmp_path):
    from src.core.convert import edit
    a, b = tmp_path / "a.fit", tmp_path / "b.zip"
    a.write_bytes(minimal_dive())
    _zip_of({"b_ACTIVITY.fit": build_fit([_file_id(), _session(timer=900.0), _record(0, 1.0), _record(60, 12.0), _dive_summary(number=2)])}, b)
    result = read_files([a, b])
    edited = [edit.apply_edits(result.dives[0], {"dive_number": "5"}), edit.apply_edits(result.dives[1], {"dive_number": "6"})]
    folder = tmp_path / "out"
    written = write_each(edited, "fit", folder, sources=result.sources, originals=result.dives)
    assert [os.path.basename(p) for p in written.paths] == ["a (edited).fit", "b (edited).fit"] and written.warnings == []
    assert [d.dive_number for d in read_files(written.paths).dives] == [5, 6]
    assert read_file(written.paths[1]).format_id == "fit" and not zipfile.is_zipfile(written.paths[1])   # a plain .fit out of the zip
    # into the source folder: the (edited) name never collides with the source, and a second run gets (2)
    again = write_each(edited[:1], "fit", tmp_path, sources=result.sources[:1], originals=result.dives[:1])
    assert [os.path.basename(p) for p in again.paths] == ["a (edited).fit"] and a.read_bytes() == minimal_dive()
    again = write_each(edited[:1], "fit", tmp_path, sources=result.sources[:1], originals=result.dives[:1])
    assert [os.path.basename(p) for p in again.paths] == ["a (edited) (2).fit"]


def test_a_dive_from_an_ssrf_is_encoded_as_fit(tmp_path):
    """A dive read from a .ssrf is saved as .fit by encoding it (I12), the
    source and originals given or not; it reads back through the registry,
    and the name is the date one, not "(edited)"."""
    (tmp_path / "s.ssrf").write_text(SSRF_SAMPLE)
    ssrf = read_file(tmp_path / "s.ssrf")
    assert formats.cannot_write_reason("fit", ssrf.sources) == ""
    assert output_file_name(ssrf.dives[0], "fit", ssrf.sources[0]) == "2026-06-22 0930 dive 1.fit"
    out = tmp_path / "s.fit"
    assert write_file(ssrf.dives, "fit", out, sources=ssrf.sources, originals=ssrf.dives) == []
    back = read_file(out)
    assert back.format_id == "fit" and back.dives[0].dive_number == 1 and back.dives[0].date_time == datetime(2026, 6, 22, 9, 30)
    assert back.dives[0].duration == 3120 and back.dives[0].timezone is None
    assert write_file(ssrf.dives, "fit", tmp_path / "t.fit") == [] and (tmp_path / "t.fit").read_bytes() == out.read_bytes()
    # one file per dive: the encoded dive beside a patched one, each named its own way
    (tmp_path / "d.fit").write_bytes(minimal_dive())
    both = read_files([tmp_path / "s.ssrf", tmp_path / "d.fit"])
    written = write_each(both.dives, "fit", tmp_path / "out", sources=both.sources, originals=both.dives)
    assert [os.path.basename(p) for p in written.paths] == ["2026-06-22 0930 dive 1.fit", "d (edited).fit"]
    assert [d.dive_number for d in read_files(written.paths).dives] == [1, 12]


@fixtures
def test_the_fixture_patched_through_the_registry_round_trips(tmp_path):
    """The FIT fixture written as .fit through the registry is patched, not
    encoded: the edits read back and the drops are the patcher's."""
    from src.core.convert import edit
    fit = read_file(TWO_TANKS)
    edited = edit.apply_edits(fit.dives[0], {"gf_high": "90", "buddy": "Kim"})
    out = tmp_path / "two_tanks (edited).fit"
    assert write_file([edited], "fit", out, sources=fit.sources, originals=fit.dives) == [
        "Garmin FIT: not in a .fit, save as UDDF or Subsurface for these: buddy"]
    assert read_file(out).dives[0].gf_high == 90
