"""The merge debug report ``src/core/convert/debug_report.py`` (plans/convert.md I21).

Pinned on the synthetic logs of ``test_convert_fit_merge`` (the builder
`_log`): what the report says per log (file_id and device_info serials,
the computed serial and where it came from, samples, gases, tanks,
events), the request and the outcome (a refusal; an error with its
traceback), an unreadable file, and - the part that matters most - what
never gets in: positions, string fields (a site name), full paths, the
home folder, credentials.
"""
import os
import struct

import pytest

from src.core.convert import debug_report
from src.core.convert.debug_report import PRIVACY_NOTE, format_exception, merge_report, scrub
from tests.test_convert_fit import SESSION, T0, _message, build_fit
from tests.test_convert_fit_merge import SERIAL, _a, _b

HOME = os.path.expanduser("~")
SPORT = 12          # the sport message: its `name` (field 3) is a string


def _write(folder, name, data):
    path = folder / name
    path.write_bytes(data)
    return str(path)


def _string_message(local, global_num, field_num, text):
    """A data message with one string field (the merge tests' encoder has
    no string type), e.g. a sport's name."""
    raw = text.encode() + b"\0"
    definition = bytearray([0x40 | local, 0, 0]) + struct.pack("<H", global_num) + bytes([1, field_num, len(raw), 0x07])
    return bytes(definition + bytearray([local]) + raw)


def _with_positions_and_names(lat, lng, site):
    """The merge tests' log a with a session that carries the start position
    (fields 3 and 4, semicircles; its end position is already in `_log`)
    and a sport message naming a site - what a report must leave out."""
    data = _a()
    # splice: rebuild from the builder's messages is involved; instead, append the extra messages before the CRC
    body = data[14:-2]
    extra = _message(11, SESSION, [(253, "uint32", T0), (2, "uint32", T0), (3, "sint32", lat), (4, "sint32", lng),
                                   (38, "sint32", lat + 1), (39, "sint32", lng + 1), (5, "enum", 53), (6, "enum", 53),
                                   (8, "uint32", 120000)])
    extra += _string_message(12, SPORT, 3, site)
    # a fresh file from the old body plus the extras (the header's size and both CRCs are recomputed by build_fit)
    return build_fit([body + extra])


def test_report_describes_the_logs_and_the_refusal(tmp_path):
    """A report for two synthetic logs, b without a serial: the header
    (the privacy note, the time, the app, OS and library versions - the
    ones given or `about.components`), the request, the refusal, then per
    log the file name only, size and SHA-256, file_id and device_info
    fields with their serials, the samples, the serial and its source
    (file_id for a, none for b), gases, tank sensors and summaries, event
    counts, the dive number; then the merge's own checks."""
    a, b = _write(tmp_path, "a.fit", _a()), _write(tmp_path, "b.fit", _b(serial=0))
    text = merge_report([a, b], problem="b.fit is odd", versions=[{"name": "fitdecode", "version": "9.9.9"}])
    assert text.startswith("DiveSync debug report\n=====================\n\n" + PRIVACY_NOTE)
    assert "\nGenerated: 20" in text and " UTC\nDiveSync: " in text and "\nOS: " in text and "\nfitdecode: 9.9.9\n" in text
    assert "Python: " not in text          # the versions given stand in for the installed ones
    assert "\nRequest\n-------\nFiles: 2\n  1. a.fit\n  2. b.fit\nMode: not chosen (the merge was not run)\nSerial chosen: none\n" in text
    assert "\nOutcome\n-------\nRefused: b.fit is odd\n" in text
    assert str(tmp_path) not in text       # base names only
    log1 = text[text.index("\nLog 1\n"):text.index("\nLog 2\n")]
    log2 = text[text.index("\nLog 2\n"):text.index("\nMerge checks\n")]
    assert f"File: a.fit\nSize: {os.path.getsize(a)} bytes\nSHA-256: " in log1 and "Readable: yes\n" in log1
    assert f"file_id: type=activity (4) manufacturer=garmin (1) product=descent_mk3i (4223) garmin_product=descent_mk3i (4223) serial_number={SERIAL} time_created=2024-11-08 11:33:20" in log1
    assert f"device_info 1 [creator]: timestamp=2024-11-08 11:33:20 device_index=creator (0) product=4223 serial_number={SERIAL}" in log1
    assert "device_info 2: timestamp=2024-11-08 11:33:20 product=3000 serial_number=1001" in log1
    assert "session: start_time=2024-11-08 11:33:20 timestamp=2024-11-08 11:33:20 total_elapsed_time=120 total_timer_time=120 sport=diving (53)" in log1
    assert "Records: 3 samples, first 2024-11-08 11:33:20, last 2024-11-08 11:35:20, logging interval 60 s" in log1
    assert f"Serial: {SERIAL} (from file_id)" in log1
    assert "Gases: message_index=0 oxygen_content=32 helium_content=0 mode=open_circuit (0) status=enabled (1)" in log1
    assert "Tank sensors (tank_update): 1001\nTank summaries: sensor=1001 start_pressure=200 end_pressure=150 volume_used=50" in log1
    assert "Events: dive_gas_switched/marker 1, timer/start 1, timer/stop_all 1" in log1
    assert "dive_summary: dive_number=12 max_depth=18 avg_depth=6.5 bottom_time=120" in log1
    assert "Messages: activity 1, device_info 2, dive_gas 1, dive_summary 1, event 3, file_id 1, record 3, session 1, tank_summary 1, tank_update 3" in log1
    assert "File: b.fit" in log2 and "Serial: none (neither file_id nor a creator device_info carries one)" in log2
    assert "file_id: type=activity (4) manufacturer=garmin (1) product=descent_mk3i (4223) garmin_product=descent_mk3i (4223) time_created=" in log2
    assert "device_info 1 [creator]: timestamp=2024-11-08 11:38:20 device_index=creator (0) product=4223\n" in log2
    assert "dive_summary: dive_number=13" in log2
    checks = text[text.index("\nMerge checks\n"):text.index("\nRecent log lines\n")]
    assert "merge_problem: none (the logs merge)" in checks and "missing_serials: b.fit" in checks and f"serial_choices: {SERIAL}" in checks
    assert text.endswith("\nRecent log lines\n----------------\n(none)\n")
    # the installed versions when none are given
    assert "\nPython: " in merge_report([a, b]) and "\nfitdecode: " in merge_report([a, b])


