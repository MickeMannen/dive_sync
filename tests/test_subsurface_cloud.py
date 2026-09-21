"""Subsurface Cloud adapter (rework.md F6) against a local bare git
repository standing in for the cloud server."""
import os
import shutil
from datetime import datetime

import pytest
from dulwich import porcelain
from dulwich.repo import Repo

from src.core.models import UnifiedDive
from src.core.services.subsurface_cloud import SubsurfaceCloudAdapter, default_clone_dir, safe_account_dir

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "subsurface_cloud")
EMAIL = "diver@example.com"
pytestmark = pytest.mark.skipif(not os.path.isdir(FIXTURE), reason="subsurface fixture missing")


def _commit_all(path, message):
    repo = Repo(path)
    status = porcelain.status(repo)
    if status.untracked:
        porcelain.add(repo, [os.path.join(path, p if isinstance(p, str) else p.decode()) for p in status.untracked])
    sha = porcelain.commit(repo, message=message.encode(), author=b"t <t@x>", committer=b"t <t@x>", all=True)
    repo.close()
    return sha


@pytest.fixture
def remote(tmp_path):
    """A bare repo whose branch <email> holds the fixture, like Subsurface Cloud."""
    bare = str(tmp_path / "remote.git")
    porcelain.init(bare, bare=True)
    work = str(tmp_path / "seed")
    shutil.copytree(FIXTURE, work)
    porcelain.init(work)
    repo = Repo(work)
    repo.refs.set_symbolic_ref(b"HEAD", f"refs/heads/{EMAIL}".encode())
    repo.close()
    _commit_all(work, "Initial")
    porcelain.push(work, bare, refspecs=[f"refs/heads/{EMAIL}:refs/heads/{EMAIL}".encode()])
    return bare


def _adapter(tmp_path, remote, name="clone"):
    return SubsurfaceCloudAdapter(EMAIL, "pw", clone_dir=str(tmp_path / name), clone_url=remote)


def _remote_head_files(remote):
    repo = Repo(remote)
    sha = repo.refs[f"refs/heads/{EMAIL}".encode()]
    tree = repo[repo[sha].tree]
    names = []

    def walk(tree_obj, prefix=""):
        for item in tree_obj.items():
            path = prefix + item.path.decode()
            obj = repo[item.sha]
            if obj.type_name == b"tree":
                walk(obj, path + "/")
            else:
                names.append(path)
    walk(tree)
    repo.close()
    return names, sha


def test_clone_write_push_and_refresh(tmp_path, remote):
    adapter = _adapter(tmp_path, remote)
    assert adapter.clone_dir.endswith("clone")
    assert adapter.login()
    assert os.path.isdir(os.path.join(adapter.clone_dir, ".git"))
    dives = adapter.fetch_dives()
    assert len(dives) == 8

    new = UnifiedDive(date_time=datetime(2026, 9, 3, 9, 0), duration=1800, max_depth=12.0, dive_number=14,
                      external_ids={"garmin": "42"}, notes="from garmin", location="Reef", lat=34.9, lng=33.7)
    new_id = adapter.add_dive(new)
    existing = dives[-1]
    existing.buddy = "Bo"
    assert adapter.update_dive(existing.external_ids["subsurface"], existing)
    assert adapter.delete_dive(dives[0].external_ids["subsurface"])

    before_files, before_sha = _remote_head_files(remote)
    adapter.finish()
    after_files, after_sha = _remote_head_files(remote)
    assert after_sha != before_sha
    assert f"2026/09/03-Thu-09=00=00/Dive-14" in after_files
    assert "2025/02/08-Sat-11=50=31/Dive-1" not in after_files and "2025/02/08-Sat-11=50=31/Dive-1" in before_files
    assert adapter._journal == []
    # the commit is ours
    repo = Repo(remote)
    commit = repo[after_sha]
    assert commit.author == b"dive_sync <dive_sync@localhost>" and b"1 added, 1 updated, 1 deleted" in commit.message
    repo.close()

    # a fresh clone elsewhere sees the pushed state, and finish() with nothing to do is a no-op
    other = _adapter(tmp_path, remote, "other")
    assert other.login()
    ids = {d.external_ids["subsurface"]: d for d in other.fetch_dives()}
    assert new_id in ids and ids[new_id].external_ids["garmin"] == "42" and len(ids) == 8
    assert ids[existing.external_ids["subsurface"]].buddy == "Bo"
    other.finish()
    assert _remote_head_files(remote)[1] == after_sha


