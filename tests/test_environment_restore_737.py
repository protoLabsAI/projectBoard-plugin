"""Promotion transactions on real git worktrees, with offline installer executables."""

import asyncio
import errno
import os
from pathlib import Path
import shlex
import sys

import pytest

from project_board import worktree
from project_board.loop import BoardLoop
from test_promoted_worktree_environment import git


@pytest.fixture
async def promoted(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(".venv/\nnode_modules/\n.worktrees/\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    src = repo / ".worktrees/candidate"
    git(repo, "worktree", "add", "-b", "candidate", str(src))
    for rel in (".venv", "node_modules"):
        (src / rel).mkdir()
        (src / rel / "original").write_text(rel)
    (src / ".venv/pyvenv.cfg").write_text("home = /original\n")
    (src / "unique.txt").write_text("preserve feature work")
    wt, _ = await worktree.promote_worktree(str(repo), str(src), "candidate", "bd-737")
    return Path(wt)


def assert_transaction(wt, *, pending):
    marker = Path(worktree._environment_marker(str(wt)))
    assert marker.exists() is pending
    assert not list(marker.parent.glob("board-old-environment-*"))
    assert (wt / "unique.txt").read_text() == "preserve feature work"
    if pending:
        for rel in (".venv", "node_modules"):
            assert (wt / rel / "original").read_text() == rel


@pytest.mark.parametrize(
    "cmd, reason",
    [
        ("mkdir node_modules", ".venv"),
        ("mkdir .venv; exit 7", "exited 7"),
        ("mkdir .venv; sleep 30", "timed out"),
        ("ln -s node_modules .venv; exit 8", "exited 8"),
    ],
)
async def test_failure_rolls_back_every_environment(promoted, cmd, reason):
    with pytest.raises(worktree.EnvironmentPreparationError) as error:
        await worktree.restore_worktree_environment(str(promoted), cmd, env=None, timeout=0.1)
    assert reason in str(error.value)
    assert repr(cmd) in str(error.value)
    assert_transaction(promoted, pending=True)
    await worktree.restore_worktree_environment(
        str(promoted), "mkdir .venv node_modules; touch .venv/pyvenv.cfg", env=None, timeout=5
    )
    assert_transaction(promoted, pending=False)
    assert not (promoted / ".venv/original").exists()


async def test_cancellation_rolls_back_and_keeps_marker(promoted):
    task = asyncio.create_task(
        worktree.restore_worktree_environment(
            str(promoted), "mkdir .venv; touch started; sleep 30", env=None, timeout=60
        )
    )
    try:
        async with asyncio.timeout(5):
            while not (promoted / "started").exists():
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert_transaction(promoted, pending=True)


@pytest.mark.parametrize(
    "locks, expected",
    [
        (
            ["uv.lock", "package-lock.json"],
            ["uv sync --frozen --quiet --python 3.12", "npm ci --no-audit --no-fund --prefer-offline"],
        ),
        (
            ["uv.lock", "requirements-dev.txt", "pnpm-lock.yaml", "package-lock.json"],
            [
                "uv sync --frozen --quiet --python 3.12",
                "uv pip install --python .venv/bin/python -r requirements-dev.txt",
                "pnpm install --frozen-lockfile",
            ],
        ),
        (
            ["requirements-dev.txt", "package-lock.json"],
            [
                "uv venv --python 3.12 .venv",
                "uv pip install --python .venv/bin/python -r requirements-dev.txt",
                "npm ci --no-audit --no-fund --prefer-offline",
            ],
        ),
    ],
)
async def test_default_restores_before_board_gate(promoted, tmp_path, monkeypatch, locks, expected):
    for name in locks:
        (promoted / name).touch()
    # Real subprocesses record arguments and create installations without network access.
    bins = tmp_path / "bin"
    bins.mkdir()
    for name in ("uv", "npm", "pnpm"):
        exe = bins / name
        exe.write_text(
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} - \"$@\" <<'PYTHON'\n"
            "import pathlib, sys\n"
            f"name = {name!r}\n"
            'with open("install.log", "a") as out:\n'
            '    out.write(name + " " + " ".join(sys.argv[1:]) + "\\n")\n'
            'target = pathlib.Path(".venv" if name == "uv" else "node_modules")\n'
            "target.mkdir(exist_ok=True)\n"
            'if name == "uv": (target / "pyvenv.cfg").touch()\n'
            "PYTHON\n"
        )
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bins}{os.pathsep}{os.environ['PATH']}")
    loop = BoardLoop({"local_gate_cmd": "touch gate-ran"})
    assert await loop._run_local_gate(str(promoted)) is None
    assert (promoted / "install.log").read_text().splitlines() == expected
    assert (promoted / "gate-ran").exists()
    assert_transaction(promoted, pending=False)


