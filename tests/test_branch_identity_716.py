"""#716 (1/2): a card's branch is recorded once, so renaming the card never moves it.

Real git (a working clone of a bare origin) and the REAL `br` store; only `gh` is faked.
The bug: the branch/dir names were derived from the CURRENT title, so a card renamed after
its PR opened sent the fix round to `feat/<id>-<new-slug>` — absent on origin — and
`create_worktree` fell back to base: the coder rebuilt on a new branch, a second PR opened
and the original was orphaned.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest
from conftest import REAL_SEAMS

from project_board import store as store_mod
from project_board import worktree
from project_board.loop.drive import DriveMixin
from project_board.store import LABEL_BRANCH_SLUG_PREFIX, BeadsBoard, BoardError

pytestmark = pytest.mark.skipif(shutil.which(store_mod.BR) is None, reason="real `br` (beads) CLI not on PATH")

ROOT = ".worktrees"


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE).strip()


@pytest.fixture
def repo(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    repo = tmp_path / "repo"
    git(tmp_path, "clone", str(remote), str(repo))
    git(repo, "config", "user.email", "board@example.test")
    git(repo, "config", "user.name", "Board")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "checkout", "-b", "main")
    (repo / "a").write_text("initial\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    git(repo, "push", "origin", "main")
    return repo


@pytest.fixture
def board(tmp_path):
    beads = tmp_path / "beads"
    beads.mkdir()
    return BeadsBoard(repo=str(beads), actor="test")


@pytest.fixture
def fake_gh(monkeypatch):
    """Restore the real PR-head read and fake only the `gh` subprocess under it."""
    monkeypatch.setattr(worktree, "pr_head_branch", REAL_SEAMS["worktree.pr_head_branch"])
    calls: list[tuple] = []
    state = {"head": "", "rc": 0}

    async def _gh(*args, cwd, timeout=60):
        calls.append(args)
        if state["rc"]:
            return state["rc"], "", "gh: could not resolve to a PullRequest"
        return 0, state["head"] + "\n", ""

    monkeypatch.setattr(worktree, "_gh", _gh)
    state["calls"] = calls
    return state


def _drive():
    return DriveMixin.__new__(DriveMixin)


async def _fix_round(board, repo, fid):
    """The drive's build / fix-round path: resolve the branch tail, then cut the tree."""
    feature = board.get_feature(fid)
    title = await _drive()._branch_title(board, feature, str(repo))
    return await worktree.create_worktree(
        str(repo),
        "main",
        fid,
        ROOT,
        title=title,
        resume=bool(feature.get("pr_url")),
        integrate=bool(feature.get("pr_url")),
    )


async def _first_build_and_push(board, repo, fid):
    wt, branch = await _fix_round(board, repo, fid)
    with open(f"{wt}/feature.txt", "w") as fh:
        fh.write("the change\n")
    git(wt, "add", "feature.txt")
    git(wt, "commit", "-m", "the change")
    git(wt, "push", "origin", branch)
    sha = git(wt, "rev-parse", "HEAD")
    await worktree.remove_worktree(str(repo), wt, branch)
    return branch, sha


def _remote_feature_branches(repo):
    out = git(repo, "ls-remote", "--heads", "origin")
    return sorted(line.split("refs/heads/", 1)[1] for line in out.splitlines() if "refs/heads/feat/" in line)


async def test_rename_after_pr_resumes_the_original_branch(board, repo):
    fid = board.create_feature("Old title", spec="s")["id"]
    branch, sha = await _first_build_and_push(board, repo, fid)
    assert branch == f"feat/{fid}-old-title"
    board.record_pr_url(fid, "https://github.com/o/r/pull/1")
    board.update_feature(fid, title="New title")

    wt, resumed = await _fix_round(board, repo, fid)

    assert resumed == f"feat/{fid}-old-title"
    assert wt.endswith(f"feat-{fid}-old-title")
    assert git(wt, "rev-parse", "HEAD") == sha  # the PR head, not a fresh tree off base
    assert _remote_feature_branches(repo) == [f"feat/{fid}-old-title"]  # no second branch


async def test_rename_before_first_build_records_the_then_current_title(board, repo):
    fid = board.create_feature("Draft name", spec="s")["id"]
    board.update_feature(fid, title="Final name")
    _wt, branch = await _fix_round(board, repo, fid)
    assert branch == f"feat/{fid}-final-name"
    assert board.get_feature(fid)["branch_slug"] == "final-name"

    board.update_feature(fid, title="Another name")
    await worktree.remove_worktree(str(repo), _wt, branch)
    _wt, again = await _fix_round(board, repo, fid)
    assert again == f"feat/{fid}-final-name"


