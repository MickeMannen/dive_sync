"""The debug report of a merge that was refused or failed (plans/convert.md
I21): a plain-text file the user saves through the Dive edit page's
**Generate debug report…** and sends to the developer.

`merge_report` describes the request (the files, the mode, the chosen
serial), the outcome (the refusal text, or the error and its traceback),
each log as the FIT decoder sees it - the parts the merge's rules look at:
file_id, every device_info, the session times, the samples, the serial and
where it came from, gases, tanks, event counts, the dive number - the
merge's own checks re-run over the files, and the app's recent log lines.

What it does not hold, by construction (`tests/test_debug_report.py` pins
it): no GPS position (no ``position`` or semicircle field is ever read),
no site, location, notes or buddy (only the named numeric and enumerated
fields listed here are printed; never a string field), no full file path
(base names only, and the home folder is written as ``~`` in the error,
the traceback and the log lines), no password or token (lines that carry
one are redacted). The report says so at the top, in `PRIVACY_NOTE`.
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import traceback as traceback_module
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

import fitdecode

from src.core import about
from src.core.convert import fit_merge
from src.core.convert.fit_patch import FitPatchError, PathLike, fit_bytes
from src.core.convert.fit_reader import FitReadError, _creators, _Messages, _serial, _value, decode

TITLE = "DiveSync debug report"

PRIVACY_NOTE = (
    "What this report contains: the DiveSync, system and library versions, the merge that was asked for, why it was "
    "refused or how it failed, and technical details of each dive log (file name, size and checksum, the dive "
    "computer and its serial, start and end times, sample count, gases, tanks and event counts) plus the app's recent "
    "log lines. It holds no GPS positions, no site or location names, no notes, no buddy, no full file paths (file "
    "names only; your home folder is written as ~) and no passwords or tokens."
)

# The fields read per message: named, numeric or enumerated, never a string
# or a position. Nothing outside these lists is printed.
FILE_ID_FIELDS = ("type", "manufacturer", "product", "garmin_product", "serial_number", "time_created", "number")
DEVICE_INFO_FIELDS = ("timestamp", "device_index", "manufacturer", "product", "garmin_product", "serial_number",
                      "software_version", "source_type", "device_type")
SESSION_FIELDS = ("start_time", "timestamp", "total_elapsed_time", "total_timer_time", "sport", "sub_sport")
DIVE_GAS_FIELDS = ("message_index", "oxygen_content", "helium_content", "mode", "status")
TANK_SUMMARY_FIELDS = ("sensor", "start_pressure", "end_pressure", "volume_used")
DIVE_SUMMARY_FIELDS = ("dive_number", "max_depth", "avg_depth", "bottom_time")

MAX_LOG_LINES = 300

# A credential on a line: everything after password= / token: / Authorization: … to the end of the line is redacted
# (over-redacting a log line is the safe side), and a bare "Bearer <token>" too.
_SECRET = re.compile(r"(?im)(password|passwd|pwd|token|secret|api[_-]?key|authorization)\b(\s*[=:]\s*)(.*)$")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9+/=._-]{8,}")


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------

def _home_patterns() -> List[re.Pattern]:
    home = os.path.expanduser("~")
    if not home or home == "~":
        return []
    variants = {home, home.replace("\\", "/"), home.replace("/", "\\")}
    flags = re.IGNORECASE if os.name == "nt" else 0
    return [re.compile(re.escape(v), flags) for v in sorted(variants, key=len, reverse=True)]


def scrub(text: str) -> str:
    """``text`` with the home folder written as ``~`` and credentials
    (``password=…``, ``token: …``, ``Authorization: …``, bearer tokens)
    redacted - applied to the error, the traceback, the log lines and,
    last, to the whole report."""
    for pattern in _home_patterns():
        text = pattern.sub("~", text)
    text = _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
    return _BEARER.sub(lambda m: f"{m.group(1)} [redacted]", text)


# ---------------------------------------------------------------------------
# One log
# ---------------------------------------------------------------------------

def _show(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_show(v) for v in value) + "]"
    return str(value)


def _fields(message: Optional[fitdecode.FitDataMessage], names: Iterable[str]) -> str:
    """``name=value`` for each of ``names`` the message carries (the raw
    number next to an enumerated name when they differ), "" for none."""
    parts = []
    for name in names:
        value = _value(message, name)
        if value is None:
            continue
        raw = _value(message, name, raw=True)
        shown = _show(value)
        if raw is not None and not isinstance(raw, (datetime, bytes, bytearray)) and str(raw) != shown and isinstance(value, str):
            shown = f"{shown} ({raw})"
        parts.append(f"{name}={shown}")
    return " ".join(parts)


def _timestamps(messages: Sequence[fitdecode.FitDataMessage]) -> List[datetime]:
    return [t for t in (_value(m, "timestamp") for m in messages) if isinstance(t, datetime)]


def _interval(stamps: Sequence[datetime]) -> Optional[int]:
    gaps = Counter(int((b - a).total_seconds()) for a, b in zip(stamps, stamps[1:]) if b > a)
    return gaps.most_common(1)[0][0] if gaps else None


def _serial_source(messages: _Messages) -> str:
    serial = _serial(messages)
    if serial is None:
        return "none (neither file_id nor a creator device_info carries one)"
    file_serial = _value(messages.first("file_id"), "serial_number")
    if file_serial and int(file_serial) == serial:
        return f"{serial} (from file_id)"
    return f"{serial} (from the creator device_info; file_id has none)"


def describe_log(path: PathLike) -> List[str]:
    """The lines of one log's section: name, size, SHA-256, then - when it
    decodes - the message counts and the fields the merge looks at; else
    the read error."""
    path = os.fspath(path)
    name = os.path.basename(path)
    lines = [f"File: {name}"]
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        lines.append(f"Read error: {e.strerror or e}")
        return lines
    lines.append(f"Size: {len(raw)} bytes")
    lines.append(f"SHA-256: {hashlib.sha256(raw).hexdigest()}")
    try:
        data, member = fit_bytes(raw)
    except FitPatchError as e:
        lines.append(f"Read error: {e}")
        return lines
    if member:
        lines.append(f"Zip member: {os.path.basename(member)} ({len(data)} bytes)")
    try:
        messages = decode(data)
    except FitReadError as e:
        lines.append(f"Read error: {e}")
        return lines
    lines.append("Readable: yes")
    counts = Counter()
    for key, items in messages.by_name.items():
        counts[str(key)] += len(items)
    lines.append("Messages: " + ", ".join(f"{k} {n}" for k, n in sorted(counts.items())))
    file_id = messages.first("file_id")
    lines.append("file_id: " + (_fields(file_id, FILE_ID_FIELDS) or "-") if file_id is not None else "file_id: missing")
    creators = _creators(messages)
    infos = messages.all("device_info")
    for i, info in enumerate(infos, 1):
        tag = " [creator]" if info in creators else ""
        lines.append(f"device_info {i}{tag}: " + (_fields(info, DEVICE_INFO_FIELDS) or "-"))
    if not infos:
        lines.append("device_info: none")
    session = messages.first("session")
    lines.append("session: " + (_fields(session, SESSION_FIELDS) or "-") if session is not None else "session: missing")
    records = messages.all("record")
    stamps = _timestamps(records)
    if records:
        interval = _interval(stamps)
        lines.append(f"Records: {len(records)} samples, first {_show(stamps[0] if stamps else None)}, "
                     f"last {_show(stamps[-1] if stamps else None)}, logging interval {interval if interval is not None else '-'} s")
    else:
        lines.append("Records: none")
    lines.append(f"Serial: {_serial_source(messages)}")
    gases = messages.all("dive_gas")
    lines.append("Gases: " + ("; ".join(_fields(g, DIVE_GAS_FIELDS) or "-" for g in gases) if gases else "none"))
    sensors = sorted({str(s) for s in (_value(u, "sensor") for u in messages.all("tank_update")) if s is not None})
    lines.append("Tank sensors (tank_update): " + (", ".join(sensors) if sensors else "none"))
    summaries = messages.all("tank_summary")
    lines.append("Tank summaries: " + ("; ".join(_fields(t, TANK_SUMMARY_FIELDS) or "-" for t in summaries) if summaries else "none"))
    events = Counter()
    for e in messages.all("event"):
        events[f"{_show(_value(e, 'event'))}/{_show(_value(e, 'event_type'))}"] += 1
    lines.append("Events: " + (", ".join(f"{k} {n}" for k, n in sorted(events.items())) if events else "none"))
    summary = messages.first("dive_summary")
    lines.append("dive_summary: " + (_fields(summary, DIVE_SUMMARY_FIELDS) or "-") if summary is not None else "dive_summary: missing")
    return lines


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def _heading(text: str, char: str = "-") -> List[str]:
    return ["", text, char * len(text)]


def _versions(versions: Optional[Sequence[Dict[str, str]]]) -> List[str]:
    items = list(versions) if versions is not None else about.components()
    return [f"{item.get('name', '?')}: {item.get('version', '?')}" for item in items]


def _mode_text(mode: Optional[Union[bool, str]]) -> str:
    if mode is None or mode == "":
        return "not chosen (the merge was not run)"
    if isinstance(mode, str):
        return mode
    return "remove the surface time (close the gaps)" if mode else "add the surface time (keep the clock)"


def _checks(paths: Sequence[str]) -> List[str]:
    lines = []
    try:
        problem = fit_merge.merge_problem(paths) if paths else "no files"
        lines.append(f"merge_problem: {problem or 'none (the logs merge)'}")
    except Exception as e:  # a check that itself breaks is worth seeing, not hiding
        lines.append(f"merge_problem: check failed: {e!r}")
    try:
        missing = fit_merge.missing_serials(paths) if paths else []
        lines.append("missing_serials: " + (", ".join(missing) if missing else "none"))
    except Exception as e:
        lines.append(f"missing_serials: check failed: {e!r}")
    try:
        choices = fit_merge.serial_choices(paths) if paths else []
        lines.append("serial_choices: " + (", ".join(choices) if choices else "none"))
    except Exception as e:
        lines.append(f"serial_choices: check failed: {e!r}")
    return lines


def merge_report(paths: Sequence[PathLike], *, problem: Optional[str] = None, error: Optional[str] = None,
                 traceback_text: Optional[str] = None, mode: Optional[Union[bool, str]] = None,
                 serial: Optional[str] = None, versions: Optional[Sequence[Dict[str, str]]] = None,
                 log_lines: Optional[Sequence[str]] = None) -> str:
    """The report for the merge of ``paths`` (the logs, in the order the
    merge was asked for) as text: the header (the time, the versions -
    ``versions`` as `about.components` lists them, read when not given -
    and the platform), the request (``mode``: True to close the gaps,
    False to keep the clock, a string as given, None when the merge was
    not run; ``serial``: the chosen serial), the outcome (``problem``: the
    refusal; ``error`` and ``traceback_text``: a failure), each log
    (`describe_log`), the merge's checks re-run over the files and the
    last `MAX_LOG_LINES` of ``log_lines``. Everything is passed through
    `scrub`; see the module docstring for what is kept out."""
    paths = [os.fspath(p) for p in paths]
    now = datetime.now(timezone.utc)
    lines = [TITLE, "=" * len(TITLE), "", PRIVACY_NOTE, "",
             f"Generated: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC",
             f"DiveSync: {about.app_version()}",
             f"OS: {platform.platform()}"]
    lines += _versions(versions)
    lines += _heading("Request")
    lines.append(f"Files: {len(paths)}")
    for i, path in enumerate(paths, 1):
        lines.append(f"  {i}. {os.path.basename(path)}")
    lines.append(f"Mode: {_mode_text(mode)}")
    lines.append(f"Serial chosen: {serial if serial else 'none'}")
    lines += _heading("Outcome")
    if problem:
        lines.append(f"Refused: {problem}")
    if error:
        lines.append(f"Error: {error}")
    if traceback_text:
        lines.append("Traceback:")
        lines += ["  " + line for line in traceback_text.rstrip("\n").splitlines()]
    if not (problem or error or traceback_text):
        lines.append("(none recorded)")
    for i, path in enumerate(paths, 1):
        lines += _heading(f"Log {i}")
        lines += describe_log(path)
    lines += _heading("Merge checks")
    lines += _checks(paths)
    lines += _heading("Recent log lines")
    recent = list(log_lines or [])[-MAX_LOG_LINES:]
    lines += recent if recent else ["(none)"]
    return scrub("\n".join(lines)) + "\n"


def format_exception(exc: BaseException) -> str:
    """``exc``'s traceback as text, for the controller to keep with a
    failed request."""
    return "".join(traceback_module.format_exception(type(exc), exc, exc.__traceback__))
