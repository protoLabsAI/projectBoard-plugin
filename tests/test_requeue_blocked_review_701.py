"""Operator design decisions go straight to a fix round on a gate-blocked PR (#701)."""

from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

import project_board as pb
from project_board import loop, worktree
from project_board.loop import _common

FID = "bd-701"
PR_URL = "https://github.com/acme/widget/pull/7"
REASON = (
    "new findings each round after 3 fix attempt(s) (rounds conflict) — "
    "needs a design call; needs human review: " + PR_URL
)


@pytest.fixture
def card(make_board, monkeypatch):
    bead = {
        "id": FID,
        "title": "Choose a design",
        "description": "Original implementation task",
        "status": "in_progress",
        "labels": ["in-review", "blocked", "blocked-class:terminal", "budget:review-fix:3", "budget:ci-fix:2"],
        "external_ref": PR_URL,
        "comments": [{"text": "blocked: " + REASON}],
    }
    writes = []

    def run(*args, want_json=False):
        if args[0] == "show":
            return [deepcopy(bead)] if args[1] == FID else []
        writes.append(args)
        if args[0] == "update":
            for flag, value in zip(args[2::2], args[3::2]):
                if flag == "--remove-label":
                    bead["labels"] = [label for label in bead["labels"] if label != value]
                elif flag == "--add-label":
                    bead["labels"].append(value)
                else:
                    bead[flag.removeprefix("--")] = value
        elif args[:2] == ("comments", "add"):
            bead["comments"].append({"text": args[3]})
        else:
            raise AssertionError(args)
        return [] if want_json else ""

    board = make_board(run)
    monkeypatch.setattr("project_board.store.get_store", lambda **kw: board)
    live = loop.BoardLoop({})
    live._budget_cache("review-fix")[FID] = 3
    monkeypatch.setattr(loop, "live_loop", lambda: live)
    monkeypatch.setattr(_common, "live_loop", lambda: live)
    monkeypatch.setattr(loop, "requeue_refusal", lambda fid: "")
    gh = Mock(side_effect=AssertionError("requeue must not contact GitHub"))
    monkeypatch.setattr(worktree, "_gh_sync", gh)
    return board, bead, writes, live, gh


def requeue(**kwargs):
    tool = next(t for t in pb._board_tools({}) if t.name == "board_requeue_feature")
    return tool.invoke({"feature_id": FID, "findings": "use approach B", **kwargs})


@pytest.mark.asyncio
async def test_design_call_requeues_same_pr_without_rearming_gate(card):
    board, bead, writes, live, gh = card
    result = requeue()
    assert not result.startswith("Error:"), result
    assert json.loads(result)["state"] == "ready"
    feature = board.get_feature(FID)
    assert feature["pr_url"] == PR_URL
    assert "blocked" not in feature["labels"]
    assert "review requested changes: use approach B" in [c["text"] for c in bead["comments"]]
    assert "use approach B" in loop._PENDING_FEEDBACK[FID]
    prompt = live._build_prompt(feature)
    assert prompt.index("use approach B") < prompt.index("Original implementation task")
    assert FID not in loop._PENDING_FEEDBACK
    assert "review-pending" not in feature["labels"]
    assert not any("review-pending" in args for args in writes)
    gh.assert_not_called()
    assert FID not in live._budget_cache("review-fix")
    assert await live._budget_get(board, FID, "review-fix") == 0
    assert "budget:ci-fix:2" in feature["labels"]


@pytest.mark.parametrize(
    "reason",
    [
        "parked by operator until the design meeting",
        "transient failure — needs triage",
        "3 fix attempt(s) but no human-review marker",
        "external review FAILED — needs human review",
    ],
)
def test_other_blocks_are_still_refused(card, reason):
    board, bead, writes, live, gh = card
    bead["comments"] = [{"text": "blocked: " + reason}]
    assert requeue() == "Error: review bounce expects in_review, got 'blocked'"
    assert board.get_feature(FID)["board_state"] == "blocked"
    assert writes == []
    assert live._budget_cache("review-fix")[FID] == 3
    assert FID not in loop._PENDING_FEEDBACK
    gh.assert_not_called()


def test_busy_loop_leaves_gate_block_untouched(card, monkeypatch):
    board, _, writes, live, gh = card
    monkeypatch.setattr(loop, "requeue_refusal", lambda fid: "loop is working the card")
    assert requeue() == "Error: loop is working the card"
    # The shared API must enforce the same guard independently of the tool wrapper.
    from project_board.api import review_bounce
    from project_board.store import BoardError

    with pytest.raises(BoardError, match="loop is working the card"):
        review_bounce(board, FID, "use approach B")
    assert board.get_feature(FID)["board_state"] == "blocked"
    assert writes == []
    assert live._budget_cache("review-fix")[FID] == 3
    assert FID not in loop._PENDING_FEEDBACK
    gh.assert_not_called()


def test_escalation_keeps_existing_blocked_state_refusal(card):
    board, _, writes, _, _ = card
    assert requeue(escalate=True) == "Error: review bounce expects in_review, got 'blocked'"
    assert board.get_feature(FID)["board_state"] == "blocked"
    assert writes == []


@pytest.mark.parametrize("missing", ["in-review", "pr_url"])
def test_gate_fingerprint_alone_cannot_clear_an_unrelated_card(card, missing):
    board, bead, writes, _, _ = card
    if missing == "in-review":
        bead["labels"].remove("in-review")
    else:
        bead["external_ref"] = ""
    assert requeue() == "Error: review bounce expects in_review, got 'blocked'"
    assert board.get_feature(FID)["board_state"] == "blocked"
    assert writes == []


def test_shared_api_without_live_loop(card, monkeypatch):
    from project_board.api import review_bounce
    from project_board.store import budgets_from_labels

    board, _, _, _, gh = card
    monkeypatch.setattr(loop, "live_loop", lambda: None)
    out = review_bounce(board, FID, "use approach B")
    assert out["feature"]["board_state"] == "ready"
    assert out["feature"]["pr_url"] == PR_URL
    assert budgets_from_labels(out["feature"]["labels"]).get("review-fix", 0) == 0
    gh.assert_not_called()


def test_missing_feature_preserves_existing_error(card):
    from project_board.api import review_bounce
    from project_board.store import BoardError

    board, _, writes, _, _ = card
    with pytest.raises(BoardError, match="unknown feature"):
        review_bounce(board, "bd-missing", "use approach B")
    assert writes == []


def test_failed_feature_read_does_not_mutate_card(card, monkeypatch):
    from project_board.store import BoardError

    board, _, writes, _, _ = card
    monkeypatch.setattr(board, "get_feature", Mock(side_effect=BoardError("read failed")))
    assert requeue() == "Error: read failed"
    assert writes == []
    assert FID not in loop._PENDING_FEEDBACK


def test_stale_gate_reason_does_not_reset_ordinary_review_budget(card):
    board, bead, _, live, _ = card
    bead["labels"].remove("blocked")
    assert json.loads(requeue())["state"] == "ready"
    assert "budget:review-fix:3" in board.get_feature(FID)["labels"]
    assert live._budget_cache("review-fix")[FID] == 3
