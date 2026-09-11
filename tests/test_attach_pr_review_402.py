"""Review findings on the attach edge (#402, PR #437), each reproduced through the real `br`.

- A / A2: eligibility was decided on the board STATE, and `blocked` is a flag on top of a
  lane. A backlog card that was merely blocked, one that never passed the Ready gate, went
  straight into review. So did a blocked epic.
- B: re-attaching a blocked card's OWN PR lifted the block, which can be the review gate
  asking for a human, and re-armed the gate without one.
- C: a replacement PR kept the old PR's `merged-verified` stamp, so the merge edge could
  treat new code as gated.
- D / E / D2: the health sweep moved cards on reads taken before a `gh` round-trip, outside
  the claim lock attach writes under. It requeued an attach that had just landed. When the
  sweep adopted the PR first, the attach reported nothing, armed nothing and left no audit line.
- H: a `gh` timeout or a missing checkout escaped the tool and the route as an exception.
- F: the loop read `review_gate: "false"` as ON while every other surface read it as off.
- An audit comment that failed turned a completed attach into a reported error.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

import project_board as pb
from project_board import store as store_mod
from project_board import worktree
from project_board.loop import BoardLoop, _register_loop, _unregister_loop
from project_board.loop.attach import attach_external_pr
from project_board.store import BeadsBoard, BoardError

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH — CI installs it and sets PB_REQUIRE_BR=1",
)

_AC = "- WHEN x THE SYSTEM SHALL y"
_URL = "https://github.com/protoLabsAI/protoAgent/pull/3369"
_OLD = "https://github.com/protoLabsAI/protoAgent/pull/1"
_SLUG = "protoLabsAI/protoAgent"


def _ready(board: BeadsBoard, repo, title="plain card", path="p.py") -> dict:
    (repo / path).write_text("x = 1\n")
    fid = board.create_feature(title, spec="s", acceptance_criteria=_AC, files_to_modify=[path])["id"]
    return board.mark_ready(fid)


def _github(monkeypatch, card: dict, **over) -> None:
    async def _identity(url, *, cwd="."):
        return {
            "url": _URL,
            "state": "OPEN",
            "head": worktree.branch_name(card["id"], card["title"]),
            "base": "main",
            "cross_repo": False,
            **over,
        }

    async def _slug(*, cwd="."):
        return _SLUG

    monkeypatch.setattr(worktree, "pr_identity", _identity)
    monkeypatch.setattr(worktree, "repo_slug", _slug)


async def _attach(board, fid, url=_URL, **kw):
    kw.setdefault("repo", board.repo)
    kw.setdefault("base", "main")
    return await attach_external_pr(board, board.get_feature(fid), url, **kw)


def _audit(board, fid) -> list[str]:
    return [c for c in board.feature_comments(fid) if c.startswith("attached PR:")]


def _snapshot(board, fid) -> tuple:
    f = board.get_feature(fid)
    return f["board_state"], f["pr_url"], sorted(f["labels"])


# ── A / A2: eligibility is the lane underneath the block ───────────────────────────────


@requires_br
async def test_a_blocked_backlog_card_cannot_skip_the_ready_gate(tmp_path, monkeypatch):
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    card = board.create_feature("half-baked idea")  # no spec, no AC, no files: never Ready
    board.flag_blocked(card["id"], "parked: open question", category="terminal")
    _github(monkeypatch, card)
    before = _snapshot(board, card["id"])

    with pytest.raises(BoardError, match="backlog underneath its block"):
        await _attach(board, card["id"], review_gate=True)

    assert _snapshot(board, card["id"]) == before and not _audit(board, card["id"])


@requires_br
async def test_an_epic_cannot_take_a_pr(tmp_path, monkeypatch):
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    epic = board.create_epic("milestone 3")
    board.flag_blocked(epic["id"], "waiting on design", category="terminal")
    _github(monkeypatch, board.get_feature(epic["id"]))

    with pytest.raises(BoardError, match="not a coding feature"):
        await _attach(board, epic["id"])

    assert board.get_feature(epic["id"])["board_state"] == "blocked"


# ── B: a card's own PR is never a way around its block ─────────────────────────────────


@requires_br
async def test_re_attaching_a_blocked_cards_own_pr_is_refused_naming_the_block(tmp_path, monkeypatch):
    """The review gate blocked this card for a human after its fix budget ran out. Attaching
    the same PR again would lift that block and re-arm the gate, which has its budget back,
    so the PR could auto-merge with no human in the loop. Unblocking is a deliberate
    board_unblock_feature."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    card = _ready(board, tmp_path)
    fid = card["id"]
    board.claim(fid)
    board.open_review(fid, pr_url=_URL)
    board.flag_blocked(fid, f"review findings persist after 2 fix attempt(s) — needs human review: {_URL}")
    _github(monkeypatch, card)
    before = _snapshot(board, fid)

    with pytest.raises(BoardError) as exc:
        await _attach(board, fid, review_gate=True)

    assert "needs human review" in str(exc.value) and f"board_unblock_feature({fid})" in str(exc.value)
    assert _snapshot(board, fid) == before and not _audit(board, fid)


