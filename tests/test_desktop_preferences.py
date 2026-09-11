import desktop.preferences as prefs


def test_get_visible_columns_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs, "PREFS_FILE", str(tmp_path / "prefs.json"))
    assert prefs.get_visible_columns("garmin", default=["date", "time"]) == ["date", "time"]


def test_set_and_get_visible_columns_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs, "PREFS_FILE", str(tmp_path / "prefs.json"))
    prefs.set_visible_columns("garmin", ["date", "location"])
    assert prefs.get_visible_columns("garmin", default=["date", "time"]) == ["date", "location"]


def test_visible_columns_are_scoped_per_service(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs, "PREFS_FILE", str(tmp_path / "prefs.json"))
    prefs.set_visible_columns("garmin", ["date"])
    prefs.set_visible_columns("divelogs", ["location"])
    assert prefs.get_visible_columns("garmin", default=[]) == ["date"]
    assert prefs.get_visible_columns("divelogs", default=[]) == ["location"]


def test_get_sort_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs, "PREFS_FILE", str(tmp_path / "prefs.json"))
    assert prefs.get_sort("garmin", default_column="date") == ("date", True)


def test_set_and_get_sort_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs, "PREFS_FILE", str(tmp_path / "prefs.json"))
    prefs.set_sort("garmin", "max_depth", ascending=False)
    assert prefs.get_sort("garmin", default_column="date") == ("max_depth", False)


def test_load_survives_corrupt_file(tmp_path, monkeypatch):
    prefs_file = tmp_path / "prefs.json"
    prefs_file.write_text("{not valid json")
    monkeypatch.setattr(prefs, "PREFS_FILE", str(prefs_file))
    assert prefs.get_visible_columns("garmin", default=["date"]) == ["date"]
