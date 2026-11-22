"""Review statuses name the routing that produced this head's verdict (#712)."""

import sys
from unittest.mock import AsyncMock

import pytest

from project_board import worktree
from tests import test_review_model_pin_712 as pin
from tests.test_loop import _GateStore, _BLOCKER, _inject_fake_adapters, _inject_fake_findings, _record_review_statuses

model_api = pin.model_api


@pytest.fixture
def gate_env(monkeypatch):
    _inject_fake_findings(monkeypatch)
    monkeypatch.setattr(worktree, "pr_head_sha", AsyncMock(return_value="a" * 40))
    monkeypatch.setattr(worktree, "pr_diff", AsyncMock(return_value=""))
    return _record_review_statuses(monkeypatch)[0]


@pytest.mark.parametrize("model", [pin.MODEL, "", "custom:" + "x" * 200])
async def test_clean_status(monkeypatch, gate_env, model_api, model):
    loop, _ = pin._loop(monkeypatch, model_api.current_turn_model, {"review_model": model})
    monkeypatch.setattr(loop, "_release_board_draft", AsyncMock())
    await loop._review_gate(_GateStore(), "bd-1", pin.PR, "/repo")
    verdict = "Review gate clean — 0 finding(s), none blocking"
    used = model or "host routing"
    assert gate_env[-1]["description"] == (verdict + " · " + used)[:140]
    assert gate_env[0]["description"] == ("Review gate running… · " + used)[:140]
    assert len(gate_env[-1]["description"]) <= 140
    assert "bd-1" not in loop._review_model_used


async def test_fallback_status(monkeypatch, gate_env, model_api):
    loop, _ = pin._loop(monkeypatch, model_api.current_turn_model, fail=True)
    monkeypatch.setattr(loop, "_resolve_delegate", lambda *_: {})
    monkeypatch.setattr(loop, "_release_board_draft", AsyncMock())
    _inject_fake_adapters(monkeypatch, AsyncMock(return_value="[]"))
    await loop._review_gate(_GateStore(), "bd-1", pin.PR, "/repo")
    assert gate_env[-1]["description"].endswith(f" · reviewer {loop.reviewer_name}")


async def test_missing_model_api(monkeypatch, gate_env):
    monkeypatch.setitem(sys.modules, "graph.subagent_model", None)
    loop, _ = pin._loop(monkeypatch, lambda: None)
    monkeypatch.setattr(loop, "_release_board_draft", AsyncMock())
    await loop._review_gate(_GateStore(), "bd-1", pin.PR, "/repo")
    assert gate_env[-1]["description"].endswith(" · host routing")


@pytest.mark.parametrize("exhausted", [False, True])
async def test_blocking_status(monkeypatch, gate_env, model_api, exhausted):
    loop, _ = pin._loop(monkeypatch, model_api.current_turn_model)
    from runtime.state import STATE

    STATE.workflow_run = AsyncMock(return_value={"output": _BLOCKER})
    if exhausted:
        loop._review_fix_attempts["bd-1"] = loop.review_fix_max
    await loop._review_gate(_GateStore(), "bd-1", pin.PR, "/repo")
    assert gate_env[-1]["state"] == "failure"
    assert gate_env[-1]["description"].endswith(" · " + pin.MODEL)
    assert "bd-1" not in loop._review_model_used


async def test_exception_cleans_attribution(monkeypatch, gate_env, model_api):
    loop, _ = pin._loop(monkeypatch, model_api.current_turn_model)
    monkeypatch.setattr(loop, "_parse_findings", lambda _: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        await loop._review_gate(_GateStore(), "bd-1", pin.PR, "/repo")
    assert "bd-1" not in loop._review_model_used


async def test_retry_replaces_previous_attribution(monkeypatch, gate_env, model_api):
    loop, _ = pin._loop(monkeypatch, model_api.current_turn_model)
    monkeypatch.setattr(loop, "_release_board_draft", AsyncMock())
    await loop._review_gate(_GateStore(), "bd-1", pin.PR, "/repo")
    monkeypatch.setitem(sys.modules, "graph.subagent_model", None)
    await loop._review_gate(_GateStore(), "bd-1", pin.PR, "/repo")
    assert gate_env[-1]["description"].endswith(" · host routing")
    assert "bd-1" not in loop._review_model_used


async def test_failed_run_cleans_attribution(monkeypatch, gate_env, model_api):
    loop, _ = pin._loop(monkeypatch, model_api.current_turn_model, fail=True)
    await loop._review_gate(_GateStore(), "bd-1", pin.PR, "/repo")
    assert [status["state"] for status in gate_env] == ["pending"]
    assert "bd-1" not in loop._review_model_used


@pytest.mark.parametrize(
    "verdict",
    [
        "Review gate: 1 blocking finding(s) — a fix round is in progress",
        "Review gate: 1 finding(s) persist after 2 fix attempt(s) — needs human review",
        "Review gate: new findings each round after 2 fix attempt(s) (rounds conflict) — needs a design call",
    ],
)
def test_long_model_preserves_blocking_verdict(verdict):
    from project_board.loop import BoardLoop

    description = BoardLoop._review_model_description(verdict, "custom:" + "x" * 200)
    assert len(description) == 140
    assert description.split(" · ")[0] == verdict