@requires_br
async def test_re_attaching_the_pr_of_a_card_in_a_fix_round_is_refused(tmp_path, monkeypatch):
    """A card requeued for a CI or review fix round still tracks its PR. The loop is already
    driving it back to review, and an attach would skip the fix round."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    card = _ready(board, tmp_path)
    fid = card["id"]
    board.claim(fid)
    board.open_review(fid, pr_url=_URL)
    board.requeue(fid)  # ready again, PR kept: a fix round
    _github(monkeypatch, card)

    with pytest.raises(BoardError, match="driving it back to review"):
        await _attach(board, fid)

    assert board.get_feature(fid)["board_state"] == "ready"


# ── C: no stamp from an earlier head survives ──────────────────────────────────────────


@requires_br
async def test_a_replacement_pr_does_not_inherit_the_old_prs_merged_state_verdict(tmp_path, monkeypatch):
    """The old PR's merged state was verified against base X and stamped. Keeping the stamp
    let the merge edge treat the replacement, which is entirely different code, as verified
    while base stayed at X, and the local gate never ran on it."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    card = _ready(board, tmp_path)
    fid = card["id"]
    board.claim(fid)
    board.open_review(fid, pr_url=_OLD)
    base_sha = "abcdef1234567890abcdef1234567890abcdef12"
    board.record_merged_verified(fid, base_sha[:12])
    board.set_review_substate(fid, store_mod.LABEL_REVIEW_CLEAN, head_sha="1" * 40)
    board.flag_blocked(fid, f"PR closed without merging — needs triage: {_OLD}")
    _github(monkeypatch, card)

    async def _closed(url, *, cwd="."):
        return "CLOSED"

    monkeypatch.setattr(worktree, "pr_state", _closed)
    await _attach(board, fid, review_gate=False)

    f = board.get_feature(fid)
    stale = ("merged-verified:", "review-clean-sha:", "reviewed-head:")
    assert not [label for label in f["labels"] if label.startswith(stale)], f["labels"]

    loop = BoardLoop({"auto_rebase": True, "local_gate_cmd": "pytest -q", "auto_merge": True})
    ran = []

    async def _gate(wt, feature=None):
        ran.append(wt)
        return None

    async def _base(repo, base):
        return base_sha

    async def _merged_state_worktree(*a, **k):
        return "merged", str(tmp_path)

    async def _remove(*a, **k):
        return True

    monkeypatch.setattr(loop, "_run_local_gate", _gate)
    monkeypatch.setattr(worktree, "origin_head_sha", _base)
    monkeypatch.setattr(worktree, "merged_state_worktree", _merged_state_worktree)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    await loop._verify_merged_state(board, f, _URL, str(tmp_path))
    assert ran, "the replacement PR merged-state was never gated"


# ── D / E / D2: the sweep and an attach, racing ────────────────────────────────────────


