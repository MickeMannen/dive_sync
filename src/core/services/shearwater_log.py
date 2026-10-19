"""Decoder for the Shearwater computer's own dive log (rework.md Track H, H5).

The Shearwater app keeps each dive's profile only as the
computer's native log, in ``log_data.data_bytes_1``: a 4-byte little-endian
uncompressed length followed by a gzip stream of the **Petrel Native Format
(PNF)** - a run of 32-byte records, byte 0 being the record type. The layout
is libdivecomputer's (``shearwater_predator_parser.c``, PNF branch), checked
against the 27 fixture dives in ``tests/data/shearwater/dive_data.db``
(rework.md "Native log format"):

* ``0x10``-``0x19`` opening records 0-9 and ``0x20``-``0x29`` closing records
  0-9 (the header: units, log version, dive mode, sample interval, dive
  time, max depth, gases, GF, surface pressure);
* ``0x01`` dive sample (``0x03`` Avelo sample, same layout): depth,
  temperature, PPO2, gas, NDL/stop, TTS, CNS, GF99, the two transmitters'
  pressures; ``0xE1`` extended sample: transmitters 3/4 (log version 13+),
  belonging to the ``0x01`` before it;
* ``0x02`` freedive samples (four 8-byte samples per record, no gas, depth
  as absolute pressure) - such a log gives no profile here;
* ``0x30`` info event, ``0xFF`` final record (model, serial), padding and
  record types libdivecomputer ignores (``0x51``, ``0x70``-``0x75``,
  ``0x80``-``0x87``, ``0xA0``/``0xA1``).

Sample *k* (1-based) is at *k* x interval: the computer's first sample is
one interval after the start, and it keeps logging about 60 s after
surfacing; every sample is carried as logged (decisions 2026-09-30: no
synthetic start point, no trimming, no thinning).

Where the layout comes from, so a change upstream is easy to spot:
libdivecomputer ``src/shearwater_predator_parser.c`` (LGPL-2.1, Jef Driesen;
https://github.com/libdivecomputer/libdivecomputer), ``master`` as fetched on
2026-09-30 (sha256 of that file starts ``8375e0bc0ef5c656``). The C code
addresses fields relative to the record *after* its type byte; here ``p = 1``
is that shift. The fields read, in libdivecomputer's terms::

    header   logversion      opening[4] + 16          units (0 = metric)  opening[0] + 8
             dive mode       opening[4] + 1  (lv >= 8) sample interval    opening[5] + 23 ms (lv >= 9, else 10 s)
             max depth       closing[0] + 4  (/10)     dive time          closing[0] + 6  (24 bit, s)
             start / end     opening[0] + 12, closing[0] + 12 (unix)
             gases           opening[4] + 20.. (O2, He x10), gas on/off   opening[4] + 17
             GF low / high   opening[1] + 4/5          surface pressure   opening[1] + 16 (mbar)
             model / serial  final record + 13 / + 2
    sample   depth           p + 0  (u16, /10)         temperature        p + 13 (s8; < 0: + 102)
             deco stop       p + 2  (u16, /10)         TTS                p + 4  (u16, min)
             PPO2            p + 6  (/100)             O2 / He            p + 7 / p + 8
             NDL or stop min p + 9                     status byte        p + 11 (OC = 0x10, SC = 0x08)
             setpoint        p + 18 (/100)             gas time remaining p + 21 (>= 0xF0 = none)
             CNS             p + 22 (/100)             GF99               p + 24 (255 = none)
             transmitter 1/2 p + 27 / p + 19 (u16 psi x2; >= 0xFFF0 = status, lv >= 7)
             0xE1 record     transmitters 3/4 at 1 / 3 (lv >= 13), attached to the 0x01 before it
    cells    calibrated mask opening[3] + 6 (bit n = cell n) and the three calibration words
             opening[3] + 7 / 9 / 11 (u16, bar per mV x 100000; 2100 is the factory default)
             cell 1/2/3 mV   p + 12 / p + 14 / p + 15, on a closed-circuit sample (OC bit clear) whose
             PPO2 is internal (status bit 0x02 clear); bar = mV x the cell's calibration

The cells (plans/gap-roadmap.md R18b): ``shearwater_predator_parser_cache``
reads the calibration block (``data[base]`` the mask, ``base + 1 + 2i`` the
words) and ``samples_foreach`` reports cell *i* only when its mask bit is
set; when every calibrated cell still holds the factory default (2100) the
parser treats them all as uncalibrated and reports none ("to avoid
returning incorrect ppO2 values"), which this decoder follows (a Petrel 3 is
known to write the default for every cell - the computer's own voted PPO2
at p + 6 stays). The Predator's own correction of the factor does not apply:
a Predator writes the legacy 128-byte layout, which this decoder refuses.
The calibration block sits in opening record 3, three bytes after the
density: the mask, then one big-endian word per cell. A real Petrel 3 CCR
dive in the app's database (local test data, 2026-10-20) reads with its
mode, setpoint and PPO2, and with no cells, as its calibration is the
factory default. **Per-cell PO2 is untested against a calibrated
Shearwater CCR dive**; the ``PPO2_EXTERNAL`` bit value (0x02) is
libdivecomputer's as remembered, not read from a file.

To check against a newer libdivecomputer: diff its ``shearwater_predator_parser.c``
against the fetched revision, and compare every offset above with the
``dc_parser_samples_foreach`` / ``dc_parser_get_field`` code for the PNF
branch (``pnf`` / ``logversion`` conditions). ``tests/test_shearwater_log.py``
pins the fixture's 27 dives, so a wrong offset shows up there.

Pure Python (``gzip``, ``struct``), no SQLite: the adapter hands in the blob
(plus which tank each transmitter slot is, and the dive's gases) and gets a
:class:`DecodedLog` back - the ``UnifiedSample``s with every channel the
computer logged (``SampleChannels``: every transmitter's pressure keyed by
tank, NDL, TTS, the deco stop, CNS, ppO2, the setpoint and the calibrated O2
cells on a rebreather, GF99, the gas time remaining; plans/gap-roadmap.md R5,
R18b), the gas switches and
circuit changes as ``DiveEvent``s, and the header's carried fields (computer,
gradient factors, dive mode, water). Units are converted to the models' (the
log has minutes and a CNS fraction; the models seconds and percent). Any
structural problem raises :class:`ShearwaterLogError`; the adapter turns that
into "no samples" plus one warning, so a profile can never make loading a dive
fail.
"""
from __future__ import annotations

