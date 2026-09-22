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
    submersionStatusChanged = Signal()
    credentialsChanged = Signal()
    profileSummaryChanged = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._message = ""
        self._garmin_status = ""
        self._divelogs_status = ""
        self._subsurface_status = ""
        self._submersion_status = ""
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

    @Property(str, notify=submersionStatusChanged)
    def submersionStatus(self) -> str:
        return self._submersion_status

    @Property(str, notify=profileSummaryChanged)
    def profileSummary(self) -> str:
        return self._profile_summary

    @Property(bool, notify=credentialsChanged)
    def hasCredentials(self) -> bool:
        return creds_store.has_any_credentials()

    @Property("QVariantList", notify=credentialsChanged)
    def garminAccounts(self):
        """Usernames and token dirs only - never the password, same as the
        status page's account rows (rework.md E7)."""
        return [{"username": a.username, "token_dir": a.token_dir} for a in self._model.get_garmin_accounts()]

    @Property("QVariantList", notify=credentialsChanged)
    def divelogsAccounts(self):
        return [{"username": a.username} for a in self._model.get_divelogs_accounts()]

    @Slot("QVariantList")
    def saveGarminAccounts(self, rows) -> None:
        from src.core.config import GarminCredentials
        accounts = [
            GarminCredentials(username=str(r.get("username", "")).strip(), password=str(r.get("password", "")),
                              token_dir=str(r.get("token_dir", "")).strip() or creds_store.DEFAULT_GARMIN_TOKEN_DIR)
            for r in rows if str(r.get("username", "")).strip()
        ]
        creds_store._save_accounts("garmin", accounts)
        self._model = creds_store.load_credentials_model()
        self.credentialsChanged.emit()
        self._set("_message", "Saved to keychain.", self.messageChanged)

    @Slot("QVariantList")
    def saveDivelogsAccounts(self, rows) -> None:
        from src.core.config import DivelogsCredentials
        accounts = [
            DivelogsCredentials(username=str(r.get("username", "")).strip(), password=str(r.get("password", "")))
            for r in rows if str(r.get("username", "")).strip()
        ]
        creds_store._save_accounts("divelogs", accounts)
        self._model = creds_store.load_credentials_model()
        self.credentialsChanged.emit()
        self._set("_message", "Saved to keychain.", self.messageChanged)

    @Property(str, notify=credentialsChanged)
    def subsurfaceEmail(self) -> str:
        return self._model.subsurface.email

    @Property(str, notify=credentialsChanged)
    def submersionStoreType(self) -> str:
        return self._model.submersion.store_type

    @Property(str, notify=credentialsChanged)
    def submersionEndpointUrl(self) -> str:
        return self._model.submersion.endpoint_url

    @Property(str, notify=credentialsChanged)
    def submersionRegion(self) -> str:
        return self._model.submersion.region

    @Property(str, notify=credentialsChanged)
    def submersionBucket(self) -> str:
        return self._model.submersion.bucket

    @Property(str, notify=credentialsChanged)
    def submersionPrefix(self) -> str:
        return self._model.submersion.prefix

    @Property(str, notify=credentialsChanged)
    def submersionAccessKeyId(self) -> str:
        return self._model.submersion.access_key_id

    @Property(bool, notify=credentialsChanged)
    def submersionPathStyle(self) -> bool:
        return self._model.submersion.path_style

    @Property(str, notify=credentialsChanged)
    def submersionFolderPath(self) -> str:
        return self._model.submersion.folder_path

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
        stored = next((a for a in self._model.get_garmin_accounts() if a.username == username), None)
        password = password or (stored.password if stored else "")
        token_dir = token_dir or (stored.token_dir if stored else "") or creds_store.DEFAULT_GARMIN_TOKEN_DIR
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
        stored = next((a for a in self._model.get_divelogs_accounts() if a.username == username), None)
        password = password or (stored.password if stored else "")
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

    @Slot(str, str, str, str, str, str, str, bool, str, str)
    def testSubmersion(self, store_type: str, endpoint_url: str, region: str, bucket: str, prefix: str,
                        access_key_id: str, secret_access_key: str, path_style: bool, folder_path: str,
                        passphrase: str = "") -> None:
        from src.core.config import SubmersionCredentials
        config = SubmersionCredentials(
            store_type=store_type or "s3",
            endpoint_url=endpoint_url,
            region=region,
            bucket=bucket,
            prefix=prefix or SubmersionCredentials().prefix,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key or self._model.submersion.secret_access_key,
            path_style=path_style,
            folder_path=folder_path,
            passphrase=passphrase or self._model.submersion.passphrase,
        )
        if not config.configured:
            self._set("_submersion_status", "Fill in the store details first.", self.submersionStatusChanged)
            return
        self._set("_submersion_status", "Testing…", self.submersionStatusChanged)

        def work():
            import tempfile
            from src.core.services.submersion.store import check_store_access
            from src.core.services.submersion.adapter import SubmersionAdapter
            ok, message = check_store_access(config)
            if not ok:
                return message
            # Connectivity is fine; also try to unlock an end-to-end
            # encrypted library so a wrong/missing passphrase surfaces here
            # rather than only on the next real sync.
            with tempfile.TemporaryDirectory() as scratch:
                enc_ok, enc_message = SubmersionAdapter(config, device_state_dir=scratch)._resolve_encryption()
            if not enc_ok:
                return enc_message
            return f"{message} {enc_message}." if enc_message else message
        self._run(work, lambda text: self._set("_submersion_status", str(text), self.submersionStatusChanged))

    # -- save -------------------------------------------------------------

    @Slot(str, str, str, str, str, str, str, str, str, bool, str, str)
    def save(self, subsurface_email: str, subsurface_pw: str,
             submersion_store_type: str, submersion_endpoint_url: str, submersion_region: str,
             submersion_bucket: str, submersion_prefix: str, submersion_access_key_id: str,
             submersion_secret_access_key: str, submersion_path_style: bool, submersion_folder_path: str,
             submersion_passphrase: str = "") -> None:
        """Subsurface Cloud + Submersion only - Garmin/Divelogs accounts save
        independently via saveGarminAccounts()/saveDivelogsAccounts() (E7),
        since this writes those two sections directly rather than going
        through save_credentials_model(), which would otherwise replace the
        whole account list with whatever (nothing) this slot was given."""
        from src.core.config import SubsurfaceCredentials, SubmersionCredentials
        creds_store.save_subsurface_credentials(SubsurfaceCredentials(
            email=subsurface_email,
            password=subsurface_pw or self._model.subsurface.password,
            base_url=self._model.subsurface.base_url,
        ) if subsurface_email else None)
        creds_store.save_submersion_credentials(SubmersionCredentials(
            store_type=submersion_store_type or "s3",
            endpoint_url=submersion_endpoint_url,
            region=submersion_region,
            bucket=submersion_bucket,
            prefix=submersion_prefix or SubmersionCredentials().prefix,
            access_key_id=submersion_access_key_id,
            secret_access_key=submersion_secret_access_key or self._model.submersion.secret_access_key,
            path_style=submersion_path_style,
            folder_path=submersion_folder_path,
            passphrase=submersion_passphrase or self._model.submersion.passphrase,
        ))

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
