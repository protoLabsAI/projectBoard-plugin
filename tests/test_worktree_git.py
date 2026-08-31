"""Real local-git integration tier for the 11 credential-free ``worktree.py`` seams
(#361, slice 1).

Every seam here shells ``git`` against an ACTUAL repository — a temporary bare origin
plus a clone, so ``origin/<base>`` and a pushed PR branch resolve exactly as they do in
production. NOTHING stubs ``_git`` for the covered functions: unlike ``test_worktree.py``
(which fakes ``_git``/``_gh`` to pin the error-path branching), this tier validates what
real git actually does with the plugin's payloads. The two tiers are complementary — the
mocked one keeps the cheap error-path coverage, this one closes the "a mock can't prove
the binary accepts it" gap the external-seam ratchet exists to burn down.

The fixture is hermetic: no network, no GitHub, no global git config dependency (every
identity is pinned repo-local), and nothing escapes ``tmp_path`` — pytest disposes the
whole origin+clone tree per test, so no worktree or branch leaks.

``create_worktree(..., resume=True)`` is characterized against real refs because #337
showed that mocked branch-from-base behavior hid a rebuild loop: resume must start from
the PR branch when it exists, and fall back to the LATEST base — not a stale/arbitrary
ref — when that branch is gone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from project_board import worktree


def _git_run(*args):
    """Run git for FIXTURE setup (never under test) — raises on failure, returns stdout."""
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _git_rc(*args):
    """Run git as a boolean probe inside a test — returns the exit code, never raises."""
    return subprocess.run(["git", *args], capture_output=True, text=True).returncode


def _configure_identity(repo):
    """Pin a repo-LOCAL identity so commits neither depend on nor leak into global config."""
    _git_run("-C", str(repo), "config", "user.email", "board-test@localhost")
    _git_run("-C", str(repo), "config", "user.name", "Board Test")
    _git_run("-C", str(repo), "config", "commit.gpgsign", "false")


class _Origin:
    """A temporary bare origin + a clone, wired so ``origin/<base>`` resolves exactly as in
    production. All identities are repo-local, so the fixture depends on no global git config
    and leaks nothing outside ``tmp_path``."""

    base = "main"

    def __init__(self, tmp_path):
        self.origin = str(tmp_path / "origin.git")
        self.seed = str(tmp_path / "seed")
        self.clone = str(tmp_path / "clone")
        _git_run("init", "--bare", self.origin)
        _git_run("init", "-b", self.base, self.seed)
        _configure_identity(self.seed)
        Path(self.seed, "README.md").write_text("base\n")
        _git_run("-C", self.seed, "add", "-A")
        _git_run("-C", self.seed, "commit", "-m", "base commit")
        _git_run("-C", self.seed, "remote", "add", "origin", self.origin)
        _git_run("-C", self.seed, "push", "-u", "origin", self.base)
        _git_run("clone", self.origin, self.clone)
        _configure_identity(self.clone)

    def base_sha(self):
        return _git_run("-C", self.seed, "rev-parse", self.base)

    def advance_base(self, filename="advance.txt", text="more\n"):
        """Add a commit to ``base`` on origin (base moves under the clone) and return its sha."""
        Path(self.seed, filename).write_text(text)
        _git_run("-C", self.seed, "add", "-A")
        _git_run("-C", self.seed, "commit", "-m", "advance base")
        _git_run("-C", self.seed, "push", "origin", self.base)
        return self.base_sha()

    def push_feature(self, branch, filename="feature.txt", text="feature\n"):
        """Push a feature branch (off the CURRENT base) to origin and return its tip sha."""
        _git_run("-C", self.seed, "checkout", "-B", branch, self.base)
        Path(self.seed, filename).write_text(text)
        _git_run("-C", self.seed, "add", "-A")
        _git_run("-C", self.seed, "commit", "-m", "feature commit")
        _git_run("-C", self.seed, "push", "origin", branch)
        sha = _git_run("-C", self.seed, "rev-parse", "HEAD")
        _git_run("-C", self.seed, "checkout", self.base)
        return sha


@pytest.fixture
def origin(tmp_path):
    return _Origin(tmp_path)


# ── create_worktree ──────────────────────────────────────────────────────────────────


async def test_create_worktree_starts_from_the_intended_base(origin):
    wt, branch = await worktree.create_worktree(origin.clone, origin.base, "bd-create")
    assert branch == "feat/bd-create"
    assert os.path.isdir(wt)
    assert _git_run("-C", wt, "rev-parse", "HEAD") == origin.base_sha()


async def test_create_worktree_resume_starts_from_the_pr_branch(origin):
    branch = worktree.branch_name("bd-resume", "")
    feature_sha = origin.push_feature(branch)
    wt, created = await worktree.create_worktree(origin.clone, origin.base, "bd-resume", resume=True)
    assert created == branch
    assert _git_run("-C", wt, "rev-parse", "HEAD") == feature_sha
    assert feature_sha != origin.base_sha()


async def test_create_worktree_resume_missing_branch_falls_back_to_base(origin):
    # #337: a resume whose PR branch was deleted must build off the LATEST base, not a
    # stale/arbitrary ref. Advance the base so "latest" and "the clone's local main" differ.
    stale = origin.base_sha()
    advanced = origin.advance_base()
    wt, _branch = await worktree.create_worktree(origin.clone, origin.base, "bd-gone", resume=True)
    head = _git_run("-C", wt, "rev-parse", "HEAD")
    assert head == advanced
    assert head != stale


# ── stage_all / commit_worktree ──────────────────────────────────────────────────────


async def test_stage_all_excludes_coder_scratch(origin):
    wt, _branch = await worktree.create_worktree(origin.clone, origin.base, "bd-stage")
    Path(wt, "real.txt").write_text("real\n")
    scratch = Path(wt, ".proto")
    scratch.mkdir()
    Path(scratch, "notes.md").write_text("scratch\n")
    await worktree.stage_all(wt)
    staged = _git_run("-C", wt, "diff", "--cached", "--name-only")
    assert "real.txt" in staged
    assert ".proto" not in staged


async def test_commit_worktree_commits_intended_files_only(origin):
    wt, _branch = await worktree.create_worktree(origin.clone, origin.base, "bd-commit")
    Path(wt, "real.txt").write_text("real\n")
    scratch = Path(wt, ".proto")
    scratch.mkdir()
    Path(scratch, "notes.md").write_text("scratch\n")
    await worktree.commit_worktree(wt, "feat: real change")
    assert _git_run("-C", wt, "log", "-1", "--pretty=%s") == "feat: real change"
    committed = _git_run("-C", wt, "show", "--name-only", "--pretty=format:", "HEAD")
    assert "real.txt" in committed
    assert ".proto" not in committed


async def test_commit_worktree_is_a_noop_on_a_clean_tree(origin):
    wt, _branch = await worktree.create_worktree(origin.clone, origin.base, "bd-clean")
    before = _git_run("-C", wt, "rev-parse", "HEAD")
    await worktree.commit_worktree(wt, "should not commit")
    assert _git_run("-C", wt, "rev-parse", "HEAD") == before


# ── base_checkout_dirt ───────────────────────────────────────────────────────────────


async def test_base_checkout_dirt_detects_uncommitted_and_wrong_head(origin):
    assert await worktree.base_checkout_dirt(origin.clone, origin.base) == ""
    Path(origin.clone, "README.md").write_text("locally edited\n")
    dirty = await worktree.base_checkout_dirt(origin.clone, origin.base)
    assert "uncommitted changes" in dirty
    _git_run("-C", origin.clone, "checkout", "--", "README.md")
    _git_run("-C", origin.clone, "checkout", "-b", "sidebar")
    off_base = await worktree.base_checkout_dirt(origin.clone, origin.base)
    assert "HEAD is on" in off_base


# ── prune_stale_worktrees ────────────────────────────────────────────────────────────


async def test_prune_stale_worktrees_removes_a_stale_admin_entry(origin):
    # Characterization: real `git worktree prune -v` writes its "Removing …" line to
    # STDERR, which `prune_stale_worktrees` does not read — so the return (and its WARNING
    # log) is empty even when it prunes. Assert the real SIDE EFFECT (the stale admin entry
    # is gone), not the return string, which is what makes this credible coverage.
    admin = Path(origin.clone, ".git", "worktrees")
    assert isinstance(await worktree.prune_stale_worktrees(origin.clone), str)
    wt, _branch = await worktree.create_worktree(origin.clone, origin.base, "bd-prune")
    assert (admin / "feat-bd-prune").exists()
    shutil.rmtree(wt)  # a working tree deleted by hand → a stale admin entry
    await worktree.prune_stale_worktrees(origin.clone)
    assert not (admin / "feat-bd-prune").exists()


# ── remove_worktree ──────────────────────────────────────────────────────────────────


async def test_remove_worktree_removes_tree_and_branch(origin):
    wt, branch = await worktree.create_worktree(origin.clone, origin.base, "bd-remove")
    assert os.path.isdir(wt)
    assert await worktree.remove_worktree(origin.clone, wt, branch) is True
    assert not os.path.exists(wt)
    assert _git_rc("-C", origin.clone, "rev-parse", "--verify", f"refs/heads/{branch}") != 0


async def test_remove_worktree_falls_back_to_rmtree_when_metadata_is_gone(origin):
    orphan = Path(origin.clone, ".worktrees", "orphan")
    orphan.mkdir(parents=True)
    Path(orphan, "leftover.txt").write_text("x\n")
    assert await worktree.remove_worktree(origin.clone, str(orphan)) is True
    assert not orphan.exists()


async def test_remove_worktree_returns_false_on_a_locked_tree(origin):
    wt, branch = await worktree.create_worktree(origin.clone, origin.base, "bd-locked")
    _git_run("-C", origin.clone, "worktree", "lock", wt)
    assert await worktree.remove_worktree(origin.clone, wt, branch) is False
    assert os.path.exists(wt)


# ── promote_worktree ─────────────────────────────────────────────────────────────────


async def test_promote_worktree_moves_candidate_to_canonical(origin):
    src_wt, src_branch = await worktree.create_worktree(origin.clone, origin.base, "bd-promote.g2")
    assert src_branch == "feat/bd-promote.g2"
    Path(src_wt, "candidate_work.txt").write_text("wip\n")  # uncommitted — must ride along
    canon_path, canon_branch = await worktree.promote_worktree(origin.clone, src_wt, src_branch, "bd-promote")
    assert canon_branch == "feat/bd-promote"
    assert os.path.isdir(canon_path)
    assert not os.path.exists(src_wt)
    assert Path(canon_path, "candidate_work.txt").exists()
    assert _git_rc("-C", origin.clone, "rev-parse", "--verify", "refs/heads/feat/bd-promote") == 0
    assert _git_rc("-C", origin.clone, "rev-parse", "--verify", "refs/heads/feat/bd-promote.g2") != 0


# ── origin_head_sha ──────────────────────────────────────────────────────────────────


async def test_origin_head_sha_tracks_base_moving(origin):
    first = await worktree.origin_head_sha(origin.clone, origin.base)
    assert first == origin.base_sha()
    advanced = origin.advance_base()
    second = await worktree.origin_head_sha(origin.clone, origin.base)
    assert second == advanced
    assert second != first


# ── delete_remote_branch ─────────────────────────────────────────────────────────────


async def test_delete_remote_branch_removes_it_from_origin(origin):
    branch = worktree.branch_name("bd-del", "")
    origin.push_feature(branch)
    assert _git_run("-C", origin.clone, "ls-remote", "origin", branch) != ""
    assert await worktree.delete_remote_branch(origin.clone, branch) is True
    assert _git_run("-C", origin.clone, "ls-remote", "origin", branch) == ""


async def test_delete_remote_branch_returns_false_for_a_missing_branch(origin):
    assert await worktree.delete_remote_branch(origin.clone, "feat/never-existed") is False


# ── rebase_onto_base ─────────────────────────────────────────────────────────────────


async def test_rebase_onto_base_replays_cleanly(origin):
    branch = worktree.branch_name("bd-rebase", "")
    origin.push_feature(branch)
    base_sha = origin.advance_base()  # base moves under the PR branch
    status, detail = await worktree.rebase_onto_base(origin.clone, branch, origin.base)
    assert status == "clean"
    assert detail == ""
    _git_run("-C", origin.clone, "fetch", "origin", branch)
    assert _git_rc("-C", origin.clone, "merge-base", "--is-ancestor", base_sha, f"origin/{branch}") == 0


async def test_rebase_onto_base_reports_conflict(origin):
    branch = worktree.branch_name("bd-rebase-x", "")
    origin.push_feature(branch, "README.md", "feature version\n")
    origin.advance_base("README.md", "base version\n")  # both edit README → real conflict
    status, detail = await worktree.rebase_onto_base(origin.clone, branch, origin.base)
    assert status == "conflict"
    assert "README.md" in detail


# ── merged_state_worktree ────────────────────────────────────────────────────────────


async def test_merged_state_worktree_builds_a_merged_tree(origin):
    branch = worktree.branch_name("bd-merge", "")
    origin.push_feature(branch)
    base_sha = origin.advance_base()
    # The caller fetches base_sha first (origin_head_sha) so it is locally reachable.
    assert await worktree.origin_head_sha(origin.clone, origin.base) == base_sha
    status, path = await worktree.merged_state_worktree(origin.clone, branch, base_sha)
    assert status == "merged"
    assert os.path.isdir(path)
    assert Path(path, "feature.txt").exists()
    assert Path(path, "advance.txt").exists()
    assert await worktree.remove_worktree(origin.clone, path) is True
