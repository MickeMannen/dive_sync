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

To check against a newer libdivecomputer: diff its ``shearwater_predator_parser.c``
against the fetched revision, and compare every offset above with the
``dc_parser_samples_foreach`` / ``dc_parser_get_field`` code for the PNF
branch (``pnf`` / ``logversion`` conditions). ``tests/test_shearwater_log.py``
pins the fixture's 27 dives, so a wrong offset shows up there.

Pure Python (``gzip``, ``struct``), no SQLite: the adapter hands in the blob
and gets ``UnifiedSample``s back. Any structural problem raises
:class:`ShearwaterLogError`; the adapter turns that into "no samples" plus
one warning, so a profile can never make loading a dive fail.
"""
from __future__ import annotations

import gzip
import logging
import struct
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.core.models import UnifiedSample

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

# status byte bits of a sample
STATUS_OC = 0x10
STATUS_SC = 0x08

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

def _parse_sample(rec: bytes, time_s: int, imperial: bool, log_version: int) -> ShearwaterSample:
    """One ``0x01``/``0x03`` record; offsets are libdivecomputer's, shifted by
    the PNF type byte (``p``)."""
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
    return ShearwaterSample(
        time=time_s,
        depth=round(depth, 2),
        temp=round(temp, 2),
        ppo2=rec[p + 6] / 100.0,
        o2=rec[p + 7],
        he=rec[p + 8],
        ccr=(status & STATUS_OC) == 0,
        deco_stop_depth=round(stop, 2),
        ndl_or_stop_min=rec[p + 9],
        tts_min=_u16(rec, p + 4),
        cns=rec[p + 22] / 100.0,
        setpoint=rec[p + 18] / 100.0,
        gf99=None if gf99 == 255 else gf99,
        gas_time_min=None if gtr >= 0xF0 else gtr,
        pressures=pressures,
    )


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
    )

    time_ms = 0
    previous: Optional[Tuple[int, int, bool]] = None
    for off in range(0, len(raw), RECORD):
        rec = raw[off:off + RECORD]
        t = rec[0]
        if t in (DIVE_SAMPLE, AVELO_SAMPLE):
            time_ms += interval_ms
            sample = _parse_sample(rec, time_ms // 1000, imperial, log_version)
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
    """The lowest transmitter index with any reading, whose pressures the
    profile carries (``UnifiedSample`` has one pressure per sample)."""
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


def profile_samples(log: ShearwaterLog, check: bool = True) -> List[UnifiedSample]:
    """The log's samples as ``UnifiedSample``s: time, depth, temperature and
    the primary transmitter's pressure, every sample as logged (including
    the surface tail). Freedive logs have no depth samples and raise."""
    if log.is_freedive:
        raise ShearwaterLogError("freedive log: its samples are not decoded")
    if check:
        check_against_header(log)
    elif not log.samples:
        raise ShearwaterLogError("log has no dive samples")
    tank = primary_transmitter(log)
    return [UnifiedSample(depth=s.depth, temp=s.temp, time=s.time,
                          pressure=s.pressures.get(tank) if tank is not None else None)
            for s in log.samples]


def decode_profile(blob: Optional[bytes]) -> List[UnifiedSample]:
    """``data_bytes_1`` -> ``UnifiedSample``s, or :class:`ShearwaterLogError`."""
    return profile_samples(parse_log(unpack_blob(blob)))
