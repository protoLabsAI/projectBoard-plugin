"""The Ready gate checks files_to_modify against the card's BASE BRANCH, not just the tree (#702).

A board project can target an epic branch (`base_branch: epic/x`) while its team checkout
has `main` checked out. A file that exists only on the epic is real for the coder — its
worktree is cut from the epic — but the phantom-path check used to read the checkout's
working tree, so it refused the card. The only way through was marking the file `(new)`,
which tells the coder an existing file is brand new.

Now a path passes when it exists in `origin/<base>`, in the local `<base>` branch, or in the
working tree. All three are LOCAL reads: the gate never fetches.

These run REAL git against a bare origin plus a clone, because what `git cat-file` sees on a
ref the checkout doesn't have checked out is exactly the behaviour under test.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from project_board import store as store_mod
from project_board.store import BoardError

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="real git not on PATH")

EPIC = "epic/x"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _identity(repo: str) -> None:
    _git("-C", repo, "config", "user.email", "board-test@localhost")
    _git("-C", repo, "config", "user.name", "Board Test")
    _git("-C", repo, "config", "commit.gpgsign", "false")


def _card(fid, files, *, state="backlog", project="epic", **over):
    base = {
        "id": fid,
        "board_state": state,
        "spec": "s",
        "acceptance_criteria": "- WHEN x THE SYSTEM SHALL y",
        "files_to_modify": list(files),
        "difficulty": "medium",
        "design": "",
        "depends_on": [],
        "created_at": "",
        "project": project,
        "issue_type": "feature",
        "pr_url": "",
    }
    base.update(over)
    return base


def _wire(monkeypatch, board, cards):
    by_id = {c["id"]: c for c in cards}
    monkeypatch.setattr(board, "get_feature", lambda fid: by_id.get(fid))
    monkeypatch.setattr(board, "list_features", lambda *a, **k: list(cards))


def _gates(check: dict) -> list[str]:
    return [r["gate"] for r in check["refusals"]]


@pytest.fixture
def checkout(tmp_path: Path) -> str:
    """A clone (the project checkout) wired to a bare origin. ``main`` has ``a.py``;
    ``epic/x`` adds ``only_on_epic.py``. Both are pushed, then ``main`` is checked out and the
    LOCAL ``epic/x`` branch is deleted — so ``only_on_epic.py`` is not in the working tree and
    is reachable only through the remote-tracking ref ``origin/epic/x``."""
    origin = str(tmp_path / "origin.git")
    clone = str(tmp_path / "clone")
    _git("init", "--bare", origin)
    _git("init", "-b", "main", clone)
    _identity(clone)
    Path(clone, "a.py").write_text("a\n")
    _git("-C", clone, "add", "-A")
    _git("-C", clone, "commit", "-m", "a on main")
    _git("-C", clone, "remote", "add", "origin", origin)
    _git("-C", clone, "push", "-q", "-u", "origin", "main")

    _git("-C", clone, "checkout", "-q", "-b", EPIC)
    Path(clone, "only_on_epic.py").write_text("epic\n")
    _git("-C", clone, "add", "-A")
    _git("-C", clone, "commit", "-m", "only_on_epic on the epic")
    _git("-C", clone, "push", "-q", "-u", "origin", EPIC)
    _git("-C", clone, "checkout", "-q", "main")
    _git("-C", clone, "branch", "-qD", EPIC)

    assert not Path(clone, "only_on_epic.py").exists()
    return clone


@pytest.fixture
def epic_board(checkout, make_board):
    """A board whose ``epic`` project is bound to the checkout with ``base_branch: epic/x``.
    The board's own repo is the same checkout, on the instance default ``main``."""

    def _make(**project_over):
        b = make_board(lambda *a, **k: [] if k.get("want_json") else "", repo=checkout)
        b.projects = {"epic": {"repo": checkout, "base_branch": EPIC, **project_over}}
        b.default_project = "epic"
        return b

    return _make


