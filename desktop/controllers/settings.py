"""Settings page backend: keychain credentials with test buttons, and sync
profile export / import (rework.md D5, C11)."""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Property, QObject, Signal, Slot

from desktop import credentials as creds_store
from desktop.jobs import Worker
from src.core import layout

logger = logging.getLogger("dive_sync.desktop.settings")


class SettingsController(QObject):
    messageChanged = Signal()
    garminStatusChanged = Signal()
    divelogsStatusChanged = Signal()
    subsurfaceStatusChanged = Signal()
    submersionStatusChanged = Signal()
    ssiStatusChanged = Signal()
    credentialsChanged = Signal()
    profileSummaryChanged = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._message = ""
        self._garmin_status = ""
        self._divelogs_status = ""
        self._subsurface_status = ""
        self._submersion_status = ""
        self._ssi_status = ""
        self._profile_summary = ""
        self._pending_profile = None
        self._workers = []
        self._model = creds_store.load_credentials_model()
        # tests give the MySSI test login a fake transport; the app uses the defaults
        self._ssi_client_kwargs = {}

    # -- properties -------------------------------------------------------

    @Property(str, notify=messageChanged)
    def message(self) -> str:
        return self._message

    # Every card's status line is always on screen, so testing or saving
    # rewrites a line that is already there instead of adding one and pushing
    # the rest of the page down. With no test result to show, each one says
    # what is stored for that service.

    def _stored_accounts(self, service: str):
        return {"garmin": self._model.get_garmin_accounts, "divelogs": self._model.get_divelogs_accounts,
                "subsurface": self._model.get_subsurface_accounts}[service]()

    def _stored_account(self, service: str, name: str):
        return next((a for a in self._stored_accounts(service) if creds_store.account_name(a) == name), None)

    def _accounts_status(self, service: str) -> str:
        names = [creds_store.account_name(a) for a in self._stored_accounts(service)]
        names = [n for n in names if n]
        if not names:
            return "No account saved yet."
        missing = [n for n in names if not self._stored_account(service, n).password]
        if missing:
            return f"No password stored for {', '.join(missing)} - enter it and save."
        if len(names) == 1:
            return f"Saved: {names[0]}. Press Test to check the login."
        return f"{len(names)} accounts saved. Press Test to check a login."

    @Property(str, notify=garminStatusChanged)
    def garminStatus(self) -> str:
        return self._garmin_status or self._accounts_status("garmin")

    @Property(str, notify=divelogsStatusChanged)
    def divelogsStatus(self) -> str:
        return self._divelogs_status or self._accounts_status("divelogs")

    @Property(str, notify=subsurfaceStatusChanged)
    def subsurfaceStatus(self) -> str:
        return self._subsurface_status or self._accounts_status("subsurface")

    @Property(str, notify=submersionStatusChanged)
    def submersionStatus(self) -> str:
        if self._submersion_status:
            return self._submersion_status
        store = self._model.submersion
        if not store.configured:
            return "Not configured yet."
        if store.store_type == "folder":
            return f"Saved: folder {store.folder_path}. Press Test to check it."
        return f"Saved: bucket '{store.bucket}' at {store.endpoint_url}. Press Test to check it."

    @Property(str, notify=profileSummaryChanged)
    def profileSummary(self) -> str:
        return self._profile_summary

    @Property(bool, notify=credentialsChanged)
    def hasCredentials(self) -> bool:
        return creds_store.has_any_credentials()

    @Property("QVariantList", notify=credentialsChanged)
    def garminAccounts(self):
        """Usernames and token dirs only - never the password, same as the
        status page's account rows (rework.md E7). has_password says whether
        one is stored at all, so a half-saved account is visible here rather
        than only when a sync fails to log in."""
        return [{"username": a.username, "token_dir": a.token_dir, "has_password": bool(a.password)}
                for a in self._model.get_garmin_accounts()]

    @Property("QVariantList", notify=credentialsChanged)
    def divelogsAccounts(self):
        return [{"username": a.username, "has_password": bool(a.password)}
                for a in self._model.get_divelogs_accounts()]

    @Slot("QVariantList")
    def saveGarminAccounts(self, rows) -> None:
        # A blank password keeps the stored one - _save_accounts() merges it.
        from src.core.config import GarminCredentials
        accounts = [
            GarminCredentials(username=str(r.get("username", "")).strip(), password=str(r.get("password", "")),
                              token_dir=str(r.get("token_dir", "")).strip())
            for r in rows if str(r.get("username", "")).strip()
        ]
        creds_store._save_accounts("garmin", accounts)
        self._saved()
        # Drop the old test result: the status line falls back to describing
        # what is now stored, which is what the user just changed.
        self._set("_garmin_status", "", self.garminStatusChanged)

    @Property("QVariantList", notify=credentialsChanged)
    def subsurfaceAccounts(self):
        """Emails only, like garminAccounts (``username`` so the settings
        page's account rows need no per-service case)."""
        return [{"username": a.email, "has_password": bool(a.password)}
                for a in self._model.get_subsurface_accounts()]

    @Slot("QVariantList")
    def saveSubsurfaceAccounts(self, rows) -> None:
        from src.core.config import SubsurfaceCredentials
        base_url = self._model.first_subsurface_account().base_url
        accounts = [
            SubsurfaceCredentials(email=str(r.get("username", "")).strip(), password=str(r.get("password", "")),
                                  base_url=base_url)
            for r in rows if str(r.get("username", "")).strip()
        ]
        creds_store._save_accounts("subsurface", accounts)
        self._saved()
        self._set("_subsurface_status", "", self.subsurfaceStatusChanged)

    @Slot("QVariantList")
    def saveDivelogsAccounts(self, rows) -> None:
        from src.core.config import DivelogsCredentials
        accounts = [
            DivelogsCredentials(username=str(r.get("username", "")).strip(), password=str(r.get("password", "")))
            for r in rows if str(r.get("username", "")).strip()
        ]
        creds_store._save_accounts("divelogs", accounts)
        self._saved()
        self._set("_divelogs_status", "", self.divelogsStatusChanged)

    @Property(str, notify=credentialsChanged)
    def submersionStoreType(self) -> str:
        return self._model.submersion.store_type

    @Property(str, notify=credentialsChanged)
    def submersionEndpointUrl(self) -> str:
        return self._model.submersion.endpoint_url

    @Property(str, notify=credentialsChanged)
    def submersionRegion(self) -> str:
        return self._model.submersion.region

    @Property(bool, notify=credentialsChanged)
    def submersionRegionIsAuto(self) -> bool:
        """No override saved - the region comes from the endpoint, so the
        settings page can keep its Advanced section folded away."""
        return not self._model.submersion.region

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

    # -- Shearwater app (rework.md Track H): a database path, not a login ----

    @Property("QVariantList", notify=credentialsChanged)
    def shearwaterAccounts(self):
        """The saved databases as {account, database, exists}; one row per
        Shearwater account, like the other services' account rows."""
        return [{"account": a.name, "database": a.database, "exists": a.configured}
                for a in self._model.saved_shearwater_accounts()]

    @Property(str, notify=credentialsChanged)
    def shearwaterStatus(self) -> str:
        """What a sync can pick: the saved databases that exist, or the app's
        active account when none is saved, or why there is nothing."""
        saved = self._model.saved_shearwater_accounts()
        usable = self._model.get_shearwater_accounts()
        if saved:
            missing = [a.name for a in saved if not a.configured]
            text = f"{len(usable)} account(s) ready" if usable else "No saved database exists"
            return text + (f"; missing: {', '.join(missing)}" if missing else "") + "."
        if usable:
            return f"Nothing saved: using the app's active account on this computer, {usable[0].name} ({usable[0].database})."
        return "The Shearwater app is not installed here, or has no account signed in - press Detect, or enter the path of a copy of its dive_data.db."

    @Slot(result="QVariantList")
    def detectShearwater(self):
        """Every account the app has on this computer, as {account,
        database}, the active one first - the Detect button adds the ones
        not listed yet."""
        from src.core.services.shearwater import find_live_databases
        found = [{"account": account, "database": path} for account, path in find_live_databases()]
        self._set("_message", f"Found {len(found)} Shearwater app account(s) on this computer." if found
                  else "No Shearwater app database found on this computer.", self.messageChanged)
        return found

    @Slot("QVariantList")
    def saveShearwaterAccounts(self, rows) -> None:
        from src.core.config import ShearwaterCredentials
        accounts = [ShearwaterCredentials(database=str(r.get("database", "")).strip(), account=str(r.get("account", "")).strip())
                    for r in rows if str(r.get("database", "")).strip()]
        creds_store.save_shearwater_accounts(accounts)
        self._saved()

    # -- MySSI (plans/convert.md I7/I8): the Convert page's upload login ----
    #
    # One login, not an account list: MySSI is no sync service, so it is kept
    # apart from CredentialsModel (desktop/credentials.py's ssi functions) and
    # never materialised to credentials.json. The whole card is hidden behind
    # `ssi.UPLOAD_ENABLED` (off since 2026-10-02, until the owner's live
    # test); the slots refuse through `_ssi_allowed` while it is off.

    @Property(bool, constant=True)
    def ssiEnabled(self) -> bool:
        """Whether the MySSI card is shown at all (`ssi.UPLOAD_ENABLED`)."""
        from src.core.services import ssi
        return bool(ssi.UPLOAD_ENABLED)

    def _ssi_allowed(self, action: str) -> bool:
        """False, with one log line, when the SSI upload is switched off."""
        if self.ssiEnabled:
            return True
        logger.info("Send to SSI is switched off (ssi.UPLOAD_ENABLED): %s ignored", action)
        return False

    @Property(str, notify=credentialsChanged)
    def ssiEmail(self) -> str:
        return creds_store.load_ssi_credentials().email

    @Property(bool, notify=credentialsChanged)
    def ssiHasPassword(self) -> bool:
        return bool(creds_store.load_ssi_credentials().password)

    @Property(str, notify=ssiStatusChanged)
    def ssiStatus(self) -> str:
        if self._ssi_status:
            return self._ssi_status
        login = creds_store.load_ssi_credentials()
        if not login.email:
            return "No MySSI login saved yet. Used by the Convert page's Send to SSI only."
        if not login.password:
            return f"No password stored for {login.email} - enter it and save."
        return f"Saved: {login.email}. Press Test to check the login."

    @Slot(str, str)
    def saveSsi(self, email: str, password: str) -> None:
        """Store the MySSI login; a blank password keeps the stored one, a
        blank email removes the login."""
        if not self._ssi_allowed("saveSsi"):
            return
        creds_store.save_ssi_credentials(email, password)
        self._saved()
        self._set("_ssi_status", "", self.ssiStatusChanged)

    @Slot()
    def clearSsi(self) -> None:
        if not self._ssi_allowed("clearSsi"):
            return
        creds_store.clear_ssi_credentials()
        self._saved()
        self._set("_ssi_status", "", self.ssiStatusChanged)

    @Slot(str, str)
    def testSsi(self, email: str, password: str) -> None:
        """Log in to MySSI (off the GUI thread); the token it gets is not kept
        - only a save does that. The route is unofficial (ssi.UNOFFICIAL_NOTE)."""
        if not self._ssi_allowed("testSsi"):
            return
        stored = creds_store.load_ssi_credentials()
        email = (email or "").strip() or stored.email
        password = password or (stored.password if stored.email == email else "")
        if not email or not password:
            self._set("_ssi_status", "Enter an email and password first.", self.ssiStatusChanged)
            return
        self._set("_ssi_status", "Testing…", self.ssiStatusChanged)

        def work():
            from src.core.services.ssi import SsiAdapter
            return "Login OK." if SsiAdapter(email, password, **self._ssi_client_kwargs).login() else "Login failed."
        self._run(work, lambda text: self._set("_ssi_status", str(text), self.ssiStatusChanged))

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

    @Slot(str, str)
    def testGarmin(self, username: str, password: str) -> None:
        stored = next((a for a in self._model.get_garmin_accounts() if a.username == username), None)
        password = password or (stored.password if stored else "")
        # Not user-configurable (rework.md decision 2026-09-22): the token
        # directory is internal token-staging plumbing, not a real setting.
        token_dir = layout.garmin_token_dir(username, stored.token_dir if stored else "")
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
        stored = self._stored_account("subsurface", email)
        password = password or (stored.password if stored else "")
        base_url = self._model.first_subsurface_account().base_url
        if not email or not password:
            self._set("_subsurface_status", "Enter an email and password first.", self.subsurfaceStatusChanged)
            return
        self._set("_subsurface_status", "Testing…", self.subsurfaceStatusChanged)

        def work():
            from src.core.services.subsurface_cloud import check_cloud_login
            ok, message = check_cloud_login(email, password, base_url)
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

    def _saved(self) -> None:
        self._model = creds_store.load_credentials_model()
        self.credentialsChanged.emit()
        self._set("_message", "Saved to keychain.", self.messageChanged)

    @Slot(str, str)
    def saveSubsurface(self, email: str, password: str) -> None:
        """Add or update one Subsurface Cloud account, keeping the others."""
        rows = [{"username": a.email, "password": ""} for a in self._model.get_subsurface_accounts()]
        email = email.strip()
        if email:
            row = next((r for r in rows if r["username"] == email), None)
            if row is None:
                rows.append({"username": email, "password": password})
            else:
                row["password"] = password
        self.saveSubsurfaceAccounts(rows)

    @Slot(str, str, str, str, str, str, str, bool, str, str)
    def saveSubmersion(self, store_type: str, endpoint_url: str, region: str, bucket: str, prefix: str,
                       access_key_id: str, secret_access_key: str, path_style: bool, folder_path: str,
                       passphrase: str = "") -> None:
        from src.core.config import SubmersionCredentials
        creds_store.save_submersion_credentials(SubmersionCredentials(
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
        ))
        self._saved()
        self._set("_submersion_status", "", self.submersionStatusChanged)

    @Slot(str, str, str, str, str, str, str, str, str, bool, str, str)
    def save(self, subsurface_email: str, subsurface_pw: str,
             submersion_store_type: str, submersion_endpoint_url: str, submersion_region: str,
             submersion_bucket: str, submersion_prefix: str, submersion_access_key_id: str,
             submersion_secret_access_key: str, submersion_path_style: bool, submersion_folder_path: str,
             submersion_passphrase: str = "") -> None:
        """Kept for callers that still save both at once. Each card in the
        settings page now has its own Save button, the way the Garmin and
        Divelogs account lists always have (rework.md E7) - none of these
        slots may go through save_credentials_model(), which would replace
        the whole account list with whatever (nothing) it was given."""
        self.saveSubsurface(subsurface_email, subsurface_pw)
        self.saveSubmersion(submersion_store_type, submersion_endpoint_url, submersion_region,
                            submersion_bucket, submersion_prefix, submersion_access_key_id,
                            submersion_secret_access_key, submersion_path_style, submersion_folder_path,
                            submersion_passphrase)

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
