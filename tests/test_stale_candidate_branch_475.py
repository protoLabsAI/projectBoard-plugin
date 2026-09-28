"""A leftover ``feat/<id>.g<n>`` branch never blocks a card terminally (#475).

The incident (bd-fgtf, projectManager board, 2026-09-27): every attempt blocked terminal with

    worktree add failed: Preparing worktree (new branch 'feat/bd-fgtf.g1')
    fatal: a branch named 'feat/bd-fgtf.g1' already exists

five times in one night. Each time the operator deleted the branch (no commits beyond
``origin/main``, not checked out) and unblocked, and the next attempt re-created it, failed,
and left it again. The branch was the symptom. Under it sat ``.worktrees/feat-bd-fgtf.g1``:
a husk of five entries with no ``.git``, left by a removal interrupted half-way.
``unpublished_work`` called it clean, ``worktree remove`` refused it, and ``worktree add``
creates the branch BEFORE it checks the path. So every attempt created the branch, died on
"already exists", and its one retry failed on the branch it had just made, which hid the
real error and left the branch for the next attempt to trip on.

Every test here runs REAL git against a bare origin plus a clone. The defect is what
``git worktree add`` does, in what order, which a faked ``_git`` cannot show.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from project_board import coder_seam, worktree


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _identity(repo: str) -> None:
    _git("-C", repo, "config", "user.email", "board-test@localhost")
    _git("-C", repo, "config", "user.name", "Board Test")
    _git("-C", repo, "config", "commit.gpgsign", "false")


class _Origin:
    """A bare origin and a clone of it, so ``origin/<base>`` resolves as in production."""

    base = "main"

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.origin = str(tmp_path / "origin.git")
        self.seed = str(tmp_path / "seed")
        self.clone = str(tmp_path / "clone")
        _git("init", "--bare", self.origin)
        _git("init", "-b", self.base, self.seed)
        _identity(self.seed)
        Path(self.seed, ".gitignore").write_text(".worktrees/\n")
        Path(self.seed, "README.md").write_text("base\n")
        _git("-C", self.seed, "add", "-A")
        _git("-C", self.seed, "commit", "-m", "base commit")
        _git("-C", self.seed, "remote", "add", "origin", self.origin)
        _git("-C", self.seed, "push", "-u", "origin", self.base)
        _git("-C", self.origin, "symbolic-ref", "HEAD", f"refs/heads/{self.base}")
        _git("clone", self.origin, self.clone)
        _identity(self.clone)
        self.root = os.path.join(self.clone, ".worktrees")

    def sha(self, ref: str) -> str:
        return _git("-C", self.clone, "rev-parse", ref)

    def branches(self, pattern: str = "refs/heads/") -> list[str]:
        out = _git("-C", self.clone, "for-each-ref", "--format=%(refname:short)", pattern)
        return [line for line in out.splitlines() if line]

    def stranded(self, name: str) -> list[str]:
        return self.branches(f"refs/heads/stranded/{name}/")

    def branch_with_commit(self, branch: str, filename: str = "work.txt") -> str:
        """``branch`` off ``origin/main`` holding one commit of its own, and NO worktree —
        the tree it was built in is gone. Returns that commit."""
        tree = str(self.tmp / f"scratch-{branch.replace('/', '-')}")
        _git("-C", self.clone, "worktree", "add", "-b", branch, tree, f"origin/{self.base}")
        Path(tree, filename).write_text("work that exists nowhere else\n")
        _git("-C", tree, "add", "-A")
        _git("-C", tree, "commit", "-m", "the coder's work")
        sha = _git("-C", tree, "rev-parse", "HEAD")
        _git("-C", self.clone, "worktree", "remove", "--force", tree)
        return sha

    def advance_origin(self) -> str:
        """A new commit on ``origin/main`` — so a stale branch is BEHIND the fresh start."""
        Path(self.seed, "README.md").write_text("moved on\n")
        _git("-C", self.seed, "commit", "-am", "base moves on")
        _git("-C", self.seed, "push", "origin", self.base)
        _git("-C", self.clone, "fetch", "origin")
        return self.sha(f"origin/{self.base}")


@pytest.fixture
def origin(tmp_path):
    return _Origin(tmp_path)


# ── a leftover branch at the candidate's name ─────────────────────────────────────────


async def test_a_leftover_empty_branch_is_recreated_off_the_fresh_base(origin):
    stale = origin.sha("origin/main")
    _git("-C", origin.clone, "branch", "feat/bd-x.g1", stale)
    fresh = origin.advance_origin()

    path, branch = await worktree.create_worktree(origin.clone, "main", "bd-x.g1", ".worktrees")

    assert branch == "feat/bd-x.g1"
    assert os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(os.path.join(path, ".git"))
    assert origin.sha(branch) == fresh  # reset onto the fresh start, not left behind at the stale tip
    assert origin.stranded("feat-bd-x.g1") == []  # nothing unique, so nothing to save


async def test_the_bd_fgtf_husk_no_longer_blocks_and_its_files_are_kept(origin):
    """The live shape: a leftover branch AND a husk with no ``.git`` on the candidate path."""
    _git("-C", origin.clone, "branch", "feat/bd-x.g1", "origin/main")
    husk = Path(origin.root, "feat-bd-x.g1")
    (husk / "deck").mkdir(parents=True)
    (husk / "CLAUDE.md").write_text("left by a removal interrupted half-way\n")
    (husk / "deck" / "app.py").write_text("x = 1\n")

    path, branch = await worktree.create_worktree(origin.clone, "main", "bd-x.g1", ".worktrees")

    assert os.path.realpath(path) == os.path.realpath(husk)
    assert Path(path, "README.md").read_text() == "base\n"  # a real checkout now
    assert not Path(path, "CLAUDE.md").exists()
    moved = [n for n in os.listdir(os.path.join(origin.root, ".stranded")) if n.startswith("feat-bd-x.g1-")]
    assert len(moved) == 1  # the husk's bytes were moved aside, not deleted
    assert Path(origin.root, ".stranded", moved[0], "deck", "app.py").read_text() == "x = 1\n"
    assert origin.sha(branch) == origin.sha("origin/main")


async def test_an_empty_husk_directory_is_simply_removed(origin):
    Path(origin.root, "feat-bd-x.g1").mkdir(parents=True)
    _git("-C", origin.clone, "branch", "feat/bd-x.g1", "origin/main")

    await worktree.create_worktree(origin.clone, "main", "bd-x.g1", ".worktrees")

    assert not os.path.exists(os.path.join(origin.root, ".stranded"))


async def test_a_leftover_branch_holding_unique_work_is_saved_then_recreated(origin):
    work = origin.branch_with_commit("feat/bd-x.g1")

    path, branch = await worktree.create_worktree(origin.clone, "main", "bd-x.g1", ".worktrees")

    saved = origin.stranded("feat-bd-x.g1")
    assert len(saved) == 1, saved
    assert origin.sha(saved[0]) == work  # the coder's commit survives, on its own branch
    assert origin.sha(branch) == origin.sha("origin/main")  # and the candidate starts fresh
    assert not Path(path, "work.txt").exists()


async def test_a_branch_checked_out_in_another_live_worktree_is_not_touched(origin):
    elsewhere = str(origin.tmp / "elsewhere")
    _git("-C", origin.clone, "worktree", "add", "-b", "feat/bd-x.g1", elsewhere, "origin/main")
    Path(elsewhere, "in-progress.txt").write_text("someone is working here\n")
    before = origin.sha("feat/bd-x.g1")

    with pytest.raises(worktree.BranchCheckedOutError) as exc:
        await worktree.create_worktree(origin.clone, "main", "bd-x.g1", ".worktrees")

    assert "elsewhere" in str(exc.value)
    assert origin.sha("feat/bd-x.g1") == before
    assert Path(elsewhere, "in-progress.txt").read_text() == "someone is working here\n"
    assert not os.path.exists(os.path.join(origin.root, "feat-bd-x.g1"))


async def test_a_candidate_moves_to_the_next_free_index_when_its_branch_is_held(origin):
    elsewhere = str(origin.tmp / "elsewhere")
    _git("-C", origin.clone, "worktree", "add", "-b", "feat/bd-x.g1", elsewhere, "origin/main")
    adapter = coder_seam._WorktreeSolveAdapter(
        repo=origin.clone,
        base="main",
        root=".worktrees",
        fid="bd-x",
        coder=None,
        dispatch_timeout=None,
        test_cmd="true",
        test_timeout=10,
        verdict_cls=None,
    )

    wt, branch = await adapter._new_candidate_worktree()

    assert branch == "feat/bd-x.g2"
    assert adapter.candidates == [(wt, branch)]
    assert os.path.isdir(elsewhere)  # the held tree is untouched


# ── a failed or reaped candidate leaves no empty branch behind ────────────────────────


async def test_a_failed_worktree_add_leaves_no_branch_and_reports_the_real_error(origin):
    """The recurrence: the add made the branch, then failed on the path. The branch must go,
    and the error must be the path's, not "a branch named … already exists"."""
    os.makedirs(origin.root)
    Path(origin.root, "feat-bd-x.g1").write_text("a file squatting on the tree's path\n")

    with pytest.raises(worktree.WorktreeError) as exc:
        await worktree.create_worktree(origin.clone, "main", "bd-x.g1", ".worktrees")

    assert "already exists" in str(exc.value)
    assert "a branch named" not in str(exc.value)
    assert origin.branches("refs/heads/feat/") == []


