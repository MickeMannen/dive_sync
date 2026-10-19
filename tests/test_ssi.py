"""MySSI upload client (plans/convert.md I7), against a fake backend.

Nothing here reaches the network: an autouse fixture makes every
`requests.Session.request` fail the test, and every client gets `FakeSsi`,
an in-memory ``a21.php`` whose answers are modelled on what the I7a
research documents (the real API is unproven until the owner's live test).
"""
import io
import json
import logging
import os
import zipfile
from datetime import datetime

import pytest
import requests

from src.core.models import DiveEvent, GasMixture, SampleChannels, UnifiedDive, UnifiedSample
from src.core.services import ssi
from src.core.services.ssi import (
    API_URL, APP_IDENTITY, APP_USER_AGENT, STATUS_FAILED, STATUS_SENT, STATUS_SKIPPED, HttpResponse,
    SiteIndex, SsiAuthError, SsiClient, SsiCredentials, SsiError, SsiLogbook, SsiRateLimited, SsiResponseError,
    SsiSite, build_payload, computer_reference, describe_tank, find_duplicate, mask_email, parse_logbook,
    parse_site_record, parse_ssi_datetime, preselect_site, scrub, ssi_drops, upload_dives, verify_upload,
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
    """``api.divessi.com/app/a21.php`` in memory, modelled on what the
    research documents (myssi.md, 2026-10-19). Tokens are ``tok-<n>``;
    `expire()` invalidates them all so the next call answers
    ``authenticated: false``.

    ``save_divelog`` behaves like the live server since 2026-10-07: without
    the MySSI app's User-Agent it answers ``{"success": {"ok": "added to
    Log", ..., "odin_user_log_id": <random 9 digits>}}`` and stores nothing
    (counted in ``filtered``); with it, a new dive is stored and the stored
    record echoed back, and a record with ``odin_user_log_deleted`` true
    flags the stored dive deleted and answers ``{"success": {"ok":
    "deleted"}}``. Deleted dives are left out of ``get_divelog`` unless
    ``return_deleted`` is set (then they come back with the flag, the
    other reading of the sources). ``drop_saves`` keeps a saved dive out of
    the logbook (read-back failure); ``mangle`` edits a saved dive before
    it is stored; ``refuse_deletes`` acknowledges a delete and keeps the
    dive."""

    def __init__(self, dives=None, sites=None, email=EMAIL, password=PASSWORD):
        self.logbook = [dict(d) for d in (dives or [])]
        self.sites = list(sites or [])
        self.email, self.password = email, password
        self.calls = []
        self.saved = []
        self.filtered = 0
        self.valid_tokens = set()
        self.logins = 0
        self.next_id = 900
        self.drop_saves = False
        self.mangle = None
        self.return_deleted = False
        self.refuse_deletes = False
        self.fail_next = None        # an HttpResponse to answer the next call with
        self.raise_next = None       # an exception to raise on the next call

    def expire(self):
        self.valid_tokens.clear()

    def whats(self):
        return [c["what"] for c in self.calls]

    def live(self):
        return [d for d in self.logbook if not d.get("odin_user_log_deleted")]

    def __call__(self, method, url, params, data, timeout, headers):
        what = params.get("what")
        self.calls.append({"method": method, "url": url, "what": what, "params": dict(params), "data": data,
                           "headers": dict(headers)})
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
                                   "authenticated_email": self.email, "privacy_required": False})
            return self._json({"authenticated": False, "error_message": "bad login"})
        if params.get("token") not in self.valid_tokens:
            return self._json({"status": 401, "authenticated": False})
        if what == "get_divelog":
            shown = list(self.logbook) if self.return_deleted else self.live()
            return self._json({"authenticated": True, "logbook_details": shown,
                               "logbook_sites": list(self.sites), "logbook_buddies": [],
                               "logbook_stats": {"dives": len(self.live())}})
        if what == "get_divelog_vars":
            return self._json({"logbook_vars": {"watertype": {"4": "fresh", "5": "salt"},
                                                "tanktype": {"19": "steel", "20": "aluminium"}}})
        if what == "save_divelog":
            assert method == "POST" and data is not None and set(data) == {"json_data"}
            dive = json.loads(data["json_data"])
            self.saved.append(dive)
            if headers.get("User-Agent") != APP_USER_AGENT:
                self.filtered += 1       # the 2026-10-07 filter: acknowledged, nothing stored
                return self._json({"success": {"ok": "added to Log", "error": "", "temp_id": "",
                                               "odin_user_log_id": 100000000 + 7 * len(self.saved)}})
            if dive.get("odin_user_log_deleted"):
                for stored in self.logbook:
                    if str(stored.get("odin_user_log_id")) == str(dive.get("odin_user_log_id")) and not self.refuse_deletes:
                        stored["odin_user_log_deleted"] = 1
                return self._json({"success": {"ok": "deleted", "error": ""}})
            stored = dict(dive)
            stored["odin_user_log_id"] = str(self.next_id)
            self.next_id += 1
            if self.mangle:
                self.mangle(stored)
            if not self.drop_saves:
                self.logbook.append(stored)
            return self._json(stored)
        return HttpResponse(404, b"unknown what")

    @staticmethod
    def _json(obj):
        return HttpResponse(200, json.dumps(obj).encode())


