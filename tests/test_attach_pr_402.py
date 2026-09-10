"""#402: attach a PR the board did not open to the card it belongs to.

Live on protoEngineer, 2026-09-07: bd-ezs7's coder left a finished implementation as
uncommitted files in its candidate worktree (#400). The operator promoted it onto the card's
own branch, `feat/bd-ezs7-fix-a2a-make-poll-timeout-s-a-no-progres`, and opened PR #3369.
The board had no edge to attach it. Re-dispatching the card would have force-removed the
candidate worktree the work was still in. So the card sat terminal-blocked while #3369 went
through CI and review invisible to the board, and the PM hand-closed it with board_mark_done
after the merge.

The edge is deliberately no wider than the loop's own adoption. Crash recovery already adopts
the PR whose head is the card's canonical branch. This adopts the same PR on request, for an
in-flight coding card the loop is not working, under the loop's claim lock. The card then
sits exactly where `open_review` leaves one, so the ordinary reconcile drives it. The board
side runs through the real `br`. The two `gh` reads are faked at their seam here, and the
seam itself is read against a real PR in tests/test_attach_pr_gh_402.py, which the real-GitHub
CI job runs.
"""

from __future__ import annotations

import asyncio
import json
import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import project_board as pb
from project_board import api, worktree
from project_board import store as store_mod
from project_board.loop import BoardLoop, _register_drive, _register_loop, _unregister_drive, _unregister_loop
from project_board.loop.attach import attach_external_pr
from project_board.store import BeadsBoard, BoardError

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH — CI installs it and sets PB_REQUIRE_BR=1",
)

_AC = "- WHEN x THE SYSTEM SHALL y"
_URL = "https://github.com/protoLabsAI/protoAgent/pull/3369"
_SLUG = "protoLabsAI/protoAgent"


def _card(board: BeadsBoard, repo, title: str, path: str, **kw) -> dict:
    """A coding card that passed the real Ready gate."""
    (repo / path).write_text("x = 1\n")
    fid = board.create_feature(title, spec="s", acceptance_criteria=_AC, files_to_modify=[path], **kw)["id"]
    return board.mark_ready(fid)


def _pr_for(card: dict, **over) -> dict:
    """The facts `worktree.pr_identity` reports for the PR the board would find for this card."""
    return {
        "url": _URL,
        "state": "OPEN",
        "head": worktree.branch_name(card["id"], card["title"]),
        "base": "main",
        "cross_repo": False,
        **over,
    }


def _github(monkeypatch, pr: dict, *, slug: str = _SLUG) -> list:
    """Fake the two `gh` reads the attach makes, and record what it asked for."""
    asked: list = []

    async def _identity(url, *, cwd="."):
        asked.append(("pr_identity", url, cwd))
        return dict(pr)

    async def _slug(*, cwd="."):
        asked.append(("repo_slug", cwd))
        return slug

    monkeypatch.setattr(worktree, "pr_identity", _identity)
    monkeypatch.setattr(worktree, "repo_slug", _slug)
    return asked


async def _attach(board, fid, url=_URL, **kw):
    kw.setdefault("repo", board.repo)
    kw.setdefault("base", "main")
    return await attach_external_pr(board, board.get_feature(fid), url, **kw)


