"""MySSI upload client for the Convert page (plans/convert.md I7, features.md B23).

**Unofficial route - can stop working at any time.** SSI publishes no API
and no file import. This module talks to the backend of the MySSI Android
app, ``https://api.divessi.com/app/a21.php``, presenting itself as that
app (`APP_IDENTITY`). SSI can change or close the route without notice,
and nothing public permits or forbids its use (plans/convert.md, "MySSI API
(I7a research)"). Every detail below counts as **unproven until the owner's
own test dive confirms it**: it comes from reading open-source clients, not
from a capture of the app.

The logic is ported (not copied) from three MIT-licensed clients:
- divebridge, (c) 2026 Benedikt Reiz (Python; login, logbook read, dive
  save, de-duplication, read-back verification, sites);
- divessi-log-importer, (c) 2026 Manuel Leitold (TypeScript; the typed
  ``save_divelog`` body);
- divessi-export, (c) 2023 Gerard Puig (Python; login and ``get_divelog``).

The calls (all on `API_URL`, operation in ``what=``):
- ``authenticate``: GET with ``l=<email>&p=<password>`` **in the query
  string** (over HTTPS). Always HTTP 200; success is ``authenticated: true``
  and a ``token`` (plus ``mid``, ``imperial``). The token's lifetime is
  unknown; a call that answers ``authenticated: false`` triggers one new
  login and a retry.
- ``get_divelog``: GET with ``token``; ``logbook_details`` (one object per
  dive, ``odin_user_log_*`` keys), ``logbook_sites``, ``logbook_buddies``.
- ``save_divelog``: POST with ``token``, body ``json_data=<JSON>`` (form
  encoded). Nobody parses the answer; success is the dive turning up on a
  fresh ``get_divelog``.
- ``get_divelog_vars``: the id lists (water type, tank type, ...).

**Never log a request URL or its parameters**: the login URL carries the
password and every other one the token. `SsiClient` logs only the operation
name, and scrubs query strings and the secrets out of any exception text.

`SsiAdapter` is the `BaseDiveAdapter` over `SsiClient` (owner's decision
2026-10-02: every service client implements the interface, AGENTS.md).
It is honest about what the app's API lets it do: ``login``,
``fetch_dives`` (the logbook as `UnifiedDive`s, `logbook_dive_to_unified`
says what is read and what is left out), ``fetch_dive`` and ``add_dive``
(the upload flow below, so the duplicate check and the read-back apply);
``update_dive`` and ``delete_dive`` refuse - no documented call does
either. It is **not** registered as a sync side: `pairs.KNOWN_SERVICES`,
the boards, settings, the web UI and the desktop controllers do not know
``ssi``; the Convert page builds the adapter itself.

The upload flow (`upload_dives`): read the logbook first, skip a dive the
logbook already has (same computer reference, or a start within
`DUPLICATE_WINDOW_S`), number new dives after SSI's highest number, send
the main tank only (the others are named in the dive's ``dropped`` list
until I7b maps the second-tank fields), preselect the nearest SSI site
within `SITE_PRESELECT_RADIUS_M` of the dive's position (a site is
optional), send ``_divecomputer_imported`` **false** (owner's decision:
uploaded dives look like hand-logged ones), and read every upload back.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from pydantic import BaseModel

from src.core.adapter import BaseDiveAdapter
from src.core.fields import FieldSpec
from src.core.models import GasMixture, SampleChannels, UnifiedDive, UnifiedSample
from src.core.site_matcher import distance_m

logger = logging.getLogger("dive_sync.ssi")

# --- the one place the route and the app identity live -----------------------
API_URL = "https://api.divessi.com/app/a21.php"
SITES_URL = "https://api.divessi.com/app/APP_CACHE_SITES.zip"
# Every working client sends these: the backend serves the Android app.
APP_IDENTITY: Dict[str, str] = {
    "ssiapp": "0815_ADR",
    "lang": "en",
    "version": "ADR_4.1.268-ssi",
    "context": "s",
}
UNOFFICIAL_NOTE = (
    "MySSI has no public API or file import. DiveSync uses the MySSI app's own backend, "
    "which SSI can change or close at any time; the upload can stop working without notice."
)
# The one switch for the whole upload (owner's decision 2026-10-02, plans/
# decisions.md): off until the owner's live test dive confirms the route.
# The desktop controllers expose it to QML as ``ssiEnabled``; when it is off
# the Convert page shows no "Send to SSI", the Settings page no MySSI card,
# and every SSI slot refuses with a log line, so nothing in the UI can reach
# MySSI. The module itself, `SsiAdapter` and the credentials helpers keep
# working (and keep their tests); tests of the desktop flow turn it on with
# a monkeypatch. Read it at call time (``ssi.UPLOAD_ENABLED``), not by name.
UPLOAD_ENABLED = False

REQUEST_TIMEOUT_S = 30.0
DEFAULT_COOLDOWN_S = 0.5
SITE_PRESELECT_RADIUS_M = 5000.0     # decided 2026-10-01: nearest SSI site within 5 km
DUPLICATE_WINDOW_S = 120             # a start within 2 minutes is the same dive
SAMPLE_INTERVAL_S = 5                # the profile grid both pushing clients use
READBACK_DEPTH_TOLERANCE_M = 0.5
READBACK_TIME_TOLERANCE_MIN = 1

# The payload's date/time formats as the research documents them; the `+`
# between date and time in `_datetime` is kept as written there (unproven:
# it may be an encoded space).
DATE_FORMAT = "%Y-%m-%d"
TIME_FORMAT = "%H:%M"
DATETIME_SEPARATOR = "+"

STATUS_PLANNED = "planned"
STATUS_SENT = "sent"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

M_TO_FT = 3.28084
BAR_TO_PSI = 14.5038
LB_TO_KG = 0.45359237


# --- errors -------------------------------------------------------------------

class SsiError(Exception):
    """Any failure talking to MySSI. The message never holds a URL, the
    password or the token."""


class SsiAuthError(SsiError):
    """MySSI did not accept the email and password (or a re-login after an
    expired token failed too)."""


class SsiRateLimited(SsiError):
    """HTTP 429. The caller stops the batch; nothing else is sent."""


class SsiResponseError(SsiError):
    """An unexpected answer: a non-200 status or a body that is not JSON."""


_QUERY_RE = re.compile(r"\?[^\s'\"<>)]*")


def scrub(text: Any, secrets: Iterable[Optional[str]] = ()) -> str:
    """``text`` with every query string cut to ``?...`` and every non-empty
    secret replaced by ``***``, for exception messages and logs: urllib3
    and requests put the full URL (password or token included) into
    connection errors."""
    out = str(text)
    out = _QUERY_RE.sub("?...", out)
    for secret in secrets:
        if secret:
            out = out.replace(str(secret), "***")
    return out


# --- credentials ----------------------------------------------------------------

class SsiCredentials(BaseModel):
    """The MySSI login the desktop app keeps in the OS keychain
    (desktop/credentials.py): email, password and the current token, so a
    later upload reuses the token instead of logging in again."""
    email: str = ""
    password: str = ""
    token: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.email.strip() and self.password)


def mask_email(email: str) -> str:
    """``m***@example.com``: enough to tell accounts apart in a log, no more."""
    email = (email or "").strip()
    if "@" not in email:
        return "***" if email else ""
    local, domain = email.split("@", 1)
    return f"{local[:1]}***@{domain}"


# --- HTTP -------------------------------------------------------------------------

@dataclass
class HttpResponse:
    """What a transport hands back: the status and the raw body."""
    status: int
    content: bytes = b""

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


# (method, url, query parameters, form body or None, timeout) -> HttpResponse
Transport = Callable[[str, str, Dict[str, str], Optional[Dict[str, str]], float], HttpResponse]


def requests_transport() -> Transport:
    """The real transport: one `requests.Session` for the client's lifetime.
    Built lazily so the tests (which always pass a fake) never touch it."""
    import requests

    session = requests.Session()

    def send(method: str, url: str, params: Dict[str, str], data: Optional[Dict[str, str]], timeout: float) -> HttpResponse:
        response = session.request(method, url, params=params, data=data, timeout=timeout)
        return HttpResponse(response.status_code, response.content)

    return send


class SsiClient:
    """The three MySSI calls, with token reuse, one re-login on expiry, the
    AGENTS.md cool-down after every request and HTTP 429 as `SsiRateLimited`.

    ``transport`` is the HTTP layer (`requests_transport()` by default; the
    tests pass a fake), ``on_token`` is told every new token so the caller
    can store it, ``sleep`` is `time.sleep` unless a test replaces it."""

    def __init__(self, email: str, password: str, token: Optional[str] = None, *,
                 cooldown_seconds: float = DEFAULT_COOLDOWN_S, transport: Optional[Transport] = None,
                 on_token: Optional[Callable[[str], None]] = None, sleep: Optional[Callable[[float], None]] = None,
                 timeout: float = REQUEST_TIMEOUT_S):
        self.email = (email or "").strip()
        self.password = password or ""
        self.token: Optional[str] = token or None
        self.master_id: Optional[str] = None
        self.imperial: Optional[bool] = None
        self.cooldown_seconds = cooldown_seconds
        self.timeout = timeout
        self._transport = transport
        self._on_token = on_token
        self._sleep = sleep  # None: time.sleep, looked up at call time

    # -- plumbing --

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            self._transport = requests_transport()
        return self._transport

    def _secrets(self) -> Tuple[Optional[str], ...]:
        return (self.password, self.token)

    def _send(self, what: str, method: str, url: str, params: Dict[str, str], data: Optional[Dict[str, str]]) -> HttpResponse:
        logger.debug("MySSI request: %s", what)
        try:
            response = self.transport(method, url, params, data, self.timeout)
        except Exception as e:  # the message may carry the URL with the secrets
            raise SsiError(f"MySSI request '{what}' failed: {type(e).__name__}: {scrub(e, self._secrets())}") from None
        finally:
            if self.cooldown_seconds > 0:
                (self._sleep or time.sleep)(self.cooldown_seconds)
        if response.status == 429:
            raise SsiRateLimited(f"MySSI rate limit (HTTP 429) on '{what}'; try again later")
        if response.status != 200:
            raise SsiResponseError(f"MySSI answered HTTP {response.status} to '{what}'")
        return response

    def _call(self, what: str, params: Optional[Dict[str, str]] = None, data: Optional[Dict[str, str]] = None, *,
              auth: bool = True, _retried: bool = False) -> Any:
        query: Dict[str, str] = dict(APP_IDENTITY)
        query["what"] = what
        if auth:
            query["token"] = self.ensure_token()
        if params:
            query.update(params)
        response = self._send(what, "POST" if data is not None else "GET", API_URL, query, data)
        try:
            payload = response.json()
        except ValueError:
            raise SsiResponseError(f"MySSI gave no JSON answer to '{what}'") from None
        if auth and isinstance(payload, dict) and payload.get("authenticated") is False:
            if _retried:
                raise SsiAuthError("MySSI rejected the login after a new authentication")
            logger.info("MySSI token no longer valid for %s; logging in again", mask_email(self.email))
            self.token = None
            return self._call(what, params, data, auth=True, _retried=True)
        return payload

    # -- the calls --

    def ensure_token(self) -> str:
        """The stored token, or a fresh login's."""
        return self.token or self.authenticate()

    def authenticate(self) -> str:
        """Log in with the email and password; returns (and keeps) the token."""
        if not self.email or not self.password:
            raise SsiAuthError("MySSI needs an email and a password")
        logger.info("MySSI login for %s", mask_email(self.email))
        payload = self._call("authenticate", {"l": self.email, "p": self.password}, auth=False)
        if not isinstance(payload, dict) or not payload.get("authenticated") or not payload.get("token"):
            raise SsiAuthError("MySSI did not accept the email and password")
        self.token = str(payload["token"])
        self.master_id = str(payload["mid"]) if payload.get("mid") is not None else None
        self.imperial = bool(payload.get("imperial")) if "imperial" in payload else None
        if self._on_token is not None:
            self._on_token(self.token)
        logger.info("MySSI login succeeded for %s", mask_email(self.email))
        return self.token

    def get_divelog(self) -> "SsiLogbook":
        """The diver's logbook: every dive, the sites and buddies used."""
        payload = self._call("get_divelog")
        if not isinstance(payload, dict):
            raise SsiResponseError("MySSI's logbook answer is not an object")
        logbook = parse_logbook(payload)
        logger.info("MySSI logbook read: %d dives, highest number %s", len(logbook.dives), logbook.highest_number or "-")
        return logbook

    def get_divelog_vars(self) -> Dict[str, Any]:
        """The id lists (dive type, water type, tank type, weather, ...)."""
        payload = self._call("get_divelog_vars")
        return payload if isinstance(payload, dict) else {}

    def save_divelog(self, payload: Dict[str, Any]) -> Any:
        """Send one dive (`build_payload`). The answer is returned as is;
        nobody relies on it - `upload_dives` reads the logbook back."""
        body = {"json_data": json.dumps(payload, separators=(",", ":"), ensure_ascii=False)}
        logger.info("MySSI: sending dive number %s", payload.get("odin_user_log_nr"))
        return self._call("save_divelog", data=body)

    def download_sites(self, dest_path: str) -> str:
        """Fetch the public SSI site database zip to ``dest_path`` (no token;
        tens of MB). The zip's inner format is unproven; `SiteIndex.from_zip`
        reads what it can."""
        response = self._send("sites", "GET", SITES_URL, {}, None)
        with open(dest_path, "wb") as f:
            f.write(response.content)
        logger.info("MySSI site database saved (%d bytes)", len(response.content))
        return dest_path