# The keys of the MySSI app's dive object, from divebridge's
# tests/data/create_dive_keys.txt (MIT, (c) 2026 Benedikt Reiz; commit
# 69935c0, 2026-10-13; 340 keys), the reference its own payload test pins.
# Kept here, independently of ssi.payload_template, so a typo in the port
# shows up. 12 keys have no prefix; the other 328 are odin_user_log_<name>.
_DIVEBRIDGE_PLAIN_KEYS = """
localBuddyIds localSiteId log_extended_data_cleanup_weight_kg log_extended_data_cleanup_weight_lb
log_linked_brevet_rule_id log_linked_facility_id needsUnverifyUpload needsUpload needsVerificationUpload
reset_profile_divelog_number_with_deletion timestamp uploadError
"""
_DIVEBRIDGE_LOG_KEYS = """
accelerationDataset airtemp_c airtemp_f alarmDataset alarm_deco_stop alarm_deco_violation alarm_fast_ascent
amv_l amv_psi animal_ids apple_watch apple_watch_app_version apple_watch_id apple_watch_log_id
apple_watch_os_version avg_depth_ft avg_depth_m batteryLevelDataset batteryLevelEnd batteryLevelStart
buddy_ids ccr_bailout01 ccr_bailout01_end_bar ccr_bailout01_end_psi ccr_bailout01_he ccr_bailout01_o2
ccr_bailout01_start_bar ccr_bailout01_start_psi ccr_bailout01_tank_vol_cuft ccr_bailout01_tank_vol_l
ccr_bailout01_tanktype_id ccr_bailout02 ccr_bailout02_end_bar ccr_bailout02_end_psi ccr_bailout02_he
ccr_bailout02_o2 ccr_bailout02_start_bar ccr_bailout02_start_psi ccr_bailout02_tank_vol_cuft
ccr_bailout02_tank_vol_l ccr_bailout02_tanktype_id ccr_bailout03 ccr_bailout03_end_bar
ccr_bailout03_end_psi ccr_bailout03_he ccr_bailout03_o2 ccr_bailout03_start_bar ccr_bailout03_start_psi
ccr_bailout03_tank_vol_cuft ccr_bailout03_tank_vol_l ccr_bailout03_tanktype_id ccr_bottom_tank_vol_cuft
ccr_diluent_end_bar ccr_diluent_end_psi ccr_diluent_gas ccr_diluent_he ccr_diluent_o2 ccr_diluent_start_bar
ccr_diluent_start_psi ccr_diluent_tank_vol_cuft ccr_diluent_tank_vol_l ccr_diluent_tanktype_id
ccr_o2_end_bar ccr_o2_end_psi ccr_o2_start_bar ccr_o2_start_psi ccr_o2_tank_vol_cuft ccr_o2_tank_vol_l
ccr_o2_tanktype_id ccr_sac_bailout_l ccr_sac_bailout_psi ccr_sac_deco_l ccr_sac_deco_psi
ccr_total_deco_time ccr_unit_id cns_end cns_start comment confirmed crdate date datetime deco_dive deco_gas
deco_gas_end_bar deco_gas_end_psi deco_gas_o2 deco_gas_start_bar deco_gas_start_psi deco_gas_tank_vol_cuft
deco_gas_tank_vol_l deco_gas_tanktype_id deco_time deepestDecoDataset deleted depthDataset depth_ft depth_m
diveComputer diveComputerData diveSamples dive_on_own_risk dive_on_own_risk_os_app dive_sites_id dive_type
divecenter_confirmed divecenter_confirmed_id divecenter_confirmed_logo divecenter_confirmed_name
divecomputer_ble_id divecomputer_bottomtimer divecomputer_dive_ref divecomputer_firmware divecomputer_id
divecomputer_imported divecomputer_manufacturer divecomputer_max_sensor_depth divecomputer_name
divecomputer_productname divecomputer_raw_data_details divecomputer_raw_data_header divecomputer_ref
divecomputer_serial_nr divetime ean ean_percent entry_time frd_NOTES frd_divetype_id frd_neutral_ft
frd_neutral_m frd_suit frd_weight_kg frd_weight_lb frddisc frddisc_CNF frddisc_CNF_MAX_ft frddisc_CNF_MAX_m
frddisc_CNF_TIME frddisc_CNF_WU frddisc_CWT frddisc_CWT_MAX_ft frddisc_CWT_MAX_m frddisc_CWT_TIME
frddisc_CWT_WU frddisc_DNF frddisc_DNF_MAX_ft frddisc_DNF_MAX_m frddisc_DNF_WU frddisc_DYN frddisc_DYNTT
frddisc_DYNTT_MAX_ft frddisc_DYNTT_MAX_m frddisc_DYNTT_RP frddisc_DYN_MAX_ft frddisc_DYN_MAX_m
frddisc_DYN_WU frddisc_FIM frddisc_FIM_MAX_ft frddisc_FIM_MAX_m frddisc_FIM_TIME frddisc_FIM_WU frddisc_FRC
frddisc_FRC_MAX_ft frddisc_FRC_MAX_m frddisc_FRC_RP frddisc_STA frddisc_STATT frddisc_STATT_MAX
frddisc_STATT_RP frddisc_STA_CT frddisc_STA_MAX frddisc_STA_WU frddisc_VWT frddisc_VWT_MAX_ft
frddisc_VWT_MAX_m frddisc_VWT_TIME frddisc_VWT_WU frddisc_WAPN frddisc_WAPN_MAX frddisc_WAPN_RP
frddisc_WAPN_WU frdwater_body_id freeDiveSessionCharts gear gearconfiguration_id gfSurfDataset gf_end
gf_set gf_set_1 gf_set_2 gfnowDataset gyroDataset heartRateAvg heartRateDataset heartRateMax heartRateMin
housing_local_dive_media id leader_confirmed_id leader_confirmed_name leader_nr locationDataset nr otu_end
otu_start pos_end_latitude pos_end_longitude pos_start_latitude pos_start_longitude pressureDataset
pressure_end_bar pressure_end_psi pressure_start_bar pressure_start_psi rating scr_bottom_end_bar
scr_bottom_end_psi scr_bottom_o2 scr_bottom_setpoint scr_bottom_start_bar scr_bottom_start_psi
scr_bottom_tank_vol_cuft scr_bottom_tank_vol_l scr_bottom_tanktype_id scr_deco scr_deco_end_bar
scr_deco_end_psi scr_deco_o2 scr_deco_setpoint scr_deco_start_bar scr_deco_start_psi scr_deco_tank_vol_cuft
scr_deco_tank_vol_l scr_deco_tanktype_id scr_end_time scr_oc scr_sac_bailout_l scr_sac_bailout_psi
scr_sac_deco_l scr_sac_deco_psi scr_start_time scr_total_deco_time scr_unit_id si_before
tankPressureDataset tank_vol_cuft tank_vol_l tempDataset transferDate user_confirmed_id user_confirmed_name
user_master_id var_current_id var_divetype_id var_entry_id var_specialdive_id var_surface_id
var_tanktype_id var_water_body_id var_watertype_id var_weather_id verified vis_ft vis_m watertemp_c
watertemp_f watertemp_max_c watertemp_max_f weight_kg weight_lb xr_back xr_back_ean xr_back_end_bar
xr_back_end_psi xr_back_he xr_back_o2 xr_back_start_bar xr_back_start_psi xr_back_tanktype_id xr_back_tmx
xr_back_vol_cuft xr_back_vol_l xr_deco1 xr_deco1_ean xr_deco1_end_bar xr_deco1_end_psi xr_deco1_he
xr_deco1_o2 xr_deco1_start_bar xr_deco1_start_psi xr_deco1_tanktype_id xr_deco1_tmx xr_deco1_vol_cuft
xr_deco1_vol_l xr_deco2 xr_deco2_ean xr_deco2_end_bar xr_deco2_end_psi xr_deco2_he xr_deco2_o2
xr_deco2_start_bar xr_deco2_start_psi xr_deco2_tanktype_id xr_deco2_tmx xr_deco2_vol_cuft xr_deco2_vol_l
xr_deco3 xr_deco3_ean_o2 xr_deco3_end_bar xr_deco3_end_psi xr_deco3_he xr_deco3_o2 xr_deco3_start_bar
xr_deco3_start_psi xr_deco3_tanktype_id xr_deco3_vol_cuft xr_deco3_vol_l xr_deco_tanktype_id xr_divetype_id
xr_planned_bottom_time xr_planned_deco_time xr_planned_depth xr_sac_bottom_l xr_sac_bottom_psi
xr_sac_deco_l xr_sac_deco_psi xr_total_deco_time
"""
DIVEBRIDGE_KEYS = frozenset(_DIVEBRIDGE_PLAIN_KEYS.split()) | frozenset(
    "odin_user_log_" + k for k in _DIVEBRIDGE_LOG_KEYS.split())


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
    assert APP_USER_AGENT == "Dart/3.12 (dart:io)"           # the MySSI app's (Flutter/dio); owner's decision 2026-10-19
    assert ssi.DEFAULT_COOLDOWN_S == 2.0                        # MySSI throttles by going silent; 2 s between calls
    assert "unofficial" in ssi.__doc__.lower() and "divebridge" in ssi.__doc__ and "User-Agent" in ssi.__doc__
    assert "can stop working" in ssi.UNOFFICIAL_NOTE


def test_every_request_carries_the_app_identity_user_agent_and_operation():
    fake = FakeSsi()
    client = client_for(fake)
    client.authenticate()
    client.get_divelog()
    client.save_divelog({"odin_user_log_nr": 1})
    for call in fake.calls:
        assert call["url"] == API_URL
        assert all(call["params"][k] == v for k, v in APP_IDENTITY.items())
        assert call["headers"] == {"User-Agent": APP_USER_AGENT}
    assert fake.whats() == ["authenticate", "get_divelog", "save_divelog"]
    assert fake.calls[0]["params"]["l"] == EMAIL and fake.calls[0]["params"]["p"] == PASSWORD
    assert fake.calls[1]["params"]["token"] == "tok-1"


def test_real_transport_sends_the_user_agent_header(monkeypatch):
    """`requests_transport` passes the client's headers to requests (captured
    at the blocked Session.request, so nothing leaves the machine)."""
    seen = {}

    def capture(self, method, url, *args, **kwargs):
        seen.update(method=method, headers=kwargs.get("headers"), params=kwargs.get("params"), data=kwargs.get("data"))
        return type("R", (), {"status_code": 200, "content": b'{"authenticated": false}'})()
    monkeypatch.setattr(requests.sessions.Session, "request", capture)
    with pytest.raises(SsiAuthError):
        SsiClient(EMAIL, PASSWORD, sleep=lambda s: None).authenticate()
    assert seen["headers"] == {"User-Agent": APP_USER_AGENT} and seen["method"] == "GET"
    assert seen["params"]["what"] == "authenticate" and seen["data"] is None


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


