"""Settings page backend: keychain credentials with test buttons, and sync
profile export / import (rework.md D5, C11)."""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Property, QObject, Signal, Slot

from desktop import credentials as creds_store
from desktop.jobs import Worker


class SettingsController(QObject):
    messageChanged = Signal()
    garminStatusChanged = Signal()
    divelogsStatusChanged = Signal()
    subsurfaceStatusChanged = Signal()
    credentialsChanged = Signal()
    profileSummaryChanged = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._message = ""
        self._garmin_status = ""
        self._divelogs_status = ""
        self._subsurface_status = ""
        self._profile_summary = ""
        self._pending_profile = None
        self._workers = []
        self._model = creds_store.load_credentials_model()

    # -- properties -------------------------------------------------------

    @Property(str, notify=messageChanged)
    def message(self) -> str:
        return self._message

    @Property(str, notify=garminStatusChanged)
    def garminStatus(self) -> str:
        return self._garmin_status

    @Property(str, notify=divelogsStatusChanged)
    def divelogsStatus(self) -> str:
        return self._divelogs_status

    @Property(str, notify=subsurfaceStatusChanged)
    def subsurfaceStatus(self) -> str:
        return self._subsurface_status

    @Property(str, notify=profileSummaryChanged)
    def profileSummary(self) -> str:
        return self._profile_summary

    @Property(bool, notify=credentialsChanged)
    def hasCredentials(self) -> bool:
        return creds_store.has_any_credentials()

    @Property(str, notify=credentialsChanged)
    def garminUsername(self) -> str:
        accounts = self._model.get_garmin_accounts()
        return accounts[0].username if accounts else ""

    @Property(str, notify=credentialsChanged)
    def garminTokenDir(self) -> str:
        accounts = self._model.get_garmin_accounts()
        return (accounts[0].token_dir if accounts else "") or creds_store.DEFAULT_GARMIN_TOKEN_DIR

    @Property(str, notify=credentialsChanged)
    def divelogsUsername(self) -> str:
        accounts = self._model.get_divelogs_accounts()
        return accounts[0].username if accounts else ""

    @Property(str, notify=credentialsChanged)
    def subsurfaceEmail(self) -> str:
        return self._model.subsurface.email

    def _set(self, attr, value, signal):
        setattr(self, attr, value)
        signal.emit()

    def _run(self, work, done):
        worker = Worker(work, parent=self)
        worker.finished_ok.connect(done)
        worker.failed.connect(lambda message: done(f"Error: {message}"))
        self._workers.append(worker)
        worker.start()

    # -- tests ------------------------------------------------------------

    @Slot(str, str, str)
    def testGarmin(self, username: str, password: str, token_dir: str) -> None:
        accounts = self._model.get_garmin_accounts()
        password = password or (accounts[0].password if accounts else "")
        token_dir = token_dir or creds_store.DEFAULT_GARMIN_TOKEN_DIR
        if not username or not password:
            self._set("_garmin_status", "Enter a username and password first.", self.garminStatusChanged)
            return
        self._set("_garmin_status", "Testing…", self.garminStatusChanged)

        def work():
            from src.core.services.garmin import GarminAdapter
            return "Login OK." if GarminAdapter(username, password, token_dir=token_dir).login() else "Login failed."
        self._run(work, lambda text: self._set("_garmin_status", str(text), self.garminStatusChanged))

    @Slot(str, str)
    def testDivelogs(self, username: str, password: str) -> None:
        accounts = self._model.get_divelogs_accounts()
        password = password or (accounts[0].password if accounts else "")
        if not username or not password:
            self._set("_divelogs_status", "Enter a username and password first.", self.divelogsStatusChanged)
            return
        self._set("_divelogs_status", "Testing…", self.divelogsStatusChanged)

        def work():
            from src.core.services.divelogs import DivelogsAdapter
            return "Login OK." if DivelogsAdapter(username, password).login() else "Login failed."
        self._run(work, lambda text: self._set("_divelogs_status", str(text), self.divelogsStatusChanged))

    @Slot(str, str)
    def testSubsurface(self, email: str, password: str) -> None:
        password = password or self._model.subsurface.password
        if not email or not password:
            self._set("_subsurface_status", "Enter an email and password first.", self.subsurfaceStatusChanged)
            return
        self._set("_subsurface_status", "Testing…", self.subsurfaceStatusChanged)

        def work():
            from src.core.services.subsurface_cloud import check_cloud_login
            ok, message = check_cloud_login(email, password, self._model.subsurface.base_url)
            return message
        self._run(work, lambda text: self._set("_subsurface_status", str(text), self.subsurfaceStatusChanged))

    # -- save -------------------------------------------------------------

    @Slot(str, str, str, str, str, str, str)
    def save(self, garmin_user: str, garmin_pw: str, token_dir: str, divelogs_user: str, divelogs_pw: str,
             subsurface_email: str, subsurface_pw: str) -> None:
        from src.core.config import CredentialsModel, DivelogsCredentials, GarminCredentials, SubsurfaceCredentials
        g_accounts = self._model.get_garmin_accounts()
        d_accounts = self._model.get_divelogs_accounts()
        model = CredentialsModel(
            garmin=GarminCredentials(username=garmin_user, password=garmin_pw or (g_accounts[0].password if g_accounts else ""),
                                     token_dir=token_dir or creds_store.DEFAULT_GARMIN_TOKEN_DIR),
            divelogs=DivelogsCredentials(username=divelogs_user, password=divelogs_pw or (d_accounts[0].password if d_accounts else "")),
            subsurface=SubsurfaceCredentials(email=subsurface_email, password=subsurface_pw or self._model.subsurface.password,
                                             base_url=self._model.subsurface.base_url),
        )
        creds_store.save_credentials_model(model)
        self._model = creds_store.load_credentials_model()
        self.credentialsChanged.emit()
        self._set("_message", "Saved to keychain.", self.messageChanged)

    # -- profiles ---------------------------------------------------------

    @Slot(str, result=str)
    def exportProfile(self, path: str) -> str:
        from src.core.config import ConfigManager, write_profile
        path = _local_path(path)
        try:
            write_profile(ConfigManager.load_settings(), path)
        except Exception as e:
            return f"Export failed: {e}"
        return f"Profile written to {path}"

    @Slot(str, result=str)
    def checkProfile(self, path: str) -> str:
        """Read a profile and show the diff summary; nothing is written."""
        from src.core.config import ConfigManager, ProfileError, import_profile, read_profile
        from src.core.templates import validate_links
        from src.web.app import _pair_catalog  # same catalogue the status page uses
        path = _local_path(path)
        try:
            data = read_profile(path)
            new_settings, summary = import_profile(data, ConfigManager.load_settings(), _pair_catalog())
        except ProfileError as e:
            self._pending_profile = None
            self._set("_profile_summary", "", self.profileSummaryChanged)
            return f"Profile cannot be imported: {e}"
        problems = validate_links(new_settings.field_links, _pair_catalog())
        if problems:
            self._pending_profile = None
            self._set("_profile_summary", "\n".join(problems), self.profileSummaryChanged)
            return "The profile's field links are invalid."
        self._pending_profile = new_settings
        self._set("_profile_summary", summary.as_text(), self.profileSummaryChanged)
        return "Review the changes, then press Apply." if summary.changes or summary.skipped_links else "Nothing would change."

    @Slot(result=str)
    def applyProfile(self) -> str:
        from src.core.config import ConfigManager
        if self._pending_profile is None:
            return "Check a profile first."
        ConfigManager.save_settings(self._pending_profile)
        self._pending_profile = None
        self._set("_profile_summary", "", self.profileSummaryChanged)
        return "Profile applied."


def _local_path(path: str) -> str:
    """QML file dialogs hand back file:// URLs."""
    if path.startswith("file://"):
        from PySide6.QtCore import QUrl
        return QUrl(path).toLocalFile()
    return path