# --- the logbook as MySSI returns it --------------------------------------------------

def parse_ssi_datetime(text: Any) -> Optional[datetime]:
    """``YYYY-MM-DD+HH:MM:SS.mmm`` (the app's `_datetime`), with a space or a
    ``T`` instead of the ``+``, with or without seconds and milliseconds."""
    if not text:
        return None
    raw = str(text).strip().replace("+", " ").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _as_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_text(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    return str(value).strip() or None


@dataclass
class SsiLogbookDive:
    """One dive of ``logbook_details``, the few fields the upload flow needs;
    ``raw`` is the whole object (its keys are what I7b reads for the
    second-tank fields)."""
    id: Optional[str]
    number: Optional[int]
    start: Optional[datetime]
    computer_ref: Optional[str]
    max_depth: Optional[float] = None
    divetime_min: Optional[int] = None
    site_id: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: Dict[str, Any]) -> "SsiLogbookDive":
        start = parse_ssi_datetime(raw.get("odin_user_log_datetime"))
        if start is None and raw.get("odin_user_log_date"):
            start = parse_ssi_datetime(f"{raw.get('odin_user_log_date')} {raw.get('odin_user_log_entry_time') or '00:00'}")
        return cls(
            id=_as_text(raw.get("odin_user_log_id")),
            number=_as_int(raw.get("odin_user_log_nr")),
            start=start,
            computer_ref=_as_text(raw.get("odin_user_log_divecomputer_dive_ref")),
            max_depth=_as_float(raw.get("odin_user_log_depth_m")),
            divetime_min=_as_int(raw.get("odin_user_log_divetime")),
            site_id=_as_int(raw.get("odin_user_log_dive_sites_id")),
            raw=dict(raw),
        )


