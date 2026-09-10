"""Repo-command children must die as a tree and never read the server's stdin (#423).

REAL processes, no mocks — the defect lives entirely in how the OS treats a process
tree and a pipe, which a fake `create_subprocess_shell` cannot model (a mocked seam is
what let the old shell-only kill look correct). Each command forks a grandchild and
records its pid, so a test can check the grandchild — the process that actually
leaked in production — is gone, not just the shell.

The live incident (2026-09-10): ~15 `pnpm install`s hung across two boards, the oldest
for 19.5h, all orphaned by a shell-only kill and all holding the desktop app's
never-closing stdin pipe. A drive sat silent for 8h because `await proc.wait()` on
Python >= 3.12 waits for the orphan to close its stdout.
"""

from __future__ import annotations

import asyncio
import os
import sys
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


async def _pid_from(pidfile) -> int:
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            return int(pidfile.read_text().strip())
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
