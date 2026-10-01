"""The coder-monitor drawer reads cleanly on camera.

* "saying" no longer glues sentences across work ("…the feature.Let me check…"): a tool
  call or plan update between two narration runs is a paragraph break, and the adapter's
  whole-block replay is dropped (mirrors protoAgent #3408 / #3979).
* Paths are shown relative to the gen's worktree (or the repo it came from), basename
  otherwise, and the home directory is never printed.
* The current tool shows what it does in plain words, not the raw JSON args.
* The coder's plan reaches the drawer LIVE when the host's seam can forward it.
* The tool feed lists each call once, with no raw ``[edit]`` kinds; the header shows
  only the br version.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field

import pytest

from project_board import coder_seam
from project_board.board_view import BOARD_PAGE

HOME = os.path.expanduser("~")
REPO = os.path.join(HOME, "demo", "tiny-todo")
WT = os.path.join(REPO, ".worktrees", "feat-bd-8l4-add-a-clear-command")


@pytest.fixture(autouse=True)
def _clean_buffer():
    coder_seam._progress.clear()
    yield
    coder_seam._progress.clear()


def _gen() -> dict:
    return coder_seam.progress_snapshot("bd-x")["gens"][0]


def _tool(tid: str, name: str, inp: dict | None = None, phase: str = "start", **extra):
    ev = {"phase": phase, "id": tid, "name": name, **extra}
    if inp is not None:
        ev["input"] = json.dumps(inp)
    coder_seam.progress_tool("bd-x", 1, ev)


# ── "saying": separators + replay ─────────────────────────────────────────────────


def test_narration_after_a_tool_call_starts_a_new_paragraph():
    coder_seam.progress_begin("bd-x", 1, root=WT)
    coder_seam.progress_answer("bd-x", 1, "I'll explore the code, then implement ")
    coder_seam.progress_answer("bd-x", 1, "the feature.")
    _tool("t1", "Read tinytodo/cli.py", {"file_path": f"{WT}/tinytodo/cli.py"})
    _tool("t1", "Read tinytodo/cli.py", phase="end")
    coder_seam.progress_answer("bd-x", 1, "Let me check the test helpers.")
    _tool("t2", "Read tests/helpers.py", {"file_path": f"{WT}/tests/helpers.py"})
    _tool("t2", "Read tests/helpers.py", phase="end")
    coder_seam.progress_answer("bd-x", 1, "Adding `Store.clear_done()`:")
    _tool("t3", "Edit tinytodo/store.py", {"file_path": f"{WT}/tinytodo/store.py"})
    coder_seam.progress_answer("bd-x", 1, "Now the CLI.")
    assert _gen()["answer_tail"] == (
        "I'll explore the code, then implement the feature.\n\n"
        "Let me check the test helpers.\n\n"
        "Adding `Store.clear_done()`:\n\n"
        "Now the CLI."
    )


def test_a_plan_update_is_a_paragraph_boundary_too():
    coder_seam.progress_begin("bd-x", 1, root=WT)
    coder_seam.progress_answer("bd-x", 1, "Here is the 3-item plan.")
    coder_seam.progress_plan("bd-x", 1, [{"content": "a", "status": "pending"}])
    coder_seam.progress_answer("bd-x", 1, "Plan created.")
    assert _gen()["answer_tail"] == "Here is the 3-item plan.\n\nPlan created."


def test_streamed_deltas_without_work_between_are_not_split():
    coder_seam.progress_begin("bd-x", 1, root=WT)
    for d in ("All 11 ", "tests ", "pass."):
        coder_seam.progress_answer("bd-x", 1, d)
    assert _gen()["answer_tail"] == "All 11 tests pass."


def test_the_adapters_whole_block_replay_is_dropped():
    """claude-agent-acp streams a block as deltas, then re-sends the WHOLE block as one
    more chunk — "I'll look at calc.py first.I'll look at calc.py first." (#3979)."""
    coder_seam.progress_begin("bd-x", 1, root=WT)
    coder_seam.progress_answer("bd-x", 1, "I'll look at ")
    coder_seam.progress_answer("bd-x", 1, "calc.py first.")
    coder_seam.progress_answer("bd-x", 1, "I'll look at calc.py first.")
    assert _gen()["answer_tail"] == "I'll look at calc.py first."
    # …but the same sentence AFTER a tool call is real narration, kept as its own paragraph.
    _tool("t1", "Read calc.py", {"file_path": "calc.py"})
    coder_seam.progress_answer("bd-x", 1, "I'll look at calc.py first.")
    assert _gen()["answer_tail"] == "I'll look at calc.py first.\n\nI'll look at calc.py first."


def test_a_short_single_chunk_echo_is_not_a_replay():
    coder_seam.progress_begin("bd-x", 1, root=WT)
    coder_seam.progress_answer("bd-x", 1, "ok")
    coder_seam.progress_answer("bd-x", 1, "ok")
    assert _gen()["answer_tail"] == "okok"


# ── paths ──────────────────────────────────────────────────────────────────────────


def test_locations_are_relative_to_the_worktree_and_never_show_home():
    coder_seam.progress_begin("bd-x", 1, root=WT)
    _tool("t1", "Edit tests/test_cli.py", {"file_path": f"{WT}/tests/test_cli.py", "old_string": "a"})
    _tool("t1", "Edit tests/test_cli.py", phase="end")
    _tool("t2", "Read", {"file_path": f"{REPO}/tinytodo/cli.py"})  # the main checkout
    _tool("t3", "Read", {"file_path": "/etc/hosts"})  # outside both: basename
    g = _gen()
    locs = [r["locations"] for r in g["recent_tools"]]
    assert locs == [["tests/test_cli.py"], ["tests/test_cli.py"], ["tinytodo/cli.py"], ["hosts"]]
    assert HOME not in json.dumps(g)


def test_structured_acp_locations_are_preferred_and_relativized():
    coder_seam.progress_begin("bd-x", 1, root=WT)
    _tool("t1", "Edit", {"x": 1}, locations=[{"path": f"{WT}/a/b.py", "line": 3}])
    assert _gen()["current_tool"]["locations"] == ["a/b.py"]


def test_free_text_is_scrubbed_of_the_worktree_and_home():
    coder_seam.progress_begin("bd-x", 1, root=WT)
    _tool("t1", f"cd {WT} && make gate", {"command": f"cd {WT} && make gate"})
    coder_seam.progress_answer("bd-x", 1, f"Edited {WT}/tinytodo/cli.py and {HOME}/notes.txt.")
    g = _gen()
    # the coder already runs in the worktree: a leading `cd <worktree> &&` is noise
    assert g["current_tool"]["name"] == "make gate"
    assert g["answer_tail"] == "Edited tinytodo/cli.py and ~/notes.txt."
    assert HOME + "/" not in json.dumps(g)


def test_a_resolved_tmp_root_matches_both_spellings(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    coder_seam.progress_begin("bd-x", 1, root=str(wt))
    _tool("t1", "Read", {"path": os.path.realpath(str(wt)) + "/src/x.py"})
    assert _gen()["current_tool"]["locations"] == ["src/x.py"]


def test_dispatch_records_the_worktree_as_the_gens_root():
    """The live path wires the root: dispatch_coder_tapped hands progress_begin its
    worktree, so a real run's drawer is relative without any caller remembering to."""
    import asyncio

    async def seam(delegate, prompt, *, on_tool=None, on_thought=None, on_text=None, timeout=None):
        await on_tool({"phase": "start", "id": "t", "name": "Read", "input": json.dumps({"path": f"{WT}/x.py"})})
        return "done"

    asyncio.run(coder_seam.dispatch_coder_tapped(_Coder(), WT, "p", fid="bd-x", gen=1, _dispatch_tapped=seam))
    assert _gen()["current_tool"]["locations"] == ["x.py"]


# ── current tool detail ────────────────────────────────────────────────────────────


def test_current_tool_carries_a_plain_words_detail():
    coder_seam.progress_begin("bd-x", 1, root=WT)
    _tool("t1", "make gate 2>&1", {"command": "make gate 2>&1", "description": "Run the pre-merge gate"})
    assert _gen()["current_tool"]["detail"] == "Run the pre-merge gate"
    _tool("t2", "Grep", {"pattern": "def clear"})
    assert _gen()["current_tool"]["detail"] == "def clear"
    # a long edit's JSON is not a detail (its file is the location)
    _tool("t3", "Edit x.py", {"file_path": "x.py", "old_string": "a" * 500, "new_string": "b"})
    assert _gen()["current_tool"]["detail"] == ""


# ── live plan ──────────────────────────────────────────────────────────────────────


@dataclass
class _Coder:
    workdir: str = ""
    manage_git: bool = True
    env: dict = field(default_factory=dict)


async def test_plan_streams_live_when_the_seam_takes_on_plan():
    seen: dict = {}

    async def seam(delegate, prompt, *, on_tool=None, on_thought=None, on_text=None, on_plan=None, timeout=None):
        await on_plan([{"content": "write tests", "status": "in_progress"}])
        # MID-turn: the drawer already has the plan.
        seen["mid"] = coder_seam.progress_snapshot("bd-x")["gens"][0]["plan"]
        return "done"

    await coder_seam.dispatch_coder_tapped(_Coder(), WT, "p", fid="bd-x", gen=1, _dispatch_tapped=seam)
    assert seen["mid"] == [{"content": "write tests", "status": "in_progress", "priority": ""}]


async def test_on_plan_is_never_passed_to_a_seam_that_does_not_name_it():
    async def seam(delegate, prompt, *, on_tool=None, on_thought=None, on_text=None, timeout=None):
        return "done"  # an unknown kwarg would raise TypeError here

    assert await coder_seam.dispatch_coder_tapped(_Coder(), WT, "p", fid="bd-x", gen=1, _dispatch_tapped=seam) == "done"


def test_the_coder_prompt_asks_for_a_task_list_checklist():
    from project_board.loop import prompt as prompt_mod

    src = open(prompt_mod.__file__, encoding="utf-8").read()
    assert "TaskCreate/TaskUpdate" in src and "TodoWrite" in src


# ── the view ───────────────────────────────────────────────────────────────────────


def test_header_shows_only_the_br_version_with_the_path_in_a_tooltip():
    assert 'fetched to " + s.setup.br.path' not in BOARD_PAGE
    assert '$("sub").title = "br fetched to "' in BOARD_PAGE


def test_tool_rows_are_one_line_and_never_print_raw_kinds():
    assert "'+esc(t.kind)+']" not in BOARD_PAGE
    assert "white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" in BOARD_PAGE


def _extract(name: str) -> str:
    start = BOARD_PAGE.index(f"function {name}(")
    depth, i = 0, BOARD_PAGE.index("{", start)
    while True:
        c = BOARD_PAGE[i]
        depth += c == "{"
        depth -= c == "}"
        i += 1
        if depth == 0:
            return BOARD_PAGE[start:i]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_tool_feed_collapses_each_call_to_one_row_in_node():
    status_map = BOARD_PAGE[BOARD_PAGE.index("const TOOL_STATUS_WORD") :].split("\n", 1)[0]
    esc = BOARD_PAGE[BOARD_PAGE.index("const esc = ") :].split("\n", 1)[0]
    rows = [
        {
            "id": "a",
            "name": "Edit tests/test_cli.py",
            "kind": "edit",
            "status": "start",
            "locations": ["tests/test_cli.py"],
        },
        {
            "id": "a",
            "name": "Edit tests/test_cli.py",
            "kind": "edit",
            "status": "completed",
            "locations": ["tests/test_cli.py"],
        },
        {"id": "b", "name": "make gate", "kind": "execute", "status": "start", "locations": []},
    ]
    js = "\n".join(
        [
            esc,
            status_map,
            _extract("toolLine"),
            _extract("collapseTools"),
            f"const out = collapseTools({json.dumps(rows)}).map(toolLine);",
            "console.log(JSON.stringify(out));",
        ]
    )
    out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    assert len(out) == 2
    assert ">done</span>" in out[0] and "Edit tests/test_cli.py" in out[0]
    assert 'class="loc"' not in out[0]  # the title already names the file
    assert "[edit]" not in out[0]
    assert ">running</span>" in out[1]


def test_a_clipped_cd_title_never_leaks_the_worktree_prefix():
    """claude-agent-acp titles a shell call with its command, clipped by the host — so a
    `cd "<worktree>" && make gate` title arrives as `cd "/Users/me/…/tiny-to`. Seen live."""
    coder_seam.progress_begin("bd-x", 1, root=WT)
    cmd = f'cd "{WT}" && make gate 2>&1'
    _tool("t1", cmd[:40], {"command": cmd, "description": ""})
    g = _gen()
    assert g["current_tool"]["name"] == "make gate 2>&1"
    assert g["current_tool"]["detail"] == "make gate 2>&1"
    _tool("t2", cmd, {"command": cmd})
    assert _gen()["current_tool"]["name"] == "make gate 2>&1"
    assert HOME not in json.dumps(_gen())


def test_a_sentence_glued_to_the_next_without_a_work_signal_still_breaks():
    """Seen live: "…set up my checklist and implement.Now let me implement." — the task
    tool calls between them became plan updates the host forwards only at turn end."""
    coder_seam.progress_begin("bd-x", 1, root=WT)
    coder_seam.progress_answer("bd-x", 1, "Let me set up my checklist and implement.")
    coder_seam.progress_answer("bd-x", 1, "Now let me implement.")
    coder_seam.progress_answer("bd-x", 1, " Starting with the store")
    coder_seam.progress_answer("bd-x", 1, ": version 0.")
    coder_seam.progress_answer("bd-x", 1, "3.2")
    assert _gen()["answer_tail"] == (
        "Let me set up my checklist and implement.\n\nNow let me implement. Starting with the store: version 0.3.2"
    )


def test_parallel_placeholder_reads_are_named_on_their_own_rows():
    """claude-agent-acp opens parallel reads as placeholder "Read File" calls and names
    each after the next has started; the host clips the title. Seen live as "Read …"."""
    coder_seam.progress_begin("bd-x", 1, root=WT)
    _tool("a", "Read File")
    _tool("b", "Read File")
    p = f"{WT}/tests/helpers.py"
    coder_seam.progress_tool(
        "bd-x", 1, {"phase": "update", "id": "a", "name": ("Read " + p)[:60], "input": json.dumps({"file_path": p})}
    )
    coder_seam.progress_tool("bd-x", 1, {"phase": "end", "id": "a", "name": ("Read " + p)[:60]})
    rows = _gen()["recent_tools"]
    assert [(r["id"], r["name"], r["locations"], r["status"]) for r in rows] == [
        ("a", "Read tests/helpers.py", ["tests/helpers.py"], "start"),
        ("b", "Read File", [], "start"),
        ("a", "Read tests/helpers.py", ["tests/helpers.py"], "completed"),
    ]