@requires_br
async def test_an_operator_pr_brings_a_blocked_card_into_review_and_the_reconcile_closes_it(tmp_path, monkeypatch):
    """The incident end to end. A terminal-blocked card gets the operator's PR. It lands
    exactly where the loop's own open_review leaves a card, with the review gate armed for the
    attached head and an audit line on the card. Then the ORDINARY PR reconcile, not a hand
    close, takes it to done when the PR merges."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    card = _card(board, tmp_path, "fix(a2a): make poll_timeout_s a no-progress bound (#3360a)", "adapters.py")
    fid = card["id"]
    board.flag_blocked(fid, "zombie drive: no process, no branch, no PR", category="terminal")
    _github(monkeypatch, _pr_for(card))

    result = await _attach(board, fid, review_gate=True, reason="recovered from the .g1 candidate", by="operator")

    f = board.get_feature(fid)
    assert result["state"] == f["board_state"] == "in_review"
    assert f["pr_url"] == _URL and not f["blocked"]
    labels = set(f["labels"])
    assert "review-pending" in labels, "with the gate on, the attached head must be reviewed, not wait forever"
    assert not labels & {"ready", "blocked"} and not any(label.startswith("blocked-class:") for label in labels)
    audit = [c for c in board.feature_comments(fid) if c.startswith("attached PR:")]
    assert len(audit) == 1 and _URL in audit[0] and "by operator" in audit[0] and "was blocked" in audit[0]
    assert "recovered from the .g1 candidate" in audit[0]

    async def _merged(pr_url, *, cwd="."):
        return "MERGED"

    async def _reap(repo, root, fid):
        return True

    monkeypatch.setattr(worktree, "pr_state", _merged)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)
    await BoardLoop({})._reconcile_prs()
    assert board.get_feature(fid)["board_state"] == "done"


@requires_br
@pytest.mark.parametrize("source", ["ready", "in_progress"])
async def test_a_ready_or_orphaned_in_progress_card_takes_the_attach(tmp_path, monkeypatch, source):
    """The other two in-flight shapes: a card still queued (the operator did the work by hand
    before the loop got to it), and one the loop claimed whose drive is gone. With the gate
    off, no review sub-state is armed: the merge edge would wait on one forever."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    card = _card(board, tmp_path, "hand-built fix", "fix.py")
    if source == "in_progress":
        board.claim(card["id"], assignee="proto")
    _github(monkeypatch, _pr_for(card))

    await _attach(board, card["id"], review_gate=False)

    f = board.get_feature(card["id"])
    assert f["board_state"] == "in_review" and f["pr_url"] == _URL
    assert not set(f["labels"]) & {"ready", "review-pending", "changes-requested", "review-clean"}


def _task(board, repo):
    fid = board.create_feature("a decision", spec="s", acceptance_criteria=_AC, issue_type="task")["id"]
    return board.mark_ready(fid)


def _backlog(board, repo):
    (repo / "b.py").write_text("x = 1\n")
    return board.create_feature("not ready", spec="s", acceptance_criteria=_AC, files_to_modify=["b.py"])


def _waiting_on_a_dependency(board, repo):
    first = _card(board, repo, "foundation", "base.py")
    return _card(board, repo, "builds on it", "top.py", depends_on=[first["id"]])


def _already_reviewing_another_pr(board, repo):
    card = _card(board, repo, "coder-built", "c.py")
    board.claim(card["id"])
    return board.open_review(card["id"], pr_url="https://github.com/protoLabsAI/protoAgent/pull/1")


def _plain(board, repo):
    return _card(board, repo, "plain card", "p.py")


# case → (card builder, PR-fact override (None = the card's own PR), repo slug, url, expected refusal)
_REFUSALS = {
    "task": (_task, None, _SLUG, _URL, "is a task"),
    "backlog": (_backlog, None, _SLUG, _URL, "backlog"),
    "open dependency": (_waiting_on_a_dependency, None, _SLUG, _URL, "depends on open card"),
    "another PR in review": (
        _already_reviewing_another_pr,
        None,
        _SLUG,
        _URL,
        "will not swap the PR under a live review",
    ),
    "not a PR url": (
        _plain,
        None,
        _SLUG,
        "https://github.com/protoLabsAI/protoAgent/issues/3369",
        "not a GitHub pull request",
    ),
    "unreadable": (_plain, {}, _SLUG, _URL, "could not read"),
    "other repo": (_plain, None, "someone/else", _URL, "someone/else"),
    "fork": (_plain, {"cross_repo": True}, _SLUG, _URL, "fork"),
    "merged": (_plain, {"state": "MERGED"}, _SLUG, _URL, "board_mark_done"),
    "closed": (_plain, {"state": "CLOSED"}, _SLUG, _URL, "closed without merging"),
    "other branch": (_plain, {"head": "fix/bd-x-by-hand"}, _SLUG, _URL, "canonical branch"),
    "other base": (_plain, {"base": "release"}, _SLUG, _URL, "base"),
}


