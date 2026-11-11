"""Real moves and pip-installed entrypoints, through the configured board gate."""

import asyncio
import errno
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from project_board import worktree
from project_board.loop import BoardLoop


SETUP = """import pathlib, subprocess, sys, venv, zipfile
root = pathlib.Path.cwd()
venv.EnvBuilder(with_pip=True).create('.venv')
wheel = root / 'fixture_tool-1.0-py3-none-any.whl'
with zipfile.ZipFile(wheel, 'w') as z:
    files = {
        'fixture_tool-1.0.dist-info/METADATA': 'Metadata-Version: 2.1\\nName: fixture-tool\\nVersion: 1.0\\n',
        'fixture_tool-1.0.dist-info/WHEEL': 'Wheel-Version: 1.0\\nGenerator: test\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n',
        'fixture_tool-1.0.dist-info/entry_points.txt': '[console_scripts]\\nfeature-check = feature:main\\n',
        'feature_source.pth': str(root) + '\\n',
    }
    files['fixture_tool-1.0.dist-info/RECORD'] = ''.join(p + ',,\\n' for p in files)
    for path, content in files.items():
        z.writestr(path, content)
subprocess.run([str(root / '.venv/bin/python'), '-m', 'pip', 'install', '--no-index',
                '--find-links', str(root), '-r', 'requirements.lock'], check=True)
"""


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


@pytest.fixture
async def candidate(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(".venv/\n*.whl\n__pycache__/\n.worktrees/\n")
    (repo / "requirements.lock").write_text("fixture-tool==1.0\n")
    (repo / "setup.py").write_text(SETUP)
    (repo / "feature.py").write_text('def main():\n    print("feature works")\n')
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    src = repo / ".worktrees/candidate"
    git(repo, "worktree", "add", "-b", "candidate", str(src))
    (src / "unique.txt").write_text("unique commit")
    git(src, "add", "unique.txt")
    git(src, "commit", "-m", "unique feature work")
    (src / "dirty.txt").write_text("keep this edit")
    cmd = f"{shlex.quote(sys.executable)} setup.py"
    assert await worktree.prepare_worktree(str(src), cmd, env=None, timeout=60) == ""
    return repo, src, cmd, git(src, "rev-parse", "HEAD")


async def promote(candidate):
    repo, src, cmd, sha = candidate
    wt, branch = await worktree.promote_worktree(str(repo), str(src), "candidate", "bd-env")
    return Path(wt), branch


async def test_installed_entrypoint_and_source_metadata_rebuilt_before_gate(candidate):
    repo, src, cmd, sha = candidate
    wt, branch = await promote(candidate)
    loop = BoardLoop({"setup_cmd": cmd, "local_gate_cmd": ".venv/bin/feature-check"})
    assert await loop._run_local_gate(str(wt)) is None
    assert subprocess.check_output([str(wt / ".venv/bin/feature-check")], text=True).strip() == "feature works"
    assert git(wt, "rev-parse", "HEAD") == sha
    assert (wt / "dirty.txt").read_text() == "keep this edit"
    assert (wt / "requirements.lock").read_bytes() == (repo / "requirements.lock").read_bytes()
    for pattern in ("bin/*", "lib/**/feature_source.pth"):
        for path in (wt / ".venv").glob(pattern):
            if path.is_file() and not path.is_symlink():
                assert str(src).encode() not in path.read_bytes()
    before = (wt / ".venv/bin/feature-check").stat().st_mtime_ns
    assert await worktree.promote_worktree(str(repo), str(wt), branch, "bd-env") == (str(wt), branch)
    assert await loop._run_local_gate(str(wt)) is None
    assert (wt / ".venv/bin/feature-check").stat().st_mtime_ns == before


@pytest.mark.parametrize("failure", ["exit 7", "mkdir .venv; touch .venv/pyvenv.cfg; exit 7", "sleep 30", ""])
async def test_failed_or_interrupted_setup_stays_pending_and_retries(candidate, failure):
    wt, branch = await promote(candidate)
    loop = BoardLoop({"setup_cmd": failure, "local_gate_cmd": "touch gate-ran"})
    loop.setup_timeout = 0.05
    with pytest.raises(worktree.EnvironmentPreparationError):
        await loop._run_local_gate(str(wt))
    assert not (wt / "gate-ran").exists()
    assert Path(worktree._environment_marker(str(wt))).exists()
    assert git(wt, "rev-parse", "HEAD") == candidate[3]
    loop.setup_cmd = candidate[2]
    loop.setup_timeout = 60
    assert await loop._run_local_gate(str(wt)) is None
    assert (wt / "gate-ran").exists()


async def test_cancelled_setup_keeps_pending_transaction(candidate):
    wt, _ = await promote(candidate)
    task = asyncio.create_task(
        worktree.restore_worktree_environment(str(wt), "touch started; sleep 30", env=None, timeout=60)
    )
    while not (wt / "started").exists():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert Path(worktree._environment_marker(str(wt))).exists()
    await worktree.restore_worktree_environment(str(wt), candidate[2], env=None, timeout=60)
    assert subprocess.check_output([str(wt / ".venv/bin/feature-check")], text=True).strip() == "feature works"


async def test_environment_failure_does_not_dispatch_repair(monkeypatch, tmp_path):
    from test_loop_reliability_461_462 import _drive_over_a_reaped_tree

    async def no_budget(*args, **kwargs):
        pytest.fail("environment recovery spent a repair budget")

    monkeypatch.setattr(BoardLoop, "_budget_set", no_budget)

    def gate(tree, real):
        async def fail(wt, feature=None):
            raise worktree.EnvironmentPreparationError("setup_cmd exited 7")

        return fail

    store, dispatches, opened = await _drive_over_a_reaped_tree(monkeypatch, tmp_path, gate=gate)
    blocks = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocks) == 1 and blocks[0][3] == "environment-preparation"
    assert len(dispatches) == 1
    assert not opened
    assert not [c for c in store.calls if c[0] in ("record_attempt", "escalate")]


