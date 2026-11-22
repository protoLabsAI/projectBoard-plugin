"""The Ready gate accepts a path an unfinished depends_on card declares `(new)` (#702, 2/2).

A chain boarded up front: card A creates `x.py`, card B depends on A and edits `x.py`. Until
A is built there is no branch to read, so the phantom-path check refused B — the only way
through was marking `x.py` `(new)` on B, which tells B's coder an existing-to-be file is
brand new.

Now a still-phantom path passes when an OPEN, unblocked `depends_on` card lists that path
`(new)` in its own `files_to_modify`. What a dependency's state grants:

- cancelled: nothing — not its declarations, and not its leftover `feat/<id>` branch either;
- blocked: its branch (as #537), not its declarations;
- done: its branch (as #537), not its declarations — the path must be on the base by then;
- anything else: its branch, then its `(new)` declarations.

These run REAL git against a bare origin plus a clone, the same way
tests/test_ready_gate_base_branch_702.py and tests/test_ready_gate_stacked_537.py do.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from project_board import store as store_mod
from project_board.store import BoardError

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="real git not on PATH")

A_BRANCH = "feat/dep-a"


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
        "blocked": state == "blocked",
    }
    base.update(over)
    return base


def _wire(monkeypatch, board, cards):
    by_id = {c["id"]: c for c in cards}
    monkeypatch.setattr(board, "get_feature", lambda fid: by_id.get(fid))
    monkeypatch.setattr(board, "list_features", lambda *a, **k: list(cards))


def _gates(check: dict) -> list[str]:
    return [r["gate"] for r in check["refusals"]]


def _path_advice(check: dict) -> str:
    return " ".join(a["message"] for a in check["advisories"] if a["gate"] == "phantom-paths")


def _commit_on(clone: str, branch: str, name: str, *, push: bool) -> None:
    """Commit ``name`` on ``branch`` (created from the current HEAD when new), optionally
    push it, and return to ``main``."""
    exists = _git("-C", clone, "branch", "--list", branch)
    _git("-C", clone, "checkout", "-q", *(() if exists else ("-b",)), branch)
    Path(clone, name).write_text(f"{name}\n")
    _git("-C", clone, "add", "-A")
    _git("-C", clone, "commit", "-q", "-m", f"{name} on {branch}")
    if push:
        _git("-C", clone, "push", "-q", "origin", branch)
    if branch != "main":
        _git("-C", clone, "checkout", "-q", "main")


@pytest.fixture
def checkout(tmp_path: Path) -> str:
    """A clone (the project checkout, ``main`` checked out) wired to a bare origin. ``main``
    has only ``base.py`` — ``x.py`` and ``y.py`` exist nowhere until a test puts them there."""
    origin = str(tmp_path / "origin.git")
    clone = str(tmp_path / "clone")
    _git("init", "--bare", origin)
    _git("init", "-b", "main", clone)
    _identity(clone)
    Path(clone, "base.py").write_text("base\n")
    _git("-C", clone, "add", "-A")
    _git("-C", clone, "commit", "-m", "base commit")
    _git("-C", clone, "remote", "add", "origin", origin)
    _git("-C", clone, "push", "-q", "-u", "origin", "main")
    return clone


@pytest.fixture
def board(checkout, make_board):
    return make_board(lambda *a, **k: [] if k.get("want_json") else "", repo=checkout)


@requires_git
@pytest.mark.parametrize("dep_state", ["backlog", "ready", "in_progress"])
def test_a_path_an_unbuilt_dependency_declares_new_is_accepted(board, monkeypatch, dep_state):
    """(a) A has no branch yet and declares `x.py (new)`. B depends on A and lists the real
    path `x.py`: no phantom-paths refusal, and an advisory names A."""
    a = _card("dep-a", ["x.py (new)", "base.py"], state=dep_state)
    b = _card("bd-b", ["x.py", "base.py"], deps=["dep-a"])
    _wire(monkeypatch, board, [a, b])

    check = board.ready_check("bd-b")
    assert "phantom-paths" not in _gates(check), check["refusals"]
    assert check["ok"], check["refusals"]
    advice = _path_advice(check)
    assert "x.py" in advice and "will be created by 'dep-a'" in advice and "declared (new)" in advice
    board.mark_ready("bd-b")  # same verdict: must not raise


@requires_git
def test_the_declaration_matches_through_marker_case_and_a_leading_dot_slash(board, monkeypatch):
    """Both sides are compared with their `(new)` marker and any `./` stripped."""
    a = _card("dep-a", ["./pkg/x.py (NEW)"], state="ready")
    b = _card("bd-b", ["pkg/x.py"], deps=["dep-a"])
    _wire(monkeypatch, board, [a, b])

    assert board.ready_check("bd-b")["ok"]


@requires_git
def test_an_unrelated_missing_path_is_still_refused_alone(board, monkeypatch):
    """(b) The declaration covers x.py only: y.py, which nobody creates, is still a phantom —
    and the refusal names y.py, not x.py."""
    a = _card("dep-a", ["x.py (new)"], state="ready")
    b = _card("bd-b", ["x.py", "y.py"], deps=["dep-a"])
    _wire(monkeypatch, board, [a, b])

    check = board.ready_check("bd-b")
    assert _gates(check) == ["phantom-paths"]
    message = check["refusals"][0]["message"]
    assert "y.py" in message and "x.py" not in message
    assert "will be created by 'dep-a'" in _path_advice(check)
    with pytest.raises(BoardError):
        board.mark_ready("bd-b")


@requires_git
def test_a_path_the_dependency_only_modifies_grants_nothing(board, monkeypatch):
    """A's entry without `(new)` is not a promise to create the file."""
    a = _card("dep-a", ["x.py"], state="ready")
    b = _card("bd-b", ["x.py"], deps=["dep-a"])
    _wire(monkeypatch, board, [a, b])

    assert _gates(board.ready_check("bd-b")) == ["phantom-paths"]


