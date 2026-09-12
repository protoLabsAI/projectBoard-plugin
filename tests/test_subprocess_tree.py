"""Repo-command children must die as a tree and never read the server's stdin (#423).

REAL processes, no mocks — the defect lives entirely in how the OS treats a process
tree and a pipe, which a fake `create_subprocess_shell` cannot model (a mocked seam is
what let the old shell-only kill look correct). Each command forks a grandchild and
records its pid, so a test can check the grandchild — the process that actually
leaked in production — is gone, not just the shell.

The live incident (2026-09-10): ~15 `pnpm install`s hung across two boards, the oldest
for 19.5h, all orphaned by a shell-only kill and all holding the desktop app's
never-closing stdin pipe. A drive sat silent for 8h because `await proc.wait()` on
Python >= 3.11 waits for the orphan to close its stdout.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field

import pytest

from project_board import worktree
from project_board.coder_seam import _WorktreeSolveAdapter
from project_board.loop import BoardLoop

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")

# `sleep` runs as a background GRANDCHILD of the shell, its pid written where the test
# can find it; `wait` keeps the shell alive on it, the shape of `pnpm install && …`.
_TREE = 'sleep 300 & echo $! > "{pidfile}"; wait'


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, not ours — never the case for our own child
        return True
    return True


async def _gone(pid: int, within: float = 3.0) -> bool:
    """A SIGKILLed orphan is reaped by init asynchronously — poll briefly."""
    for _ in range(int(within / 0.05)):
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return not _alive(pid)


# Every grandchild pid a test learns about, so teardown can kill whatever a FAILING test
# left behind — a regression here would otherwise leak `sleep 300`s for five minutes.
_SPAWNED: list[int] = []


@pytest.fixture(autouse=True)
def _kill_leftover_grandchildren():
    yield
    while _SPAWNED:
        pid = _SPAWNED.pop()
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass


async def _pid_from(pidfile) -> int:
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            pid = int(pidfile.read_text().strip())
            _SPAWNED.append(pid)
            return pid
        await asyncio.sleep(0.05)
    raise AssertionError("the command never recorded its grandchild's pid")


@pytest.fixture
def server_stdin_that_never_closes():
    """Put fd 0 on a pipe nobody ever writes to or closes — the desktop app's stdin into
    its sidecar servers, which every repo-command child used to inherit."""
    r, w = os.pipe()
    saved = os.dup(0)
    os.dup2(r, 0)
    try:
        yield
    finally:
        os.dup2(saved, 0)
        for fd in (saved, r, w):
            os.close(fd)


def _gate_loop(cmd: str, timeout: float) -> BoardLoop:
    loop = BoardLoop({"coder": "proto"})
    loop.local_gate_cmd = cmd
    loop.local_gate_timeout = timeout
    return loop


async def test_a_timed_out_gate_leaves_no_survivors(tmp_path):
    """A shell-only kill orphaned every `pnpm install` behind a timed-out gate."""
    pidfile = tmp_path / "grandchild.pid"
    loop = _gate_loop(_TREE.format(pidfile=pidfile), timeout=0.5)

    result = await asyncio.wait_for(loop._run_local_gate(str(tmp_path)), timeout=15)

    assert result is None  # a timed-out gate still fails OPEN — unchanged
    assert await _gone(await _pid_from(pidfile)), "the gate's grandchild outlived its timeout"


async def test_a_timed_out_acceptance_test_returns_instead_of_hanging_the_drive(tmp_path):
    """The 8-hour silence: kill the shell, then `await proc.wait()` — which waits for the
    orphaned grandchild to close the stdout pipe it inherited. It never did."""

    @dataclass
    class _Verdict:
        passed: bool
        total: int = 0
        failed: int = 0
        failing: list = field(default_factory=list)
        output: str = ""

    pidfile = tmp_path / "grandchild.pid"
    adapter = _WorktreeSolveAdapter(
        repo=str(tmp_path),
        base="main",
        root=".worktrees",
        fid="bd-1",
        coder=object(),
        dispatch_timeout=None,
        test_cmd=_TREE.format(pidfile=pidfile),
        test_timeout=0.5,
        verdict_cls=_Verdict,
    )

    verdict = await asyncio.wait_for(adapter._run_acceptance_tests(str(tmp_path)), timeout=15)

    assert verdict.passed is False and "timed out" in verdict.output
    assert await _gone(await _pid_from(pidfile)), "the test runner's grandchild outlived its timeout"


async def test_a_cancelled_gate_takes_its_tree_down(tmp_path):
    """A drive cancel or a shutdown mid-gate used to leave the whole tree running."""
    pidfile = tmp_path / "grandchild.pid"
    loop = _gate_loop(_TREE.format(pidfile=pidfile), timeout=300)

    task = asyncio.create_task(loop._run_local_gate(str(tmp_path)))
    pid = await _pid_from(pidfile)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=15)

    assert await _gone(pid), "a cancelled gate left its grandchild running"


@pytest.mark.usefixtures("server_stdin_that_never_closes")
async def test_a_gate_that_reads_stdin_gets_eof_not_the_servers_pipe(tmp_path):
    """Inherited, the server's stdin blocks a reading child forever (and would hand it
    bytes meant for the server). With no stdin, the read sees EOF at once."""
    loop = _gate_loop('if read line; then echo "GOT:$line"; else echo EOF; fi; exit 3', timeout=5)

    result = await asyncio.wait_for(loop._run_local_gate(str(tmp_path)), timeout=15)

    # A clean non-zero exit returns its output. Blocked on the pipe instead, the gate
    # would have timed out and failed open to None.
    assert result is not None and result.strip() == "EOF"


async def test_a_timed_out_git_commit_takes_its_hook_tree_down(tmp_path):
    """`git commit` runs the repo's hooks — a shell tree like any gate (husky → pnpm →
    lint-staged). Killing only `git` on a timeout orphaned the hook's children.
    (Stdin is not the risk here: git already gives a pre-commit hook /dev/null.)"""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "t@t"), ("config", "user.name", "t")):
        rc, _out, err = await worktree._git(str(repo), *args)
        assert rc == 0, err
    pidfile = tmp_path / "hook-grandchild.pid"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\n" + _TREE.format(pidfile=pidfile) + "\n")
    hook.chmod(0o755)

    with pytest.raises(worktree.WorktreeError, match="timed out"):
        await asyncio.wait_for(
            worktree._git(str(repo), "commit", "--allow-empty", "-q", "-m", "x", timeout=1), timeout=15
        )

    assert await _gone(await _pid_from(pidfile)), "the commit hook's grandchild outlived git's timeout"


async def test_communicate_or_kill_passes_a_normal_exit_through(tmp_path):
    """The helper is invisible on the happy path: output and exit code as before."""
    proc = await worktree.spawn_shell(
        "echo out; exit 4", cwd=str(tmp_path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await worktree.communicate_or_kill(proc, timeout=10)
    assert out.decode().strip() == "out" and proc.returncode == 4


async def test_a_timed_out_preflight_leaves_no_survivors(tmp_path):
    """The preflight smokes the gate in the OPERATOR'S base checkout — the worst place to
    leave an orphaned install behind."""
    pidfile = tmp_path / "grandchild.pid"
    loop = BoardLoop({"coder": "proto"})
    loop.preflight_timeout = 0.5

    await asyncio.wait_for(loop._preflight("p", _TREE.format(pidfile=pidfile), str(tmp_path)), timeout=15)

    assert await _gone(await _pid_from(pidfile)), "the preflight gate's grandchild outlived its timeout"


async def test_a_timed_out_fixups_command_is_killed_at_all(tmp_path, monkeypatch):
    """The fixups timeout used to kill NOTHING: `wait_for` raised, `except Exception`
    swallowed it, and the formatter kept running in a worktree about to be PR'd."""
    monkeypatch.setattr("project_board.loop.drive._FIXUPS_TIMEOUT_S", 0.5)
    pidfile = tmp_path / "grandchild.pid"
    loop = BoardLoop({"coder": "proto"})
    loop.format_cmd = _TREE.format(pidfile=pidfile)

    await asyncio.wait_for(loop._run_fixups(str(tmp_path)), timeout=15)

    assert await _gone(await _pid_from(pidfile)), "the fixups command's grandchild outlived its timeout"


