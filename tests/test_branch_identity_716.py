"""#716: a card's branch is recorded once, so renaming the card never moves it.

Real git (a working clone of a bare origin) and the REAL `br` store; only `gh` is faked.
The bug: the branch/dir names were derived from the CURRENT title, so a card renamed after
its PR opened sent the fix round to `feat/<id>-<new-slug>` — absent on origin — and
`create_worktree` fell back to base: the coder rebuilt on a new branch, a second PR opened
and the original was orphaned.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest
from conftest import REAL_SEAMS

from project_board import store as store_mod
from project_board import worktree
from project_board.loop.attach import attach_external_pr
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


# ── #716 (2/3): attaching a PR checks the card's recorded branch, not its new title ──

_AC = "- WHEN x THE SYSTEM SHALL y"
_PR = "https://github.com/o/r/pull/9"


def _ready_card(board, title: str) -> dict:
    """A coding card that passed the real Ready gate (attach refuses anything else)."""
    (pathlib.Path(board.repo) / "mod.py").write_text("x = 1\n")
    fid = board.create_feature(title, spec="s", acceptance_criteria=_AC, files_to_modify=["mod.py"])["id"]
    return board.mark_ready(fid)


@pytest.fixture
def fake_pr(monkeypatch):
    """Fake only the `gh` reads the attach makes: the PR's identity and the repo slug."""
    pr = {"url": _PR, "state": "OPEN", "head": "", "base": "main", "cross_repo": False}

    async def _identity(url, *, cwd="."):
        return dict(pr)

    async def _slug(*, cwd="."):
        return "o/r"

    monkeypatch.setattr(worktree, "pr_identity", _identity)
    monkeypatch.setattr(worktree, "repo_slug", _slug)
    return pr


async def _attach(board, fid):
    return await attach_external_pr(board, board.get_feature(fid), _PR, repo=board.repo, base="main")


async def test_attach_after_rename_checks_the_recorded_branch(board, fake_pr):
    fid = _ready_card(board, "Old title")["id"]
    board.record_branch_slug(fid, "old-title")
    board.update_feature(fid, title="New title")
    fake_pr["head"] = f"feat/{fid}-old-title"

    result = await _attach(board, fid)

    assert result["state"] == "in_review" and result["pr_url"] == _PR
    f = board.get_feature(fid)
    assert f["branch_slug"] == "old-title"
    assert worktree.branch_name(fid, worktree.branch_title_for(f)) == f"feat/{fid}-old-title"


async def test_attach_after_rename_refuses_the_new_title_branch_once_recorded(board, fake_pr):
    fid = _ready_card(board, "Old title")["id"]
    board.record_branch_slug(fid, "old-title")
    board.update_feature(fid, title="New title")
    fake_pr["head"] = f"feat/{fid}-new-title"

    with pytest.raises(BoardError) as exc:
        await _attach(board, fid)
    assert f"push the work to branch 'feat/{fid}-old-title'" in str(exc.value)


async def test_legacy_card_attach_adopts_and_records_the_pr_head_slug(board, fake_pr):
    fid = _ready_card(board, "Old title")["id"]
    board.update_feature(fid, title="New title")
    assert board.get_feature(fid)["branch_slug"] is None
    fake_pr["head"] = f"feat/{fid}-old-title"

    result = await _attach(board, fid)

    assert result["state"] == "in_review"
    assert board.get_feature(fid)["branch_slug"] == "old-title"


async def test_legacy_card_attach_survives_a_failed_slug_record(board, fake_pr, monkeypatch):
    fid = _ready_card(board, "Old title")["id"]
    fake_pr["head"] = f"feat/{fid}-older-title"

    def _boom(*_a, **_k):
        raise BoardError("br is busy")

    monkeypatch.setattr(board, "record_branch_slug", _boom)
    result = await _attach(board, fid)
    assert result["state"] == "in_review"
    assert board.get_feature(fid)["branch_slug"] is None


async def test_legacy_card_attach_still_refuses_a_non_canonical_head(board, fake_pr):
    fid = _ready_card(board, "Old title")["id"]
    fake_pr["head"] = "someone/else"

    with pytest.raises(BoardError) as exc:
        await _attach(board, fid)
    msg = str(exc.value)
    assert "is on branch 'someone/else', not" in msg and "canonical branch" in msg
    assert f"push the work to branch 'feat/{fid}-old-title'" in msg
    f = board.get_feature(fid)
    assert f["branch_slug"] is None and f["board_state"] != "in_review"


async def test_legacy_card_refuses_another_cards_branch(board, fake_pr):
    fid = _ready_card(board, "Old title")["id"]
    fake_pr["head"] = f"feat/{fid}x-old-title"
    with pytest.raises(BoardError):
        await _attach(board, fid)
    assert board.get_feature(fid)["branch_slug"] is None


def test_branch_title_for_prefers_a_valid_recorded_slug_without_gh(monkeypatch):
    async def _no_gh(*_a, **_k):  # pragma: no cover — the helper must never reach gh
        raise AssertionError("branch_title_for called gh")

    monkeypatch.setattr(worktree, "_gh", _no_gh)
    monkeypatch.setattr(worktree, "pr_head_branch", _no_gh)
    card = {"id": "bd-x", "title": "New title", "pr_url": _PR}
    assert worktree.branch_title_for({**card, "branch_slug": "old-title"}) == "old-title"
    assert worktree.branch_title_for({**card, "branch_slug": ""}) == ""
    assert worktree.branch_title_for({**card, "branch_slug": None}) == "New title"
    assert worktree.branch_title_for({**card, "branch_slug": "Bad/Slug"}) == "New title"
    assert worktree.branch_title_for({"id": "bd-x"}) == ""
    assert worktree.branch_name("bd-x", worktree.branch_title_for({**card, "branch_slug": ""})) == "feat/bd-x"


