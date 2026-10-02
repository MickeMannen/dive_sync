"""MySSI upload client (plans/convert.md I7), against a fake backend.

Nothing here reaches the network: an autouse fixture makes every
`requests.Session.request` fail the test, and every client gets `FakeSsi`,
an in-memory ``a21.php`` whose answers are modelled on what the I7a
research documents (the real API is unproven until the owner's live test).
"""
import io
import json
import logging
import zipfile
from datetime import datetime

import pytest
import requests

from src.core.models import DiveEvent, GasMixture, SampleChannels, UnifiedDive, UnifiedSample
from src.core.services import ssi
from src.core.services.ssi import (
    API_URL, APP_IDENTITY, STATUS_FAILED, STATUS_PLANNED, STATUS_SENT, STATUS_SKIPPED, HttpResponse,
    SiteIndex, SsiAuthError, SsiClient, SsiCredentials, SsiError, SsiLogbook, SsiRateLimited, SsiResponseError,
    SsiSite, build_payload, computer_reference, describe_tank, find_duplicate, mask_email, parse_logbook,
    parse_site_record, parse_ssi_datetime, plan_upload, preselect_site, scrub, ssi_drops, upload_dives,
    verify_upload,
)

EMAIL = "diver@example.com"
PASSWORD = "s3cret-pw"
NETWORK_BLOCKED = "network blocked by tests/test_ssi.py"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any real HTTP attempt fails the test."""
    def blocked(self, method, url, *args, **kwargs):
        raise AssertionError(f"{NETWORK_BLOCKED}: {method} {url}")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)


@pytest.fixture
def no_sleep(monkeypatch):
    calls = []
    monkeypatch.setattr(ssi.time, "sleep", lambda s: calls.append(s))
    return calls


# --- the fake backend -------------------------------------------------------------

class FakeSsi:
    """``api.divessi.com/app/a21.php`` in memory. Tokens are ``tok-<n>``;
    `expire()` invalidates them all so the next call answers
    ``authenticated: false``. ``drop_saves`` keeps a saved dive out of the
    logbook (read-back failure); ``mangle`` edits a saved dive before it is
    stored."""

    def __init__(self, dives=None, sites=None, email=EMAIL, password=PASSWORD):
        self.logbook = [dict(d) for d in (dives or [])]
        self.sites = list(sites or [])
        self.email, self.password = email, password
        self.calls = []
        self.saved = []
        self.valid_tokens = set()
        self.logins = 0
        self.next_id = 900
        self.drop_saves = False
        self.mangle = None
        self.fail_next = None        # an HttpResponse to answer the next call with
        self.raise_next = None       # an exception to raise on the next call

    def expire(self):
        self.valid_tokens.clear()

    def whats(self):
        return [c["what"] for c in self.calls]

    def __call__(self, method, url, params, data, timeout):
        what = params.get("what")
        self.calls.append({"method": method, "url": url, "what": what, "params": dict(params), "data": data})
        if self.raise_next is not None:
            exc, self.raise_next = self.raise_next, None
            raise exc
        if self.fail_next is not None:
            response, self.fail_next = self.fail_next, None
            return response
        assert url == API_URL
        for key, value in APP_IDENTITY.items():
            assert params.get(key) == value, f"app identity {key} missing"
        if what == "authenticate":
            self.logins += 1
            if params.get("l") == self.email and params.get("p") == self.password:
                token = f"tok-{self.logins}"
                self.valid_tokens.add(token)
                return self._json({"authenticated": True, "token": token, "mid": "12345", "imperial": False,
                                   "authenticated_email": self.email})
            return self._json({"authenticated": False})
        if params.get("token") not in self.valid_tokens:
            return self._json({"authenticated": False})
        if what == "get_divelog":
            return self._json({"authenticated": True, "logbook_details": list(self.logbook),
                               "logbook_sites": list(self.sites), "logbook_buddies": [],
                               "stats": {"dives": len(self.logbook)}})
        if what == "get_divelog_vars":
            return self._json({"authenticated": True, "watertype": [{"id": 5, "name": "Salt"}, {"id": 4, "name": "Fresh"}]})
        if what == "save_divelog":
            assert method == "POST" and data is not None and set(data) == {"json_data"}
            dive = json.loads(data["json_data"])
            self.saved.append(dive)
            stored = dict(dive)
            stored["odin_user_log_id"] = str(self.next_id)
            self.next_id += 1
            if self.mangle:
                self.mangle(stored)
            if not self.drop_saves:
                self.logbook.append(stored)
            return self._json({"authenticated": True, "success": True})
        return HttpResponse(404, b"unknown what")

    @staticmethod
    def _json(obj):
        return HttpResponse(200, json.dumps(obj).encode())


def logbook_dive(number, date, time_, depth=18.0, divetime=45, ref=None, **extra):
    """One ``logbook_details`` object as MySSI returns a hand-logged dive."""
    raw = {
        "odin_user_log_id": str(100 + number),
        "odin_user_log_nr": number,
        "odin_user_log_date": date,
        "odin_user_log_entry_time": time_,
        "odin_user_log_datetime": f"{date}+{time_}:00.000",
        "odin_user_log_divetime": divetime,
        "odin_user_log_depth_m": depth,
        "odin_user_log_divecomputer_dive_ref": ref,
        "odin_user_log_dive_sites_id": None,
    }
    raw.update(extra)
    return raw


def make_dive(start="2026-06-27 10:00", number=None, tanks=None, samples=True, activity="24449823352", **kwargs):
    gas = tanks if tanks is not None else [GasMixture(oxygen=32.0, start_pressure=200.0, end_pressure=70.0, tank_volume=12.0, tank_name="Tank 1")]
    profile = []
    if samples:
        for t in range(0, 61, 3):
            depth = min(18.0, t * 0.6) if t < 45 else max(0.0, 18.0 - (t - 45) * 1.2)
            profile.append(UnifiedSample(depth=round(depth, 2), temp=28.0 - t * 0.01, time=t, pressure=200.0 - t * 2.0))
    fields = dict(
        date_time=datetime.strptime(start, "%Y-%m-%d %H:%M"),
        duration=2700, max_depth=18.0, avg_depth=11.2, temp_min=27.5, temp_max=29.0,
        dive_number=number, gas_mixtures=gas, samples=profile, lat=-10.0, lng=-30.0,
        external_ids={"garmin": activity} if activity else {},
        computer_vendor="Garmin", computer_model="Descent Mk3i", computer_serial="1000000001", computer_firmware="19.10",
        gf_low=40, gf_high=85, notes="nice dive", surface_interval=3600,
    )
    fields.update(kwargs)
    return UnifiedDive(**fields)


SITES = [
    {"odin_dive_sites_id": 501, "odin_dive_sites_name": "House Reef", "odin_dive_sites_latitude": -10.001, "odin_dive_sites_longitude": -30.001,
     "odin_dive_sites_country": "Testland", "odin_dive_sites_region": "South"},
    {"odin_dive_sites_id": 502, "odin_dive_sites_name": "Far Wall", "odin_dive_sites_latitude": -10.08, "odin_dive_sites_longitude": -30.0,
     "odin_dive_sites_country": "Testland", "odin_dive_sites_region": "South"},
    {"odin_dive_sites_id": 503, "odin_dive_sites_name": "Nowhere Pinnacle", "odin_dive_sites_latitude": None, "odin_dive_sites_longitude": None},
]


def client_for(fake, token=None, **kwargs):
    kwargs.setdefault("sleep", lambda s: None)
    return SsiClient(EMAIL, PASSWORD, token, transport=fake, **kwargs)


# --- identity, scrubbing, credentials -------------------------------------------------

def test_route_and_identity_constants_in_one_place():
    assert API_URL == "https://api.divessi.com/app/a21.php"
    assert APP_IDENTITY == {"ssiapp": "0815_ADR", "lang": "en", "version": "ADR_4.1.268-ssi", "context": "s"}
    assert "unofficial" in ssi.__doc__.lower() and "divebridge" in ssi.__doc__
    assert "can stop working" in ssi.UNOFFICIAL_NOTE


def test_every_request_carries_the_app_identity_and_operation():
    fake = FakeSsi()
    client = client_for(fake)
    client.authenticate()
    client.get_divelog()
    for call in fake.calls:
        assert call["url"] == API_URL
        assert all(call["params"][k] == v for k, v in APP_IDENTITY.items())
    assert fake.whats() == ["authenticate", "get_divelog"]
    assert fake.calls[0]["params"]["l"] == EMAIL and fake.calls[0]["params"]["p"] == PASSWORD
    assert fake.calls[1]["params"]["token"] == "tok-1"


def test_scrub_cuts_query_strings_and_secrets():
    text = f"HTTPSConnectionPool: {API_URL}?what=authenticate&l=a@b.c&p={PASSWORD} failed; token tok-9 bad"
    out = scrub(text, (PASSWORD, "tok-9"))
    assert PASSWORD not in out and "tok-9" not in out and "what=authenticate" not in out
    assert out.startswith(f"HTTPSConnectionPool: {API_URL}?... failed")
    assert scrub(None, ("",)) == "None"


def test_mask_email():
    assert mask_email("mikael@example.com") == "m***@example.com"
    assert mask_email("nonsense") == "***" and mask_email("") == ""


def test_credentials_model():
    assert SsiCredentials().configured is False
    assert SsiCredentials(email=" a@b.c ", password="x").configured is True
    assert SsiCredentials(email="a@b.c").configured is False


# --- auth, token reuse and expiry, errors -----------------------------------------------

def test_authenticate_success_tells_on_token():
    fake = FakeSsi()
    seen = []
    client = client_for(fake, on_token=seen.append)
    assert client.authenticate() == "tok-1"
    assert seen == ["tok-1"] and client.master_id == "12345" and client.imperial is False


def test_authenticate_bad_password():
    fake = FakeSsi(password="other")
    with pytest.raises(SsiAuthError):
        client_for(fake).authenticate()
    with pytest.raises(SsiAuthError):
        SsiClient("", "", transport=fake, sleep=lambda s: None).authenticate()


def test_token_reuse_skips_login():
    fake = FakeSsi()
    fake.valid_tokens.add("stored-token")
    client = client_for(fake, token="stored-token")
    client.get_divelog()
    assert fake.whats() == ["get_divelog"] and fake.logins == 0


def test_expired_token_relogs_once_and_retries():
    fake = FakeSsi()
    seen = []
    client = client_for(fake, token="stale", on_token=seen.append)
    logbook = client.get_divelog()
    assert fake.whats() == ["get_divelog", "authenticate", "get_divelog"]
    assert client.token == "tok-1" and seen == ["tok-1"] and logbook.dives == []


def test_expired_token_and_rejected_relogin_is_an_auth_error():
    fake = FakeSsi(password="changed")
    with pytest.raises(SsiAuthError):
        client_for(fake, token="stale").get_divelog()
    assert fake.whats() == ["get_divelog", "authenticate"]


def test_http_errors():
    fake = FakeSsi()
    client = client_for(fake, token="t")
    fake.valid_tokens.add("t")
    fake.fail_next = HttpResponse(429, b"slow down")
    with pytest.raises(SsiRateLimited):
        client.get_divelog()
    fake.fail_next = HttpResponse(500, b"boom")
    with pytest.raises(SsiResponseError) as e:
        client.get_divelog()
    assert "HTTP 500" in str(e.value)
    fake.fail_next = HttpResponse(200, b"<html>not json</html>")
    with pytest.raises(SsiResponseError) as e:
        client.get_divelog()
    assert "no JSON" in str(e.value)


def test_transport_exception_is_scrubbed(caplog):
    fake = FakeSsi()
    fake.raise_next = requests.ConnectionError(f"Max retries for url: {API_URL}?what=authenticate&l={EMAIL}&p={PASSWORD}")
    with caplog.at_level(logging.DEBUG, logger="dive_sync.ssi"):
        with pytest.raises(SsiError) as e:
            client_for(fake).authenticate()
    assert PASSWORD not in str(e.value) and "what=authenticate" not in str(e.value)
    assert "ConnectionError" in str(e.value)
    assert PASSWORD not in caplog.text and "p=" not in caplog.text


def test_default_transport_would_use_requests_and_is_blocked_here():
    """The real transport goes through requests.Session.request, which this
    module's autouse fixture turns into a failure: no test can reach SSI."""
    client = SsiClient(EMAIL, PASSWORD, sleep=lambda s: None)
    with pytest.raises(SsiError) as e:
        client.authenticate()
    assert NETWORK_BLOCKED in str(e.value) and PASSWORD not in str(e.value) and "?what" not in str(e.value)


