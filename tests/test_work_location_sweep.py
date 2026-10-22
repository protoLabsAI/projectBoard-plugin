"""REAL external seam coverage: read-only probes against a bare origin and clones."""

import asyncio
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from project_board import work_location_sweep as sweep, work_locations
from project_board.loop.drive import DriveMixin


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.DEVNULL, text=True).strip()


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(origin), str(clone)], check=True, capture_output=True)
    git(clone, "config", "user.name", "Test")
    git(clone, "config", "user.email", "test@example.com")
    git(clone, "checkout", "-b", "main")
    git(clone, "commit", "--allow-empty", "-m", "initial")
    git(clone, "push", "-u", "origin", "main")
    return clone, origin


def check(path, roots):
    return sweep.probe({"path": str(path)}, roots, time.monotonic() + 10)


def snapshot(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}


def test_real_ahead_push_divergence_missing_and_no_mutation(repo, tmp_path):
    clone, origin = repo
    assert check(clone, [tmp_path])["pushed"] is True
    git(clone, "commit", "--allow-empty", "-m", "local")
    before = snapshot(tmp_path)
    facts = check(clone, [tmp_path])
    assert facts["ahead"] == 1 and facts["pushed"] is False
    assert snapshot(tmp_path) == before
    git(clone, "push")
    assert check(clone, [tmp_path])["ahead"] == 0
    git(clone, "checkout", "-b", "other", "HEAD~1")
    git(clone, "commit", "--allow-empty", "-m", "diverged")
    git(clone, "push", "--force", "origin", "other:main")
    git(clone, "checkout", "main")
    facts = check(clone, [tmp_path])
    assert (facts["ahead"], facts["behind"]) == (1, 1)
    git(origin, "update-ref", "-d", "refs/heads/main")
    assert check(clone, [tmp_path])["pushed"] is None
    assert check(tmp_path / "missing", [tmp_path])["status"] == "missing"


def test_authorization_and_errors(repo, tmp_path, monkeypatch):
    clone, origin = repo
    assert "authorization required" in check(clone, [tmp_path / "elsewhere"])["error"]
    link = tmp_path / "allowed"
    link.mkdir()
    (link / "escape").symlink_to(clone)
    assert check(link / "escape", [link])["status"] == "unknown"
    monkeypatch.setattr(sweep, "git", lambda *a, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired("git", 2)))
    assert check(clone, [tmp_path])["pushed"] is None


def test_persistence_dedup_recovery_and_identity(repo, tmp_path, monkeypatch):
    clone, _ = repo
    monkeypatch.setattr(sweep, "approved_roots", lambda: [tmp_path])
    entry = work_locations.validate({"id": "clone", "path": str(clone), "purpose": "work", "sha": ""})
    feature = {"id": "bd-1", "blocked": True, "work_locations": {"entries": [entry]}}
    writes, notices = [], []

    def record(fid, state):
        state = work_locations.validate(state)
        writes.append(state)
        feature["work_locations"]["entries"] = [state]

    board = SimpleNamespace(list_features=lambda: [feature], record_work_location=record)
    worker = sweep.Sweep()
    notify = lambda *a, **kw: notices.append((a, kw))
    git(clone, "commit", "--allow-empty", "-m", "local")
    worker.check(board, notify)
    worker.check(board, notify)
    assert len(writes) == len(notices) == 1
    assert writes[-1]["sha"] == ""
    assert "blocked local commit" in str(notices)
    git(clone, "push")
    worker.check(board, notify)
    assert len(notices) == 2 and "risk resolved" in notices[-1][0][1]
    monkeypatch.setattr(sweep, "git", lambda *a, **kw: (_ for _ in ()).throw(PermissionError("denied")))
    worker.check(board, notify)
    assert writes[-1]["pushed"] is None and writes[-1]["sha"] == ""