import gzip
import logging
import re
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.core.models import (
    EVENT_GAS_SWITCH,
    EVENT_MODE_CHANGE,
    DiveEvent,
    GasMixture,
    SampleChannels,
    UnifiedSample,
)

logger = logging.getLogger(__name__)

RECORD = 32
FEET = 0.3048
PSI_PER_BAR = 14.5037738          # the same constant as shearwater.py's PSI text columns
LATEST_KNOWN_LOG_VERSION = 17     # the fixture's newest (Perdix 2, firmware of 2025)

# record types
OPENING_0, OPENING_9 = 0x10, 0x19
CLOSING_0, CLOSING_9 = 0x20, 0x29
DIVE_SAMPLE = 0x01
FREEDIVE_SAMPLE = 0x02
AVELO_SAMPLE = 0x03
INFO_EVENT = 0x30
DIVE_SAMPLE_EXT = 0xE1
FINAL = 0xFF

# dive modes (opening 4, byte 1, log version 8+)
DIVE_MODES = {0: "CC", 1: "OC tec", 2: "gauge", 3: "PPO2", 4: "SC", 5: "CC2", 6: "OC rec", 7: "freedive", 12: "Avelo"}
MODE_FREEDIVE = 7
MODE_SC = 4
MODES_CC = (0, 5)
MODES_OC = (1, 6, 12)
MODE_GAUGE = 2

# the final record's model byte -> the computer, as libdivecomputer's
# shearwater_common.h names them (the fixture's 11 is the owner's Perdix 2)
MODELS = {2: "Predator", 3: "Petrel", 4: "Nerd", 5: "Perdix", 6: "Perdix AI", 7: "Nerd 2", 8: "Teric",
          9: "Peregrine", 10: "Petrel 3", 11: "Perdix 2", 12: "Tern", 13: "Peregrine TX"}
VENDOR = "Shearwater"

# opening record 3's water density (kg/m3) -> UnifiedDive.water_type: the
# computer's three salinity settings; any other density is a custom one
WATER_TYPES = {1000: "fresh", 1020: "en13319", 1030: "salt"}

# status byte bits of a sample
STATUS_OC = 0x10
STATUS_SC = 0x08
STATUS_PPO2_EXTERNAL = 0x02      # the PPO2 came from an external monitor: no cell bytes (libdivecomputer's PPO2_EXTERNAL, from memory)

# a rebreather's O2 cells (plans/gap-roadmap.md R18b): the three mV bytes of a
# closed-circuit sample (libdivecomputer's offsets, shifted by the type byte
# as every sample field here), and the header's calibration block in opening
# record 3 - the mask byte, then one u16 word per cell in bar per mV x 100000
CELL_OFFSETS = (12, 14, 15)
CELL_CALIBRATION_AT = 6           # opening[3] + 6 = the mask, + 7 / 9 / 11 the words
CELL_CALIBRATION_SCALE = 100000.0
CELL_CALIBRATION_DEFAULT = 2100   # the factory default word: every calibrated cell at it = cells not calibrated

