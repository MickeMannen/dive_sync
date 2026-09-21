#!/usr/bin/env python3
"""Turn raw Submersion sync files and a Subsurface Cloud clone into
committable fixtures (rework.md F9 / F4).

    python tests/tools/anonymize_fixtures.py submersion <raw dir> <out dir>
    python tests/tools/anonymize_fixtures.py subsurface <clone dir> <out dir>

What changes (everything else stays byte-for-byte as the app wrote it):
- people: diver / buddy / dive-center names, emails, phones -> placeholders
- devices: device names, dive-computer serial numbers, bluetooth addresses
- accounts: connected-account labels and identifiers (bucket names, endpoints)
- bulk: Submersion ``importedFiles.bytes`` (raw FIT uploads) and
  ``diveDataSources.rawData`` are emptied; the base ``checksum`` therefore no
  longer matches and is left as a marker of that
- git metadata: only the working tree of the Subsurface clone is copied

Dive sites, coordinates, depths, times, profiles and tank data are kept:
they are what the adapters have to parse, and the test accounts hold public
dive sites only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from typing import Any, Dict, List

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"\+?\d[\d \-]{6,}\d")

PLACEHOLDER_DIVER = "Test Diver"
PLACEHOLDER_SERIAL = "0000000000"


def _mask_text(text: str) -> str:
    return EMAIL.sub("diver@example.com", text)


def _collect_serials(data: Dict[str, Any]) -> List[str]:
    serials = set()
    for row in data.get("diveComputers", []) + data.get("equipment", []):
        if row.get("serialNumber"):
            serials.add(str(row["serialNumber"]))
    for row in data.get("dives", []) + data.get("diveDataSources", []):
        for key in ("diveComputerSerial", "computerSerial"):
            if row.get(key):
                serials.add(str(row[key]))
    return sorted(serials, key=len, reverse=True)


def anonymize_submersion_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload.get("data", {})
    serials = _collect_serials(data)

    for row in data.get("divers", []):
        row["name"] = PLACEHOLDER_DIVER
        for key in ("email", "phone", "emergencyContactName", "emergencyContactPhone", "emergencyContact2Name",
                    "emergencyContact2Phone", "insurancePolicyNumber", "insuranceProvider", "insurancePhone",
                    "insuranceEmergencyPhone", "medicalNotes", "medications", "allergies", "notes", "photo", "photoPath"):
            if row.get(key) not in (None, ""):
                row[key] = None
    for row in data.get("buddies", []):
        row["name"] = "Test Buddy"
        for key in ("email", "phone", "notes", "photo"):
            if row.get(key) not in (None, ""):
                row[key] = None
    for row in data.get("diveCenters", []):
        row["name"] = "Test Dive Center"
        for key in ("email", "phone", "website", "street", "postalCode", "notes"):
            if row.get(key) not in (None, ""):
                row[key] = None
    for row in data.get("connectedAccounts", []):
        row["label"] = f"test-bucket ({row.get('kind')})"
        row["accountIdentifier"] = None
    for row in data.get("diveComputers", []):
        row["bluetoothAddress"] = None
        row["serialNumber"] = PLACEHOLDER_SERIAL if row.get("serialNumber") else row.get("serialNumber")
    for row in data.get("equipment", []):
        if row.get("serialNumber"):
            row["serialNumber"] = PLACEHOLDER_SERIAL
    for row in data.get("dives", []):
        if row.get("diveComputerSerial"):
            row["diveComputerSerial"] = PLACEHOLDER_SERIAL
        for key in ("buddy", "diveMaster", "boatCaptain"):
            if row.get(key):
                row[key] = "Test Buddy"
    for row in data.get("diveDataSources", []):
        if row.get("computerSerial"):
            row["computerSerial"] = PLACEHOLDER_SERIAL
        if row.get("rawData"):
            row["rawData"] = None
        if row.get("sourceUuid"):
            for serial in serials:
                row["sourceUuid"] = str(row["sourceUuid"]).replace(serial, PLACEHOLDER_SERIAL)
    for row in data.get("importedFiles", []):
        if row.get("bytes"):
            row["bytes"] = ""
    return payload


def anonymize_submersion(raw_dir: str, out_dir: str) -> List[str]:
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for name in sorted(os.listdir(raw_dir)):
        if not name.startswith("ssv1."):
            continue
        with open(os.path.join(raw_dir, name), "r", encoding="utf-8") as f:
            text = f.read()
        payload = json.loads(text)
        if name.endswith(".manifest.json"):
            payload["deviceName"] = "Test device " + payload["deviceId"][:4]
        else:
            payload = anonymize_submersion_payload(payload)
        out = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        out = _mask_text(out)
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            f.write(out)
        written.append(name)
    return written


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
    if len(argv) != 4 or argv[1] not in ("submersion", "subsurface"):
        print(__doc__)
        return 2
    kind, src, dst = argv[1], argv[2], argv[3]
    files = anonymize_submersion(src, dst) if kind == "submersion" else anonymize_subsurface(src, dst)
    print(f"{kind}: wrote {len(files)} file(s) to {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
