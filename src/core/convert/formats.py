"""The file formats the Convert page knows (plans/convert.md I4): what a
chosen file is, how to read it into `UnifiedDive`s, how to write dives out
again, the file-dialog filters and the name of each output file.

The registry is a tuple of `FileFormat`s. Detection goes by the extension
where it is unambiguous (``.fit``, ``.uddf``, ``.ssrf``) and by content where
it is not (``.zip`` must hold a ``.fit``; ``.xml`` is UDDF or a Subsurface
divelog by its root element; a file with any other extension is sniffed
the same way). Readers: the FIT reader of I3 (a bare ``.fit`` or a zip of
them: Connect's "export original" holds one, but every ``.fit`` entry is
read, so a zip of several gives all their dives) and the UDDF reader of the
UDDF adapter, and the Subsurface ``.ssrf`` reader of I6b. Writers: UDDF
through the existing `UddfDocument`, Subsurface ``.ssrf`` through
`ssrf_writer.SsrfDocument` (I6). A later format (the Shearwater database,
"to Garmin") is one more entry here.

The core is pure: no adapter, engine, settings or credentials, and nothing
is written but the paths the caller names.
"""
from __future__ import annotations

import logging
import os
import re
import zipfile
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Set, Tuple, Union
from xml.etree import ElementTree as ET

from src.core.convert.fit_reader import FitReadError, read_fit
from src.core.convert.ssrf_reader import SsrfReadError, read_ssrf
from src.core.convert.ssrf_writer import ssrf_drops, write_ssrf
from src.core.models import UnifiedDive
from src.core.services.uddf import UDDF_DIVE_MODES, WRITTEN_EVENT_TYPES, UddfDocument, read_uddf

logger = logging.getLogger("dive_sync.convert.formats")

PathLike = Union[str, "os.PathLike[str]"]

FIT, UDDF, SSRF = "fit", "uddf", "ssrf"
FIT_MAGIC = b".FIT"            # bytes 8..12 of a FIT header
ZIP_MAGIC = b"PK"
XML_ROOT_UDDF = "uddf"
XML_ROOT_SSRF = "divelog"      # Subsurface's XML log: <divelog program="subsurface" version="3">


class UnsupportedFileError(ValueError):
    """The file is not one of the formats the Convert page reads or writes
    (unknown extension and content, a zip without a FIT, a write-only
    format given to `read_file`, a read-only one given to `write_file`)."""


@dataclass(frozen=True)
class FileFormat:
    """One entry of the registry. ``extensions`` are lower case with the dot,
    the first one being what a written file gets; ``ambiguous`` lists those
    that other formats use too, which detection settles by content."""
    id: str
    label: str
    extensions: Tuple[str, ...]
    can_read: bool
    can_write: bool
    ambiguous: Tuple[str, ...] = ()

    @property
    def extension(self) -> str:
        return self.extensions[0]

    @property
    def name_filter(self) -> str:
        """Qt's file-dialog filter for this format alone: ``UDDF (*.uddf *.xml)``."""
        return f"{self.label} ({' '.join('*' + ext for ext in self.extensions)})"


FORMATS: Tuple[FileFormat, ...] = (
    FileFormat(FIT, "Garmin FIT", (".fit", ".zip"), can_read=True, can_write=False, ambiguous=(".zip",)),
    FileFormat(UDDF, "UDDF", (".uddf", ".xml"), can_read=True, can_write=True, ambiguous=(".xml",)),
    FileFormat(SSRF, "Subsurface", (".ssrf", ".xml"), can_read=True, can_write=True, ambiguous=(".xml",)),
)
ALL_FILES_FILTER = "All files (*)"


def get_format(format_id: str) -> FileFormat:
    """The registry entry with that id; `UnsupportedFileError` for an unknown one."""
    for fmt in FORMATS:
        if fmt.id == format_id:
            return fmt
    raise UnsupportedFileError(f"unknown file format '{format_id}'")


def readable_formats() -> List[FileFormat]:
    return [f for f in FORMATS if f.can_read]


def writable_formats() -> List[FileFormat]:
    return [f for f in FORMATS if f.can_write]


# ---------------------------------------------------------------------------
# File-dialog filters
# ---------------------------------------------------------------------------

def open_name_filters() -> List[str]:
    """The filters of the Open dialog: every readable format's extensions in
    one entry first, then one entry per format, then all files."""
    extensions: List[str] = []
    for fmt in readable_formats():
        extensions += [ext for ext in fmt.extensions if ext not in extensions]
    return ([f"Dive files ({' '.join('*' + ext for ext in extensions)})"]
            + [fmt.name_filter for fmt in readable_formats()] + [ALL_FILES_FILTER])