@dataclass(frozen=True)
class SsiSite:
    """An SSI dive site: the id MySSI wants in ``odin_user_log_dive_sites_id``
    and what the page shows to pick one."""
    id: int
    name: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    country: str = ""
    region: str = ""

    @property
    def label(self) -> str:
        where = ", ".join(p for p in (self.region, self.country) if p)
        return f"{self.name} ({where})" if where else self.name


def _pick(raw: Dict[str, Any], *needles: str, exclude: Sequence[str] = ()) -> Any:
    """The value of the first key containing one of ``needles`` (case
    insensitive, in needle order), skipping keys containing an ``exclude``
    word. The site records' exact key names are unproven; this reads the
    likely ones."""
    keys = list(raw.keys())
    for needle in needles:
        for key in keys:
            low = key.lower()
            if needle in low and not any(x in low for x in exclude):
                return raw[key]
    return None


def parse_site_record(raw: Dict[str, Any]) -> Optional[SsiSite]:
    """A site object from ``logbook_sites`` or the site database. The id key
    is ``odin_dive_sites_id`` (documented); name, latitude, longitude,
    country and region are read by key name."""
    if not isinstance(raw, dict):
        return None
    site_id = _as_int(raw.get("odin_dive_sites_id"))
    if site_id is None:
        site_id = _as_int(_pick(raw, "dive_sites_id", "site_id", "_id", "id", exclude=("user", "country", "region", "body")))
    if site_id is None:
        return None
    name = _as_text(_pick(raw, "sites_name", "site_name", "name", exclude=("country", "region", "user"))) or f"SSI site {site_id}"
    lat = _as_float(_pick(raw, "lat"))
    lng = _as_float(_pick(raw, "lon", "lng"))
    country = _as_text(_pick(raw, "country", exclude=("id",))) or ""
    region = _as_text(_pick(raw, "region", exclude=("id",))) or ""
    return SsiSite(id=site_id, name=name, lat=lat, lng=lng, country=country, region=region)


def _records(value: Any) -> List[Dict[str, Any]]:
    """``logbook_details`` and friends as a list of objects, whether MySSI
    sends a list or an object keyed by id."""
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)]


@dataclass
class SsiLogbook:
    dives: List[SsiLogbookDive]
    sites: List[SsiSite] = field(default_factory=list)
    buddies: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def highest_number(self) -> int:
        return max((d.number for d in self.dives if d.number is not None), default=0)


def parse_logbook(payload: Dict[str, Any]) -> SsiLogbook:
    """A ``get_divelog`` answer -> `SsiLogbook`."""
    dives = [SsiLogbookDive.from_raw(r) for r in _records(payload.get("logbook_details"))]
    sites = [s for s in (parse_site_record(r) for r in _records(payload.get("logbook_sites"))) if s is not None]
    return SsiLogbook(dives=dives, sites=sites, buddies=_records(payload.get("logbook_buddies")), raw=payload)


# --- sites ---------------------------------------------------------------------------

