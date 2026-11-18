"""#727: a re-worded or relocated recurring finding is the SAME finding across rounds.

``_finding_identity`` keyed a finding's cross-round identity on exact normalized equality
of (file, claim), and the review gate read "no overlap with an earlier round" as "the
rounds conflict — needs a design call". Reviewer claims are re-worded between rounds and a
fix round relocates the code, so a persisting defect read as new every round and the card
was blocked with the wrong triage instruction.

The fixture is the bd-j5qr2 / #720 review history from the issue (2026-11-11): rounds 3 and
4 report one defect — a consumed repair exemption surviving a restart, so an edit-free reply
publishes a failing candidate — at two files, two lines and in two wordings.
"""

from __future__ import annotations

import json

import pytest

from project_board import worktree
from project_board.loop import BoardLoop
from project_board.loop import reconcile
from project_board.loop._common import _is_review_gate_block
from project_board.store import LABEL_BLOCKED

from tests.test_loop import _GateStore, _inject_fake_findings

_PR = "https://github.com/o/r/pull/9"

# bd-j5qr2 / #720, as reported in #727.
_ROUND_1 = {
    "file": "loop/drive.py",
    "line": 2518,
    "claim": "An edit-free NO_TEST_NEEDED retry is rejected as an empty fix",
}
_ROUND_3 = {
    "file": "worktree.py",
    "line": 2322,
    "claim": (
        "Restarting during a gate-fix round after automatic formatting resurrects the consumed repair "
        "exemption, allowing an edit-free coder reply to publish the still-failing candidate when the "
        "gate budget is exhausted"
    ),
}
_ROUND_4 = {
    "file": "worktree.py",
    "line": 2441,
    "claim": (
        "After a failed gate consumes a repair candidate, preparing a clean base merge and restarting "
        "retains the pre-merge baseline, misclassifies board-generated changes as coder progress, and "
        "allows an edit-free reply to publish the still-failing implementation when the gate budget is "
        "exhausted"
    ),
}
# The same defect as round 4, reported where the code lived BEFORE the fix round moved it.
_ROUND_3_BEFORE_THE_MOVE = {**_ROUND_3, "file": "loop/drive.py", "line": 2518}


class _F:
    """A current blocking finding, as the gate holds it (attributes, not a dict)."""

    def __init__(self, d: dict):
        self.file, self.line, self.claim = d["file"], d["line"], d["claim"]


def _history(*rounds: dict) -> list[dict]:
    return [{"round": i, "head": f"h{i}", "findings": [f]} for i, f in enumerate(rounds, start=1)]


def _blocker(d: dict) -> str:
    """One round's review output; single-token ``evidence`` keeps the #381 grounding guard open."""
    return json.dumps([{**d, "severity": "blocker", "evidence": "x", "verdict": "confirmed"}])


async def _block(monkeypatch, outputs) -> tuple[str, list[str]]:
    """Drive two gate rounds with ``review_fix_max=1`` (bounce, then budget-exhausted block)
    and return the block reason plus the PR comments the gate posted."""
    _inject_fake_findings(monkeypatch)
    loop = BoardLoop({"review_gate": True, "review_fix_max": 1})
    idx = {"i": 0}
    comments: list[str] = []

    async def _run(fid, pr_url):
        out = outputs[min(idx["i"], len(outputs) - 1)]
        idx["i"] += 1
        return f"review…\n```json\n{out}\n```", None

    async def _diff(pr_url, cwd=".", *, max_chars=4000):
        return "diff --git a/worktree.py b/worktree.py"

    async def _head(pr_url, *, cwd="."):
        return "a" * 40

    async def _status(repo_slug, head_sha, *, state, description, target_url="", context="", cwd="."):
        return True

    async def _comment(pr_url, body, *, marker="", cwd="."):
        comments.append(body)
        return True

    monkeypatch.setattr(loop, "_run_review_workflow", _run)
    monkeypatch.setattr(worktree, "pr_diff", _diff)
    monkeypatch.setattr(worktree, "pr_head_sha", _head)
    monkeypatch.setattr(worktree, "post_review_status", _status)
    monkeypatch.setattr(worktree, "post_or_update_pr_comment", _comment)

    store = _GateStore()
    await loop._review_gate(store, "bd-1", _PR, "/repo")  # bounce
    assert not [c for c in store.calls if c[0] == "flag_blocked"], "the first round bounces, it does not block"
    await loop._review_gate(store, "bd-1", _PR, "/repo")  # budget exhausted → block
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert blocked, "the exhausted budget must block the card"
    return blocked[-1][2], comments


