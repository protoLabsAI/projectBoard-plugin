"""Real local-git integration tier for worktree.py's 11 LOCAL-GIT seams (#361 S1).

The mocked unit tier (``test_worktree.py``) proves the plugin's call SHAPE and its
local branching against a canned ``_git``; it is structurally blind to what real
``git`` actually decides — which ``origin/<base>`` resolves to, whether a resume
branch exists, whether a rebase/merge conflicts. #337 was exactly that gap: mocked
branch-from-base behaviour looked fine while the real fallback was untested.

This tier closes it for the 11 seams whose whole effect is LOCAL git —
``base_checkout_dirt``, ``commit_worktree``, ``create_worktree``,
``delete_remote_branch``, ``merged_state_worktree``, ``origin_head_sha``,
``promote_worktree``, ``prune_stale_worktrees``, ``rebase_onto_base``,
``remove_worktree``, ``stage_all`` — by running them against an ACTUAL git repo. The
fixture builds a temporary bare ``origin`` plus a working ``clone`` (== production's
``repo``), so ``origin/<base>`` and remote branch existence resolve EXACTLY as in the
deployed board — no network, no GitHub credentials, no global git config (identity is
set as LOCAL config), and every artifact lives under ``tmp_path`` so nothing leaks.

The mocked tier stays: it owns the error paths (a corrupt worktree admin dir, a
``gh`` refusal) that are awkward to provoke with real git. This tier validates the
external behaviour those mocks can only assume.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from project_board import worktree

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="real `git` not on PATH — the local-git integration tier needs it",
)

# The 11 seams this file exercises against real git. Kept next to the tests so the
# coupling check below can prove the ratchet registry and this tier agree.
REAL_LOCAL_GIT_SEAMS = (
    "base_checkout_dirt",
    "commit_worktree",
    "create_worktree",
    "delete_remote_branch",
    "merged_state_worktree",
    "origin_head_sha",
    "promote_worktree",
    "prune_stale_worktrees",
    "rebase_onto_base",
    "remove_worktree",
    "stage_all",
)


def _sh(cwd, *args, check=True):
    """Run a raw ``git`` command for fixture setup / assertions (NOT the seam under
    test — the seams shell their own ``_git``). Asserts a clean exit by default."""
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check:
        assert proc.returncode == 0, f"git -C {cwd} {' '.join(args)} failed ({proc.returncode}): {proc.stderr}"
    return proc.stdout.strip()


def _configure(repo):
    """Pin a LOCAL committer identity + no gpg signing, so the tier depends on NO
    global git config (a machine with ``commit.gpgsign=true`` and no key would else
    fail every commit/rebase). Worktrees created under ``repo`` inherit this config."""
    _sh(repo, "config", "user.name", "PB Test")
    _sh(repo, "config", "user.email", "pb-test@localhost")
    _sh(repo, "config", "commit.gpgsign", "false")


def _is_ancestor(repo, ancestor, descendant):
    """True iff ``ancestor`` is reachable from ``descendant`` (``--is-ancestor``)."""
    return (
        subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant]
        ).returncode
        == 0
    )


class _Origin:
    """A temp bare ``origin`` + a working ``clone`` whose ``origin/<base>`` resolves
    exactly as the deployed board's checkout does. A throwaway ``seed`` working repo
    is the only way to push feature branches / advance the base ON ORIGIN so the seams
    fetch real refs; it is not the repo under test — ``self.repo`` (the clone) is."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.origin = tmp_path / "origin.git"
        self.seed = tmp_path / "seed"
        self.clone = tmp_path / "clone"
        self.base = "main"
        self.seed.mkdir()
        _sh(self.seed, "init")
        _configure(self.seed)
        (self.seed / "README.md").write_text("base\n")
        _sh(self.seed, "add", "-A")
        _sh(self.seed, "commit", "-m", "base: initial")
        # Name the base 'main' regardless of the global init.defaultBranch (hermetic).
        _sh(self.seed, "branch", "-M", self.base)
        # Bare origin FROM the seed → its HEAD symref points at 'main'.
        _sh(tmp_path, "clone", "--bare", str(self.seed), str(self.origin))
        # Let the seed push further branches/commits to that same origin.
        _sh(self.seed, "remote", "add", "origin", str(self.origin))
        # The repo under test: a clone whose `origin/main` remote-tracking ref resolves.
        _sh(tmp_path, "clone", str(self.origin), str(self.clone))
        _configure(self.clone)

    @property
    def repo(self):
        return str(self.clone)

    def origin_sha(self, ref):
        """The tip of ``ref`` as ORIGIN holds it — the source of truth the seams fetch."""
        return _sh(self.origin, "rev-parse", ref)

    def clone_git(self, *args, check=True):
        return _sh(self.clone, *args, check=check)

    def head_of(self, worktree_path):
        return _sh(worktree_path, "rev-parse", "HEAD")

    def push_feature_branch(self, branch, *, filename="feature.txt", content="feature\n", message="feat: work"):
        """Create ``branch`` off the base and push it to origin — the PR head a resume /
        rebase / merged-state seam fetches. Returns its tip sha."""
        _sh(self.seed, "checkout", self.base)
        _sh(self.seed, "checkout", "-B", branch, self.base)
        (self.seed / filename).write_text(content)
        _sh(self.seed, "add", "-A")
        _sh(self.seed, "commit", "-m", message)
        sha = _sh(self.seed, "rev-parse", "HEAD")
        _sh(self.seed, "push", "-f", "origin", branch)
        _sh(self.seed, "checkout", self.base)
        return sha

    def advance_base(self, *, filename="base.txt", content="moved\n", message="base: move"):
        """Move ``origin/<base>`` forward one commit and return its new tip — so a seam
        that fetches the LATEST origin base is distinguishable from the clone's stale
        local ref."""
        _sh(self.seed, "checkout", self.base)
        (self.seed / filename).write_text(content)
        _sh(self.seed, "add", "-A")
        _sh(self.seed, "commit", "-m", message)
        sha = _sh(self.seed, "rev-parse", "HEAD")
        _sh(self.seed, "push", "origin", self.base)
        return sha


