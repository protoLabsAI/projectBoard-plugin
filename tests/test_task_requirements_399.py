"""#399 point 3: a task's requirements are SURFACED to its verifier, never used to gate.

On bd-krhr every requirement (r1..r5) stayed ``open`` through a delivery that addressed
them all. Not a bug in one edge — the task lane had no edge that could close one:
``mark_ready`` decomposes a task's acceptance criteria into the same ledger a coding
feature gets, but the task prompt never asked for dispositions, a delivery never applied
any, and no tool set them. A ledger nothing can close reads, forever, as "none of this
was done".

The shape of the fix, and what these tests pin:

- the task prompt asks for the SAME ``## Requirements`` section a coder writes, and a
  reply written the way it asks is one the existing parser reads;
- a delivery through either door — ``board_deliver`` or the loop's recorded reply —
  closes the items its text disposes of, best-effort (a failed ledger write never costs
  the delivery), and never refuses on items left open;
- what is still open reaches the verifier: ``board_get_feature``'s
  ``open_requirements``, and a ``note`` on the ``board_verify`` / ``POST …/verify``
  result — with the approval still standing, because the verifier decides.

Real ``br`` for everything the store writes and reads back; the one fake is the model's
turn (``coder_seam.dispatch_task``), plus one injected ``br`` fault.
"""

from __future__ import annotations

import json
import logging
import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import project_board as pb
from project_board import api, coder_seam
from project_board import store as store_mod
from project_board.loop import BoardLoop, _parse_requirements_reply
from project_board.store import BeadsBoard, BoardError

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH — see tests/test_integration.py (CI sets PB_REQUIRE_BR=1)",
)

CRITERIA = (
    "- WHEN decided THE SYSTEM SHALL say whether only completed tasks may answer\n"
    "- WHEN decided THE SYSTEM SHALL cover billing and continuity together\n"
    "- WHEN decided THE SYSTEM SHALL supply a ready-to-board feature spec"
)
DISPOSED = (
    "# Decision — #3362b\n\nOnly COMPLETED yields a successful delegated answer.\n\n"
    "## Requirements\n"
    "- r1: done\n"
    "- r3: declined — the follow-up is filed as its own card instead\n"
)


@pytest.fixture
def board(tmp_path):
    """A REAL ``BeadsBoard`` over a throwaway workspace (the test_integration pattern)."""
    return BeadsBoard(repo=str(tmp_path), actor="test")


def _ready_task(board, assignee="alice"):
    task = board.create_feature(
        "Decide A2A answer eligibility",
        spec="Record the decision.",
        acceptance_criteria=CRITERIA,
        issue_type="task",
        assignee=assignee,
    )
    ready = board.mark_ready(task["id"])
    assert [(r["id"], r["status"]) for r in ready["requirements"]] == [("r1", "open"), ("r2", "open"), ("r3", "open")]
    return task["id"]


def _task_in_progress(board, assignee="alice"):
    fid = _ready_task(board, assignee)
    assert board.claim_task(fid, assignee=assignee)["board_state"] == "in_progress"
    return fid


def _tools(board, monkeypatch):
    monkeypatch.setattr(store_mod, "get_store", lambda **_kw: board)
    return {t.name: t for t in pb._board_tools({})}


def _statuses(board, fid):
    return {r["id"]: (r["status"], r.get("decline_reason", "")) for r in board.get_feature(fid)["requirements"]}


def test_the_task_prompt_asks_for_the_same_section_a_coder_writes():
    """The prompt lists the ledger with ids and statuses, and asks for the coder's
    `## Requirements` rows. The load-bearing half is the round trip: a reply written the
    way the prompt says is one the SHARED parser reads — the two cannot drift apart."""
    loop = BoardLoop({})
    feature = {
        "title": "Decide A2A answer eligibility",
        "spec": "Record the decision.",
        "acceptance_criteria": CRITERIA,
        "requirements": [
            {"id": "r1", "text": "say whether only completed tasks may answer", "status": "open"},
            {"id": "r2", "text": "cover billing and continuity together", "status": "done"},
        ],
    }
    prompt = loop._build_task_prompt(feature)

    assert "## Requirements ledger" in prompt
    assert "- `r1` [open] say whether only completed tasks may answer" in prompt
    assert "- `r2` [done] cover billing and continuity together" in prompt  # a re-dispatch sees what closed
    assert "`## Requirements` section" in prompt and "`- <id>: done`" in prompt
    assert "`- <id>: declined — <concrete reason>`" in prompt

    reply = "The decision…\n\n## Requirements\n- r1: done\n- r2: declined — <concrete reason>\n"
    assert _parse_requirements_reply(reply) == [
        {"id": "r1", "status": "done"},
        {"id": "r2", "status": "declined", "decline_reason": "<concrete reason>"},
    ]
    # No ledger, no section to ask for.
    assert "Requirements" not in loop._build_task_prompt({**feature, "requirements": []})


