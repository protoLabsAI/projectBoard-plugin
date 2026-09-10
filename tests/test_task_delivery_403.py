"""#403: a task delivery that is REPEATED must not be an error, and one that DIFFERS
must not overwrite the delivery on record.

Observed live on protoEngineer's board (bd-krhr, 2026-09-07). The card's self-drive ran
the board's own agent, and that agent — its board tools live during the turn — called
``board_deliver`` itself. The card moved to ``in_review``. When the turn ended, the loop
then recorded the agent's REPLY as the deliverable, and ``record_delivery`` raised
``expects in_progress, got 'in_review'`` straight out of the drive task: an
"exception was never retrieved" traceback in the agent log on BOTH runs that day. A PM
session delivering its own rewrite of the same card got the same bare state error, which
never said the card was already delivered, or how to replace what was there.

Real ``br`` throughout: the replay check compares a call against what beads READS BACK,
and only real beads decides that. The one fake is the model boundary — the agent's turn
(``coder_seam.dispatch_self`` / ``dispatch_task``) — which here does what the live agent
did: it calls the real ``board_deliver`` tool mid-turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil

import pytest

import project_board as pb
from project_board import coder_seam
from project_board import store as store_mod
from project_board.loop import BoardLoop
from project_board.store import BeadsBoard, BoardError

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH — see tests/test_integration.py (CI sets PB_REQUIRE_BR=1)",
)

URL = "https://github.com/protoLabsAI/protoAgent/issues/3362"
RECORD = "# Decision — #3362b\n\nOnly COMPLETED yields a successful delegated answer."


@pytest.fixture
def board(tmp_path):
    """A REAL ``BeadsBoard`` over a throwaway workspace (the test_integration pattern)."""
    return BeadsBoard(repo=str(tmp_path), actor="test")


def _task_in_progress(board, assignee="alice"):
    task = board.create_feature(
        "Decide A2A answer eligibility",
        spec="Record the decision.",
        acceptance_criteria="- WHEN decided THE SYSTEM SHALL record why",
        issue_type="task",
        assignee=assignee,
    )
    board.mark_ready(task["id"])
    claimed = board.claim_task(task["id"], assignee=assignee)
    assert claimed is not None and claimed["board_state"] == "in_progress"
    return task["id"]


def _comment_count(board, fid):
    """The raw thread length off `br show` — the audit trail a replay must not grow."""
    rows = board._run("show", fid, want_json=True)
    bead = rows[0] if isinstance(rows, list) else rows
    return len(bead.get("comments") or [])


@requires_br
def test_an_identical_redelivery_is_a_no_op_that_writes_nothing(board):
    """A tool-call retry or a driver replay of a delivery the card already carries
    returns the card, still in review, and writes nothing. The first delivery is the
    ordinary edge (in_progress → in_review), unchanged."""
    fid = _task_in_progress(board)
    first = board.record_delivery(fid, text=RECORD, ref=URL)
    assert first["board_state"] == "in_review"
    assert first["deliverable"] == RECORD and first["pr_url"] == URL
    trail = _comment_count(board, fid)

    again = board.record_delivery(fid, text=RECORD, ref=URL)

    assert again["board_state"] == "in_review"  # still where board_verify expects it
    assert again["deliverable"] == RECORD and again["pr_url"] == URL
    assert _comment_count(board, fid) == trail  # no second deliverable, no second stamp

    # A repeat that brings LESS is still a repeat: nothing it carries disagrees.
    board.record_delivery(fid, text=RECORD)
    board.record_delivery(fid, ref=URL)
    assert _comment_count(board, fid) == trail


@requires_br
def test_a_different_redelivery_is_refused_and_the_record_stands(board):
    """A DIFFERENT deliverable for a card in review is refused — named as an existing
    delivery, not a bare state error — and writes nothing, so the record board_verify
    will judge is still the one that was delivered."""
    fid = _task_in_progress(board)
    board.record_delivery(fid, text=RECORD, ref=URL)
    trail = _comment_count(board, fid)

    with pytest.raises(BoardError, match="already delivered by alice") as refused:
        board.record_delivery(fid, text="A rewrite of the decision.", ref=URL)
    assert isinstance(refused.value, store_mod.AlreadyDelivered)
    assert "board_verify approved=false" in str(refused.value)  # …and says how to replace it

    # The same text under a different link is a different delivery too.
    with pytest.raises(store_mod.AlreadyDelivered):
        board.record_delivery(fid, text=RECORD, ref=URL + "#issuecomment-1")

    after = board.get_feature(fid)
    assert after["board_state"] == "in_review"
    assert after["deliverable"] == RECORD and after["pr_url"] == URL
    assert _comment_count(board, fid) == trail


@requires_br
def test_the_board_deliver_tool_reports_a_replay_as_success(board, monkeypatch):
    """The agent-facing shape of both halves: the incident's `board_deliver` called twice
    with the same payload now reads success twice; a different payload reads an
    `Error:` that names the existing delivery."""
    monkeypatch.setattr(store_mod, "get_store", lambda **_kw: board)
    deliver = {t.name: t for t in pb._board_tools({})}["board_deliver"]
    fid = _task_in_progress(board)
    args = {"feature_id": fid, "text": RECORD, "ref": URL}

    assert json.loads(deliver.invoke(args)) == {"id": fid, "state": "in_review"}
    assert json.loads(deliver.invoke(args)) == {"id": fid, "state": "in_review"}

    out = deliver.invoke({**args, "text": "placeholder"})
    assert out.startswith("Error: ") and "already delivered" in out


@requires_br
async def test_a_self_task_whose_agent_delivers_in_turn_ends_cleanly(board, monkeypatch, caplog):
    """The bd-krhr drive, end to end: the agent board_delivers mid-turn, then replies
    with something else. The drive must end cleanly — no exception escaping the task —
    and the agent's explicit delivery is what stays on record, not the reply."""
    task = board.create_feature(
        "Decide A2A answer eligibility",
        spec="Record the decision.",
        acceptance_criteria="- WHEN decided THE SYSTEM SHALL record why",
        issue_type="task",
        assignee="agent",  # the board's own agent — the self-dispatch path (#311)
    )
    fid = task["id"]
    board.mark_ready(fid)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)
    monkeypatch.setattr(store_mod, "get_store", lambda **_kw: board)
    monkeypatch.setattr(coder_seam, "resolve_self_invoke", lambda: object())
    deliver = {t.name: t for t in pb._board_tools({})}["board_deliver"]

    async def _agent_turn(invoke, prompt, session_id, *, timeout=None):
        # What the live agent did: deliver explicitly through its own board tool…
        out = await asyncio.to_thread(deliver.invoke, {"feature_id": fid, "text": RECORD, "ref": URL})
        assert json.loads(out)["state"] == "in_review"
        # …then end the turn with a reply that is NOT that deliverable.
        return f"Delivered {fid} to review — the decision record is on the card."

    monkeypatch.setattr(coder_seam, "dispatch_self", _agent_turn)
    loop = BoardLoop({"coder": "proto"})

    with caplog.at_level(logging.INFO, logger="protoagent.plugins.project_board"):
        assert await loop._spawn_ready() is True
        drives = list(loop._drives)
        assert drives
        await asyncio.gather(*drives)  # NOT return_exceptions: an escaped error fails here

    after = board.get_feature(fid)
    assert after["board_state"] == "in_review"
    assert after["deliverable"] == RECORD  # the explicit delivery stands; the reply is not written over it
    kept = [r for r in caplog.records if fid in r.getMessage() and "not recorded" in r.getMessage()]
    assert kept and kept[0].levelno == logging.INFO  # an expected outcome, not a warning


