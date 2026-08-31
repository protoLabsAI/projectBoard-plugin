"""Real local-git integration tier for worktree.py (#361 S1).

The unit tier (tests/test_worktree.py) fakes ``_git`` and proves the plugin's CALL
SHAPE. It cannot prove that real git accepts those calls or behaves as assumed — the
exact blind spot that shipped #353/#354/#356 for the ``br``/``gh`` seams, and that
#337 hit for ``create_worktree``: a mocked "branch-from-base" test passed while the
real resume path rebuilt a fix round off a stale ref.

This file gives the 11 LOCAL-git seams a credential-free real tier. Every test runs
against a temporary bare origin plus a working clone, built in ``tmp_path`` so
``origin/<base>`` resolves exactly as it does in production — no network, no GitHub,
no ``gh``, no dependence on the developer's ``~/.gitconfig`` (the env is pinned
hermetic below), and no worktree/branch leak (everything lives under ``tmp_path``,
which pytest reaps).

Seams covered here (classified REAL in tests/test_external_seams.py):
``base_checkout_dirt``, ``commit_worktree``, ``create_worktree``,
``delete_remote_branch``, ``merged_state_worktree``, ``origin_head_sha``,
``promote_worktree``, ``prune_stale_worktrees``, ``rebase_onto_base``,
``remove_worktree``, ``stage_all``.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

from project_board import worktree

# git is present on every CI runner (checkout needs it); the skip only guards a
# git-less local box. It is deliberately NOT under a PB_REQUIRE_* gate — unlike the
# `br` tier there is no separately-installed binary that could silently be absent.
requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH — the real-git tier needs it")

pytestmark = requires_git


def _sh(*args: str) -> str:
    """A raw ``git <args>`` (no ``-C``) for init/clone; asserts success, returns stdout."""
    proc = subprocess.run(["git", *args], capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr.strip()}"
    return proc.stdout.strip()


def _run(cwd: str, *args: str) -> str:
    """A ``git -C <cwd> <args>`` that asserts success and returns stripped stdout — for
    fixture setup only (the seams under test run through worktree.py's own ``_git``)."""
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert proc.returncode == 0, f"git -C {cwd} {' '.join(args)} failed: {proc.stderr.strip()}"
    return proc.stdout.strip()


def _write(cwd: str, relpath: str, content: str) -> None:
    p = pathlib.Path(cwd) / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


class _GitHarness:
    """A temp bare origin + working clone, with the setup helpers the tests share."""

    def __init__(self, tmp: str, origin: str, repo: str) -> None:
        self._tmp = tmp
        self.origin = origin
        self.repo = repo
        self._n = 0

    def _side_clone(self, prefix: str) -> str:
        """A throwaway second clone of origin — the stand-in for another actor (a
        teammate, GitHub's auto-delete) mutating the remote behind ``repo``'s back."""
        self._n += 1
        path = str(pathlib.Path(self._tmp) / f"{prefix}-{self._n}")
        _sh("clone", self.origin, path)
        return path

    def sha(self, ref: str, cwd: str | None = None) -> str:
        return _run(cwd or self.repo, "rev-parse", ref)

    def push_feature_branch(self, branch: str, relpath: str, content: str, message: str) -> str:
        """Push a fresh ``branch`` off ``main`` to origin, then drop the LOCAL ref and
        return to ``main`` — leaving only ``origin/<branch>`` (a remote-tracking ref), the
        state a card with an open PR presents. Returns the pushed tip sha."""
        _run(self.repo, "checkout", "-b", branch, "main")
        _write(self.repo, relpath, content)
        _run(self.repo, "add", "-A")
        _run(self.repo, "commit", "-m", message)
        _run(self.repo, "push", "-u", "origin", branch)
        sha = self.sha("HEAD")
        _run(self.repo, "checkout", "main")
        _run(self.repo, "branch", "-D", branch)
        return sha

    def advance_main(self, relpath: str, content: str, message: str) -> str:
        """Advance origin/main from a SIDE clone — so ``repo``'s own ``origin/main`` stays
        stale until it fetches, proving the seam pulls the freshest base. Returns new tip."""
        side = self._side_clone("advance")
        _write(side, relpath, content)
        _run(side, "add", "-A")
        _run(side, "commit", "-m", message)
        _run(side, "push", "origin", "main")
        return self.sha("HEAD", cwd=side)

    def external_delete(self, branch: str) -> None:
        """Delete ``branch`` on origin from a SIDE clone — ``repo`` never learns via a push,
        so its ``origin/<branch>`` tracking ref goes STALE (the #337 rebuild trigger)."""
        side = self._side_clone("delete")
        _run(side, "push", "origin", "--delete", branch)

    def blob(self, ref_path: str, cwd: str | None = None) -> str | None:
        """The content of ``<ref>:<path>`` (e.g. ``origin/feat-x:g.txt``), or None if the
        path is absent in that tree — a git-native existence probe."""
        proc = subprocess.run(["git", "-C", str(cwd or self.repo), "show", ref_path], capture_output=True, text=True)
        return proc.stdout if proc.returncode == 0 else None

    def local_branch_exists(self, branch: str) -> bool:
        return bool(_run(self.repo, "branch", "--list", branch).strip())

    def remote_branch_exists(self, branch: str) -> bool:
        return bool(_run(self.repo, "ls-remote", "--heads", "origin", branch).strip())


@pytest.fixture
def real_git(monkeypatch, tmp_path):
    """A hermetic temp origin + clone. The git env is pinned so NO test depends on (or
    mutates) global/system git config, and commits get an identity without one. worktree's
    own ``_git`` (asyncio subprocess) and these helpers both inherit this env."""
    empty_cfg = tmp_path / "empty.gitconfig"
    empty_cfg.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "PB Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "pb-test@localhost")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "PB Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "pb-test@localhost")

    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    _sh("init", "--bare", "-b", "main", str(origin))
    _sh("clone", str(origin), str(repo))
    _write(str(repo), "README.md", "# base\n")
    _run(str(repo), "add", "-A")
    _run(str(repo), "commit", "-m", "initial commit on main")
    _run(str(repo), "push", "-u", "origin", "main")
    return _GitHarness(str(tmp_path), str(origin), str(repo))


