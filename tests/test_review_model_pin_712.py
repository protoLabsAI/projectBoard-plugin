"""The workflow gate's model override is local to the review run (#712)."""

import sys
import types
from contextlib import contextmanager
from contextvars import ContextVar

import pytest

from project_board.loop import BoardLoop

PR = "https://github.com/acme/repo/pull/12"
MODEL = "anthropic-oauth:claude-sonnet-5"


@pytest.fixture
def model_api(monkeypatch):
    try:
        from graph import subagent_model
    except ImportError:
        subagent_model = types.ModuleType("graph.subagent_model")
        current = ContextVar("test_review_model", default=None)

        @contextmanager
        def turn_model_scope(model):
            token = current.set(model)
            try:
                yield
            finally:
                current.reset(token)

        subagent_model.turn_model_scope = turn_model_scope
        subagent_model.current_turn_model = current.get
        monkeypatch.setitem(sys.modules, "graph.subagent_model", subagent_model)
    return subagent_model


def _loop(monkeypatch, current_model, cfg=None, *, fail=False):
    seen = []

    def runner(name, inputs):
        # Observe both coroutine creation and execution in _within's child task.
        seen.append(current_model())

        async def run():
            seen.append(current_model())
            if fail:
                raise RuntimeError("workflow failed")
            return {"output": "[]"}

        return run()

    runtime = types.ModuleType("runtime")
    state = types.ModuleType("runtime.state")
    state.STATE = types.SimpleNamespace(workflow_run=runner)
    runtime.state = state
    monkeypatch.setitem(sys.modules, "runtime", runtime)
    monkeypatch.setitem(sys.modules, "runtime.state", state)
    loop = BoardLoop(cfg or {})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda *_: None)
    return loop, seen


@pytest.mark.parametrize(
    "cfg,expected",
    [({}, MODEL), ({"review_model": ""}, None), ({"review_model": "custom:model"}, "custom:model")],
)
async def test_workflow_model(monkeypatch, model_api, cfg, expected):
    loop, seen = _loop(monkeypatch, model_api.current_turn_model, cfg)
    before = model_api.current_turn_model()
    assert await loop._run_review_workflow("bd-1", PR) == ("[]", None)
    assert seen == [expected, expected]
    assert model_api.current_turn_model() == before


@pytest.mark.parametrize("fail", [False, True])
async def test_restores_previous_model(monkeypatch, model_api, fail):
    loop, seen = _loop(monkeypatch, model_api.current_turn_model, fail=fail)
    with model_api.turn_model_scope("previous:model"):
        output, reason = await loop._run_review_workflow("bd-1", PR)
        assert model_api.current_turn_model() == "previous:model"
    assert seen == [MODEL, MODEL]
    assert (output is None) == fail
    assert (reason is not None) == fail


@pytest.mark.parametrize("missing", [None, types.ModuleType("graph.subagent_model")])
async def test_missing_host_model_api_still_runs(monkeypatch, missing):
    monkeypatch.setitem(sys.modules, "graph.subagent_model", missing)
    loop, seen = _loop(monkeypatch, lambda: None)
    assert await loop._run_review_workflow("bd-1", PR) == ("[]", None)
    assert seen == [None, None]