def test_silence_is_rate_limiting():
    """MySSI throttles by not answering: a timeout or connection failure
    (every requests error is an OSError) and an empty 200 are
    `SsiRateLimited`; a bug in the transport stays a plain `SsiError`."""
    fake = FakeSsi()
    client = client_for(fake, token="t")
    fake.valid_tokens.add("t")
    for exc in (requests.ReadTimeout("read timed out"), requests.ConnectTimeout("connect"), requests.ConnectionError("reset"),
                TimeoutError("socket"), OSError("network down")):
        fake.raise_next = exc
        with pytest.raises(SsiRateLimited) as e:
            client.get_divelog()
        assert "did not answer 'get_divelog'" in str(e.value) and "silent" in str(e.value) and type(exc).__name__ in str(e.value)
    fake.fail_next = HttpResponse(200, b"  ")
    with pytest.raises(SsiRateLimited) as e:
        client.get_divelog()
    assert "empty answer" in str(e.value)
    fake.raise_next = RuntimeError("bug")
    with pytest.raises(SsiError) as e:
        client.get_divelog()
    assert not isinstance(e.value, SsiRateLimited) and "RuntimeError" in str(e.value)


def test_transport_exception_is_scrubbed(caplog):
    fake = FakeSsi()
    fake.raise_next = requests.ConnectionError(f"Max retries for url: {API_URL}?what=authenticate&l={EMAIL}&p={PASSWORD}")
    with caplog.at_level(logging.DEBUG, logger="dive_sync.ssi"):
        with pytest.raises(SsiError) as e:
            client_for(fake).authenticate()
    assert PASSWORD not in str(e.value) and "what=authenticate" not in str(e.value) and EMAIL not in str(e.value)
    assert "ConnectionError" in str(e.value) and isinstance(e.value, SsiRateLimited)
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
    assert client.get_divelog_vars()["logbook_vars"]["watertype"] == {"4": "fresh", "5": "salt"}
    answer = client.save_divelog({"odin_user_log_nr": 7, "odin_user_log_comment": "å"})
    call = fake.calls[-1]
    assert call["method"] == "POST" and call["what"] == "save_divelog"
    assert json.loads(call["data"]["json_data"]) == {"odin_user_log_nr": 7, "odin_user_log_comment": "å"}
    assert answer["odin_user_log_id"] == "900"          # the real server echoes the stored record


def test_download_sites_writes_the_zip(tmp_path):
    def transport(method, url, params, data, timeout, headers):
        assert url == ssi.SITES_URL and params == {} and method == "GET"
        assert headers == {"User-Agent": APP_USER_AGENT}
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


def test_parse_logbook_sets_deleted_dives_apart():
    """Whether MySSI returns deleted dives is unsettled; if it does, they
    are neither numbered nor duplicates nor read as dives."""
    dives = [logbook_dive(11, "2026-06-20", "09:30", ref="garmin:1"),
             logbook_dive(12, "2026-06-21", "14:05", ref="garmin:2", odin_user_log_deleted=1),
             logbook_dive(13, "2026-06-22", "14:05", odin_user_log_deleted="true"),
             logbook_dive(14, "2026-06-23", "14:05", odin_user_log_deleted="0"),
             logbook_dive(15, "2026-06-24", "14:05", odin_user_log_deleted=False)]
    book = parse_logbook({"logbook_details": dives})
    assert [d.number for d in book.dives] == [11, 14, 15] and book.highest_number == 15
    assert [d.number for d in book.deleted] == [12, 13] and all(d.deleted for d in book.deleted)
    assert ssi.is_deleted({"odin_user_log_deleted": None}) is False and ssi.is_deleted({}) is False
    # not a duplicate, not counted in numbering
    assert find_duplicate(book, make_dive(start="2026-06-25 10:00", activity="2")) is None
    assert find_duplicate(book, make_dive(start="2026-06-21 14:05", activity=None)) is None
    assert book.highest_number + 1 == 16                                    # the next upload's number
    fake = FakeSsi(dives=dives)
    fake.return_deleted = True
    assert [d.dive_number for d in adapter_for(fake).fetch_dives()] == [11, 14, 15]


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


def test_site_index_lookup():
    """By id, and iteration; the text search the Dive edit site picker used
    went with plans/myssi-sync.md K4."""
    index = SiteIndex.from_records(SITES)
    assert not hasattr(index, "search")
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
    assert payload["odin_user_log_ean"] == 1 and payload["odin_user_log_ean_percent"] == 32     # whole number, as the app
    assert payload["odin_user_log_var_tanktype_id"] is None                                    # steel/aluminium: not known
    # deco settings and the computer (the app's _divecomputer_* keys), with the imported flag off
    assert payload["odin_user_log_gf_set_1"] == 40 and payload["odin_user_log_gf_set_2"] == 85
    assert payload["odin_user_log_gf_set"] == "40 / 85"
    assert payload["odin_user_log_divecomputer_imported"] is False
    assert payload["odin_user_log_divecomputer_name"] == "Garmin Descent Mk3i"
    assert payload["odin_user_log_divecomputer_manufacturer"] == "Garmin"
    assert payload["odin_user_log_divecomputer_serial_nr"] == "1000000001"
    assert payload["odin_user_log_divecomputer_firmware"] == "19.10"
    assert payload["odin_user_log_divecomputer_ref"] == "Garmin Descent Mk3i_1000000001"
    assert payload["odin_user_log_divecomputer_dive_ref"] == "garmin:24449823352"
    assert not any(k in payload for k in ("odin_user_log_manufacturer", "odin_user_log_serial_nr", "odin_user_log_firmware"))
    # the profile: 5 s grid from 0 to 60 s, 13 points, interpolated between the 3 s records
    samples = json.loads(payload["odin_user_log_diveSamples"])
    assert [p["t"] for p in samples] == [t * 1000 for t in range(0, 61, 5)]
    assert samples[0] == {"a": 0, "d": 0.0, "dr": False, "gs": 0, "mf": ssi.FLAG_DIVE | ssi.FLAG_SURFACED, "n": 1, "ndl": 99,
                          "o": False, "s": 0.0, "t": 0, "te": 28.0, "pressure": 200.0}
    assert samples[1]["d"] == 3.0 and samples[1]["pressure"] == 190.0    # t=5 s between the 3 s and 6 s records
    assert samples[1]["s"] == -36.0 and samples[1]["n"] == 2              # 3 m in 5 s, descending = negative
    assert samples[-1]["t"] == 60000 and samples[-1]["pressure"] == 80.0 and samples[-1]["n"] == 13
    assert json.loads(payload["odin_user_log_depthDataset"]) == [p["d"] for p in samples]
    assert json.loads(payload["odin_user_log_tempDataset"])[0] == 28.0
    assert json.loads(payload["odin_user_log_tankPressureDataset"]) == json.loads(payload["odin_user_log_pressureDataset"])
    assert json.loads(payload["odin_user_log_gfSurfDataset"]) == [0] * 13
    # the body is JSON-serialisable as the client sends it
    json.dumps(payload)