@requires_git
def test_a_path_only_on_the_origin_base_branch_passes_with_main_checked_out(checkout, epic_board, monkeypatch):
    """(a) only_on_epic.py exists on origin/epic/x and nowhere in the working tree (main is
    checked out, no local epic branch). The card targets the epic, so the file is real."""
    b = epic_board()
    _wire(monkeypatch, b, [_card("bd-epic", ["only_on_epic.py", "a.py"])])

    check = b.ready_check("bd-epic")
    assert "phantom-paths" not in _gates(check), check["refusals"]
    assert check["ok"], check["refusals"]
    b.mark_ready("bd-epic")  # same verdict: must not raise

    # The gate read refs only — it did not check the epic out or create a local branch.
    assert _git("-C", checkout, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _git("-C", checkout, "branch", "--list", EPIC) == ""


@requires_git
def test_a_path_only_on_the_local_base_branch_passes(checkout, epic_board, monkeypatch):
    """(b) No origin ref at all: the epic exists only as a LOCAL branch (never pushed, or the
    checkout has no remote). The local branch is read instead."""
    _git("-C", checkout, "branch", "-q", EPIC, f"origin/{EPIC}")
    _git("-C", checkout, "remote", "remove", "origin")  # drops every remote-tracking ref
    assert _git("-C", checkout, "for-each-ref", "refs/remotes") == ""

    b = epic_board()
    _wire(monkeypatch, b, [_card("bd-local", ["only_on_epic.py"])])

    check = b.ready_check("bd-local")
    assert check["ok"], check["refusals"]


@requires_git
def test_a_path_on_no_ref_and_not_in_the_tree_is_still_a_phantom(checkout, epic_board, monkeypatch):
    """(c) A guessed path that is on neither origin/<base>, nor local <base>, nor disk is
    still refused, naming the path."""
    b = epic_board()
    _wire(monkeypatch, b, [_card("bd-ghost", ["only_on_epic.py", "nowhere.py"])])

    check = b.ready_check("bd-ghost")
    assert not check["ok"]
    assert _gates(check) == ["phantom-paths"]
    assert "nowhere.py" in check["refusals"][0]["message"]
    assert "only_on_epic.py" not in check["refusals"][0]["message"]
    with pytest.raises(BoardError):
        b.mark_ready("bd-ghost")


@requires_git
def test_a_path_on_another_branch_but_not_the_cards_base_is_still_a_phantom(checkout, make_board, monkeypatch):
    """(c) The base that counts is the CARD's: a card whose project targets main does not get
    to name a file that exists only on the epic."""
    b = make_board(lambda *a, **k: [] if k.get("want_json") else "", repo=checkout)
    b.projects = {"trunk": {"repo": checkout, "base_branch": "main"}}
    b.default_project = "trunk"
    _wire(monkeypatch, b, [_card("bd-trunk", ["only_on_epic.py"], project="trunk")])

    check = b.ready_check("bd-trunk")
    assert _gates(check) == ["phantom-paths"]


@requires_git
def test_an_untracked_working_tree_file_still_passes(checkout, epic_board, monkeypatch):
    """(d) A file on disk but on no ref (untracked) passes, exactly as before."""
    Path(checkout, "scratch").mkdir()
    Path(checkout, "scratch", "untracked.py").write_text("wip\n")
    b = epic_board()
    _wire(monkeypatch, b, [_card("bd-wip", ["scratch/untracked.py"])])

    check = b.ready_check("bd-wip")
    assert check["ok"], check["refusals"]


@requires_git
def test_a_new_marker_still_bypasses_the_check(checkout, epic_board, monkeypatch):
    """(f) `(new)` declares the file doesn't exist yet — no ref or disk lookup applies."""
    b = epic_board()
    _wire(monkeypatch, b, [_card("bd-new", ["brand_new.py (new)", "pkg/also_new.py (NEW)"])])

    check = b.ready_check("bd-new")
    assert check["ok"], check["refusals"]


@requires_git
def test_rehoming_a_ready_card_accepts_a_path_on_the_target_projects_base_branch(checkout, tmp_path, make_board):
    """(e) Moving a ready card to a project whose base_branch is the epic: the path check
    runs against the TARGET project's base, so only_on_epic.py is accepted there — and a path
    on no ref is still refused."""
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "only_on_epic.py").write_text("old home\n")
    b = make_board(lambda *a, **k: [] if k.get("want_json") else "", repo=str(legacy))
    b.projects = {
        "old": {"repo": str(legacy), "base_branch": "main"},
        "epic": {"repo": checkout, "base_branch": EPIC},
        "trunk": {"repo": checkout, "base_branch": "main"},
    }
    b.default_project = "old"
    card = _card("bd-move", ["only_on_epic.py"], state="ready", project="old")

    assert b._rehome_refusal(card, "epic") == ""
    # Same checkout, but a project that targets main: the file is not on ITS base.
    assert "only_on_epic.py" in b._rehome_refusal(card, "trunk")
    # A path on no ref is refused in the epic project too; `(new)` still bypasses.
    assert "nowhere.py" in b._rehome_refusal(card, "epic", files_to_modify=["nowhere.py"])
    assert b._rehome_refusal(card, "epic", files_to_modify=["nowhere.py (new)"]) == ""


