"""Submersion sync store backends (seed of rework.md F11).

Submersion keeps a per-device changeset log as flat ``ssv1.*`` objects under
one prefix (see ``docs/submersion_sync_format.md``). ``S3Store`` talks to any
S3-compatible bucket through ``boto3``; Backblaze B2 is the recommended
hosted option. ``boto3`` is imported lazily so the rest of dive_sync does not
need it installed.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

import hashlib
import json
import os
import re
import uuid

from src.core.config import SubmersionCredentials

logger = logging.getLogger("dive_sync.submersion.store")

MANIFEST_PREFIX = "ssv1."
FORMAT_VERSION = 1
SCHEMA_VERSION = 210
PART_BYTES = 8 * 1024 * 1024
EPOCH_FILE = "submersion_library_epoch.json"
ENCRYPTED_MAGIC = b"SBE1"

NAME_RE = re.compile(r"^ssv1\.(?P<device>[^.]+)\.(?P<kind>manifest\.json|retired\.json|base\.(?P<bseq>\d{12})\.p(?P<part>\d{4})|cs\.(?P<cseq>\d{12})\.json)$")


def manifest_name(device_id: str) -> str:
    return f"ssv1.{device_id}.manifest.json"


def retired_name(device_id: str) -> str:
    return f"ssv1.{device_id}.retired.json"


def base_name(device_id: str, seq: int, part: int = 0) -> str:
    return f"ssv1.{device_id}.base.{seq:012d}.p{part:04d}"


def changeset_name(device_id: str, seq: int) -> str:
    return f"ssv1.{device_id}.cs.{seq:012d}.json"


def parse_name(name: str) -> Optional[Dict[str, Any]]:
    m = NAME_RE.match(name)
    if not m:
        return None
    kind = m.group("kind")
    if kind.startswith("base."):
        return {"device": m.group("device"), "kind": "base", "seq": int(m.group("bseq")), "part": int(m.group("part"))}
    if kind.startswith("cs."):
        return {"device": m.group("device"), "kind": "changeset", "seq": int(m.group("cseq"))}
    return {"device": m.group("device"), "kind": kind.split(".")[0]}


class StoreError(RuntimeError):
    pass


class EncryptedStoreError(StoreError):
    """The store holds end-to-end encrypted files; dive_sync cannot read them."""


def ensure_plain(data: bytes, name: str) -> bytes:
    if data[:4] == ENCRYPTED_MAGIC:
        raise EncryptedStoreError(
            f"{name} is end-to-end encrypted (SBE1). dive_sync cannot read encrypted Submersion stores; "
            "turn off end-to-end encryption in Submersion's sync settings for this store."
        )
    return data


def data_checksum(data: Dict[str, Any]) -> str:
    """SHA-256 over the compact UTF-8 JSON of the payload's ``data`` object,
    keys in insertion order, exactly as Submersion writes it."""
    encoded = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def encode_payload(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def file_checksum(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class FolderStore:
    """A local directory (a Dropbox or iCloud folder on the desktop)."""

    def __init__(self, path: str):
        self.path = path

    def list(self, name_prefix: str = "") -> List[str]:
        if not os.path.isdir(self.path):
            return []
        return sorted(n for n in os.listdir(self.path) if n.startswith(name_prefix) and os.path.isfile(os.path.join(self.path, n)))

    def get(self, name: str) -> bytes:
        with open(os.path.join(self.path, name), "rb") as f:
            return f.read()

    def put(self, name: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        os.makedirs(self.path, exist_ok=True)
        tmp = os.path.join(self.path, name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(self.path, name))

    def delete(self, name: str) -> None:
        try:
            os.remove(os.path.join(self.path, name))
        except FileNotFoundError:
            pass

    def exists(self, name: str) -> bool:
        return os.path.isfile(os.path.join(self.path, name))


def open_store(config: SubmersionCredentials):
    if config.store_type == "folder":
        return FolderStore(config.folder_path)
    return S3Store(config)


# ---------------------------------------------------------------------------
# Reading peers
# ---------------------------------------------------------------------------

def read_json(store, name: str) -> Dict[str, Any]:
    return json.loads(ensure_plain(store.get(name), name).decode("utf-8"))


def list_devices(store) -> Dict[str, Dict[str, Any]]:
    """``{device_id: {"manifest": name|None, "retired": bool, "bases": {seq: [parts]}, "changesets": {seq: name}}}``."""
    devices: Dict[str, Dict[str, Any]] = {}
    for name in store.list(MANIFEST_PREFIX):
        info = parse_name(name)
        if not info:
            continue
        entry = devices.setdefault(info["device"], {"manifest": None, "retired": False, "bases": {}, "changesets": {}})
        if info["kind"] == "manifest":
            entry["manifest"] = name
        elif info["kind"] == "retired":
            entry["retired"] = True
        elif info["kind"] == "base":
            entry["bases"].setdefault(info["seq"], {})[info["part"]] = name
        elif info["kind"] == "changeset":
            entry["changesets"][info["seq"]] = name
    return devices


def read_epoch(store) -> Optional[str]:
    if hasattr(store, "exists") and not store.exists(EPOCH_FILE):
        return None
    try:
        data = read_json(store, EPOCH_FILE)
    except FileNotFoundError:
        return None
    except Exception as e:
        if "NoSuchKey" in str(e) or "Not Found" in str(e) or "404" in str(e):
            return None
        raise
    return data.get("epochId") or data.get("id")


def read_base(store, device_id: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Reassemble the parts of the manifest's base and decode the payload."""
    seq = int(manifest["baseSeq"])
    parts = int(manifest.get("basePartCount") or 1)
    chunks = []
    for part in range(parts):
        name = base_name(device_id, seq, part)
        chunks.append(ensure_plain(store.get(name), name))
    data = b"".join(chunks)
    expected = manifest.get("baseChecksum")
    if expected and file_checksum(data) != expected:
        logger.warning("Submersion base of %s seq %d does not match its manifest checksum", device_id, seq)
    return json.loads(data.decode("utf-8"))