def test_cooldown_after_every_request(no_sleep):
    fake = FakeSsi()
    client = SsiClient(EMAIL, PASSWORD, transport=fake, cooldown_seconds=0.25)
    client.authenticate()
    client.get_divelog()
    assert no_sleep == [0.25, 0.25]
    fake.raise_next = RuntimeError("down")
    with pytest.raises(SsiError):
        client.get_divelog()
    assert no_sleep == [0.25, 0.25, 0.25]
    quiet = SsiClient(EMAIL, PASSWORD, transport=FakeSsi(), cooldown_seconds=0)
    quiet.authenticate()
    assert no_sleep == [0.25, 0.25, 0.25]


def test_get_divelog_vars_and_save_divelog_body():
    fake = FakeSsi()
    client = client_for(fake)
    assert client.get_divelog_vars()["watertype"][0]["id"] == 5
    client.save_divelog({"odin_user_log_nr": 7, "odin_user_log_comment": "å"})
    call = fake.calls[-1]
    assert call["method"] == "POST" and call["what"] == "save_divelog"
    assert json.loads(call["data"]["json_data"]) == {"odin_user_log_nr": 7, "odin_user_log_comment": "å"}


def test_download_sites_writes_the_zip(tmp_path):
    def transport(method, url, params, data, timeout):
        assert url == ssi.SITES_URL and params == {} and method == "GET"
        return HttpResponse(200, b"PK\x05\x06" + b"\0" * 18)
    client = SsiClient(EMAIL, PASSWORD, transport=transport, sleep=lambda s: None)
    out = client.download_sites(str(tmp_path / "sites.zip"))
    assert (tmp_path / "sites.zip").read_bytes().startswith(b"PK")
    assert out.endswith("sites.zip")


