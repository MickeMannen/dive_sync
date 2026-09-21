"""Submersion profile-series codecs (rework.md F10), ported from the
``ProfileSeriesCodec`` v1 format and verified byte-for-byte against real
exports (tests/data/submersion).

Layout (after zlib): ``version(1) | sampleCount varint | one block per
column``. A block starts with a presence byte: 0 = all null (no values
follow), 1 = all present, 2 = bitmap of ceil(n/8) bytes, LSB first, then the
present values. Column kinds: ``deltaInt`` = zigzag varint delta from the
previous present value; ``float64`` = little-endian double; the string kind
is unused by the columns dive_sync reads.

The dive profile table (24 columns; the six ``o2_sensor_mv`` columns of the
documentation are absent in the v1 writer) and the tank pressure table
(timestamp, pressure) are below. Pure functions, no I/O.
"""
from __future__ import annotations

import struct
import zlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

PROFILE_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("timestamp", "deltaInt"),
    ("depth", "float64"),
    ("pressure", "float64"),
    ("temperature", "float64"),
    ("heart_rate", "deltaInt"),
    ("ascent_rate", "float64"),
    ("ceiling", "float64"),
    ("ndl", "deltaInt"),
    ("setpoint", "float64"),
    ("pp_o2", "float64"),
    ("o2_sensor1", "float64"),
    ("o2_sensor2", "float64"),
    ("o2_sensor3", "float64"),
    ("o2_sensor4", "float64"),
    ("o2_sensor5", "float64"),
    ("o2_sensor6", "float64"),
    ("cns", "float64"),
    ("tts", "float64"),
    ("rbt", "float64"),
    ("deco_type", "deltaInt"),
    ("heart_rate_source", "string"),
    ("heading", "float64"),
    ("reserved_22", "float64"),
    ("reserved_23", "float64"),
)
PRESSURE_COLUMNS: Tuple[Tuple[str, str], ...] = (("timestamp", "deltaInt"), ("pressure", "float64"))
CODEC_VERSION = 1
ZLIB_LEVEL = 6


class CodecError(ValueError):
    pass


# ---------------------------------------------------------------------------
# byte helpers
# ---------------------------------------------------------------------------

def _write_varint(out: bytearray, value: int) -> None:
    if value < 0:
        raise CodecError("varint must be non-negative")
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return


def _read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    shift, value = 0, 0
    while True:
        if pos >= len(buf):
            raise CodecError("truncated varint")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _zigzag_encode(value: int) -> int:
    return (value << 1) ^ (value >> 63) if value < 0 else value << 1