@requires_git
@pytest.mark.parametrize("push", [False, True], ids=["local-branch", "pushed-branch-with-pr"])
def test_a_cancelled_dependency_grants_nothing_even_with_its_branch(checkout, board, monkeypatch, push):
    """(c) A was cancelled, but its feat/<A-id> branch — carrying x.py — still exists (and A
    still declares `x.py (new)`). Neither grants the path: A's work will never land."""
    _commit_on(checkout, A_BRANCH, "x.py", push=push)
    a = _card(
        "dep-a",
        ["x.py (new)"],
        state="cancelled",
        pr_url="https://github.com/o/r/pull/7" if push else "",
    )
    b = _card("bd-b", ["x.py"], deps=["dep-a"])
    _wire(monkeypatch, board, [a, b])

    check = board.ready_check("bd-b")
    assert _gates(check) == ["phantom-paths"]
    assert "x.py" in check["refusals"][0]["message"]
    assert _path_advice(check) == ""
    with pytest.raises(BoardError):
        board.mark_ready("bd-b")


@requires_git
def test_an_archived_cancelled_dependency_grants_nothing_either(checkout, board, monkeypatch):
    """A cancelled card that aged out of the default listing is read by id, so its leftover
    branch still grants nothing."""
    _commit_on(checkout, A_BRANCH, "x.py", push=False)
    a = _card("dep-a", ["x.py (new)"], state="cancelled")
    b = _card("bd-b", ["x.py"], deps=["dep-a"])
    by_id = {"dep-a": a, "bd-b": b}
    monkeypatch.setattr(board, "get_feature", lambda fid: by_id.get(fid))
    monkeypatch.setattr(board, "list_features", lambda *args, **k: [b])  # A is archived

    assert _gates(board.ready_check("bd-b")) == ["phantom-paths"]


@requires_git
def test_a_blocked_dependency_with_no_branch_grants_nothing(board, monkeypatch):
    """(d) A is blocked and was never built: its `(new)` declaration is not a live promise."""
    a = _card("dep-a", ["x.py (new)"], state="blocked")
    b = _card("bd-b", ["x.py"], deps=["dep-a"])
    _wire(monkeypatch, board, [a, b])

    check = board.ready_check("bd-b")
    assert _gates(check) == ["phantom-paths"]
    assert "x.py" in check["refusals"][0]["message"]
    assert _path_advice(check) == ""