def save_name_filters(format_id: Optional[str] = None) -> List[str]:
    """The filters of the Save-as dialog: the chosen target's own extension
    (not its ambiguous ones, so the dialog appends the right one), or every
    writable format when none is chosen."""
    formats = [get_format(format_id)] if format_id else writable_formats()
    return [f"{fmt.label} (*{fmt.extension})" for fmt in formats]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _zip_fit_entries(path: PathLike) -> List[str]:
    """The ``.fit`` entries of a zip, in name order; [] when it is not a zip."""
    try:
        with zipfile.ZipFile(path) as archive:
            return sorted(n for n in archive.namelist() if n.lower().endswith(".fit") and not n.endswith("/"))
    except (zipfile.BadZipFile, OSError):
        return []


def _xml_root(path: PathLike) -> Optional[str]:
    """The local name of an XML file's root element, None when the file is
    not XML. Reads only up to the first start tag."""
    try:
        for _event, element in ET.iterparse(path, events=("start",)):
            tag = element.tag
            return tag.split("}", 1)[1] if isinstance(tag, str) and tag.startswith("{") else str(tag)
    except (ET.ParseError, OSError, UnicodeDecodeError):
        return None
    return None


def _sniff(path: PathLike) -> Optional[FileFormat]:
    """The format by content alone: a FIT header, a zip with a FIT, or XML
    whose root names a known format."""
    with open(path, "rb") as f:
        head = f.read(16)
    if head[8:12] == FIT_MAGIC:
        return get_format(FIT)
    if head[:2] == ZIP_MAGIC:
        return get_format(FIT) if _zip_fit_entries(path) else None
    root = _xml_root(path)
    if root == XML_ROOT_UDDF:
        return get_format(UDDF)
    if root == XML_ROOT_SSRF:
        return get_format(SSRF)
    return None


def detect_format(path: PathLike) -> FileFormat:
    """What the file is: by its extension when only one format uses it, by
    its content otherwise. `UnsupportedFileError` when neither tells;
    `FileNotFoundError` when the file is missing."""
    path = os.fspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    ext = os.path.splitext(path)[1].lower()
    for fmt in FORMATS:
        if ext in fmt.extensions and ext not in fmt.ambiguous:
            return fmt
    fmt = _sniff(path)
    if fmt is None:
        if ext == ".zip":
            raise UnsupportedFileError(f"{os.path.basename(path)}: the zip holds no .fit file")
        raise UnsupportedFileError(f"{os.path.basename(path)}: not a Garmin FIT, UDDF or Subsurface file")
    return fmt


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

@dataclass
class ReadResult:
    """What `read_file` / `read_files` got: the dives of every file read, in
    file order, the path each dive came from (``sources``, one per dive), the
    lines worth showing (an entry that was not a dive, a file that could not
    be read when several were given) and the files that gave dives as
    ``(path, format id)`` pairs."""
    dives: List[UnifiedDive] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    files: List[Tuple[str, str]] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)

    @property
    def format_id(self) -> Optional[str]:
        """The one format of the files read, None when they differ or none was read."""
        ids = {format_id for _path, format_id in self.files}
        return ids.pop() if len(ids) == 1 else None


def _read_fit_file(path: str, result: ReadResult) -> None:
    """A bare ``.fit`` or a zip of them. A zip with one FIT is read like the
    bare file (the zip's own name gives the activity id when it is a cache
    file name); with several, each entry's name does. An entry that is not a
    scuba dive is a warning, not an error, unless nothing else is left."""
    entries = _zip_fit_entries(path)
    if not entries:
        result.dives.append(read_fit(path))
        return
    if len(entries) == 1:
        result.dives.append(read_fit(path))
        return
    failures: List[str] = []
    with zipfile.ZipFile(path) as archive:
        for entry in entries:
            try:
                result.dives.append(read_fit(archive.read(entry), name=os.path.basename(entry)))
            except FitReadError as e:  # NotADiveError included
                failures.append(f"{os.path.basename(path)}: {entry}: {e}")
    if len(failures) == len(entries):
        raise UnsupportedFileError(f"{os.path.basename(path)}: none of its {len(entries)} .fit files is a dive")
    result.warnings += failures