# --- the matcher --------------------------------------------------------------------------


def test_reworded_finding_at_another_line_survives():
    """r1: bd-j5qr2 rounds 3 and 4 — same file, another line, re-worded claim — are one finding."""
    loop = BoardLoop({})
    current = _F(_ROUND_4)

    assert loop._surviving_review_findings(_history(_ROUND_3, _ROUND_4), [current]) == [current]


def test_reworded_finding_moved_to_another_file_survives():
    """r1: the file is never required to match — the fix round moved the code from
    loop/drive.py to worktree.py and the claim was re-worded on top."""
    loop = BoardLoop({})
    current = _F(_ROUND_4)
    history = _history(_ROUND_3_BEFORE_THE_MOVE, _ROUND_4)

    assert loop._surviving_review_findings(history, [current]) == [current]
    earlier, exact = loop._earlier_round_match(history, current)
    assert (earlier["file"], earlier["line"], earlier["round"], exact) == ("loop/drive.py", 2518, 1, False)


def test_match_looks_across_every_earlier_round_not_only_the_previous_one():
    """A finding that skipped a round still matches the round that first raised it."""
    loop = BoardLoop({})
    current = _F(_ROUND_4)
    history = _history(_ROUND_3, _ROUND_1, _ROUND_4)

    earlier, exact = loop._earlier_round_match(history, current)
    assert (earlier["round"], exact) == (1, False)


def test_a_different_defect_on_the_same_card_does_not_match():
    """r2: bd-j5qr2 round 1 (an edit-free retry REJECTED) and round 4 (an edit-free reply
    PUBLISHED) share vocabulary — same card, same subsystem — but are different defects."""
    loop = BoardLoop({})
    history = _history(_ROUND_1, _ROUND_4)

    assert loop._surviving_review_findings(history, [_F(_ROUND_4)]) == []
    assert loop._earlier_round_match(history, _F(_ROUND_4)) is None


def test_genuinely_different_claims_in_one_file_do_not_match():
    """r2: sharing a file is not sharing a finding — two different claims still conflict."""
    loop = BoardLoop({})
    first = {"file": "store.py", "line": 10, "claim": "An unlocked write to the label cache races the reader"}
    second = {"file": "store.py", "line": 10, "claim": "The new lock deadlocks when the reconcile re-enters"}

    assert loop._surviving_review_findings(_history(first, second), [_F(second)]) == []


@pytest.mark.parametrize(("a", "b"), [("bug A", "bug B"), ("drops the row", "drops the lock")])
def test_short_claims_match_by_exact_identity_only(a, b):
    """A two- or three-word claim has too few tokens for an overlap to be evidence, so the
    similarity path needs ``_CLAIM_SIMILARITY_MIN_SHARED`` shared tokens."""
    loop = BoardLoop({})
    history = _history({"file": "a.py", "line": 1, "claim": a}, {"file": "a.py", "line": 1, "claim": b})

    assert loop._surviving_review_findings(history, [_F({"file": "a.py", "line": 1, "claim": b})]) == []


def test_exact_repeat_still_matches_and_wins_over_a_similar_claim():
    """r3: an exact (file, claim) repeat matches as before, and is preferred to a merely
    similar claim from another round."""
    loop = BoardLoop({})
    current = _F(_ROUND_4)
    history = _history(_ROUND_3, _ROUND_4, _ROUND_4)

    earlier, exact = loop._earlier_round_match(history, current)
    assert (earlier["round"], exact) == (2, True)
    assert loop._surviving_review_findings(history, [current]) == [current]

    short = {"file": "./pkg\\a.py", "line": 9, "claim": "drops  the   ROW"}
    short_history = _history({"file": "pkg/a.py", "line": 3, "claim": "Drops the row"}, short)
    assert loop._earlier_round_match(short_history, _F(short))[1] is True  # #514 normalization kept