@requires_br
async def test_the_sweeps_orphan_requeue_does_not_undo_a_concurrent_attach(tmp_path, monkeypatch):
    """The sweep read the card as an orphan, then waited on gh for its PR. The attach landed
    meanwhile. On its stale read the sweep then requeued an in_review card, and the next
    claim put a coder back on the PR's branch."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    card = _ready(board, tmp_path)
    fid = card["id"]
    board.claim(fid)  # in_progress with no drive: the orphan the sweep reconciles
    _github(monkeypatch, card)
    loop = BoardLoop({})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)
    in_gh, release = asyncio.Event(), asyncio.Event()

    async def _pr_for_branch(branch, *, cwd="."):
        in_gh.set()
        await release.wait()
        return ""  # a gh blip: no PR visible yet

    async def _no_salvage(store, fid):
        return False

    monkeypatch.setattr(worktree, "pr_url_for_branch", _pr_for_branch)
    monkeypatch.setattr(loop, "_salvage_verified_candidate", _no_salvage)
    _register_loop(loop)
    try:
        sweep = asyncio.create_task(loop._reconcile_orphan(fid))
        await in_gh.wait()
        await _attach(board, fid)
        release.set()
        await sweep
    finally:
        _unregister_loop(loop)

    f = board.get_feature(fid)
    assert f["board_state"] == "in_review" and f["pr_url"] == _URL


@requires_br
async def test_the_blocked_self_heal_does_not_undo_a_concurrent_attach(tmp_path, monkeypatch):
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    card = _ready(board, tmp_path)
    fid = card["id"]
    board.claim(fid)
    board.flag_blocked(fid, "coder timed out", category="transient")
    _github(monkeypatch, card)
    loop = BoardLoop({})
    listed, release = asyncio.Event(), asyncio.Event()
    real_budget_get = loop._budget_get

    async def _slow_budget_get(store, f_id, kind, feature=None):
        listed.set()
        await release.wait()
        return await real_budget_get(store, f_id, kind, feature)

    monkeypatch.setattr(loop, "_budget_get", _slow_budget_get)
    _register_loop(loop)
    try:
        sweep = asyncio.create_task(loop._recover_blocked(board))
        await listed.wait()
        await _attach(board, fid)
        release.set()
        await sweep
    finally:
        _unregister_loop(loop)

    f = board.get_feature(fid)
    assert f["board_state"] == "in_review" and f["pr_url"] == _URL


@requires_br
async def test_an_attach_that_the_sweep_beat_to_the_pr_still_arms_the_gate_and_audits(tmp_path, monkeypatch):
    """D2: the sweep's recovery adopted the same PR (open_review) while the attach waited on
    gh. The attach then found the card already in review. It reported a fresh attach,
    armed nothing, and wrote no audit line. With the gate on, that card waits forever for a
    verdict."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    card = _ready(board, tmp_path)
    fid = card["id"]
    board.claim(fid)
    loop = BoardLoop({"review_gate": True})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)
    in_gh, release = asyncio.Event(), asyncio.Event()

    async def _identity(url, *, cwd="."):
        in_gh.set()
        await release.wait()
        branch = worktree.branch_name(fid, card["title"])
        return {"url": _URL, "state": "OPEN", "head": branch, "base": "main", "cross_repo": False}

    async def _slug(*, cwd="."):
        return _SLUG

    async def _pr_for_branch(branch, *, cwd="."):
        return _URL

    monkeypatch.setattr(worktree, "pr_identity", _identity)
    monkeypatch.setattr(worktree, "repo_slug", _slug)
    monkeypatch.setattr(worktree, "pr_url_for_branch", _pr_for_branch)
    _register_loop(loop)
    try:
        attaching = asyncio.create_task(_attach(board, fid, review_gate=True, reason="recovered", by="operator"))
        await in_gh.wait()
        await loop._reconcile_orphan(fid)  # the sweep adopts the same PR first
        release.set()
        result = await attaching
    finally:
        _unregister_loop(loop)

    f = board.get_feature(fid)
    assert result["already_attached"] is True and result["review_pending"] is True
    assert f["board_state"] == "in_review" and "review-pending" in f["labels"]
    (audit,) = _audit(board, fid)
    assert "by operator" in audit and "adopted by the board meanwhile" in audit


# ── H: a gh failure is a refusal, never an exception ───────────────────────────────────


@requires_br
@pytest.mark.parametrize(
    "failure",
    [worktree.WorktreeError("gh pr view timed out after 60s"), FileNotFoundError(2, "No such directory", "/gone")],
    ids=["gh-timeout", "checkout-gone"],
)
async def test_a_gh_failure_is_returned_as_an_error_not_raised(tmp_path, monkeypatch, failure):
    """The replacement path read the old PR's state unguarded. A gh timeout or a project
    checkout that has gone escaped the tool, failing the agent's turn, and made the route
    answer 500."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    card = _ready(board, tmp_path)
    fid = card["id"]
    board.claim(fid)
    board.open_review(fid, pr_url=_OLD)
    board.flag_blocked(fid, f"PR closed without merging — needs triage: {_OLD}")
    monkeypatch.setattr(store_mod, "get_store", lambda **_kw: board)

    async def _broken_gh(*a, **k):
        raise failure

    monkeypatch.setattr(worktree, "_gh", _broken_gh)
    tool = next(t for t in pb._board_tools({"repo": str(tmp_path)}) if t.name == "board_attach_pr")

    out = await tool.ainvoke({"feature_id": fid, "pr_url": _URL})

    assert out.startswith("Error:") and "could not read" in out
    assert board.get_feature(fid)["board_state"] == "blocked"


# ── the audit line cannot undo a completed attach ──────────────────────────────────────


@requires_br
async def test_a_failed_audit_comment_does_not_report_a_landed_attach_as_an_error(tmp_path, monkeypatch):
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    card = _ready(board, tmp_path)
    _github(monkeypatch, card)
    real_run = board._run

    def _run(*args, **kwargs):
        if args[:2] == ("comments", "add"):
            raise subprocess.TimeoutExpired(["br", "comments", "add"], 30)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(board, "_run", _run)
    result = await _attach(board, card["id"])

    assert result["state"] == "in_review" and "audit comment could not be written" in result["warning"]
    assert board.get_feature(card["id"])["board_state"] == "in_review"


# ── F: one reading of review_gate everywhere ───────────────────────────────────────────


def test_the_loop_reads_review_gate_false_as_off_like_every_other_surface():
    """`bool("false")` is True. The loop ran a gate that board_list, the posture hints and
    the attach verb all read as off."""
    assert BoardLoop({"review_gate": "false"}).review_gate is False
    assert BoardLoop({"review_gate": "true"}).review_gate is True
    assert BoardLoop({"review_gate": True}).review_gate is True
