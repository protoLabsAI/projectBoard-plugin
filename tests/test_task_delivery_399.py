"""#399: a delivered task must READ as delivered, and a delivery whose deliverable was
not written must not report success.

The report: ``board_deliver`` "persists the transition but not ``text``" — on bd-krhr
the card went to ``in_review`` while ``GET /features`` showed ``deliverable: ""``. The
bead says otherwise: its thread carries the 4,989-char ``deliverable:`` comment written
at 06:26:47Z, the same second the tool reported success. Nothing was lost. The READS
could not see it: ``br list`` omits comments, and ``list_features`` carried the comment
thread across only for blocked rows (#414), so every delivered task listed with an
empty deliverable (and its current assignee standing in for whoever delivered it); and
the agent's ``board_get_feature`` had no ``deliverable`` field at all.

The failure the report describes is still reachable, though, by another road:
``record_delivery`` wrote the deliverable through the best-effort ``comment()`` helper,
which swallows a failed write by contract, and then moved the card to ``in_review``
anyway — a success report on a delivery with nothing in it, whose retry is refused
because the card already left in_progress.

Real ``br`` throughout. The one injected fault is a failed ``br comments add`` — what
``_run`` raises when beads refuses a write or contention outlasts the retries — on the
real board, so every state assertion is beads' own read-back.
"""

from __future__ import annotations

import json
import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import project_board as pb
from project_board import api
from project_board import store as store_mod
from project_board.store import BeadsBoard, BoardError

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH — see tests/test_integration.py (CI sets PB_REQUIRE_BR=1)",
)

RECORD = "# Decision — #3362b\n\nOnly COMPLETED yields a successful delegated answer.\n\n" + "detail\n" * 600


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


@requires_br
def test_get_features_lists_a_delivered_task_with_its_deliverable(board, monkeypatch):
    """The exact read the report was filed from — `GET /features`, the listing the board
    view, the operator and every agent poll — must show the deliverable that is on the
    bead, and the deliverer the delivery stamped rather than whoever holds the card now."""
    fid = _task_in_progress(board, assignee="alice")
    board.record_delivery(fid, text=RECORD)
    board._run("update", fid, "--assignee", "bob")  # reassigned after delivery (#316's case)

    monkeypatch.setattr(api, "get_store", lambda **_kw: board)
    app = FastAPI()
    app.include_router(api.build_data_router({}), prefix="/api/plugins/project_board")
    listed = TestClient(app).get("/api/plugins/project_board/features").json()["features"]

    row = next(f for f in listed if f["id"] == fid)
    assert row["board_state"] == "in_review"
    assert row["deliverable"] == RECORD.strip()  # was "" — the whole of #399's evidence
    assert row["delivered_by"] == "alice"  # was "bob": the assignee fallback, not the stamp
    # …and the listing now agrees with the single-card read, which always had it.
    single = board.get_feature(fid)
    assert (row["deliverable"], row["delivered_by"]) == (single["deliverable"], single["delivered_by"])


@requires_br
def test_board_get_feature_shows_a_task_its_deliverable(board, monkeypatch):
    """The read the PM agent actually made. Its audit trail: `board_get_feature(bd-krhr)`
    at 06:23:33, then 22 seconds later a new task titled "Repair empty bd-krhr decision
    deliverable". The tool's "FULL detail" had no `deliverable` field at all, so a
    delivered task read exactly like an empty one."""
    monkeypatch.setattr(store_mod, "get_store", lambda **_kw: board)
    get = {t.name: t for t in pb._board_tools({})}["board_get_feature"]
    fid = _task_in_progress(board, assignee="alice")
    board.record_delivery(fid, text=RECORD)

    out = json.loads(get.invoke({"feature_id": fid}))

    assert out["state"] == "in_review"
    assert out["deliverable"] == RECORD.strip()
    assert out["delivered_by"] == "alice"
    # A coding feature has no deliverable to show, so its shape is unchanged.
    feat = board.create_feature("A coding feature", spec="s")
    assert "deliverable" not in json.loads(get.invoke({"feature_id": feat["id"]}))


@requires_br
def test_a_failed_deliverable_write_fails_the_delivery_and_leaves_it_retryable(board, monkeypatch):
    """A delivery whose deliverable did not land must raise BEFORE the card moves, so it
    is still in_progress — and the same call, made again, simply succeeds."""
    fid = _task_in_progress(board)
    real_run = board._run
    faults = []

    def _run(*args, **kwargs):
        if args[:2] == ("comments", "add") and args[3].startswith("deliverable:") and not faults:
            faults.append(args)
            raise BoardError("`br comments add` failed: DATABASE_ERROR: database is locked")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(board, "_run", _run)

    with pytest.raises(BoardError, match="database is locked"):
        board.record_delivery(fid, text=RECORD)

    assert faults  # the fault really fired on the deliverable write
    stuck = board.get_feature(fid)
    assert stuck["board_state"] == "in_progress"  # NOT in_review with nothing in it
    assert stuck["deliverable"] == ""

    retried = board.record_delivery(fid, text=RECORD)  # the retry is not locked out
    assert retried["board_state"] == "in_review"
    assert retried["deliverable"] == RECORD.strip()