@pytest.mark.parametrize("guard", ["tracked", "root"])
async def test_guards_preserve_original_environments(promoted, guard):
    if guard == "tracked":
        git(promoted, "add", "-f", "node_modules/original")
    else:
        (promoted / "pyvenv.cfg").touch()
    with pytest.raises(worktree.EnvironmentPreparationError, match="tracked files|root itself"):
        await worktree.restore_worktree_environment(str(promoted), "touch setup-ran", env=None, timeout=5)
    assert not (promoted / "setup-ran").exists()
    assert_transaction(promoted, pending=True)


async def test_default_without_supported_manifest_fails_closed(promoted):
    with pytest.raises(worktree.EnvironmentPreparationError) as error:
        await worktree.restore_worktree_environment(str(promoted), "", env=None, timeout=5)
    assert ".venv" in str(error.value)
    assert "node_modules" in str(error.value)
    assert repr(worktree.DEFAULT_SETUP_CMD) in str(error.value)
    assert_transaction(promoted, pending=True)


@pytest.mark.parametrize("failure", ["copy", "remove"])
async def test_cross_filesystem_quarantine_failure_preserves_complete_originals(promoted, monkeypatch, failure):
    # Fail on the second environment so rollback must also restore the first.
    target = promoted / worktree._installed_environments(str(promoted))[-1]
    (target / "nested").mkdir()
    (target / "nested/preserved").write_text("original nested content")
    (target / "link").symlink_to("nested/preserved")
    rename = os.rename
    copytree = worktree.shutil.copytree
    rmtree = worktree.shutil.rmtree
    failed = False

    def cross_device(src, dst, *args, **kwargs):
        if any(Path(p).parent.name.startswith("board-old-environment-") for p in (src, dst)):
            raise OSError(errno.EXDEV, "cross-device move")
        return rename(src, dst, *args, **kwargs)

    def incomplete_copy(src, dst, *args, **kwargs):
        nonlocal failed
        if failure == "copy" and Path(src) == target and not failed:
            failed = True
            Path(dst).mkdir()
            (Path(dst) / "original").write_text("incomplete copy")
            raise worktree.shutil.Error([(str(src), str(dst), "injected copy failure")])
        return copytree(src, dst, *args, **kwargs)

    def partial_removal(path, *args, **kwargs):
        nonlocal failed
        if failure == "remove" and Path(path) == target and not failed:
            failed = True
            (target / "original").unlink()
            rmtree(target / "nested")
            raise OSError("injected removal failure")
        return rmtree(path, *args, **kwargs)

    monkeypatch.setattr(os, "rename", cross_device)
    monkeypatch.setattr(worktree.shutil, "copytree", incomplete_copy)
    monkeypatch.setattr(worktree.shutil, "rmtree", partial_removal)
    with pytest.raises(worktree.EnvironmentPreparationError, match="injected"):
        await worktree.restore_worktree_environment(str(promoted), "touch setup-ran", env=None, timeout=5)
    assert failed
    assert not (promoted / "setup-ran").exists()
    assert_transaction(promoted, pending=True)
    assert (target / "nested/preserved").read_text() == "original nested content"
    assert (target / "link").is_symlink()
    assert os.readlink(target / "link") == "nested/preserved"
    # The pending transaction remains usable after the failure.
    await worktree.restore_worktree_environment(
        str(promoted), "mkdir .venv node_modules; touch .venv/pyvenv.cfg", env=None, timeout=5
    )
    assert_transaction(promoted, pending=False)
