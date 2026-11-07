"""Review fix dispatch recovers the durable bounce after process memory is lost."""

from unittest.mock import AsyncMock

import pytest

from project_board import worktree
from project_board.loop._common import _PENDING_FEEDBACK
from tests.test_loop import FEATURE, _StrandedFixStore, _drive_with, _findings_comment


@pytest.mark.parametrize("feedback", ["", "live feedback", "pending feedback"])
async def test_requeued_bounce_survives_restart(monkeypatch, feedback):
    store = _StrandedFixStore()
    store.set_review_substate("bd-1", "changes-requested", _findings_comment())
    store.labels.append("budget:review-fix:1")
    store.requeue("bd-1")
    feature = {**FEATURE, **store.get_feature("bd-1"), "pr_url": store.pr_url}
    diff = AsyncMock(return_value="+ recovered PR diff")
    monkeypatch.setattr(worktree, "pr_diff", diff)
    monkeypatch.setattr(worktree, "fix_round_unchanged", AsyncMock(return_value=""))
    prompts = []

    async def dispatch(coder, wt, prompt, **kwargs):
        prompts.append(prompt)
        return "fixed"

    def seed(loop):
        assert not loop._ci_feedback and not loop._ci_prior_diff
        monkeypatch.delitem(_PENDING_FEEDBACK, "bd-1", raising=False)
        if feedback == "live feedback":
            loop._ci_feedback["bd-1"] = feedback
        elif feedback:
            monkeypatch.setitem(_PENDING_FEEDBACK, "bd-1", feedback)

    await _drive_with(
        monkeypatch,
        store=store,
        feature=feature,
        seed=seed,
        dispatch=dispatch,
        open_pr=AsyncMock(return_value=store.pr_url),
    )
    assert prompts
    if feedback:
        assert feedback in prompts[0]
        assert "drops data" not in prompts[0]
        diff.assert_not_awaited()
    else:
        assert "drops data" in prompts[0]
        assert "+ recovered PR diff" in prompts[0]
        diff.assert_awaited_once()


@pytest.mark.parametrize("labels", [[], ["review-clean"], ["review-clean", "budget:review-fix:1"]])
async def test_cleared_round_does_not_recover_stale_findings(monkeypatch, labels):
    store = _StrandedFixStore()
    store.comments = [_findings_comment()]
    store.state = "ready"
    store.labels = labels
    feature = {**FEATURE, "pr_url": store.pr_url, "labels": labels}
    diff = AsyncMock()
    monkeypatch.setattr(worktree, "pr_diff", diff)
    monkeypatch.setattr(worktree, "fix_round_unchanged", AsyncMock(return_value=""))
    prompts = []

    async def dispatch(coder, wt, prompt, **kwargs):
        prompts.append(prompt)
        return "done"

    await _drive_with(
        monkeypatch, store=store, feature=feature, dispatch=dispatch, open_pr=AsyncMock(return_value=store.pr_url)
    )
    assert prompts and "drops data" not in prompts[0]
    diff.assert_not_awaited()


@pytest.mark.parametrize("had_tools", [True, False])
async def test_empty_fix_names_missing_findings(monkeypatch, had_tools):
    feature = {
        **FEATURE,
        "pr_url": "https://github.com/o/r/pull/9",
        "labels": ["changes-requested", "budget:review-fix:1"],
    }
    monkeypatch.setattr(worktree, "fix_round_unchanged", AsyncMock(return_value="abc123"))

    def seed(loop):
        monkeypatch.setattr(loop, "_empty_result_signals", lambda fid: (had_tools, "end_turn"))

    _, store = await _drive_with(monkeypatch, feature=feature, seed=seed, open_pr=AsyncMock())
    blocked = [call for call in store.calls if call[0] == "flag_blocked"]
    assert blocked
    assert "fix round dispatched without review findings (lost on restart?)" in blocked[-1][2]