# Recovery must share the same identity as build and attach, including after restart.
def _recovery_card(repo):
    board = BeadsBoard(repo=str(repo), actor="test")
    fid = _ready_card(board, "Old title")["id"]
    assert board.claim(fid)
    return board, fid


def _recovery_loop(repo):
    from project_board.loop import BoardLoop

    # Use the actual store through the loop's normal configuration.
    return BoardLoop(
        {
            "db_path": str(repo / ".beads" / "beads.db"),
            "repo": str(repo),
            "base_branch": "main",
            "coder": "proto",
            "local_gate_cmd": "",
        }
    )


@pytest.mark.parametrize("linked", [False, True])
async def test_renamed_orphan_adopts_original_pr(repo, monkeypatch, linked):
    board, fid = _recovery_card(repo)
    branch, sha = await _first_build_and_push(board, repo, fid)
    if linked:
        board.record_pr_url(fid, _PR)
    board.update_feature(fid, title="New title")
    seen = []

    async def gh(*args, cwd, timeout=60):
        assert args[:2] == ("pr", "view")
        seen.append(args[2])
        return (0, _PR, "") if args[2] == branch else (1, "", "no PR")

    monkeypatch.setattr(worktree, "_gh", gh)
    await _recovery_loop(repo)._reconcile_orphan(fid)

    assert seen == [branch]
    assert board.get_feature(fid)["board_state"] == "in_review"
    assert board.get_feature(fid)["pr_url"] == _PR
    assert git(repo, "rev-parse", f"origin/{branch}") == sha
    assert _remote_feature_branches(repo) == [branch]


async def test_renamed_stranded_resume_and_reap_keep_original_commit(repo):
    board, fid = _recovery_card(repo)
    branch, sha = await _first_build_and_push(board, repo, fid)
    wt, _ = await worktree.create_worktree(str(repo), "main", fid, ROOT, title="old-title", resume=True)
    # A shutdown saves the current checkout and persists its resume record.
    saved = await worktree.preserve_worktree(str(repo), wt, branch)
    board.record_stranded_work(fid, ref=saved.ref, sha=git(repo, "rev-parse", saved.ref))
    await worktree.remove_worktree(str(repo), wt, branch)
    board.update_feature(fid, title="New title")
    loop = _recovery_loop(repo)

    assert await loop._resume_stranded_work(board, fid)
    assert board.get_feature(fid)["board_state"] == "ready"
    assert pathlib.Path(wt).is_dir()
    assert git(wt, "branch", "--show-current") == branch
    assert git(wt, "rev-parse", "HEAD") == sha
    assert await loop._reap_feature_worktree(str(repo), fid)
    assert not pathlib.Path(wt).exists()
    assert git(repo, "rev-parse", f"origin/{branch}") == sha
    assert _remote_feature_branches(repo) == [branch]
    assert not git(repo, "branch", "--list", f"feat/{fid}-new-title")


async def test_renamed_orphan_resumes_retained_integration_before_adopting_pr(repo, monkeypatch):
    board, fid = _recovery_card(repo)
    branch, _ = await _first_build_and_push(board, repo, fid)
    board.record_pr_url(fid, _PR)
    wt, _ = await _fix_round(board, repo, fid)
    # A real pending merge on the original checkout survives the card rename.
    (repo / "sibling").write_text("sibling change\n")
    git(repo, "add", "sibling")
    git(repo, "commit", "-m", "sibling change")
    git(repo, "push", "origin", "main")
    git(wt, "merge", "--no-commit", "--no-ff", "main")
    board.update_feature(fid, title="New title")

    async def gh(*args, **kwargs):
        pytest.fail("pending repair must be resumed before adopting the existing PR")

    monkeypatch.setattr(worktree, "_gh", gh)
    await _recovery_loop(repo)._reconcile_orphan(fid)

    assert board.get_feature(fid)["board_state"] == "ready"
    assert board.get_feature(fid)["pr_url"] == _PR
    assert await worktree.pending_integration(str(repo), ROOT, fid, "old-title") == (wt, branch)
    assert _remote_feature_branches(repo) == [branch]

    # Restart through publication: the existing PR receives the repaired head.
    restarted = BeadsBoard(repo=str(repo), actor="test")
    resumed, actual = await _fix_round(restarted, repo, fid)
    assert (resumed, actual) == (wt, branch)
    await worktree.finish_integration(wt, branch, "main")

    async def existing_pr(*args, cwd, timeout=60):
        if args[:2] == ("pr", "create"):
            assert args[args.index("--head") + 1] == branch
            return 1, "", "a pull request already exists"
        assert args[:3] == ("pr", "view", branch)
        return 0, _PR, ""

    monkeypatch.setattr(worktree, "_gh", existing_pr)
    assert await worktree.open_pr(wt, branch, title="New title", base="main", promote_draft=False) == _PR
    assert git(repo, "rev-parse", f"origin/{branch}") == git(wt, "rev-parse", "HEAD")
    assert git(wt, "show", "HEAD:feature.txt") == "the change"
    assert git(wt, "show", "HEAD:sibling") == "sibling change"
    assert _remote_feature_branches(repo) == [branch]
    assert await worktree.pending_integration(str(repo), ROOT, fid, "old-title") is None
