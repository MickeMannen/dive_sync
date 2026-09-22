"""Subsurface git storage format: parser, writer and adapter (rework.md F4).

This is the format Subsurface keeps in its cloud repository and in local
git repositories ("git storage"), reverse-engineered from
``core/load-git.cpp`` / ``core/save-git.cpp``:

    00-Subsurface                      settings (never touched here)
    01-Divesites/Site-<8 hex>          name/description/notes/gps
    YYYY/MM/DD-Www-hh=mm=ss[~hex]/     one dive; the local start time is the name
        Dive-<number>                  dive fields (duration, buddy, notes, cylinders, ...)
        Divecomputer[-NNN]             per dive computer: depths, extradata, events, samples
    YYYY/MM/DD-<trip>[~hex]/           trip; contains 00-Trip and dive dirs named [MM-]DD-Www-...

Numbers are SI with milli precision ("11.094l", "206.843bar", "30.0°C"),
durations "mm:ss", strings quoted with ``\\"`` / ``\\\\`` escapes and a
newline written as newline + tab. Dives we create get a ``Divecomputer``
with ``model "dive_sync"``; the other services' ids are stored as
``keyvalue "dive_sync:<service>" "<id>"`` extra data on that computer, which
is what makes tier-1 matching work on re-runs. Existing files are edited
line by line so everything we do not understand is preserved verbatim.

The adapter works on a directory (a checked-out clone). Pull/commit/push for
Subsurface Cloud is step F6.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.core.adapter import BaseDiveAdapter
from src.core.fields import FieldSpec, are_gas_mixtures_different
from src.core.models import GasMixture, UnifiedDive, UnifiedSample
from src.core.site_matcher import find_site

logger = logging.getLogger("dive_sync.subsurface")

SERVICE_ID = "subsurface"
OUR_MODEL = "dive_sync"
EXTERNAL_ID_PREFIX = "dive_sync:"
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")  # datetime.weekday() order

DIVE_DIR_RE = re.compile(r"^(?:(\d{4})-)?(?:(\d{2})-)?(\d{2})-[A-Za-z]{3}-(\d{2})[=:](\d{2})[=:](\d{2})(?:~[0-9a-f]+)?$")
TRIP_DIR_RE = re.compile(r"^(\d{2})-[^\d].*$")
SITE_FILE_RE = re.compile(r"^Site-([0-9a-fA-F]{8})$")
DIVE_FILE_RE = re.compile(r"^Dive(?:-(\d+))?$")
DC_FILE_RE = re.compile(r"^Divecomputer(?:-(\d{3}))?$")


# ---------------------------------------------------------------------------
# Scalars and strings
# ---------------------------------------------------------------------------

def fmt_milli(value: float) -> str:
    """Subsurface's put_milli: integer part, then 1 to 3 decimals with
    trailing zeros trimmed ("30.0", "1.2", "11.094")."""
    milli = int(round(value * 1000))
    sign = "-" if milli < 0 else ""
    milli = abs(milli)
    whole, frac = divmod(milli, 1000)
    digits = f"{frac:03d}"
    if digits[2] == "0":
        digits = digits[:2]
        if digits[1] == "0":
            digits = digits[:1]
    return f"{sign}{whole}.{digits}"


def fmt_duration(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def parse_duration(text: str) -> int:
    m = re.match(r"\s*(\d+)(?::(\d+))?", text)
    if not m:
        return 0
    return int(m.group(1)) * 60 + int(m.group(2) or 0)


def parse_number(text: str) -> Optional[float]:
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


def quote(text: str) -> str:
    out = []
    for i, ch in enumerate(text):
        code = ord(ch)
        if (0 < code < 9) or code in (11, 12) or (14 <= code <= 31):
            out.append("?")
        elif ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            nxt = text[i + 1] if i + 1 < len(text) else ""
            out.append("\n" if nxt == "\n" else "\n\t")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def split_line(raw: str) -> Tuple[str, List[str]]:
    """Subsurface's parse_one_line: return the line with every quoted string
    replaced by ``\"`` markers, plus the strings (unescaped) in order. ``raw``
    may span several physical lines (a newline inside a string is followed by
    a tab)."""
    strings: List[str] = []
    out: List[str] = []
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch != '"':
            out.append(ch)
            i += 1
            continue
        out.append('"')
        i += 1
        buf: List[str] = []
        while i < n:
            ch = raw[i]
            if ch == "\\" and i + 1 < n:
                buf.append(raw[i + 1])
                i += 2
            elif ch == "\n" and i + 1 < n and raw[i + 1] == "\t":
                buf.append("\n")
                i += 2
            elif ch == '"':
                i += 1
                break
            else:
                buf.append(ch)
                i += 1
        out.append('"')
        strings.append("".join(buf))
    return "".join(out), strings


def read_logical_lines(text: str) -> List[str]:
    """Physical lines joined where a quoted string continues (newline + tab)."""
    lines: List[str] = []
    for physical in text.split("\n"):
        if physical.startswith("\t") and lines:
            lines[-1] += "\n" + physical
        else:
            lines.append(physical)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def parse_keyvalues(text: str, strings: List[str]) -> List[Tuple[str, str]]:
    """``key=value`` tokens (and bare keys) of a cylinder / event / sample tail.
    A value written as ``"`` takes the next string from ``strings``."""
    pairs: List[Tuple[str, str]] = []
    idx = 0
    for token in text.split():
        if "=" in token:
            key, value = token.split("=", 1)
            if value.startswith('"'):
                value = strings[idx] if idx < len(strings) else ""
                idx += 1
        else:
            key, value = token, ""
        pairs.append((key, value))
    return pairs


