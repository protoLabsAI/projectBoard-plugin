"""Operator fix briefs survive restart and acknowledge only delivered occurrences."""

from copy import deepcopy
import shutil

import pytest

import project_board as pb
from project_board import loop
from project_board import store as store_mod
from project_board.store import BeadsBoard, BoardError

FID = "bd-757"


@pytest.mark.br_shape
@pytest.mark.skipif(shutil.which(store_mod.BR) is None, reason="real br CLI required (installed in CI)")
def test_delivery_receipt_survives_restart_real_br(tmp_path, monkeypatch):
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    fid = board.create_feature("Durable feedback", spec="Original task")["id"]
    board._run("update", fid, "--add-label", "ready")
    assert board.claim(fid, assignee="test")
    board.open_review(fid, pr_url="https://github.com/acme/repo/pull/7")
    board.record_review_bounce(fid, "X: retain the review fix")
    board.bounce_ci_fail(fid, "Y: failing CI test")
    board.record_ci_fix_feedback(fid, "Y: failing CI test")
    board.requeue(fid)

    # A new store and loop must recover both briefs without the memory bridge.
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    monkeypatch.setattr(loop, "get_store", lambda **kw: board)
    monkeypatch.setattr(loop, "_PENDING_FEEDBACK", {})
    assert loop.has_undelivered_fix_feedback(board, fid)
    prompt = loop.BoardLoop({})._build_prompt(board.get_feature(fid))
    assert "X: retain the review fix" in prompt
    assert "Y: failing CI test" in prompt
    assert any(text.startswith(loop._FEEDBACK_DELIVERED_PREFIX) for text in board.feature_comments(fid))

    # The strict receipt written by prompt construction must persist in beads.
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    assert not loop.has_undelivered_fix_feedback(board, fid)
    prompt = loop.BoardLoop({})._build_prompt(board.get_feature(fid))
    assert "X: retain the review fix" not in prompt
    assert "Y: failing CI test" not in prompt


@pytest.fixture
def card(make_board, monkeypatch):
    bead = dict(
        id=FID,
        title="Fix card",
        description="Original task",
        status="in_progress",
        labels=["in-review"],
        external_ref="https://github.com/acme/repo/pull/7",
        comments=[],
    )

    def run(*args, want_json=False):
        if args[0] == "show":
            return [deepcopy(bead)] if args[1] == FID else []
        if args[:2] == ("comments", "add"):
            bead["comments"].append({"text": args[3]})
        elif args[0] == "update":
            for flag, value in zip(args[2::2], args[3::2]):
                if flag == "--remove-label":
                    bead["labels"] = [label for label in bead["labels"] if label != value]
                elif flag == "--add-label":
                    bead["labels"].append(value)
                else:
                    bead[flag.removeprefix("--")] = value
        else:
            raise AssertionError(args)
        return [] if want_json else ""

    board = make_board(run)
    monkeypatch.setattr("project_board.store.get_store", lambda **kw: board)
    monkeypatch.setattr(loop, "get_store", lambda **kw: board)
    monkeypatch.setattr(loop, "requeue_refusal", lambda fid: "")
    loop._PENDING_FEEDBACK.clear()
    yield board, bead, loop.BoardLoop({})
    loop._PENDING_FEEDBACK.clear()


def invoke(name, **kwargs):
    tool = next(t for t in pb._board_tools({}) if t.name == name)
    result = tool.invoke({"feature_id": FID, **kwargs})
    assert not result.startswith("Error:"), result


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("with_ci", [False, True])
def test_delivery_and_no_replay(card, restart, with_ci):
    board, bead, live = card
    invoke("board_requeue_feature", findings="X: retain the review fix")
    if with_ci:
        # CI bounce leaves the open PR in_progress before its operator requeue.
        bead["status"] = "in_progress"
        invoke("board_requeue_ci_fix", ci_failure="Y: failing CI test")
    if restart:
        loop._PENDING_FEEDBACK.clear()
        live = loop.BoardLoop({})
    assert loop.has_undelivered_fix_feedback(board, FID)
    prompt = live._build_prompt(board.get_feature(FID))
    assert "X: retain the review fix" in prompt
    assert prompt.index("X: retain") < prompt.index("Original task")
    if with_ci:
        assert "Y: failing CI test" in prompt
    assert not loop.has_undelivered_fix_feedback(board, FID)
    for fresh in (live, loop.BoardLoop({})):
        assert "X: retain" not in fresh._build_prompt(board.get_feature(FID))
        assert "Y: failing" not in fresh._build_prompt(board.get_feature(FID))
    # An identical finding in a later bounce is a new occurrence.
    bead["status"] = "in_progress"
    bead["labels"].append("in-review")
    invoke("board_requeue_feature", findings="X: retain the review fix")
    loop._PENDING_FEEDBACK.clear()
    assert loop.has_undelivered_fix_feedback(board, FID)
    assert "X: retain" in live._build_prompt(board.get_feature(FID))


