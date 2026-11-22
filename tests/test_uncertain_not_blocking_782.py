"""Verify-pass uncertainty cannot bounce a card, even with grounded evidence (#782)."""

import json

import pytest

from project_board import worktree
from project_board.loop._common import partition_by_grounding
from tests.test_loop import _GateStore, _grounding_loop, _inject_fake_findings


PR = "https://github.com/o/r/pull/9"
EVIDENCE = "entries = history_store.search(match, limit=fetch_limit)"
DIFF = f"diff --git a/search.py b/search.py\n--- a/search.py\n+++ b/search.py\n@@ -0,0 +1 @@\n+{EVIDENCE}\n"


@pytest.fixture(autouse=True)
def gate_seams(monkeypatch):
    _inject_fake_findings(monkeypatch)

    async def head(*args, **kwargs):
        return "a" * 40

    async def published(*args, **kwargs):
        return True

    monkeypatch.setattr(worktree, "pr_head_sha", head)
    monkeypatch.setattr(worktree, "post_review_status", published)
    monkeypatch.setattr(worktree, "post_or_update_pr_comment", published)


def finding(verdict="uncertain", severity="major", claim="Private rows may be reachable"):
    result = dict(file="search.py", line=1, severity=severity, claim=claim, evidence=EVIDENCE)
    if verdict is not None:
        result["verdict"] = verdict
    return result


@pytest.mark.parametrize("severity", ["major", "blocker"])
@pytest.mark.parametrize("exhausted", [False, True])
async def test_grounded_uncertain_finding_is_clean_and_visible(monkeypatch, severity, exhausted):
    payload = json.dumps([finding(severity=severity)])
    loop = _grounding_loop(monkeypatch, payload, DIFF)
    parsed = loop._parse_findings(payload)
    assert partition_by_grounding(parsed, DIFF) == (parsed, [])
    if exhausted:
        loop._review_fix_attempts["bd-1"] = loop.review_fix_max
    store = _GateStore()

    await loop._review_gate(store, "bd-1", PR, "/repo")

    assert store.review_states[-1][0] == "review-clean"
    assert store.state == "in_review"
    assert ("requeue", "bd-1") not in store.calls
    assert loop._review_fix_attempts.get("bd-1", 0) == 0
    saved = loop._review_history("bd-1")[0]
    assert saved["head"] == "a" * 40
    assert saved["findings"][0]["verdict"] == "uncertain"
    assert saved["findings"][0]["evidence"] == EVIDENCE
    comments = [c[2] for c in store.calls if c[0] == "comment"]
    assert any("non-blocking" in c and "uncertain" in c and "Private rows may be reachable" in c for c in comments)
    assert all("Review findings (blocking)" not in c for c in comments)


@pytest.mark.parametrize("verdict", ["confirmed", None, "", "unknown"])
async def test_grounded_confirmed_or_unset_or_unknown_still_blocks(monkeypatch, verdict):
    loop = _grounding_loop(monkeypatch, json.dumps([finding(verdict)]), DIFF)
    store = _GateStore()
    await loop._review_gate(store, "bd-1", PR, "/repo")
    assert store.review_states[-1][0] == "changes-requested"
    assert ("requeue", "bd-1") in store.calls


async def test_mixed_round_keeps_uncertain_out_of_required_fixes_and_recovery(monkeypatch):
    payload = json.dumps([finding(), finding("confirmed", claim="Confirmed defect")])
    loop = _grounding_loop(monkeypatch, payload, DIFF)
    store = _GateStore()
    await loop._review_gate(store, "bd-1", PR, "/repo")
    note = store.review_states[-1][1]
    assert "Confirmed defect" in note
    assert "Private rows may be reachable" not in note
    assert len(loop._review_history("bd-1")[0]["findings"]) == 2
    comments = [c[2] for c in store.calls if c[0] == "comment"]
    monkeypatch.setattr(store, "feature_comments", lambda fid: [note, *comments], raising=False)
    loop._ci_feedback.clear()
    await loop._reinject_review_feedback(store, "bd-1", PR, "/repo")
    context, required = loop._ci_feedback["bd-1"].split("## Required fixes — current blocking findings\n")
    assert "Private rows may be reachable" in context
    assert required.endswith(note)
    assert "Private rows may be reachable" not in required


async def test_refuted_finding_remains_non_blocking_without_uncertain_comment(monkeypatch):
    loop = _grounding_loop(monkeypatch, json.dumps([finding("refuted")]), DIFF)
    store = _GateStore()
    await loop._review_gate(store, "bd-1", PR, "/repo")
    assert store.review_states[-1][0] == "review-clean"
    assert not [c for c in store.calls if c[0] == "comment" and "uncertain" in c[2]]


async def test_comment_failure_does_not_block_or_drop_history(monkeypatch):
    loop = _grounding_loop(monkeypatch, json.dumps([finding()]), DIFF)
    store = _GateStore()

    def unavailable(*args, **kwargs):
        raise RuntimeError("comment store unavailable")

    monkeypatch.setattr(store, "comment", unavailable)
    await loop._review_gate(store, "bd-1", PR, "/repo")
    assert store.review_states[-1][0] == "review-clean"
    assert loop._review_history("bd-1")[0]["findings"][0]["verdict"] == "uncertain"