# --- logbook parsing -----------------------------------------------------------------------

def test_parse_ssi_datetime_forms():
    assert parse_ssi_datetime("2026-06-27+10:00:00.000") == datetime(2026, 6, 27, 10, 0)
    assert parse_ssi_datetime("2026-06-27 10:00:30") == datetime(2026, 6, 27, 10, 0, 30)
    assert parse_ssi_datetime("2026-06-27T10:00") == datetime(2026, 6, 27, 10, 0)
    assert parse_ssi_datetime("") is None and parse_ssi_datetime("yesterday") is None


def test_parse_logbook_list_and_dict_shapes():
    dives = [logbook_dive(11, "2026-06-20", "09:30", ref="garmin:1"), logbook_dive(12, "2026-06-21", "14:05", depth="22.5", divetime="50")]
    book = parse_logbook({"logbook_details": dives, "logbook_sites": SITES, "logbook_buddies": [{"id": 1}]})
    assert [d.number for d in book.dives] == [11, 12] and book.highest_number == 12
    assert book.dives[0].start == datetime(2026, 6, 20, 9, 30) and book.dives[0].computer_ref == "garmin:1"
    assert book.dives[1].max_depth == 22.5 and book.dives[1].divetime_min == 50 and book.dives[1].computer_ref is None
    assert [s.id for s in book.sites] == [501, 502, 503] and len(book.buddies) == 1
    keyed = {"a": dives[0], "b": dives[1]}
    assert [d.id for d in parse_logbook({"logbook_details": keyed}).dives] == ["111", "112"]
    assert parse_logbook({}).highest_number == 0


def test_parse_logbook_dive_without_datetime_uses_date_and_time():
    raw = logbook_dive(3, "2026-01-02", "08:15")
    del raw["odin_user_log_datetime"]
    assert ssi.SsiLogbookDive.from_raw(raw).start == datetime(2026, 1, 2, 8, 15)
    raw["odin_user_log_entry_time"] = None
    assert ssi.SsiLogbookDive.from_raw(raw).start == datetime(2026, 1, 2, 0, 0)


# --- sites -------------------------------------------------------------------------------------

def test_parse_site_record_by_key_names():
    site = parse_site_record(SITES[0])
    assert site == SsiSite(501, "House Reef", -10.001, -30.001, "Testland", "South")
    assert site.label == "House Reef (South, Testland)"
    assert parse_site_record({"id": "7", "name": "Plain", "lat": "1.5", "lng": "2.5"}) == SsiSite(7, "Plain", 1.5, 2.5)
    assert parse_site_record({"odin_dive_sites_id": 9}) == SsiSite(9, "SSI site 9")
    assert parse_site_record({"name": "no id"}) is None and parse_site_record("junk") is None


def test_site_index_nearest_within_5km_outside_and_without_gps():
    index = SiteIndex.from_records(SITES)
    dive = make_dive()                                   # at 10 S 30 W
    assert preselect_site(index, dive).id == 501          # ~160 m away
    far = make_dive(lat=-10.045, lng=-30.0)               # ~5.0 km from House Reef, 3.9 km from Far Wall
    assert preselect_site(index, far).id == 502
    nothing_near = make_dive(lat=-11.0, lng=-30.0)        # > 100 km
    assert preselect_site(index, nothing_near) is None
    assert preselect_site(index, make_dive(lat=None, lng=None)) is None
    assert preselect_site(None, dive) is None
    site, distance = index.nearest(-10.0, -30.0)
    assert site.id == 501 and 150 < distance < 170
    assert index.nearest(-10.0, -30.0, radius_m=100) is None