# sanity check of the samples against the header (rework.md "H5 design")
DEPTH_TOLERANCE_M = 1.0
TIME_TOLERANCE_S = 30


class ShearwaterLogError(ValueError):
    """The blob is not a decodable Shearwater PNF log (or has no profile)."""


@dataclass
class ShearwaterSample:
    """One ``0x01``/``0x03`` record, units converted (m, °C, bar, seconds)."""
    time: int                          # seconds since the start
    depth: float                       # m
    temp: float                        # °C
    ppo2: float                        # bar (OC: of the gas breathed)
    o2: int                            # % of the gas breathed
    he: int
    ccr: bool                          # status byte: not open circuit
    deco_stop_depth: float             # m, 0 = no deco
    ndl_or_stop_min: int               # NDL, or the stop time when in deco (99 = cap)
    tts_min: int
    cns: float                         # fraction (0.12 = 12 %)
    setpoint: float                    # bar
    gf99: Optional[int]                # %, None when the computer had none (255)
    gas_time_min: Optional[int]        # remaining gas time, None when not available (>= 0xF0)
    pressures: Dict[int, float] = field(default_factory=dict)   # transmitter index (0 = T1) -> bar
    cell_mv: Dict[int, int] = field(default_factory=dict)       # cell index (0 = cell 1) -> raw mV; only a closed-circuit sample with an internal PPO2 (R18b)
    cells: Dict[int, float] = field(default_factory=dict)       # cell index -> bar, the calibrated cells only (SampleChannels.o2_cells)


@dataclass
class ShearwaterLog:
    """The header fields and the samples of one log."""
    log_version: int
    units: str                          # "metric" | "imperial"
    dive_mode: Optional[int]            # DIVE_MODES key, None before log version 8
    interval_ms: int
    dive_time: int                      # s, closing record 0
    max_depth: float                    # m, closing record 0
    start_ticks: int                    # unix seconds, local time as if UTC
    end_ticks: int
    gf_low: int
    gf_high: int
    o2: List[int]                       # the 10 programmed gases
    he: List[int]
    gas_enabled: List[bool]
    surface_pressure_mbar: int
    density: int
    ai_mode: int
    model: Optional[int]
    serial: Optional[int]
    cell_calibrated: Tuple[bool, bool, bool] = (False, False, False)   # the header's mask, as written (R18b)
    cell_calibration: Tuple[float, float, float] = (0.0, 0.0, 0.0)     # bar per mV per cell, as written
    cells_at_default: bool = False     # every calibrated cell holds the factory default: libdivecomputer reports no cells, nor does this
    samples: List[ShearwaterSample] = field(default_factory=list)
    gas_switches: List[Tuple[int, int, int]] = field(default_factory=list)   # (time s, o2 %, he %); the first is the starting gas
    info_events: List[Tuple[int, int, int, int]] = field(default_factory=list)   # (event, w0, w1, w2)
    freedive_records: int = 0

    @property
    def dive_mode_name(self) -> str:
        return DIVE_MODES.get(self.dive_mode, f"mode {self.dive_mode}") if self.dive_mode is not None else "unknown"

    @property
    def is_freedive(self) -> bool:
        return self.dive_mode == MODE_FREEDIVE or (self.freedive_records > 0 and not self.samples)

    @property
    def cell_factors(self) -> Dict[int, float]:
        """Bar per mV of each cell whose reading is reported: the cells the
        header marks calibrated, none when they all hold the factory default
        (libdivecomputer's rule, R18b)."""
        if self.cells_at_default:
            return {}
        return {i: f for i, (on, f) in enumerate(zip(self.cell_calibrated, self.cell_calibration)) if on}


# ---------------------------------------------------------------------------
# Byte helpers
# ---------------------------------------------------------------------------

def _u16(b: bytes, o: int) -> int:
    return struct.unpack_from(">H", b, o)[0]


def _u24(b: bytes, o: int) -> int:
    return (b[o] << 16) | (b[o + 1] << 8) | b[o + 2]


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from(">I", b, o)[0]


def _s8(b: bytes, o: int) -> int:
    return struct.unpack_from("b", b, o)[0]


def psi2_to_bar(word: int) -> Optional[float]:
    """A transmitter word of a sample: ``>= 0xFFF0`` are status codes (AI off,
    no comms, not paired), otherwise the top 4 bits are the battery level and
    the low 12 bits the pressure in units of 2 psi; 0 = no reading."""
    if word >= 0xFFF0:
        return None
    raw = word & 0x0FFF
    if not raw:
        return None
    return round(raw * 2 / PSI_PER_BAR, 2)


