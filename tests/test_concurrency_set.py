"""board_concurrency_get / board_concurrency_set: operator-gated, persisted, live (host-free)."""

import copy
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
from _host_apply import apply_updates

from project_board import _board_tools, settings_api
from project_board.loop import BoardLoop

OPERATOR = {"configurable": {"auth": {"tier": "operator", "subject": "alice"}}}


@pytest.fixture
def host(monkeypatch, tmp_path):
    live = SimpleNamespace(plugin_config={"project_board": {"max_concurrent": 2, "coder_solve_k": 3}})
    loop = BoardLoop(copy.deepcopy(live.plugin_config["project_board"]))
    calls = []

    def apply(compute):
        patch = compute(live)
        calls.append(patch)
        apply_updates(live.plugin_config, patch)
        loop.reload(live)
        return True, []

    module = ModuleType("graph.plugins.host")
    module.HOST = SimpleNamespace(apply_settings=apply)
    monkeypatch.setitem(sys.modules, "graph.plugins.host", module)
    monkeypatch.setattr(settings_api, "_live_section", lambda **kw: copy.deepcopy(live.plugin_config["project_board"]))
    monkeypatch.setattr(settings_api, "_boot_values", None)
    settings_api.capture_boot_settings(loop.cfg)
    monkeypatch.setenv("PROJECT_BOARD_DATA_DIR", str(tmp_path))
    tools = {t.name: t for t in _board_tools({})}
    return live, loop, calls, tmp_path / "concurrency-audit.jsonl", tools


