"""The desktop mapping board's drag-and-drop, driven with real mouse events
on an offscreen window (rework.md G6): press a sender field, move it onto a
receiver field, release - a rule must appear."""
import os

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

from tests.test_desktop_qt import fake_keyring, qapp, scratch_data_dir, wait  # noqa: F401  (fixtures)


def _walk(item):
    yield item
    for child in item.childItems():
        yield from _walk(child)


def _item(window, name):
    """Find a QML item by objectName in the *visual* tree: Repeater delegates
    have no QObject parent, so QObject.findChild never reaches them."""
    for item in _walk(window.contentItem()):
        if item.objectName() == name:
            return item
    names = sorted(i.objectName() for i in _walk(window.contentItem())
                   if i.objectName().startswith(("panel-", "sender-", "receiver-")))
    raise AssertionError(f"{name} not found; board items: {names[:80]}")


def _centre(item):
    return item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()


def _drag(window, start, end, steps=40, vertical_first=250):
    """Press, pull mostly *vertically* first (the gesture a scrolling
    Flickable likes to steal), then across to the target, release."""
    from PySide6.QtCore import QPoint
    waypoint = QPoint(start.x(), start.y() + vertical_first)
    QTest.mousePress(window, Qt.LeftButton, Qt.NoModifier, start)
    QTest.qWait(30)
    for a, b in ((start, waypoint), (waypoint, end)):
        for i in range(1, steps + 1):
            pos = a + (b - a) * i / steps
            QTest.mouseMove(window, pos, 10)
    QTest.qWait(30)
    QTest.mouseRelease(window, Qt.LeftButton, Qt.NoModifier, end)
    QTest.qWait(80)


def test_dragging_a_sender_field_onto_a_receiver_field_creates_a_rule(qapp, scratch_data_dir, fake_keyring, monkeypatch):
    from desktop import app as desktop_app
    from desktop import logging_bridge
    from src.core import dive_cache
    monkeypatch.setattr(dive_cache, "list_garmin_dives", lambda *a, **k: [])
    monkeypatch.setattr(dive_cache, "list_divelogs_dives", lambda *a, **k: [])
    controllers = desktop_app.build_controllers(logging_bridge.install())
    mapping = controllers["mappingController"]
    warnings = []
    engine = desktop_app.create_engine(controllers, "Mapping",
                                       on_warnings=lambda errs: warnings.extend(str(e.toString()) for e in errs))
    assert engine.rootObjects(), "main.qml did not load"
    import shiboken6
    from PySide6.QtQuick import QQuickWindow
    # PySide hands the root object back typed as QWindow; the QML tree hangs off QQuickWindow.contentItem
    window = shiboken6.wrapInstance(shiboken6.getCppPointer(engine.rootObjects()[0])[0], QQuickWindow)
    window.setWidth(1400)
    window.setHeight(1000)
    window.show()
    wait(qapp, 400)
    sections = window.property("sections").toVariant()
    window.setProperty("currentSection", sections.index("Mapping"))
    wait(qapp, 400)

    assert [p["receiver"] for p in mapping.receivers] == ["divelogs", "garmin"]
    assert warnings == [], warnings
    assert _item(window, "panel-divelogs") is not None
    # Divelogs is the run's receiver (to_divelogs): its panel comes first.
    # Divelogs' 'notes' already has a rule; 'temp_min' has none - give it Garmin's minimum temperature.
    # a long, mostly vertical pull from the bottom of the sender list to the top of
    # the receiver list - exactly the gesture a scrolling Flickable likes to steal
    target = _item(window, "receiver-divelogs-divelogs.temp_min")
    source = _item(window, "sender-divelogs-garmin.temp_min")
    assert not any(r["target"] == "divelogs.temp_min" for r in mapping.receivers[0]["rules"])
    start, end = _centre(source), _centre(target)
    assert 0 <= start.y() < window.height() and 0 <= end.y() < window.height(), (start, end, window.height())

    _drag(window, start, end)
    wait(qapp, 200)

    rules = mapping.receivers[0]["rules"]
    created = next((r for r in rules if r["target"] == "divelogs.temp_min"), None)
    assert created is not None, [r["id"] for r in rules]
    assert created["source"] == ["garmin.temp_min"] and mapping.selectedId == created["id"]

    # the other way round works too: pull a receiver field onto a sender field.
    # Switch the viewed direction so the Garmin panel comes first (on screen).
    mapping.setViewDirection("to_garmin")
    wait(qapp, 300)
    target = _item(window, "receiver-garmin-garmin.temp_min")
    source = _item(window, "sender-garmin-divelogs.temp_min")
    _drag(window, _centre(target), _centre(source), vertical_first=-120)
    wait(qapp, 200)
    garmin_rules = next(p for p in mapping.receivers if p["receiver"] == "garmin")["rules"]
    created = next((r for r in garmin_rules if r["target"] == "garmin.temp_min"), None)
    assert created is not None, [r["id"] for r in garmin_rules]
    assert created["source"] == ["divelogs.temp_min"]

    # a drop that lands nowhere says so instead of snapping back silently
    from PySide6.QtCore import QPoint
    source = _item(window, "sender-garmin-divelogs.max_depth")
    _drag(window, _centre(source), _centre(source) + QPoint(0, 60), vertical_first=0)
    wait(qapp, 200)
    assert mapping.message.startswith("Drop a field onto a field")
    engine.deleteLater()
    wait(qapp, 50)