def test_serial_from_the_device_info_and_the_mode(tmp_path):
    """A log whose file_id has no serial but whose creator device_info
    does: the serial's source says so; the mode and the chosen serial
    are written out as given."""
    a = _write(tmp_path, "a.fit", _a(file_serial=0, creator_serial=4242))
    text = merge_report([a], mode=True, serial="4242")
    assert "Serial: 4242 (from the creator device_info; file_id has none)" in text
    assert "Mode: remove the surface time (close the gaps)\nSerial chosen: 4242\n" in text
    assert "Mode: add the surface time (keep the clock)" in merge_report([a], mode=False)
    assert "Mode: as the user said" in merge_report([a], mode="as the user said")
    assert "merge_problem: two or more dives are needed" in text


def test_report_with_an_error_and_a_traceback(tmp_path):
    """A failed merge: the error, the traceback indented under it (the home
    folder written as ~ in it) and the recent log lines, the last 300 of
    them."""
    a, b = _write(tmp_path, "a.fit", _a()), _write(tmp_path, "b.fit", _b())
    try:
        raise ValueError("a.fit logs its samples without full timestamps")
    except ValueError as e:
        trace = format_exception(e)
    assert trace.startswith("Traceback (most recent call last):") and trace.rstrip().endswith("ValueError: a.fit logs its samples without full timestamps")
    trace = trace.replace(__file__, os.path.join(HOME, "code", "tests", "test_debug_report.py"))
    lines = [f"line {i}" for i in range(350)]
    text = merge_report([a, b], error="a.fit logs its samples without full timestamps", traceback_text=trace, mode=False, log_lines=lines)
    assert "\nOutcome\n-------\nError: a.fit logs its samples without full timestamps\nTraceback:\n  Traceback (most recent call last):\n" in text
    assert '  File "~/code/tests/test_debug_report.py"' in text and HOME not in text
    recent = text[text.index("\nRecent log lines\n"):]
    assert "line 49\n" not in recent and recent.endswith("line 50\nline 51\n" + "\n".join(f"line {i}" for i in range(52, 350)) + "\n")
    assert "(none recorded)" in merge_report([a, b]) and "(none recorded)" not in text


