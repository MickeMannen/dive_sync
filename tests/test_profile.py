import json

import pytest

from src.core.config import (
    PROFILE_VERSION,
    CronJobModel,
    ProfileError,
    SettingsModel,
    SyncFilters,
    export_profile,
    import_profile,
    read_profile,
    write_profile,
)
from src.core.fields import FieldLink, build_catalog
from src.core.services.divelogs import DivelogsAdapter
from src.core.services.garmin import GarminAdapter


def _catalog():
    return build_catalog(GarminAdapter.field_catalog(), DivelogsAdapter.field_catalog())


def test_export_contains_everything_but_secrets():
    settings = SettingsModel(directionality="to_divelogs", cron_jobs=[CronJobModel(id="n")])
    profile = export_profile(settings)
    assert profile["dive_sync_profile"] == PROFILE_VERSION
    assert set(profile) == {"dive_sync_profile", "directionality", "sync_filters", "grace_window_minutes",
                            "api_cooldown_seconds", "field_links", "schedule", "cron_jobs"}
    text = json.dumps(profile)
    assert "password" not in text and "token" not in text
    assert profile["cron_jobs"][0]["id"] == "n" and len(profile["field_links"]) == 11


def test_round_trip_through_a_file(tmp_path):
    settings = SettingsModel(grace_window_minutes=30, field_links=[
        FieldLink(id="buddy", source=["garmin.buddy"], target="divelogs.buddy", conflict="manual")
    ])
    path = str(tmp_path / "profile.json")
    write_profile(settings, path)
    new, summary = import_profile(read_profile(path), SettingsModel(), _catalog())
    assert new == settings
    assert "grace_window_minutes: 15 -> 30" in summary.changes
    assert any(line.startswith("field_links: 11 -> 1 links") and "removed" in line for line in summary.changes)


def test_import_replaces_only_present_sections_and_reports():
    current = SettingsModel(directionality="to_garmin", grace_window_minutes=20,
                            sync_filters=SyncFilters(only_new=False), cron_jobs=[CronJobModel(id="keep")])
    data = {"dive_sync_profile": 1, "directionality": "bidirectional",
            "sync_filters": {"only_new": True, "sync_gases": False}, "unknown_section": 5}
    new, summary = import_profile(data, current)
    assert new.directionality == "bidirectional"
    assert new.grace_window_minutes == 20 and new.cron_jobs[0].id == "keep"  # untouched sections stay
    assert new.sync_filters.only_new is True and new.sync_filters.sync_gases is False
    assert summary.sections == ["directionality", "sync_filters"]
    assert summary.ignored_keys == ["unknown_section"]
    assert "directionality: 'to_garmin' -> 'bidirectional'" in summary.changes
    assert "sync_filters.only_new: False -> True" in summary.changes
    assert "Profile version 1" in summary.as_text()


def test_import_skips_links_with_unknown_fields_but_keeps_the_rest():
    data = {"dive_sync_profile": 1, "field_links": [
        {"id": "ok", "source": ["garmin.buddy"], "target": "divelogs.buddy"},
        {"id": "future", "source": ["garmin.rating"], "target": "divelogs.rating"},
    ]}
    new, summary = import_profile(data, SettingsModel(), _catalog())
    assert [l.id for l in new.field_links] == ["ok"] and summary.skipped_links == ["future"]
    # without a catalogue nothing is skipped
    new, summary = import_profile(data, SettingsModel())
    assert len(new.field_links) == 2 and summary.skipped_links == []


def test_import_refuses_newer_or_broken_profiles(tmp_path):
    with pytest.raises(ProfileError, match="newer Dive Sync"):
        import_profile({"dive_sync_profile": PROFILE_VERSION + 1}, SettingsModel())
    with pytest.raises(ProfileError, match="not a Dive Sync profile"):
        import_profile({"directionality": "bidirectional"}, SettingsModel())
    with pytest.raises(ProfileError, match="whole number"):
        import_profile({"dive_sync_profile": "1"}, SettingsModel())
    with pytest.raises(ProfileError, match="invalid values"):
        import_profile({"dive_sync_profile": 1, "grace_window_minutes": "soon"}, SettingsModel())
    with pytest.raises(ProfileError, match="not found"):
        read_profile(str(tmp_path / "missing.json"))
    (tmp_path / "bad.json").write_text("{")
    with pytest.raises(ProfileError, match="not valid JSON"):
        read_profile(str(tmp_path / "bad.json"))