# ---------------------------------------------------------------------------
# The blob
# ---------------------------------------------------------------------------

def unpack_blob(blob: Optional[bytes]) -> bytes:
    """``log_data.data_bytes_1`` -> the raw log. The 4-byte little-endian
    prefix must equal the gunzipped length."""
    if not blob or len(blob) < 4:
        raise ShearwaterLogError("profile blob is empty or shorter than its length prefix")
    expected = struct.unpack_from("<I", blob, 0)[0]
    try:
        raw = gzip.decompress(bytes(blob[4:]))
    except (OSError, EOFError, zlib.error, ValueError) as e:
        raise ShearwaterLogError(f"profile blob is not a gzip stream ({e})") from e
    if len(raw) != expected:
        raise ShearwaterLogError(f"profile blob declares {expected} bytes but holds {len(raw)}")
    return raw


# ---------------------------------------------------------------------------
# The log
# ---------------------------------------------------------------------------

def _parse_sample(rec: bytes, time_s: int, imperial: bool, log_version: int,
                  cell_factors: Optional[Mapping[int, float]] = None) -> ShearwaterSample:
    """One ``0x01``/``0x03`` record; offsets are libdivecomputer's, shifted by
    the PNF type byte (``p``). ``cell_factors`` (cell index -> bar per mV,
    ``ShearwaterLog.cell_factors``) turns the three cell bytes of a
    closed-circuit sample into ``cells``; the raw mV of every cell is kept
    in ``cell_mv`` whether or not it is calibrated (R18b)."""
    p = 1
    depth = _u16(rec, p) / 10.0
    temp = float(_s8(rec, p + 13))
    if temp < 0:
        # libdivecomputer: "fix negative temperatures"
        temp += 102
        if temp > 0:
            temp = 0.0
    stop = float(_u16(rec, p + 2))
    if imperial:
        depth *= FEET
        stop *= FEET
        temp = (temp - 32.0) * 5.0 / 9.0
    status = rec[p + 11] if rec[0] != AVELO_SAMPLE else STATUS_OC
    gf99 = rec[p + 24]
    gtr = rec[p + 21]
    pressures: Dict[int, float] = {}
    if log_version >= 7:
        # transmitter 1 at 27, transmitter 2 at 19 (the Avelo sample has one)
        slots = (27,) if rec[0] == AVELO_SAMPLE else (27, 19)
        for idx, at in enumerate(slots):
            bar = psi2_to_bar(_u16(rec, p + at))
            if bar is not None:
                pressures[idx] = bar
    ccr = (status & STATUS_OC) == 0
    cell_mv: Dict[int, int] = {}
    cells: Dict[int, float] = {}
    if ccr and not (status & STATUS_PPO2_EXTERNAL):
        # the cell bytes mean something only on a closed-circuit sample whose
        # PPO2 the computer measured itself (libdivecomputer: `if (ccr)` and
        # `(status & PPO2_EXTERNAL) == 0`); a calibrated cell's mV x its
        # factor is its PO2 in bar, 0 mV carried as 0.0 as the parser does
        for idx, at in enumerate(CELL_OFFSETS):
            cell_mv[idx] = rec[p + at]
            factor = (cell_factors or {}).get(idx)
            if factor is not None:
                cells[idx] = round(cell_mv[idx] * factor, 3)
    return ShearwaterSample(
        time=time_s,
        depth=round(depth, 2),
        temp=round(temp, 2),
        ppo2=rec[p + 6] / 100.0,
        o2=rec[p + 7],
        he=rec[p + 8],
        ccr=ccr,
        deco_stop_depth=round(stop, 2),
        ndl_or_stop_min=rec[p + 9],
        tts_min=_u16(rec, p + 4),
        cns=rec[p + 22] / 100.0,
        setpoint=rec[p + 18] / 100.0,
        gf99=None if gf99 == 255 else gf99,
        gas_time_min=None if gtr >= 0xF0 else gtr,
        pressures=pressures,
        cell_mv=cell_mv,
        cells=cells,
    )


def _cell_calibration(raw: bytes, o3: int) -> Tuple[Tuple[bool, bool, bool], Tuple[float, float, float], bool]:
    """Opening record 3's calibration block (R18b): the mask (bit n = cell n
    calibrated), the three words as bar per mV, and whether every calibrated
    cell still holds the factory default word - libdivecomputer then
    disables them all rather than report a PO2 computed from a default."""
    base = o3 + CELL_CALIBRATION_AT
    mask = raw[base]
    words = tuple(_u16(raw, base + 1 + 2 * i) for i in range(3))
    calibrated = tuple(bool(mask & (1 << i)) for i in range(3))
    factors = tuple(w / CELL_CALIBRATION_SCALE for w in words)
    on = [w for w, c in zip(words, calibrated) if c]
    at_default = bool(on) and all(w == CELL_CALIBRATION_DEFAULT for w in on)
    return calibrated, factors, at_default  # type: ignore[return-value]


