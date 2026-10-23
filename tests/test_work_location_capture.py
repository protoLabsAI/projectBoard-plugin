"""All publication targets are disposable local bare repositories.

Two seams are exercised: ``observe`` (the coder's tool stream) only ACCUMULATES paths
in memory, and ``drain`` (the drive's result/interruption seam) probes, persists and
publishes. The drive drains at each dispatch boundary, with an outer finally as a safety net.
"""

import asyncio
from dataclasses import dataclass
import json
import subprocess
from types import SimpleNamespace

import pytest

from project_board import coder_seam, work_location_capture as capture, work_locations, worktree


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.PIPE, text=True).strip()


class Store:
    def __init__(self, comments=None):
        self.comments = comments if comments is not None else []

    def list_work_locations(self, fid):
        return work_locations.project(self.comments)

    def record_work_location(self, fid, state):
        self.comments.append(work_locations.PREFIX + json.dumps(work_locations.record(state, "board")))


async def run(c, event, root=""):
    """One progress event followed by the drive's result-seam drain."""
    await c.observe(event, root)
    await c.drain()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    origin = tmp_path / "remote.git"
    origin.mkdir()
    git(origin, "init", "--bare")
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-b", "main")
    git(project, "config", "user.name", "Test")
    git(project, "config", "user.email", "test@example.invalid")
    (project / "seed").write_text("seed")
    git(project, "add", "seed")
    git(project, "commit", "-m", "seed")
    git(project, "remote", "add", "origin", str(origin))
    git(project, "push", "origin", "main")
    external = tmp_path / "external"
    git(tmp_path, "clone", "-b", "main", str(origin), str(external))
    git(external, "config", "user.name", "Test")
    git(external, "config", "user.email", "test@example.invalid")
    git(external, "switch", "-c", "evidence")
    (external / "evidence").write_text("result")
    git(external, "add", "evidence")
    git(external, "commit", "-m", "evidence")
    host = SimpleNamespace(filesystem_enabled=True, filesystem_projects=[{"path": str(tmp_path), "write": True}])
    monkeypatch.setattr(capture.project_registry, "_live_host", lambda: host)
    return project, external, origin, host


def latest(store):
    return store.list_work_locations("bd-test")["entries"][0]


def drive_loop(store, project, monkeypatch):
    from project_board.loop import BoardLoop

    loop = BoardLoop.__new__(BoardLoop)
    monkeypatch.setattr(loop, "_store", lambda: store)
    monkeypatch.setattr(loop, "_repo_for", lambda feature: str(project))
    return loop


async def test_push_and_restart_from_card(repos):
    project, external, remote, _ = repos
    store = Store()
    c = capture.Capture(store, "bd-test", project)
    await run(c, {"phase": "end", "locations": [{"path": str(external)}]})
    entry = latest(Store(list(store.comments)))  # no capture object / monitor memory
    assert entry["pushed"] is True
    assert entry["sha"] == git(remote, "rev-parse", "refs/heads/wip/bd-test")
    assert entry["branch"] == "evidence"
    assert entry["path"] == str(external)
    assert git(remote, "rev-parse", "main") != entry["sha"]
    assert any("publication-pending" in comment for comment in store.comments)
    assert git(external, "status", "--porcelain") == ""


@pytest.mark.parametrize("failure", ["permission", "network", "non-fast-forward", "fork"])
async def test_refused_publication_keeps_exact_recovery(repos, monkeypatch, failure):
    project, external, remote, host = repos
    if failure == "permission":
        host.filesystem_projects[0]["write"] = False
    elif failure == "network":
        real = capture.sweep.git

        def denied(path, *args, **kw):
            if "push" in args:
                raise PermissionError("secret credentials must not be recorded")
            return real(path, *args, **kw)

        monkeypatch.setattr(capture.sweep, "git", denied)
    elif failure == "non-fast-forward":
        (project / "other").write_text("competing")
        git(project, "add", "other")
        git(project, "commit", "-m", "competing")
        git(project, "push", "origin", "HEAD:refs/heads/wip/bd-test")
    else:
        git(external, "remote", "set-url", "origin", str(remote.parent / "fork.git"))
    store = Store()
    await run(capture.Capture(store, "bd-test", project), {"input": {"cwd": str(external)}})
    restarted = Store(list(store.comments))
    entry = latest(restarted)
    assert entry["pushed"] is False
    assert entry["sha"] == git(external, "rev-parse", "HEAD")
    assert entry["last_check"]["status"] == "publication-blocked"
    assert entry["sha"] in entry["last_check"]["next_step"]
    assert str(external) in entry["last_check"]["next_step"]
    assert "secret" not in json.dumps(entry)
    assert "remote_ref" not in entry
    assert external.exists()
    # A fresh dispatch can retry from only the recorded path once authority is restored.
    if failure == "permission":
        host.filesystem_projects[0]["write"] = True
        await run(capture.Capture(restarted, "bd-test", project), {"locations": [entry["path"]]})
        assert latest(restarted)["pushed"] is True


