"""The Ready gate accepts files that exist on a depends_on card's open-PR branch (#537).

Stacked cards: bd-8ac depends on bd-0d4, and bd-0d4's open PR creates the files bd-8ac
edits. Those files don't exist on the base checkout yet, so the phantom-path check used to
refuse bd-8ac — the only workaround was marking them `(new)`, which tells the coder the
file is brand new when it is really about to arrive from the dependency.

Now, for a path missing from the base checkout, the gate consults each depends_on card that
has an open `pr_url` or a feature branch and checks whether the path exists on that card's
branch (`git cat-file -e <ref>:<path>`). WHICH REF: an open PR reflects what is PUSHED, so
origin's copy is authoritative — the gate refreshes it with ONE bounded `git fetch` and reads
the remote-tracking ref `refs/remotes/origin/feat-<dep>`; with no open PR (or no remote) it
reads the local branch refs. A git error counts as "not found" (the normal refusal), never an
exception.

These run REAL git against a bare origin plus a clone, because what `git fetch` /
`git cat-file` see on a branch is exactly the behaviour under test — a faked git cannot show it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from project_board import store as store_mod
from project_board.store import BoardError

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="real git not on PATH")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _identity(repo: str) -> None:
    _git("-C", repo, "config", "user.email", "board-test@localhost")
    _git("-C", repo, "config", "user.name", "Board Test")
    _git("-C", repo, "config", "commit.gpgsign", "false")


def _card(fid, files, *, state="backlog", deps=(), pr_url="", **over):
    base = {
        "id": fid,
        "board_state": state,
        "spec": "s",
        "acceptance_criteria": "- WHEN x THE SYSTEM SHALL y",
        "files_to_modify": list(files),
        "difficulty": "medium",
        "design": "",
        "depends_on": list(deps),
        "created_at": "",
        "project": "",
        "issue_type": "feature",
        "pr_url": pr_url,
    }
    base.update(over)
    return base


def _wire(monkeypatch, board, cards):
    by_id = {c["id"]: c for c in cards}
    monkeypatch.setattr(board, "get_feature", lambda fid: by_id.get(fid))
    monkeypatch.setattr(board, "list_features", lambda *a, **k: list(cards))


@pytest.fixture
def stack(tmp_path: Path):
    """A clone (the project checkout, on `main`) wired to a bare origin, with two dependency
    branches set up to exercise both ref paths:

    - ``feat-dep-a`` carries ``alpha.py`` and lives ONLY on origin (local branch and remote-
      tracking ref removed), so it is reachable only after a fetch — the open-PR path.
    - ``feat-dep-b`` carries ``beta.py`` as a LOCAL branch, never pushed — the no-fetch path.

    The working tree stays on ``main`` (``base.py`` only), so neither stacked file exists in
    the base checkout."""
    origin = str(tmp_path / "origin.git")
    clone = str(tmp_path / "clone")
    _git("init", "--bare", origin)
    _git("init", "-b", "main", clone)
    _identity(clone)
    Path(clone, "base.py").write_text("base\n")
    _git("-C", clone, "add", "-A")
    _git("-C", clone, "commit", "-m", "base commit")
    _git("-C", clone, "remote", "add", "origin", origin)
    _git("-C", clone, "push", "-u", "origin", "main")

    # feat-dep-a: alpha.py, pushed to origin, then removed locally so only the fetch can see it.
    _git("-C", clone, "checkout", "-q", "-b", "feat-dep-a")
    Path(clone, "alpha.py").write_text("alpha\n")
    _git("-C", clone, "add", "-A")
    _git("-C", clone, "commit", "-m", "alpha on dep-a")
    _git("-C", clone, "push", "-q", "origin", "feat-dep-a")
    _git("-C", clone, "checkout", "-q", "main")
    _git("-C", clone, "branch", "-qD", "feat-dep-a")
    subprocess.run(  # drop the remote-tracking ref so the fetch, not a leftover ref, restores it
        ["git", "-C", clone, "update-ref", "-d", "refs/remotes/origin/feat-dep-a"],
        capture_output=True,
        text=True,
    )

    # feat-dep-b: beta.py, a local branch never pushed.
    _git("-C", clone, "checkout", "-q", "-b", "feat-dep-b")
    Path(clone, "beta.py").write_text("beta\n")
    _git("-C", clone, "add", "-A")
    _git("-C", clone, "commit", "-m", "beta on dep-b")
    _git("-C", clone, "checkout", "-q", "main")

    return clone


@requires_git
def test_path_on_an_open_pr_dep_branch_is_accepted_via_the_fetched_remote_ref(stack, make_board, monkeypatch):
    """r1/r4: B depends on dep-a; alpha.py exists only on dep-a's pushed branch. ready_check
    and mark_ready both accept B, with an advisory naming dep-a."""
    b = make_board(lambda *a, **k: [] if k.get("want_json") else "", repo=stack)
    dep = _card("dep-a", ["base.py"], state="in_review", pr_url="https://github.com/o/r/pull/56")
    nxt = _card("bd-8ac", ["alpha.py"], deps=["dep-a"])
    _wire(monkeypatch, b, [dep, nxt])

    check = b.ready_check("bd-8ac")
    assert check["ok"], check["refusals"]
    advice = " ".join(a["message"] for a in check["advisories"])
    assert "dep-a" in advice and "path from dep-a's branch" in advice

    # mark_ready is the same verdict — it must not raise.
    b.mark_ready("bd-8ac")


@requires_git
def test_path_on_a_local_dep_branch_is_accepted_without_a_fetch(stack, make_board, monkeypatch):
    """A dep with no open PR but a local feature branch is read from local refs (no fetch)."""
    b = make_board(lambda *a, **k: [] if k.get("want_json") else "", repo=stack)
    dep = _card("dep-b", ["base.py"], state="in_progress")  # no pr_url
    nxt = _card("bd-9zz", ["beta.py"], deps=["dep-b"])
    _wire(monkeypatch, b, [dep, nxt])

    check = b.ready_check("bd-9zz")
    assert check["ok"], check["refusals"]
    assert "path from dep-b's branch" in " ".join(a["message"] for a in check["advisories"])


@requires_git
def test_same_file_without_a_depends_on_edge_is_still_a_phantom(stack, make_board, monkeypatch):
    """r2: alpha.py exists on dep-a's branch, but a card that does NOT depend on it is still
    refused — the dep branch is only consulted along a real depends_on edge."""
    b = make_board(lambda *a, **k: [] if k.get("want_json") else "", repo=stack)
    orphan = _card("bd-orph", ["alpha.py"])  # no depends_on
    _wire(monkeypatch, b, [orphan])

    check = b.ready_check("bd-orph")
    assert not check["ok"]
    assert [r["gate"] for r in check["refusals"]] == ["phantom-paths"]
    assert "alpha.py" in check["refusals"][0]["message"]
    with pytest.raises(BoardError):
        b.mark_ready("bd-orph")


@requires_git
def test_a_git_error_on_the_dep_branch_falls_back_to_the_phantom_refusal(stack, make_board, monkeypatch):
    """r3: when the git call itself errors, the path is treated as not found — the card gets
    the ordinary phantom-path refusal, no exception escapes."""
    b = make_board(lambda *a, **k: [] if k.get("want_json") else "", repo=stack)
    dep = _card("dep-a", ["base.py"], state="in_review", pr_url="https://github.com/o/r/pull/56")
    nxt = _card("bd-8ac", ["alpha.py"], deps=["dep-a"])
    _wire(monkeypatch, b, [dep, nxt])

    def boom(*a, **k):
        raise OSError("git exploded")

    monkeypatch.setattr(store_mod.subprocess, "run", boom)

    check = b.ready_check("bd-8ac")  # must not raise
    assert not check["ok"]
    assert "phantom-paths" in [r["gate"] for r in check["refusals"]]
    assert "alpha.py" in check["refusals"][0]["message"]
