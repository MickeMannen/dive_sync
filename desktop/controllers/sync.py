"""Sync page backend: run a sync or a raw download off the GUI thread and
stream the log (rework.md D2/D3, parity with the Toga B2/B3 sections)."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Property, QObject, Signal, Slot

from desktop import accounts, credentials
from desktop.jobs import LogPump, Worker
from src.core import scheduler
from src.core import config


class SyncController(QObject):
    runningChanged = Signal()
    statusChanged = Signal()
    logLine = Signal(str)
    pairsChanged = Signal()
    accountsChanged = Signal()
    finished = Signal(bool)   # ok

    def __init__(self, log_queue, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._running = False
        self._status = "Idle"
        self._worker: Optional[Worker] = None
        self._pump = LogPump(log_queue, parent=self)
        self._pump.line.connect(self.logLine)
        self._pump.start()

    # -- properties -------------------------------------------------------

    @Property(bool, notify=runningChanged)
    def running(self) -> bool:
        return self._running

    @Property(str, notify=statusChanged)
    def status(self) -> str:
        return self._status

    @Slot()
    def reloadAccounts(self) -> None:
        """Credentials were saved: the account lists and the services they
        make available may have changed."""
        self.accountsChanged.emit()
        self.pairsChanged.emit()

    @Property("QVariantList", notify=accountsChanged)
    def services(self):
        """The services that have credentials, as {id, label} - what the
        Download button and the built-in pair list are built from."""
        model = credentials.load_credentials_model()
        out = []
        if model.get_garmin_accounts():
            out.append({"id": "garmin", "label": "Garmin Connect", "spec": "garmin"})
        if model.get_divelogs_accounts():
            out.append({"id": "divelogs", "label": "Divelogs.org", "spec": "divelogs"})
        if config.SUBMERSION_ENABLED and model.submersion.configured:
            out.append({"id": "submersion", "label": "Submersion", "spec": "submersion"})
        if model.get_subsurface_accounts():
            # The cloud spec; a local checkout is a hand-written pair instead.
            out.append({"id": "subsurface", "label": "Subsurface Cloud", "spec": "subsurface-cloud"})
        return out

    @Property("QVariantList", notify=pairsChanged)
    def pairs(self):
        """Every combination of the configured services, plus whatever pairs
        are saved in settings. The combinations carry their specs rather than
        a pair id, so syncing e.g. Garmin to Submersion needs no hand-written
        entry in settings.json first (the scheduler builds the engine from
        the two specs). Garmin ↔ Divelogs keeps the empty id it has always
        had, so anything that stored that selection still resolves."""
        from src.core.config import ConfigManager
        configured = self.services
        out = []
        for i, source in enumerate(configured):
            for target in configured[i + 1:]:
                builtin_id = "" if (source["id"], target["id"]) == ("garmin", "divelogs") \
                    else f"{source['id']}~{target['id']}"
                out.append({"id": builtin_id, "label": f"{source['label']} ↔ {target['label']}",
                            "source": source["spec"], "target": target["spec"], "builtin": True})
        if not out:
            out.append({"id": "", "label": "Garmin ↔ Divelogs", "source": "garmin",
                        "target": "divelogs", "builtin": True})
        from src.core.config import DEFAULT_PAIR_ID
        for pair in ConfigManager.load_settings().sync_pairs:
            if pair.enabled and pair.id != DEFAULT_PAIR_ID:   # covered by the built-in entry above
                out.append({"id": pair.id, "label": f"{pair.id} ({pair.source} → {pair.target})",
                            "source": pair.source, "target": pair.target, "builtin": False})
        return out

    @Property("QVariantList", notify=pairsChanged)
    def endpoints(self):
        """What the Source and Target boxes offer: every configured service,
        plus the other end of any saved pair (e.g. a UDDF file), as
        {spec, id, label}. ``id`` is the service id, so a Target box can leave
        out the service picked as Source."""
        from src.core.pairs import service_id_of
        out = [{"spec": s["spec"], "id": s["id"], "label": s["label"]} for s in self.services]
        seen = {e["spec"] for e in out}
        for pair in self.pairs:
            for spec in (pair["source"], pair["target"]):
                if spec not in seen:
                    seen.add(spec)
                    try:
                        sid = service_id_of(spec)
                    except ValueError:
                        continue
                    out.append({"spec": spec, "id": sid, "label": spec})
        return out

    @Slot(str, result="QVariantList")
    def targetsFor(self, source_spec: str):
        """The Target box for a Source: every endpoint but that service."""
        source = next((e for e in self.endpoints if e["spec"] == source_spec), None)
        return [e for e in self.endpoints if not source or e["id"] != source["id"]]

    def _pair_between(self, source_spec: str, target_spec: str) -> Optional[dict]:
        """The pair (built-in combination or saved) joining the two, in
        either order: a pair is two services, the run picks the direction."""
        ends = {source_spec, target_spec}
        matches = [p for p in self.pairs if {p["source"], p["target"]} == ends]
        # a saved pair (its own board and options) before the plain combination
        return next((p for p in matches if not p.get("builtin")), matches[0] if matches else None)

    @Slot(bool, str, str, bool, bool, str, str, bool, bool)
    def runSyncBetween(self, dry_run: bool, source_spec: str, target_spec: str, only_new: bool, sync_gases: bool,
                       garmin_username: str = "", divelogs_username: str = "", use_garmin_cache: bool = True,
                       mirror: bool = False) -> None:
        """Sync from ``source_spec`` into ``target_spec``: the run writes the
        target only (rework.md G0). A blank username means the account
        picked on this page (selectedAccount)."""
        from src.core.pairs import service_id_of
        pair = self._pair_between(source_spec, target_spec)
        if not pair or source_spec == target_spec:
            self._set_status("Pick two different services to sync.")
            return
        self.runSync(dry_run, f"to_{service_id_of(target_spec)}", only_new, sync_gases, pair["id"],
                     garmin_username, divelogs_username, use_garmin_cache, mirror)

    @Property("QVariantList", notify=accountsChanged)
    def garminAccounts(self):
        return accounts.names("garmin")

    @Property("QVariantList", notify=accountsChanged)
    def divelogsAccounts(self):
        return accounts.names("divelogs")

    @Property("QVariantList", notify=accountsChanged)
    def subsurfaceAccounts(self):
        return accounts.names("subsurface")

    @Property("QVariantMap", notify=accountsChanged)
    def selectedAccounts(self):
        """{service id: account} picked on this page (rework.md E19)."""
        return accounts.sync_selection()

    @Slot(str, result=str)
    def selectedAccount(self, service: str) -> str:
        """The account this page syncs and downloads ``service`` with
        (rework.md E19); remembered across restarts."""
        return accounts.selected("sync", service)

    @Slot(str, str)
    def setSelectedAccount(self, service: str, account: str) -> None:
        accounts.select("sync", service, account)
        self.accountsChanged.emit()

    @Slot(str, result="QVariantList")
    def directionsFor(self, pair_id: str):
        for pair in self.pairs:
            if pair["id"] == pair_id:
                s, t = pair["source"].split(":")[0], pair["target"].split(":")[0]
                if s == "subsurface-cloud":
                    s = "subsurface"
                if t == "subsurface-cloud":
                    t = "subsurface"
                # rework.md G0: a run writes one side; run the other
                # direction as a second run.
                return [{"value": f"to_{t}", "label": f"To {t}"},
                        {"value": f"to_{s}", "label": f"To {s}"}]
        return [{"value": "to_divelogs", "label": "To divelogs"},
                {"value": "to_garmin", "label": "To garmin"}]

    def _set_status(self, text: str) -> None:
        self._status = text
        self.statusChanged.emit()

    def _set_running(self, on: bool) -> None:
        self._running = on
        self.runningChanged.emit()

    @Slot()
    def stop(self) -> None:
        """Ends the running sync or download at its next dive (progress.Stopped)."""
        if self._running:
            from src.core import progress
            progress.request_stop()
            self._set_status("Stopping after the current dive…")

    def _busy(self) -> bool:
        return self._running or scheduler.is_sync_running or scheduler.is_download_running

    # -- actions ----------------------------------------------------------

    @Slot(bool, str, bool, bool, str, str, str, bool, bool)
    def runSync(self, dry_run: bool, direction: str, only_new: bool, sync_gases: bool, pair_id: str,
                garmin_username: str = "", divelogs_username: str = "", use_garmin_cache: bool = True,
                mirror: bool = False) -> None:
        if self._busy():
            self._set_status("A sync or download is already running.")
            return
        # A run from this page never deletes unless it mirrors: then the
        # target keeps only the dives the source has (SyncEngine mirror).
        custom = {"directionality": direction, "only_new": only_new and not mirror, "sync_gases": sync_gases,
                  "use_garmin_cache": use_garmin_cache, "propagate_deletes": False}
        if mirror:
            custom["mirror"] = True
        selected = next((p for p in self.pairs if p["id"] == pair_id), None)
        if selected and selected.get("builtin"):
            # Built-in combinations have no entry in settings.json, so they
            # travel as their two specs; only a saved pair goes by id.
            if (selected["source"], selected["target"]) != ("garmin", "divelogs"):
                custom["source"], custom["target"] = selected["source"], selected["target"]
        elif pair_id:
            custom["pair"] = pair_id
        # Every account-backed side runs as the account picked on this page,
        # and the pair's history is kept per account combination (E19).
        selection = accounts.sync_selection()
        if garmin_username:
            selection["garmin"] = garmin_username
        if divelogs_username:
            selection["divelogs"] = divelogs_username
        for service, key in (("garmin", "garmin_username"), ("divelogs", "divelogs_username"),
                             ("subsurface", "subsurface_username")):
            if selection.get(service):
                custom[key] = selection[service]
        custom["account_scoped"] = True
        self._start("Running…", lambda: self._run_sync(dry_run, custom),
                    lambda: "Dry run complete." if dry_run else "Sync complete.")

    @Slot(bool, str)
    def download(self, overwrite: bool, service: str) -> None:
        """``service`` is one service id, or "" for every configured one.
        ``overwrite`` re-fetches every Garmin dive instead of reusing the
        unchanged ones (the page's "Use cached Garmin dives" unticked); the
        other services are always downloaded in full."""
        if self._busy():
            self._set_status("A sync or download is already running.")
            return
        services = [service] if service else [s["id"] for s in self.services]
        if not services:
            self._set_status("No service is configured yet - add credentials in Settings.")
            return
        selection = accounts.sync_selection()
        self._start("Downloading…", lambda: self._run_download(overwrite, services, selection),
                    lambda: "Download complete.")

    def _start(self, status: str, target, done_text) -> None:
        self._set_running(True)
        self._set_status(status)
        worker = Worker(target, parent=self)

        def on_ok(result):
            self._pump.drain()
            self._set_running(False)
            error = (result or {}).get("error")
            if (result or {}).get("stopped"):
                self._set_status("Stopped. What was done so far is kept; the next run does the rest.")
                self.finished.emit(False)
            elif error:
                self._set_status(f"Failed: {error}")
                self.finished.emit(False)
            elif result is not None and result.get("success") is False:
                self._set_status("Finished with errors — check the log.")
                self.finished.emit(False)
            else:
                self._set_status(done_text())
                self.finished.emit(True)

        def on_fail(message):
            self._pump.drain()
            self._set_running(False)
            self._set_status(f"Failed: {message}")
            self.finished.emit(False)

        worker.finished_ok.connect(on_ok)
        worker.failed.connect(on_fail)
        self._worker = worker
        worker.start()

    @staticmethod
    def _run_sync(dry_run: bool, custom: dict) -> dict:
        credentials.begin_operation()
        try:
            scheduler.run_sync_thread(dry_run, custom)
        finally:
            credentials.end_operation()
        # last_sync_results is keyed by job id (rework.md A7); a manual
        # desktop run never sets "id", so it always lands under "Manual".
        return dict(scheduler.last_sync_results.get(custom.get("id") or "Manual", {}))

    @staticmethod
    def _run_download(overwrite: bool, services: list, selection: dict = None) -> dict:
        credentials.begin_operation()
        try:
            scheduler.run_download_thread(overwrite, None, services=services, accounts=selection)
        finally:
            credentials.end_operation()
        return dict(scheduler.last_download_results)
