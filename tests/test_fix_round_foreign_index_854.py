"""A fix round starts from a worktree holding only its own branch (#854).

Card bd-v3y41's fix round 3 opened on a tree that already had ANOTHER card's changes
staged in it. The coder refused to commit them; a coder running ``git add -A && git
commit`` — or the board's own "Preserve retained edits" commit — would have shipped them
inside the wrong PR. These tests drive ``create_worktree(..., resume=True,
integrate=True)`` exactly as ``loop/drive.py`` dispatches a fix round, against a real
bare origin and clone, with a foreign change staged (and, separately, unstaged) in the
card's tree before the round.

Both ways a fix-round tree is produced are covered: the normal remove-and-re-add from the
PR head, and the early return that KEEPS the existing tree (an unpublished recovery
journal, or an unfinished board merge — ``MERGE_HEAD``). The card's own recovery work and
the board's prepared base merge must survive; only the foreign paths go, and they go to a
``stranded/…`` branch first, never into the bin.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from project_board import worktree

FID = "bd-card854"
FOREIGN_NEW = "loop/foreign_card.py"  # a file another card added
FOREIGN_TRACKED = "shared.txt"  # a tracked file only another card changed
OWN = "feature.txt"  # the file this card's branch changes


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE).strip()


def status_paths(path) -> set[str]:
    out = git(path, "status", "--porcelain=v1", "--untracked-files=all")
    return {line[3:] for line in out.splitlines() if line}


def stranded_refs(repo) -> list[str]:
    out = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/stranded/")
    return [line for line in out.splitlines() if line]


@pytest.fixture
def repo(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    clone = tmp_path / "repo"
    git(tmp_path, "clone", str(remote), str(clone))
    git(clone, "config", "user.email", "board@example.test")
    git(clone, "config", "user.name", "Board")
    git(clone, "config", "commit.gpgsign", "false")
    git(clone, "checkout", "-b", "main")
    (clone / ".gitignore").write_text(".worktrees/\n")
    (clone / OWN).write_text("initial\n")
    (clone / FOREIGN_TRACKED).write_text("initial\n")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "initial")
    git(clone, "push", "origin", "main")
    return clone


async def open_pr_branch(repo) -> tuple[str, str]:
    """The card's first round: a tree off base, one commit, pushed — its open PR."""
    wt, branch = await worktree.create_worktree(str(repo), "main", FID)
    with open(f"{wt}/{OWN}", "w") as stream:
        stream.write("card change\n")
    git(wt, "commit", "-am", "card change")
    git(wt, "push", "origin", branch)
    return wt, branch


def plant_foreign(wt, *, staged: bool) -> None:
    """Another card's changes, left in this card's tree."""
    wt_path = Path(wt)
    (wt_path / "loop").mkdir(exist_ok=True)
    (wt_path / FOREIGN_NEW).write_text("another card's module\n")
    (wt_path / FOREIGN_TRACKED).write_text("another card's edit\n")
    if staged:
        git(wt, "add", FOREIGN_NEW, FOREIGN_TRACKED)


async def fix_round(repo):
    # Exactly how loop/drive.py dispatches a fix round on a card with an open PR.
    return await worktree.create_worktree(str(repo), "main", FID, resume=True, integrate=True)


def assert_foreign_gone(wt, branch) -> None:
    assert not {FOREIGN_NEW, FOREIGN_TRACKED} & status_paths(wt)
    assert not {FOREIGN_NEW, FOREIGN_TRACKED} & set(git(wt, "diff", "--cached", "--name-only").splitlines())
    committed = set(git(wt, "diff", "--name-only", f"origin/{branch}", "HEAD").splitlines())
    assert not {FOREIGN_NEW, FOREIGN_TRACKED} & committed
    assert open(f"{wt}/{FOREIGN_TRACKED}").read() == "initial\n"