def test_file_breaks_a_tie_between_equally_similar_earlier_claims():
    """The file is a tiebreaker at most: of two earlier findings with the identical claim,
    the one in the current finding's file is the one named."""
    loop = BoardLoop({})
    history = [
        {"round": 1, "head": "h1", "findings": [{**_ROUND_3, "file": "worktree.py", "line": 11}]},
        {"round": 2, "head": "h2", "findings": [{**_ROUND_3, "file": "loop/drive.py", "line": 22}]},
        {"round": 3, "head": "h3", "findings": [_ROUND_4]},
    ]

    earlier, exact = loop._earlier_round_match(history, _F(_ROUND_4))
    assert (earlier["file"], earlier["line"], exact) == ("worktree.py", 11, False)


def test_the_measured_scores_sit_clear_of_the_threshold():
    """The threshold is one module constant, set between what the issue's rounds measure:
    the re-worded pair above it, the different defect on the same card below it."""
    same = reconcile._claim_similarity(
        reconcile._claim_tokens(_ROUND_3["claim"]), reconcile._claim_tokens(_ROUND_4["claim"])
    )
    different = reconcile._claim_similarity(
        reconcile._claim_tokens(_ROUND_1["claim"]), reconcile._claim_tokens(_ROUND_4["claim"])
    )

    assert different < reconcile._CLAIM_SIMILARITY_THRESHOLD < same
    assert same - reconcile._CLAIM_SIMILARITY_THRESHOLD > 0.05
    assert reconcile._CLAIM_SIMILARITY_THRESHOLD - different > 0.05


# --- the block reason, through the gate ---------------------------------------------------


async def test_reworded_relocated_finding_blocks_as_persisting_not_a_design_call(monkeypatch):
    """r1: the bd-j5qr2 rounds, driven through the gate — the block reason says the finding
    PERSISTS, names it, and says where it was matched; it does not ask for a design call."""
    reason, comments = await _block(monkeypatch, [_blocker(_ROUND_3_BEFORE_THE_MOVE), _blocker(_ROUND_4)])

    assert "review findings persist after 1 fix attempt(s)" in reason
    assert "1 surviving across rounds" in reason
    assert "worktree.py:2441" in reason and _ROUND_4["claim"] in reason
    assert "re-worded since round 1, was loop/drive.py:2518" in reason
    assert "design call" not in reason and "rounds conflict" not in reason
    assert _is_review_gate_block({"labels": [LABEL_BLOCKED], "blocked_reason": reason})
    assert any("persist across rounds" in c and "re-worded since round 1" in c for c in comments)
    assert not any("design call" in c for c in comments)


async def test_different_claims_each_round_still_block_as_a_design_call(monkeypatch):
    """r2: two different defects (bd-j5qr2 rounds 1 and 4) still read as rounds in conflict."""
    reason, comments = await _block(monkeypatch, [_blocker(_ROUND_1), _blocker(_ROUND_4)])

    assert "new findings each round after 1 fix attempt(s)" in reason
    assert "rounds conflict" in reason and "needs a design call" in reason
    assert "review findings persist after" not in reason
    assert "re-worded since" not in reason
    assert "live now (1)" in reason and "worktree.py:2441" in reason
    assert _is_review_gate_block({"labels": [LABEL_BLOCKED], "blocked_reason": reason})
    assert any("needs a design call" in c for c in comments)


async def test_exact_repeat_blocks_as_persisting_with_the_unchanged_wording(monkeypatch):
    """r3: an exact repeat is named exactly as before #727 — no 're-worded' note."""
    reason, _ = await _block(monkeypatch, [_blocker(_ROUND_4), _blocker(_ROUND_4)])

    assert "review findings persist after 1 fix attempt(s)" in reason
    assert f"(1 surviving across rounds: worktree.py:2441 {_ROUND_4['claim']}): {_PR}" in reason
    assert "re-worded since" not in reason


async def test_the_bounce_budget_is_unchanged(monkeypatch):
    """r4: matching a recurring finding does not buy it another round — the card still blocks
    on the round after ``review_fix_max`` bounces (asserted inside ``_block``), and the
    default budget is what it was."""
    assert BoardLoop({}).review_fix_max == BoardLoop({"review_gate": True}).review_fix_max
    reason, _ = await _block(monkeypatch, [_blocker(_ROUND_3), _blocker(_ROUND_4)])
    assert "after 1 fix attempt(s)" in reason