class SiteIndex:
    """SSI sites to pick from: the ones the diver already used
    (`from_logbook`) and, when downloaded, the whole site database
    (`from_zip`). No site-search call is documented for the app's API, so
    the search is local, over this index."""

    def __init__(self, sites: Iterable[SsiSite] = ()):
        self._by_id: Dict[int, SsiSite] = {}
        self.extend(sites)

    def extend(self, sites: Iterable[SsiSite]) -> None:
        for site in sites:
            self._by_id.setdefault(site.id, site)

    def __len__(self) -> int:
        return len(self._by_id)

    def __iter__(self):
        return iter(self._by_id.values())

    def get(self, site_id: Optional[int]) -> Optional[SsiSite]:
        return self._by_id.get(site_id) if site_id is not None else None

    @classmethod
    def from_logbook(cls, logbook: SsiLogbook) -> "SiteIndex":
        return cls(logbook.sites)

    @classmethod
    def from_records(cls, records: Iterable[Dict[str, Any]]) -> "SiteIndex":
        return cls(s for s in (parse_site_record(r) for r in records) if s is not None)

    @classmethod
    def from_zip(cls, path: str) -> "SiteIndex":
        """The downloaded ``APP_CACHE_SITES.zip``. Its inner layout is
        unproven: every JSON entry (a list, or an object whose first list
        value is the records) and every CSV entry is read with
        `parse_site_record`; anything else is skipped."""
        index = cls()
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                low = name.lower()
                if low.endswith("/"):
                    continue
                data = zf.read(name)
                if low.endswith(".json"):
                    try:
                        loaded = json.loads(data.decode("utf-8", errors="replace"))
                    except ValueError:
                        continue
                    if isinstance(loaded, dict):
                        loaded = next((v for v in loaded.values() if isinstance(v, list)), [])
                    index.extend(index.from_records(_records(loaded)))
                elif low.endswith(".csv"):
                    reader = csv.DictReader(io.StringIO(data.decode("utf-8", errors="replace")))
                    index.extend(index.from_records(list(reader)))
        return index

    def nearest(self, lat: Optional[float], lng: Optional[float],
                radius_m: float = SITE_PRESELECT_RADIUS_M) -> Optional[Tuple[SsiSite, float]]:
        """The closest site with a position within ``radius_m`` and its
        distance, or None."""
        if lat is None or lng is None:
            return None
        best: Optional[Tuple[SsiSite, float]] = None
        for site in self._by_id.values():
            if site.lat is None or site.lng is None:
                continue
            d = distance_m(lat, lng, site.lat, site.lng)
            if d <= radius_m and (best is None or d < best[1]):
                best = (site, d)
        return best

    def search(self, text: str, limit: int = 25) -> List[SsiSite]:
        """Sites whose name, region or country contains ``text`` (case
        insensitive), names first, then regions and countries."""
        wanted = (text or "").strip().lower()
        if not wanted:
            return []
        by_name = sorted((s for s in self._by_id.values() if wanted in s.name.lower()), key=lambda s: s.name.lower())
        by_place = sorted((s for s in self._by_id.values()
                           if wanted not in s.name.lower() and (wanted in s.region.lower() or wanted in s.country.lower())),
                          key=lambda s: s.name.lower())
        return (by_name + by_place)[:limit]


def preselect_site(index: Optional[SiteIndex], dive: UnifiedDive,
                   radius_m: float = SITE_PRESELECT_RADIUS_M) -> Optional[SsiSite]:
    """The nearest SSI site within ``radius_m`` of the dive's entry position,
    or None (no index, no position, nothing near)."""
    if index is None:
        return None
    found = index.nearest(dive.lat, dive.lng, radius_m)
    return found[0] if found else None


# --- UnifiedDive -> the save_divelog payload ---------------------------------------------

def computer_reference(dive: UnifiedDive) -> Optional[str]:
    """The ``_divecomputer_dive_ref`` sent with a dive and matched on a later
    run: the Garmin activity id when the dive came from a Garmin file or
    cache (``garmin:<id>``), else another service's id, else the computer's
    serial with the start instant; None for a dive with neither."""
    ids = dive.external_ids or {}
    for service in ("garmin",) + tuple(sorted(k for k in ids if k != "garmin")):
        value = str(ids.get(service) or "").strip()
        if value:
            return f"{service}:{value}"
    if dive.computer_serial:
        start = dive.date_time_utc or dive.date_time
        return f"{dive.computer_serial}:{start.strftime('%Y%m%dT%H%M%S')}"
    return None


def describe_tank(index: int, gm: GasMixture) -> str:
    """``tank 2 (Tank 2): EAN50, 11 l, 200 -> 80 bar`` for the dropped-tanks message."""
    if gm.helium:
        mix = f"TMX {gm.oxygen:g}/{gm.helium:g}"
    elif gm.oxygen and abs(gm.oxygen - 21.0) > 0.5:
        mix = f"EAN{gm.oxygen:g}"
    else:
        mix = "air"
    parts = [mix]
    if gm.tank_volume:
        parts.append(f"{gm.tank_volume:g} l")
    if gm.start_pressure is not None or gm.end_pressure is not None:
        s = f"{gm.start_pressure:.0f}" if gm.start_pressure is not None else "?"
        e = f"{gm.end_pressure:.0f}" if gm.end_pressure is not None else "?"
        parts.append(f"{s} -> {e} bar")
    name = f" ({gm.tank_name})" if gm.tank_name else ""
    return f"tank {index + 1}{name}: {', '.join(parts)}"


def ssi_drops(dive: UnifiedDive) -> List[str]:
    """What MySSI does not get from this dive, one line each, for the page
    and the per-dive result. The first line names the dropped tanks (owner's
    decision: main tank only until I7b)."""
    drops: List[str] = []
    tanks = dive.gas_mixtures or []
    if len(tanks) > 1:
        others = "; ".join(describe_tank(i, gm) for i, gm in enumerate(tanks) if i > 0)
        drops.append(f"MySSI gets the main tank only; dropped: {others}")
    if tanks and tanks[0].helium:
        drops.append(f"helium ({tanks[0].helium:g} %) of the main tank: MySSI's helium field is unconfirmed, "
                     f"the mix is sent as EAN{tanks[0].oxygen:g}")
    if dive.buddy:
        drops.append("buddy (MySSI takes buddy ids from its own list, not a name)")
    if dive.location and not (dive.lat is not None and dive.lng is not None):
        drops.append("site name (MySSI takes a site from its own database; this dive has no position to find one by)")
    if dive.events:
        drops.append("events (gas switches, alerts)")
    channels = {name for s in dive.samples if s.channels is not None
                for name, value in s.channels.model_dump().items()
                if name != "pressures" and value is not None}
    if channels:
        drops.append("per-sample " + ", ".join(sorted(channels)))
    if dive.timezone:
        drops.append("time zone (MySSI stores the local time only)")
    return drops