def test_site_index_search_and_lookup():
    index = SiteIndex.from_records(SITES)
    assert [s.id for s in index.search("reef")] == [501]
    assert [s.id for s in index.search("south")] == [502, 501]        # region matches after name matches (none) -> by name
    assert [s.id for s in index.search("testland", limit=1)] == [502]
    assert index.search("") == [] and index.search("atlantis") == []
    assert index.get(503).name == "Nowhere Pinnacle" and index.get(None) is None and index.get(1) is None
    assert len(index) == 3 and sorted(s.id for s in index) == [501, 502, 503]


def test_site_index_from_zip_json_and_csv(tmp_path):
    path = tmp_path / "APP_CACHE_SITES.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("sites.json", json.dumps({"version": 3, "sites": SITES[:2]}))
        zf.writestr("more/extra.json", json.dumps([{"odin_dive_sites_id": 504, "odin_dive_sites_name": "Extra", "lat": 1, "lon": 2}]))
        zf.writestr("centers.csv", "odin_dive_sites_id,name,latitude,longitude\n505,CSV Site,3.0,4.0\n")
        zf.writestr("readme.txt", "ignored")
        zf.writestr("broken.json", "{not json")
    index = SiteIndex.from_zip(str(path))
    assert sorted(s.id for s in index) == [501, 502, 504, 505]
    assert index.get(505) == SsiSite(505, "CSV Site", 3.0, 4.0)


def test_site_index_from_logbook_and_duplicates_keep_first():
    book = parse_logbook({"logbook_sites": SITES + [{"odin_dive_sites_id": 501, "odin_dive_sites_name": "Renamed"}]})
    index = SiteIndex.from_logbook(book)
    assert len(index) == 3 and index.get(501).name == "House Reef"


# --- payload mapping -------------------------------------------------------------------------

def test_computer_reference_variants():
    assert computer_reference(make_dive()) == "garmin:24449823352"
    assert computer_reference(make_dive(activity=None, external_ids={"shearwater": "abc", "divelogs": "9"})) == "divelogs:9"
    serial_only = make_dive(activity=None, date_time_utc=datetime(2026, 6, 27, 2, 0))
    assert computer_reference(serial_only) == "1000000001:20260627T020000"
    assert computer_reference(make_dive(activity=None, computer_serial=None)) is None


def test_build_payload_single_tank_profile_and_flags():
    dive = make_dive()
    payload = build_payload(dive, 13, site_id=501, computer_ref=computer_reference(dive))
    assert payload["odin_user_log_nr"] == 13
    assert payload["odin_user_log_date"] == "2026-06-27" and payload["odin_user_log_entry_time"] == "10:00"
    assert payload["odin_user_log_datetime"] == "2026-06-27+10:00:00.000"
    assert payload["odin_user_log_divetime"] == 45
    assert payload["odin_user_log_depth_m"] == 18.0 and payload["odin_user_log_depth_ft"] == 59.1
    assert payload["odin_user_log_avg_depth_m"] == 11.2 and payload["odin_user_log_avg_depth_ft"] == 36.7
    assert payload["odin_user_log_watertemp_c"] == 27.5 and payload["odin_user_log_watertemp_f"] == 81.5
    assert payload["odin_user_log_watertemp_max_c"] == 29.0
    assert payload["odin_user_log_comment"] == "nice dive" and payload["odin_user_log_si_before"] == 3600
    assert payload["odin_user_log_dive_sites_id"] == 501
    assert payload["odin_user_log_pos_start_latitude"] == -10.0 and payload["odin_user_log_pos_start_longitude"] == -30.0
    # main tank: EAN32, 12 l, 200 -> 70 bar, psi twins
    assert payload["odin_user_log_tank_vol_l"] == 12.0
    assert payload["odin_user_log_pressure_start_bar"] == 200.0 and payload["odin_user_log_pressure_end_bar"] == 70.0
    assert payload["odin_user_log_pressure_start_psi"] == 2901 and payload["odin_user_log_pressure_end_psi"] == 1015
    assert payload["odin_user_log_ean"] == 1 and payload["odin_user_log_ean_percent"] == 32.0
    assert payload["odin_user_log_var_tanktype_id"] is None
    # deco settings and the computer, with the imported flag off
    assert payload["odin_user_log_gf_set_1"] == 40 and payload["odin_user_log_gf_set_2"] == 85
    assert payload["odin_user_log_divecomputer_imported"] is False
    assert payload["odin_user_log_divecomputer_name"] == "Garmin Descent Mk3i"
    assert payload["odin_user_log_manufacturer"] == "Garmin" and payload["odin_user_log_serial_nr"] == "1000000001"
    assert payload["odin_user_log_firmware"] == "19.10" and payload["odin_user_log_divecomputer_dive_ref"] == "garmin:24449823352"
    # the profile: 5 s grid from 0 to 60 s, 13 points, interpolated between the 3 s records
    samples = json.loads(payload["odin_user_log_diveSamples"])
    assert [p["t"] for p in samples] == [t * 1000 for t in range(0, 61, 5)]
    assert samples[0] == {"t": 0, "d": 0.0, "te": 28.0, "pressure": 200.0}
    assert samples[1]["d"] == 3.0 and samples[1]["pressure"] == 190.0    # t=5 s between the 3 s and 6 s records
    assert samples[-1]["t"] == 60000 and samples[-1]["pressure"] == 80.0
    assert json.loads(payload["odin_user_log_depthDataset"]) == [p["d"] for p in samples]
    assert json.loads(payload["odin_user_log_tempDataset"])[0] == 28.0
    assert json.loads(payload["odin_user_log_tankPressureDataset"]) == json.loads(payload["odin_user_log_pressureDataset"])
    assert payload["odin_user_log_gfSurfDataset"] is None
    # nothing undocumented was guessed
    assert not any(k.startswith("odin_user_log_var_") and k != "odin_user_log_var_tanktype_id" for k in payload)
    assert "ndl" not in samples[0] and "mf" not in samples[0]
    # the body is JSON-serialisable as the client sends it
    json.dumps(payload)