@pytest.fixture
def origin(tmp_path):
    return _Origin(tmp_path)


pytestmark = requires_git


# ── stage_all ────────────────────────────────────────────────────────────────────


async def test_stage_all_stages_intended_files_and_excludes_coder_scratch(origin):
    repo = origin.clone
    (repo / "app.py").write_text("x = 1\n")
    (repo / ".proto").mkdir()
    (repo / ".proto" / "notes.md").write_text("agent session notes\n")
    (repo / ".cursor").mkdir()
    (repo / ".cursor" / "cache").write_text("editor cache\n")

    rc, _out, _err = await worktree.stage_all(str(repo))

    assert rc == 0
    staged = set(origin.clone_git("diff", "--cached", "--name-only").splitlines())
    assert "app.py" in staged  # the intended file is staged
    assert not any(p.startswith((".proto/", ".cursor/")) for p in staged)  # scratch excluded


# ── base_checkout_dirt ───────────────────────────────────────────────────────────


async def test_base_checkout_dirt_clean_untracked_dirty_and_wrong_head(origin):
    repo = origin.repo
    # Clean checkout, on the base branch → no dirt.
    assert await worktree.base_checkout_dirt(repo, "main") == ""
    # An untracked file is build/scratch, NOT dirt.
    (origin.clone / "scratch.txt").write_text("junk\n")
    assert await worktree.base_checkout_dirt(repo, "main") == ""
    # A tracked, uncommitted edit IS dirt and names the file.
    (origin.clone / "README.md").write_text("locally edited\n")
    dirt = await worktree.base_checkout_dirt(repo, "main")
    assert "uncommitted changes" in dirt and "README.md" in dirt
    # A HEAD that isn't on the base branch is dirt.
    origin.clone_git("checkout", "--", "README.md")  # undo the edit → clean tree
    origin.clone_git("checkout", "-b", "sidebar")
    off_base = await worktree.base_checkout_dirt(repo, "main")
    assert "HEAD is on 'sidebar'" in off_base and "not the base branch 'main'" in off_base


# ── prune_stale_worktrees ────────────────────────────────────────────────────────


async def test_prune_stale_worktrees_clears_orphaned_admin_entry(origin):
    repo = origin.repo
    assert await worktree.prune_stale_worktrees(repo) == ""  # nothing stale yet
    # A real worktree whose directory is then deleted out from under git → stale admin.
    origin.clone_git("worktree", "add", "--detach", ".worktrees/tmpwt", "main")
    assert "tmpwt" in origin.clone_git("worktree", "list")
    shutil.rmtree(origin.clone / ".worktrees" / "tmpwt")
    # Real git writes its `-v` report to STDERR, which the seam discards, so the return
    # is "" even here — but the EFFECT lands: the stale admin entry is pruned (which is
    # what unblocks the subsequent `worktree add`, #225). A mock could never show this.
    await worktree.prune_stale_worktrees(repo)
    assert "tmpwt" not in origin.clone_git("worktree", "list")


