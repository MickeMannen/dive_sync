"""Qt Quick desktop app (rework.md Track D).

One ``QGuiApplication``, one ``QQmlApplicationEngine`` loading
``desktop/qml/main.qml``, and a handful of controller objects exposed as
context properties. All long work runs on ``QThread`` workers inside the
controllers; QML only binds to properties and calls slots.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Dict

from PySide6.QtCore import QCoreApplication, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

from desktop import credentials as creds_store
from desktop import logging_bridge
from desktop.controllers.about import AboutController
from desktop.controllers.conflicts import ConflictsController
from desktop.controllers.dives import DivesController
from desktop.controllers.mapping import MappingController
from desktop.controllers.settings import SettingsController
from desktop.controllers.sync import SyncController

QML_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qml")
APP_NAME = "DiveSync"

logger = logging.getLogger("dive_sync.desktop.app")


def build_controllers(log_queue) -> Dict[str, object]:
    return {
        "syncController": SyncController(log_queue),
        "garminDives": DivesController("garmin", log_queue),
        "divelogsDives": DivesController("divelogs", log_queue),
        "subsurfaceDives": DivesController("subsurface", log_queue),
        "settingsController": SettingsController(),
        "mappingController": MappingController(),
        "conflictsController": ConflictsController(),
        "aboutController": AboutController(),
    }


def available_screen_size() -> Dict[str, int]:
    """The launch screen's usable area, menu bar and dock/taskbar excluded.

    QML's Screen attached property only offers ``desktopAvailable*``, which is
    the union of every screen: on a laptop docked to a larger monitor that is
    far taller than the screen the window actually opens on, so it cannot be
    used to keep the window on-screen. QScreen gives the real thing."""
    screen = QGuiApplication.primaryScreen()
    if screen is None:      # offscreen/headless platform plugin
        return {"width": 0, "height": 0}
    geometry = screen.availableGeometry()
    return {"width": geometry.width(), "height": geometry.height()}


def create_engine(controllers: Dict[str, object], initial_section: str,
                  on_warnings=None) -> QQmlApplicationEngine:
    """``on_warnings`` is connected before the QML is loaded, so it sees the
    errors raised while loading it too. A caller that connects to
    ``engine.warnings`` on the returned engine only ever sees later ones -
    which is how a "Delegate must be of Item type" in MappingPage.qml went
    unnoticed by the test suite while printing on every single launch."""
    engine = QQmlApplicationEngine()
    if on_warnings is not None:
        engine.warnings.connect(on_warnings)
    context = engine.rootContext()
    for name, controller in controllers.items():
        context.setContextProperty(name, controller)
    context.setContextProperty("initialSection", initial_section)
    context.setContextProperty("availableScreen", available_screen_size())
    engine.addImportPath(QML_DIR)
    engine.load(QUrl.fromLocalFile(os.path.join(QML_DIR, "main.qml")))
    return engine


def cleanup_on_quit() -> None:
    """Qt's aboutToQuit fires on Cmd+Q / the Quit menu too (unlike Toga), so
    the materialised credentials and Garmin token never outlive the app.

    Each step is its own try/except: a hands-on run (rework.md D7,
    2026-09-22) found that a keychain permission re-prompt on
    sync_garmin_token_from_file's write (e.g. after rebuilding the app with
    a new ad-hoc signature) raised, and because that call previously shared
    a try block with clear_garmin_token_file right after it, the exception
    skipped the clear too - silently leaving the plaintext token file on
    disk with no error logged anywhere. Losing an unsynced token refresh
    (forces a fresh login next time) is an acceptable trade against leaving
    a real credential in plaintext, so the file must still be removed even
    when the keychain sync failed."""
    try:
        model = creds_store.load_credentials_model()
    except Exception as e:
        logger.warning("Could not load credentials for quit cleanup: %s", e)
        model = None
    if model is not None:
        for account in model.get_garmin_accounts():
            try:
                creds_store.sync_garmin_token_from_file(account.username, account.token_dir)
            except Exception as e:
                logger.warning("Failed to sync Garmin token for %s back to the keychain on quit: %s", account.username, e)
            try:
                creds_store.clear_garmin_token_file(account.username, account.token_dir)
            except Exception as e:
                logger.warning("Failed to remove the materialized Garmin token file for %s on quit: %s", account.username, e)
    try:
        creds_store.clear_local_cache()
    except Exception as e:
        logger.warning("Failed to remove the materialized credentials file on quit: %s", e)


def main() -> int:
    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setOrganizationName("Mikael Christersson")
    app = QGuiApplication(sys.argv)
    log_queue = logging_bridge.install()
    controllers = build_controllers(log_queue)
    initial = "Sync" if creds_store.has_any_credentials() else "Settings"
    engine = create_engine(controllers, initial)
    if not engine.rootObjects():
        return 1
    app.aboutToQuit.connect(cleanup_on_quit)
    app._dive_sync_controllers = controllers  # keep them alive for the app's lifetime
    result = app.exec()
    # Tear the QML tree down here, while the controllers its bindings read are
    # still alive. Left to interpreter shutdown, the controllers can be
    # collected first: every binding on every page the user visited then
    # re-evaluates against a context property that has become null, and the
    # app exits behind a screenful of "TypeError: Cannot read property ... of
    # null". Nothing is broken when that happens, but it buries any real
    # message in the log.
    del engine
    return result
