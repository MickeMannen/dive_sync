"""Read-only checks against the test accounts. See conftest.py."""
import os

import pytest

pytestmark = pytest.mark.live


@pytest.fixture(scope="session")
def recent(live_engine):
    return {
        "garmin": live_engine.source.fetch_recent_dives(5),
        "divelogs": live_engine.target.fetch_recent_dives(5),
    }


def test_both_services_return_dives(recent):
    assert recent["garmin"], "no dives on the Garmin test account"
    assert recent["divelogs"], "no dives on the Divelogs test account"
    for service, dives in recent.items():
        for dive in dives:
            assert dive.external_ids.get(service), f"{service} dive without an external id"
            assert dive.duration > 0 and dive.max_depth > 0


def test_garmin_live_fetch_populates_profile_and_tanks(recent):
    """The offline cache showed Garmin dives with no samples/tanks; a live
    fetch must deliver them for at least one recent dive (a dive logged by
    hand on Garmin Connect legitimately has neither, so 'any' not 'all')."""
    dives = recent["garmin"]
    with_samples = [d for d in dives if d.samples]
    with_tanks = [d for d in dives if d.gas_mixtures]
    print(f"garmin: {len(dives)} dives, {len(with_samples)} with samples, {len(with_tanks)} with tanks; "
          f"tank counts {[len(d.gas_mixtures) for d in dives]}")
    assert with_samples, "no recent Garmin dive carries profile samples"
    assert with_tanks, "no recent Garmin dive carries tank/gas data"


def test_divelogs_live_fetch_shape(recent):
    dives = recent["divelogs"]
    print(f"divelogs: {len(dives)} dives; tank counts {[len(d.gas_mixtures) for d in dives]}; "
          f"samples {[len(d.samples) for d in dives]}")
    for dive in dives:
        assert "location" in dive.service_fields and "divesite" in dive.service_fields


def test_default_board_test_mapping_is_clean(live_engine):
    out = live_engine.test_mapping(limit=5)
    assert out["ok"], out["problems"]
    print(f"test mapping: fetched {out['fetched']}, matched {out['matched']}, "
          f"results {sorted({r['result'] for r in out['rows']})}")
    assert not any(r["warnings"] for r in out["rows"])
    assert not os.path.exists(live_engine.state_file), "test_mapping must not write state"


def test_subsurface_cloud_login(live_credentials):
    from src.core.services.subsurface_cloud import check_cloud_login
    creds = live_credentials.subsurface
    if not creds.configured:
        pytest.skip("no Subsurface credentials in the test data dir")
    ok, message = check_cloud_login(creds.email, creds.password, creds.base_url)
    assert ok, message


def test_submersion_store_access(live_credentials):
    from src.core.services.submersion.store import S3Store, check_store_access
    creds = live_credentials.submersion
    if not creds.configured:
        pytest.skip("no Submersion store in the test data dir")
    ok, message = check_store_access(creds)
    assert ok, message
    print(message)
    if creds.store_type == "s3":
        names = S3Store(creds).list("ssv1.")
        print(f"submersion sync files: {len(names)}; kinds: {sorted({n.split('.')[1] for n in names if n.count('.') > 1})}")