def test_build_payload_is_the_apps_whole_object():
    """Every key the MySSI app sends (divebridge's reference list), nothing
    else; the bookkeeping as the app sets it on a new dive; a value we do
    not have at the app's empty default - nothing guessed."""
    payload = build_payload(make_dive(), 3)
    assert set(payload) == DIVEBRIDGE_KEYS, (sorted(DIVEBRIDGE_KEYS - set(payload)), sorted(set(payload) - DIVEBRIDGE_KEYS))
    assert len(payload) == 340
    assert payload["odin_user_log_id"] is None and payload["needsUpload"] is True and payload["odin_user_log_deleted"] is False
    assert payload["odin_user_log_dive_type"] == 0 and payload["odin_user_log_var_divetype_id"] == 24
    assert payload["odin_user_log_frd_divetype_id"] == 50 and payload["odin_user_log_apple_watch"] == 0
    assert payload["localBuddyIds"] == [] and payload["odin_user_log_buddy_ids"] == []
    assert payload["odin_user_log_animal_ids"] == [] and payload["odin_user_log_gear"] == []
    assert payload["odin_user_log_diveComputer"] == "" and payload["odin_user_log_dive_on_own_risk"] == 0
    # not known from a UnifiedDive: left at the app's empty default
    for key in ("odin_user_log_rating", "odin_user_log_airtemp_c", "odin_user_log_var_weather_id", "odin_user_log_var_entry_id",
                "odin_user_log_var_watertype_id", "odin_user_log_deco_dive", "odin_user_log_cns_start", "odin_user_log_xr_back_he",
                "odin_user_log_scr_unit_id", "odin_user_log_ccr_bailout01_o2", "odin_user_log_frddisc_CWT", "odin_user_log_tank_vol_cuft"):
        assert payload[key] is None, key
    assert all(payload[k] is None for k in ssi.NULL_KEY_GROUPS)
    # the template is a fresh object every time (the lists are mutable)
    a, b = ssi.payload_template(), ssi.payload_template()
    a["localBuddyIds"].append(1)
    assert b["localBuddyIds"] == [] and set(a) == DIVEBRIDGE_KEYS
    # the water type from the computer's setting
    assert build_payload(make_dive(water_type="salt"), 1)["odin_user_log_var_watertype_id"] == 5
    assert build_payload(make_dive(water_type="fresh"), 1)["odin_user_log_var_watertype_id"] == 4
    assert build_payload(make_dive(water_type="en13319"), 1)["odin_user_log_var_watertype_id"] is None


def test_build_payload_air_no_site_no_profile_imperial_units():
    dive = make_dive(samples=False, tanks=[GasMixture(oxygen=21.0)], weight=12.0, weight_unit="pound",
                     visibility=60.0, visibility_unit="foot", avg_depth=None, temp_min=None, temp_max=None, notes=None,
                     computer_vendor=None, computer_model=None, computer_serial=None, computer_firmware=None)
    payload = build_payload(dive, 1)
    assert set(payload) == DIVEBRIDGE_KEYS
    assert payload["odin_user_log_ean"] is None and payload["odin_user_log_ean_percent"] is None
    assert payload["odin_user_log_dive_sites_id"] is None
    # no profile: the datasets are empty JSON lists as the app sends them, the pressure ones null
    assert payload["odin_user_log_diveSamples"] == "[]" and payload["odin_user_log_depthDataset"] == "[]"
    assert payload["odin_user_log_tempDataset"] == "[]" and payload["odin_user_log_gfSurfDataset"] == "[]"
    assert payload["odin_user_log_tankPressureDataset"] is None and payload["odin_user_log_pressureDataset"] is None
    assert payload["odin_user_log_pressure_start_bar"] is None and payload["odin_user_log_pressure_start_psi"] is None
    assert payload["odin_user_log_weight_kg"] == 5.4 and payload["odin_user_log_weight_lb"] == 11.9
    assert payload["odin_user_log_vis_m"] == 18.3 and payload["odin_user_log_vis_ft"] == 60.0
    assert payload["odin_user_log_avg_depth_ft"] is None and payload["odin_user_log_watertemp_f"] is None
    assert payload["odin_user_log_comment"] is None and payload["odin_user_log_gf_set"] == "40 / 85"
    # no computer: name, maker, serial and ref null, firmware "" (the app's empty value)
    assert payload["odin_user_log_divecomputer_name"] is None and payload["odin_user_log_divecomputer_manufacturer"] is None
    assert payload["odin_user_log_divecomputer_serial_nr"] is None and payload["odin_user_log_divecomputer_ref"] is None
    assert payload["odin_user_log_divecomputer_firmware"] == ""
    no_tank = build_payload(make_dive(tanks=[], samples=False, gf_low=None), 2)
    assert no_tank["odin_user_log_tank_vol_l"] is None and no_tank["odin_user_log_ean"] is None and no_tank["odin_user_log_gf_set"] is None
    assert ssi.computer_device_ref(make_dive(computer_serial=None)) == "Garmin Descent Mk3i_unknown"
    assert ssi.computer_device_ref(make_dive(computer_vendor=None, computer_model=None)) == "_1000000001"


def test_build_payload_pressure_from_channels_of_tank_0():
    samples = [UnifiedSample(depth=0.0, time=0, channels=SampleChannels(pressures={0: 180.0, 1: 150.0})),
               UnifiedSample(depth=10.0, time=10, channels=SampleChannels(pressures={1: 140.0}), pressure=170.0)]
    payload = build_payload(make_dive(samples=False).model_copy(update={"samples": samples}), 1)
    grid = json.loads(payload["odin_user_log_diveSamples"])
    assert [p.get("pressure") for p in grid] == [180.0, 175.0, 170.0]      # tank 0 first, the plain pressure as fallback
    assert [p["te"] for p in grid] == [0, 0, 0]                            # no temperature: the format's 0
    assert json.loads(payload["odin_user_log_tempDataset"]) == [0, 0, 0]


def test_grid_samples_phase_bits_ndl_speed_and_no_pressure():
    """`mf` from the depth (DIVE always, AT_DEPTH from 8.5 m until back
    above 6 m, SURFACED at 1 m or less), `ndl` from the channel in whole
    minutes capped at 99, `s` from the grid's depth change, no `pressure`
    key without a reading."""
    def s(t, d, ndl=None):
        return UnifiedSample(depth=d, time=t, channels=SampleChannels(ndl=ndl) if ndl is not None else None)
    grid = ssi._grid_samples([s(0, 0.0, 6000), s(5, 5.0), s(10, 9.0, 1800), s(15, 7.0), s(20, 5.0), s(25, 0.5, 0)])
    assert [p["d"] for p in grid] == [0.0, 5.0, 9.0, 7.0, 5.0, 0.5]
    assert [p["mf"] for p in grid] == [ssi.FLAG_DIVE | ssi.FLAG_SURFACED, ssi.FLAG_DIVE, ssi.FLAG_DIVE | ssi.FLAG_AT_DEPTH,
                                       ssi.FLAG_DIVE | ssi.FLAG_AT_DEPTH, ssi.FLAG_DIVE, ssi.FLAG_DIVE | ssi.FLAG_SURFACED]
    assert [p["ndl"] for p in grid] == [99, 99, 30, 99, 99, 0]           # 100 min capped at 99; no reading = 99
    between = ssi._grid_samples([s(0, 0.0, 6000), s(10, 2.0, 1200)])
    assert [p["ndl"] for p in between] == [99, 60, 20]                   # interpolated between two readings
    assert [p["s"] for p in grid] == [0.0, -60.0, -48.0, 24.0, 24.0, 54.0]
    assert all("pressure" not in p for p in grid) and all(set(p) == set(ssi.SAMPLE_KEYS) for p in grid)
    assert [p["n"] for p in grid] == [1, 2, 3, 4, 5, 6]
    assert ssi._grid_samples([s(0, 0.0)]) == [] and ssi._grid_samples([]) == []


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


def test_upload_result_summaries_and_no_plan_step():
    """`UploadResult.summary` per status; the planned state and
    `plan_upload` (the Dive edit page's Send to SSI preview) went with
    plans/myssi-sync.md K4 - the duplicate reasons they showed are
    `upload_dives`' own (test_upload_dives_full_flow_numbers_skips_and_verifies)."""
    from src.core.services.ssi import UploadResult
    assert not hasattr(ssi, "plan_upload") and not hasattr(ssi, "STATUS_PLANNED")
    dive = make_dive(start="2026-06-22 10:00", activity="2")
    assert UploadResult(dive, STATUS_SENT, number=43).summary == "2026-06-22 10:00: sent as dive 43"
    assert UploadResult(dive, STATUS_SKIPPED, "already in MySSI as dive 42 (starts within 2 minutes)").summary == \
        "2026-06-22 10:00: skipped, already in MySSI as dive 42 (starts within 2 minutes)"
    assert UploadResult(dive, STATUS_FAILED, "sent, but not found in the logbook after sending").summary == \
        "2026-06-22 10:00: failed, sent, but not found in the logbook after sending"


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

    def transport(method, url, params, data, timeout, headers):
        if params.get("what") == "save_divelog" and len(fake.saved) == 1:
            fake.calls.append({"what": "save_divelog", "method": method, "url": url, "params": params, "data": data})
            return HttpResponse(429, b"")
        return original(method, url, params, data, timeout, headers)

    client._transport = transport
    results = upload_dives(client, dives)
    assert [r.status for r in results] == [STATUS_SENT, STATUS_FAILED, STATUS_FAILED]
    assert "rate limit" in results[1].reason and results[2].reason.startswith("not attempted: MySSI rate limit")
    assert [c["what"] for c in fake.calls] == ["get_divelog", "save_divelog", "get_divelog", "save_divelog"]