async def test_restart_reads_the_recorded_branch(board, repo):
    fid = board.create_feature("Old title", spec="s")["id"]
    _branch, sha = await _first_build_and_push(board, repo, fid)
    board.record_pr_url(fid, "https://github.com/o/r/pull/1")
    board.update_feature(fid, title="New title")

    fresh = BeadsBoard(repo=board.repo, actor="test")  # a restarted loop's store
    feature = fresh.get_feature(fid)
    assert feature["branch_slug"] == "old-title"
    ident = await worktree.resolve_branch(feature, cwd=str(repo))
    assert (ident.branch, ident.worktree_dir, ident.source) == (
        f"feat/{fid}-old-title",
        f"feat-{fid}-old-title",
        "recorded",
    )
    wt, branch = await _fix_round(fresh, repo, fid)
    assert branch == f"feat/{fid}-old-title"
    assert git(wt, "rev-parse", "HEAD") == sha


async def test_legacy_card_resolves_from_pr_head_and_records_it(board, repo, fake_gh):
    fid = board.create_feature("Old title", spec="s")["id"]
    # Built before #716: the branch exists on origin, the card has a PR, no slug recorded.
    wt = repo.parent / "legacy"
    git(repo, "worktree", "add", "-b", f"feat/{fid}-old-title", str(wt), "main")
    (wt / "feature.txt").write_text("legacy change\n")
    git(wt, "add", "feature.txt")
    git(wt, "commit", "-m", "legacy change")
    git(wt, "push", "origin", f"feat/{fid}-old-title")
    sha = git(wt, "rev-parse", "HEAD")
    git(repo, "worktree", "remove", "--force", str(wt))
    board.record_pr_url(fid, "https://github.com/o/r/pull/7")
    board.update_feature(fid, title="New title")
    assert board.get_feature(fid)["branch_slug"] is None
    fake_gh["head"] = f"feat/{fid}-old-title"

    wt2, branch = await _fix_round(board, repo, fid)

    assert branch == f"feat/{fid}-old-title"
    assert git(wt2, "rev-parse", "HEAD") == sha
    assert board.get_feature(fid)["branch_slug"] == "old-title"
    assert any("headRefName" in a for a in fake_gh["calls"][0])


async def test_gh_failure_falls_back_to_title_and_records_nothing(board, repo, fake_gh):
    fid = board.create_feature("Old title", spec="s")["id"]
    board.record_pr_url(fid, "https://github.com/o/r/pull/7")
    board.update_feature(fid, title="New title")
    fake_gh["rc"] = 1

    feature = board.get_feature(fid)
    ident = await worktree.resolve_branch(feature, cwd=str(repo))
    assert (ident.branch, ident.source, ident.should_record) == (f"feat/{fid}-new-title", "fallback", False)
    assert await _drive()._branch_title(board, feature, str(repo)) == "New title"
    assert board.get_feature(fid)["branch_slug"] is None


async def test_foreign_pr_head_is_not_recorded(board, repo, fake_gh):
    fid = board.create_feature("Old title", spec="s")["id"]
    board.record_pr_url(fid, "https://github.com/o/r/pull/7")
    fake_gh["head"] = "someone/else"
    ident = await worktree.resolve_branch(board.get_feature(fid), cwd=str(repo))
    assert (ident.branch, ident.source) == (f"feat/{fid}-old-title", "fallback")


def test_record_is_idempotent_and_never_overwrites(board):
    fid = board.create_feature("Old title", spec="s")["id"]
    board.record_branch_slug(fid, "old-title")
    board.record_branch_slug(fid, "old-title")
    got = board.record_branch_slug(fid, "new-title")
    slugs = [l for l in got["labels"] if l.startswith(LABEL_BRANCH_SLUG_PREFIX)]
    assert slugs == ["slug:old-title"]
    assert got["branch_slug"] == "old-title"


def test_empty_slug_records_the_bare_shape(board):
    fid = board.create_feature("!!!", spec="s")["id"]
    got = board.record_branch_slug(fid, "")
    assert got["branch_slug"] == ""
    assert board.record_branch_slug(fid, "later")["branch_slug"] == ""
    assert worktree.branch_name(fid, "Later", slug="") == f"feat/{fid}"
    assert worktree.worktree_dir(fid, "Later", slug="") == f"feat-{fid}"


def test_longest_slug_fits_the_label_cap_and_bad_slugs_are_refused(board):
    fid = board.create_feature("x" * 80, spec="s")["id"]
    slug = worktree.slugify("x" * 80)
    assert len(slug) == 40
    assert board.record_branch_slug(fid, slug)["branch_slug"] == slug  # 45 chars through real br
    for bad in ("feat/x", "Upper", "-lead", "a" * 41):
        with pytest.raises(BoardError):
            board.record_branch_slug(fid, bad)


def test_slug_from_head_shapes():
    assert worktree.slug_from_head("bd-x", "feat/bd-x-old-title") == "old-title"
    assert worktree.slug_from_head("bd-x", "feat/bd-x") == ""
    assert worktree.slug_from_head("bd-x", "feat/bd-y-old") is None
    assert worktree.slug_from_head("bd-x", "feat/bd-x-Bad_Slug") is None
    assert worktree.slug_from_head("bd-x", "") is None