@requires_br
def test_board_deliver_closes_the_requirements_its_text_disposes_of(board, monkeypatch):
    """The agent's door. r1 done and r3 declined (with its reason) land on the ledger; r2,
    which the deliverable never mentions, stays open — and the delivery is NOT refused
    for it."""
    deliver = _tools(board, monkeypatch)["board_deliver"]
    fid = _task_in_progress(board)

    out = json.loads(deliver.invoke({"feature_id": fid, "text": DISPOSED}))

    assert out == {"id": fid, "state": "in_review"}  # an open r2 refuses nothing
    assert _statuses(board, fid) == {
        "r1": ("done", ""),
        "r2": ("open", ""),
        "r3": ("declined", "the follow-up is filed as its own card instead"),
    }


@requires_br
async def test_a_reply_the_loop_records_applies_its_dispositions_too(board, monkeypatch):
    """The loop's door: the assignee answers the task prompt, the drive records the reply,
    and the same dispositions land — whichever door a delivery comes through."""
    fid = _task_in_progress(board, assignee="quinn")
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)

    async def _assignee_replies(delegate, prompt, *, timeout=None):
        assert "## Requirements ledger" in prompt  # it was asked…
        return DISPOSED  # …and answered as asked

    monkeypatch.setattr(coder_seam, "dispatch_task", _assignee_replies)
    await BoardLoop({"coder": "proto"})._drive_task(board.get_feature(fid), delegate=object())

    assert board.get_feature(fid)["board_state"] == "in_review"
    assert _statuses(board, fid)["r1"] == ("done", "")
    assert _statuses(board, fid)["r3"][0] == "declined"


@requires_br
def test_a_failed_ledger_write_never_costs_the_delivery(board, monkeypatch, caplog):
    """Best-effort, and deliberately the opposite of the deliverable write: the ledger is
    what a verifier checks against, the dispositions still sit in the recorded text, so a
    `br` failure on the ledger is logged and the delivery lands anyway."""
    fid = _task_in_progress(board)
    real_run = board._run
    faults = []

    def _run(*args, **kwargs):
        if args[:2] == ("update", fid) and any(str(a).startswith("--notes=") for a in args):
            faults.append(args)
            raise BoardError("`br update` failed: DATABASE_ERROR: database is locked")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(board, "_run", _run)
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        delivered = board.record_delivery(fid, text=DISPOSED)

    assert faults  # the ledger write was really attempted, and really failed
    assert delivered["board_state"] == "in_review" and delivered["deliverable"] == DISPOSED.strip()
    assert all(status == "open" for status, _ in _statuses(board, fid).values())  # nothing half-written
    assert any("dispositions not applied" in r.getMessage() for r in caplog.records)


@requires_br
def test_open_requirements_reach_the_verifier_and_the_approval_still_stands(board, monkeypatch):
    """Where a verifier looks: board_get_feature names what is still open BEFORE the
    decision, and board_verify's result says it again AFTER — while the approval goes
    through. Surfaced, never enforced."""
    tools = _tools(board, monkeypatch)
    fid = _task_in_progress(board)
    tools["board_deliver"].invoke(
        {"feature_id": fid, "text": "A decision that addresses only r1.\n\n## Requirements\n- r1: done"}
    )

    card = json.loads(tools["board_get_feature"].invoke({"feature_id": fid}))
    assert card["open_requirements"] == ["r2", "r3"]

    verdict = json.loads(tools["board_verify"].invoke({"feature_id": fid, "approved": True, "by": "reviewer"}))
    assert verdict["state"] == "done"  # approved, open items and all — the verifier decided
    assert verdict["note"] == "2 requirement(s) still open: r2, r3"


@requires_br
def test_the_verify_route_carries_the_same_note_and_none_when_all_are_closed(board, monkeypatch):
    """The console's door (`POST …/verify`) says the same thing — and says nothing when
    every item was closed, so the note only ever appears when there is something to weigh."""
    fid = _task_in_progress(board, assignee="alice")
    board.record_delivery(fid, text="Partial.\n\n## Requirements\n- r2: done")
    done = _task_in_progress(board, assignee="bob")
    board.record_delivery(done, text="Complete.\n\n## Requirements\n- r1: done\n- r2: done\n- r3: declined — n/a")

    monkeypatch.setattr(api, "get_store", lambda **_kw: board)
    app = FastAPI()
    app.include_router(api.build_data_router({}), prefix="/api/plugins/project_board")
    client = TestClient(app)

    partial = client.post(f"/api/plugins/project_board/features/{fid}/verify", json={"approved": True}).json()
    assert partial["board_state"] == "done"
    assert partial["note"] == "2 requirement(s) still open: r1, r3"

    complete = client.post(f"/api/plugins/project_board/features/{done}/verify", json={"approved": True}).json()
    assert complete["board_state"] == "done" and "note" not in complete