def test_build_payload_air_no_site_no_profile_imperial_units():
    dive = make_dive(samples=False, tanks=[GasMixture(oxygen=21.0)], weight=12.0, weight_unit="pound",
                     visibility=60.0, visibility_unit="foot", avg_depth=None, temp_min=None, temp_max=None, notes=None)
    payload = build_payload(dive, 1)
    assert payload["odin_user_log_ean"] is None and payload["odin_user_log_ean_percent"] is None
    assert payload["odin_user_log_dive_sites_id"] is None
    assert payload["odin_user_log_diveSamples"] is None and payload["odin_user_log_depthDataset"] is None
    assert payload["odin_user_log_tankPressureDataset"] is None
    assert payload["odin_user_log_pressure_start_bar"] is None and payload["odin_user_log_pressure_start_psi"] is None
    assert payload["odin_user_log_weight_kg"] == 5.4 and payload["odin_user_log_vis_m"] == 18.3
    assert payload["odin_user_log_avg_depth_ft"] is None and payload["odin_user_log_watertemp_f"] is None
    assert payload["odin_user_log_comment"] is None
    no_tank = build_payload(make_dive(tanks=[], samples=False), 2)
    assert no_tank["odin_user_log_tank_vol_l"] is None and no_tank["odin_user_log_ean"] is None


def test_build_payload_pressure_from_channels_of_tank_0():
    samples = [UnifiedSample(depth=0.0, time=0, channels=SampleChannels(pressures={0: 180.0, 1: 150.0})),
               UnifiedSample(depth=10.0, time=10, channels=SampleChannels(pressures={1: 140.0}), pressure=170.0)]
    payload = build_payload(make_dive(samples=False).model_copy(update={"samples": samples}), 1)
    grid = json.loads(payload["odin_user_log_diveSamples"])
    assert [p.get("pressure") for p in grid] == [180.0, 175.0, 170.0]      # tank 0 first, the plain pressure as fallback
    assert "te" not in grid[0]


def test_describe_tank_and_dropped_tanks_message():
    tanks = [GasMixture(oxygen=32.0, start_pressure=200.0, end_pressure=70.0, tank_volume=12.0),
             GasMixture(oxygen=50.0, start_pressure=200.0, end_pressure=80.0, tank_volume=7.0, tank_name="Deco"),
             GasMixture(oxygen=18.0, helium=45.0, tank_volume=24.0),
             GasMixture(oxygen=21.0, start_pressure=210.0)]
    assert describe_tank(1, tanks[1]) == "tank 2 (Deco): EAN50, 7 l, 200 -> 80 bar"
    assert describe_tank(2, tanks[2]) == "tank 3: TMX 18/45, 24 l"
    assert describe_tank(3, tanks[3]) == "tank 4: air, 210 -> ? bar"
    drops = ssi_drops(make_dive(tanks=tanks))
    assert drops[0] == ("MySSI gets the main tank only; dropped: tank 2 (Deco): EAN50, 7 l, 200 -> 80 bar; "
                        "tank 3: TMX 18/45, 24 l; tank 4: air, 210 -> ? bar")
    payload = build_payload(make_dive(tanks=tanks), 5)
    assert payload["odin_user_log_ean_percent"] == 32.0 and payload["odin_user_log_tank_vol_l"] == 12.0


def test_ssi_drops_lists_what_is_not_carried():
    assert ssi_drops(make_dive()) == []
    dive = make_dive(tanks=[GasMixture(oxygen=18.0, helium=45.0)], buddy="Pat", timezone="Etc/GMT-8",
                     events=[DiveEvent(time=10, type="alert", name="ascent")])
    dive.samples[0].channels = SampleChannels(ndl=600, cns=3.0)
    drops = ssi_drops(dive)
    assert drops == [
        "helium (45 %) of the main tank: MySSI's helium field is unconfirmed, the mix is sent as EAN18",
        "buddy (MySSI takes buddy ids from its own list, not a name)",
        "events (gas switches, alerts)",
        "per-sample cns, ndl",
        "time zone (MySSI stores the local time only)",
    ]
    assert ssi_drops(make_dive(location="Blue Hole", lat=None, lng=None))[0].startswith("site name")
    assert ssi_drops(make_dive(location="Blue Hole")) == []


# --- duplicates, numbering, planning ---------------------------------------------------------------

def test_find_duplicate_by_reference_and_by_time():
    book = parse_logbook({"logbook_details": [
        logbook_dive(11, "2026-06-20", "09:30", ref="garmin:24449823352"),
        logbook_dive(12, "2026-06-27", "10:01"),
    ]})
    assert find_duplicate(book, make_dive(start="2026-06-25 15:00")).number == 11          # same reference, other time
    assert find_duplicate(book, make_dive(start="2026-06-27 09:59", activity=None)).number == 12    # 2 min window
    assert find_duplicate(book, make_dive(start="2026-06-27 09:58", activity=None)) is None          # 3 min: another dive
    assert find_duplicate(book, make_dive(start="2026-06-27 10:03", activity=None)).number == 12
    assert find_duplicate(book, make_dive(start="2026-06-27 10:04", activity=None)) is None
    assert find_duplicate(SsiLogbook(dives=[]), make_dive()) is None


