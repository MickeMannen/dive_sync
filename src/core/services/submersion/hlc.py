"""Hybrid logical clock as Submersion stores it (rework.md F11, format
section 3): ``"<physicalMs:15>:<counter:6>:<deviceId>"``, ordered by
(physical ms, counter, device id). One persistent clock per dive_sync
Submersion device; every row we write is stamped from it and every remote
HLC we read advances it."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True, order=True)
class Hlc:
    physical_ms: int
    counter: int
    device_id: str

    @classmethod
    def parse(cls, text: Optional[str]) -> Optional["Hlc"]:
        if not text:
            return None
        try:
            ms, counter, device = str(text).split(":", 2)
            return cls(int(ms), int(counter), device)
        except ValueError:
            return None

    def __str__(self) -> str:
        return f"{self.physical_ms:015d}:{self.counter:06d}:{self.device_id}"

    def key(self) -> Tuple[int, int, str]:
        return (self.physical_ms, self.counter, self.device_id)


def hlc_key(text: Optional[str]) -> Tuple[int, int, str]:
    """Sort key for an HLC string; absent or malformed sorts lowest."""
    parsed = Hlc.parse(text)
    return parsed.key() if parsed else (-1, -1, "")


class HlcClock:
    """Persistent clock. ``tick()`` hands out strictly increasing HLCs;
    ``observe()`` moves the clock past a remote one."""

    def __init__(self, device_id: str, path: Optional[str] = None, now_ms=None):
        self.device_id = device_id
        self.path = path
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.last_ms = 0
        self.last_counter = 0
        if path and os.path.exists(path):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                self.last_ms = int(data.get("last_ms", 0))
                self.last_counter = int(data.get("last_counter", 0))
            except Exception:
                pass

    def _save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w") as f:
            json.dump({"device_id": self.device_id, "last_ms": self.last_ms, "last_counter": self.last_counter}, f)

    def tick(self) -> Hlc:
        now = self._now_ms()
        if now > self.last_ms:
            self.last_ms, self.last_counter = now, 0
        else:
            self.last_counter += 1
        self._save()
        return Hlc(self.last_ms, self.last_counter, self.device_id)

    def observe(self, remote: Optional[str]) -> None:
        parsed = Hlc.parse(remote)
        if parsed is None:
            return
        if (parsed.physical_ms, parsed.counter) > (self.last_ms, self.last_counter):
            self.last_ms, self.last_counter = parsed.physical_ms, parsed.counter
            self._save()

    @property
    def current(self) -> Hlc:
        return Hlc(self.last_ms, self.last_counter, self.device_id)
