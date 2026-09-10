"""Cards stranded outside the ready lane once every dependency has closed (#406).

The loop claims only `ready` cards, and the only waiting it re-checks is `ready` +
`depends_on` (the dag gate). A card left in BACKLOG to wait for its dependencies, or
BLOCKED there for the same reason, is never looked at again once they close. It is not a
claim candidate and it appears in no skip diagnostic. On a live board a dependency-complete
backlog card sat waiting on a `mark_ready` nobody knew was owed.

The fix names the verb, everywhere a card's next action is read, and moves nothing: a
backlog card is promoted by the PM (the Ready gate still decides), and a block is lifted
by whoever set it. The second half is why a hand-set block now always carries the
`terminal` class. Its class used to be guessed from its reason by the coder-failure
classifier, so a human hold that happened to say "network" or "rebase" was cleared by the
sweep and requeued to `ready`, skipping the Ready gate on the way.

Real `br` throughout: the dependency edges and their closed state are what beads itself
reports, not a hand-built row.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import project_board as pb
import project_board.loop as loop_mod
from project_board import api, work_snapshot, worktree
from project_board import store as store_mod
from project_board.loop import BoardLoop
from project_board.store import (
    NEXT_ACTION_BLOCKED_DEPS_CLEARED,
    NEXT_ACTION_DEPS_CLEARED,
    BeadsBoard,
    annotate_next_action,
    stranded_posture,
)

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH (CI installs it and sets PB_REQUIRE_BR=1)",
)

LOGGER = "protoagent.plugins.project_board"


@pytest.fixture
def board(tmp_path, monkeypatch):
    b = BeadsBoard(repo=str(tmp_path), actor="test")
    monkeypatch.setattr(store_mod, "get_store", lambda **_kw: b)
    monkeypatch.setattr(loop_mod, "get_store", lambda **_kw: b)
    work_snapshot.reset()
    yield b
    work_snapshot.reset()


def _close(board, fid):
    """Close a dependency the way a merge does, through real `br` (setup, not under test)."""
    board._run("close", fid, "-r", "merged: https://github.com/o/r/pull/1")


def _rows(board, cfg=None):
    return {f["id"]: f for f in annotate_next_action(board.list_features(), cfg or {})}


def _tool(name):
    return {t.name: t for t in pb._board_tools({})}[name]


async def _sweep(monkeypatch, board, alerts):
    """One real health sweep over the real board. Only the worktree reap is stubbed — it
    shells git, and there are no worktrees here — and the operator inbox, which is a host
    module, is captured."""
    monkeypatch.setattr(worktree, "list_feature_worktrees", lambda repo, root: [])
    loop = BoardLoop({"coder": "proto", "repo": board.repo})
    monkeypatch.setattr(
        loop, "_notify_operator", lambda fid, text, *, incident="": alerts.append((fid, text, incident))
    )
    await loop._sweep()
    return loop


# ── a backlog card whose dependencies closed reads as owed a promote ─────────────────


@requires_br
def test_a_dependency_complete_backlog_card_names_the_promote(board):
    dep = board.create_feature("Record the origin session", spec="s")["id"]
    card = board.create_feature("Wire cleanup through DELETE", spec="s", depends_on=[dep])["id"]
    assert _rows(board)[card].get("next_action", "") == ""  # its dependency is still open

    _close(board, dep)

    row = _rows(board)[card]
    assert row["board_state"] == "backlog"
    assert row["next_action"] == NEXT_ACTION_DEPS_CLEARED
    assert f"board_mark_ready({card})" in row["next_action_hint"] and dep in row["next_action_hint"]


@requires_br
def test_ready_plus_depends_on_still_releases_on_its_own_and_is_not_called_stranded(board, tmp_path):
    """The one kind of waiting the loop DOES re-check: ready + depends_on is claimable
    the moment its last dependency closes, so the board owes it no step."""
    (tmp_path / "target.py").write_text("x = 1\n")
    dep = board.create_feature("First slice", spec="s")["id"]
    card = board.create_feature(
        "Second slice", spec="s", acceptance_criteria="- WHEN x THE SYSTEM SHALL y", files_to_modify=["target.py"]
    )["id"]
    board.add_dependency(card, dep)
    board.mark_ready(card)
    assert card not in {f["id"] for f in board.ready_queue()}  # dag-held

    _close(board, dep)

    assert card in {f["id"] for f in board.ready_queue()}  # released by the dag gate alone
    assert _rows(board)[card].get("next_action", "") == ""


@requires_br
def test_a_backlog_card_parked_for_another_reason_is_not_called_stranded(board):
    dep = board.create_feature("Dependency", spec="s")["id"]
    designing = board.create_feature("Needs a design first", spec="s", depends_on=[dep])["id"]
    board.mark_designing(designing, "ADR pending")
    deferred = board.create_feature("Not this quarter", spec="s", depends_on=[dep])["id"]
    board._run("update", deferred, "--status", "deferred")
    loose = board.create_feature("No dependencies at all", spec="s")["id"]

    _close(board, dep)

    rows = _rows(board)
    for fid in (designing, deferred, loose):
        assert rows[fid].get("next_action", "") == "", fid


# ── a card blocked in backlog to wait: surfaced, NEVER cleared ───────────────────────


@requires_br
async def test_a_card_blocked_to_wait_for_dependencies_is_surfaced_and_never_cleared(board, monkeypatch, caplog):
    dep = board.create_feature("Record the origin session", spec="s")["id"]
    card = board.create_feature("Wire cleanup through DELETE", spec="s", depends_on=[dep])["id"]
    reply = _tool("board_block_feature").invoke(
        {"feature_id": card, "reason": "shared-file gate: unblock and mark ready after the dependency merges"}
    )
    assert json.loads(reply)["state"] == "blocked"
    alerts: list = []

    await _sweep(monkeypatch, board, alerts)
    [(_, _, before)] = alerts
    assert "deps-closed" not in before  # the dependency is still open: an ordinary block

    _close(board, dep)
    alerts.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await _sweep(monkeypatch, board, alerts)

    f = board.get_feature(card)
    assert f["blocked"] and f["blocked_class"] == "terminal"  # untouched — nothing lifts a block
    assert f["blocked_reason"].startswith("shared-file gate")
    row = _rows(board)[card]
    assert row["next_action"] == NEXT_ACTION_BLOCKED_DEPS_CLEARED
    assert f"board_unblock_feature({card})" in row["next_action_hint"]
    # ONE new alert, with a new incident key, now that the wait may be over…
    [(fid, text, after)] = alerts
    assert fid == card and "every card it depends on has closed" in text and after != before
    # …and the loop's own record of the moment it saw the card stranded.
    assert [r for r in caplog.records if f"{card} stranded (blocked)" in r.message]


@requires_br
async def test_a_real_external_block_is_never_auto_cleared(board, monkeypatch):
    """A human's hold whose reason happens to contain a word the coder-failure
    classifier knows. Before the fix it was classed `transient`, and the sweep cleared it
    and requeued the card to `ready`, skipping the Ready gate on a card that never
    passed it."""
    dep = board.create_feature("Dependency", spec="s")["id"]
    card = board.create_feature("Needs the VPN route", spec="s", depends_on=[dep])["id"]
    _tool("board_block_feature").invoke(
        {"feature_id": card, "reason": "waiting on the network team to open the VPN route"}
    )
    _close(board, dep)

    for _ in range(3):  # more sweeps than the self-heal's retry budget
        await _sweep(monkeypatch, board, [])

    f = board.get_feature(card)
    assert f["board_state"] == "blocked" and f["blocked_class"] == "terminal"
    assert f["blocked_reason"] == "waiting on the network team to open the VPN route"
    assert "ready" not in f["labels"]  # never promoted past the Ready gate


@requires_br
def test_the_block_route_sets_a_hold_too(board, monkeypatch):
    """POST /features/{fid}/block is the console's and any script's way to block by hand
    — it must state the class the same way the tool does."""
    card = board.create_feature("Held by an operator", spec="s")["id"]
    monkeypatch.setattr(api, "get_store", lambda **_kw: board)
    app = FastAPI()
    app.include_router(api.build_data_router({}), prefix="/api/plugins/project_board")

    r = TestClient(app).post(
        f"/api/plugins/project_board/features/{card}/block",
        json={"reason": "blocked until the rebase onto #3360 lands"},  # `rebase` read as merge-conflict
    )

    assert r.status_code == 200, r.text
    assert board.get_feature(card)["blocked_class"] == "terminal"


# ── the agent's working state: the snapshot now carries the board's own hints ────────


@requires_br
async def test_the_working_state_names_a_stranded_backlog_card_with_its_verb(board, monkeypatch):
    dep = board.create_feature("Dependency", spec="s")["id"]
    card = board.create_feature("Stranded slice", spec="s", depends_on=[dep])["id"]
    plain = board.create_feature("Ordinary backlog", spec="s")["id"]
    _close(board, dep)

    await _sweep(monkeypatch, board, [])

    items = {i["id"]: i for i in work_snapshot.provider()}
    assert items[card]["state"] == "backlog"
    assert f"board_mark_ready({card})" in items[card]["hint"]
    assert plain not in items  # a backlog card with nothing owed is still not "open work"


@requires_br
async def test_the_working_state_hint_is_the_boards_own_next_action_hint(board, monkeypatch):
    """The snapshot was built to reuse `next_action_hint`, but the sweep published a BARE
    listing, and only `annotate_next_action` writes that field, so every hint in the
    agent's working state was empty. An in_review card with auto_merge off shows it."""
    card = board.create_feature("Reviewed and green", spec="s")["id"]
    board._run("update", card, "--status", "in_progress", "--add-label", "in-review")
    board._run("update", card, "--external-ref", "https://github.com/o/r/pull/42")

    await _sweep(monkeypatch, board, [])

    [item] = [i for i in work_snapshot.provider() if i["id"] == card]
    assert item["state"] == "in_review" and "merge #42" in item["hint"]