async def test_crash_salvage_holds_verified_feature_on_setup_failure(candidate):
    from test_loop_reliability_461_462 import _DriveStore

    wt, branch = await promote(candidate)
    feature = {
        "id": "bd-env",
        "title": "",
        "repo": str(candidate[0]),
        "verified_sha": candidate[3],
        "board_state": "in_progress",
    }
    store = _DriveStore()
    store.get_feature = lambda fid: feature
    loop = BoardLoop({"repo": str(candidate[0]), "setup_cmd": "exit 8"})
    assert await loop._salvage_verified_candidate(store, "bd-env") is True
    assert [c[3] for c in store.calls if c[0] == "flag_blocked"] == ["environment-preparation"]
    assert not [c for c in store.calls if c[0] in ("open_review", "clear_verified")]
    assert git(wt, "rev-parse", "HEAD") == candidate[3]


async def test_retry_finishes_interrupted_branch_rename(candidate, monkeypatch):
    real_git = worktree._git

    async def interrupted(repo, *args, **kwargs):
        if args[:2] == ("branch", "-m"):
            raise asyncio.CancelledError()
        return await real_git(repo, *args, **kwargs)

    monkeypatch.setattr(worktree, "_git", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await promote(candidate)
    monkeypatch.setattr(worktree, "_git", real_git)
    wt = candidate[0] / ".worktrees/feat-bd-env"
    await worktree.promote_worktree(str(candidate[0]), str(wt), "candidate", "bd-env")
    assert git(wt, "branch", "--show-current") == "feat/bd-env"
    assert Path(worktree._environment_marker(str(wt))).exists()


async def test_quarantine_across_filesystems_rebuilds_before_gate(candidate, monkeypatch):
    (candidate[1] / ".venv/bin/check-link").symlink_to("feature-check")
    wt, _ = await promote(candidate)
    real_rename = os.rename
    quarantined = []

    def cross_device_rename(src, dst, *args, **kwargs):
        if Path(dst).parent.name.startswith("board-old-environment-"):
            quarantined.append(Path(dst))
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", cross_device_rename)
    loop = BoardLoop({"setup_cmd": "exit 7", "local_gate_cmd": ".venv/bin/feature-check"})
    with pytest.raises(worktree.EnvironmentPreparationError, match="setup_cmd exited 7"):
        await loop._run_local_gate(str(wt))
    assert quarantined
    assert (quarantined[0] / "bin/feature-check").is_file()
    assert (quarantined[0] / "bin/check-link").is_symlink()
    assert (quarantined[0] / "bin/check-link").readlink() == Path("feature-check")
    assert not (wt / ".venv").exists()
    loop.setup_cmd = candidate[2]
    assert await loop._run_local_gate(str(wt)) is None
    assert subprocess.check_output([str(wt / ".venv/bin/feature-check")], text=True).strip() == "feature works"
    assert git(wt, "rev-parse", "HEAD") == candidate[3]
    assert (wt / "dirty.txt").read_text() == "keep this edit"
    assert (wt / "requirements.lock").read_bytes() == (candidate[0] / "requirements.lock").read_bytes()


@pytest.mark.parametrize("legacy_marker", [False, True])
async def test_shared_node_modules_survive_promotion_without_setup(tmp_path, legacy_marker):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text("node_modules/\n.worktrees/\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    src = repo / ".worktrees/candidate"
    git(repo, "worktree", "add", "-b", "candidate", str(src))
    dependencies = ["node_modules", "packages/app/node_modules"]
    for rel in dependencies:
        package = repo / rel
        package.mkdir(parents=True)
        (package / "gate.sh").write_text("echo shared-dependencies-work\n")
    assert worktree.link_node_modules(str(repo), str(src)) == 2
    wt, branch = await worktree.promote_worktree(str(repo), str(src), "candidate", "bd-node")
    marker = Path(worktree._environment_marker(wt))
    if legacy_marker:
        marker.write_text(json.dumps({"environments": dependencies}))
    loop = BoardLoop({"local_gate_cmd": "sh node_modules/gate.sh && sh packages/app/node_modules/gate.sh"})
    assert await loop._run_local_gate(wt) is None
    assert not marker.exists()
    for rel in dependencies:
        assert (Path(wt) / rel).is_symlink()
        assert (Path(wt) / rel).resolve() == repo / rel
    assert await worktree.promote_worktree(str(repo), wt, branch, "bd-node") == (wt, branch)
    assert await loop._run_local_gate(wt) is None


@pytest.mark.parametrize("kind", ["local", "internal_absolute", "relative", "broken"])
def test_path_dependent_node_dependencies_still_require_recovery(tmp_path, kind):
    wt = tmp_path / "tree"
    wt.mkdir()
    dependencies = wt / "node_modules"
    target = wt / "installed"
    target.mkdir()
    if kind == "local":
        dependencies.mkdir()
    elif kind == "internal_absolute":
        dependencies.symlink_to(target, target_is_directory=True)
    elif kind == "relative":
        dependencies.symlink_to("installed", target_is_directory=True)
    else:
        dependencies.symlink_to(tmp_path / "missing", target_is_directory=True)
    assert worktree._installed_environments(str(wt)) == ["node_modules"]