async def test_a_failed_ladder_leaves_no_candidate_branch_behind(origin, monkeypatch):
    """``dispatch()``'s failure path: a candidate whose ``create_worktree`` failed never
    reached ``adapter.candidates``, and an older leftover branch has no tree at all. Neither
    survives the ladder's failure."""
    _git("-C", origin.clone, "branch", "feat/bd-x.g7", "origin/main")  # an earlier run's orphan
    os.makedirs(origin.root)
    Path(origin.root, "feat-bd-x.g2").write_text("squats on the second candidate's path\n")

    async def _solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        wt = await generate(task)
        assert os.path.isdir(wt)
        await generate(task)  # g2: its worktree add fails
        raise AssertionError("unreachable")

    async def _tapped(coder, wt, prompt, **_kw):
        return "done"

    monkeypatch.setattr(coder_seam, "dispatch_coder_tapped", _tapped)
    with pytest.raises(worktree.WorktreeError, match="already exists"):
        await coder_seam.dispatch(
            task="t",
            coder=object(),
            repo=origin.clone,
            base="main",
            root=".worktrees",
            fid="bd-x",
            dispatch_timeout=None,
            test_cmd="true",
            test_timeout=10,
            budget=4,
            k=2,
            tree_depth=1,
            _solve=_solve,
            _budget_cls=lambda n: n,
            _verdict_cls=object,
        )

    assert origin.branches("refs/heads/feat/") == []
    assert not os.path.exists(os.path.join(origin.root, "feat-bd-x.g1"))


