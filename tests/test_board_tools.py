"""Board lifecycle tools (#112): cancel / requeue / block / unblock.

The PM agent could create and promote work but had no verb to redirect, block, unblock,
or retire it — even though the store methods (``cancel_feature`` / ``requeue`` /
``flag_blocked`` / ``clear_blocked``) and their HTTP routes already existed. These tests
pin the four new tool wrappers: each de-quotes its free-text ``reason``, calls through to
its store method, and returns the ``{id, state}`` JSON echo (or an ``Error: …`` string on
a bad id).

The store path is exercised with the shared ``make_board`` fixture (a fake ``br``): a
recording ``_run`` captures the exact args each store method emits, and ``get_feature`` is
patched to supply the projection the method reads back — the same tool-level wiring
``test_board_updates.py`` uses.
"""

from __future__ import annotations

import json

import pytest

import project_board as pb


class _RecordingBr:
    """A fake ``_run`` that records every call and returns an inert value, so the exact
    ``br`` args a store method emits can be asserted. The feature projection is supplied
    separately via a patched ``get_feature`` (these lifecycle methods don't read state out
    of ``_run``; they act, then re-read)."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, want_json=False):
        self.calls.append(args)
        return [] if want_json else ""

    def cmds(self, name):
        return [a for a in self.calls if a and a[0] == name]


def _get_tool(name, cfg=None):
    tools = {t.name: t for t in pb._board_tools(cfg or {})}
    return tools[name]


def _wire(make_board, monkeypatch, state):
    """Build a board whose ``_run`` records calls and whose ``get_feature`` returns
    ``state``, and point ``get_store`` at it so the tool wrappers call through."""
    br = _RecordingBr()
    board = make_board(br)
    monkeypatch.setattr(board, "get_feature", lambda fid: state)
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)
    return board, br


# ── registration: all four verbs are exposed as agent tools ─────────────────────────


def test_lifecycle_tools_are_registered():
    names = {t.name for t in pb._board_tools({})}
    assert {
        "board_cancel_feature",
        "board_requeue_feature",
        "board_block_feature",
        "board_unblock_feature",
    } <= names


# ── board_cancel_feature → store.cancel_feature ─────────────────────────────────────


def test_board_cancel_feature_tags_cancelled_and_closes_with_reason(make_board, monkeypatch):
    _, br = _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "cancelled"})

    out = json.loads(_get_tool("board_cancel_feature").invoke({"feature_id": "bd-1", "reason": "duplicate"}))

    assert out == {"id": "bd-1", "state": "cancelled"}
    (update,) = br.cmds("update")
    assert update == ("update", "bd-1", "--add-label", "cancelled", "--assignee", "")
    (close,) = br.cmds("close")
    assert close == ("close", "bd-1", "-r", "cancelled: duplicate")


def test_board_cancel_feature_without_reason_closes_bare(make_board, monkeypatch):
    _, br = _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "cancelled"})

    _get_tool("board_cancel_feature").invoke({"feature_id": "bd-1"})

    (close,) = br.cmds("close")
    assert close == ("close", "bd-1", "-r", "cancelled")


def test_board_cancel_feature_strips_wrapping_quotes_from_reason(make_board, monkeypatch):
    _, br = _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "cancelled"})

    _get_tool("board_cancel_feature").invoke({"feature_id": "bd-1", "reason": '"bad decomposition"'})

    # the wrapping quotes are peeled before the reason reaches the audit trail.
    (close,) = br.cmds("close")
    assert close == ("close", "bd-1", "-r", "cancelled: bad decomposition")


# ── board_requeue_feature → store.requeue ───────────────────────────────────────────


def test_board_requeue_feature_returns_to_ready_and_drops_in_review(make_board, monkeypatch):
    _, br = _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "ready"})

    out = json.loads(_get_tool("board_requeue_feature").invoke({"feature_id": "bd-1"}))

    assert out == {"id": "bd-1", "state": "ready"}
    (update,) = br.cmds("update")
    # back to ready + in-review dropped + assignee cleared, so the puller can re-claim it.
    assert update == (
        "update",
        "bd-1",
        "--status",
        "open",
        "--assignee",
        "",
        "--add-label",
        "ready",
        "--remove-label",
        "in-review",
    )


# ── board_block_feature → store.flag_blocked ────────────────────────────────────────


def test_board_block_feature_flags_blocked_and_comments_reason(make_board, monkeypatch):
    _, br = _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "blocked"})

    out = json.loads(_get_tool("board_block_feature").invoke({"feature_id": "bd-1", "reason": "waiting on bd-0"}))

    assert out == {"id": "bd-1", "state": "blocked"}
    (update,) = br.cmds("update")
    assert update == ("update", "bd-1", "--add-label", "blocked", "--assignee", "")
    # the reason rides through as an auditable comment on the bead.
    (comment,) = br.cmds("comments")
    assert comment == ("comments", "add", "bd-1", "blocked: waiting on bd-0")


def test_board_block_feature_strips_wrapping_quotes_from_reason(make_board, monkeypatch):
    _, br = _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "blocked"})

    _get_tool("board_block_feature").invoke({"feature_id": "bd-1", "reason": '"waiting on bd-0"'})

    (comment,) = br.cmds("comments")
    assert comment == ("comments", "add", "bd-1", "blocked: waiting on bd-0")


# ── board_unblock_feature → store.clear_blocked ─────────────────────────────────────


def test_board_unblock_feature_removes_the_blocked_label(make_board, monkeypatch):
    _, br = _wire(make_board, monkeypatch, {"id": "bd-1", "board_state": "ready"})

    out = json.loads(_get_tool("board_unblock_feature").invoke({"feature_id": "bd-1"}))

    assert out == {"id": "bd-1", "state": "ready"}
    (update,) = br.cmds("update")
    assert update == ("update", "bd-1", "--remove-label", "blocked")


# ── the shared error path: a bad id surfaces as an Error string, not an exception ───


@pytest.mark.parametrize(
    "name,args",
    [
        ("board_cancel_feature", {"feature_id": "bd-x"}),
        ("board_requeue_feature", {"feature_id": "bd-x"}),
        ("board_block_feature", {"feature_id": "bd-x", "reason": "r"}),
        ("board_unblock_feature", {"feature_id": "bd-x"}),
    ],
)
def test_lifecycle_tools_return_error_string_for_unknown_feature(make_board, monkeypatch, name, args):
    # get_feature → None makes the store's `_require(fid)` raise BoardError; the tool must
    # catch it and hand back an "Error: …" string rather than blowing up the agent turn.
    br = _RecordingBr()
    board = make_board(br)
    monkeypatch.setattr(board, "get_feature", lambda fid: None)
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)

    out = _get_tool(name).invoke(args)

    assert out.startswith("Error: unknown feature")
    assert br.cmds("update") == []  # nothing was written for a non-existent feature
