"""About page backend: version, license, links, and an update check."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Property, QObject, Signal, Slot

from desktop import paths
from desktop.jobs import Worker
from src.core import about


class AboutController(QObject):
    updateStatusChanged = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._info = about.about_info()
        self._update_status = ""
        self._update_url = ""
        self._worker = None

    @Property(str, constant=True)
    def name(self) -> str:
        return str(self._info["name"])

    @Property(str, constant=True)
    def version(self) -> str:
        return str(self._info["version"])

    @Property(str, constant=True)
    def author(self) -> str:
        return str(self._info["author"])

    @Property(str, constant=True)
    def licenseName(self) -> str:
        return str(self._info["license_name"])

    @Property(str, constant=True)
    def licenseText(self) -> str:
        return str(self._info["license_text"])

    @Property(str, constant=True)
    def projectUrl(self) -> str:
        return str(self._info["project_url"])

    @Property(str, constant=True)
    def issuesUrl(self) -> str:
        return str(self._info["issues_url"])

    @Property(str, constant=True)
    def platform(self) -> str:
        return str(self._info["platform"])

    @Property("QVariantList", constant=True)
    def components(self):
        return list(self._info["components"])

    @Property(str, constant=True)
    def dataDir(self) -> str:
        return paths.data_dir()

    @Property(str, notify=updateStatusChanged)
    def updateStatus(self) -> str:
        return self._update_status

    @Property(str, notify=updateStatusChanged)
    def updateUrl(self) -> str:
        return self._update_url

    @Slot()
    def checkForUpdates(self) -> None:
        """Ask GitHub for the latest release (a two-second request, off the GUI thread)."""
        self._update_status = "Checking…"
        self.updateStatusChanged.emit()

        def work():
            from src.core.version import get_version_info
            return get_version_info()

        def done(info):
            latest = info.get("latest_version") or ""
            if info.get("update_available"):
                self._update_status = f"Version {latest.lstrip('v')} is available."
                self._update_url = info.get("release_url") or ""
            else:
                self._update_status = "You have the latest version."
                self._update_url = ""
            self.updateStatusChanged.emit()

        def fail(message):
            self._update_status = f"Could not check: {message}"
            self.updateStatusChanged.emit()

        worker = Worker(work, parent=self)
        worker.finished_ok.connect(done)
        worker.failed.connect(fail)
        self._worker = worker
        worker.start()