def _grid_samples(samples: Sequence[UnifiedSample], interval: int = SAMPLE_INTERVAL_S) -> List[Dict[str, Any]]:
    """The profile on a fixed ``interval`` grid from the first to the last
    timed sample, depth, temperature and the main tank's pressure
    linearly interpolated; ``[]`` when fewer than two samples have a time."""
    timed = sorted((s for s in samples if s.time is not None), key=lambda s: s.time)
    if len(timed) < 2:
        return []

    def pressure_of(s: UnifiedSample) -> Optional[float]:
        if s.channels is not None and 0 in s.channels.pressures:
            return s.channels.pressures[0]
        return s.pressure

    def lerp(a: Optional[float], b: Optional[float], f: float) -> Optional[float]:
        if a is None:
            return b if f >= 1.0 else None
        if b is None:
            return a
        return a + (b - a) * f

    out: List[Dict[str, Any]] = []
    idx = 0
    t = timed[0].time
    last = timed[-1].time
    while t <= last:
        while idx + 1 < len(timed) and timed[idx + 1].time <= t:
            idx += 1
        a = timed[idx]
        b = timed[idx + 1] if idx + 1 < len(timed) else a
        if b.time == a.time or t <= a.time:
            depth, temp, pressure = a.depth, a.temp, pressure_of(a)
        else:
            f = (t - a.time) / (b.time - a.time)
            depth = a.depth + (b.depth - a.depth) * f
            temp = lerp(a.temp, b.temp, f)
            pressure = lerp(pressure_of(a), pressure_of(b), f)
        point: Dict[str, Any] = {"t": int(t * 1000), "d": round(depth, 2)}
        if temp is not None:
            point["te"] = round(temp, 1)
        if pressure is not None:
            point["pressure"] = round(pressure, 1)
        out.append(point)
        t += interval
    return out


def _ean(gm: Optional[GasMixture]) -> Tuple[Optional[int], Optional[float]]:
    if gm is None or gm.oxygen is None or abs(gm.oxygen - 21.0) <= 0.5:
        return None, None
    return 1, round(float(gm.oxygen), 1)


def _weight_kg(dive: UnifiedDive) -> Optional[float]:
    if dive.weight is None:
        return None
    unit = (dive.weight_unit or "").lower()
    return round(dive.weight * LB_TO_KG, 1) if unit.startswith(("lb", "pound")) else round(dive.weight, 1)


def _visibility_m(dive: UnifiedDive) -> Optional[float]:
    if dive.visibility is None:
        return None
    unit = (dive.visibility_unit or "").lower()
    return round(dive.visibility / M_TO_FT, 1) if unit.startswith(("ft", "foot", "feet")) else round(dive.visibility, 1)


def _ft(m: Optional[float]) -> Optional[float]:
    return None if m is None else round(m * M_TO_FT, 1)


def _f(c: Optional[float]) -> Optional[float]:
    return None if c is None else round(c * 9 / 5 + 32, 1)


def _psi(bar: Optional[float]) -> Optional[int]:
    return None if bar is None else int(round(bar * BAR_TO_PSI))


def _json_text(values: Any) -> str:
    return json.dumps(values, separators=(",", ":"))


def build_payload(dive: UnifiedDive, number: int, site_id: Optional[int] = None,
                  computer_ref: Optional[str] = None) -> Dict[str, Any]:
    """The ``save_divelog`` JSON for one dive, with the keys the research
    documents. Metric and imperial twins are both sent as the app does
    (``_ft``, ``_f``; the ``_psi`` names follow that pattern, unproven).
    Keys whose exact name or unit is undocumented (``_var_*_id`` other than
    the tank type, ``ndl``/``mf`` in the samples, ``_deco_dive``, CNS/OTU)
    are left out rather than guessed; MySSI's own defaults apply.

    Main tank only (``gas_mixtures[0]``); ``_divecomputer_imported`` is
    false by the owner's decision, while the computer's name, serial and
    the dive reference are still sent (the reference is what a later run
    de-duplicates on)."""
    start = dive.date_time
    tank = dive.gas_mixtures[0] if dive.gas_mixtures else None
    ean, ean_percent = _ean(tank)
    grid = _grid_samples(dive.samples)
    divetime_min = int(round(dive.duration / 60.0))
    pressures = [p.get("pressure") for p in grid]
    has_pressure = any(p is not None for p in pressures)

    payload: Dict[str, Any] = {
        "odin_user_log_nr": int(number),
        "odin_user_log_date": start.strftime(DATE_FORMAT),
        "odin_user_log_entry_time": start.strftime(TIME_FORMAT),
        "odin_user_log_datetime": f"{start.strftime(DATE_FORMAT)}{DATETIME_SEPARATOR}{start.strftime('%H:%M:%S')}.000",
        "odin_user_log_divetime": divetime_min,
        "odin_user_log_depth_m": round(dive.max_depth, 1),
        "odin_user_log_depth_ft": _ft(dive.max_depth),
        "odin_user_log_avg_depth_m": None if dive.avg_depth is None else round(dive.avg_depth, 1),
        "odin_user_log_avg_depth_ft": _ft(dive.avg_depth),
        "odin_user_log_watertemp_c": None if dive.temp_min is None else round(dive.temp_min, 1),
        "odin_user_log_watertemp_f": _f(dive.temp_min),
        "odin_user_log_watertemp_max_c": None if dive.temp_max is None else round(dive.temp_max, 1),
        "odin_user_log_watertemp_max_f": _f(dive.temp_max),
        "odin_user_log_airtemp_c": None,
        "odin_user_log_vis_m": _visibility_m(dive),
        "odin_user_log_weight_kg": _weight_kg(dive),
        "odin_user_log_rating": None,
        "odin_user_log_comment": dive.notes or None,
        "odin_user_log_si_before": dive.surface_interval,
        "odin_user_log_buddy_ids": None,
        # site: an SSI site id (optional by the owner's decision; whether SSI
        # accepts none is a live check) and the entry position
        "odin_user_log_dive_sites_id": int(site_id) if site_id is not None else None,
        "odin_user_log_pos_start_latitude": dive.lat,
        "odin_user_log_pos_start_longitude": dive.lng,
        # main tank
        "odin_user_log_tank_vol_l": None if tank is None or tank.tank_volume is None else round(tank.tank_volume, 1),
        "odin_user_log_var_tanktype_id": None,
        "odin_user_log_pressure_start_bar": None if tank is None or tank.start_pressure is None else round(tank.start_pressure, 1),
        "odin_user_log_pressure_end_bar": None if tank is None or tank.end_pressure is None else round(tank.end_pressure, 1),
        "odin_user_log_pressure_start_psi": _psi(None if tank is None else tank.start_pressure),
        "odin_user_log_pressure_end_psi": _psi(None if tank is None else tank.end_pressure),
        "odin_user_log_ean": ean,
        "odin_user_log_ean_percent": ean_percent,
        # deco settings
        "odin_user_log_gf_set_1": dive.gf_low,
        "odin_user_log_gf_set_2": dive.gf_high,
        # the computer (the "imported" flag off by the owner's decision)
        "odin_user_log_divecomputer_imported": False,
        "odin_user_log_divecomputer_name": " ".join(p for p in (dive.computer_vendor, dive.computer_model) if p) or None,
        "odin_user_log_manufacturer": dive.computer_vendor,
        "odin_user_log_serial_nr": dive.computer_serial,
        "odin_user_log_firmware": dive.computer_firmware,
        "odin_user_log_divecomputer_dive_ref": computer_ref,
        # profile on the 5 s grid, plus the parallel datasets
        "odin_user_log_diveSamples": _json_text(grid) if grid else None,
        "odin_user_log_depthDataset": _json_text([p["d"] for p in grid]) if grid else None,
        "odin_user_log_tempDataset": _json_text([p.get("te") for p in grid]) if grid else None,
        "odin_user_log_gfSurfDataset": None,
        "odin_user_log_tankPressureDataset": _json_text(pressures) if has_pressure else None,
        "odin_user_log_pressureDataset": _json_text(pressures) if has_pressure else None,
    }
    return payload