@requires_br
async def test_a_stranded_card_is_logged_once_not_every_sweep(board, monkeypatch, caplog):
    dep = board.create_feature("Dependency", spec="s")["id"]
    card = board.create_feature("Stranded slice", spec="s", depends_on=[dep])["id"]
    _close(board, dep)
    monkeypatch.setattr(worktree, "list_feature_worktrees", lambda repo, root: [])
    loop = BoardLoop({"coder": "proto", "repo": board.repo})

    with caplog.at_level(logging.INFO, logger=LOGGER):
        for _ in range(3):
            await loop._sweep()

    assert len([r for r in caplog.records if f"{card} stranded" in r.message]) == 1


# ── the pure posture, edge by edge ───────────────────────────────────────────────────


def _row(**kw):
    base = {
        "id": "bd-2",
        "board_state": "backlog",
        "bead_status": "open",
        "labels": [],
        "depends_on": ["bd-1"],
        "open_depends_on": [],
    }
    return {**base, **kw}


@pytest.mark.parametrize(
    "row,expected",
    [
        (_row(), NEXT_ACTION_DEPS_CLEARED),
        (_row(issue_type="task"), NEXT_ACTION_DEPS_CLEARED),  # a task waits the same way
        (_row(board_state="blocked", labels=["blocked"]), NEXT_ACTION_BLOCKED_DEPS_CLEARED),
        (_row(open_depends_on=["bd-1"]), ""),  # still waiting
        (_row(depends_on=[]), ""),  # nothing recorded, nothing cleared
        (_row(bead_status="deferred"), ""),
        (_row(labels=["designing"]), ""),
        # blocked in the READY lane: a loop hold (preflight / livelock) or a dag-released card
        (_row(board_state="blocked", labels=["blocked", "ready"]), ""),
        # blocked mid-build: its block is about the build, not the wait
        (_row(board_state="blocked", bead_status="in_progress", labels=["blocked"]), ""),
        (_row(board_state="ready", labels=["ready"]), ""),  # the dag gate's case
    ],
)
def test_stranded_posture(row, expected):
    assert stranded_posture(row)["next_action"] == expected


def test_the_console_has_a_chip_for_each_stranded_posture():
    page = (Path(__file__).resolve().parent.parent / "view" / "board.js").read_text()
    assert f'"{NEXT_ACTION_DEPS_CLEARED}": ["pl-badge--warning",' in page
    assert f'"{NEXT_ACTION_BLOCKED_DEPS_CLEARED}": ["pl-badge--warning",' in page


def test_the_snapshot_ranks_a_stranded_backlog_card_right_after_blocked():
    work_snapshot.reset()
    work_snapshot.publish(
        [
            {"id": "bd-r", "board_state": "ready", "title": "t"},
            {"id": "bd-b", "board_state": "backlog", "title": "t", "next_action": NEXT_ACTION_DEPS_CLEARED},
            {"id": "bd-x", "board_state": "blocked", "title": "t"},
            {"id": "bd-q", "board_state": "backlog", "title": "t"},
        ]
    )
    assert [i["id"] for i in work_snapshot.provider()] == ["bd-x", "bd-b", "bd-r"]
    work_snapshot.reset()