def test_upload_dives_timeout_stops_the_batch():
    """MySSI rate-limits by going silent: a timeout on the second save stops
    the batch like a 429, the rest is not attempted."""
    fake = FakeSsi()
    client = client_for(fake)
    dives = [make_dive(start=f"2026-06-2{i} 10:00", activity=str(i)) for i in range(1, 4)]
    original = fake.__call__

    def transport(method, url, params, data, timeout, headers):
        if params.get("what") == "save_divelog" and len(fake.saved) == 1:
            raise requests.ReadTimeout("HTTPSConnectionPool: Read timed out. (read timeout=30)")
        return original(method, url, params, data, timeout, headers)

    client._transport = transport
    results = upload_dives(client, dives)
    assert [r.status for r in results] == [STATUS_SENT, STATUS_FAILED, STATUS_FAILED]
    assert results[1].reason.startswith("MySSI did not answer 'save_divelog' (ReadTimeout") and "silent" in results[1].reason
    assert results[2].reason.startswith("not attempted: MySSI did not answer")
    assert fake.whats() == ["authenticate", "get_divelog", "save_divelog", "get_divelog"] and len(fake.saved) == 1


def test_upload_without_the_app_user_agent_is_dropped_by_ssi():
    """The 2026-10-07 filter in the fake: a save without the app's
    User-Agent is answered "added to Log" with a random id and nothing is
    stored; the read-back catches it, so nothing is reported as sent."""
    fake = FakeSsi()
    client = client_for(fake)
    original = fake.__call__

    def other_client(method, url, params, data, timeout, headers):
        return original(method, url, params, data, timeout, {"User-Agent": "python-requests/2.32"})

    client._transport = other_client
    results = upload_dives(client, [make_dive(start="2026-06-22 10:00", activity="2")])
    assert results[0].status == STATUS_FAILED and results[0].reason == "sent, but not found in the logbook after sending"
    assert fake.filtered == 1 and fake.logbook == [] and len(fake.saved) == 1
    client._transport = fake                               # with the header the same dive is stored
    results = upload_dives(client, [make_dive(start="2026-06-22 10:00", activity="2")])
    assert results[0].status == STATUS_SENT and fake.filtered == 1 and len(fake.logbook) == 1


def test_delete_dive_round_trip_gone_or_flagged():
    """`SsiClient.delete_dive`: the stored record sent back with the id,
    `odin_user_log_deleted` true and `needsUpload` true (divebridge, live
    2026-10-08), then read back - gone, or returned with the flag."""
    fake = FakeSsi(dives=[logbook_dive(11, "2026-06-20", "09:30", ref="garmin:1", needsUpload=False, localSiteId=None,
                                       odin_user_log_deleted=0, odin_user_log_comment="x")])
    client = client_for(fake)
    entry = client.get_divelog().dives[0]
    assert client.delete_dive(entry) is True
    sent = fake.saved[-1]
    assert sent["odin_user_log_id"] == "111" and sent["odin_user_log_deleted"] is True and sent["needsUpload"] is True
    assert sent["odin_user_log_nr"] == 11 and sent["odin_user_log_comment"] == "x" and "localSiteId" not in sent
    assert fake.whats() == ["authenticate", "get_divelog", "save_divelog", "get_divelog"]
    assert client.get_divelog().dives == [] and fake.logbook[0]["odin_user_log_deleted"] == 1
    assert ssi.delete_payload(entry)["odin_user_log_deleted"] is True
    # the other reading of the sources: the deleted dive comes back flagged
    flagged = FakeSsi(dives=[logbook_dive(5, "2026-06-20", "09:30")])
    flagged.return_deleted = True
    client = client_for(flagged)
    assert client.delete_dive(client.get_divelog().dives[0]) is True
    book = client.get_divelog()
    assert book.dives == [] and [d.id for d in book.deleted] == ["105"] and book.highest_number == 0
    # acknowledged but still there: False; no id: refused without a request
    stubborn = FakeSsi(dives=[logbook_dive(6, "2026-06-20", "09:30")])
    stubborn.refuse_deletes = True
    client = client_for(stubborn)
    assert client.delete_dive(client.get_divelog().dives[0]) is False
    with pytest.raises(SsiError):
        client.delete_dive(ssi.SsiLogbookDive(id=None, number=1, start=None, computer_ref=None))
    assert stubborn.whats() == ["authenticate", "get_divelog", "save_divelog", "get_divelog"]


def test_delete_dive_answer_not_trusted(caplog):
    """A delete acknowledged by a server that stored nothing (the filter) is
    reported by the read-back, not by the answer."""
    fake = FakeSsi(dives=[logbook_dive(11, "2026-06-20", "09:30")])
    client = client_for(fake)
    entry = client.get_divelog().dives[0]
    original = fake.__call__
    client._transport = lambda m, u, p, d, t, h: original(m, u, p, d, t, {"User-Agent": "curl"})
    with caplog.at_level(logging.WARNING, logger="dive_sync.ssi"):
        assert client.delete_dive(entry) is False
    assert "not confirmed by the answer" in caplog.text and "still in the logbook" in caplog.text
    assert fake.filtered == 1 and not fake.logbook[0].get("odin_user_log_deleted")


def test_upload_dives_other_errors_continue_and_auth_errors_stop():
    fake = FakeSsi()
    client = client_for(fake)
    dives = [make_dive(start=f"2026-06-2{i} 10:00", activity=str(i)) for i in range(1, 4)]
    client.ensure_token()
    original = fake.__call__

    def flaky(method, url, params, data, timeout, headers):
        if params.get("what") == "save_divelog" and not fake.saved:
            fake.saved.append({})       # consume the first attempt with a server error
            return HttpResponse(503, b"")
        return original(method, url, params, data, timeout, headers)

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


def test_adapter_is_an_add_only_base_dive_adapter():
    """plans/myssi-sync.md K1: new dives yes, updates not until K7, mirror
    deletes never; the catalogue writes what a new dive is sent with, and
    keeps the dive number (MySSI numbers after its highest) and the site
    (an id from SSI's database) read-only."""
    from src.core.adapter import BaseDiveAdapter
    adapter = adapter_for(FakeSsi())
    assert isinstance(adapter, BaseDiveAdapter)
    assert ssi.SsiAdapter.service_id == "ssi" and ssi.SsiAdapter.display_name == "MySSI"
    assert ssi.SsiAdapter.accepts_new_dives is True and ssi.SsiAdapter.stores_external_ids is False
    assert ssi.SsiAdapter.accepts_updates is False and ssi.SsiAdapter.accepts_mirror_deletes is False
    assert BaseDiveAdapter.accepts_updates is True and BaseDiveAdapter.accepts_mirror_deletes is True
    named = [GasMixture(oxygen=32.0, start_pressure=200.0, end_pressure=90.0, tank_volume=11.1, tank_name="Tank 1")]
    assert ssi.SsiAdapter.as_stored("tanks", named)[0].tank_name is None and BaseDiveAdapter.as_stored("tanks", named) is named
    catalog = ssi.SsiAdapter.field_catalog()          # callable without a login
    assert catalog and all(spec.key.startswith("ssi.") for spec in catalog)
    assert {spec.unified for spec in catalog if not spec.writable} == {"dive_number", "location"}
    assert {spec.unified for spec in catalog if spec.writable} == {
        "date_time", "duration", "max_depth", "avg_depth", "temp_min", "temp_max", "notes", "weight", "visibility",
        "gps", "tanks", "samples"}
    assert "not registered" not in ssi.__doc__ and "SsiAdapter" in ssi.__doc__