def read_file(path: PathLike) -> ReadResult:
    """Every dive in one file. Raises `UnsupportedFileError` for a file of no
    known (or a write-only) format, the reader's own errors (`FitReadError`,
    `NotADiveError`, `xml.etree.ElementTree.ParseError`) for a file of a
    known format that cannot be read (`SsrfReadError` for XML whose root is
    not a divelog), `FileNotFoundError` for a missing one."""
    path = os.fspath(path)
    fmt = detect_format(path)
    if not fmt.can_read:
        raise UnsupportedFileError(f"{os.path.basename(path)}: {fmt.label} files can be written, not read")
    result = ReadResult()
    before = len(result.dives)
    if fmt.id == FIT:
        _read_fit_file(path, result)
    elif fmt.id == UDDF:
        _root, dives = read_uddf(path)
        result.dives += dives
        if not dives:
            result.warnings.append(f"{os.path.basename(path)}: the file holds no dive")
    elif fmt.id == SSRF:
        dives, warnings = read_ssrf(path)
        result.dives += dives
        result.warnings += warnings
        if not dives:
            result.warnings.append(f"{os.path.basename(path)}: the file holds no dive")
    else:  # pragma: no cover - every readable format is handled above
        raise UnsupportedFileError(f"{os.path.basename(path)}: no reader for {fmt.label}")
    result.files.append((path, fmt.id))
    result.sources += [path] * (len(result.dives) - before)
    logger.debug("Read %d dive(s) from %s (%s)", len(result.dives) - before, path, fmt.id)
    return result


def read_files(paths: Iterable[PathLike]) -> ReadResult:
    """The dives of several files, in the order given. With one path this is
    `read_file`; with several, a file that cannot be read becomes a warning
    and the rest are still read, and `UnsupportedFileError` is raised only
    when none of them could be."""
    paths = [os.fspath(p) for p in paths]
    if len(paths) == 1:
        return read_file(paths[0])
    result = ReadResult()
    failures: List[str] = []
    for path in paths:
        try:
            one = read_file(path)
        except (UnsupportedFileError, FitReadError, SsrfReadError, ET.ParseError, FileNotFoundError, OSError) as e:
            reason = str(e) if not isinstance(e, FileNotFoundError) else "file not found"
            failures.append(f"{os.path.basename(path)}: {reason}" if os.path.basename(path) not in reason else reason)
            continue
        result.dives += one.dives
        result.sources += one.sources
        result.warnings += one.warnings
        result.files += one.files
    if paths and not result.files:
        raise UnsupportedFileError("none of the files could be read: " + "; ".join(failures))
    result.warnings += failures
    return result


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _uddf_drops(dives: Sequence[UnifiedDive]) -> List[str]:
    """What the UDDF writer leaves out of these dives: the fields UDDF 3.2
    has no slot for (services/uddf.py's module docstring lists what is
    written since I5). Tank names and the timezone are not mentioned: no
    UDDF export carries them."""
    dropped: List[str] = []
    if any(s.channels and s.channels.tts is not None for d in dives for s in d.samples):
        dropped.append("the time to surface is not written")
    kinds = sorted({e.type for d in dives for e in d.events if e.type not in WRITTEN_EVENT_TYPES})
    if kinds:
        dropped.append(f"the {', '.join(k.replace('_', ' ') for k in kinds)} events are not written")
    fields = [
        ("water type", lambda d: d.water_type is not None), ("water density", lambda d: d.water_density is not None),
        ("exit position", lambda d: d.exit_lat is not None or d.exit_lng is not None),
        ("bottom time", lambda d: d.bottom_time is not None),
        ("start and end CNS", lambda d: d.cns_start is not None or d.cns_end is not None),
        ("gauge dive mode", lambda d: d.dive_mode is not None and d.dive_mode not in UDDF_DIVE_MODES),
    ]
    names = [name for name, present in fields if any(present(d) for d in dives)]
    if names:
        dropped.append(f"the {', '.join(names)} {'is' if len(names) == 1 else 'are'} not written")
    return [f"UDDF: {item}" for item in dropped]


def _write_uddf(dives: Sequence[UnifiedDive], path: str) -> List[str]:
    document = UddfDocument()
    for dive in dives:
        document.write_dive(dive)
    document.save(path)
    return _uddf_drops(dives)


# What the .ssrf writer leaves out, in the same shape as _uddf_drops (the
# writer itself knows what it writes, so the list lives next to it).
_ssrf_drops = ssrf_drops


def _write_ssrf(dives: Sequence[UnifiedDive], path: str) -> List[str]:
    return write_ssrf(dives, path)


_WRITERS = {UDDF: _write_uddf, SSRF: _write_ssrf}
_DROPS = {UDDF: _uddf_drops, SSRF: _ssrf_drops}