async def test_operator_call_persists_applies_live_and_audits(host):
    live, loop, calls, audit, tools = host
    setter = tools["board_concurrency_set"]
    assert "config" not in setter.tool_call_schema.model_fields
    assert "only call this when the operator explicitly asks" in " ".join(setter.description.split()).lower()
    result = json.loads(await setter.ainvoke({"max_concurrent": 4, "reason": "Josh: 'run 4 coders'"}, config=OPERATOR))
    assert result["old_max_concurrent"] == 2
    assert result["max_concurrent"] == 4
    assert result["live"] is True
    assert result["peak_coder_sessions"] == 12
    assert "4 × 3 = 12" in result["peak_formula"]
    # Persisted through the Settings write (one host save) and applied to the running loop.
    assert calls == [{"project_board": {"max_concurrent": 4}}]
    assert live.plugin_config["project_board"]["max_concurrent"] == 4
    assert loop.max_concurrent == 4
    records = [json.loads(line) for line in audit.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["who"] == "alice" and records[0]["when"]
    assert records[0]["reason"] == "Josh: 'run 4 coders'"
    assert (records[0]["old"], records[0]["new"]) == (2, 4)
    got = json.loads(tools["board_concurrency_get"].invoke({}))
    assert got["max_concurrent"] == 4 and got["peak_coder_sessions"] == 12


@pytest.mark.parametrize("auth", [{}, {"tier": "agent"}, {"tier": "viewer"}])
async def test_refuses_without_operator_context(host, auth):
    live, loop, calls, audit, tools = host
    before = copy.deepcopy(live.plugin_config)
    result = await tools["board_concurrency_set"].ainvoke(
        {"max_concurrent": 4, "reason": "operator asked"}, config={"configurable": {"auth": auth}}
    )
    assert result.startswith("Error:") and "board_concurrency_set requires operator" in result
    assert not calls and not audit.exists()
    assert live.plugin_config == before and loop.max_concurrent == 2


@pytest.mark.parametrize("value", [0, -1, 9, 64])
async def test_refuses_out_of_bounds(host, value):
    live, loop, calls, audit, tools = host
    result = await tools["board_concurrency_set"].ainvoke(
        {"max_concurrent": value, "reason": "operator asked"}, config=OPERATOR
    )
    assert result.startswith("Error:") and "from 1 to 8" in result
    assert not calls and not audit.exists()
    assert live.plugin_config["project_board"]["max_concurrent"] == 2


async def test_reason_is_required(host):
    live, loop, calls, audit, tools = host
    setter = tools["board_concurrency_set"]
    assert "reason" in setter.tool_call_schema.model_json_schema()["required"]
    for reason in ("", "   "):
        result = await setter.ainvoke({"max_concurrent": 3, "reason": reason}, config=OPERATOR)
        assert result.startswith("Error:") and "reason is required" in result
    assert not calls and not audit.exists()
    assert live.plugin_config["project_board"]["max_concurrent"] == 2


def test_peak_formula_names_the_sessions_cap_when_it_binds():
    # protoEngineer 2026-11-08: coder_solve_k 3 with max_concurrent_sessions 1 serializes a card's
    # candidates, so the peak is max_concurrent × 1. The formula must say why it isn't × k.
    view = settings_api._concurrency_view(
        {"max_concurrent": 4, "coder_solve_k": 3, "coder_solve": True, "max_concurrent_sessions": 1}
    )
    assert view["peak_coder_sessions"] == 4
    assert "4 × 1 = 4" in view["peak_formula"]
    assert "max_concurrent_sessions 1" in view["peak_formula"] and "coder_solve_k 3" in view["peak_formula"]
    off = settings_api._concurrency_view({"max_concurrent": 4, "coder_solve_k": 3, "coder_solve": False})
    assert off["peak_coder_sessions"] == 4 and "coder_solve off" in off["peak_formula"]


# Chat-turn authority (protoEngineer 2026-11-08): core never puts `configurable.auth` on a
# chat turn's tool config; it stamps `operator_chat_turn` into graph state instead, which
# the tool reads through InjectedState — the same check core's record_directive makes.
CHAT_TURN = {"operator_chat_turn": True}


def test_injected_state_stays_out_of_the_model_schema(host):
    *_, tools = host
    assert "state" not in tools["board_concurrency_set"].tool_call_schema.model_fields


async def test_operator_chat_turn_state_authorizes_and_persists(host):
    live, loop, calls, audit, tools = host
    result = json.loads(
        await tools["board_concurrency_set"].ainvoke(
            {"max_concurrent": 3, "reason": "Josh: 'give me 3 coders'", "state": CHAT_TURN}
        )
    )
    assert result["max_concurrent"] == 3 and loop.max_concurrent == 3
    assert calls == [{"project_board": {"max_concurrent": 3}}]
    assert json.loads(audit.read_text())["who"] == "operator"


@pytest.mark.parametrize(
    "state",
    [
        None,
        {},
        {"operator_chat_turn": False},
        {"operator_chat_turn": "true"},
        {"operator_chat_turn": True, "subagent_fence": ["board_concurrency_set"]},
    ],
)
async def test_state_without_operator_chat_turn_is_refused(host, state):
    live, loop, calls, audit, tools = host
    result = await tools["board_concurrency_set"].ainvoke({"max_concurrent": 3, "reason": "asked", "state": state})
    assert result.startswith("Error:") and "board_concurrency_set requires operator" in result
    assert not calls and not audit.exists() and loop.max_concurrent == 2


async def test_real_tool_node_injects_operator_chat_turn(host):
    """InjectedState really reaches the tool under a LangGraph ToolNode (what create_agent
    runs tools through), and the model's own args can't smuggle the flag in."""
    from typing import Annotated, TypedDict

    from langchain_core.messages import AIMessage, ToolMessage
    from langgraph.graph import END, START, StateGraph
    from langgraph.graph.message import add_messages
    from langgraph.prebuilt import ToolNode

    live, loop, calls, audit, tools = host

    class State(TypedDict, total=False):
        messages: Annotated[list, add_messages]
        operator_chat_turn: bool
        subagent_fence: list

    builder = StateGraph(State)
    builder.add_node("tools", ToolNode([tools["board_concurrency_set"]]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile()

    async def run(value, extra=None, args=None):
        call = {"max_concurrent": value, "reason": "Josh asked", **(args or {})}
        out = await graph.ainvoke(
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {"name": "board_concurrency_set", "args": call, "id": f"c{value}", "type": "tool_call"}
                        ],
                    )
                ],
                **(extra or {}),
            }
        )
        reply = out["messages"][-1]
        assert isinstance(reply, ToolMessage)
        return reply.content

    assert json.loads(await run(3, {"operator_chat_turn": True}))["max_concurrent"] == 3
    assert "requires operator" in await run(4, {"operator_chat_turn": False})
    assert "requires operator" in await run(5, {"operator_chat_turn": True, "subagent_fence": ["x"]})
    # A model-supplied `state` arg is overwritten by the injected graph state.
    assert "requires operator" in await run(6, {}, {"state": {"operator_chat_turn": True}})
    assert loop.max_concurrent == 3 and len(calls) == 1