def _zigzag_decode(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


# ---------------------------------------------------------------------------
# blocks
# ---------------------------------------------------------------------------

def _encode_block(out: bytearray, kind: str, values: Sequence[Optional[Any]], count: int) -> None:
    if len(values) != count:
        raise CodecError(f"column has {len(values)} values, expected {count}")
    present = [v is not None for v in values]
    if not any(present):
        out.append(0)
        return
    if all(present):
        out.append(1)
    else:
        out.append(2)
        bitmap = bytearray((count + 7) // 8)
        for i, p in enumerate(present):
            if p:
                bitmap[i // 8] |= 1 << (i % 8)
        out.extend(bitmap)
    previous = 0
    for value, is_present in zip(values, present):
        if not is_present:
            continue
        if kind == "deltaInt":
            value = int(value)
            _write_varint(out, _zigzag_encode(value - previous))
            previous = value
        elif kind == "float64":
            out.extend(struct.pack("<d", float(value)))
        else:
            raise CodecError(f"cannot encode column kind {kind!r}")


def _decode_block(buf: bytes, pos: int, kind: str, count: int) -> Tuple[List[Optional[Any]], int]:
    if pos >= len(buf):
        raise CodecError("truncated block header")
    mode = buf[pos]
    pos += 1
    if mode == 0:
        return [None] * count, pos
    if mode == 1:
        present = [True] * count
    elif mode == 2:
        nbytes = (count + 7) // 8
        bitmap = buf[pos:pos + nbytes]
        if len(bitmap) < nbytes:
            raise CodecError("truncated presence bitmap")
        pos += nbytes
        present = [bool(bitmap[i // 8] >> (i % 8) & 1) for i in range(count)]
    else:
        raise CodecError(f"unknown presence mode {mode}")
    values: List[Optional[Any]] = []
    previous = 0
    for is_present in present:
        if not is_present:
            values.append(None)
            continue
        if kind == "deltaInt":
            raw, pos = _read_varint(buf, pos)
            previous += _zigzag_decode(raw)
            values.append(previous)
        elif kind == "float64":
            if pos + 8 > len(buf):
                raise CodecError("truncated float")
            values.append(struct.unpack_from("<d", buf, pos)[0])
            pos += 8
        else:
            raise CodecError(f"cannot decode column kind {kind!r}")
    return values, pos


# ---------------------------------------------------------------------------
# public api
# ---------------------------------------------------------------------------

def decode_series(blob: bytes, columns: Sequence[Tuple[str, str]] = PROFILE_COLUMNS,
                  stop_after: Optional[str] = None) -> Dict[str, List[Optional[Any]]]:
    """Decode a zlib-compressed series blob into ``{column: [values]}``.
    ``stop_after`` stops reading once that column is decoded (dive_sync only
    needs the first few columns, and unknown trailing columns of a newer
    writer are then never touched)."""
    raw = zlib.decompress(blob)
    if not raw:
        raise CodecError("empty series")
    version = raw[0]
    if version != CODEC_VERSION:
        raise CodecError(f"unsupported series codec version {version}")
    count, pos = _read_varint(raw, 1)
    out: Dict[str, List[Optional[Any]]] = {"__count__": [count]}
    for name, kind in columns:
        if kind == "string":
            # the only string column comes after everything dive_sync reads
            break
        values, pos = _decode_block(raw, pos, kind, count)
        out[name] = values
        if name == stop_after:
            break
    return out


def encode_series(values: Dict[str, Sequence[Optional[Any]]], count: int,
                  columns: Sequence[Tuple[str, str]] = PROFILE_COLUMNS, level: int = ZLIB_LEVEL) -> bytes:
    """Encode ``{column: [values]}`` (missing columns are all-null) into a
    zlib-compressed blob with exactly the columns of ``columns``."""
    out = bytearray([CODEC_VERSION])
    _write_varint(out, count)
    for name, kind in columns:
        column = values.get(name)
        if column is None or kind == "string":
            out.append(0)
            continue
        _encode_block(out, kind, list(column), count)
    return zlib.compress(bytes(out), level)


def decode_profile(blob: bytes) -> List[Tuple[int, Optional[float], Optional[float]]]:
    """``[(time_s, depth_m, temp_c), ...]`` from a dive profile series."""
    cols = decode_series(blob, PROFILE_COLUMNS, stop_after="temperature")
    count = cols["__count__"][0]
    times, depths, temps = cols["timestamp"], cols["depth"], cols["temperature"]
    return [(int(times[i]) if times[i] is not None else i, depths[i], temps[i]) for i in range(count)]


def encode_profile(samples: Sequence[Tuple[int, Optional[float], Optional[float]]]) -> bytes:
    times = [s[0] for s in samples]
    depths = [s[1] for s in samples]
    temps = [s[2] for s in samples]
    return encode_series({"timestamp": times, "depth": depths, "temperature": temps}, len(samples), PROFILE_COLUMNS)


def decode_pressures(blob: bytes) -> List[Tuple[int, Optional[float]]]:
    cols = decode_series(blob, PRESSURE_COLUMNS)
    count = cols["__count__"][0]
    return [(int(cols["timestamp"][i]), cols["pressure"][i]) for i in range(count)]


def encode_pressures(samples: Sequence[Tuple[int, Optional[float]]]) -> bytes:
    return encode_series({"timestamp": [s[0] for s in samples], "pressure": [s[1] for s in samples]},
                         len(samples), PRESSURE_COLUMNS)


def profile_summary(samples: Sequence[Tuple[int, Optional[float], Optional[float]]]) -> Dict[str, Any]:
    """The ``dive_profile_series`` metadata columns for a sample list."""
    depths = [s[1] for s in samples if s[1] is not None]
    return {
        "sampleCount": len(samples),
        "startTimestamp": samples[0][0] if samples else 0,
        "endTimestamp": samples[-1][0] if samples else 0,
        "maxDepth": max(depths) if depths else None,
        "firstDepth": depths[0] if depths else None,
        "lastDepth": depths[-1] if depths else None,
        "codecVersion": CODEC_VERSION,
    }
