"""``worktree.fix_round_unchanged`` against REAL git (#476).

A fix round resumes the PR branch from ``origin/<branch>``. It changed nothing when HEAD is
still that commit and the tree holds no uncommitted work. The drive treats that as a failed
attempt (tests/test_loop.py); this file proves the question is answered right by git itself:
a bare origin, a clone, a branch pushed and resumed exactly as ``create_worktree(resume=True)``
leaves it.
"""

from __future__ import annotations

import subprocess

import pytest

from project_board import worktree


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _sha(cwd, ref="HEAD"):
    return subprocess.run(["git", "rev-parse", ref], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def resumed(tmp_path):
    """A clone whose feature branch is pushed to a bare origin and checked out from
    ``origin/<branch>`` — the tree a fix round starts in."""
    origin, clone = tmp_path / "origin.git", tmp_path / "clone"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(clone, "config", k, v)
    (clone / "a.py").write_text("x = 1\n")
    _git(clone, "add", "a.py")
    _git(clone, "commit", "-q", "-m", "base")
    _git(clone, "push", "-q", "origin", "main")
    _git(clone, "checkout", "-q", "-b", "feat/bd-1")
    (clone / "a.py").write_text("x = 2\n")
    _git(clone, "commit", "-qam", "the PR's change")
    _git(clone, "push", "-q", "origin", "feat/bd-1")
    _git(clone, "fetch", "-q", "origin", "feat/bd-1")
    return clone


async def test_an_untouched_resumed_branch_is_unchanged(resumed):
    head = _sha(resumed)
    assert await worktree.fix_round_unchanged(str(resumed), "feat/bd-1") == head


async def test_a_new_commit_moves_the_head(resumed):
    (resumed / "a.py").write_text("x = 3\n")
    _git(resumed, "commit", "-qam", "the fix")
    assert await worktree.fix_round_unchanged(str(resumed), "feat/bd-1") == ""


async def test_uncommitted_work_is_not_unchanged(resumed):
    """open_pr commits what the coder left uncommitted, so that IS a change."""
    (resumed / "a.py").write_text("x = 4\n")
    assert await worktree.fix_round_unchanged(str(resumed), "feat/bd-1") == ""
    (resumed / "a.py").write_text("x = 2\n")
    (resumed / "new_test.py").write_text("def test(): pass\n")
    assert await worktree.fix_round_unchanged(str(resumed), "feat/bd-1") == ""


async def test_the_coders_scratch_alone_is_no_change(resumed):
    (resumed / ".proto").mkdir()
    (resumed / ".proto" / "notes.md").write_text("thinking\n")
    assert await worktree.fix_round_unchanged(str(resumed), "feat/bd-1") == _sha(resumed)


async def test_no_remote_branch_or_no_tree_is_no_judgement(resumed, tmp_path):
    assert await worktree.fix_round_unchanged(str(resumed), "feat/never-pushed") == ""
    assert await worktree.fix_round_unchanged(str(tmp_path / "gone"), "feat/bd-1") == ""