def test_plan_upload_numbers_after_highest_and_skips_duplicates():
    book = parse_logbook({"logbook_details": [logbook_dive(11, "2026-06-20", "09:30", ref="garmin:1"),
                                              logbook_dive(42, "2026-06-21", "09:30")],
                          "logbook_sites": SITES})
    dives = [make_dive(start="2026-06-21 09:31", activity=None), make_dive(start="2026-06-22 10:00", activity="2"),
             make_dive(start="2026-06-23 10:00", activity="1"), make_dive(start="2026-06-24 10:00", activity="3", lat=None, lng=None)]
    plan = plan_upload(book, dives, SiteIndex.from_logbook(book))
    assert [r.status for r in plan] == [STATUS_SKIPPED, STATUS_PLANNED, STATUS_SKIPPED, STATUS_PLANNED]
    assert [r.number for r in plan] == [None, 43, None, 44]
    assert plan[0].reason == "already in MySSI as dive 42 (starts within 2 minutes)"
    assert plan[2].reason == "already in MySSI as dive 11 (same dive computer reference)"
    assert plan[1].site.id == 501 and plan[3].site is None
    assert plan[1].dropped == [] and plan[1].summary == "2026-06-22 10:00: will be sent as dive 43"
    assert plan[0].summary.startswith("2026-06-21 09:31: skipped, already in MySSI")


def test_verify_upload_outcomes():
    sent = make_dive()
    ref = computer_reference(sent)
    ok = parse_logbook({"logbook_details": [logbook_dive(13, "2026-06-27", "10:00", depth=18.2, divetime=45, ref=ref)]})
    found, problems = verify_upload(ok, sent, 13, ref)
    assert found.number == 13 and problems == []
    by_number = parse_logbook({"logbook_details": [logbook_dive(13, "2026-06-27", "10:01", depth=18.0, divetime=45)]})
    found, problems = verify_upload(by_number, sent, 13, None)
    assert found is not None and problems == []
    missing, problems = verify_upload(parse_logbook({}), sent, 13, ref)
    assert missing is None and problems == ["not found in the logbook after sending"]
    wrong = parse_logbook({"logbook_details": [logbook_dive(14, "2026-06-27", "10:00", depth=25.0, divetime=50, ref=ref)]})
    found, problems = verify_upload(wrong, sent, 13, ref)
    assert found.number == 14 and problems == ["number 14 instead of 13", "depth 25 m instead of 18 m", "dive time 50 min instead of 45 min"]


# --- the full flow ---------------------------------------------------------------------------------------

def test_upload_dives_full_flow_numbers_skips_and_verifies(no_sleep):
    fake = FakeSsi(dives=[logbook_dive(11, "2026-06-20", "09:30", ref="garmin:1"), logbook_dive(12, "2026-06-21", "09:30")],
                   sites=SITES)
    client = SsiClient(EMAIL, PASSWORD, transport=fake, cooldown_seconds=0.1)
    dives = [make_dive(start="2026-06-22 10:00", activity="2"),
             make_dive(start="2026-06-21 09:31", activity=None),          # duplicate by time
             make_dive(start="2026-06-23 11:00", activity="3", lat=-11.0, lng=-30.0)]
    seen = []
    results = upload_dives(client, dives, progress=seen.append)
    assert [r.status for r in results] == [STATUS_SENT, STATUS_SKIPPED, STATUS_SENT]
    assert [r.number for r in results] == [13, None, 14]
    assert results[0].site.id == 501 and results[2].site is None
    assert results[0].ssi_id == "900" and results[2].ssi_id == "901"
    assert results[0].summary == "2026-06-22 10:00: sent as dive 13"
    assert seen == results
    # login, read first, then per sent dive: save + read back
    assert fake.whats() == ["authenticate", "get_divelog", "save_divelog", "get_divelog", "save_divelog", "get_divelog"]
    assert [d["odin_user_log_nr"] for d in fake.saved] == [13, 14]
    assert fake.saved[0]["odin_user_log_dive_sites_id"] == 501 and fake.saved[1]["odin_user_log_dive_sites_id"] is None
    assert fake.saved[0]["odin_user_log_divecomputer_imported"] is False
    assert len(no_sleep) == len(fake.calls) and set(no_sleep) == {0.1}
    # a second run finds everything already there
    again = upload_dives(client, dives)
    assert [r.status for r in again] == [STATUS_SKIPPED] * 3
    assert again[0].reason == "already in MySSI as dive 13 (same dive computer reference)"


def test_upload_dives_chosen_sites_override_the_preselection():
    fake = FakeSsi(sites=SITES)
    client = client_for(fake)
    dives = [make_dive(start="2026-06-22 10:00", activity="2"), make_dive(start="2026-06-23 10:00", activity="3")]
    results = upload_dives(client, dives, site_ids={0: 502, 1: None})
    assert [r.status for r in results] == [STATUS_SENT, STATUS_SENT]
    assert results[0].site.id == 502 and results[1].site is None
    assert [d["odin_user_log_dive_sites_id"] for d in fake.saved] == [502, None]
    extra = upload_dives(client, [make_dive(start="2026-06-24 10:00", activity="4")], site_ids={0: 999},
                         site_index=SiteIndex.from_records(SITES))
    assert extra[0].status == STATUS_SENT and extra[0].site is None and fake.saved[-1]["odin_user_log_dive_sites_id"] == 999


def test_upload_dives_readback_failures():
    fake = FakeSsi()
    client = client_for(fake)
    fake.drop_saves = True
    gone = upload_dives(client, [make_dive(start="2026-06-22 10:00", activity="2")])
    assert gone[0].status == STATUS_FAILED and gone[0].reason == "sent, but not found in the logbook after sending"
    assert gone[0].number == 1
    fake.drop_saves = False
    fake.mangle = lambda stored: stored.update({"odin_user_log_depth_m": 9.0})
    differs = upload_dives(client, [make_dive(start="2026-06-23 10:00", activity="3")])
    assert differs[0].status == STATUS_FAILED
    assert differs[0].reason == "sent, but the read-back differs: depth 9 m instead of 18 m"
    assert differs[0].ssi_id == "901"
    # the mangled dive is in the logbook with the number, so the next new dive is numbered after it
    fake.mangle = None
    nxt = upload_dives(client, [make_dive(start="2026-06-24 10:00", activity="4")])
    assert nxt[0].status == STATUS_SENT and nxt[0].number == 2