def read_changesets(store, device_id: str, manifest: Dict[str, Any], names: Dict[int, str]) -> List[Dict[str, Any]]:
    """Every changeset after the base, in sequence order, up to headSeq."""
    base_seq = int(manifest["baseSeq"])
    head_seq = int(manifest.get("headSeq") or base_seq)
    out = []
    for seq in sorted(names):
        if base_seq < seq <= head_seq:
            out.append(read_json(store, names[seq]))
    return out


def read_peer(store, device_id: str, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """``{"manifest", "payloads": [base, cs, cs, ...]}`` for one device, or
    None when it has no manifest or is retired."""
    if entry["retired"] or not entry["manifest"]:
        return None
    manifest = read_json(store, entry["manifest"])
    if int(manifest.get("formatVersion", 1)) != FORMAT_VERSION:
        logger.warning("Submersion device %s uses format version %s; skipped", device_id, manifest.get("formatVersion"))
        return None
    payloads = [read_base(store, device_id, manifest)]
    payloads.extend(read_changesets(store, device_id, manifest, entry["changesets"]))
    return {"manifest": manifest, "payloads": payloads}


# ---------------------------------------------------------------------------
# Writing our own device
# ---------------------------------------------------------------------------

def build_payload(device_id: str, data: Dict[str, List[Dict[str, Any]]], deletions: Dict[str, List[Dict[str, Any]]],
                  seq: int, to_hlc: str, since_hlc: Optional[str], epoch_id: Optional[str], now_ms: int,
                  base_seq: Optional[int] = None) -> Dict[str, Any]:
    return {
        "version": 2,
        "exportedAt": now_ms,
        "deviceId": device_id,
        "lastSyncTimestamp": None,
        "checksum": data_checksum(data),
        "data": data,
        "deletions": deletions,
        "uploadNonce": str(uuid.uuid4()),
        "epochId": epoch_id,
        "seq": seq,
        "baseSeq": base_seq,
        "sinceHlc": since_hlc,
        "toHlc": to_hlc,
    }


def publish_base(store, device_id: str, device_name: str, payload: Dict[str, Any], applied_peer_hlc: Dict[str, str],
                 provider: str, now_ms: int) -> Dict[str, Any]:
    """Write the base parts, then the manifest (last, so readers never see a
    manifest pointing at missing parts). Returns the manifest."""
    encoded = encode_payload(payload)
    parts = [encoded[i:i + PART_BYTES] for i in range(0, max(len(encoded), 1), PART_BYTES)] or [b""]
    seq = int(payload["seq"])
    for index, chunk in enumerate(parts):
        store.put(base_name(device_id, seq, index), chunk, "application/json")
    manifest = {
        "formatVersion": FORMAT_VERSION,
        "schemaVersion": SCHEMA_VERSION,
        "writerSchemaVersion": SCHEMA_VERSION,
        "deviceName": device_name,
        "deviceId": device_id,
        "provider": provider,
        "baseSeq": seq,
        "basePartCount": len(parts),
        "baseBytes": len(encoded),
        "baseChecksum": file_checksum(encoded),
        "basePartChecksums": [file_checksum(chunk) for chunk in parts],
        "headSeq": seq,
        "publishedHlcHigh": payload["toHlc"],
        "epochId": payload.get("epochId"),
        "uploadNonce": payload["uploadNonce"],
        "appliedPeerHlc": dict(applied_peer_hlc),
        "updatedAt": now_ms,
    }
    store.put(manifest_name(device_id), encode_payload(manifest), "application/json")
    return manifest


def touch_manifest(store, device_id: str, manifest: Dict[str, Any], now_ms: int) -> Dict[str, Any]:
    """Heartbeat: rewrite the manifest with a fresh nonce and timestamp."""
    manifest = dict(manifest, uploadNonce=str(uuid.uuid4()), updatedAt=now_ms)
    store.put(manifest_name(device_id), encode_payload(manifest), "application/json")
    return manifest


def remove_device_files(store, device_id: str) -> None:
    for name in store.list(f"{MANIFEST_PREFIX}{device_id}."):
        store.delete(name)


class S3Store:
    """Minimal object-store client: list / get / put / delete under a prefix."""

    def __init__(self, config: SubmersionCredentials):
        if config.store_type != "s3":
            raise StoreError(f"S3Store needs store_type 's3', got {config.store_type!r}")
        self.config = config
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as e:
                raise StoreError("boto3 is not installed; run 'pip install boto3' to use an S3 store.") from e
            extra: Dict[str, Any] = {"signature_version": "s3v4"}
            if self.config.path_style:
                extra["s3"] = {"addressing_style": "path"}
            # B2 rejects the newer CRC checksum headers unless they are only
            # sent when an operation requires them.
            extra["request_checksum_calculation"] = "when_required"
            extra["response_checksum_validation"] = "when_required"
            self._client = boto3.client(
                "s3",
                endpoint_url=self.config.endpoint_url or None,
                region_name=self.config.region or None,
                aws_access_key_id=self.config.access_key_id,
                aws_secret_access_key=self.config.secret_access_key,
                config=Config(**extra),
            )
        return self._client

    def _key(self, name: str) -> str:
        prefix = self.config.prefix or ""
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        return prefix + name

    def list(self, name_prefix: str = "") -> List[str]:
        """Object names (without the store prefix) starting with ``name_prefix``."""
        full = self._key(name_prefix)
        names: List[str] = []
        token: Optional[str] = None
        while True:
            kwargs: Dict[str, Any] = {"Bucket": self.config.bucket, "Prefix": full, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            for item in page.get("Contents", []) or []:
                names.append(item["Key"][len(self._key("")):])
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        return names

    def get(self, name: str) -> bytes:
        return self.client.get_object(Bucket=self.config.bucket, Key=self._key(name))["Body"].read()

    def put(self, name: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.client.put_object(Bucket=self.config.bucket, Key=self._key(name), Body=data, ContentType=content_type)

    def delete(self, name: str) -> None:
        self.client.delete_object(Bucket=self.config.bucket, Key=self._key(name))

    def exists(self, name: str) -> bool:
        return name in self.list(name)

    def check_access(self) -> Tuple[bool, str]:
        """Read-only probe: one listing under the prefix. Reports whether the
        bucket answers and how many Submersion sync files it already holds."""
        try:
            names = self.list()
        except StoreError as e:
            return False, str(e)
        except Exception as e:  # botocore raises many classes; the message is what matters
            code = getattr(e, "response", {}).get("Error", {}).get("Code") if hasattr(e, "response") else None
            hint = ""
            if code in ("InvalidAccessKeyId", "SignatureDoesNotMatch", "AccessDenied", "Unauthorized"):
                hint = " Check the key id, the application key and that the key is allowed to read this bucket."
            elif code in ("NoSuchBucket",):
                hint = " The bucket does not exist on this endpoint."
            return False, f"S3 store check failed ({code or type(e).__name__}): {e}.{hint}"
        sync_files = [n for n in names if n.startswith(MANIFEST_PREFIX)]
        return True, (f"S3 store OK: bucket '{self.config.bucket}' prefix '{self.config.prefix}' reachable, "
                      f"{len(sync_files)} Submersion sync file(s) present.")


def check_store_access(config: SubmersionCredentials) -> Tuple[bool, str]:
    if config.store_type == "folder":
        import os
        if not config.folder_path:
            return False, "No folder path configured."
        if not os.path.isdir(config.folder_path):
            return False, f"Folder does not exist: {config.folder_path}"
        files = [n for n in os.listdir(config.folder_path) if n.startswith(MANIFEST_PREFIX)]
        return True, f"Folder store OK: {config.folder_path}, {len(files)} Submersion sync file(s) present."
    return S3Store(config).check_access()