# ── base_checkout_dirt ────────────────────────────────────────────────────────────────


async def test_base_checkout_dirt_is_empty_on_a_clean_base_checkout(real_git):
    assert await worktree.base_checkout_dirt(real_git.repo, "main") == ""


async def test_base_checkout_dirt_reports_an_uncommitted_tracked_change(real_git):
    _write(real_git.repo, "README.md", "operator edited this by hand\n")
    dirt = await worktree.base_checkout_dirt(real_git.repo, "main")
    assert "uncommitted changes" in dirt
    assert "README.md" in dirt


async def test_base_checkout_dirt_reports_a_head_off_the_base_branch(real_git):
    _run(real_git.repo, "checkout", "-b", "sidebar")
    dirt = await worktree.base_checkout_dirt(real_git.repo, "main")
    assert "sidebar" in dirt
    assert "main" in dirt


async def test_base_checkout_dirt_skips_the_head_check_without_a_base(real_git):
    _run(real_git.repo, "checkout", "-b", "sidebar")  # off base, but clean...
    assert await worktree.base_checkout_dirt(real_git.repo, "") == ""  # ...and base='' → no head check


# ── stage_all ─────────────────────────────────────────────────────────────────────────


async def test_stage_all_stages_intended_files_but_excludes_coder_scratch(real_git):
    _write(real_git.repo, "feature.py", "print('real work')\n")
    _write(real_git.repo, ".proto/session.md", "coder private notes\n")
    _write(real_git.repo, ".cursor/cache", "editor cache\n")
    rc, _out, _err = await worktree.stage_all(real_git.repo)
    assert rc == 0
    staged = set(_run(real_git.repo, "diff", "--cached", "--name-only").splitlines())
    assert "feature.py" in staged
    assert not any(p.startswith((".proto", ".cursor")) for p in staged)


# ── commit_worktree ───────────────────────────────────────────────────────────────────


async def test_commit_worktree_commits_a_dirty_tree_without_the_scratch(real_git):
    _write(real_git.repo, "README.md", "coder changed this\n")
    _write(real_git.repo, ".proto/session.md", "coder private notes\n")
    await worktree.commit_worktree(real_git.repo, "coder change")
    assert _run(real_git.repo, "log", "-1", "--format=%s") == "coder change"
    assert real_git.blob("HEAD:README.md") == "coder changed this\n"
    assert real_git.blob("HEAD:.proto/session.md") is None  # scratch never rode into the commit


async def test_commit_worktree_is_a_noop_on_a_clean_tree(real_git):
    before = real_git.sha("HEAD")
    await worktree.commit_worktree(real_git.repo, "should not commit")
    assert real_git.sha("HEAD") == before


# ── create_worktree ───────────────────────────────────────────────────────────────────


async def test_create_worktree_starts_from_the_freshest_base_tip(real_git):
    """Ordinary creation branches off the intended base — and specifically the FRESHEST
    origin tip, since create_worktree fetches before it adds."""
    stale = real_git.sha("origin/main")
    advanced = real_git.advance_main("adv.txt", "base moved on\n", "advance main")
    path, branch = await worktree.create_worktree(real_git.repo, "main", "bd-1")
    assert branch == "feat/bd-1"
    assert os.path.isdir(path)
    head = real_git.sha("HEAD", cwd=path)
    assert head == advanced  # the freshest base, fetched by create_worktree
    assert head != stale
    assert real_git.local_branch_exists("feat/bd-1")