# ── create_worktree ──────────────────────────────────────────────────────────────


async def test_create_worktree_starts_from_the_intended_base(origin):
    stale_local = origin.clone_git("rev-parse", "main")
    advanced = origin.advance_base()  # origin/main now ahead of the clone's local main
    path, branch = await worktree.create_worktree(origin.repo, "main", "bd-cw1")
    assert branch == "feat/bd-cw1"
    assert os.path.isdir(path)
    # Built off the LATEST origin/main (fetched), not the clone's stale local main.
    assert origin.head_of(path) == advanced == origin.origin_sha("main")
    assert origin.head_of(path) != stale_local
    assert await worktree.remove_worktree(origin.repo, path, branch)


async def test_create_worktree_resume_starts_from_existing_pr_branch(origin):
    feat_sha = origin.push_feature_branch("feat/bd-cw2")
    base_sha = origin.origin_sha("main")
    path, branch = await worktree.create_worktree(origin.repo, "main", "bd-cw2", resume=True)
    assert branch == "feat/bd-cw2"
    # resume=True opens the PR head, not a clean tree off base.
    assert origin.head_of(path) == feat_sha
    assert origin.head_of(path) != base_sha
    assert await worktree.remove_worktree(origin.repo, path, branch)


async def test_create_worktree_resume_missing_branch_falls_back_to_base(origin):
    # No feat/bd-cw3 was ever pushed. resume=True must fall back to the intended base
    # (origin/main) rather than rebuilding from an arbitrary/stale ref — #337's
    # rebuild-loop failure class, which mocked branch-from-base behaviour missed.
    advanced = origin.advance_base()
    path, branch = await worktree.create_worktree(origin.repo, "main", "bd-cw3", resume=True)
    assert branch == "feat/bd-cw3"
    assert origin.head_of(path) == advanced == origin.origin_sha("main")
    assert await worktree.remove_worktree(origin.repo, path, branch)


# ── remove_worktree ──────────────────────────────────────────────────────────────


async def test_remove_worktree_removes_tree_and_branch(origin):
    path, branch = await worktree.create_worktree(origin.repo, "main", "bd-rm1")
    assert os.path.isdir(path)
    removed = await worktree.remove_worktree(origin.repo, path, branch)
    assert removed is True
    assert not os.path.exists(path)
    assert origin.clone_git("branch", "--list", branch) == ""  # branch deleted too


# ── promote_worktree ─────────────────────────────────────────────────────────────


async def test_promote_worktree_moves_candidate_to_canonical_with_dirty_tree(origin):
    src_path, src_branch = await worktree.create_worktree(origin.repo, "main", "bd-pr1.c1")
    assert src_branch == "feat/bd-pr1.c1"
    # An uncommitted change must ride along the move (the winning candidate's work).
    (Path(src_path) / "wip.txt").write_text("in progress\n")

    canon_path, canon_branch = await worktree.promote_worktree(origin.repo, src_path, src_branch, "bd-pr1")

    assert canon_branch == "feat/bd-pr1"
    assert os.path.basename(canon_path) == "feat-bd-pr1"
    assert os.path.isdir(canon_path)
    assert not os.path.exists(src_path)  # candidate tree moved, not copied
    assert (Path(canon_path) / "wip.txt").read_text() == "in progress\n"  # dirty tree preserved
    assert _sh(canon_path, "rev-parse", "--abbrev-ref", "HEAD") == "feat/bd-pr1"  # branch renamed
    assert await worktree.remove_worktree(origin.repo, canon_path, canon_branch)


# ── commit_worktree ──────────────────────────────────────────────────────────────


async def test_commit_worktree_commits_dirty_and_noops_on_clean(origin):
    path, branch = await worktree.create_worktree(origin.repo, "main", "bd-ci1")
    before = _sh(path, "rev-parse", "HEAD")
    # Clean tree → no commit (the coder may have committed its own work).
    await worktree.commit_worktree(path, "should-not-commit")
    assert _sh(path, "rev-parse", "HEAD") == before
    # Dirty tree → the leftover is committed under the given message.
    (Path(path) / "new.py").write_text("y = 2\n")
    await worktree.commit_worktree(path, "feat: add new.py")
    assert _sh(path, "log", "-1", "--format=%s") == "feat: add new.py"
    assert _sh(path, "status", "--porcelain") == ""  # tree clean afterwards
    assert await worktree.remove_worktree(origin.repo, path, branch)


# ── delete_remote_branch ─────────────────────────────────────────────────────────