@requires_git
def test_a_blocked_dependencys_branch_still_counts(checkout, board, monkeypatch):
    """A blocked dependency that DID build keeps the #537 branch relief, unchanged."""
    _commit_on(checkout, A_BRANCH, "x.py", push=False)
    a = _card("dep-a", ["x.py (new)"], state="blocked")
    b = _card("bd-b", ["x.py"], deps=["dep-a"])
    _wire(monkeypatch, board, [a, b])

    check = board.ready_check("bd-b")
    assert check["ok"], check["refusals"]
    assert "path from dep-a's branch" in _path_advice(check)


@requires_git
def test_a_done_dependency_whose_file_is_on_the_base_passes_without_dependency_advice(checkout, board, monkeypatch):
    """(e) A merged: x.py is on origin/main (the local checkout has not pulled it). B passes
    on the base-branch read alone — no dependency advisory is needed or given."""
    _commit_on(checkout, "main", "x.py", push=True)
    _git("-C", checkout, "reset", "-q", "--hard", "HEAD~1")  # local main is behind origin
    assert not Path(checkout, "x.py").exists()
    a = _card("dep-a", ["x.py (new)"], state="done")
    b = _card("bd-b", ["x.py"], deps=["dep-a"])
    _wire(monkeypatch, board, [a, b])

    check = board.ready_check("bd-b")
    assert check["ok"], check["refusals"]
    assert _path_advice(check) == ""


@requires_git
def test_a_done_dependency_whose_file_is_nowhere_grants_nothing(board, monkeypatch):
    """(e) A is done, yet x.py is on neither the base nor a branch of A's: the declaration of
    a finished card grants nothing, so B is refused."""
    a = _card("dep-a", ["x.py (new)"], state="done")
    b = _card("bd-b", ["x.py"], deps=["dep-a"])
    _wire(monkeypatch, board, [a, b])

    check = board.ready_check("bd-b")
    assert _gates(check) == ["phantom-paths"]
    assert "x.py" in check["refusals"][0]["message"]


@requires_git
def test_without_a_depends_on_edge_the_declaration_grants_nothing(board, monkeypatch):
    """(f) A declares `x.py (new)`, but B does not depend on A — nothing orders B after A, so
    A's declaration is not consulted."""
    a = _card("dep-a", ["x.py (new)"], state="ready")
    b = _card("bd-b", ["x.py"])
    _wire(monkeypatch, board, [a, b])

    check = board.ready_check("bd-b")
    assert "phantom-paths" in _gates(check)
    assert _path_advice(check) == ""


@requires_git
def test_the_new_marker_on_the_dependent_still_bypasses_the_check(board, monkeypatch):
    """The `(new)` bypass is untouched: it needs no dependency at all."""
    _wire(monkeypatch, board, [_card("bd-b", ["x.py (new)"])])

    assert "phantom-paths" not in _gates(board.ready_check("bd-b"))


@requires_git
def test_rehoming_a_ready_card_accepts_a_declared_path_in_the_target_project(checkout, board):
    """The re-home check shares the relief: a ready card moving projects keeps a path its
    unbuilt dependency declares `(new)`, and loses it when that dependency is cancelled."""
    board.projects = {"old": {"repo": checkout}, "new": {"repo": checkout}}
    board.default_project = "old"
    b = _card("bd-b", ["x.py"], state="ready", deps=["dep-a"], project="old")

    board.list_features = lambda *a, **k: [_card("dep-a", ["x.py (new)"], state="ready"), b]
    assert board._rehome_refusal(b, "new") == ""
    board.list_features = lambda *a, **k: [_card("dep-a", ["x.py (new)"], state="cancelled"), b]
    assert "x.py" in board._rehome_refusal(b, "new")


@requires_git
def test_the_declaration_check_never_fetches(board, monkeypatch):
    """No network: a dependency with no open PR is read from the board rows and local refs."""
    calls: list[tuple[str, ...]] = []
    real = store_mod._run_git

    def _spy(repo, *args):
        calls.append(args)
        return real(repo, *args)

    monkeypatch.setattr(store_mod, "_run_git", _spy)
    a = _card("dep-a", ["x.py (new)"], state="ready")
    b = _card("bd-b", ["x.py", "y.py"], deps=["dep-a"])
    _wire(monkeypatch, board, [a, b])

    board.ready_check("bd-b")
    assert calls, "the refs were never read"
    assert not [c for c in calls if c and c[0] == "fetch"]