async def test_create_worktree_resume_starts_from_the_pushed_pr_branch(real_git):
    """resume=True opens the coder ON its prior work (the PR head), not a clean base."""
    feat_sha = real_git.push_feature_branch("feat/bd-2", "wip.py", "print('wip')\n", "wip on the PR")
    main_sha = real_git.sha("origin/main")
    path, branch = await worktree.create_worktree(real_git.repo, "main", "bd-2", resume=True)
    assert branch == "feat/bd-2"
    head = real_git.sha("HEAD", cwd=path)
    assert head == feat_sha  # the PR head
    assert head != main_sha
    assert os.path.isfile(os.path.join(path, "wip.py"))  # the prior work is present


async def test_create_worktree_resume_falls_back_to_base_when_the_branch_never_existed(real_git):
    """A resume for a branch that was never pushed still builds — off base."""
    main_sha = real_git.sha("origin/main")
    path, _branch = await worktree.create_worktree(real_git.repo, "main", "bd-3", resume=True)
    assert real_git.sha("HEAD", cwd=path) == main_sha


async def test_create_worktree_resume_falls_back_to_base_when_the_branch_was_deleted_externally(real_git):
    """#337's rebuild-loop class, against real git: a branch tidied away on the remote
    leaves a STALE ``origin/<branch>`` tracking ref locally. Resume must fall back to base
    — NOT rebuild the fix round off the dead ref. (Against the pre-fix code this fails: the
    stale ref still rev-parses, so it rebuilt off it; the mocked tier could never see it.)"""
    stale_sha = real_git.push_feature_branch("feat/bd-4", "old.py", "print('old')\n", "superseded work")
    _run(real_git.repo, "fetch", "origin", "feat/bd-4")  # populate repo's origin/feat/bd-4 tracking ref
    assert real_git.sha("origin/feat/bd-4") == stale_sha  # the tracking ref is present...
    real_git.external_delete("feat/bd-4")  # ...and now points at a branch that is gone on origin
    main_sha = real_git.sha("origin/main")
    path, _branch = await worktree.create_worktree(real_git.repo, "main", "bd-4", resume=True)
    head = real_git.sha("HEAD", cwd=path)
    assert head == main_sha  # fell back to base
    assert head != stale_sha  # not rebuilt off the stale ref
    assert not os.path.isfile(os.path.join(path, "old.py"))  # the dead branch's work is absent


# ── delete_remote_branch ──────────────────────────────────────────────────────────────


async def test_delete_remote_branch_removes_the_branch_on_origin(real_git):
    real_git.push_feature_branch("feat/bd-5", "x.txt", "x\n", "branch to delete")
    assert real_git.remote_branch_exists("feat/bd-5")
    assert await worktree.delete_remote_branch(real_git.repo, "feat/bd-5") is True
    assert not real_git.remote_branch_exists("feat/bd-5")


async def test_delete_remote_branch_is_false_when_the_branch_is_absent(real_git):
    assert await worktree.delete_remote_branch(real_git.repo, "feat/never-pushed") is False


# ── origin_head_sha ───────────────────────────────────────────────────────────────────


async def test_origin_head_sha_returns_the_fetched_remote_tip(real_git):
    advanced = real_git.advance_main("a.txt", "advanced base\n", "advance main")
    assert await worktree.origin_head_sha(real_git.repo, "main") == advanced


async def test_origin_head_sha_is_empty_for_an_unknown_ref(real_git):
    assert await worktree.origin_head_sha(real_git.repo, "no-such-branch") == ""


# ── merged_state_worktree ─────────────────────────────────────────────────────────────


async def test_merged_state_worktree_builds_the_merged_tree(real_git):
    """A clean (non-conflicting) merge yields a detached worktree holding BOTH the branch's
    work and the advanced base — with no push (branch/PR/CI untouched)."""
    real_git.push_feature_branch("feat/bd-6", "feature.txt", "from the feature\n", "feature work")
    real_git.advance_main("baseonly.txt", "from the base\n", "advance main")
    base_sha = await worktree.origin_head_sha(real_git.repo, "main")  # fetch it → locally reachable
    status, path = await worktree.merged_state_worktree(real_git.repo, "feat/bd-6", base_sha)
    assert status == "merged"
    assert os.path.isfile(os.path.join(path, "feature.txt"))
    assert os.path.isfile(os.path.join(path, "baseonly.txt"))
    # no push happened: origin/feat/bd-6 is still the single-commit branch tip
    assert real_git.remote_branch_exists("feat/bd-6")
    assert await worktree.remove_worktree(real_git.repo, path) is True


