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