@dataclass
class Coder:
    workdir: str = ""


async def _dispatch(loop, fid, tapped):
    async def drive_captured(feature):
        return await loop._dispatch_captured(
            coder_seam.dispatch_coder_tapped(
                Coder(), str(loop._repo_for(feature)), "task", fid=feature["id"], gen=1, _dispatch_tapped=tapped
            )
        )

    loop._drive_captured = drive_captured
    return await loop._drive({"id": fid})


@pytest.mark.parametrize("outcome", ["cancel", "error", "partial"])
async def test_tapped_seam_drains_through_drive_on_interruption(repos, monkeypatch, outcome):
    project, external, remote, _ = repos
    store = Store()
    loop = drive_loop(store, project, monkeypatch)

    async def tapped(coder, prompt, *, on_tool, on_thought, on_text, timeout):
        await on_tool({"id": "exec-1", "phase": "start", "input": json.dumps({"cwd": str(external)})})
        # A commit produced after the last tool event is picked up by the drive's drain.
        (external / "evidence").write_text("partial work before interruption")
        git(external, "add", "evidence")
        git(external, "commit", "-m", "partial")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        if outcome == "error":
            raise RuntimeError("provider failed")
        return SimpleNamespace(reply="partial answer", stop_reason="max_tokens")

    if outcome == "partial":
        assert await _dispatch(loop, "bd-test", tapped) == "partial answer"
    else:
        with pytest.raises(asyncio.CancelledError if outcome == "cancel" else worktree.WorktreeError):
            await _dispatch(loop, "bd-test", tapped)
    entry = latest(Store(list(store.comments)))  # recovered from the card alone
    assert entry["sha"] == git(external, "rev-parse", "HEAD")
    assert entry["sha"] == git(remote, "rev-parse", "refs/heads/wip/bd-test")
    assert entry["session_id"]
    assert capture.active.get() is None


async def test_drive_accumulates_during_stream_then_drains_at_result_seam(repos, monkeypatch):
    project, external, remote, _ = repos
    store = Store()
    loop = drive_loop(store, project, monkeypatch)

    async def tapped(coder, prompt, *, on_tool, on_thought, on_text, timeout):
        await on_tool({"phase": "end", "input": {"cwd": str(external)}})
        raise asyncio.TimeoutError()

    async def drive_captured(feature):
        assert capture.active.get().fid == feature["id"]
        with pytest.raises(worktree.CoderTimeout):
            await loop._dispatch_captured(
                coder_seam.dispatch_coder_tapped(
                    Coder(), str(project), "task", fid=feature["id"], gen=1, _dispatch_tapped=tapped
                )
            )
        # Evidence is already durable before timeout handling can hold or retry.
        assert latest(Store(list(store.comments)))["pushed"] is True

    monkeypatch.setattr(loop, "_drive_captured", drive_captured)
    await loop._drive({"id": "bd-test"})
    # The outer safety net preserves the dispatch-boundary verdict.
    entry = latest(Store(list(store.comments)))
    assert entry["sha"] == git(external, "rev-parse", "HEAD")
    assert entry["sha"] == git(remote, "rev-parse", "refs/heads/wip/bd-test")
    assert capture.active.get() is None