def test_adapter_is_registered_as_a_sync_service(tmp_path):
    """The inverse of the Track I guard: ``ssi[:account]`` is a service spec
    (plans/myssi-sync.md K1), built from the ``ssi`` accounts of
    credentials.json the way Divelogs is, and never from the network."""
    from src.core import pairs
    from src.core.config import ConfigManager, CredentialsModel, SettingsModel, SsiCredentials
    assert "ssi" in pairs.KNOWN_SERVICES and pairs.display_name_of("ssi") == "MySSI"
    assert pairs.parse_service_spec("ssi") == ("ssi", None)
    assert pairs.parse_service_spec(" SSI:diver@example.com ") == ("ssi", "diver@example.com")
    assert pairs.service_id_of("ssi:diver@example.com") == "ssi"
    assert pairs.field_catalog_of("ssi") == ssi.SsiAdapter.field_catalog()
    # a Dives page and "Download dives" since K3 (one get_divelog call reads the whole logbook)
    assert pairs.CONFIGURED_SPECS["ssi"] == "ssi" and pairs.CACHE_SERVICE_ORDER[-1] == "ssi"
    assert pairs.cache_services(["ssi", "garmin"]) == [{"id": "garmin", "label": "Garmin Connect", "spec": "garmin"},
                                                        {"id": "ssi", "label": "MySSI", "spec": "ssi"}]
    assert pairs.adapter_class_of("ssi") is ssi.SsiAdapter
    assert pairs.configured_specs(["garmin", "ssi"]) == ["garmin", "ssi"]
    endpoints = pairs.sync_endpoints(SettingsModel(), ["garmin", "ssi"])
    assert {"spec": "ssi", "id": "ssi", "label": "MySSI"} in endpoints
    assert [b["id"] for b in pairs.board_pairs(SettingsModel(), ["garmin", "ssi"])] == ["garmin_divelogs", "garmin_ssi"]
    assert ssi.SsiCredentials is SsiCredentials                     # one model, re-exported by the service module

    # the credentials model: single form and list, both shapes
    one = CredentialsModel(ssi={"email": EMAIL, "password": PASSWORD, "token": "tok-9"})
    assert [a.email for a in one.get_ssi_accounts()] == [EMAIL] and one.configured_services() == ["ssi"]
    assert CredentialsModel().get_ssi_accounts() == [] and "ssi" not in CredentialsModel().configured_services()
    unfinished = CredentialsModel(ssi=[{"email": EMAIL}])
    assert len(unfinished.get_ssi_accounts()) == 1 and "ssi" not in unfinished.configured_services()

    creds = tmp_path / "credentials.json"
    settings = SettingsModel()
    with pytest.raises(ValueError, match="MySSI is not configured"):
        pairs.build_adapter("ssi", settings, credentials_path=str(creds))
    ConfigManager.save_credentials(one, str(creds))
    adapter = pairs.build_adapter("ssi", settings, credentials_path=str(creds))
    assert isinstance(adapter, ssi.SsiAdapter) and adapter.email == EMAIL and adapter.client.token == "tok-9"
    assert adapter.client.cooldown_seconds == ssi.DEFAULT_COOLDOWN_S         # MySSI's own spacing, not Garmin's
    assert pairs.adapter_account(adapter) == EMAIL
    # a token MySSI hands out during the run goes back into the credentials
    # file for that account (K2), so the next build - and the desktop
    # keychain, which reads the file back after the operation - reuses it
    adapter.client._on_token("tok-10")
    assert ConfigManager.load_credentials(str(creds)).get_ssi_accounts()[0].token == "tok-10"
    assert pairs.build_adapter("ssi", settings, credentials_path=str(creds)).client.token == "tok-10"
    assert ConfigManager.store_ssi_token(EMAIL, "tok-10", str(creds)) is False            # unchanged: not rewritten
    assert ConfigManager.store_ssi_token("nobody@example.com", "x", str(creds)) is False  # not an account
    assert ConfigManager.store_ssi_token(EMAIL, "x", str(tmp_path / "missing.json")) is False
    assert not (tmp_path / "missing.json").exists()
    # several accounts: the spec's argument or --ssi-account picks, else it is an error
    two = CredentialsModel(ssi=[SsiCredentials(email=EMAIL, password=PASSWORD),
                                SsiCredentials(email="other@example.com", password="pw2"),
                                SsiCredentials(email="nopw@example.com")])
    ConfigManager.save_credentials(two, str(creds))
    with pytest.raises(ValueError, match="Multiple MySSI"):
        pairs.build_adapter("ssi", settings, credentials_path=str(creds))
    assert pairs.build_adapter("ssi:other@example.com", settings, credentials_path=str(creds)).email == "other@example.com"
    assert pairs.build_adapter("ssi", settings, credentials_path=str(creds), ssi_account=EMAIL).email == EMAIL
    with pytest.raises(ValueError, match="No MySSI password stored"):
        pairs.build_adapter("ssi:nopw@example.com", settings, credentials_path=str(creds))
    with pytest.raises(ValueError, match="matching username"):
        pairs.build_adapter("ssi:stranger@example.com", settings, credentials_path=str(creds))
    # the engine for a pair, per-account state when asked (no login happens here)
    engine = pairs.engine_for("garmin", "ssi:other@example.com", settings_path=str(tmp_path / "s.json"),
                              credentials_path=str(creds), mock_data_dir=str(tmp_path), account_scoped_state=True)
    assert engine.source_id == "garmin" and engine.target_id == "ssi" and engine.target.email == "other@example.com"
    assert engine.state_file.endswith("sync_state_garmin_ssi-other@example.com.json")


def test_default_board_for_a_myssi_pair():
    """Garmin -> MySSI ships the common links MySSI can take: notes, weight,
    visibility and position both ways (manual on a disagreement), the
    profile and the tanks towards MySSI; no buddy (MySSI keeps ids only),
    no dive number (MySSI numbers its own dives, so it is no match key
    either - matching is the sync state, the computer reference and the
    start time). The board is valid and saveable either way round."""
    from src.core.fields import build_catalog, links_to_rules, rules_to_links
    from src.core.pairs import default_links_for, field_catalog_of
    from src.core.templates import validate_links
    for a, b in (("garmin", "ssi"), ("ssi", "garmin"), ("divelogs", "ssi"), ("subsurface", "ssi")):
        links = default_links_for(a, b)
        ids = {l.id: l for l in links}
        assert "dive_number" not in ids and "buddy" not in ids, (a, b)
        assert not any(l.match_order for l in links)
        catalog = build_catalog(field_catalog_of(a), field_catalog_of(b))
        assert validate_links(links, catalog) == [], (a, b)
        rules, keys = links_to_rules(links, a, b)
        assert keys == [] and validate_links(rules_to_links(rules, keys, a, b), catalog) == []
    ids = {l.id: l for l in default_links_for("garmin", "ssi")}
    assert set(ids) == {"notes", "weight", "visibility", "gps", "samples", "tanks"}
    assert ids["notes"].direction == "bidirectional" and ids["notes"].conflict == "manual"
    assert ids["tanks"].direction == "to_target" and ids["tanks"].target == "ssi.tanks"
    assert ids["samples"].direction == "to_target" and ids["samples"].target == "ssi.samples"
    ids = {l.id: l for l in default_links_for("ssi", "garmin")}          # Garmin takes no tanks or profile
    assert set(ids) == {"notes", "weight", "visibility", "gps"} and ids["gps"].source == ["ssi.gps"]


def test_reference_ids_and_a_read_back_dive_carries_the_other_services_id():
    """The ``_divecomputer_dive_ref`` a dive was sent with comes back as the
    other service's id in ``external_ids`` (a tier-1 match for the engine);
    a serial reference or an unknown prefix gives nothing, and MySSI's own
    id is never written as a reference."""
    assert ssi.reference_ids("garmin:24449823352") == {"garmin": "24449823352"}
    assert ssi.reference_ids(" Divelogs:77 ") == {"divelogs": "77"}
    assert ssi.reference_ids("1000000001:20260627T100000") == {}
    assert ssi.reference_ids("garmin:") == {} and ssi.reference_ids(None) == {} and ssi.reference_ids("") == {}
    assert ssi.reference_ids("ssi:5") == {}
    assert ssi.computer_reference(make_dive(activity=None, external_ids={"ssi": "5", "divelogs": "9"})) == "divelogs:9"
    assert ssi.computer_reference(make_dive(activity=None, external_ids={"ssi": "5"}, computer_serial="S1")).startswith("S1:")
    fake = FakeSsi(dives=[logbook_dive(11, "2026-06-20", "09:30", ref="garmin:1"),
                          logbook_dive(12, "2026-06-21", "09:30", ref="S1:20260621T093000"),
                          logbook_dive(13, "2026-06-22", "09:30")])
    dives = adapter_for(fake).fetch_dives()
    assert [d.external_ids for d in dives] == [{"garmin": "1", "ssi": "111"}, {"ssi": "112"}, {"ssi": "113"}]