def test_upload_dives_rate_limit_stops_the_batch():
    fake = FakeSsi()
    client = client_for(fake)
    dives = [make_dive(start=f"2026-06-2{i} 10:00", activity=str(i)) for i in range(1, 4)]
    client.ensure_token()
    client.get_divelog()
    fake.calls.clear()
    # the first save works, the second answers 429
    original = fake.__call__

    def transport(method, url, params, data, timeout):
        if params.get("what") == "save_divelog" and len(fake.saved) == 1:
            fake.calls.append({"what": "save_divelog", "method": method, "url": url, "params": params, "data": data})
            return HttpResponse(429, b"")
        return original(method, url, params, data, timeout)

    client._transport = transport
    results = upload_dives(client, dives)
    assert [r.status for r in results] == [STATUS_SENT, STATUS_FAILED, STATUS_FAILED]
    assert "rate limit" in results[1].reason and results[2].reason.startswith("not attempted: MySSI rate limit")
    assert [c["what"] for c in fake.calls] == ["get_divelog", "save_divelog", "get_divelog", "save_divelog"]


def test_upload_dives_other_errors_continue_and_auth_errors_stop():
    fake = FakeSsi()
    client = client_for(fake)
    dives = [make_dive(start=f"2026-06-2{i} 10:00", activity=str(i)) for i in range(1, 4)]
    client.ensure_token()
    original = fake.__call__

    def flaky(method, url, params, data, timeout):
        if params.get("what") == "save_divelog" and not fake.saved:
            fake.saved.append({})       # consume the first attempt with a server error
            return HttpResponse(503, b"")
        return original(method, url, params, data, timeout)

    client._transport = flaky
    results = upload_dives(client, dives)
    assert [r.status for r in results] == [STATUS_FAILED, STATUS_SENT, STATUS_SENT]
    assert "HTTP 503" in results[0].reason and [r.number for r in results] == [1, 1, 2]

    broken = FakeSsi(password="changed")
    results = upload_dives(client_for(broken, token="stale"), dives[:2])
    assert [r.status for r in results] == [STATUS_FAILED, STATUS_FAILED]
    assert results[0].reason == "logbook not read: MySSI did not accept the email and password"


def test_full_fake_flow_logs_hold_neither_password_nor_token(caplog):
    fake = FakeSsi(dives=[logbook_dive(11, "2026-06-20", "09:30")], sites=SITES)
    fake.raise_next = None
    client = client_for(fake, token="stale")
    with caplog.at_level(logging.DEBUG, logger="dive_sync.ssi"):
        results = upload_dives(client, [make_dive(start="2026-06-22 10:00", activity="2")])
        fake.raise_next = requests.ConnectionError(f"url: {API_URL}?what=get_divelog&token={client.token}")
        with pytest.raises(SsiError):
            client.get_divelog()
    assert results[0].status == STATUS_SENT
    assert caplog.text, "the flow logs something"
    for secret in (PASSWORD, "tok-1", "stale", "token=", "p=", API_URL + "?"):
        assert secret not in caplog.text, secret
    assert EMAIL not in caplog.text and "d***@example.com" in caplog.text


# --- the adapter ----------------------------------------------------------------------------------------

def adapter_for(fake, token=None, **kwargs):
    kwargs.setdefault("sleep", lambda s: None)
    return ssi.SsiAdapter(EMAIL, PASSWORD, token, transport=fake, **kwargs)


def test_adapter_is_a_base_dive_adapter_with_a_read_only_catalogue():
    from src.core.adapter import BaseDiveAdapter
    adapter = adapter_for(FakeSsi())
    assert isinstance(adapter, BaseDiveAdapter)
    assert ssi.SsiAdapter.service_id == "ssi" and ssi.SsiAdapter.display_name == "MySSI"
    assert ssi.SsiAdapter.accepts_new_dives is True and ssi.SsiAdapter.stores_external_ids is False
    catalog = ssi.SsiAdapter.field_catalog()          # callable without a login
    assert catalog and all(spec.key.startswith("ssi.") for spec in catalog)
    assert not any(spec.writable for spec in catalog), "no documented update call: nothing is writable"
    assert {spec.unified for spec in catalog} >= {"date_time", "duration", "max_depth", "dive_number", "location", "samples", "tanks"}
    assert "What this module is not" not in ssi.__doc__ and "SsiAdapter" in ssi.__doc__


def test_adapter_stays_out_of_the_sync_registry():
    from src.core import pairs
    assert "ssi" not in pairs.KNOWN_SERVICES
    with pytest.raises(ValueError):
        pairs.parse_service_spec("ssi")
    from src.core.config import SettingsModel
    with pytest.raises(ValueError):
        pairs.build_adapter("ssi", SettingsModel())
    with pytest.raises(ValueError):
        pairs.field_catalog_of("ssi")


def test_adapter_login_paths():
    fake = FakeSsi()
    assert adapter_for(fake).login() is True and fake.whats() == ["authenticate"]
    fake.calls.clear()
    fake.valid_tokens.add("kept")
    assert adapter_for(fake, token="kept").login() is True and fake.whats() == ["get_divelog"]
    assert adapter_for(FakeSsi(password="other")).login() is False       # no raise, like the other adapters
    assert ssi.SsiAdapter("", "", transport=fake, sleep=lambda s: None).login() is False