async def test_a_timed_out_gh_takes_its_tree_down(tmp_path, monkeypatch):
    """`gh` shells out (auth helpers, extensions, a pager) — a timeout that kills only `gh`
    orphans whatever it started."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pidfile = tmp_path / "grandchild.pid"
    fake_gh = bin_dir / "gh"
    fake_gh.write_text("#!/bin/sh\n" + _TREE.format(pidfile=pidfile) + "\n")
    fake_gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    with pytest.raises(worktree.WorktreeError, match="timed out"):
        await asyncio.wait_for(worktree._gh("pr", "list", cwd=str(tmp_path), timeout=1), timeout=15)

    assert await _gone(await _pid_from(pidfile)), "gh's grandchild outlived its timeout"


async def test_communicate_or_kill_reaps_the_tree_before_it_raises(tmp_path):
    """The reap is part of the contract: a caller that removes the worktree right after a
    timeout must not race a shell that is still being torn down."""
    proc = await worktree.spawn_shell(
        "sleep 300; echo done", cwd=str(tmp_path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    with pytest.raises(asyncio.TimeoutError):
        await worktree.communicate_or_kill(proc, timeout=0.5)
    assert proc.returncode is not None, "communicate_or_kill raised before the killed shell was reaped"


async def test_a_descendant_that_escapes_the_group_cannot_hang_the_caller(tmp_path, monkeypatch):
    """The one case the reap BOUND exists for: a descendant that re-`setsid`s survives the
    group kill and keeps our stdout pipe open. The caller must get its timeout anyway."""
    monkeypatch.setattr(worktree, "_REAP_TIMEOUT_S", 1.0)
    pidfile = tmp_path / "escaped.pid"
    escape = f"{sys.executable} -c 'import os, time; os.setsid(); time.sleep(300)' & echo $! > \"{pidfile}\"; wait"
    proc = await worktree.spawn_shell(
        escape, cwd=str(tmp_path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    pid = await _pid_from(pidfile)

    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(worktree.communicate_or_kill(proc, timeout=0.5), timeout=10)
    # The outer wait_for would raise TimeoutError too — only the elapsed time proves the
    # reap's own bound (1s here) cut the wait, rather than the test's safety net (10s).
    assert time.monotonic() - started < 5, "the reap bound did not cap the wait on the escaped pipe"
    assert _alive(pid), "the escaped descendant should have survived the group kill — the premise of this test"


async def test_a_hung_worktree_install_is_killed_as_a_tree(tmp_path):
    """`setup_cmd` is an install — the exact command (`pnpm install`) that hung for 19.5h
    in #424. Its timeout must take the whole tree down and still return, not raise."""
    pidfile = tmp_path / "grandchild.pid"

    reason = await asyncio.wait_for(
        worktree.prepare_worktree(str(tmp_path), _TREE.format(pidfile=pidfile), env=None, timeout=0.5), timeout=15
    )

    assert "timed out" in reason
    assert await _gone(await _pid_from(pidfile)), "the install's grandchild outlived its timeout"


async def test_a_worktree_install_never_reads_the_servers_stdin(tmp_path, server_stdin_that_never_closes):
    """An install that prompts (`npx` asking to fetch a package) must see EOF, not block on
    the desktop app's pipe until the timeout."""
    reason = await asyncio.wait_for(
        worktree.prepare_worktree(str(tmp_path), "cat > seen.txt", env=None, timeout=10), timeout=15
    )
    assert reason == ""
    assert (tmp_path / "seen.txt").read_text() == ""
