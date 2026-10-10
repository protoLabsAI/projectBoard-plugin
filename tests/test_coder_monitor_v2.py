"""Coder monitor v2 — the gen card renders as natural-height sections in ONE scroll.

The render functions are plain vanilla JS inside the assembled board page, so each test
extracts them from ``BOARD_PAGE`` and runs ``genCard`` in node against a snapshot shaped
like ``coder_seam._GenBuffer.snapshot()``.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from project_board.board_view import BOARD_PAGE

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")


def _extract_fn(name: str) -> str:
    start = BOARD_PAGE.index(f"function {name}(")
    depth, i = 0, BOARD_PAGE.index("{", start)
    while True:
        c = BOARD_PAGE[i]
        depth += c == "{"
        depth -= c == "}"
        i += 1
        if depth == 0:
            return BOARD_PAGE[start:i]


def _line(prefix: str) -> str:
    return BOARD_PAGE[BOARD_PAGE.index(prefix) :].split("\n", 1)[0]


def _render(gens: list[dict], ui: dict | None = None) -> list[str]:
    js = "\n".join(
        [
            _line("const esc = "),
            _line("const TOOL_STATUS_WORD"),
            "const MON_UI = " + json.dumps(ui or {}) + ";",
            _line("const ACTIVITY_ROWS"),
            *(
                _extract_fn(n)
                for n in ("fmtDur", "fmtTok", "uiOpen", "section", "toolLine", "groupTools", "collapseTools", "genCard")
            ),
            f"console.log(JSON.stringify({json.dumps(gens)}.map(genCard)));",
        ]
    )
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)


def _tool(i: int, name: str, status: str = "completed") -> list[dict]:
    row = {"id": f"t{i}", "name": name, "kind": "execute", "locations": []}
    return [dict(row, status="start")] + ([dict(row, status=status)] if status != "start" else [])


def _gen(**kw) -> dict:
    g = {
        "gen": 1,
        "tier": "reasoning",
        "done": False,
        "elapsed_s": 492.3,
        "current_tool": {
            "id": "c",
            "name": "make gate",
            "status": "running",
            "locations": [],
            "detail": "Run the gate",
        },
        "recent_tools": [],
        "thought_tail": "",
        "answer_tail": "Working on it.",
        "plan": None,
        "usage": {"used": 99361, "size": 1000000},
        "verify": None,
        "stop_reason": None,
    }
    g.update(kw)
    return g


def test_header_formats_elapsed_and_tokens_for_humans():
    (html,) = _render([_gen()])
    assert ">8m 12s<" in html
    assert "99.4k / 1M tokens · 10%" in html
    assert "running" in html


def test_sections_render_in_reading_order():
    g = _gen(
        plan=[{"content": "a", "status": "in_progress"}],
        thought_tail="hmm",
        recent_tools=_tool(1, "ls"),
        verify={"passed": True, "test_cmd": "make test"},
    )
    (html,) = _render([g])
    order = [html.index(f"sec-{k}") for k in ("verify", "now", "plan", "saying", "thinking", "tools")]
    assert order == sorted(order)


def test_thinking_starts_collapsed_and_saying_starts_open():
    (html,) = _render([_gen(thought_tail="internal")])
    thinking = html.split("sec-thinking", 1)[1].split("</section>", 1)[0]
    saying = html.split("sec-saying", 1)[1].split("</section>", 1)[0]
    assert "internal" not in thinking and 'data-open="false"' in thinking
    assert "Working on it." in saying and 'class="tail"' in saying


def test_a_long_plan_folds_finished_steps_behind_one_row():
    plan = [{"content": f"done {i}", "status": "completed"} for i in range(5)] + [
        {"content": "now", "status": "in_progress"},
        {"content": "later", "status": "pending"},
    ]
    (html,) = _render([_gen(plan=plan)])
    assert "✓ 5 done — show" in html and "done 0" not in html
    assert "5 / 7" in html
    (opened,) = _render([_gen(plan=plan)], ui={"1:plan-done": True})
    assert "done 0" in opened


def test_repeated_calls_group_and_activity_caps_with_show_all():
    tools = []
    for i in range(4):
        tools += _tool(i, "Edit a.py")
    for i in range(10, 22):
        tools += _tool(i, f"cmd {i}")
    (html,) = _render([_gen(recent_tools=tools)])
    assert html.count("<li ") - html.split("sec-tools", 1)[0].count("<li ") == 8  # ACTIVITY_ROWS
    assert "Show all 13" in html  # 12 distinct commands + one grouped Edit row
    assert "16 calls" in html
    (everything,) = _render([_gen(recent_tools=tools)], ui={"1:tools-all": True})
    assert '<span class="rep">×4</span>' in everything


def test_a_finished_gen_folds_to_its_summary_and_live_state_survives_rerender():
    done = _gen(gen=2, done=True, stop_reason="end_turn", verify={"passed": True, "test_cmd": "t"})
    (html,) = _render([done])
    assert "folded" in html and "finished" in html
    assert "sec-verify" in html and "sec-saying" not in html
    (opened,) = _render([done], ui={"2:body": True})
    assert "sec-saying" in opened and "folded" not in opened


def test_a_non_end_turn_stop_reason_reads_as_stopped():
    (html,) = _render([_gen(done=True, stop_reason="max_tokens")])
    assert "stopped · max_tokens" in html


def test_current_tool_row_is_clamped_until_expanded():
    (html,) = _render([_gen()])
    assert 'class="cur"' in html and 'data-open="false"' in html
    (wide,) = _render([_gen()], ui={"1:cur-full": True})
    assert 'class="cur full"' in wide


def test_tool_rows_keep_the_status_word_for_screen_readers():
    (html,) = _render([_gen(recent_tools=_tool(1, "Edit tests/test_cli.py") + _tool(2, "make gate", "start"))])
    assert '<span class="sr">done</span>' in html
    assert '<span class="sr">running</span>' in html
    assert "[edit]" not in html