def test_add_dive_skips_a_dive_with_two_or_more_tanks(caplog):
    """Owner 2026-10-19: a two-tank dive is not sent (nothing reaches MySSI)
    and the log says why, until I7b maps the other tanks; the engine asks
    `cannot_add_reason` first so the sync summary counts it as skipped."""
    fake = FakeSsi()
    adapter = adapter_for(fake)
    two = [GasMixture(oxygen=32.0, start_pressure=200.0, end_pressure=70.0, tank_volume=12.0),
           GasMixture(oxygen=50.0, start_pressure=200.0, end_pressure=80.0, tank_volume=7.0)]
    dive = make_dive(start="2026-06-30 09:00", activity="3", tanks=two)
    reason = adapter.cannot_add_reason(dive)
    assert reason == "it has 2 tanks; MySSI dives with more than one tank are not supported yet (I7b)"
    assert adapter.cannot_add_reason(make_dive(activity="4")) is None
    assert adapter.cannot_add_reason(make_dive(activity="5", tanks=[])) is None
    with caplog.at_level(logging.WARNING, logger="dive_sync.ssi"):
        assert adapter.add_dive(dive) is None
    assert "MySSI: dive 2026-06-30 09:00 skipped: it has 2 tanks; MySSI dives with more than one tank are not supported yet (I7b)" in caplog.text
    assert fake.calls == [] and fake.saved == []                       # nothing sent, not even a logbook read
    assert len(adapter.cannot_add_reason(make_dive(activity="6", tanks=two + two[:1])).split("3 tanks")) == 2


def test_sync_garmin_to_myssi_through_the_fake(tmp_path, caplog):
    """A Garmin -> MySSI run end to end against `FakeSsi` (plans/myssi-sync.md
    K1): the single-tank dive MySSI lacks is sent and linked, the two-tank
    one is skipped (counted, not failed), the dive MySSI already has (sent
    by an earlier run, found by its computer reference without any sync
    state) is matched - its changed comment is not written and queues no
    conflict - and a second run sends nothing again. A mirror run deletes
    nothing on MySSI; it only says what it would have deleted."""
    from src.core.config import ConfigManager, SettingsModel, SyncFilters, SyncPairModel
    from src.core.pairs import default_links_for
    from src.core.sync_engine import SyncEngine
    from tests.test_link_engine import FakeGarmin
    pair = SyncPairModel(id="garmin_ssi", source="garmin", target="ssi", directionality="to_ssi",
                         field_links=default_links_for("garmin", "ssi"))
    settings_path = str(tmp_path / "settings.json")
    ConfigManager.save_settings(SettingsModel(api_cooldown_seconds=0.0, sync_filters=SyncFilters(only_new=False),
                                              sync_pairs=[pair]), settings_path)
    two = [GasMixture(oxygen=32.0, start_pressure=200.0, end_pressure=70.0, tank_volume=12.0),
           GasMixture(oxygen=50.0, start_pressure=200.0, end_pressure=80.0, tank_volume=7.0)]
    garmin = [make_dive(start="2026-06-20 09:30", activity="1", notes="changed on Garmin"),     # MySSI has it (ref garmin:1)
              make_dive(start="2026-06-25 10:00", activity="2", weight=5.0, weight_unit="kilogram"),
              make_dive(start="2026-06-30 09:00", activity="3", tanks=two)]
    fake = FakeSsi(dives=[logbook_dive(11, "2026-06-20", "09:30", ref="garmin:1", **{"odin_user_log_comment": "as logged"}),
                          logbook_dive(12, "2026-01-05", "08:00")],                            # only on MySSI
                   sites=SITES)

    def engine():
        return SyncEngine(settings_path=settings_path, credentials_path=str(tmp_path / "c.json"),
                          source_adapter=FakeGarmin(garmin), target_adapter=adapter_for(fake))

    with caplog.at_level(logging.INFO):
        res = engine().run_sync(dry_run=False)
    assert res["matched_count"] == 1 and [e["new_id"] for e in res["uploaded_to_ssi"]] == ["900"]
    assert res["updated_on_ssi"] == [] and res["updated_on_garmin"] == [] and res["conflicts"] == []
    assert [(s["reason"], s["time"][:10]) for s in res["skipped"]] == [
        ("receiver_cannot_add_dive", "2026-06-30"), ("receiver_takes_no_updates", "2026-06-20")]
    assert res["skipped"][0]["detail"].startswith("it has 2 tanks")
    assert "MySSI: dive 2026-06-30 09:00 skipped: it has 2 tanks" in caplog.text
    assert "MySSI: changes to dive 11 at 2026-06-20 09:30:00 not sent (updating a MySSI dive is not supported yet)" in caplog.text
    assert "conflict left for manual resolution" in caplog.text           # the rule ran and said so ...
    assert not os.path.exists(engine().conflicts_file)                    # ... but nothing was queued
    assert len(fake.saved) == 1 and fake.saved[0]["odin_user_log_weight_kg"] == 5.0
    stored = {d["odin_user_log_id"]: d for d in fake.live()}
    assert stored["111"]["odin_user_log_comment"] == "as logged"           # the matched dive untouched
    assert "ERROR" not in caplog.text
    links = json.loads((tmp_path / "sync" / "sync_state_garmin_ssi.json").read_text())["links"]
    assert links == {"1": "111", "2": "900"}

    # second run: the sent dive is a known pair, the two-tank one is skipped again, nothing new is sent
    caplog.clear()
    res = engine().run_sync(dry_run=False)
    assert res["matched_count"] == 2 and res["uploaded_to_ssi"] == [] and len(fake.saved) == 1
    # the sent dive comes back equal (MySSI keeps no tank name, and a name it cannot hold is no change),
    # so only the dive with the changed comment says "not sent"
    assert [s["reason"] for s in res["skipped"]] == ["receiver_cannot_add_dive", "receiver_takes_no_updates"]
    assert res["conflicts"] == [] and res["updated_on_ssi"] == []

    # mirror: the MySSI-only dive would go on any other service; here it is logged and kept
    caplog.clear()
    with caplog.at_level(logging.INFO):
        res = engine().run_sync(dry_run=False, mirror_override=True)
    assert res["deleted_on_ssi"] == [] and len(fake.live()) == 3
    assert [s for s in res["skipped"] if s["reason"] == "mirror_never_deletes_here"] == [
        {"reason": "mirror_never_deletes_here", "id": "112", "time": "2026-01-05 08:00:00"}]
    assert "Mirror: MySSI dive at 2026-01-05 08:00:00 (ID 112) has no match on the other side; not deleted (a mirror never deletes on MySSI)" in caplog.text
    assert "deleting it" not in caplog.text
    assert fake.whats().count("save_divelog") == 1                        # the one upload of the first run; no delete


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
    assert got.external_ids == {"garmin": "2", "ssi": "900"}          # the reference read back as Garmin's id (K1)
    assert got.weight == 6.0 and got.weight_unit == "kilogram" and got.visibility == 15.0 and got.visibility_unit == "meter"
    assert len(got.gas_mixtures) == 1
    tank = got.gas_mixtures[0]
    assert (tank.oxygen, tank.start_pressure, tank.end_pressure, tank.tank_volume) == (32.0, 200.0, 70.0, 12.0)
    assert got.gf_low == 40 and got.gf_high == 85
    assert (got.computer_vendor, got.computer_model, got.computer_serial, got.computer_firmware) == ("Garmin", "Descent Mk3i", "1000000001", "19.10")
    assert got.device_logged is False                   # the imported flag was sent off
    assert got.water_type is None
    assert [s.time for s in got.samples] == list(range(0, 61, 5))
    assert got.samples[1].depth == 3.0 and got.samples[1].pressure == 190.0 and got.samples[1].channels.pressures == {0: 190.0}
    assert got.samples[0].temp == 28.0 and got.samples[0].channels.ndl is None
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
                                                     "odin_user_log_divecomputer_manufacturer": "Shearwater",
                                                     "odin_user_log_divecomputer_serial_nr": "A1B2", "odin_user_log_divecomputer_firmware": "v93",
                                                     "odin_user_log_divecomputer_imported": True, "odin_user_log_var_watertype_id": "5",
                                                     "odin_user_log_diveSamples": "not json",
                                                     "odin_user_log_manufacturer": "ignored (not an app key)"})
    dive = ssi.logbook_dive_to_unified(ssi.SsiLogbookDive.from_raw(raw))
    assert dive.gas_mixtures[0].oxygen == 21.0 and dive.gas_mixtures[0].start_pressure == 200.0   # ean flag off: air
    assert dive.computer_vendor == "Shearwater" and dive.computer_model == "Perdix 2" and dive.device_logged is True
    assert dive.computer_serial == "A1B2" and dive.computer_firmware == "v93" and dive.water_type == "salt"
    assert dive.samples == []
    assert ssi.logbook_dive_to_unified(ssi.SsiLogbookDive.from_raw({"odin_user_log_id": "1"})) is None
    assert ssi._parse_samples_text('[{"t": 5000, "d": 2.5}, {"x": 1}, "junk"]')[0].channels is None
    assert ssi._parse_samples_text("[1, 2]") == [] and ssi._parse_samples_text('{"a": 1}') == []
    # the app's point format: te 0 and ndl 99 are the format's "none", ndl minutes -> seconds
    points = ssi._parse_samples_text('[{"n": 1, "t": 0, "d": 0.0, "te": 0, "ndl": 99, "s": 0, "mf": 134217728, "gs": 0, "a": 0, "o": false, "dr": false},'
                                     ' {"n": 2, "t": 5000, "d": 4.0, "te": 27.5, "ndl": 45, "pressure": 190.5}]')
    assert points[0].temp is None and points[0].channels is None and points[0].time == 0
    assert points[1].temp == 27.5 and points[1].channels.ndl == 2700 and points[1].channels.pressures == {0: 190.5}
    assert points[1].pressure == 190.5 and points[1].time == 5


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