# ---------------------------------------------------------------------------
# Data model of one repository
# ---------------------------------------------------------------------------

@dataclass
class Site:
    uuid: str
    name: str = ""
    description: str = ""
    notes: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None
    extra_lines: List[str] = field(default_factory=list)


# Subsurface's cylinderuse enum, as written by save_cylinder_info (only
# values above OC_GAS get a "use=" attribute at all; the default open-circuit
# gas has none). This is a narrower, different axis than a multi-tank "role"
# (Submersion's backGas/stage/deco/...): Subsurface only distinguishes a
# rebreather diluent/oxygen tank, a bailout tank, or an unused spare.
USE_TO_TANK_ROLE = {"diluent": "diluent", "oxygen": "oxygen", "bailout": "bailout", "not used": "not_used"}
TANK_ROLE_TO_USE = {v: k for k, v in USE_TO_TANK_ROLE.items()}


@dataclass
class Cylinder:
    volume_l: Optional[float] = None
    workpressure_bar: Optional[float] = None
    description: str = ""
    o2: Optional[float] = None       # percent; None = air
    he: Optional[float] = None
    start_bar: Optional[float] = None
    end_bar: Optional[float] = None
    use: str = ""                    # "", "diluent", "oxygen", "bailout", "not used"
    depth_m: Optional[float] = None


@dataclass
class Sample:
    time_s: int
    depth_m: Optional[float] = None
    temp_c: Optional[float] = None
    pressures: Dict[int, float] = field(default_factory=dict)   # sensor index -> bar
    raw_tail: str = ""                                           # everything else, verbatim


@dataclass
class DiveComputer:
    filename: str = "Divecomputer"
    model: str = ""
    deviceid: str = ""
    diveid: str = ""
    duration_s: Optional[int] = None
    maxdepth_m: Optional[float] = None
    meandepth_m: Optional[float] = None
    airtemp_c: Optional[float] = None
    watertemp_c: Optional[float] = None
    extradata: List[Tuple[str, str]] = field(default_factory=list)
    samples: List[Sample] = field(default_factory=list)
    lines: List[str] = field(default_factory=list)   # logical lines as read, for minimal-diff rewrites


