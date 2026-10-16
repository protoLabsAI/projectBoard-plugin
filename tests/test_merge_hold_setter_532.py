"""#532 — a first-class setter for the per-card `merge-hold` operator veto.

The `merge-hold` label (store.LABEL_MERGE_HOLD) is the documented operator veto on the
auto-merge edge, but it was only ever READ: no tool, no store verb and no HTTP route
could add or remove it, so on an `auto_merge=true` board nobody but a human running
`br label add` could make one card human-merge-only. This adds the audited verb pair —
`store.set_merge_hold` / `store.release_merge_hold`, the `board_hold_merge` /
`board_release_merge_hold` tools, and POST/DELETE `/features/{fid}/merge-hold` — plus
the `merge_hold: bool` the projection and `board_get_feature` now expose. Auto-merge
behaviour is unchanged: the existing merge_posture / `_auto_merge_blockers` edge already
honours the label; these tests pin only the new setter.

The store path is exercised with a stateful fake `br` (``_FakeBr``): it serves `show`,
applies `update` label edits in place and records `comments add`, so the real verbs
round-trip (a re-read reflects a write) and idempotency is exercised for real.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import project_board as pb
from project_board import api, store, worktree
from project_board.loop import BoardLoop
from project_board.store import NEXT_ACTION_MERGE_HOLD, merge_posture

PR = "https://github.com/o/r/pull/42"


def _bead(fid="bd-1", *, status="open", labels=(), title="t"):
    return {"id": fid, "title": title, "status": status, "labels": list(labels)}


class _FakeBr:
    """A stateful fake ``br`` for one board: serves ``show``, applies ``update``
    label/status edits in place, and appends ``comments add`` — so the real store verbs
    round-trip and idempotency is genuine, not mocked."""

    def __init__(self, beads):
        self.beads = {b["id"]: dict(b) for b in beads}
        self.calls = []

    def __call__(self, *args, want_json=False, with_has_more=False, actor=""):
        self.calls.append(args)
        cmd = args[0] if args else ""
        if cmd == "show":
            bead = self.beads.get(args[1])
            if bead is None:
                raise store.BoardNotFound(f"issue not found: {args[1]!r}")
            row = dict(bead)
            row["labels"] = list(bead.get("labels", []))
            row["comments"] = list(bead.get("comments", []))
            return [row]
        if cmd == "update":
            bead = self.beads[args[1]]
            labels = list(bead.get("labels", []))
            rest = list(args[2:])
            i = 0
            while i < len(rest):
                flag = rest[i]
                val = rest[i + 1] if i + 1 < len(rest) else ""
                if flag == "--add-label" and val not in labels:
                    labels.append(val)
                elif flag == "--remove-label" and val in labels:
                    labels.remove(val)
                elif flag == "--status":
                    bead["status"] = val
                i += 2
            bead["labels"] = labels
            return [] if want_json else ""
        if cmd == "comments":  # ("comments", "add", fid, text)
            self.beads[args[2]].setdefault("comments", []).append(args[3])
            return [] if want_json else ""
        return [] if want_json else ""

    def comments_for(self, fid):
        return list(self.beads.get(fid, {}).get("comments", []))

    def count(self, name):
        return sum(1 for c in self.calls if c and c[0] == name)


def _tool(name):
    return {t.name: t for t in pb._board_tools({})}[name]


def _wire(board, monkeypatch):
    """Point the tool wrappers' lazily-resolved ``get_store`` at ``board`` so
    ``board_*`` calls go through the real store verbs against the fake ``br``."""
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)


# ── registration ─────────────────────────────────────────────────────────────────


def test_merge_hold_tools_are_registered():
    names = {t.name for t in pb._board_tools({})}
    assert {"board_hold_merge", "board_release_merge_hold"} <= names


# ── r1: board_hold_merge adds the label across non-terminal states, merge_hold reads true ─


@pytest.mark.parametrize(
    "labels, state",
    [
        ([], "backlog"),
        (["ready"], "ready"),
        (["blocked"], "blocked"),
    ],
)
def test_board_hold_merge_adds_the_label_across_non_terminal_states(make_board, monkeypatch, labels, state):
    board = make_board(_FakeBr([_bead("bd-1", status="open", labels=labels)]))
    _wire(board, monkeypatch)

    out = json.loads(_tool("board_hold_merge").invoke({"feature_id": "bd-1", "reason": "QA this by hand"}))
    assert out == {"id": "bd-1", "state": state, "merge_hold": True}

    got = json.loads(_tool("board_get_feature").invoke({"feature_id": "bd-1"}))
    assert got["merge_hold"] is True
    assert store.LABEL_MERGE_HOLD in got["labels"]


# ── r2: a terminal card or an unknown id returns an Error and changes nothing ───────


@pytest.mark.parametrize(
    "bead",
    [
        _bead("bd-1", status="closed"),  # done
        _bead("bd-1", status="closed", labels=["cancelled"]),  # cancelled
    ],
)
def test_set_merge_hold_refuses_terminal_and_changes_nothing(make_board, bead):
    board = make_board(_FakeBr([bead]))
    with pytest.raises(store.BoardError):
        board.set_merge_hold("bd-1", "too late")
    assert board._run.count("update") == 0
    assert board._run.comments_for("bd-1") == []


def test_set_merge_hold_unknown_id_errors_and_changes_nothing(make_board):
    board = make_board(_FakeBr([]))
    with pytest.raises(store.BoardError):
        board.set_merge_hold("bd-nope", "whatever")
    assert board._run.count("update") == 0 and board._run.count("comments") == 0


def test_board_hold_merge_on_terminal_returns_error_string(make_board, monkeypatch):
    board = make_board(_FakeBr([_bead("bd-1", status="closed")]))
    _wire(board, monkeypatch)
    out = _tool("board_hold_merge").invoke({"feature_id": "bd-1", "reason": "too late"})
    assert out.startswith("Error:")
    assert board._run.count("update") == 0


def test_board_hold_merge_unknown_id_returns_error_string(make_board, monkeypatch):
    board = make_board(_FakeBr([]))
    _wire(board, monkeypatch)
    out = _tool("board_hold_merge").invoke({"feature_id": "bd-x", "reason": "r"})
    assert out.startswith("Error:")


def test_board_hold_merge_requires_a_reason(make_board, monkeypatch):
    board = make_board(_FakeBr([_bead("bd-1", status="open", labels=["ready"])]))
    _wire(board, monkeypatch)
    out = _tool("board_hold_merge").invoke({"feature_id": "bd-1", "reason": "   "})
    assert out.startswith("Error:")
    assert board._run.count("update") == 0  # a blank reason never touches the bead


# ── r3: each set and each release writes exactly one audit comment; repeat is idempotent ─


def test_each_set_and_release_writes_exactly_one_comment(make_board):
    board = make_board(_FakeBr([_bead("bd-1", status="open", labels=["ready"])]))
    fake = board._run

    board.set_merge_hold("bd-1", "QA by hand")
    assert fake.comments_for("bd-1") == ["merge-hold set: QA by hand"]
    assert fake.count("update") == 1

    # repeat set is a no-op: no duplicate label write, no second comment
    board.set_merge_hold("bd-1", "QA by hand again")
    assert fake.comments_for("bd-1") == ["merge-hold set: QA by hand"]
    assert fake.count("update") == 1

    board.release_merge_hold("bd-1")
    assert fake.comments_for("bd-1") == ["merge-hold set: QA by hand", "merge-hold released"]

    # repeat release is likewise idempotent — no second "released" comment
    board.release_merge_hold("bd-1")
    assert fake.comments_for("bd-1") == ["merge-hold set: QA by hand", "merge-hold released"]


# ── r4: board_release_merge_hold removes the label; merge_hold reads false ──────────


def test_board_release_merge_hold_removes_the_label(make_board, monkeypatch):
    board = make_board(_FakeBr([_bead("bd-1", status="in_progress", labels=["in-review", "merge-hold"])]))
    _wire(board, monkeypatch)

    out = json.loads(_tool("board_release_merge_hold").invoke({"feature_id": "bd-1"}))
    assert out == {"id": "bd-1", "state": "in_review", "merge_hold": False}

    got = json.loads(_tool("board_get_feature").invoke({"feature_id": "bd-1"}))
    assert got["merge_hold"] is False
    assert store.LABEL_MERGE_HOLD not in got["labels"]


# ── r5: a card held via the NEW verb is NOT auto-merged, and reads the operator veto ─


async def test_held_card_is_not_auto_merged_and_names_the_veto(make_board, monkeypatch):
    """Drive a card that is otherwise green for auto-merge (in_review, review-clean,
    its clean verdict pinned to the live head) through the loop's auto-merge edge after
    placing a hold via the NEW verb: the edge must NOT merge it, and the operator-facing
    posture must read `merge-hold (operator veto)`. Releasing the hold leaves nothing
    else holding it — proving the veto is the sole blocker, not a mis-set gate."""
    head = "0123456789abcdef0123456789abcdef01234567"
    board = make_board(
        _FakeBr(
            [
                _bead(
                    "bd-1",
                    status="in_progress",
                    labels=["in-review", "review-clean", f"review-clean-sha:{head[: store.SHORT_SHA_LEN]}"],
                )
            ]
        )
    )

    held = board.set_merge_hold("bd-1", "QA the console layout by hand")
    assert held["merge_hold"] is True and store.LABEL_MERGE_HOLD in held["labels"]

    merges = []

    async def _never_merge(*a, **k):  # pragma: no cover — asserted never called
        merges.append(a)
        return (True, "")

    monkeypatch.setattr(worktree, "merge_pr", _never_merge)

    loop = BoardLoop({"auto_merge": True, "review_gate": True})
    assert await loop._maybe_auto_merge(board, "bd-1", PR, "/repo") is False
    assert merges == []  # the merge edge never fired on a held card

    posture = merge_posture(board.get_feature("bd-1"), auto_merge=True, review_gate=True, head_sha=head)
    assert "merge-hold" in posture["blockers"]
    assert posture["next_action"] == NEXT_ACTION_MERGE_HOLD

    # lift the hold: the SAME card now has an empty blocker set and auto-merges
    released = board.release_merge_hold("bd-1")
    assert released["merge_hold"] is False
    open_posture = merge_posture(board.get_feature("bd-1"), auto_merge=True, review_gate=True, head_sha=head)
    assert open_posture["blockers"] == []
    assert open_posture["next_action"] == "auto-merge pending"


# ── r6: POST / DELETE /features/{fid}/merge-hold behave the same as the tools ───────


def _client(board, monkeypatch):
    monkeypatch.setattr(api, "get_store", lambda **_kw: board)
    app = FastAPI()
    app.include_router(api.build_data_router({}), prefix="/api/plugins/project_board")
    return TestClient(app)


def test_http_merge_hold_post_then_delete_round_trips(make_board, monkeypatch):
    board = make_board(_FakeBr([_bead("bd-1", status="in_progress", labels=["in-review"])]))
    c = _client(board, monkeypatch)
    base = "/api/plugins/project_board/features/bd-1/merge-hold"

    r = c.post(base, json={"reason": "QA by hand"})
    assert r.status_code == 200
    body = r.json()
    assert body["merge_hold"] is True and store.LABEL_MERGE_HOLD in body["labels"]
    assert board._run.comments_for("bd-1") == ["merge-hold set: QA by hand"]

    # idempotent over HTTP: a second POST writes no second audit comment
    assert c.post(base, json={"reason": "again"}).json()["merge_hold"] is True
    assert board._run.comments_for("bd-1") == ["merge-hold set: QA by hand"]

    d = c.delete(base)
    assert d.status_code == 200
    assert d.json()["merge_hold"] is False and store.LABEL_MERGE_HOLD not in d.json()["labels"]
    assert board._run.comments_for("bd-1")[-1] == "merge-hold released"


def test_http_merge_hold_refuses_a_terminal_card(make_board, monkeypatch):
    board = make_board(_FakeBr([_bead("bd-1", status="closed")]))
    c = _client(board, monkeypatch)
    resp = c.post("/api/plugins/project_board/features/bd-1/merge-hold", json={"reason": "too late"})
    assert resp.status_code == 400
    assert board._run.count("update") == 0


def test_http_merge_hold_requires_a_reason(make_board, monkeypatch):
    board = make_board(_FakeBr([_bead("bd-1", status="open", labels=["ready"])]))
    c = _client(board, monkeypatch)
    resp = c.post("/api/plugins/project_board/features/bd-1/merge-hold", json={})
    assert resp.status_code == 400
    assert board._run.count("update") == 0
