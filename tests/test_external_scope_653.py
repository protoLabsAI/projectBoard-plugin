"""Pre-existing panel findings must not enter the fix-budget/requeue path (#653)."""

import json
from unittest.mock import AsyncMock, Mock

import pytest

from project_board import external_review
from project_board.loop.reconcile import ReconcileMixin

HEAD = "a" * 40
PR = "https://github.com/protoLabsAI/protoAgent/pull/4581"


def finding(**overrides):
    return {
        "file": "src/graph/config.py",
        "line": 418,
        "severity": "major",
        "verdict": "confirmed",
        "claim": "Existing configuration issue",
        "scope": "pre-existing",
        **overrides,
    }


def view_for(findings):
    payload = findings if isinstance(findings, str) else json.dumps(findings)
    return {
        "headRefOid": HEAD,
        "reviews": [
            {
                "author": {"login": "protoreview[bot]"},
                "state": "CHANGES_REQUESTED",
                "body": f"<!-- protoagent-qa-review head={HEAD} verdict=FAIL -->\n```json\n{payload}\n```",
            }
        ],
    }


def harness():
    loop = ReconcileMixin()
    loop._external_review_cfg_for = Mock(return_value=external_review.Config())
    loop._record_external_bounce = AsyncMock()
    loop._notify_operator = Mock()
    return loop, Mock()


@pytest.mark.parametrize("severity", ["major", "blocker", " MAJOR "])
@pytest.mark.parametrize("verdict", ["confirmed", "verified"])
async def test_pre_existing_fail_holds_without_entering_bounce(severity, verdict):
    loop, store = harness()
    findings = [finding(severity=severity, verdict=verdict), finding(line=419), finding(severity="minor", scope="new")]
    for _ in range(2):
        assert await loop._reconcile_external_review(store, {"id": "card"}, PR, ".", view=view_for(findings))
    loop._record_external_bounce.assert_not_awaited()
    # The only store write is the once-per-head hold: no budget, bounce, or requeue write.
    assert len(store.mock_calls) == 1
    store.comment.assert_called_once()
    fid, note = store.comment.call_args.args
    assert fid == "card"
    assert "Advisory (pre-existing, not blocking)" in note
    assert "src/graph/config.py:418" in note
    assert "src/graph/config.py:419" in note
    assert PR in note


@pytest.mark.parametrize(
    "findings",
    [
        [finding(), finding(scope="in-diff")],
        [finding(scope=None)],
        [{k: v for k, v in finding().items() if k != "scope"}],
        [finding(), finding(scope="new", verdict="unconfirmed")],
        "not JSON",
        '{"findings": "invalid"}',
        [],
        [finding(severity="minor")],
        [finding(verdict="unconfirmed")],
    ],
)
async def test_other_failures_keep_existing_bounce(findings):
    loop, store = harness()
    view = view_for(findings)
    assert await loop._reconcile_external_review(store, {"id": "card"}, PR, ".", view=view)
    loop._record_external_bounce.assert_awaited_once()
    kwargs = loop._record_external_bounce.call_args.kwargs
    assert kwargs["head"] == HEAD
    assert kwargs["reject_label"] == "external review FAILED"
    assert kwargs["rendered"] == external_review.render_findings(
        external_review.evaluate(view, external_review.Config()), PR
    )
    store.comment.assert_not_called()


def test_blocking_and_separate_advisory_rendering():
    old = finding()
    new = finding(file="changed.py", scope="in-diff", claim="New issue")
    legacy = {k: v for k, v in new.items() if k != "scope"}
    assert external_review.blocking([old, new, legacy]) == [new, legacy]
    verdict = external_review.evaluate(view_for([old, new]), external_review.Config())
    rendered = external_review.render_findings(verdict, PR)
    required, advisory = rendered.split("Advisory (pre-existing, not blocking):")
    assert "Blocking findings (1)" in required
    assert "changed.py:418" in required
    assert "src/graph/config.py:418" not in required
    assert "src/graph/config.py:418" in advisory
    assert "Existing configuration issue" in advisory
    assert "other finding(s)" not in rendered