@requires_br
@pytest.mark.parametrize("case", sorted(_REFUSALS))
async def test_incompatible_shapes_fail_closed_and_touch_nothing(tmp_path, monkeypatch, case):
    """Every refusal is actionable and leaves the card exactly as it was: no state change, no
    PR recorded, no audit line claiming an attach that did not happen."""
    build, override, slug, url, expected = _REFUSALS[case]
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    card = build(board, tmp_path)
    pr = {} if override == {} else _pr_for(card, **(override or {}))
    _github(monkeypatch, pr, slug=slug)
    before = board.get_feature(card["id"])

    with pytest.raises(BoardError, match=expected):
        await _attach(board, card["id"], url=url)

    after = board.get_feature(card["id"])
    assert (after["board_state"], after["pr_url"], sorted(after["labels"])) == (
        before["board_state"],
        before["pr_url"],
        sorted(before["labels"]),
    )
    assert not [c for c in board.feature_comments(card["id"]) if c.startswith("attached PR:")]


_OLD_PR = "https://github.com/protoLabsAI/protoAgent/pull/1"


@requires_br
@pytest.mark.parametrize(
    "prior,expected",
    [
        ("CLOSED", None),
        ("OPEN", "still open"),
        ("MERGED", "already merged"),
        ("", "unreadable"),
    ],
)
async def test_a_new_pr_replaces_an_earlier_one_only_once_that_is_closed(tmp_path, monkeypatch, prior, expected):
    """The board tracks one PR per card. The card's coder PR was closed unmerged (the
    reconcile blocks such a card for triage), the branch was reworked, and a new PR opened
    from it. The new PR may take the old one's place. An old PR that is still open would be
    orphaned, and a merged one means the card is already done."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    card = _plain(board, tmp_path)
    board.claim(card["id"])
    board.open_review(card["id"], pr_url=_OLD_PR)
    board.flag_blocked(card["id"], f"PR closed without merging — needs triage: {_OLD_PR}")
    _github(monkeypatch, _pr_for(card))

    async def _state(pr_url, *, cwd="."):
        assert pr_url == _OLD_PR  # only the PR being replaced is asked about
        return prior

    monkeypatch.setattr(worktree, "pr_state", _state)

    if expected:
        with pytest.raises(BoardError, match=expected):
            await _attach(board, card["id"])
        f = board.get_feature(card["id"])
        assert f["board_state"] == "blocked" and f["pr_url"] == _OLD_PR
        return
    await _attach(board, card["id"])
    f = board.get_feature(card["id"])
    assert f["board_state"] == "in_review" and f["pr_url"] == _URL
    audit = [c for c in board.feature_comments(card["id"]) if c.startswith("attached PR:")]
    assert f"replaces closed PR {_OLD_PR}" in audit[0]


@requires_br
async def test_the_other_branch_refusal_names_the_branch_to_use(tmp_path, monkeypatch):
    """Every later edge (a CI or review fix round resumes origin/<canonical branch>, recovery,
    the reap) keys on the card's own branch. A PR on another branch would be abandoned by the
    first fix round, which opens a second PR beside it. Say which branch to push to."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    card = _plain(board, tmp_path)
    _github(monkeypatch, _pr_for(card, head="fix/by-hand"))

    with pytest.raises(BoardError) as exc:
        await _attach(board, card["id"])

    branch = worktree.branch_name(card["id"], card["title"])
    assert f"push the work to branch {branch!r}" in str(exc.value)  # the exact branch, as an instruction
    assert "fix/by-hand" in str(exc.value)  # and the branch the PR is actually on