async def test_merged_state_worktree_reports_conflict(real_git):
    real_git.push_feature_branch("feat/bd-7", "README.md", "feature edit of the readme\n", "feature edit")
    real_git.advance_main("README.md", "base edit of the readme\n", "base edit")
    base_sha = await worktree.origin_head_sha(real_git.repo, "main")
    status, files = await worktree.merged_state_worktree(real_git.repo, "feat/bd-7", base_sha)
    assert status == "conflict"
    assert "README.md" in files


# ── rebase_onto_base ──────────────────────────────────────────────────────────────────


async def test_rebase_onto_base_clean_rebases_and_force_pushes(real_git):
    real_git.push_feature_branch("feat/bd-8", "g.txt", "feature file\n", "feature work")
    advanced = real_git.advance_main("h.txt", "base advanced\n", "advance main")
    status, detail = await worktree.rebase_onto_base(real_git.repo, "feat/bd-8", "main")
    assert (status, detail) == ("clean", "")
    _run(real_git.repo, "fetch", "origin", "feat/bd-8")  # observe what was force-pushed
    assert real_git.blob("origin/feat/bd-8:g.txt") == "feature file\n"  # its own work rode along
    assert real_git.blob("origin/feat/bd-8:h.txt") == "base advanced\n"  # now on top of base
    assert real_git.sha("origin/main") == advanced


async def test_rebase_onto_base_reports_conflict_and_leaves_the_remote_untouched(real_git):
    stale_tip = real_git.push_feature_branch("feat/bd-9", "README.md", "feature edit\n", "feature edit")
    real_git.advance_main("README.md", "base edit\n", "base edit")
    status, files = await worktree.rebase_onto_base(real_git.repo, "feat/bd-9", "main")
    assert status == "conflict"
    assert "README.md" in files
    _run(real_git.repo, "fetch", "origin", "feat/bd-9")
    assert real_git.sha("origin/feat/bd-9") == stale_tip  # aborted: the remote branch is untouched


# ── promote_worktree ──────────────────────────────────────────────────────────────────


async def test_promote_worktree_moves_the_candidate_to_canonical_carrying_dirty_work(real_git):
    """A Max-Mode candidate is promoted to the canonical ``feat-<id>`` name/branch with its
    still-uncommitted work intact (``git worktree move`` + ``branch -m`` preserve it)."""
    src_path, src_branch = await worktree.create_worktree(real_git.repo, "main", "bd-10.c1")
    _write(src_path, "candidate.py", "print('uncommitted candidate work')\n")  # dirty, never committed
    canon_path, canon_branch = await worktree.promote_worktree(real_git.repo, src_path, src_branch, "bd-10")
    assert canon_branch == "feat/bd-10"
    assert canon_path.endswith(os.path.join(".worktrees", "feat-bd-10"))
    assert not os.path.exists(src_path)  # the candidate dir moved
    assert os.path.isfile(os.path.join(canon_path, "candidate.py"))  # dirty work rode along
    assert _run(canon_path, "rev-parse", "--abbrev-ref", "HEAD") == "feat/bd-10"
    assert await worktree.remove_worktree(real_git.repo, canon_path, canon_branch) is True


# ── prune_stale_worktrees ─────────────────────────────────────────────────────────────


async def test_prune_stale_worktrees_is_empty_on_a_healthy_repo(real_git):
    assert await worktree.prune_stale_worktrees(real_git.repo) == ""


async def test_prune_stale_worktrees_clears_a_corrupt_worktree_admin_entry(real_git):
    """#225: a worktree whose tree was deleted by hand — leaving a corrupt
    ``.git/worktrees/*`` admin entry — is exactly what makes a later ``worktree add`` fail
    with 'fatal: not a git repository'. Prune clears the stale admin entry so the repo is
    usable again (asserted on STATE: the output text of ``git worktree prune -v`` is not a
    stable contract across git builds, but the cleanup it performs is)."""
    path, _branch = await worktree.create_worktree(real_git.repo, "main", "bd-11")
    admin = os.path.join(real_git.repo, ".git", "worktrees", "feat-bd-11")
    assert os.path.isdir(admin)
    shutil.rmtree(path)  # the working tree is gone...
    os.remove(os.path.join(admin, "gitdir"))  # ...and its admin entry is corrupt (#225)
    out = await worktree.prune_stale_worktrees(real_git.repo)
    assert isinstance(out, str)  # best-effort: never raises, returns the stripped report
    assert not os.path.exists(admin)  # the stale admin entry is cleared


# ── remove_worktree ───────────────────────────────────────────────────────────────────


async def test_remove_worktree_tears_down_the_tree_and_its_branch(real_git):
    path, branch = await worktree.create_worktree(real_git.repo, "main", "bd-12")
    assert os.path.isdir(path)
    assert await worktree.remove_worktree(real_git.repo, path, branch) is True
    assert not os.path.exists(path)
    assert not real_git.local_branch_exists("feat/bd-12")