def test_failed_prompt_or_receipt_keeps_feedback(card, monkeypatch):
    board, bead, live = card
    invoke("board_requeue_feature", findings="X")
    feature = board.get_feature(FID)
    del feature["title"]
    with pytest.raises(KeyError):
        live._build_prompt(feature)
    assert loop.has_undelivered_fix_feedback(board, FID)

    def fail(*args):
        raise BoardError("write failed")

    monkeypatch.setattr(board, "record_fix_feedback_delivery", fail)
    with pytest.raises(BoardError, match="write failed"):
        live._build_prompt(board.get_feature(FID))
    assert loop.has_undelivered_fix_feedback(board, FID)


def test_failed_read_and_missing_record(card, monkeypatch):
    board, bead, live = card
    assert not loop.has_undelivered_fix_feedback(board, "missing")
    invoke("board_requeue_feature", findings="X")
    feature = board.get_feature(FID)

    def fail(*args):
        raise BoardError("read failed")

    monkeypatch.setattr(board, "feature_comments", fail)
    with pytest.raises(BoardError, match="read failed"):
        live._build_prompt(feature)
    assert FID in loop._PENDING_FEEDBACK


def test_new_bounce_during_receipt_is_not_delivered(card, monkeypatch):
    board, bead, live = card
    invoke("board_requeue_feature", findings="X")
    record = board.record_fix_feedback_delivery

    def receipt(fid, text):
        bead["comments"].append({"text": "CI fix requested: Y"})
        loop.queue_ci_feedback(fid, "Y")
        record(fid, text)

    monkeypatch.setattr(board, "record_fix_feedback_delivery", receipt)
    assert "CI fix requested: Y" not in live._build_prompt(board.get_feature(FID))
    assert "X" not in loop._PENDING_FEEDBACK[FID]
    loop._PENDING_FEEDBACK.clear()
    assert loop.has_undelivered_fix_feedback(board, FID)
    monkeypatch.setattr(board, "record_fix_feedback_delivery", record)
    prompt = live._build_prompt(board.get_feature(FID))
    assert "CI fix requested: Y" in prompt
    assert "review requested changes: X" not in prompt


def test_partial_memory_does_not_hide_older_review(card):
    board, bead, live = card
    invoke("board_requeue_feature", findings="older review finding")
    loop._PENDING_FEEDBACK.clear()
    bead["status"] = "in_progress"
    invoke("board_requeue_ci_fix", ci_failure="new CI failure")
    prompt = live._build_prompt(board.get_feature(FID))
    assert "older review finding" in prompt
    assert "new CI failure" in prompt
    assert not loop.has_undelivered_fix_feedback(board, FID)


@pytest.mark.parametrize("ci", [False, True])
def test_feedback_write_failure_prevents_requeue(card, monkeypatch, ci):
    board, bead, live = card
    if ci:
        bead["labels"] = []
    run = board._run

    def fail(*args, **kwargs):
        if args[:2] == ("comments", "add"):
            raise BoardError("storage unavailable")
        return run(*args, **kwargs)

    monkeypatch.setattr(board, "_run", fail)
    tool = next(t for t in pb._board_tools({}) if t.name == ("board_requeue_ci_fix" if ci else "board_requeue_feature"))
    result = tool.invoke({"feature_id": FID, **({"ci_failure": "Y"} if ci else {"findings": "X"})})
    assert result == "Error: storage unavailable"
    assert bead["status"] == "in_progress"
    assert not bead["comments"]
    assert FID not in loop._PENDING_FEEDBACK