async def test_the_reap_sweeps_candidate_branches_whose_trees_are_gone(origin):
    _git("-C", origin.clone, "branch", "feat/bd-x.g1", "origin/main")  # empty, no tree
    work = origin.branch_with_commit("feat/bd-x.g2")  # unique work, no tree
    elsewhere = str(origin.tmp / "elsewhere")
    _git("-C", origin.clone, "worktree", "add", "-b", "feat/bd-x.g3", elsewhere, "origin/main")  # live
    _git("-C", origin.clone, "branch", "feat/bd-xy.g1", "origin/main")  # another card's
    _git("-C", origin.clone, "branch", "feat/bd-x", "origin/main")  # the canonical: not a candidate's

    await worktree.reap_feature_worktree(origin.clone, ".worktrees", "bd-x")

    left = origin.branches("refs/heads/feat/")
    assert "feat/bd-x.g1" not in left
    assert "feat/bd-x.g2" not in left
    assert origin.sha(origin.stranded("feat-bd-x.g2")[0]) == work  # saved before it went
    assert "feat/bd-x.g3" in left and os.path.isdir(elsewhere)  # checked out: untouched
    assert "feat/bd-xy.g1" in left


async def test_the_candidate_sweep_can_leave_the_test_rungs_branches_alone(origin):
    _git("-C", origin.clone, "branch", "feat/bd-x.test.g1", "origin/main")
    _git("-C", origin.clone, "branch", "feat/bd-x.g1", "origin/main")

    deleted = await worktree.reap_candidate_branches(origin.clone, "bd-x", test_rung=False)

    assert deleted == ["feat/bd-x.g1"]
    assert origin.branches("refs/heads/feat/") == ["feat/bd-x.test.g1"]


async def test_checked_out_branches_names_every_worktrees_branch(origin):
    elsewhere = str(origin.tmp / "elsewhere")
    _git("-C", origin.clone, "worktree", "add", "-b", "feat/bd-x.g3", elsewhere, "origin/main")

    held = await worktree._checked_out_branches(origin.clone)

    assert os.path.realpath(held["feat/bd-x.g3"]) == os.path.realpath(elsewhere)
    assert os.path.realpath(held["main"]) == os.path.realpath(origin.clone)


async def test_remove_worktree_clears_a_tree_whose_removal_was_interrupted(origin):
    """How the husk is born: git deleted the ``.git`` link and some files, then stopped. The
    next ``worktree remove`` refuses it ('.git' does not exist); the removal must still
    finish, and drop the branch."""
    path, branch = await worktree.create_worktree(origin.clone, "main", "bd-x.g1", ".worktrees")
    os.remove(os.path.join(path, ".git"))

    assert await worktree.remove_worktree(origin.clone, path, branch)

    assert not os.path.exists(path)
    assert origin.branches("refs/heads/feat/") == []