def test_adapter_update_refuses_without_a_request(caplog):
    fake = FakeSsi()
    fake.valid_tokens.add("t")
    adapter = adapter_for(fake, token="t")
    with caplog.at_level(logging.ERROR, logger="dive_sync.ssi"):
        assert adapter.update_dive("900", make_dive()) is False
    assert fake.calls == []
    assert "not updated" in caplog.text and "untested" in caplog.text


def test_adapter_delete_dive_undoes_an_upload(caplog):
    """`SsiAdapter.delete_dive(id)`: the dive is looked up in a fresh
    logbook, sent back flagged, read back; False when it is not there or
    MySSI keeps it; True again when it is already flagged."""
    fake = FakeSsi(sites=SITES)
    adapter = adapter_for(fake)
    ssi_id = adapter.add_dive(make_dive(start="2026-06-22 10:00", activity="2"))
    assert ssi_id == "900" and len(adapter.fetch_dives()) == 1
    fake.calls.clear()
    with caplog.at_level(logging.INFO, logger="dive_sync.ssi"):
        assert adapter.delete_dive(ssi_id) is True
    assert fake.whats() == ["get_divelog", "save_divelog", "get_divelog"]
    assert "deleting dive id 900" in caplog.text and "gone from the logbook" in caplog.text
    assert adapter.fetch_dives() == [] and fake.logbook[0]["odin_user_log_deleted"] == 1
    sent = fake.saved[-1]
    assert sent["odin_user_log_id"] == "900" and sent["odin_user_log_deleted"] is True and sent["needsUpload"] is True
    assert set(sent) - {"needsUpload"} <= {k for k in DIVEBRIDGE_KEYS if k.startswith("odin_user_log_")}
    # the same dive can be sent again afterwards: a deleted entry is no duplicate
    assert adapter.add_dive(make_dive(start="2026-06-22 10:00", activity="2")) == "901"
    with caplog.at_level(logging.ERROR, logger="dive_sync.ssi"):
        assert adapter.delete_dive("nope") is False
    assert "dive nope not deleted - not in the logbook" in caplog.text
    fake.return_deleted = True
    assert adapter.delete_dive("900") is True                 # already flagged
    fake.refuse_deletes = True
    assert adapter.delete_dive("901") is False
    broken = adapter_for(FakeSsi(password="other"))
    assert broken.delete_dive("900") is False


def test_adapter_honours_the_cooldown(no_sleep):
    adapter = ssi.SsiAdapter(EMAIL, PASSWORD, transport=FakeSsi(), cooldown_seconds=0.2)
    adapter.login()
    adapter.fetch_dives()
    assert no_sleep == [0.2, 0.2]


def test_no_module_switches_and_the_beta_note():
    """The Dive edit era's module switches (`UPLOAD_ENABLED` for Send to
    SSI, `READ_ENABLED` for Open MySSI; plans/convert.md I7, I7e) went with
    plans/myssi-sync.md K4: what MySSI takes is the adapter's flags.
    `BETA_NOTE`, shown on the Settings card, the MySSI Dives page and the
    Sync page's pair line (K3), says beta and names the two known gaps: a
    sync is add-only until the update test, and a dive with several tanks
    is skipped (until I7b)."""
    assert not hasattr(ssi, "UPLOAD_ENABLED") and not hasattr(ssi, "READ_ENABLED")
    assert ssi.SsiAdapter.accepts_updates is False and ssi.SsiAdapter.accepts_mirror_deletes is False
    assert ssi.BETA_NOTE.startswith("MySSI is in beta")
    assert "not fully tested" in ssi.BETA_NOTE and "unofficial" in ssi.BETA_NOTE
    assert "adds new dives only" in ssi.BETA_NOTE and "not sent yet" in ssi.BETA_NOTE
    assert "two or more tanks" in ssi.BETA_NOTE and "skips" in ssi.BETA_NOTE
    assert ssi.BETA_NOTE != ssi.UNOFFICIAL_NOTE and "\n" not in ssi.BETA_NOTE


def test_as_stored_makes_a_sent_profile_and_tank_equal_to_what_comes_back():
    """`SsiAdapter.as_stored`: MySSI keeps the profile on its own grid and no
    tank name, so the engine compares a sender's value in that form. A dive
    sent with an irregular (Garmin-like) profile and a named tank reads back
    different from what was sent, but equal to its stored form - no
    "Dive profile differs" or "changes not sent" on the next run."""
    from src.core.fields import values_equal
    fake = FakeSsi(sites=SITES)
    adapter = adapter_for(fake)
    times = [0, 1, 3, 4, 7, 9, 12, 13, 17, 22, 26, 29, 33, 38, 41, 47, 52, 58, 61]
    samples = [UnifiedSample(time=t, depth=round(min(t, 61 - t) * 0.4, 2), temp=28.0 - t / 100) for t in times]
    sent = make_dive(start="2026-06-22 10:00", activity="2").model_copy(update={"samples": samples})
    assert adapter.add_dive(sent) == "900"
    got = adapter.fetch_dives()[0]
    assert not values_equal("samples", sent.samples, got.samples)          # the raw comparison: MySSI regridded it
    assert values_equal("samples", ssi.SsiAdapter.as_stored("samples", sent.samples), got.samples)
    assert not values_equal("tanks", sent.gas_mixtures, got.gas_mixtures)  # "Tank 1" did not come back
    assert values_equal("tanks", ssi.SsiAdapter.as_stored("tanks", sent.gas_mixtures), got.gas_mixtures)
    assert ssi.SsiAdapter.as_stored("text", "x") == "x" and ssi.SsiAdapter.as_stored("samples", []) == []