# --- the upload flow ----------------------------------------------------------------------

@dataclass
class UploadResult:
    """One dive's fate: `STATUS_PLANNED` (from `plan_upload`), `STATUS_SENT`
    (sent and read back), `STATUS_SKIPPED` (already in the logbook) or
    `STATUS_FAILED` (``reason`` says why)."""
    dive: UnifiedDive
    status: str
    reason: str = ""
    number: Optional[int] = None
    site: Optional[SsiSite] = None
    ssi_id: Optional[str] = None
    dropped: List[str] = field(default_factory=list)
    duplicate_of: Optional[SsiLogbookDive] = None

    @property
    def summary(self) -> str:
        when = self.dive.date_time.strftime("%Y-%m-%d %H:%M")
        if self.status == STATUS_SENT:
            return f"{when}: sent as dive {self.number}"
        if self.status == STATUS_SKIPPED:
            return f"{when}: skipped, {self.reason}"
        if self.status == STATUS_FAILED:
            return f"{when}: failed, {self.reason}"
        return f"{when}: will be sent as dive {self.number}"


def find_duplicate(logbook: SsiLogbook, dive: UnifiedDive,
                   window_s: int = DUPLICATE_WINDOW_S) -> Optional[SsiLogbookDive]:
    """The logbook dive this one already is: the same computer reference
    (whatever its time), else a start within ``window_s`` of the dive's
    local start."""
    ref = computer_reference(dive)
    if ref:
        for entry in logbook.dives:
            if entry.computer_ref == ref:
                return entry
    window = timedelta(seconds=window_s)
    nearest: Optional[Tuple[timedelta, SsiLogbookDive]] = None
    for entry in logbook.dives:
        if entry.start is None:
            continue
        gap = abs(entry.start - dive.date_time)
        if gap <= window and (nearest is None or gap < nearest[0]):
            nearest = (gap, entry)
    return nearest[1] if nearest else None


def _duplicate_reason(entry: SsiLogbookDive, dive: UnifiedDive) -> str:
    ref = computer_reference(dive)
    which = f"dive {entry.number}" if entry.number is not None else f"a dive"
    if ref and entry.computer_ref == ref:
        return f"already in MySSI as {which} (same dive computer reference)"
    return f"already in MySSI as {which} (starts within {DUPLICATE_WINDOW_S // 60} minutes)"


def plan_upload(logbook: SsiLogbook, dives: Sequence[UnifiedDive],
                site_index: Optional[SiteIndex] = None) -> List[UploadResult]:
    """What `upload_dives` would do, without sending anything: which dives
    are duplicates, the number each new dive gets, the preselected site and
    the dropped-data lines. The page shows this and lets the diver change
    the sites before sending."""
    results: List[UploadResult] = []
    next_number = logbook.highest_number + 1
    for dive in dives:
        dup = find_duplicate(logbook, dive)
        if dup is not None:
            results.append(UploadResult(dive, STATUS_SKIPPED, _duplicate_reason(dup, dive), duplicate_of=dup,
                                        dropped=ssi_drops(dive)))
            continue
        results.append(UploadResult(dive, STATUS_PLANNED, number=next_number, site=preselect_site(site_index, dive),
                                    dropped=ssi_drops(dive)))
        next_number += 1
    return results


def verify_upload(logbook: SsiLogbook, dive: UnifiedDive, number: int,
                  computer_ref: Optional[str]) -> Tuple[Optional[SsiLogbookDive], List[str]]:
    """Find the dive just sent in a freshly read logbook: by computer
    reference, else by number with a start within the duplicate window.
    Returns it and the list of what disagrees with what was sent (number,
    start, depth, dive time); ``(None, [...])`` when it is not there."""
    found: Optional[SsiLogbookDive] = None
    if computer_ref:
        found = next((e for e in logbook.dives if e.computer_ref == computer_ref), None)
    if found is None:
        window = timedelta(seconds=DUPLICATE_WINDOW_S)
        found = next((e for e in logbook.dives if e.number == number and e.start is not None
                      and abs(e.start - dive.date_time) <= window), None)
    if found is None:
        return None, ["not found in the logbook after sending"]
    problems: List[str] = []
    if found.number != number:
        problems.append(f"number {found.number} instead of {number}")
    if found.start is not None and abs(found.start - dive.date_time) > timedelta(seconds=DUPLICATE_WINDOW_S):
        problems.append(f"start {found.start:%Y-%m-%d %H:%M} instead of {dive.date_time:%Y-%m-%d %H:%M}")
    if found.max_depth is not None and abs(found.max_depth - dive.max_depth) > READBACK_DEPTH_TOLERANCE_M:
        problems.append(f"depth {found.max_depth:g} m instead of {dive.max_depth:g} m")
    sent_min = int(round(dive.duration / 60.0))
    if found.divetime_min is not None and abs(found.divetime_min - sent_min) > READBACK_TIME_TOLERANCE_MIN:
        problems.append(f"dive time {found.divetime_min} min instead of {sent_min} min")
    return found, problems