@pytest.mark.parametrize("boom", ["list", "probe", "record"])
async def test_capture_failure_never_breaks_the_drive(repos, monkeypatch, boom):
    project, external, _, _ = repos
    store = Store()
    loop = drive_loop(store, project, monkeypatch)
    if boom == "list":
        monkeypatch.setattr(
            store, "list_work_locations", lambda *a, **k: (_ for _ in ()).throw(ValueError("ledger boom"))
        )
    elif boom == "probe":
        # A PermissionError the way a denied Path.exists/resolve raises mid-probe.
        monkeypatch.setattr(capture.sweep, "probe", lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")))
    else:
        # A validate ValueError (e.g. an over-4096-byte last_check) surfaces as a store raise.
        monkeypatch.setattr(store, "record_work_location", lambda *a, **k: (_ for _ in ()).throw(ValueError("too big")))

    async def tapped(coder, prompt, *, on_tool, on_thought, on_text, timeout):
        await on_tool({"input": {"cwd": str(external)}})
        return SimpleNamespace(reply="clean result")

    # The healthy build's reply survives; the capture explosion is swallowed and logged.
    assert await _dispatch(loop, "bd-test", tapped) == "clean result"
    assert capture.active.get() is None


async def test_prose_is_not_authority_and_denied_path_is_recorded(repos, monkeypatch):
    project, external, _, _ = repos
    store = Store()
    c = capture.Capture(store, "bd-test", project)
    await run(c, {"input": f"cd {external}; git push origin main", "output": f"commit at {external}"})
    assert not store.comments
    monkeypatch.setattr(capture.sweep, "approved_roots", lambda: [project])
    await run(c, {"locations": [{"path": str(external)}]})
    assert latest(store)["path"] == str(external)
    assert latest(store)["pushed"] is None
    assert latest(store)["last_check"]["status"] == "capture-pending"


async def test_file_evidence_discovers_repo_without_staging_dirt(repos):
    project, external, remote, _ = repos
    (external / "uncommitted.txt").write_text("sole copy")
    store = Store()
    await run(
        capture.Capture(store, "bd-test", project),
        {"phase": "update", "locations": [{"path": str(external / "uncommitted.txt")}]},
    )
    entries = store.list_work_locations("bd-test")["entries"]
    assert {e["path"] for e in entries} == {str(external), str(external / "uncommitted.txt")}
    assert git(external, "status", "--porcelain") == "?? uncommitted.txt"
    assert git(remote, "rev-parse", "wip/bd-test") == git(external, "rev-parse", "HEAD")


async def test_bare_remote_refusal_and_unverified_push(repos, monkeypatch):
    project, external, remote, _ = repos
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    store = Store()
    await run(capture.Capture(store, "bd-test", project), {"input": {"workdir": str(external)}})
    assert latest(store)["pushed"] is False
    hook.unlink()
    real = capture.sweep.git

    def no_readback(path, *args, **kw):
        if "ls-remote" in args:
            raise TimeoutError("readback timed out")
        return real(path, *args, **kw)

    monkeypatch.setattr(capture.sweep, "git", no_readback)
    await run(capture.Capture(store, "bd-test", project), {"input": {"workdir": str(external)}})
    assert latest(store)["pushed"] is False
    assert "remote_ref" not in latest(store)
    assert git(remote, "rev-parse", "wip/bd-test") == latest(store)["sha"]


def test_internal_prefixes_cover_worktree_and_cut_from_checkout():
    wt = "/home/me/proj/.worktrees/feat-x"
    prefixes = capture._internal_prefixes(wt)
    assert wt in prefixes
    assert "/home/me/proj" in prefixes  # the checkout a .worktrees tree was cut from
    c = capture.Capture(Store(), "bd-test", "/home/me/proj")
    c.internal |= prefixes
    assert c._is_internal("/home/me/proj/src/app.py")
    assert c._is_internal("/home/me/proj/.worktrees/feat-x/app.py")
    assert not c._is_internal("/home/me/other/app.py")


async def test_project_main_checkout_is_never_captured_as_external(repos):
    project, external, remote, _ = repos
    (project / ".worktrees").mkdir()
    store = Store()
    c = capture.Capture(store, "bd-test", project)
    # A coder reads the project's own main checkout and a board worktree by absolute path,
    # alongside genuinely external evidence — only the external clone may be captured.
    await c.observe(
        {
            "locations": [
                {"path": str(project / "seed")},
                {"path": str(project / ".worktrees" / "feat-x" / "app.py")},
                {"path": str(external)},
            ]
        },
        root=str(project / ".worktrees" / "feat-x"),
    )
    await c.drain()
    assert {e["path"] for e in store.list_work_locations("bd-test")["entries"]} == {str(external)}
    wip = git(remote, "rev-parse", "refs/heads/wip/bd-test")
    assert wip == git(external, "rev-parse", "HEAD")
    assert wip != git(project, "rev-parse", "HEAD")  # the project's own HEAD was never pushed


async def test_observe_touches_no_git_or_store_until_drain(repos, monkeypatch):
    project, external, _, _ = repos
    store = Store()
    calls = {"git": 0, "list": 0, "record": 0}

    def counted(name, real):
        def wrapper(*a, **k):
            calls[name] += 1
            return real(*a, **k)

        return wrapper

    monkeypatch.setattr(capture.sweep, "git", counted("git", capture.sweep.git))
    monkeypatch.setattr(store, "list_work_locations", counted("list", store.list_work_locations))
    monkeypatch.setattr(store, "record_work_location", counted("record", store.record_work_location))
    c = capture.Capture(store, "bd-test", project)
    # Many repeated tool events — no ledger read, no git probe, no push on the hot path.
    for _ in range(20):
        await c.observe({"input": {"cwd": str(external)}, "locations": [{"path": str(external)}]})
    assert calls == {"git": 0, "list": 0, "record": 0}
    await c.drain()
    assert calls["list"] == 1 and calls["git"] > 0 and calls["record"] > 0


async def test_symlink_metadata_and_remote_override_are_not_authority(repos):
    project, external, _, host = repos
    store = Store()
    git(external, "config", "remote.origin.pushurl", "https://user:secret@example.invalid/fork")
    await run(capture.Capture(store, "bd-test", project), {"locations": [str(external)]})
    assert latest(store)["pushed"] is False
    assert "secret" not in json.dumps(store.comments)
    # Authority is resolve-based, never lexical: a symlink OUTSIDE the project that points
    # into an unauthorized tree cannot borrow the project's read grant.
    link = external.parent / "symlink-outside"
    link.symlink_to(external, target_is_directory=True)
    host.filesystem_projects = [{"path": str(project), "write": True}]
    other = Store()
    await run(capture.Capture(other, "bd-test", project), {"locations": [str(link)]})
    assert latest(other)["sha"] == ""
    assert latest(other)["pushed"] is None


async def test_more_specific_read_only_grant_blocks_push(repos):
    project, external, _, host = repos
    host.filesystem_projects.append({"path": str(external), "write": False})
    store = Store()
    await run(capture.Capture(store, "bd-test", project), {"locations": [str(external)]})
    assert latest(store)["sha"] == git(external, "rev-parse", "HEAD")
    assert latest(store)["pushed"] is False


async def test_cancellation_waits_for_durable_record(repos, monkeypatch):
    import threading

    project, external, _, _ = repos
    store = Store()
    started, release = threading.Event(), threading.Event()
    real = store.record_work_location

    def record(fid, state):
        started.set()
        assert release.wait(5)
        return real(fid, state)

    monkeypatch.setattr(store, "record_work_location", record)
    c = capture.Capture(store, "bd-test", project)
    await c.observe({"locations": [str(external)]})
    task = asyncio.create_task(c.drain())
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert latest(Store(list(store.comments)))["sha"] == git(external, "rev-parse", "HEAD")


async def test_later_sweep_preserves_publication_recovery_step(repos):
    import time

    project, external, _, host = repos
    host.filesystem_projects[0]["write"] = False
    store = Store()
    await run(capture.Capture(store, "bd-test", project), {"locations": [str(external)]})
    entry = latest(store)
    step = entry["last_check"]["next_step"]
    capture.sweep.Sweep()._check_entry(
        store,
        lambda *a, **kw: None,
        {"id": "bd-test"},
        entry,
        capture.sweep.approved_roots(),
        time.monotonic() + 10,
        {},
    )
    assert latest(Store(list(store.comments)))["last_check"]["next_step"] == step


async def test_transport_uses_project_config_and_external_objects(repos, monkeypatch):
    project, external, _, _ = repos
    git(project, "config", "credential.https://example.invalid.helper", "!printf trusted-helper")
    git(external, "config", "credential.https://example.invalid.helper", "!printf untrusted-helper")
    assert capture.project_credentials(project, "https://example.invalid/repo") == ["!printf trusted-helper"]
    real = capture.sweep.git
    pushes = []

    def checked(path, *args, **kw):
        if "push" in args:
            assert path == project
            assert kw["alternate_objects"] == external / ".git" / "objects"
            pushes.append(args)
        return real(path, *args, **kw)

    monkeypatch.setattr(capture.sweep, "git", checked)
    store = Store()
    await run(capture.Capture(store, "bd-test", project), {"locations": [str(external)]})
    assert len(pushes) == 1
    assert latest(store)["pushed"] is True
    assert "helper" not in json.dumps(store.comments)


@pytest.mark.parametrize("tracking", [False, True])
async def test_published_ref_survives_restart_and_sweep(repos, tracking):
    import time

    project, external, remote, _ = repos
    if tracking:
        git(external, "branch", "--set-upstream-to=origin/main")
    store = Store()
    await run(capture.Capture(store, "bd-test", project), {"locations": [str(external)]})
    restarted = Store(list(store.comments))
    alerts = []
    checker = capture.sweep.Sweep()
    cache = {}
    for _ in range(2):
        checker._check_entry(
            restarted,
            lambda *a, **kw: alerts.append(a),
            {"id": "bd-test"},
            latest(restarted),
            capture.sweep.approved_roots(),
            time.monotonic() + 10,
            cache,
        )
    assert latest(restarted)["pushed"] is True
    assert latest(restarted)["last_check"]["status"] == "verified"
    assert latest(restarted)["last_check"]["risks"] == []
    assert not alerts
    # Same checkout/identity, different publication target: no cache verdict reuse.
    other = dict(latest(restarted), id="other-ref", remote_ref="origin/main")
    checker._check_entry(
        restarted,
        lambda *a, **kw: alerts.append(a),
        {"id": "bd-test"},
        other,
        capture.sweep.approved_roots(),
        time.monotonic() + 10,
        cache,
    )
    entries = restarted.list_work_locations("bd-test")["entries"]
    assert next(e for e in entries if e["id"] == "other-ref")["pushed"] is False
    assert alerts


async def test_each_attempt_persists_before_next_attempt_or_hold(repos, monkeypatch):
    project, external, remote, _ = repos
    store = Store()
    loop = drive_loop(store, project, monkeypatch)
    later_started, release = asyncio.Event(), asyncio.Event()

    async def tapped(coder, prompt, *, on_tool, on_thought, on_text, timeout):
        await on_tool({"locations": [str(external)]})
        assert not store.comments  # still no I/O on the tool callback
        return SimpleNamespace(reply="first attempt")

    async def later():
        # Simulate a retry that advances the same external checkout immediately.
        later_started.set()
        await release.wait()
        (external / "evidence").write_text("second attempt")
        git(external, "add", "evidence")
        git(external, "commit", "-m", "second attempt")
        return "second attempt"

    async def drive_captured(feature):
        await loop._dispatch_captured(
            coder_seam.dispatch_coder_tapped(
                Coder(), str(project), "task", fid=feature["id"], gen=1, _dispatch_tapped=tapped
            )
        )
        await loop._dispatch_captured(later())
        # This is the hold boundary, still inside the multi-attempt drive.
        assert latest(Store(list(store.comments)))["sha"] == git(external, "rev-parse", "HEAD")
        assert latest(store)["pushed"] is True

    monkeypatch.setattr(loop, "_drive_captured", drive_captured)
    task = asyncio.create_task(loop._drive({"id": "bd-test"}))
    try:
        await asyncio.wait_for(later_started.wait(), 10)
        # Restart recovery while attempt two is running; the outer finally has not run.
        entry = latest(Store(list(store.comments)))
        assert entry["sha"] == git(remote, "rev-parse", "refs/heads/wip/bd-test")
        assert entry["pushed"] is True
    finally:
        release.set()
        await task
    assert latest(store)["sha"] != entry["sha"]