@requires_git
def test_the_base_branch_lookup_never_fetches(checkout, epic_board, monkeypatch):
    """The Ready gate stays offline: resolving paths against the base branch runs no
    `git fetch` — even for a path it ends up refusing."""
    calls: list[tuple[str, ...]] = []
    real = store_mod._run_git

    def _spy(repo, *args):
        calls.append(args)
        return real(repo, *args)

    monkeypatch.setattr(store_mod, "_run_git", _spy)
    b = epic_board()
    _wire(monkeypatch, b, [_card("bd-off", ["only_on_epic.py", "nowhere.py"])])

    b.ready_check("bd-off")
    assert calls, "the base-branch refs were never read"
    assert not [c for c in calls if c and c[0] == "fetch"]


def test_a_plain_directory_keeps_the_working_tree_check(tmp_path, make_board, monkeypatch):
    """A repo that is not a git checkout behaves as before: on disk passes, absent is refused."""
    (tmp_path / "here.py").write_text("x\n")
    b = make_board(lambda *a, **k: [] if k.get("want_json") else "", repo=str(tmp_path))
    _wire(monkeypatch, b, [_card("bd-plain", ["here.py"], project=""), _card("bd-gone", ["gone.py"], project="")])

    assert b.ready_check("bd-plain")["ok"]
    assert _gates(b.ready_check("bd-gone")) == ["phantom-paths"]


@requires_git
def test_base_branch_resolution_follows_the_loops_order(checkout, make_board):
    """The card's project entry wins, then the card's stamped base_branch, then the default
    project's, then the board's own, then main — the order the loop builds against."""
    b = make_board(lambda *a, **k: [] if k.get("want_json") else "", repo=checkout, base_branch="develop")
    b.projects = {"epic": {"repo": checkout, "base_branch": EPIC}, "bare": {"repo": checkout}}
    b.default_project = "epic"

    assert b._base_branch_for({"project": "epic", "base_branch": "stamped"}) == EPIC
    assert b._base_branch_for({"project": "bare", "base_branch": "stamped"}) == "stamped"
    assert b._base_branch_for({"project": "bare"}) == "develop"
    assert b._base_branch_for({"project": ""}) == EPIC  # the default project's
    assert b._base_branch_for({"project": "unknown"}) == EPIC  # unknown label → the default's
    b.projects, b.default_project, b.base_branch = {}, "", ""
    assert b._base_branch_for({}) == "main"