@dataclass
class Dive:
    dir_path: str                    # relative to the repo root, e.g. "2026/08/29-Sat-09=56=11"
    when: datetime                   # naive local start time (from the directory name)
    number: Optional[int] = None
    file_name: str = "Dive"
    duration_s: Optional[int] = None
    rating: Optional[int] = None
    visibility: Optional[int] = None
    tags: List[str] = field(default_factory=list)
    site_uuid: Optional[str] = None
    divemaster: Optional[str] = None
    buddy: Optional[str] = None
    suit: Optional[str] = None
    notes: Optional[str] = None
    cylinders: List[Cylinder] = field(default_factory=list)
    weights_kg: List[float] = field(default_factory=list)
    airtemp_c: Optional[float] = None
    watertemp_c: Optional[float] = None
    computers: List[DiveComputer] = field(default_factory=list)
    lines: List[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.dir_path}/{self.file_name}"

    def external_ids(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for dc in self.computers:
            for key, value in dc.extradata:
                if key.startswith(EXTERNAL_ID_PREFIX) and value:
                    out[key[len(EXTERNAL_ID_PREFIX):]] = value
        return out


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_site(uuid: str, text: str) -> Site:
    site = Site(uuid=uuid)
    for line in read_logical_lines(text):
        stripped, strings = split_line(line)
        key = stripped.split(" ", 1)[0]
        if key == "name":
            site.name = strings[0] if strings else ""
        elif key == "description":
            site.description = strings[0] if strings else ""
        elif key == "notes":
            site.notes = strings[0] if strings else ""
        elif key == "gps":
            nums = stripped.split()[1:]
            if len(nums) >= 2:
                site.lat, site.lng = float(nums[0]), float(nums[1])
        else:
            site.extra_lines.append(line)
    return site


def parse_cylinder(tail: str, strings: List[str]) -> Cylinder:
    cyl = Cylinder()
    for key, value in parse_keyvalues(tail, strings):
        if key == "vol":
            cyl.volume_l = parse_number(value)
        elif key == "workpressure":
            cyl.workpressure_bar = parse_number(value)
        elif key == "description":
            cyl.description = value
        elif key == "o2":
            cyl.o2 = parse_number(value)
        elif key == "he":
            cyl.he = parse_number(value)
        elif key == "start":
            cyl.start_bar = parse_number(value)
        elif key == "end":
            cyl.end_bar = parse_number(value)
        elif key == "use":
            cyl.use = value
        elif key == "depth":
            cyl.depth_m = parse_number(value)
    return cyl


def parse_dive_file(dive: Dive, text: str) -> None:
    dive.lines = read_logical_lines(text)
    for line in dive.lines:
        stripped, strings = split_line(line)
        parts = stripped.split(" ", 1)
        key, tail = parts[0], (parts[1] if len(parts) > 1 else "")
        if key == "duration":
            dive.duration_s = parse_duration(tail)
        elif key == "rating":
            dive.rating = int(parse_number(tail) or 0)
        elif key == "visibility":
            dive.visibility = int(parse_number(tail) or 0)
        elif key == "tags":
            dive.tags = [s for s in strings if s]
        elif key == "divesiteid":
            dive.site_uuid = tail.strip().lower()
        elif key == "divemaster":
            dive.divemaster = strings[0] if strings else ""
        elif key == "buddy":
            dive.buddy = strings[0] if strings else ""
        elif key == "suit":
            dive.suit = strings[0] if strings else ""
        elif key == "notes":
            dive.notes = strings[0] if strings else ""
        elif key == "cylinder":
            dive.cylinders.append(parse_cylinder(tail, strings))
        elif key == "weightsystem":
            for k, v in parse_keyvalues(tail, strings):
                if k == "weight":
                    dive.weights_kg.append(parse_number(v) or 0.0)
        elif key == "airtemp":
            dive.airtemp_c = parse_number(tail)
        elif key == "watertemp":
            dive.watertemp_c = parse_number(tail)


def parse_sample_line(line: str) -> Sample:
    tokens = line.split()
    sample = Sample(time_s=parse_duration(tokens[0]))
    tail: List[str] = []
    for token in tokens[1:]:
        m = re.match(r"^(-?\d+(?:\.\d+)?)(m|°C|bar(?::\d+)?)$", token)
        if m and "=" not in token:
            value, unit = float(m.group(1)), m.group(2)
            if unit == "m":
                sample.depth_m = value
            elif unit == "°C":
                sample.temp_c = value
            else:
                sensor = int(unit[4:]) if ":" in unit else 0
                sample.pressures[sensor] = value
        else:
            tail.append(token)
    sample.raw_tail = " ".join(tail)
    return sample


def parse_divecomputer_file(filename: str, text: str) -> DiveComputer:
    dc = DiveComputer(filename=filename)
    dc.lines = read_logical_lines(text)
    for line in dc.lines:
        if line[:1] == " " or re.match(r"^\d+:\d+", line):
            dc.samples.append(parse_sample_line(line))
            continue
        stripped, strings = split_line(line)
        parts = stripped.split(" ", 1)
        key, tail = parts[0], (parts[1] if len(parts) > 1 else "")
        if key == "model":
            dc.model = strings[0] if strings else ""
        elif key == "deviceid":
            dc.deviceid = tail.strip()
        elif key == "diveid":
            dc.diveid = tail.strip()
        elif key == "duration":
            dc.duration_s = parse_duration(tail)
        elif key == "maxdepth":
            dc.maxdepth_m = parse_number(tail)
        elif key == "meandepth":
            dc.meandepth_m = parse_number(tail)
        elif key == "airtemp":
            dc.airtemp_c = parse_number(tail)
        elif key == "watertemp":
            dc.watertemp_c = parse_number(tail)
        elif key == "keyvalue" and len(strings) >= 2:
            dc.extradata.append((strings[0], strings[1]))
    return dc


def parse_dive_dir_name(name: str, year: int, month: int) -> Optional[datetime]:
    m = DIVE_DIR_RE.match(name)
    if not m:
        return None
    yyyy = int(m.group(1)) if m.group(1) else year
    mm = int(m.group(2)) if m.group(2) else month
    dd, hh, mi, ss = (int(m.group(i)) for i in (3, 4, 5, 6))
    try:
        return datetime(yyyy, mm, dd, hh, mi, ss)
    except ValueError:
        return None


class SubsurfaceRepo:
    """A checked-out Subsurface git storage tree."""

    def __init__(self, root: str):
        self.root = root
        self.sites: Dict[str, Site] = {}
        self.dives: List[Dive] = []

    # -- reading -----------------------------------------------------------

    def load(self) -> "SubsurfaceRepo":
        self.sites, self.dives = {}, []
        sites_dir = os.path.join(self.root, "01-Divesites")
        if os.path.isdir(sites_dir):
            for name in sorted(os.listdir(sites_dir)):
                m = SITE_FILE_RE.match(name)
                if m:
                    with open(os.path.join(sites_dir, name), "r", encoding="utf-8") as f:
                        self.sites[m.group(1).lower()] = parse_site(m.group(1).lower(), f.read())
        for year_name in sorted(os.listdir(self.root)):
            if not re.match(r"^\d{4}$", year_name):
                continue
            year_dir = os.path.join(self.root, year_name)
            for month_name in sorted(os.listdir(year_dir)):
                if not re.match(r"^\d{2}$", month_name):
                    continue
                month_dir = os.path.join(year_dir, month_name)
                self._walk_month(month_dir, f"{year_name}/{month_name}", int(year_name), int(month_name))
        self.dives.sort(key=lambda d: d.when)
        return self

    def _walk_month(self, month_dir: str, rel: str, year: int, month: int) -> None:
        for name in sorted(os.listdir(month_dir)):
            path = os.path.join(month_dir, name)
            if not os.path.isdir(path):
                continue
            when = parse_dive_dir_name(name, year, month)
            if when is not None:
                self._load_dive(path, f"{rel}/{name}", when)
            elif TRIP_DIR_RE.match(name):
                for inner in sorted(os.listdir(path)):
                    inner_path = os.path.join(path, inner)
                    inner_when = parse_dive_dir_name(inner, year, month)
                    if os.path.isdir(inner_path) and inner_when is not None:
                        self._load_dive(inner_path, f"{rel}/{name}/{inner}", inner_when)

    def _load_dive(self, path: str, rel: str, when: datetime) -> None:
        dive = Dive(dir_path=rel, when=when)
        found_dive_file = False
        for name in sorted(os.listdir(path)):
            file_path = os.path.join(path, name)
            if not os.path.isfile(file_path):
                continue
            dm = DIVE_FILE_RE.match(name)
            cm = DC_FILE_RE.match(name)
            if dm:
                found_dive_file = True
                dive.file_name = name
                dive.number = int(dm.group(1)) if dm.group(1) else None
                with open(file_path, "r", encoding="utf-8") as f:
                    parse_dive_file(dive, f.read())
            elif cm:
                with open(file_path, "r", encoding="utf-8") as f:
                    dive.computers.append(parse_divecomputer_file(name, f.read()))
        if found_dive_file:
            dive.computers.sort(key=lambda dc: dc.filename)
            self.dives.append(dive)

    # -- writing -----------------------------------------------------------

    def site_file(self, uuid: str) -> str:
        return os.path.join(self.root, "01-Divesites", f"Site-{uuid}")

    def write_site(self, site: Site) -> None:
        os.makedirs(os.path.join(self.root, "01-Divesites"), exist_ok=True)
        lines = [f"name {quote(site.name)}", f"description {quote(site.description)}", f"notes {quote(site.notes)}"]
        if site.lat is not None and site.lng is not None:
            lines.append(f"gps {site.lat:.6f} {site.lng:.6f}")
        lines.extend(site.extra_lines)
        with open(self.site_file(site.uuid), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.sites[site.uuid] = site

    def new_site(self, name: str, lat: Optional[float], lng: Optional[float]) -> Site:
        uuid = secrets.token_hex(4)
        while uuid in self.sites:
            uuid = secrets.token_hex(4)
        site = Site(uuid=uuid, name=name or "", lat=lat, lng=lng)
        self.write_site(site)
        return site

    def resolve_site(self, name: Optional[str], lat: Optional[float], lng: Optional[float],
                     radius_m: float = 200.0) -> Optional[Site]:
        if not name and (lat is None or lng is None):
            return None
        site = find_site(self.sites.values(), name, lat, lng,
                         get_name=lambda s: s.name, get_gps=lambda s: (s.lat, s.lng), radius_m=radius_m)
        return site or self.new_site(name or "", lat, lng)

    @staticmethod
    def dive_dir_name(when: datetime) -> str:
        return f"{when.day:02d}-{WEEKDAYS[when.weekday()]}-{when.hour:02d}={when.minute:02d}={when.second:02d}"

    def write_dive(self, dive: Dive) -> None:
        """Write (or rewrite) ``Dive-N`` and every computer of ``dive``.
        Lines read from disk are kept; only fields we own are replaced."""
        path = os.path.join(self.root, dive.dir_path)
        os.makedirs(path, exist_ok=True)
        # a renumbered dive gets a new file name; drop the old one
        wanted = f"Dive-{dive.number}" if dive.number is not None else "Dive"
        if dive.file_name != wanted and os.path.exists(os.path.join(path, dive.file_name)):
            os.remove(os.path.join(path, dive.file_name))
        dive.file_name = wanted
        with open(os.path.join(path, dive.file_name), "w", encoding="utf-8") as f:
            f.write("\n".join(render_dive_lines(dive)) + "\n")
        for dc in dive.computers:
            with open(os.path.join(path, dc.filename), "w", encoding="utf-8") as f:
                f.write("\n".join(render_dc_lines(dc)) + "\n")

    def delete_dive(self, dive: Dive) -> None:
        shutil.rmtree(os.path.join(self.root, dive.dir_path), ignore_errors=True)
        self.dives = [d for d in self.dives if d.dir_path != dive.dir_path]

    def find(self, dive_id: str) -> Optional[Dive]:
        for dive in self.dives:
            if dive.id == dive_id or dive.dir_path == dive_id:
                return dive
        return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_cylinder(cyl: Cylinder) -> str:
    parts = ["cylinder"]
    if cyl.volume_l:
        parts.append(f"vol={fmt_milli(cyl.volume_l)}l")
    if cyl.workpressure_bar:
        parts.append(f"workpressure={fmt_milli(cyl.workpressure_bar)}bar")
    if cyl.description:
        parts.append(f"description={quote(cyl.description)}")
    if cyl.o2:
        parts.append(f"o2={cyl.o2:.1f}%")
        if cyl.he:
            parts.append(f"he={cyl.he:.1f}%")
    if cyl.start_bar:
        parts.append(f"start={fmt_milli(cyl.start_bar)}bar")
    if cyl.end_bar:
        parts.append(f"end={fmt_milli(cyl.end_bar)}bar")
    if cyl.use:
        parts.append(f"use={quote(cyl.use)}")
    if cyl.depth_m:
        parts.append(f"depth={fmt_milli(cyl.depth_m)}m")
    return " ".join(parts)


_OWNED_DIVE_KEYS = ("duration", "rating", "visibility", "tags", "divesiteid", "divemaster", "buddy",
                    "suit", "notes", "cylinder", "weightsystem", "airtemp", "watertemp")
_DIVE_ORDER = ("duration", "rating", "visibility", "wavesize", "current", "surge", "chill", "watersalinity",
               "airpressure", "notrip", "invalid", "tags", "divesiteid", "divemaster", "buddy", "suit", "notes",
               "cylinder", "weightsystem", "airtemp", "watertemp")


def _dive_field_lines(dive: Dive) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    if dive.duration_s:
        out["duration"] = [f"duration {fmt_duration(dive.duration_s)} min"]
    if dive.rating:
        out["rating"] = [f"rating {dive.rating}"]
    if dive.visibility:
        out["visibility"] = [f"visibility {dive.visibility}"]
    if dive.tags:
        out["tags"] = ["tags " + ", ".join(quote(t) for t in dive.tags)]
    if dive.site_uuid:
        out["divesiteid"] = [f"divesiteid {dive.site_uuid}"]
    for key in ("divemaster", "buddy", "suit", "notes"):
        value = getattr(dive, key)
        if value is not None:
            out[key] = [f"{key} {quote(value)}"]
    if dive.cylinders:
        out["cylinder"] = [render_cylinder(c) for c in dive.cylinders]
    if dive.weights_kg:
        out["weightsystem"] = [f"weightsystem weight={fmt_milli(w)}kg description={quote('weight')}" for w in dive.weights_kg]
    if dive.airtemp_c is not None:
        out["airtemp"] = [f"airtemp {fmt_milli(dive.airtemp_c)}°C"]
    if dive.watertemp_c is not None:
        out["watertemp"] = [f"watertemp {fmt_milli(dive.watertemp_c)}°C"]
    return out


def render_dive_lines(dive: Dive) -> List[str]:
    """Existing lines in their original order with the owned fields replaced
    in place (blocks such as cylinders replaced where the first one stood);
    new owned fields appended in Subsurface's order; unknown lines kept."""
    fields = _dive_field_lines(dive)
    out: List[str] = []
    done = set()
    for line in dive.lines:
        key = line.split(" ", 1)[0]
        if key in _OWNED_DIVE_KEYS:
            if key in done:
                continue
            done.add(key)
            out.extend(fields.get(key, []))
        else:
            out.append(line)
    for key in _DIVE_ORDER:
        if key in fields and key not in done:
            out.extend(fields[key])
    return out


def render_sample(sample: Sample) -> str:
    parts = [f"{sample.time_s // 60:3d}:{sample.time_s % 60:02d}"]
    if sample.depth_m is not None:
        parts.append(f"{fmt_milli(sample.depth_m)}m")
    if sample.temp_c is not None:
        parts.append(f"{fmt_milli(sample.temp_c)}°C")
    for sensor, bar in sorted(sample.pressures.items()):
        parts.append(f"{fmt_milli(bar)}bar:{sensor}")
    if sample.raw_tail:
        parts.append(sample.raw_tail)
    return " ".join(parts)


def render_dc_lines(dc: DiveComputer) -> List[str]:
    """Our own computers are rendered from the fields; a computer read from
    disk keeps its lines and only gets its ``dive_sync:`` extradata lines
    refreshed (inserted before the first event/sample)."""
    ours = [f'keyvalue {quote(k)} {quote(v)}' for k, v in dc.extradata if k.startswith(EXTERNAL_ID_PREFIX)]
    if dc.lines and dc.model != OUR_MODEL:
        out: List[str] = []
        inserted = False
        for line in dc.lines:
            if line.startswith("keyvalue "):
                _s, strings = split_line(line)
                if strings and strings[0].startswith(EXTERNAL_ID_PREFIX):
                    continue
            if not inserted and (line.startswith("event ") or line[:1] == " " or re.match(r"^\d+:\d+", line)):
                out.extend(ours)
                inserted = True
            out.append(line)
        if not inserted:
            out.extend(ours)
        return out
    out = [f"model {quote(dc.model or OUR_MODEL)}"]
    if dc.duration_s:
        out.append(f"duration {fmt_duration(dc.duration_s)} min")
    if dc.maxdepth_m is not None:
        out.append(f"maxdepth {fmt_milli(dc.maxdepth_m)}m")
    if dc.meandepth_m is not None:
        out.append(f"meandepth {fmt_milli(dc.meandepth_m)}m")
    if dc.airtemp_c is not None:
        out.append(f"airtemp {fmt_milli(dc.airtemp_c)}°C")
    if dc.watertemp_c is not None:
        out.append(f"watertemp {fmt_milli(dc.watertemp_c)}°C")
    out.extend(f'keyvalue {quote(k)} {quote(v)}' for k, v in dc.extradata)
    out.extend(render_sample(s) for s in dc.samples)
    return out


# ---------------------------------------------------------------------------
# Unified mapping
# ---------------------------------------------------------------------------

def _primary_dc(dive: Dive) -> Optional[DiveComputer]:
    return dive.computers[0] if dive.computers else None


def dive_to_unified(dive: Dive, sites: Dict[str, Site]) -> UnifiedDive:
    dc = _primary_dc(dive)
    site = sites.get(dive.site_uuid or "")
    samples = []
    max_depth = dc.maxdepth_m if dc and dc.maxdepth_m is not None else None
    if dc:
        for s in dc.samples:
            if s.depth_m is not None:
                samples.append(UnifiedSample(depth=s.depth_m, temp=s.temp_c, time=s.time_s))
        if max_depth is None and samples:
            max_depth = max(s.depth for s in samples)
    # Computer-downloaded dives keep pressures only in the samples (one
    # sensor per cylinder index); derive start/end from those when the
    # cylinder line has none.
    first_last: Dict[int, Tuple[float, float]] = {}
    if dc:
        for s in dc.samples:
            for sensor, bar in s.pressures.items():
                first_last[sensor] = (first_last.get(sensor, (bar, bar))[0], bar)
    tanks = []
    for idx, cyl in enumerate(dive.cylinders):
        start, end = cyl.start_bar, cyl.end_bar
        if (start is None or end is None) and idx in first_last:
            start = first_last[idx][0] if start is None else start
            end = first_last[idx][1] if end is None else end
        tanks.append(GasMixture(
            oxygen=cyl.o2 if cyl.o2 else 21.0,
            helium=cyl.he or 0.0,
            start_pressure=start,
            end_pressure=end,
            tank_volume=cyl.volume_l,
            tank_name=cyl.description or None,
            tank_role=USE_TO_TANK_ROLE.get(cyl.use),
        ))
    duration = dive.duration_s if dive.duration_s is not None else (dc.duration_s if dc and dc.duration_s else 0)
    water = dive.watertemp_c if dive.watertemp_c is not None else (dc.watertemp_c if dc else None)
    external_ids = {SERVICE_ID: dive.id}
    external_ids.update(dive.external_ids())
    return UnifiedDive(
        date_time=dive.when,
        duration=duration or 0,
        max_depth=max_depth or 0.0,
        avg_depth=dc.meandepth_m if dc else None,
        temp_min=water,
        external_ids=external_ids,
        gas_mixtures=tanks,
        location=site.name if site else None,
        notes=dive.notes or None,
        dive_number=dive.number,
        weight=sum(dive.weights_kg) if dive.weights_kg else None,
        weight_unit="kilogram" if dive.weights_kg else None,
        buddy=dive.buddy or None,
        lat=site.lat if site else None,
        lng=site.lng if site else None,
        samples=samples,
        service_fields={
            "suit": dive.suit,
            "divemaster": dive.divemaster,
            "rating": dive.rating,
            "visibility_stars": dive.visibility,
            "tags": list(dive.tags),
        },
    )


def apply_unified(dive: Dive, unified: UnifiedDive, repo: SubsurfaceRepo, write_samples: bool) -> None:
    """Copy the fields dive_sync owns from ``unified`` into ``dive``."""
    if unified.duration:
        dive.duration_s = int(unified.duration)
    dive.number = unified.dive_number if unified.dive_number else dive.number
    dive.buddy = unified.buddy if unified.buddy is not None else dive.buddy
    dive.notes = unified.notes if unified.notes is not None else dive.notes
    sf = unified.service_fields
    if "suit" in sf and sf["suit"] is not None:
        dive.suit = sf["suit"]
    if "divemaster" in sf and sf["divemaster"] is not None:
        dive.divemaster = sf["divemaster"]
    if sf.get("rating") is not None:
        dive.rating = int(sf["rating"])
    if sf.get("visibility_stars") is not None:
        dive.visibility = int(sf["visibility_stars"])
    if sf.get("tags") is not None:
        dive.tags = list(sf["tags"])
    if unified.weight is not None:
        kg = unified.weight / 2.20462 if (unified.weight_unit or "").lower().startswith("p") else unified.weight
        dive.weights_kg = [round(kg, 3)]
    dc0 = _primary_dc(dive)
    if unified.temp_min is not None and not (dc0 and dc0.watertemp_c is not None and abs(dc0.watertemp_c - unified.temp_min) < 0.05):
        # Subsurface writes a dive-level water temperature only when it differs from the computer's
        dive.watertemp_c = unified.temp_min
    if unified.gas_mixtures:
        current = dive_to_unified(dive, repo.sites).gas_mixtures if dive.lines else []
        if are_gas_mixtures_different(unified.gas_mixtures, current):
            rebuilt = []
            for idx, g in enumerate(unified.gas_mixtures):
                old = dive.cylinders[idx] if idx < len(dive.cylinders) else Cylinder()
                use = TANK_ROLE_TO_USE.get(g.tank_role, old.use) if g.tank_role is not None else old.use
                rebuilt.append(Cylinder(
                    volume_l=g.tank_volume if g.tank_volume is not None else old.volume_l,
                    workpressure_bar=old.workpressure_bar,
                    description=g.tank_name or old.description,
                    o2=None if (g.oxygen is None or abs(g.oxygen - 21.0) < 0.05) else g.oxygen,
                    he=g.helium or None,
                    start_bar=g.start_pressure, end_bar=g.end_pressure,
                    use=use, depth_m=old.depth_m,
                ))
            dive.cylinders = rebuilt
    site = repo.resolve_site(unified.location, unified.lat, unified.lng)
    if site is not None:
        dive.site_uuid = site.uuid
    # our own computer carries depths, samples and the foreign ids
    ours = next((dc for dc in dive.computers if dc.model == OUR_MODEL), None)
    if ours is None and (write_samples or not dive.computers):
        ours = DiveComputer(filename=f"Divecomputer-{len(dive.computers):03d}" if dive.computers else "Divecomputer",
                            model=OUR_MODEL)
        dive.computers.append(ours)
    if ours is not None:
        if write_samples or not ours.samples:
            ours.maxdepth_m = unified.max_depth or None
            ours.meandepth_m = unified.avg_depth
            ours.watertemp_c = unified.temp_min
            ours.duration_s = int(unified.duration) if unified.duration else None
            ours.samples = [Sample(time_s=int(s.time or 0), depth_m=s.depth, temp_c=s.temp) for s in unified.samples]
    target_dc = ours or dive.computers[0]
    foreign = {k: v for k, v in unified.external_ids.items() if k != SERVICE_ID and v}
    target_dc.extradata = [(k, v) for k, v in target_dc.extradata if not k.startswith(EXTERNAL_ID_PREFIX)]
    target_dc.extradata.extend((EXTERNAL_ID_PREFIX + k, str(v)) for k, v in sorted(foreign.items()))


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class SubsurfaceAdapter(BaseDiveAdapter):
    """Reads and writes a Subsurface git-storage directory."""

    service_id = SERVICE_ID
    display_name = "Subsurface"
    stores_external_ids = True

    @classmethod
    def field_catalog(cls) -> List[FieldSpec]:
        return [
            FieldSpec(key="subsurface.date_time", label="Start time", type="datetime", unified="date_time", writable=False),
            FieldSpec(key="subsurface.duration", label="Duration", type="number", unified="duration", unit="s"),
            FieldSpec(key="subsurface.max_depth", label="Max depth", type="number", unified="max_depth", unit="m", writable=False),
            FieldSpec(key="subsurface.avg_depth", label="Mean depth", type="number", unified="avg_depth", unit="m", writable=False),
            FieldSpec(key="subsurface.temp_min", label="Water temperature", type="number", unified="temp_min", unit="°C"),
            FieldSpec(key="subsurface.dive_number", label="Dive number", type="number", unified="dive_number"),
            FieldSpec(key="subsurface.location", label="Dive site", type="text", unified="location"),
            FieldSpec(key="subsurface.notes", label="Notes", type="text", unified="notes"),
            FieldSpec(key="subsurface.buddy", label="Buddy", type="text", unified="buddy"),
            FieldSpec(key="subsurface.weight", label="Weight", type="number", unified="weight"),
            FieldSpec(key="subsurface.gps", label="Site position", type="gps", unified="gps"),
            FieldSpec(key="subsurface.tanks", label="Cylinders", type="tanks", unified="tanks"),
            FieldSpec(key="subsurface.samples", label="Dive profile", type="samples", unified="samples"),
            FieldSpec(key="subsurface.suit", label="Suit", type="text"),
            FieldSpec(key="subsurface.divemaster", label="Dive guide", type="text"),
            FieldSpec(key="subsurface.rating", label="Rating (0-5)", type="number"),
            FieldSpec(key="subsurface.visibility_stars", label="Visibility (0-5)", type="number"),
            FieldSpec(key="subsurface.tags", label="Tags", type="list"),
        ]

    def __init__(self, path: str, site_match_radius_m: float = 200.0):
        self.path = path
        self.site_match_radius_m = site_match_radius_m
        self.repo = SubsurfaceRepo(path)
        self.loaded = False

    def login(self) -> bool:
        if not os.path.isdir(self.path):
            logger.error("Subsurface directory does not exist: %s", self.path)
            return False
        self.repo.load()
        self.loaded = True
        return True

    def _ensure(self) -> None:
        if not self.loaded and not self.login():
            raise RuntimeError(f"Subsurface directory not available: {self.path}")

    def fetch_dives(self, date_from: Optional[datetime] = None, date_to: Optional[datetime] = None) -> List[UnifiedDive]:
        self._ensure()
        self.repo.load()
        out = []
        for dive in self.repo.dives:
            if date_from and dive.when < date_from:
                continue
            if date_to and dive.when > date_to:
                continue
            out.append(dive_to_unified(dive, self.repo.sites))
        return out

    def add_dive(self, dive: UnifiedDive) -> Optional[str]:
        self._ensure()
        when = dive.date_time.replace(microsecond=0)
        rel = f"{when.year:04d}/{when.month:02d}/{self.repo.dive_dir_name(when)}"
        if os.path.exists(os.path.join(self.path, rel)):
            rel += "~" + secrets.token_hex(4)[:7]
        new = Dive(dir_path=rel, when=when, number=dive.dive_number)
        apply_unified(new, dive, self.repo, write_samples=True)
        self.repo.write_dive(new)
        self.repo.dives.append(new)
        logger.info("Subsurface: added dive %s", new.id)
        return new.id

    def update_dive(self, external_id: str, dive: UnifiedDive) -> bool:
        self._ensure()
        existing = self.repo.find(external_id)
        if existing is None:
            logger.error("Subsurface: no dive %s", external_id)
            return False
        apply_unified(existing, dive, self.repo, write_samples=False)
        self.repo.write_dive(existing)
        logger.info("Subsurface: updated dive %s", existing.id)
        return True

    def delete_dive(self, external_id: str) -> bool:
        self._ensure()
        existing = self.repo.find(external_id)
        if existing is None:
            return False
        self.repo.delete_dive(existing)
        logger.info("Subsurface: deleted dive %s", external_id)
        return True
