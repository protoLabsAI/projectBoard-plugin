"""A task drive blocks its card only while the card is still its own (#398, #432).

The task-side twin of the coding drive's stand-aside. When a sister-agent or self task's
dispatch fails, the drive re-reads the card before blocking it. #432 made a delivered
card (in_review / done) exempt, but a re-read that FAILED still blocked "as it always
did", and a card a human had held under the drive was blocked over, its reason
replaced. Both now leave the card alone: an unreadable card is never assumed to be the
drive's own (a card left in_progress with no drive is the sweep's to reconcile), and a
move stands.

The store is REAL `br`; only the delegate's dispatch is faked — it is not what moves the
card — and, for the fault-injection test, the one re-read after the failure.
"""

from __future__ import annotations

import logging
import shutil

import pytest

import project_board.loop as loop_mod
from project_board import coder_seam, worktree
from project_board import store as store_mod
from project_board.loop import BoardLoop
from project_board.store import BeadsBoard

pytestmark = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH (CI installs it and sets PB_REQUIRE_BR=1)",
)

LOGGER = "protoagent.plugins.project_board"


@pytest.fixture
def task(tmp_path, monkeypatch):
    """A task card in flight (in_progress) on a real board, and a loop whose store it is."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    fid = board.create_feature(
        "Write the migration note",
        spec="s",
        acceptance_criteria="- a note exists",
        issue_type="task",
        assignee="sister",
    )["id"]
    board._run("update", fid, "--status", "in_progress")  # setup: claimed, being delivered
    monkeypatch.setattr(loop_mod, "get_store", lambda **_kw: board)
    loop = BoardLoop({"coder": "proto", "repo": str(tmp_path)})
    return board, fid, loop


def _failing_dispatch(monkeypatch, *, then=None):
    """The delegate's dispatch fails; ``then`` runs first — whatever else happened while
    the agent was working."""

    async def _dispatch(delegate, prompt, timeout=None):
        if then is not None:
            then()
        raise worktree.WorktreeError("coder dispatch failed: the sister agent is unreachable")

    monkeypatch.setattr(coder_seam, "dispatch_task", _dispatch)


async def test_an_unreadable_card_is_not_blocked_after_a_failed_dispatch(task, monkeypatch, caplog):
    """Fault-inject the re-read. Before: the failed read fell through to a block."""
    board, fid, loop = task
    real_get = board.get_feature
    failing = {"on": False}

    def _get(feature_id):
        if failing["on"]:
            failing["on"] = False
            raise store_mod.BoardTimeout("`br show` timed out after 45s and was stopped")
        return real_get(feature_id)

    monkeypatch.setattr(board, "get_feature", _get)
    _failing_dispatch(monkeypatch, then=lambda: failing.update(on=True))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await loop._drive_task(real_get(fid), object())

    f = board.get_feature(fid)
    assert f["board_state"] == "in_progress" and not f["blocked"]  # left for the sweep, not blocked blind
    assert any("could not be re-read" in r.message and "not blocking" in r.message for r in caplog.records)


async def test_a_task_held_under_its_dispatch_keeps_its_hold(task, monkeypatch):
    """Before: a human's hold placed while the agent worked was blocked over — the drive's
    failure replaced the hold's reason (and its class)."""
    board, fid, loop = task
    _failing_dispatch(
        monkeypatch,
        then=lambda: board.flag_blocked(fid, "hold: legal must sign off on the wording", "terminal"),
    )

    await loop._drive_task(board.get_feature(fid), object())

    f = board.get_feature(fid)
    assert f["blocked"] and f["blocked_class"] == "terminal"
    assert f["blocked_reason"] == "hold: legal must sign off on the wording"


async def test_a_task_still_in_flight_is_blocked_as_before(task, monkeypatch):
    """The carve-outs are for a card that moved or cannot be read. The drive's own card,
    still in_progress, is blocked for triage exactly as before."""
    board, fid, loop = task
    _failing_dispatch(monkeypatch)

    await loop._drive_task(board.get_feature(fid), object())

    f = board.get_feature(fid)
    assert f["blocked"] and "the sister agent is unreachable" in f["blocked_reason"]