def test_login_refreshes_and_drops_local_leftovers(tmp_path, remote):
    adapter = _adapter(tmp_path, remote)
    assert adapter.login()
    leftover = os.path.join(adapter.clone_dir, "2027", "01", "01-Fri-10=00=00")
    os.makedirs(leftover)
    open(os.path.join(leftover, "Dive-99"), "w").write("duration 10:00 min\n")
    stray = os.path.join(adapter.clone_dir, "2025/02/08-Sat-11=50=31/Dive-1")
    open(stray, "a").write('buddy "local edit"\n')
    # someone else pushes meanwhile
    seed = str(tmp_path / "seed")
    open(os.path.join(seed, "2025/02/08-Sat-11=50=31/Dive-1"), "a").write('suit "remote edit"\n')
    _commit_all(seed, "remote change")
    porcelain.push(seed, remote, refspecs=[f"refs/heads/{EMAIL}:refs/heads/{EMAIL}".encode()])

    assert adapter.login()
    assert not os.path.exists(leftover)
    text = open(stray).read()
    assert "remote edit" in text and "local edit" not in text
    assert len(adapter.fetch_dives()) == 8


def test_rejected_push_is_retried_with_replay(tmp_path, remote, monkeypatch):
    adapter = _adapter(tmp_path, remote)
    assert adapter.login()
    new = UnifiedDive(date_time=datetime(2026, 9, 4, 9, 0), duration=1800, max_depth=12.0, dive_number=15)
    adapter.add_dive(new)

    # the remote moves on after our clone: the first push must be rejected
    seed = str(tmp_path / "seed")
    open(os.path.join(seed, "2025/02/08-Sat-11=50=31/Dive-1"), "a").write('suit "remote edit"\n')
    _commit_all(seed, "remote change")
    porcelain.push(seed, remote, refspecs=[f"refs/heads/{EMAIL}:refs/heads/{EMAIL}".encode()])

    attempts = []
    real = adapter._commit_and_push

    def counting(message):
        attempts.append(message)
        if len(attempts) == 1:
            raise RuntimeError("push rejected: non-fast-forward")
        return real(message)
    monkeypatch.setattr(adapter, "_commit_and_push", counting)

    adapter.finish()
    assert len(attempts) == 2 and attempts[1].endswith("(retry)")
    files, sha = _remote_head_files(remote)
    assert "2026/09/04-Fri-09=00=00/Dive-15" in files
    repo = Repo(remote)
    parent = repo[repo[sha].parents[0]]
    assert parent.message == b"remote change"   # built on top of the other push, not over it
    repo.close()

    # a second failure gives up loudly and leaves the checkout clean
    adapter.add_dive(new.model_copy(update={"date_time": datetime(2026, 9, 5, 9, 0), "dive_number": 16}))
    monkeypatch.setattr(adapter, "_commit_and_push", lambda m: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError, match="failed twice"):
        adapter.finish()
    assert not os.path.exists(os.path.join(adapter.clone_dir, "2026/09/05-Sat-09=00=00"))


def test_login_failures_and_paths(tmp_path):
    assert SubsurfaceCloudAdapter("", "", clone_dir=str(tmp_path / "x")).login() is False
    bad = SubsurfaceCloudAdapter(EMAIL, "pw", clone_dir=str(tmp_path / "y"), clone_url=str(tmp_path / "missing.git"))
    assert bad.login() is False
    assert safe_account_dir("Me+You@Example.com") == "Me_You@Example.com"
    os.environ["DATA_DIR"] = str(tmp_path)
    try:
        assert default_clone_dir(EMAIL) == str(tmp_path / "subsurface_cloud" / EMAIL)
    finally:
        os.environ.pop("DATA_DIR", None)


def test_engine_calls_finish(tmp_path):
    from src.core.sync_engine import SyncEngine
    from tests.test_link_engine import FakeDivelogs, FakeGarmin, _dive, _settings_file

    class Finishing(FakeDivelogs):
        finished = 0

        def finish(self):
            type(self).finished += 1

    path = _settings_file(tmp_path)
    engine = SyncEngine(settings_path=path, credentials_path=str(tmp_path / "c.json"),
                        source_adapter=FakeGarmin([_dive(external_ids={"garmin": "1"})]), target_adapter=Finishing([]))
    engine.run_sync(dry_run=True)
    assert Finishing.finished == 0
    engine.run_sync(dry_run=False)
    assert Finishing.finished == 1


def test_pairs_know_the_cloud_spec(tmp_path):
    from src.core.pairs import build_adapter, parse_service_spec, service_id_of
    from src.core.config import SettingsModel
    assert parse_service_spec("subsurface-cloud") == ("subsurface-cloud", None)
    assert service_id_of("subsurface-cloud") == "subsurface"
    with pytest.raises(ValueError, match="not configured"):
        build_adapter("subsurface-cloud", SettingsModel(), credentials_path=str(tmp_path / "c.json"))