def test_adapter_add_dive_then_fetch_dives_round_trip():
    fake = FakeSsi(sites=SITES)
    adapter = adapter_for(fake)
    sent = make_dive(start="2026-06-22 10:00", activity="2", weight=6.0, weight_unit="kilogram", visibility=15.0, visibility_unit="meter")
    assert adapter.add_dive(sent) == "900"
    dives = adapter.fetch_dives()
    assert len(dives) == 1
    got = dives[0]
    assert got.date_time == datetime(2026, 6, 22, 10, 0) and got.duration == 2700 and got.max_depth == 18.0
    assert got.avg_depth == 11.2 and got.temp_min == 27.5 and got.temp_max == 29.0 and got.dive_number == 1
    assert got.notes == "nice dive" and got.surface_interval == 3600 and (got.lat, got.lng) == (-10.0, -30.0)
    assert got.location == "House Reef" and got.service_fields == {"site_id": 501}
    assert got.external_ids == {"ssi": "900"}
    assert got.weight == 6.0 and got.weight_unit == "kilogram" and got.visibility == 15.0 and got.visibility_unit == "meter"
    assert len(got.gas_mixtures) == 1
    tank = got.gas_mixtures[0]
    assert (tank.oxygen, tank.start_pressure, tank.end_pressure, tank.tank_volume) == (32.0, 200.0, 70.0, 12.0)
    assert got.gf_low == 40 and got.gf_high == 85
    assert (got.computer_vendor, got.computer_model, got.computer_serial, got.computer_firmware) == ("Garmin", "Descent Mk3i", "1000000001", "19.10")
    assert got.device_logged is False                   # the imported flag was sent off
    assert [s.time for s in got.samples] == list(range(0, 61, 5))
    assert got.samples[1].depth == 3.0 and got.samples[1].pressure == 190.0 and got.samples[1].channels.pressures == {0: 190.0}
    assert got.samples[0].temp == 28.0
    # date filters, and the id lookup
    assert adapter.fetch_dives(date_from=datetime(2026, 6, 23)) == []
    assert adapter.fetch_dives(date_to=datetime(2026, 6, 21)) == []
    assert adapter.fetch_dive("900").dive_number == 1 and adapter.fetch_dive("nope") is None


def test_adapter_fetch_dives_hand_logged_entries_and_order():
    minimal = {"odin_user_log_id": "5", "odin_user_log_nr": 3, "odin_user_log_date": "2026-01-02", "odin_user_log_entry_time": "08:15",
               "odin_user_log_divecomputer_imported": "1"}
    no_start = {"odin_user_log_id": "6", "odin_user_log_nr": 4}
    later = logbook_dive(9, "2026-03-01", "09:00", depth=12.5, divetime=40)
    fake = FakeSsi(dives=[later, minimal, no_start])
    dives = adapter_for(fake).fetch_dives()
    assert [d.dive_number for d in dives] == [3, 9]                     # sorted by start, the dive without one skipped
    hand = dives[0]
    assert hand.date_time == datetime(2026, 1, 2, 8, 15) and hand.duration == 0 and hand.max_depth == 0.0
    assert hand.gas_mixtures == [] and hand.samples == [] and hand.location is None and hand.device_logged is None
    assert hand.external_ids == {"ssi": "5"} and hand.computer_vendor is None
    assert dives[1].duration == 2400 and dives[1].max_depth == 12.5


def test_logbook_dive_to_unified_tank_and_computer_variants():
    raw = logbook_dive(1, "2026-01-02", "08:15", **{"odin_user_log_pressure_start_bar": "200", "odin_user_log_ean": 0,
                                                     "odin_user_log_ean_percent": 32, "odin_user_log_divecomputer_name": "Perdix 2",
                                                     "odin_user_log_manufacturer": "Shearwater", "odin_user_log_divecomputer_imported": True,
                                                     "odin_user_log_diveSamples": "not json"})
    dive = ssi.logbook_dive_to_unified(ssi.SsiLogbookDive.from_raw(raw))
    assert dive.gas_mixtures[0].oxygen == 21.0 and dive.gas_mixtures[0].start_pressure == 200.0   # ean flag off: air
    assert dive.computer_vendor == "Shearwater" and dive.computer_model == "Perdix 2" and dive.device_logged is True
    assert dive.samples == []
    assert ssi.logbook_dive_to_unified(ssi.SsiLogbookDive.from_raw({"odin_user_log_id": "1"})) is None
    assert ssi._parse_samples_text('[{"t": 5000, "d": 2.5}, {"x": 1}, "junk"]')[0].channels is None
    assert ssi._parse_samples_text("[1, 2]") == [] and ssi._parse_samples_text('{"a": 1}') == []


def test_adapter_add_dive_duplicate_and_failure_return_none(caplog):
    fake = FakeSsi()
    adapter = adapter_for(fake)
    dive = make_dive(start="2026-06-22 10:00", activity="2")
    assert adapter.add_dive(dive) == "900"
    with caplog.at_level(logging.WARNING, logger="dive_sync.ssi"):
        assert adapter.add_dive(dive) is None                            # duplicate: skipped, nothing sent
    assert "already in MySSI as dive 1" in caplog.text and len(fake.saved) == 1
    fake.drop_saves = True
    assert adapter.add_dive(make_dive(start="2026-06-23 10:00", activity="3")) is None
    assert "not found in the logbook" in caplog.text


def test_adapter_update_and_delete_refuse_without_a_request(caplog):
    fake = FakeSsi()
    fake.valid_tokens.add("t")
    adapter = adapter_for(fake, token="t")
    with caplog.at_level(logging.ERROR, logger="dive_sync.ssi"):
        assert adapter.update_dive("900", make_dive()) is False
        assert adapter.delete_dive("900") is False
    assert fake.calls == []
    assert "not updated" in caplog.text and "not deleted" in caplog.text


def test_adapter_honours_the_cooldown(no_sleep):
    adapter = ssi.SsiAdapter(EMAIL, PASSWORD, transport=FakeSsi(), cooldown_seconds=0.2)
    adapter.login()
    adapter.fetch_dives()
    assert no_sleep == [0.2, 0.2]
