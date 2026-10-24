"""#514 slice 2: the review-budget block reason distinguishes "persist" from "conflict".

When the review gate exhausts its fix budget it used to ALWAYS write "review findings
persist after N fix attempt(s) — needs human review", even when every round had raised a
DIFFERENT finding (bd-49yo). Using slice 1's per-round history, the gate now compares
findings by identity (normalized file + claim):

- a finding from an earlier round that survives into the last round → keep the "persist"
  wording and NAME the survivor(s);
- otherwise → "new findings each round after N fix attempt(s) (rounds conflict) — needs a
  design call; needs human review".

Both wordings keep "<n> fix attempt(s)" and "needs human review" so ``_is_review_gate_block``
still classes the block as a review-gate block (unchanged behavior for the operator unblock).
"""

from __future__ import annotations

import json

import pytest

from project_board import worktree
from project_board.loop import BoardLoop
from project_board.loop._common import _REVIEW_GATE_BLOCK_MARKERS, _is_review_gate_block
from project_board.store import LABEL_BLOCKED

from tests.test_loop import _GateStore, _inject_fake_findings

_PR = "https://github.com/o/r/pull/9"


def _finding(claim: str, *, file: str = "a.py", line: int = 3) -> dict:
    """A one-finding blocker; ``evidence`` is a single token so the #381 grounding guard
    fails OPEN (it only demotes a quote the diff demonstrably lacks)."""
    return {"file": file, "line": line, "severity": "blocker", "claim": claim, "evidence": "x", "verdict": "confirmed"}


def _seq_loop(monkeypatch, outputs):
    """A review_gate loop whose ``_run_review_workflow`` returns ``outputs`` in order (the
    last repeats), with the PR diff stubbed to one findings ground in. ``review_fix_max=1``
    so the budget exhausts on the SECOND round — the first round bounces, the second blocks,
    leaving two rounds on the card's history to compare."""
    loop = BoardLoop({"review_gate": True, "review_fix_max": 1})
    idx = {"i": 0}

    async def _run(fid, pr_url):
        out = outputs[min(idx["i"], len(outputs) - 1)]
        idx["i"] += 1
        return f"review…\n```json\n{out}\n```", None

    async def _diff(pr_url, cwd=".", *, max_chars=4000):
        return "diff --git a/a.py b/a.py"

    async def _head(pr_url, *, cwd="."):
        return "a" * 40

    monkeypatch.setattr(loop, "_run_review_workflow", _run)
    monkeypatch.setattr(worktree, "pr_diff", _diff)
    monkeypatch.setattr(worktree, "pr_head_sha", _head)
    monkeypatch.setattr(worktree, "post_review_status", _ok_status)
    monkeypatch.setattr(worktree, "post_or_update_pr_comment", _capture_comment)
    return loop


_COMMENTS: list[str] = []


async def _ok_status(repo_slug, head_sha, *, state, description, target_url="", context="", cwd="."):
    return True


async def _capture_comment(pr_url, body, *, marker="", cwd="."):
    _COMMENTS.append(body)
    return True


@pytest.fixture(autouse=True)
def _reset_comments():
    _COMMENTS.clear()
    yield


async def _block(loop, store):
    """Drive two gate rounds (bounce, then budget-exhaust block) and return the reason the
    gate flagged the card blocked with."""
    await loop._review_gate(store, "bd-1", _PR, "/repo")  # round 1 — bounce
    await loop._review_gate(store, "bd-1", _PR, "/repo")  # round 2 — budget exhausted → block
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert blocked, "the exhausted budget must block the card"
    return blocked[-1][2]


async def test_same_finding_across_rounds_says_persist_and_names_it(monkeypatch):
    """r1: the SAME finding (same file + claim) in both rounds → the block reason keeps the
    'persist' wording and names the surviving finding."""
    _inject_fake_findings(monkeypatch)
    same = json.dumps([_finding("drops the last row on flush")])
    loop = _seq_loop(monkeypatch, [same, same])
    store = _GateStore()

    reason = await _block(loop, store)

    assert "review findings persist after 1 fix attempt(s)" in reason
    assert "needs human review" in reason
    assert "drops the last row on flush" in reason  # the survivor is named
    assert "rounds conflict" not in reason
    # …and the PR findings comment calls out the survivor too.
    assert any("persist across rounds" in c and "drops the last row on flush" in c for c in _COMMENTS)