def assert_saved(repo) -> None:
    """The foreign changes were moved aside, not destroyed."""
    refs = stranded_refs(repo)
    assert refs, "foreign changes must be saved to a stranded/… branch before they are cleared"
    tip = refs[-1]
    assert git(repo, "show", f"{tip}:{FOREIGN_NEW}") == "another card's module"
    assert git(repo, "show", f"{tip}:{FOREIGN_TRACKED}") == "another card's edit"


@pytest.mark.parametrize("staged", [True, False], ids=["staged", "unstaged"])
async def test_normal_path_rebuilds_from_the_pr_head_without_foreign_changes(repo, staged):
    wt, branch = await open_pr_branch(repo)
    plant_foreign(wt, staged=staged)

    wt, branch = await fix_round(repo)

    assert_foreign_gone(wt, branch)
    assert git(wt, "rev-parse", "HEAD") == git(wt, "rev-parse", f"origin/{branch}")
    assert_saved(repo)


@pytest.mark.parametrize("staged", [True, False], ids=["staged", "unstaged"])
async def test_kept_recovery_tree_drops_foreign_changes_before_the_round(repo, staged):
    wt, branch = await open_pr_branch(repo)
    # A previous fix round left the board's recovery journal behind.
    await fix_round(repo)
    assert await worktree.recovery_context(wt, branch)
    plant_foreign(wt, staged=staged)
    # The journal plus a dirty tree is what keeps the existing tree as it is.
    assert await worktree.pending_integration(str(repo), ".worktrees", FID, "")

    wt2, branch2 = await fix_round(repo)

    assert (wt2, branch2) == (wt, branch)
    assert_foreign_gone(wt, branch)
    assert git(wt, "rev-parse", "HEAD") == git(wt, "rev-parse", f"origin/{branch}")
    assert_saved(repo)


async def test_card_own_recovery_edits_survive_while_foreign_ones_go(repo):
    wt, branch = await open_pr_branch(repo)
    await fix_round(repo)
    # An interrupted round's own unpublished edit, on a file the card's branch changes.
    with open(f"{wt}/{OWN}", "w") as stream:
        stream.write("card change\nown recovery work\n")
    plant_foreign(wt, staged=True)

    wt2, branch2 = await fix_round(repo)

    assert (wt2, branch2) == (wt, branch)
    assert not {FOREIGN_NEW, FOREIGN_TRACKED} & status_paths(wt)
    assert not {FOREIGN_NEW, FOREIGN_TRACKED} & set(
        git(wt, "diff", "--name-only", f"origin/{branch}", "HEAD").splitlines()
    )
    # The card's own work is kept (the board preserves it as feature history).
    assert open(f"{wt}/{OWN}").read() == "card change\nown recovery work\n"
    assert_saved(repo)


async def test_board_merge_in_progress_keeps_base_changes_and_drops_foreign(repo):
    wt, branch = await open_pr_branch(repo)
    # The base moved: another card merged a file into main.
    git(repo, "checkout", "main")
    (repo / "merged_sibling.txt").write_text("merged into main\n")
    git(repo, "add", "merged_sibling.txt")
    git(repo, "commit", "-m", "sibling card merged")
    git(repo, "push", "origin", "main")
    # The previous fix round's board integration is still pending — its base changes staged.
    await fix_round(repo)
    assert "merged_sibling.txt" in git(wt, "diff", "--cached", "--name-only").splitlines()
    plant_foreign(wt, staged=True)

    wt2, _branch = await fix_round(repo)

    assert wt2 == wt
    assert not {FOREIGN_NEW, FOREIGN_TRACKED} & status_paths(wt)
    # The board's prepared base merge is NOT foreign: it stays staged, MERGE_HEAD intact.
    assert "merged_sibling.txt" in git(wt, "diff", "--cached", "--name-only").splitlines()
    git(wt, "rev-parse", "--verify", "MERGE_HEAD")
    assert_saved(repo)