def upload_dives(client: SsiClient, dives: Sequence[UnifiedDive], *,
                 site_ids: Optional[Dict[int, Optional[int]]] = None,
                 site_index: Optional[SiteIndex] = None,
                 progress: Optional[Callable[[UploadResult], None]] = None) -> List[UploadResult]:
    """Send ``dives`` to MySSI: read the logbook, skip duplicates, number the
    rest after the highest number, send each, read the logbook back and
    verify. ``site_ids`` maps a dive's index in ``dives`` to the SSI site id
    the diver chose (None = no site); a dive not in it gets the preselected
    site from ``site_index`` when one is within range. ``progress`` is told
    each result as it is settled. A rate limit or a failed login stops the
    batch; the dives not reached are reported as failed, not attempted."""
    results: List[UploadResult] = []

    def settle(result: UploadResult) -> UploadResult:
        results.append(result)
        if progress is not None:
            progress(result)
        return result

    try:
        logbook = client.get_divelog()
    except SsiError as e:
        logger.error("MySSI: cannot read the logbook: %s", e)
        for dive in dives:
            settle(UploadResult(dive, STATUS_FAILED, f"logbook not read: {e}", dropped=ssi_drops(dive)))
        return results

    if site_index is None and logbook.sites:
        site_index = SiteIndex.from_logbook(logbook)
    next_number = logbook.highest_number + 1
    stop_reason: Optional[str] = None

    for i, dive in enumerate(dives):
        dropped = ssi_drops(dive)
        if stop_reason:
            settle(UploadResult(dive, STATUS_FAILED, f"not attempted: {stop_reason}", dropped=dropped))
            continue
        dup = find_duplicate(logbook, dive)
        if dup is not None:
            settle(UploadResult(dive, STATUS_SKIPPED, _duplicate_reason(dup, dive), duplicate_of=dup, dropped=dropped))
            continue
        if site_ids is not None and i in site_ids:
            site = site_index.get(site_ids[i]) if site_index is not None else None
            site_id = site_ids[i]
        else:
            site = preselect_site(site_index, dive)
            site_id = site.id if site else None
        if site is None and site_id is not None and site_index is not None:
            site = site_index.get(site_id)
        ref = computer_reference(dive)
        number = next_number
        payload = build_payload(dive, number, site_id, ref)
        try:
            client.save_divelog(payload)
            logbook = client.get_divelog()
        except (SsiRateLimited, SsiAuthError) as e:
            stop_reason = str(e)
            settle(UploadResult(dive, STATUS_FAILED, str(e), number=number, site=site, dropped=dropped))
            continue
        except SsiError as e:
            settle(UploadResult(dive, STATUS_FAILED, str(e), number=number, site=site, dropped=dropped))
            continue
        next_number = max(next_number, logbook.highest_number + 1)
        found, problems = verify_upload(logbook, dive, number, ref)
        if found is None:
            settle(UploadResult(dive, STATUS_FAILED, "sent, but " + problems[0], number=number, site=site, dropped=dropped))
            continue
        if problems:
            settle(UploadResult(dive, STATUS_FAILED, "sent, but the read-back differs: " + "; ".join(problems),
                                number=number, site=site, ssi_id=found.id, dropped=dropped))
            continue
        logger.info("MySSI: dive at %s sent as number %d and read back", dive.date_time.strftime("%Y-%m-%d %H:%M"), number)
        settle(UploadResult(dive, STATUS_SENT, number=number, site=site, ssi_id=found.id, dropped=dropped))
    return results


# --- the adapter --------------------------------------------------------------------------------

SERVICE_ID = "ssi"


def _parse_samples_text(text: Any) -> List[UnifiedSample]:
    """``odin_user_log_diveSamples`` (a JSON string of ``{t, d, te, pressure}``
    on the 5 s grid, `_grid_samples`' shape) back to samples; ``[]`` when
    absent or unreadable."""
    if not text:
        return []
    try:
        points = json.loads(text) if isinstance(text, str) else text
    except ValueError:
        return []
    if not isinstance(points, list):
        return []
    out: List[UnifiedSample] = []
    for p in points:
        if not isinstance(p, dict) or _as_float(p.get("d")) is None:
            continue
        t = _as_float(p.get("t"))
        pressure = _as_float(p.get("pressure"))
        out.append(UnifiedSample(depth=_as_float(p.get("d")), temp=_as_float(p.get("te")),
                                 time=None if t is None else int(round(t / 1000.0)), pressure=pressure,
                                 channels=SampleChannels(pressures={0: pressure}) if pressure is not None else None))
    return out


def logbook_dive_to_unified(entry: SsiLogbookDive, sites: Optional[Dict[int, SsiSite]] = None) -> Optional[UnifiedDive]:
    """One logbook dive as a `UnifiedDive`, from the keys the research
    documents (`build_payload` writes the same ones): local start, dive time
    (whole minutes), max and average depth, water temperature min/max,
    number, comment, surface interval, weight (kg), visibility (m), start
    position, the site's name through ``sites`` (id -> `SsiSite`), the main
    tank (volume, start/end bar, EAN percent), GF, the computer's name,
    serial and firmware, ``device_logged`` from the imported flag, the
    profile from ``_diveSamples`` and ``external_ids['ssi']``.

    **Left out** (no documented key, or ids only): buddies (SSI buddy ids),
    rating, the ``_var_*_id`` dropdowns (water type, weather, ...), the
    second and further tanks (I7b), helium, the datasets, the imperial
    twins, air temperature. A dive without a start is skipped (None)."""
    raw = entry.raw
    if entry.start is None:
        return None
    sites = sites or {}
    site = sites.get(entry.site_id) if entry.site_id is not None else None
    tank: List[GasMixture] = []
    volume = _as_float(raw.get("odin_user_log_tank_vol_l"))
    start_bar = _as_float(raw.get("odin_user_log_pressure_start_bar"))
    end_bar = _as_float(raw.get("odin_user_log_pressure_end_bar"))
    ean_percent = _as_float(raw.get("odin_user_log_ean_percent")) if _as_int(raw.get("odin_user_log_ean")) else None
    if any(v is not None for v in (volume, start_bar, end_bar, ean_percent)):
        tank.append(GasMixture(oxygen=ean_percent if ean_percent is not None else 21.0, start_pressure=start_bar,
                               end_pressure=end_bar, tank_volume=volume))
    imported = raw.get("odin_user_log_divecomputer_imported")
    vendor = _as_text(raw.get("odin_user_log_manufacturer"))
    name = _as_text(raw.get("odin_user_log_divecomputer_name"))
    if name and vendor and name.lower().startswith(vendor.lower()):
        name = name[len(vendor):].strip() or None
    return UnifiedDive(
        date_time=entry.start,
        duration=(entry.divetime_min or 0) * 60,
        max_depth=entry.max_depth if entry.max_depth is not None else 0.0,
        avg_depth=_as_float(raw.get("odin_user_log_avg_depth_m")),
        temp_min=_as_float(raw.get("odin_user_log_watertemp_c")),
        temp_max=_as_float(raw.get("odin_user_log_watertemp_max_c")),
        external_ids={SERVICE_ID: entry.id} if entry.id else {},
        gas_mixtures=tank,
        location=site.name if site else None,
        notes=_as_text(raw.get("odin_user_log_comment")),
        dive_number=entry.number,
        weight=_as_float(raw.get("odin_user_log_weight_kg")),
        weight_unit="kilogram" if _as_float(raw.get("odin_user_log_weight_kg")) is not None else None,
        visibility=_as_float(raw.get("odin_user_log_vis_m")),
        visibility_unit="meter" if _as_float(raw.get("odin_user_log_vis_m")) is not None else None,
        lat=_as_float(raw.get("odin_user_log_pos_start_latitude")),
        lng=_as_float(raw.get("odin_user_log_pos_start_longitude")),
        samples=_parse_samples_text(raw.get("odin_user_log_diveSamples")),
        device_logged=bool(imported) if isinstance(imported, (bool, int)) and not isinstance(imported, str) else None,
        service_fields={"site_id": entry.site_id} if entry.site_id is not None else {},
        computer_vendor=vendor,
        computer_model=name,
        computer_serial=_as_text(raw.get("odin_user_log_serial_nr")),
        computer_firmware=_as_text(raw.get("odin_user_log_firmware")),
        gf_low=_as_int(raw.get("odin_user_log_gf_set_1")),
        gf_high=_as_int(raw.get("odin_user_log_gf_set_2")),
        surface_interval=_as_int(raw.get("odin_user_log_si_before")),
    )