async def test_disjoint_findings_each_round_says_rounds_conflict(monkeypatch):
    """r2: a DIFFERENT finding each round → the block reason switches to the rounds-conflict
    wording and asks for a design call, not another patch."""
    _inject_fake_findings(monkeypatch)
    loop = _seq_loop(
        monkeypatch,
        [
            json.dumps([_finding("unlocked write races the reader", file="a.py", line=10)]),
            json.dumps([_finding("the new lock deadlocks on reentry", file="b.py", line=22)]),
        ],
    )
    store = _GateStore()

    reason = await _block(loop, store)

    assert "new findings each round after 1 fix attempt(s)" in reason
    assert "rounds conflict" in reason
    assert "needs a design call" in reason
    assert "needs human review" in reason
    assert "review findings persist after" not in reason
    assert any("rounds conflict" in c and "needs a design call" in c for c in _COMMENTS)


async def test_both_wordings_still_class_as_a_review_gate_block(monkeypatch):
    """r3: whichever wording the gate writes, ``_is_review_gate_block`` still reads True, so
    the operator-unblock class is unchanged — and neither a transient CI block nor the
    external-review block (which also says 'needs human review') is misread as this one."""
    _inject_fake_findings(monkeypatch)

    persist = await _block(_seq_loop(monkeypatch, [json.dumps([_finding("same bug")])] * 2), _GateStore())
    conflict = await _block(
        _seq_loop(
            monkeypatch, [json.dumps([_finding("bug A", file="a.py")]), json.dumps([_finding("bug B", file="b.py")])]
        ),
        _GateStore(),
    )

    for reason in (persist, conflict):
        assert all(marker in reason for marker in _REVIEW_GATE_BLOCK_MARKERS)
        assert _is_review_gate_block({"labels": [LABEL_BLOCKED], "blocked_reason": reason})

    # Sibling blocks that also end "needs …" must NOT be misclassed as this gate block.
    ci = "CI still failing after 2 fix attempt(s) — needs triage: " + _PR
    external = (
        "external review FAILED at head abc1234 (QA panel) after 2 review fix round(s) — needs human review: " + _PR
    )
    assert not _is_review_gate_block({"labels": [LABEL_BLOCKED], "blocked_reason": ci})
    assert not _is_review_gate_block({"labels": [LABEL_BLOCKED], "blocked_reason": external})


def test_surviving_findings_compare_by_normalized_identity():
    """Identity is normalized file + claim: cosmetic drift (``./`` prefix, backslashes,
    whitespace, case) in how the same finding is quoted across rounds still compares equal."""
    loop = BoardLoop({})
    history = [
        {"round": 1, "head": "h1", "findings": [{"file": "pkg/a.py", "line": 3, "claim": "Drops the row"}]},
        {"round": 2, "head": "h2", "findings": [{"file": "./pkg\\a.py", "line": 9, "claim": "drops  the   row"}]},
    ]

    class _F:
        def __init__(self, file, line, claim):
            self.file, self.line, self.claim = file, line, claim

    survivor = _F("pkg/a.py", 9, "drops the row")
    fresh = _F("other.py", 1, "a brand new concern")
    surviving = loop._surviving_review_findings(history, [survivor, fresh])
    assert [f.claim for f in surviving] == ["drops the row"]  # matched despite the drift; the fresh one dropped


def test_single_round_history_keeps_persist_wording():
    """With fewer than two rounds there is nothing earlier to compare against, so the pre-#514
    'persist' reading is kept: every current finding counts as surviving."""
    loop = BoardLoop({})

    class _F:
        file, line, claim = "a.py", 3, "the only finding"

    history = [{"round": 1, "head": "h", "findings": [{"file": "a.py", "line": 3, "claim": "the only finding"}]}]
    assert [f.claim for f in loop._surviving_review_findings(history, [_F()])] == ["the only finding"]
    assert loop._surviving_review_findings([], [_F()])  # empty history also reads as persist
