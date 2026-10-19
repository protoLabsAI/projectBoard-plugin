"""Explicit agent holds persist until release without paging the operator (#555)."""

import json
import shutil
from unittest.mock import Mock

import pytest

import project_board as pb
from project_board import store as store_mod, work_snapshot
from project_board.loop import BoardLoop

requires_br = pytest.mark.skipif(shutil.which(store_mod.BR) is None, reason="real br CLI not on PATH")


def _tool(name):
    return {t.name: t for t in pb._board_tools({})}[name]


@pytest.mark.parametrize("owner,category", [(None, "terminal"), ("operator", "terminal"), ("agent", "agent-hold")])
def test_owner_dispatch_is_explicit(monkeypatch, owner, category):
    board = Mock()
    board.flag_blocked.return_value = {"id": "bd-a", "board_state": "blocked"}
    monkeypatch.setattr(store_mod, "get_store", lambda **kw: board)
    args = {"feature_id": "bd-a", "reason": '"waiting on the network team"'}
    if owner is not None:
        args["owner"] = owner
    assert json.loads(_tool("board_block_feature").invoke(args))["state"] == "blocked"
    board.flag_blocked.assert_called_once_with("bd-a", "waiting on the network team", category=category)


@pytest.mark.parametrize("owner", ["", "human", "terminal", "AGENT", " agent "])
def test_invalid_owner_does_not_access_store(monkeypatch, owner):
    get = Mock()
    monkeypatch.setattr(store_mod, "get_store", get)
    result = _tool("board_block_feature").invoke({"feature_id": "bd-a", "reason": "wait", "owner": owner})
    assert result.startswith("Error: owner must be one of:")
    get.assert_not_called()


@pytest.mark.parametrize("category", ["agent-hold", "terminal", "auth", "preflight-hold"])
async def test_snapshot_and_operator_alerts(monkeypatch, category):
    row = {
        "id": "bd-a",
        "title": "Held card",
        "board_state": "blocked",
        "blocked": True,
        "blocked_class": category,
        "blocked_reason": "waiting on the network team",
        "labels": ["blocked", "ready"],
        "bead_status": "open",
        "budgets": {},
    }
    board = Mock()
    board.list_features.return_value = [row]
    board.live_cards.return_value = [row.copy()]
    board.get_feature.return_value = row
    loop = BoardLoop({"coder": "proto"})
    alerts = Mock()
    monkeypatch.setattr(loop, "_notify_operator", alerts)
    work_snapshot.reset()
    try:
        loop._take_work_snapshot(board)
        [item] = work_snapshot.provider()
        assert "waiting on the network team" in item["hint"]
        if category == "agent-hold":
            assert "held by agent" in item["hint"] and "human" not in item["hint"]
        elif category != "preflight-hold":
            assert "needs a human" in item["hint"]
        for _ in range(3):
            await loop._recover_blocked(board)
        assert alerts.called == (category != "agent-hold")
        board.clear_blocked.assert_not_called()
        board.requeue.assert_not_called()
    finally:
        work_snapshot.reset()


@requires_br
async def test_real_hold_lists_and_stays_unclaimable_until_explicit_release(tmp_path, monkeypatch):
    board = store_mod.BeadsBoard(repo=str(tmp_path), actor="test")
    monkeypatch.setattr(store_mod, "get_store", lambda **kw: board)
    (tmp_path / "target.py").write_text("x = 1\n")
    fid = board.create_feature(
        "Agent-owned wait",
        spec="s",
        acceptance_criteria="- WHEN x THE SYSTEM SHALL y",
        files_to_modify=["target.py"],
    )["id"]
    board.mark_ready(fid)
    reason = "waiting for network rebase; agent will release"
    _tool("board_block_feature").invoke({"feature_id": fid, "reason": reason, "owner": "agent"})
    loop = BoardLoop({"coder": "proto"})
    for _ in range(3):
        await loop._recover_blocked(board)
    row = next(r for r in json.loads(_tool("board_list").invoke({})) if r["id"] == fid)
    assert row["blocked_class"] == "agent-hold"
    assert row["blocked_reason"] == reason
    assert "held by agent" in row["next_action_hint"]
    assert board.claim(fid) is None
    assert board.get_feature(fid)["blocked"]
    _tool("board_unblock_feature").invoke({"feature_id": fid})
    assert board.get_feature(fid)["blocked_class"] == ""
    assert board.claim(fid)["board_state"] == "in_progress"


def test_agent_hint_preserves_dependency_release_guidance():
    row = {
        "id": "bd-a",
        "board_state": "blocked",
        "blocked": True,
        "blocked_class": "agent-hold",
        "blocked_reason": "wait",
        "bead_status": "open",
        "labels": ["blocked"],
        "depends_on": ["bd-b"],
        "open_depends_on": [],
    }
    store_mod.annotate_next_action([row], {})
    assert "held by agent" in row["next_action_hint"]
    assert "board_mark_ready" in row["next_action_hint"]
    assert row["next_action"] == store_mod.NEXT_ACTION_BLOCKED_DEPS_CLEARED


def test_empty_agent_hold_reason_does_not_write(monkeypatch, tmp_path):
    board = store_mod.BeadsBoard(repo=str(tmp_path), actor="test")
    monkeypatch.setattr(board, "_require", lambda fid: {"id": fid, "labels": []})
    write = Mock()
    monkeypatch.setattr(board, "_run", write)
    with pytest.raises(store_mod.BoardError, match="agent hold needs a reason"):
        board.flag_blocked("bd-a", "  ", category=store_mod.AGENT_HOLD_CLASS)
    write.assert_not_called()