def describe_drops(dives: Sequence[UnifiedDive], format_id: str) -> List[str]:
    """What writing ``dives`` as ``format_id`` would leave out, as the
    writer reports it after a write (the Convert page shows it before one).
    `UnsupportedFileError` for a format that is not written."""
    fmt = get_format(format_id)
    if not fmt.can_write:
        raise UnsupportedFileError(f"{fmt.label} files are read, not written")
    return _DROPS[fmt.id](list(dives)) if dives else []


def write_file(dives: Sequence[UnifiedDive], format_id: str, path: PathLike) -> List[str]:
    """Write ``dives`` into one file of ``format_id`` at ``path`` (which is
    overwritten: the caller chose it in a dialog). Returns the lines worth
    showing about what the format could not hold. `UnsupportedFileError` for
    a format that is not written."""
    fmt = get_format(format_id)
    if not fmt.can_write:
        raise UnsupportedFileError(f"{fmt.label} files are read, not written")
    if not dives:
        raise ValueError("no dives to write")
    path = os.fspath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    warnings = _WRITERS[fmt.id](dives, path)
    logger.info("Wrote %d dive(s) as %s to %s", len(dives), fmt.id, path)
    return warnings


@dataclass
class WriteResult:
    """What `write_each` wrote: one path per dive, in the dives' order, and
    the warnings of every file (deduplicated, in first-seen order)."""
    paths: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def write_each(dives: Sequence[UnifiedDive], format_id: str, folder: PathLike, overwrite: bool = False) -> WriteResult:
    """One file per dive in ``folder`` (created when missing), named by
    `output_file_name`; a name taken by another dive of the batch or, unless
    ``overwrite``, by a file already there gets a ``(2)``, ``(3)`` suffix."""
    folder = os.fspath(folder)
    os.makedirs(folder, exist_ok=True)
    result = WriteResult()
    for dive, path in zip(dives, output_paths(dives, format_id, folder, overwrite=overwrite)):
        for line in write_file([dive], format_id, path):
            if line not in result.warnings:
                result.warnings.append(line)
        result.paths.append(path)
    return result


# ---------------------------------------------------------------------------
# Output file names (decided 2026-10-01: "<YYYY-MM-DD> <hhmm> dive <number>.<ext>")
# ---------------------------------------------------------------------------

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
# Device names Windows refuses as a file stem, whatever the extension.
_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)})
MAX_STEM_LENGTH = 120


def safe_file_name(name: str, fallback: str = "dive") -> str:
    """``name`` with what Windows and macOS refuse in a file name replaced:
    the reserved characters become ``_``, leading and trailing dots and
    spaces go, a Windows device name gets an underscore, and an empty result
    becomes ``fallback``. The extension, if any, is kept."""
    stem, ext = os.path.splitext(name)
    if not stem and ext:  # ".uddf" is a stem-less name, not an extension
        stem, ext = ext, ""
    stem = _UNSAFE_CHARS.sub("_", stem)
    ext = _UNSAFE_CHARS.sub("_", ext)
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    if not stem:
        stem = fallback
    if stem.upper() in _RESERVED_STEMS:
        stem += "_"
    if len(stem) > MAX_STEM_LENGTH:
        stem = stem[:MAX_STEM_LENGTH].rstrip(" .")
    return stem + ext.rstrip(" .")


def output_file_name(dive: UnifiedDive, format_id: str) -> str:
    """``2026-09-30 1432 dive 452.uddf`` from the local start time and the
    dive number; ``2026-09-30 1432.uddf`` when the dive has no number."""
    fmt = get_format(format_id)
    stem = dive.date_time.strftime("%Y-%m-%d %H%M")
    if dive.dive_number is not None:
        stem += f" dive {dive.dive_number}"
    return safe_file_name(stem + fmt.extension)


def output_paths(dives: Sequence[UnifiedDive], format_id: str, folder: PathLike, overwrite: bool = False) -> List[str]:
    """The path each dive gets under ``folder``, one per dive in order. Two
    dives that would share a name (same minute and number) get ``(2)``,
    ``(3)``... on the later ones; a name already taken by a file in the
    folder is skipped the same way unless ``overwrite``."""
    folder = os.fspath(folder)
    taken: Set[str] = set()  # lower case: Windows and macOS file systems are case-insensitive
    out: List[str] = []
    for dive in dives:
        name = output_file_name(dive, format_id)
        stem, ext = os.path.splitext(name)
        candidate, n = name, 1
        while candidate.lower() in taken or (not overwrite and os.path.exists(os.path.join(folder, candidate))):
            n += 1
            candidate = f"{stem} ({n}){ext}"
        taken.add(candidate.lower())
        out.append(os.path.join(folder, candidate))
    return out