def parse_log(raw: bytes) -> ShearwaterLog:
    """The gunzipped log -> header fields and samples. Raises
    :class:`ShearwaterLogError` for anything that is not a PNF log with the
    records libdivecomputer requires (opening and closing 0-4)."""
    if raw is None or len(raw) < 2:
        raise ShearwaterLogError("log is empty")
    if _u16(raw, 0) == 0xFFFF:
        raise ShearwaterLogError("log is in the legacy Predator layout (128-byte blocks), which is not decoded")
    if len(raw) % RECORD:
        raise ShearwaterLogError(f"log length {len(raw)} is not a multiple of {RECORD}-byte records")

    opening: Dict[int, int] = {}
    closing: Dict[int, int] = {}
    final: Optional[int] = None
    for off in range(0, len(raw), RECORD):
        t = raw[off]
        if OPENING_0 <= t <= OPENING_9:
            opening[t - OPENING_0] = off
        elif CLOSING_0 <= t <= CLOSING_9:
            closing[t - CLOSING_0] = off
        elif t == FINAL:
            final = off
    missing = [f"opening {i}" for i in range(5) if i not in opening] + [f"closing {i}" for i in range(5) if i not in closing]
    if missing:
        raise ShearwaterLogError("log lacks record(s) " + ", ".join(missing))

    o0, o1, o3, o4 = opening[0], opening[1], opening[3], opening[4]
    c0 = closing[0]
    log_version = raw[o4 + 16]
    if log_version > LATEST_KNOWN_LOG_VERSION:
        logger.warning("Shearwater: log version %d is newer than the %d this decoder was checked against; "
                       "decoding it the same way - compare the profile with the app's graph",
                       log_version, LATEST_KNOWN_LOG_VERSION)
    imperial = raw[o0 + 8] == 1
    dive_mode = raw[o4 + 1] if log_version >= 8 else None
    interval_ms = _u16(raw, opening[5] + 23) if (log_version >= 9 and 5 in opening) else 10000
    if interval_ms <= 0:
        raise ShearwaterLogError("log has a zero sample interval")
    max_depth = _u16(raw, c0 + 4) / 10.0 * (FEET if imperial else 1.0)
    gas_state = _u16(raw, o4 + 17)
    cell_calibrated, cell_calibration, cells_at_default = _cell_calibration(raw, o3)
    if cells_at_default:
        logger.info("Shearwater: every calibrated O2 cell holds the factory default calibration (%d); "
                    "the per-cell readings are left out as libdivecomputer does, the computer's own PPO2 stays",
                    CELL_CALIBRATION_DEFAULT)
    log = ShearwaterLog(
        log_version=log_version,
        units="imperial" if imperial else "metric",
        dive_mode=dive_mode,
        interval_ms=interval_ms,
        dive_time=_u24(raw, c0 + 6),
        max_depth=round(max_depth, 2),
        start_ticks=_u32(raw, o0 + 12),
        end_ticks=_u32(raw, c0 + 12),
        gf_low=raw[o0 + 4],
        gf_high=raw[o0 + 5],
        o2=[raw[o0 + 20 + i] for i in range(10)],
        he=[raw[o0 + 30], raw[o0 + 31]] + [raw[o1 + 1 + i] for i in range(8)],
        gas_enabled=[bool(gas_state & (1 << i)) for i in range(10)],
        surface_pressure_mbar=_u16(raw, o1 + 16),
        density=_u16(raw, o3 + 3),
        ai_mode=raw[o4 + 28] if log_version >= 7 else 0,
        model=raw[final + 13] if final is not None else None,
        serial=_u32(raw, final + 2) if final is not None else None,
        cell_calibrated=cell_calibrated,
        cell_calibration=cell_calibration,
        cells_at_default=cells_at_default,
    )
    cell_factors = log.cell_factors

    time_ms = 0
    previous: Optional[Tuple[int, int, bool]] = None
    for off in range(0, len(raw), RECORD):
        rec = raw[off:off + RECORD]
        t = rec[0]
        if t in (DIVE_SAMPLE, AVELO_SAMPLE):
            time_ms += interval_ms
            sample = _parse_sample(rec, time_ms // 1000, imperial, log_version, cell_factors)
            key = (sample.o2, sample.he, sample.ccr)
            if key != previous and (sample.o2 or sample.he):
                log.gas_switches.append((sample.time, sample.o2, sample.he))
                previous = key
            log.samples.append(sample)
        elif t == DIVE_SAMPLE_EXT:
            if log_version >= 13 and log.samples:
                for i in range(2):
                    bar = psi2_to_bar(_u16(rec, 1 + i * 2))
                    if bar is not None:
                        log.samples[-1].pressures[2 + i] = bar
        elif t == FREEDIVE_SAMPLE:
            log.freedive_records += 1
        elif t == INFO_EVENT:
            log.info_events.append((rec[1], _u32(rec, 4), _u32(rec, 8), _u32(rec, 12)))
    return log


# ---------------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------------

def primary_transmitter(log: ShearwaterLog) -> Optional[int]:
    """The lowest transmitter index with any reading, whose pressures go into
    ``UnifiedSample.pressure`` (the one pressure the sync services exchange);
    every transmitter's reading is in the sample's channels."""
    seen = {idx for s in log.samples for idx in s.pressures}
    return min(seen) if seen else None


def check_against_header(log: ShearwaterLog) -> None:
    """The samples must agree with the header's max depth (within 1 m) and
    dive time (the last under-water sample within 3 intervals + 30 s of
    it); a disagreement means the records were not read as the computer
    wrote them, and the profile is better left out than shown wrong."""
    if not log.samples:
        raise ShearwaterLogError("log has no dive samples")
    deepest = max(s.depth for s in log.samples)
    if abs(deepest - log.max_depth) > DEPTH_TOLERANCE_M:
        raise ShearwaterLogError(f"samples reach {deepest:.1f} m but the header says {log.max_depth:.1f} m")
    under = [s.time for s in log.samples if s.depth > 0]
    last = under[-1] if under else 0
    if abs(last - log.dive_time) > 3 * log.interval_ms / 1000 + TIME_TOLERANCE_S:
        raise ShearwaterLogError(f"last under-water sample at {last} s but the header says the dive took {log.dive_time} s")


def sample_channels(s: ShearwaterSample, tank_of_transmitter: Optional[Mapping[int, int]] = None) -> SampleChannels:
    """One sample's channels in the models' units (plans/gap-roadmap.md R5):
    minutes become seconds, the CNS fraction a percentage, GF99 a float. The
    computer logs one number for NDL *or* the stop time: with a stop depth it
    is the stop's time, otherwise the NDL (99 = the display's cap, carried
    as logged). The setpoint byte is always written but means something only
    on a closed or semi-closed circuit sample. A transmitter's pressure is
    keyed by its tank through ``tank_of_transmitter`` (slot index -> index in
    ``gas_mixtures``); a transmitter the map does not name keeps its slot
    index, unless a named one already holds it. A rebreather's calibrated
    cells go into ``o2_cells`` in bar, keyed by cell index (R18b)."""
    tank_of = tank_of_transmitter or {}
    in_deco = s.deco_stop_depth > 0
    minutes = s.ndl_or_stop_min * 60
    pressures = {tank_of[idx]: bar for idx, bar in sorted(s.pressures.items()) if idx in tank_of}
    for idx, bar in sorted(s.pressures.items()):
        if idx not in tank_of:
            pressures.setdefault(idx, bar)
    return SampleChannels(
        pressures=pressures,
        ndl=None if in_deco else minutes,
        tts=s.tts_min * 60,
        deco_stop_depth=s.deco_stop_depth,
        deco_stop_time=minutes if in_deco else None,
        cns=round(s.cns * 100.0, 1),
        ppo2=s.ppo2,
        setpoint=s.setpoint if s.ccr else None,
        o2_cells=dict(s.cells),
        gf99=float(s.gf99) if s.gf99 is not None else None,
        gas_time=s.gas_time_min * 60 if s.gas_time_min is not None else None,
    )


def profile_samples(log: ShearwaterLog, check: bool = True,
                    tank_of_transmitter: Optional[Mapping[int, int]] = None) -> List[UnifiedSample]:
    """The log's samples as ``UnifiedSample``s: time, depth, temperature,
    the primary transmitter's pressure and every channel the computer logged
    (``sample_channels``), every sample as logged (including the surface
    tail). Freedive logs have no depth samples and raise."""
    if log.is_freedive:
        raise ShearwaterLogError("freedive log: its samples are not decoded")
    if check:
        check_against_header(log)
    elif not log.samples:
        raise ShearwaterLogError("log has no dive samples")
    tank = primary_transmitter(log)
    return [UnifiedSample(depth=s.depth, temp=s.temp, time=s.time,
                          pressure=s.pressures.get(tank) if tank is not None else None,
                          channels=sample_channels(s, tank_of_transmitter))
            for s in log.samples]


def mixes_breathed(log: ShearwaterLog) -> List[Tuple[int, int]]:
    """The distinct (O2, He) mixes the samples record, in the order breathed."""
    out: List[Tuple[int, int]] = []
    for s in log.samples:
        mix = (s.o2, s.he)
        if (s.o2 or s.he) and mix not in out:
            out.append(mix)
    return out


def unified_dive_mode(log: ShearwaterLog) -> Optional[str]:
    """The header's dive mode in ``UnifiedDive.dive_mode``'s words: CC/CC2
    are ``ccr``, SC ``scr``, gauge ``gauge``, freedive ``apnea``; an open
    circuit mode (OC rec, OC tec, Avelo) is ``oc_multi_gas`` when more than
    one mix was breathed, else ``oc_single_gas`` (as the UDDF reader
    decides it). PPO2 mode and an unknown byte give None."""
    mode = log.dive_mode
    if mode is None:
        return None
    if mode in MODES_CC:
        return "ccr"
    if mode == MODE_SC:
        return "scr"
    if mode == MODE_GAUGE:
        return "gauge"
    if mode == MODE_FREEDIVE:
        return "apnea"
    if mode in MODES_OC:
        return _oc_mode(log)
    return None


def _oc_mode(log: ShearwaterLog) -> str:
    return "oc_multi_gas" if len(mixes_breathed(log)) > 1 else "oc_single_gas"


def gas_index_of(o2: float, he: float, gases: Sequence[GasMixture]) -> Optional[int]:
    """The first ``gas_mixtures`` entry with this mix, or None (a mix the
    tanks do not name, e.g. a deco gas without a transmitter)."""
    for i, gas in enumerate(gases):
        if abs((gas.oxygen or 0.0) - o2) < 0.5 and abs((gas.helium or 0.0) - he) < 0.5:
            return i
    return None


def profile_events(log: ShearwaterLog, gases: Sequence[GasMixture] = ()) -> List[DiveEvent]:
    """The gas switches and circuit changes the samples record, in time
    order (plans/gap-roadmap.md R5). A ``gas_switch`` event is written at
    every sample whose mix differs from the previous one's, with the mix and
    the ``gas_mixtures`` index that holds it (when one does); the starting
    gas is an event only when it is not the first tank's mix, as Subsurface
    keeps it. A sample whose circuit differs from the previous one's (a
    rebreather diver bailing out to open circuit, or back) is a
    ``mode_change`` named in ``UnifiedDive.dive_mode``'s words."""
    out: List[DiveEvent] = []
    previous_mix: Optional[Tuple[int, int]] = None
    previous_ccr: Optional[bool] = None
    oc_name = _oc_mode(log)
    cc_name = "scr" if log.dive_mode == MODE_SC else "ccr"
    for s in log.samples:
        mix = (s.o2, s.he)
        if (s.o2 or s.he) and mix != previous_mix:
            tank = gas_index_of(float(s.o2), float(s.he), gases)
            if previous_mix is not None or (gases and tank != 0):
                out.append(DiveEvent(time=s.time, type=EVENT_GAS_SWITCH, tank=tank, oxygen=float(s.o2), helium=float(s.he)))
            previous_mix = mix
        if previous_ccr is not None and s.ccr != previous_ccr:
            out.append(DiveEvent(time=s.time, type=EVENT_MODE_CHANGE, name=cc_name if s.ccr else oc_name))
        previous_ccr = s.ccr
    return out


def carried_fields(log: ShearwaterLog) -> Dict[str, Any]:
    """The header's values for the carried ``UnifiedDive`` fields
    (``CARRIED_DIVE_FIELDS``): the computer (vendor, model by ``MODELS``,
    the serial as the app shows it, 8 hex digits), the gradient factors, the
    dive mode and the water setting. Only the keys with a value."""
    fields = dict(
        computer_vendor=VENDOR,
        computer_model=MODELS.get(log.model) if log.model is not None else None,
        computer_serial=f"{log.serial:08X}" if log.serial is not None else None,
        gf_low=log.gf_low or None,
        gf_high=log.gf_high or None,
        dive_mode=unified_dive_mode(log),
        water_type=(WATER_TYPES.get(log.density, "custom") if log.density else None),
        water_density=float(log.density) if log.density else None,
    )
    return {k: v for k, v in fields.items() if v is not None}


# check.md B16: on a rebreather dive the app stores 21 % for every tank, so
# the gases come from the transmitter names and the log. A transmitter named
# O2 is the oxygen; one named D1/D2/DIL is a diluent, breathing the loop's
# mix. The log's ten programmed gases are five open-circuit ones (slots 0-4)
# and five closed-circuit ones, the diluents (slots 5-9) - the order the
# app's own UDDF export of a CCR dive names them (OC1-OC5, CC1-CC5).
OXYGEN_TRANSMITTER = re.compile(r"^O2$", re.IGNORECASE)
DILUENT_TRANSMITTER = re.compile(r"^(D\d|DIL\d?)$", re.IGNORECASE)
CC_GAS_SLOTS = range(5, 10)
PLACEHOLDER_MIX = (21.0, 0.0)          # what the app stores for every tank of a CCR dive


def diluent_mixes(log: ShearwaterLog) -> List[Tuple[int, int]]:
    """The diluents of a rebreather dive, best first: the mixes breathed on
    the loop (closed-circuit samples) in the order breathed, then the
    enabled closed-circuit gases of the header not among them."""
    out: List[Tuple[int, int]] = []
    for s in log.samples:
        mix = (s.o2, s.he)
        if s.ccr and (s.o2 or s.he) and mix not in out:
            out.append(mix)
    for i in CC_GAS_SLOTS:
        mix = (log.o2[i], log.he[i]) if i < len(log.o2) and i < len(log.he) else (0, 0)
        if i < len(log.gas_enabled) and log.gas_enabled[i] and (mix[0] or mix[1]) and mix not in out:
            out.append(mix)
    return out


def rebreather_gases(log: ShearwaterLog, gases: Sequence[GasMixture]) -> Optional[List[GasMixture]]:
    """The tanks of a closed or semi-closed circuit dive with roles and real
    mixes from the transmitter names (B16): ``O2`` the oxygen tank (100 %),
    ``D1``/``D2``/``DIL`` a diluent - the n-th diluent transmitter breathing
    the n-th of `diluent_mixes` (the last when there are fewer). A tank whose
    stored mix is not the app's 21 % placeholder keeps it (a mix set in the
    app wins); another name is left as it is. None for an open-circuit dive
    or when nothing changes."""
    if log.dive_mode not in MODES_CC and log.dive_mode != MODE_SC:
        return None
    diluents = diluent_mixes(log)
    out: List[GasMixture] = []
    n_diluent = 0
    changed = False
    for gas in gases:
        name = (gas.tank_name or "").strip()
        placeholder = (float(gas.oxygen or 0), float(gas.helium or 0)) == PLACEHOLDER_MIX
        update: Dict[str, Any] = {}
        if OXYGEN_TRANSMITTER.match(name):
            update["tank_role"] = "oxygen"
            if placeholder:
                update.update(oxygen=100.0, helium=0.0)
        elif DILUENT_TRANSMITTER.match(name):
            update["tank_role"] = "diluent"
            if placeholder and diluents:
                o2, he = diluents[min(n_diluent, len(diluents) - 1)]
                update.update(oxygen=float(o2), helium=float(he))
            n_diluent += 1
        if update:
            changed = True
            gas = gas.model_copy(update=update)
        out.append(gas)
    return out if changed else None


@dataclass
class DecodedLog:
    """What the adapter puts on the ``UnifiedDive``: the samples, the events,
    the carried header fields and, on a rebreather dive whose transmitters
    say which tank is which, the tanks with their roles (`rebreather_gases`;
    None: the adapter's own)."""
    samples: List[UnifiedSample] = field(default_factory=list)
    events: List[DiveEvent] = field(default_factory=list)
    fields: Dict[str, Any] = field(default_factory=dict)
    gas_mixtures: Optional[List[GasMixture]] = None


def decode_log(blob: Optional[bytes], tank_of_transmitter: Optional[Mapping[int, int]] = None,
               gases: Sequence[GasMixture] = ()) -> DecodedLog:
    """``data_bytes_1`` -> :class:`DecodedLog`, or :class:`ShearwaterLogError`.
    ``tank_of_transmitter`` maps a transmitter slot (0 = T1) to its index in
    ``gases`` (the dive's ``gas_mixtures``), which the events name tanks by -
    on a rebreather dive the gases `rebreather_gases` makes of them."""
    log = parse_log(unpack_blob(blob))
    samples = profile_samples(log, tank_of_transmitter=tank_of_transmitter)
    ccr_gases = rebreather_gases(log, gases)
    return DecodedLog(samples=samples, events=profile_events(log, ccr_gases if ccr_gases is not None else gases),
                      fields=carried_fields(log), gas_mixtures=ccr_gases)


def decode_profile(blob: Optional[bytes]) -> List[UnifiedSample]:
    """``data_bytes_1`` -> ``UnifiedSample``s (transmitters keyed by slot), or
    :class:`ShearwaterLogError`."""
    return decode_log(blob).samples
