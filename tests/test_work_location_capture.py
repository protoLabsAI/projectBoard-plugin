"""All publication targets are disposable local bare repositories."""

import asyncio
from dataclasses import dataclass
import json
import subprocess
from types import SimpleNamespace

import pytest

from project_board import coder_seam, work_location_capture as capture, work_locations


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.PIPE, text=True).strip()


class Store:
    def __init__(self, comments=None):
        self.comments = comments if comments is not None else []

    def list_work_locations(self, fid):
        return work_locations.project(self.comments)

    def record_work_location(self, fid, state):
        self.comments.append(work_locations.PREFIX + json.dumps(work_locations.record(state, "board")))


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


async def test_push_and_restart_from_card(repos):
    project, external, remote, _ = repos
    store = Store()
    c = capture.Capture(store, "bd-test", project)
    await c.observe({"phase": "end", "locations": [{"path": str(external)}]})
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
    await capture.Capture(store, "bd-test", project).observe({"input": {"cwd": str(external)}})
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
        await capture.Capture(restarted, "bd-test", project).observe({"locations": [entry["path"]]})
        assert latest(restarted)["pushed"] is True


@dataclass
class Coder:
    workdir: str = ""


@pytest.mark.parametrize("outcome", ["cancel", "error", "partial"])
async def test_actual_tapped_seam_captures_before_interruption(repos, outcome):
    project, external, remote, _ = repos
    store = Store()
    c = capture.Capture(store, "bd-test", project)

    async def tapped(coder, prompt, *, on_tool, on_thought, on_text, timeout):
        await on_tool({"id": "exec-1", "phase": "start", "input": json.dumps({"cwd": str(external)})})
        # A commit produced after the last tool event is picked up by the finally drain.
        (external / "evidence").write_text("partial work before interruption")
        git(external, "add", "evidence")
        git(external, "commit", "-m", "partial")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        if outcome == "error":
            raise RuntimeError("provider failed")
        return SimpleNamespace(reply="partial answer", stop_reason="max_tokens")

    token = capture.active.set(c)
    try:
        if outcome == "partial":
            await coder_seam.dispatch_coder_tapped(
                Coder(), str(project), "task", fid="bd-test", gen=1, _dispatch_tapped=tapped
            )
        else:
            with pytest.raises(asyncio.CancelledError if outcome == "cancel" else Exception):
                await coder_seam.dispatch_coder_tapped(
                    Coder(), str(project), "task", fid="bd-test", gen=1, _dispatch_tapped=tapped
                )
    finally:
        capture.active.reset(token)
    entry = latest(Store(list(store.comments)))
    assert entry["sha"] == git(external, "rev-parse", "HEAD")
    assert entry["sha"] == git(remote, "rev-parse", "refs/heads/wip/bd-test")
    assert entry["session_id"] == c.session


async def test_prose_is_not_authority_and_denied_path_is_recorded(repos, monkeypatch):
    project, external, _, _ = repos
    store = Store()
    c = capture.Capture(store, "bd-test", project)
    await c.observe({"input": f"cd {external}; git push origin main", "output": f"commit at {external}"})
    assert not store.comments
    monkeypatch.setattr(capture.sweep, "approved_roots", lambda: [project])
    await c.observe({"locations": [{"path": str(external)}]})
    assert latest(store)["path"] == str(external)
    assert latest(store)["pushed"] is None
    assert latest(store)["last_check"]["status"] == "capture-pending"


async def test_file_evidence_discovers_repo_without_staging_dirt(repos):
    project, external, remote, _ = repos
    (external / "uncommitted.txt").write_text("sole copy")
    store = Store()
    await capture.Capture(store, "bd-test", project).observe(
        {"phase": "update", "locations": [{"path": str(external / "uncommitted.txt")}]}
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
    await capture.Capture(store, "bd-test", project).observe({"input": {"workdir": str(external)}})
    assert latest(store)["pushed"] is False
    hook.unlink()
    real = capture.sweep.git

    def no_readback(path, *args, **kw):
        if "ls-remote" in args:
            raise TimeoutError("readback timed out")
        return real(path, *args, **kw)

    monkeypatch.setattr(capture.sweep, "git", no_readback)
    await capture.Capture(store, "bd-test", project).observe({"input": {"workdir": str(external)}})
    assert latest(store)["pushed"] is False
    assert "remote_ref" not in latest(store)
    assert git(remote, "rev-parse", "wip/bd-test") == latest(store)["sha"]


async def test_drive_scopes_capture_before_hold_and_resets_context(repos, monkeypatch):
    from project_board.loop import BoardLoop

    project, external, _, _ = repos
    store = Store()
    loop = BoardLoop.__new__(BoardLoop)
    monkeypatch.setattr(loop, "_store", lambda: store)
    monkeypatch.setattr(loop, "_repo_for", lambda feature: str(project))

    async def drive(feature):
        assert capture.active.get().fid == feature["id"]

        async def tapped(coder, prompt, *, on_tool, on_thought, on_text, timeout):
            await on_tool({"phase": "end", "input": {"cwd": str(external)}})
            raise asyncio.TimeoutError()

        with pytest.raises(Exception):
            await coder_seam.dispatch_coder_tapped(
                Coder(), str(project), "task", fid=feature["id"], gen=1, _dispatch_tapped=tapped
            )
        # This is the drive's hold/error seam; durable facts already exist.
        assert latest(Store(list(store.comments)))["sha"] == git(external, "rev-parse", "HEAD")

    monkeypatch.setattr(loop, "_drive_captured", drive)
    await loop._drive({"id": "bd-test"})
    assert capture.active.get() is None


async def test_symlink_metadata_and_remote_override_are_not_authority(repos):
    project, external, _, host = repos
    store = Store()
    git(external, "config", "remote.origin.pushurl", "https://user:secret@example.invalid/fork")
    await capture.Capture(store, "bd-test", project).observe({"locations": [str(external)]})
    assert latest(store)["pushed"] is False
    assert "secret" not in json.dumps(store.comments)
    # Narrow read authority to the project: a symlink under it cannot grant access.
    link = project / "outside"
    link.symlink_to(external, target_is_directory=True)
    host.filesystem_projects = [{"path": str(project), "write": True}]
    other = Store()
    await capture.Capture(other, "bd-test", project).observe({"locations": [str(link)]})
    assert latest(other)["sha"] == ""
    assert latest(other)["pushed"] is None


async def test_more_specific_read_only_grant_blocks_push(repos):
    project, external, _, host = repos
    host.filesystem_projects.append({"path": str(external), "write": False})
    store = Store()
    await capture.Capture(store, "bd-test", project).observe({"locations": [str(external)]})
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
    task = asyncio.create_task(capture.Capture(store, "bd-test", project).observe({"locations": [str(external)]}))
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
    await capture.Capture(store, "bd-test", project).observe({"locations": [str(external)]})
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
    await capture.Capture(store, "bd-test", project).observe({"locations": [str(external)]})
    assert len(pushes) == 1
    assert latest(store)["pushed"] is True
    assert "helper" not in json.dumps(store.comments)