class SsiAdapter(BaseDiveAdapter):
    """MySSI as a `BaseDiveAdapter` (module docstring). Reads the logbook,
    adds dives through `upload_dives` (duplicate check, numbering after the
    highest, read-back), refuses updates and deletes. Not a sync side: kept
    out of `pairs.KNOWN_SERVICES` and every adapter lookup on purpose; the
    Convert page constructs it from the keychain login."""

    service_id = SERVICE_ID
    display_name = "MySSI"
    stores_external_ids = False
    accepts_new_dives = True

    @classmethod
    def field_catalog(cls) -> List[FieldSpec]:
        """What `logbook_dive_to_unified` reads. Nothing is writable: there
        is no documented update call (``add_dive`` writes a whole new dive)."""
        return [
            FieldSpec(key="ssi.date_time", label="Start time", type="datetime", unified="date_time", writable=False),
            FieldSpec(key="ssi.duration", label="Dive time", type="number", unified="duration", unit="s", writable=False),
            FieldSpec(key="ssi.max_depth", label="Max depth", type="number", unified="max_depth", unit="m", writable=False),
            FieldSpec(key="ssi.avg_depth", label="Average depth", type="number", unified="avg_depth", unit="m", writable=False),
            FieldSpec(key="ssi.temp_min", label="Water temperature", type="number", unified="temp_min", unit="°C", writable=False),
            FieldSpec(key="ssi.temp_max", label="Highest temperature", type="number", unified="temp_max", unit="°C", writable=False),
            FieldSpec(key="ssi.dive_number", label="Dive number", type="number", unified="dive_number", writable=False),
            FieldSpec(key="ssi.location", label="Dive site", type="text", unified="location", writable=False),
            FieldSpec(key="ssi.notes", label="Comment", type="text", unified="notes", writable=False),
            FieldSpec(key="ssi.weight", label="Weight", type="number", unified="weight", unit="kg", writable=False),
            FieldSpec(key="ssi.visibility", label="Visibility", type="number", unified="visibility", unit="m", writable=False),
            FieldSpec(key="ssi.gps", label="GPS position", type="gps", unified="gps", writable=False),
            FieldSpec(key="ssi.tanks", label="Main tank", type="tanks", unified="tanks", writable=False),
            FieldSpec(key="ssi.samples", label="Dive profile", type="samples", unified="samples", writable=False),
        ]

    def __init__(self, email: str, password: str, token: Optional[str] = None, *,
                 cooldown_seconds: float = DEFAULT_COOLDOWN_S, transport: Optional[Transport] = None,
                 on_token: Optional[Callable[[str], None]] = None, sleep: Optional[Callable[[float], None]] = None):
        self.email = (email or "").strip()
        self.client = SsiClient(email, password, token, cooldown_seconds=cooldown_seconds, transport=transport,
                                on_token=on_token, sleep=sleep)
        self._logbook: Optional[SsiLogbook] = None

    def login(self) -> bool:
        """Log in (or confirm the stored token works by reading the logbook)."""
        try:
            if self.client.token:
                self._logbook = self.client.get_divelog()
            else:
                self.client.authenticate()
            return True
        except SsiError as e:
            logger.error("MySSI login failed for %s: %s", mask_email(self.email), e)
            return False

    def _sites_by_id(self, logbook: SsiLogbook) -> Dict[int, SsiSite]:
        return {s.id: s for s in logbook.sites}

    def fetch_dives(self, date_from: Optional[datetime] = None, date_to: Optional[datetime] = None) -> List[UnifiedDive]:
        logbook = self.client.get_divelog()
        self._logbook = logbook
        sites = self._sites_by_id(logbook)
        out: List[UnifiedDive] = []
        for entry in logbook.dives:
            dive = logbook_dive_to_unified(entry, sites)
            if dive is None:
                continue
            if date_from and dive.date_time < date_from:
                continue
            if date_to and dive.date_time > date_to:
                continue
            out.append(dive)
        out.sort(key=lambda d: d.date_time)
        return out

    def fetch_dive(self, external_id: str) -> Optional[UnifiedDive]:
        """One dive by its ``odin_user_log_id``; the logbook comes in one
        call, so this is a read of it."""
        logbook = self.client.get_divelog()
        self._logbook = logbook
        wanted = str(external_id).strip()
        entry = next((e for e in logbook.dives if e.id == wanted), None)
        return logbook_dive_to_unified(entry, self._sites_by_id(logbook)) if entry else None

    def add_dive(self, dive: UnifiedDive) -> Optional[str]:
        """Upload through `upload_dives` (duplicate check, numbering,
        read-back): the SSI id when the dive was sent and read back, None
        when it was skipped as a duplicate or failed (the reason is logged)."""
        result = upload_dives(self.client, [dive])[0]
        if result.status == STATUS_SENT:
            return result.ssi_id
        level = logger.warning if result.status == STATUS_SKIPPED else logger.error
        level("MySSI: dive at %s not added - %s", dive.date_time.strftime("%Y-%m-%d %H:%M"), result.reason)
        return None

    def update_dive(self, external_id: str, dive: UnifiedDive) -> bool:
        logger.error("MySSI: dive %s not updated - the app's API has no documented call to change a logged dive", external_id)
        return False

    def delete_dive(self, external_id: str) -> bool:
        logger.error("MySSI: dive %s not deleted - the app's API has no documented call to delete a logged dive", external_id)
        return False