@requires_br
async def test_a_live_drive_refuses_the_attach(tmp_path, monkeypatch):
    """A coder still running on the card would race the attach into a second PR: its own
    open_review lands on top, or it pushes a rebuild to the branch. Refuse, and say why."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    card = _plain(board, tmp_path)
    board.claim(card["id"])
    asked = _github(monkeypatch, _pr_for(card))
    drive = asyncio.create_task(asyncio.sleep(30))
    _register_drive(card["id"], drive)
    try:
        with pytest.raises(BoardError, match="live coder drive"):
            await _attach(board, card["id"])
    finally:
        _unregister_drive(card["id"], drive)
        drive.cancel()
    assert board.get_feature(card["id"])["board_state"] == "in_progress"
    assert asked == []  # refused before spending a single gh call


@requires_br
async def test_re_attaching_the_same_pr_is_a_no_op(tmp_path, monkeypatch):
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    card = _plain(board, tmp_path)
    asked = _github(monkeypatch, _pr_for(card))
    await _attach(board, card["id"])
    asked.clear()

    again = await _attach(board, card["id"])

    assert again["already_attached"] is True and again["state"] == "in_review"
    assert asked == []  # nothing left to prove
    assert len([c for c in board.feature_comments(card["id"]) if c.startswith("attached PR:")]) == 1


@requires_br
async def test_the_attach_waits_for_the_loops_claim_lock(tmp_path, monkeypatch):
    """A ready card is exactly what the claim scan takes. The attach writes under the SAME lock
    the tick and board_dispatch hold, so the card cannot be claimed and dispatched halfway
    through becoming in_review."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    card = _plain(board, tmp_path)
    _github(monkeypatch, _pr_for(card))
    loop = BoardLoop({})
    _register_loop(loop)
    try:
        async with loop._claim_guard():  # a claim scan is running
            pending = asyncio.create_task(_attach(board, card["id"]))
            await asyncio.sleep(0.3)
            assert not pending.done()
            assert board.get_feature(card["id"])["board_state"] == "ready"
        await pending
    finally:
        _unregister_loop(loop)
    assert board.get_feature(card["id"])["board_state"] == "in_review"


@requires_br
def test_the_operator_route_attaches_and_refuses_with_a_400(tmp_path, monkeypatch):
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    card = _plain(board, tmp_path)
    _github(monkeypatch, _pr_for(card))
    monkeypatch.setattr(api, "get_store", lambda **_kw: board)
    app = FastAPI()
    app.include_router(api.build_data_router({"review_gate": True}), prefix="/api/plugins/project_board")
    client = TestClient(app)
    route = f"/api/plugins/project_board/features/{card['id']}/attach-pr"

    bad = client.post(route, json={"pr_url": "https://github.com/protoLabsAI/protoAgent/issues/1"})
    assert bad.status_code == 400 and "not a GitHub pull request" in bad.json()["detail"]

    ok = client.post(route, json={"pr_url": _URL, "reason": "operator recovery"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["state"] == "in_review" and ok.json()["review_pending"] is True
    audit = [c for c in board.feature_comments(card["id"]) if c.startswith("attached PR:")]
    assert "by operator" in audit[0]  # an HTTP attach is out-of-band by construction


@requires_br
async def test_the_agent_tool_attaches_and_reports_errors_as_text(tmp_path, monkeypatch):
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    card = _plain(board, tmp_path)
    _github(monkeypatch, _pr_for(card))
    monkeypatch.setattr(store_mod, "get_store", lambda **_kw: board)
    tool = next(t for t in pb._board_tools({"repo": str(tmp_path)}) if t.name == "board_attach_pr")

    refused = await tool.ainvoke({"feature_id": card["id"], "pr_url": "not a url"})
    assert refused.startswith("Error:")

    out = json.loads(await tool.ainvoke({"feature_id": card["id"], "pr_url": _URL, "reason": "salvaged"}))
    assert out["state"] == "in_review" and out["pr_url"] == _URL and out["review_pending"] is False
