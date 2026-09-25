from tests.test_desktop_qt import qapp, scratch_data_dir, fake_keyring, wait  # noqa: F401

def test_quit(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    from PySide6.QtCore import QObject
    from desktop import app as desktop_app
    from desktop import logging_bridge
    from src.core import dive_cache
    rows = [{"date": "2026-06-22", "time": "10:00:00", "date_time": "2026-06-22 10:00:00", "filename": "1.json", "location": "R"}]
    monkeypatch.setattr(dive_cache, "list_dives", lambda service, *a, **k: [dict(r) for r in rows] if service == "garmin" else [])
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: [])
    monkeypatch.setattr(dive_cache, "get_samples", lambda *a, **k: [])
    controllers = desktop_app.build_controllers(logging_bridge.install())
    engine = desktop_app.create_engine(controllers, "Garmin Dives")
    root = engine.rootObjects()[0]
    root.show(); wait(qapp, 300)
    controllers["garminDives"].stage("1.json", {"buddy": "x"})
    root.close(); wait(qapp, 300)
    dialog = root.findChild(QObject, "leaveDivesDialog")
    print("AFTER CLOSE visible:", root.isVisible(), "dialog open:", dialog.property("opened"))
    dialog.discarded.emit(); wait(qapp, 500)
    print("AFTER DISCARD visible:", root.isVisible(), "dialog open:", dialog.property("opened"))