def test_privacy_nothing_personal_gets_in(tmp_path):
    """The rules the report is built on, enforced: a log whose session
    carries the start and end positions and whose sport message names a
    site, log lines and a traceback with a path under the home folder and
    a password, a token and an Authorization header - none of the position
    values (semicircles or degrees), the site name, the full path, the
    home folder or the secrets appear; the file names and ~ do."""
    lat, lng = 987654321, -456789012
    folder = tmp_path / "Secret Dive Trip"
    folder.mkdir()
    a = _write(folder, "a.fit", _with_positions_and_names(lat, lng, "Secret Reef by the villa"))
    b = _write(folder, "b.fit", _b())
    full = os.path.join(HOME, "Dives", "2026", "a.fit")
    log_lines = [f"2026-10-19 10:00:00 INFO dive_sync.convert: read {full}",
                 "2026-10-19 10:00:01 INFO dive_sync.garmin: login password=hunter2 user=someone",
                 "2026-10-19 10:00:02 INFO dive_sync.garmin: token: eyJabc.def.ghi",
                 "2026-10-19 10:00:03 INFO dive_sync.garmin: Authorization: Bearer abcdefghijklmnop",
                 "2026-10-19 10:00:04 INFO dive_sync.garmin: GARMIN_PASSWORD=Hunter3"]
    trace = f'Traceback (most recent call last):\n  File "{full}", line 1, in <module>\n    password="hunter2"\nValueError: boom {full}\n'
    text = merge_report([a, b], error=f"cannot read {full}", traceback_text=trace, log_lines=log_lines)
    # the positions: not the raw semicircles, not the degrees, not the end position the builder sets
    for value in (lat, lng, lat + 1, lng + 1, T0 // 2, -(T0 // 4)):
        assert str(value) not in text, value
        assert f"{value * 180 / 2 ** 31:.3f}"[:6] not in text, value
    assert "position" not in text.replace("no GPS positions", "")
    # no string field of the file, so no site name
    assert "Secret Reef" not in text and "villa" not in text
    # the session that carries the position is still counted and its times shown
    assert "session 2" in text and "start_time=2024-11-08 11:33:20" in text
    # paths: base names in the request and the logs, ~ for the home folder in the error, the traceback and the lines
    assert "Secret Dive Trip" not in text and str(tmp_path) not in text
    assert "  1. a.fit\n  2. b.fit\n" in text and "File: a.fit\n" in text and "File: b.fit\n" in text
    assert HOME not in text and full not in text
    assert "Error: cannot read ~/Dives/2026/a.fit\n" in text and 'File "~/Dives/2026/a.fit"' in text
    assert "read ~/Dives/2026/a.fit" in text
    # the secrets
    for secret in ("hunter2", "Hunter3", "eyJabc", "abcdefghijklmnop"):
        assert secret not in text, secret
    assert "password=[redacted]" in text and "token: [redacted]" in text and "Authorization: [redacted]" in text
    assert "GARMIN_PASSWORD=[redacted]" in text and "\n      password=[redacted]\n" in text   # the traceback is indented two more
    assert "user=someone" not in text    # the rest of a line with a password goes too (the safe side)


def test_scrub():
    """`scrub` alone: the home folder in both slash styles, keys in any
    case with = or :, a bare bearer token; an ordinary line is unchanged."""
    assert scrub(f"at {HOME}/x and {HOME}") == "at ~/x and ~"
    assert scrub("Password: abc") == "Password: [redacted]" and scrub("api_key=k1\nok") == "api_key=[redacted]\nok"
    assert scrub("a Bearer 0123456789abcdef b") == "a Bearer [redacted] b"
    assert scrub("12:00  Merged 2 logs") == "12:00  Merged 2 logs"
    assert scrub("the token field") == "the token field"


def test_unreadable_files_are_reported_with_their_error(tmp_path):
    """A file that is not a FIT and one that does not exist: the report
    names them with the read error and the merge checks say so (they
    stop at the first unreadable log, as the Merge dialog's check does);
    the readable log before them is described in full."""
    junk = _write(tmp_path, "junk.fit", b"not a fit file at all")
    missing = str(tmp_path / "gone.fit")
    a = _write(tmp_path, "a.fit", _a())
    text = merge_report([a, junk, missing], problem="junk.fit is not a Garmin dive log")
    assert f"File: junk.fit\nSize: {len(b'not a fit file at all')} bytes\nSHA-256: " in text
    assert "\nRead error: not a FIT file this reader can decode" in text
    assert "File: gone.fit\nRead error: No such file or directory\n" in text
    assert "Size: " + str(os.path.getsize(a)) in text and f"Serial: {SERIAL} (from file_id)" in text
    assert "merge_problem: junk.fit is not a Garmin dive log (" in text
    assert "missing_serials: none\nserial_choices: " + str(SERIAL) in text
    assert str(tmp_path) not in text
    # a zip that holds no .fit
    import zipfile
    with zipfile.ZipFile(tmp_path / "empty.zip", "w") as z:
        z.writestr("readme.txt", "x")
    text = merge_report([str(tmp_path / "empty.zip")])
    assert "File: empty.zip\n" in text and "Read error: the zip holds no .fit file" in text


def test_report_without_files():
    """No paths at all (nothing selected): still a report, saying so."""
    text = merge_report([], error="Two or more logs are needed")
    assert "Files: 0\n" in text and "Error: Two or more logs are needed" in text and "merge_problem: no files" in text
    assert "\nLog 1\n" not in text


@pytest.mark.parametrize("value, shown", [(None, "-"), (1.5, "1.5"), (2.0, "2"), (b"ab", "<2 bytes>"), ([1, "x"], "[1, x]")])
def test_show(value, shown):
    assert debug_report._show(value) == shown