async def test_delete_remote_branch_removes_present_and_reports_absent(origin):
    origin.push_feature_branch("feat/bd-del1")
    assert origin.origin_sha("feat/bd-del1")  # present on origin
    assert await worktree.delete_remote_branch(origin.repo, "feat/bd-del1") is True
    assert _sh(origin.origin, "branch", "--list", "feat/bd-del1") == ""  # gone on origin
    # Deleting an already-absent branch fails softly (never raises).
    assert await worktree.delete_remote_branch(origin.repo, "feat/nope") is False


# ── origin_head_sha ──────────────────────────────────────────────────────────────


async def test_origin_head_sha_reads_latest_and_empty_on_missing(origin):
    advanced = origin.advance_base()
    assert await worktree.origin_head_sha(origin.repo, "main") == advanced
    assert await worktree.origin_head_sha(origin.repo, "does/not/exist") == ""


# ── rebase_onto_base ─────────────────────────────────────────────────────────────


async def test_rebase_onto_base_clean_replays_onto_moved_base(origin):
    feat = origin.push_feature_branch("feat/bd-rb1", filename="feature.txt")
    moved = origin.advance_base(filename="base.txt")  # disjoint files → clean rebase
    result, detail = await worktree.rebase_onto_base(origin.repo, "feat/bd-rb1", "main")
    assert result == "clean"
    assert detail == ""
    origin.clone_git("fetch", "origin", "feat/bd-rb1", "main")
    new_tip = origin.clone_git("rev-parse", "origin/feat/bd-rb1")
    assert new_tip != feat  # history was rewritten and force-pushed
    assert _is_ancestor(origin.clone, moved, "origin/feat/bd-rb1")  # now sits on the moved base


async def test_rebase_onto_base_conflict_leaves_remote_untouched(origin):
    origin.push_feature_branch("feat/bd-rb2", filename="conflict.txt", content="feature side\n")
    origin.advance_base(filename="conflict.txt", content="base side\n")  # same file → conflict
    before = origin.origin_sha("feat/bd-rb2")
    result, detail = await worktree.rebase_onto_base(origin.repo, "feat/bd-rb2", "main")
    assert result == "conflict"
    assert "conflict.txt" in detail  # names the conflicted path
    assert origin.origin_sha("feat/bd-rb2") == before  # aborted → remote branch untouched


# ── merged_state_worktree ────────────────────────────────────────────────────────


async def test_merged_state_worktree_builds_merged_tree_without_pushing(origin):
    origin.push_feature_branch("feat/bd-ms1", filename="feature.txt")
    moved = origin.advance_base(filename="base.txt")  # disjoint files → clean merge
    before = origin.origin_sha("feat/bd-ms1")
    base_sha = await worktree.origin_head_sha(origin.repo, "main")  # caller fetches base first
    assert base_sha == moved

    state, path = await worktree.merged_state_worktree(origin.repo, "feat/bd-ms1", base_sha)

    assert state == "merged"
    assert os.path.isdir(path)
    assert (Path(path) / "feature.txt").exists()  # feature work present
    assert (Path(path) / "base.txt").exists()  # base advance merged in
    assert _is_ancestor(path, base_sha, "HEAD")  # the merge included the base commit
    assert origin.origin_sha("feat/bd-ms1") == before  # NO push — remote untouched
    assert await worktree.remove_worktree(origin.repo, path)


async def test_merged_state_worktree_conflict_removes_worktree(origin):
    origin.push_feature_branch("feat/bd-ms2", filename="conflict.txt", content="feature side\n")
    moved = origin.advance_base(filename="conflict.txt", content="base side\n")
    base_sha = await worktree.origin_head_sha(origin.repo, "main")
    assert base_sha == moved

    state, detail = await worktree.merged_state_worktree(origin.repo, "feat/bd-ms2", base_sha)

    assert state == "conflict"
    assert "conflict.txt" in detail
    # The conflicted scratch worktree is cleaned up (no leak).
    rel = os.path.join(".worktrees", ".verify-feat-bd-ms2")
    assert not os.path.exists(os.path.join(origin.repo, rel))


# ── coupling: the tier and the ratchet registry must agree ───────────────────────


def test_the_eleven_local_git_seams_are_classified_real():
    """Every seam this file exercises against real git must be classified REAL in the
    ratchet registry, so the coverage claim and the tier can't silently drift apart
    (a reverted registry with these tests still green would re-open the #354 gap)."""
    seams = importlib.import_module("test_external_seams").WORKTREE_SEAMS
    for name in REAL_LOCAL_GIT_SEAMS:
        assert seams.get(name) == "REAL", f"{name} must be classified REAL in tests/test_external_seams.py"