@requires_br
async def test_a_task_requeued_under_its_drive_is_left_where_the_operator_put_it(board, monkeypatch, caplog):
    """The other way a card leaves in_progress mid-dispatch: an operator pulls it back.
    The late reply is stale — it is dropped with a warning, not forced onto the card, and
    the drive neither raises nor blocks a card it no longer holds."""
    fid = _task_in_progress(board, assignee="quinn")
    feature = board.get_feature(fid)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)

    async def _operator_requeues_mid_dispatch(delegate, prompt, *, timeout=None):
        await asyncio.to_thread(board.requeue, fid)
        return "a deliverable nobody is waiting for any more"

    monkeypatch.setattr(coder_seam, "dispatch_task", _operator_requeues_mid_dispatch)
    loop = BoardLoop({"coder": "proto"})

    with caplog.at_level(logging.INFO, logger="protoagent.plugins.project_board"):
        await loop._drive_task(feature, delegate=object())  # raises on main: expects in_progress, got 'ready'

    after = board.get_feature(fid)
    assert after["board_state"] == "ready" and not after["blocked"]
    assert after["deliverable"] == ""
    dropped = [r for r in caplog.records if fid in r.getMessage() and "not recorded" in r.getMessage()]
    assert dropped and dropped[0].levelno == logging.WARNING
