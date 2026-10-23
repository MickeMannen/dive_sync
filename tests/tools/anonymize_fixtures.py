#!/usr/bin/env python3
"""Turn a Subsurface Cloud clone into committable fixtures (rework.md F4).

    python tests/tools/anonymize_fixtures.py subsurface <clone dir> <out dir>

What changes (everything else stays byte-for-byte as the app wrote it):
- people: divemaster / buddy names and emails -> placeholders
- devices: dive-computer serial numbers
- git metadata: only the working tree of the clone is copied

Dive sites, coordinates, depths, times, profiles and tank data are kept:
they are what the adapters have to parse, and the test accounts hold public
dive sites only.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from typing import List

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"\+?\d[\d \-]{6,}\d")

PLACEHOLDER_SERIAL = "0000000000"


def _mask_text(text: str) -> str:
    return EMAIL.sub("diver@example.com", text)


def anonymize_subsurface(clone_dir: str, out_dir: str) -> List[str]:
    """Copy the working tree; scrub serials, names and emails in the text files."""
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    serials = set()
    for root, _dirs, files in os.walk(clone_dir):
        if ".git" in root.split(os.sep):
            continue
        for fn in files:
            with open(os.path.join(root, fn), "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = re.match(r'keyvalue "Serial" "([^"]+)"', line.strip())
                    if m:
                        serials.add(m.group(1))
    written = []
    for root, _dirs, files in os.walk(clone_dir):
        if ".git" in root.split(os.sep):
            continue
        rel = os.path.relpath(root, clone_dir)
        os.makedirs(os.path.join(out_dir, rel), exist_ok=True)
        for fn in files:
            with open(os.path.join(root, fn), "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            for serial in sorted(serials, key=len, reverse=True):
                text = text.replace(serial, PLACEHOLDER_SERIAL)
            text = re.sub(r'^(divemaster|buddy) "(?!")[^"]*"', lambda m: f'{m.group(1)} "Test Buddy"', text, flags=re.M)
            text = _mask_text(text)
            with open(os.path.join(out_dir, rel, fn), "w", encoding="utf-8") as f:
                f.write(text)
            written.append(os.path.normpath(os.path.join(rel, fn)))
    return written


def main(argv: List[str]) -> int:
    if len(argv) != 4 or argv[1] != "subsurface":
        print(__doc__)
        return 2
    kind, src, dst = argv[1], argv[2], argv[3]
    files = anonymize_subsurface(src, dst)
    print(f"{kind}: wrote {len(files)} file(s) to {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
