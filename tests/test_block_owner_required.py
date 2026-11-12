"""board_block_feature requires an explicit `owner` (bd-t3286).

`owner` used to default to `operator`, so an agent reserving cards to run itself
("hold: run by mythxEngineer itself") got MANUAL_BLOCK_CLASS (`terminal`): the board
paged the operator priority-now and labelled the cards NEEDS YOU for work the agent was
doing itself. Now the caller states whose hold it is — `agent` (an `agent-hold`: no
operator page, shown as 'agent hold') or `operator` (the manual class: pages as before,
shown as 'needs you') — and a missing or invalid owner blocks nothing, with an error
naming both choices. POST /features/{fid}/block keeps `operator` as its DEFAULT so a
console/script block by hand is unchanged; it newly accepts an optional `owner`.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import project_board as pb
from project_board import api, work_snapshot
from project_board import store as store_mod
from project_board.loop import BoardLoop


def _tool(name):
    return {t.name: t for t in pb._board_tools({})}[name]


# ── the tool: a missing or invalid owner errors naming both choices, blocks nothing ──


@pytest.mark.parametrize("extra", [{}, {"owner": ""}, {"owner": "op"}, {"owner": "Agent"}, {"owner": "human"}])
def test_missing_or_invalid_owner_names_both_choices_and_blocks_nothing(monkeypatch, extra):
    get = Mock()
    monkeypatch.setattr(store_mod, "get_store", get)

    out = _tool("board_block_feature").invoke({"feature_id": "bd-a", "reason": "wait", **extra})

    assert out.startswith("Error: owner must be one of:")
    assert "'agent'" in out and "'operator'" in out  # the error teaches the choice
    get.assert_not_called()  # nothing read, nothing blocked


def test_owner_has_no_operator_default_and_the_description_states_the_choice():
    t = _tool("board_block_feature")
    # The sentinel is empty (rejected in-body) — the silent operator default is what paged
    # the operator for an agent's own hold, so it must not come back via the schema.
    assert t.args["owner"].get("default", "") == ""
    assert "agent hold" in t.description and "needs you" in t.description


# ── the stated owner picks the class ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "owner,category",
    [("agent", store_mod.AGENT_HOLD_CLASS), ("operator", store_mod.MANUAL_BLOCK_CLASS)],
)
def test_owner_picks_the_block_class(monkeypatch, owner, category):
    board = Mock()
    board.flag_blocked.return_value = {"id": "bd-a", "board_state": "blocked"}
    monkeypatch.setattr(store_mod, "get_store", lambda **kw: board)

    out = _tool("board_block_feature").invoke({"feature_id": "bd-a", "reason": "wait", "owner": owner})

    assert json.loads(out)["state"] == "blocked"
    board.flag_blocked.assert_called_once_with("bd-a", "wait", category=category)


# ── paging follows the owner: an agent hold is silent, an operator hold pages as today ──


@pytest.mark.parametrize("owner,pages", [("agent", False), ("operator", True)])
async def test_paging_follows_the_stated_owner(monkeypatch, owner, pages):
    row = {
        "id": "bd-a",
        "title": "Held card",
        "board_state": "blocked",
        "blocked": True,
        "blocked_class": "",
        "blocked_reason": "waiting on the rebase",
        "labels": ["blocked", "ready"],
        "bead_status": "open",
        "budgets": {},
    }
    board = Mock()

    def flag(fid, reason, category=""):
        row["blocked_class"] = category
        return {"id": fid, "board_state": "blocked"}

    board.flag_blocked.side_effect = flag
    # side_effect, not return_value: the rows must carry the class the TOOL just set.
    board.list_features.side_effect = lambda *a, **k: [dict(row)]
    board.live_cards.side_effect = lambda *a, **k: [dict(row)]
    board.get_feature.return_value = row
    monkeypatch.setattr(store_mod, "get_store", lambda **kw: board)

    reply = _tool("board_block_feature").invoke(
        {"feature_id": "bd-a", "reason": "waiting on the rebase", "owner": owner}
    )
    assert json.loads(reply)["state"] == "blocked"

    loop = BoardLoop({"coder": "proto"})
    alerts = Mock()
    monkeypatch.setattr(loop, "_notify_operator", alerts)
    work_snapshot.reset()
    try:
        loop._take_work_snapshot(board)
        [item] = work_snapshot.provider()
        if owner == "agent":
            assert "held by agent" in item["hint"]
        else:
            assert "needs a human" in item["hint"]
        for _ in range(3):  # more sweeps than the self-heal budget: the outcome is stable
            await loop._recover_blocked(board)
        assert alerts.called == pages
        board.clear_blocked.assert_not_called()  # a hand-set hold is never self-healed
        board.requeue.assert_not_called()
    finally:
        work_snapshot.reset()


def test_board_view_shows_agent_hold_not_needs_you_for_an_agent_hold():
    js = (Path(__file__).resolve().parent.parent / "view" / "board.js").read_text(encoding="utf-8")
    branch = js[js.index('cls === "agent-hold"') :]
    assert ">agent hold</span>" in branch[:300]  # the agent-hold label…
    assert ">needs you</span>" in branch  # …and the manual class still reads "needs you"


# ── POST /features/{fid}/block: owner optional, operator default, agent accepted ─────


def _client(monkeypatch, board):
    monkeypatch.setattr(api, "get_store", lambda **_kw: board)
    app = FastAPI()
    app.include_router(api.build_data_router({}), prefix="/api/plugins/project_board")
    return TestClient(app)


@pytest.mark.parametrize(
    "body,category",
    [
        ({"reason": "r"}, store_mod.MANUAL_BLOCK_CLASS),  # no owner: a human/UI block, unchanged
        ({"reason": "r", "owner": "operator"}, store_mod.MANUAL_BLOCK_CLASS),
        ({"reason": "r", "owner": "agent"}, store_mod.AGENT_HOLD_CLASS),
    ],
)
def test_block_route_owner_is_optional_and_defaults_to_operator(monkeypatch, body, category):
    board = Mock()
    board.flag_blocked.return_value = {"id": "bd-a", "board_state": "blocked"}

    r = _client(monkeypatch, board).post("/api/plugins/project_board/features/bd-a/block", json=body)

    assert r.status_code == 200, r.text
    board.flag_blocked.assert_called_once_with("bd-a", "r", category=category)


def test_block_route_rejects_an_unknown_owner_naming_both_choices(monkeypatch):
    board = Mock()

    r = _client(monkeypatch, board).post(
        "/api/plugins/project_board/features/bd-a/block", json={"reason": "r", "owner": "them"}
    )

    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "'agent'" in detail and "'operator'" in detail
    board.flag_blocked.assert_not_called()