async def test_slow_worker_single_flight_and_shutdown(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def slow(self, store, notify):
        entered.set()
        release.wait(2)

    monkeypatch.setattr(sweep.Sweep, "check", slow)
    owner = SimpleNamespace(_shutting_down=False, _store=lambda: None, _notify_operator=lambda *a: None)
    DriveMixin._start_location_sweep(owner)
    task = owner._location_sweep_task
    for _ in range(100):
        if entered.is_set():
            break
        await asyncio.sleep(0.001)
    assert entered.is_set()
    DriveMixin._start_location_sweep(owner)
    assert owner._location_sweep_task is task
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done() and owner._location_sweep.stop.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_changed_identity_is_not_pushed(repo, tmp_path):
    clone, _ = repo
    facts = sweep.probe({"path": str(clone), "sha": "a" * 40}, [tmp_path], time.monotonic() + 10)
    assert facts["status"] == "identity-changed" and facts["pushed"] is None


def test_host_roots_respect_fence_and_disabled(monkeypatch, tmp_path):
    host = SimpleNamespace(
        filesystem_enabled=True,
        projects=[{"path": str(tmp_path)}],
        filesystem_projects=[{"path": str(tmp_path / "scratch")}],
    )
    monkeypatch.setattr(sweep.project_registry, "_live_host", lambda: host)
    assert sweep.approved_roots() == [tmp_path / "scratch"]
    host.filesystem_enabled = False
    assert sweep.approved_roots() == []


def test_budget_rotates_and_stops(monkeypatch):
    entries = [{"id": str(i), "path": "/" + str(i), "purpose": "work"} for i in range(25)]
    board = SimpleNamespace(
        list_features=lambda: [{"id": "bd-1", "work_locations": {"entries": entries}}],
        record_work_location=lambda *a: None,
    )
    seen = []

    def probe(entry, roots, deadline):
        seen.append(entry["id"])
        return {"status": "unknown", "pushed": None}

    monkeypatch.setattr(sweep, "probe", probe)
    monkeypatch.setattr(sweep, "approved_roots", lambda: [])
    worker = sweep.Sweep()
    worker.check(board, lambda *a, **kw: None)
    assert len(seen) == 20
    worker.check(board, lambda *a, **kw: None)
    assert seen[20] == "20"
    worker.stop.set()
    worker.check(board, lambda *a, **kw: None)
    assert len(seen) == 40


def test_external_seam_ratchet():
    # Every subprocess entry point in this module is covered by the real-git tier above.
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(sweep))
    seams = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "subprocess"
            for call in ast.walk(node)
        )
    }
    assert seams == {"git"}  # REAL: test_real_ahead_push_divergence_missing_and_no_mutation


def test_real_ledger_evidence_survives_restart(tmp_path, monkeypatch):
    import shutil
    from project_board import store

    if shutil.which(store.BR) is None:
        pytest.skip("real beads CLI required (CI integration tier enforces availability)")
    board = store.BeadsBoard(repo=str(tmp_path), actor="sweep-test")
    fid = board.create_feature("External safety evidence")["id"]
    board.record_work_location(
        fid, {"id": "missing", "path": str(tmp_path / "lost"), "purpose": "preserve", "sha": "b" * 40}
    )
    monkeypatch.setattr(sweep, "approved_roots", lambda: [tmp_path])
    sweep.Sweep().check(board, lambda *a, **kw: None)
    entry = store.BeadsBoard(repo=str(tmp_path)).list_work_locations(fid)["entries"][0]
    assert entry["sha"] == "b" * 40
    assert entry["last_check"]["status"] == "missing"
    assert entry["pushed"] is None


async def test_tick_uses_health_cadence_and_skips_stalled_store(monkeypatch):
    from project_board.loop import BoardLoop

    owner = BoardLoop({})
    starts = []
    monkeypatch.setattr(owner, "_start_location_sweep", lambda: starts.append(True))
    monkeypatch.setattr(owner, "_check_claim_stall", lambda: None)

    async def phase(name, step):
        if name == "health sweep":
            owner._last_sweep += 1
        return ("failed", None)

    monkeypatch.setattr(owner, "_tick_phase", phase)
    await owner._tick()
    assert len(starts) == 1

    async def stalled(name, step):
        owner._last_sweep += 1
        return ("stalled", None)

    monkeypatch.setattr(owner, "_tick_phase", stalled)
    await owner._tick()
    assert len(starts) == 1


def test_pathless_pushed_evidence_is_not_an_external_checkout(monkeypatch):
    entry = work_locations.validate(
        {"id": "remote", "path": "", "purpose": "published", "branch": "main", "pushed": True}
    )
    board = SimpleNamespace(list_features=lambda: [{"id": "bd-1", "work_locations": {"entries": [entry]}}])
    monkeypatch.setattr(sweep, "approved_roots", lambda: [])
    sweep.Sweep().check(board, lambda *a, **kw: pytest.fail("remote evidence must not alert"))


def test_expired_budget_resumes_at_uninspected_location(monkeypatch):
    now = [0.0]
    seen = []
    entries = [{"id": str(i), "path": "/" + str(i), "purpose": "work"} for i in range(10)]
    board = SimpleNamespace(
        list_features=lambda: [{"id": "bd-1", "work_locations": {"entries": entries}}],
        record_work_location=lambda *a: None,
    )

    def probe(entry, roots, deadline):
        seen.append(entry["id"])
        now[0] += 8
        return {"status": "unknown", "pushed": None}

    monkeypatch.setattr(sweep.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(sweep, "probe", probe)
    monkeypatch.setattr(sweep, "approved_roots", lambda: [])
    worker = sweep.Sweep()
    worker.check(board, lambda *a, **kw: None)
    assert seen == ["0", "1"]
    worker.check(board, lambda *a, **kw: None)
    assert seen == ["0", "1", "2", "3"]
