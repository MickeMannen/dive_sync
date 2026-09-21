"""Subsurface Cloud access (seed of rework.md F6).

Subsurface Cloud is a plain git repository served over HTTPS
(``core/qthelper.cpp::getCloudURL`` in the Subsurface source):

    <base_url>git/<email>[<email>]

where the part before ``[`` is the repository URL and the bracketed part is
the branch name, and ``core/git-access.cpp`` authenticates with HTTP basic
auth using the account email and password. The generic host
``cloud.subsurface-divelog.org`` fronts the regional mirrors.

``SubsurfaceCloudAdapter`` (step F6) wraps the git-storage
``SubsurfaceAdapter`` around a local clone kept under
``DATA_DIR/subsurface_cloud/<account>``: every run starts by fetching and
hard-resetting the clone to the remote branch (Subsurface Cloud is a shared
history, so local state is never trusted), writes go into the checkout, and
``finish()`` commits and pushes once. A push rejected because someone else
pushed in between is retried once by resetting to the new remote state and
replaying this run's writes. Nothing is ever force-pushed.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime
from typing import List, Optional, Tuple

import requests

from src.core.models import UnifiedDive
from src.core.services.subsurface import SubsurfaceAdapter

logger = logging.getLogger("dive_sync.subsurface_cloud")

DEFAULT_BASE_URL = "https://cloud.subsurface-divelog.org/"


def repo_url(email: str, base_url: str = DEFAULT_BASE_URL) -> str:
    base = base_url if base_url.endswith("/") else base_url + "/"
    return f"{base}git/{email.strip()}"


def branch_name(email: str) -> str:
    return email.strip()


def check_cloud_login(email: str, password: str, base_url: str = DEFAULT_BASE_URL,
                      timeout: float = 20.0) -> Tuple[bool, str]:
    """Ask the cloud git server for the repository's refs with basic auth.
    200 with a git-upload-pack advertisement means the credentials work;
    401/403 means they do not; anything else is reported as is. Read-only."""
    if not email or not password:
        return False, "Email and password are required."
    url = f"{repo_url(email, base_url)}/info/refs"
    try:
        response = requests.get(url, params={"service": "git-upload-pack"},
                                auth=(email.strip(), password), timeout=timeout,
                                headers={"User-Agent": "dive_sync"})
    except requests.RequestException as e:
        return False, f"Could not reach Subsurface Cloud: {e}"
    if response.status_code in (401, 403):
        return False, "Subsurface Cloud rejected the email/password (verify the account is confirmed in Subsurface first)."
    if response.status_code == 404:
        return False, "Subsurface Cloud has no repository for this email yet; open Subsurface once with cloud storage enabled."
    if response.status_code != 200:
        return False, f"Unexpected reply from Subsurface Cloud (HTTP {response.status_code})."
    if b"git-upload-pack" not in response.content[:200]:
        return False, "The server replied, but not like a git repository; check base_url."
    return True, f"Subsurface Cloud login OK (repository {repo_url(email, base_url)}, branch '{branch_name(email)}')."


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

def safe_account_dir(email: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.@-]", "_", email.strip()) or "account"


def default_clone_dir(email: str) -> str:
    return os.path.join(os.environ.get("DATA_DIR", "."), "subsurface_cloud", safe_account_dir(email))


class SubsurfaceCloudAdapter(SubsurfaceAdapter):
    """Subsurface Cloud through a local clone (see module docstring)."""

    display_name = "Subsurface Cloud"

    def __init__(self, email: str, password: str, base_url: str = DEFAULT_BASE_URL,
                 clone_dir: Optional[str] = None, clone_url: Optional[str] = None,
                 site_match_radius_m: float = 200.0):
        self.email = email.strip()
        self.password = password
        self.base_url = base_url or DEFAULT_BASE_URL
        self.url = clone_url or repo_url(self.email, self.base_url)
        self.branch = branch_name(self.email)
        self.clone_dir = clone_dir or default_clone_dir(self.email)
        super().__init__(self.clone_dir, site_match_radius_m=site_match_radius_m)
        self._journal: List[Tuple[str, Optional[str], Optional[UnifiedDive]]] = []
        self._replaying = False

    # -- git plumbing ------------------------------------------------------

    def _auth(self) -> dict:
        return {"username": self.email, "password": self.password}

    def _open(self):
        from dulwich.repo import Repo
        return Repo(self.clone_dir)

    def _ref(self) -> bytes:
        return f"refs/heads/{self.branch}".encode("utf-8")

    def _clone(self) -> None:
        from dulwich import porcelain
        os.makedirs(os.path.dirname(self.clone_dir), exist_ok=True)
        logger.info("Subsurface Cloud: cloning %s (branch %s) into %s", self.url, self.branch, self.clone_dir)
        porcelain.clone(self.url, self.clone_dir, branch=self.branch.encode("utf-8"), checkout=True, **self._auth())

    def _refresh(self) -> None:
        """Fetch and hard-reset the checkout to the remote branch; drop
        untracked leftovers so a failed earlier run cannot be re-read as dives."""
        from dulwich import porcelain
        repo = self._open()
        try:
            result = porcelain.fetch(repo, self.url, **self._auth())
            sha = result.refs.get(self._ref())
            if sha is None:
                raise RuntimeError(f"Subsurface Cloud has no branch {self.branch!r} at {self.url}")
            repo.refs[self._ref()] = sha
            repo.refs.set_symbolic_ref(b"HEAD", self._ref())
            porcelain.reset(repo, "hard", sha)
            status = porcelain.status(repo)
            for path in status.untracked:
                full = os.path.join(self.clone_dir, path if isinstance(path, str) else path.decode("utf-8"))
                if os.path.isdir(full):
                    shutil.rmtree(full, ignore_errors=True)
                elif os.path.exists(full):
                    os.remove(full)
        finally:
            repo.close()
        logger.info("Subsurface Cloud: checkout at %s is at remote %s", self.clone_dir, sha[:10].decode())

    def _has_changes(self) -> bool:
        from dulwich import porcelain
        repo = self._open()
        try:
            status = porcelain.status(repo)
            return bool(status.untracked or any(status.unstaged) or any(status.staged.values()))
        finally:
            repo.close()

    def _commit_and_push(self, message: str) -> None:
        from dulwich import porcelain
        repo = self._open()
        try:
            status = porcelain.status(repo)
            if status.untracked:
                porcelain.add(repo, [os.path.join(self.clone_dir, p if isinstance(p, str) else p.decode("utf-8"))
                                     for p in status.untracked])
            porcelain.commit(repo, message=message.encode("utf-8"),
                             author=b"dive_sync <dive_sync@localhost>", committer=b"dive_sync <dive_sync@localhost>",
                             all=True)
            refspec = f"refs/heads/{self.branch}:refs/heads/{self.branch}".encode("utf-8")
            result = porcelain.push(repo, self.url, refspecs=[refspec], **self._auth())
            errors = {k: v for k, v in (getattr(result, "ref_status", {}) or {}).items() if v}
            if errors:
                raise RuntimeError(f"push rejected: {errors}")
        finally:
            repo.close()

    # -- adapter interface -------------------------------------------------

    def login(self) -> bool:
        if not self.email or not self.password:
            logger.error("Subsurface Cloud: email and password are required.")
            return False
        try:
            if not os.path.isdir(os.path.join(self.clone_dir, ".git")):
                if os.path.isdir(self.clone_dir):
                    shutil.rmtree(self.clone_dir)
                self._clone()
            else:
                self._refresh()
        except Exception as e:
            logger.error("Subsurface Cloud: could not get the repository: %s", e)
            return False
        self._journal = []
        return super().login()

    def add_dive(self, dive: UnifiedDive) -> Optional[str]:
        new_id = super().add_dive(dive)
        if new_id and not self._replaying:
            self._journal.append(("add", new_id, dive))
        return new_id

    def update_dive(self, external_id: str, dive: UnifiedDive) -> bool:
        ok = super().update_dive(external_id, dive)
        if ok and not self._replaying:
            self._journal.append(("update", external_id, dive))
        return ok

    def delete_dive(self, external_id: str) -> bool:
        ok = super().delete_dive(external_id)
        if ok and not self._replaying:
            self._journal.append(("delete", external_id, None))
        return ok

    def _replay(self) -> None:
        """Re-apply this run's writes on a freshly reset checkout."""
        self._replaying = True
        try:
            self.repo.load()
            for op, ext_id, dive in self._journal:
                if op == "add" and dive is not None:
                    super().add_dive(dive)
                elif op == "update" and dive is not None:
                    if not super().update_dive(ext_id, dive):
                        logger.warning("Subsurface Cloud: dive %s vanished before the retry; update dropped", ext_id)
                elif op == "delete":
                    super().delete_dive(ext_id)
        finally:
            self._replaying = False

    def finish(self) -> None:
        if not self._journal or not self._has_changes():
            self._journal = []
            return
        adds = sum(1 for op, _, _ in self._journal if op == "add")
        updates = sum(1 for op, _, _ in self._journal if op == "update")
        deletes = sum(1 for op, _, _ in self._journal if op == "delete")
        message = f"dive_sync {datetime.now():%Y-%m-%d %H:%M}: {adds} added, {updates} updated, {deletes} deleted"
        try:
            self._commit_and_push(message)
        except Exception as first:
            logger.warning("Subsurface Cloud: push failed (%s); refreshing and replaying %d change(s)", first, len(self._journal))
            self._refresh()
            self._replay()
            try:
                self._commit_and_push(message + " (retry)")
            except Exception as second:
                self._refresh()
                raise RuntimeError(f"Subsurface Cloud: push failed twice, changes discarded: {second}") from second
        logger.info("Subsurface Cloud: pushed %s", message)
        self._journal = []
