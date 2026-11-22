"""#717: two agent turns reacting to the same event end up with exactly one live card.

Turn A created bd-0ia51.33, turn B created bd-0ia51.34 before it could see .33, and each
turn then cancelled the OTHER card as "the duplicate": both ended cancelled, the work was
lost. Search-before-create cannot close that window, and a cancel had no idea which card
was canonical.

Now:
1. `create_feature(idempotency_key=...)` is an atomic create-or-get under a board-wide lock;
   the key rides the card as an `idem:<hash>` label written by `br create` itself.
2. The canonical card for a key is the oldest (created_at, then id).
3. `cancel_feature(fid, duplicate_of=other)` is refused when fid is canonical for a shared
   key, when `other` is cancelled, or when `other` is already a duplicate of fid — checked
   and applied under the same lock.

These run on the real `br` CLI: the race and the durability live in beads, not in a double.
"""

from __future__ import annotations

import json
import shutil
import threading

import pytest

import project_board as pb
from project_board import store as store_mod
from project_board.store import BeadsBoard, BoardError, dup_of_label, idempotency_label

requires_br = pytest.mark.skipif(shutil.which(store_mod.BR) is None, reason="real `br` (beads) CLI not on PATH")

_KEY = "producer:bd-0ia51 completion#7"


def _tool(name):
    return {t.name: t for t in pb._board_tools({})}[name]


def _board(tmp_path):
    return BeadsBoard(repo=str(tmp_path), actor="test")


def _live(board, *ids):
    return [i for i in ids if board.get_feature(i)["board_state"] not in ("cancelled", "done")]


def _run_together(*calls):
    """Run each zero-arg call on its own thread, released together; return results/exceptions."""
    gate = threading.Barrier(len(calls))
    out: list = [None] * len(calls)

    def _go(i, fn):
        gate.wait()
        try:
            out[i] = fn()
        except Exception as exc:  # noqa: BLE001 — the test inspects it
            out[i] = exc

    threads = [threading.Thread(target=_go, args=(i, fn)) for i, fn in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return out


def _sharing_pair(board):
    """X created with the key, then Y created later and given the same key — the shape two
    processes (or a pre-#717 race) can still leave behind."""
    x = board.create_feature("Handle producer completion", spec="s", idempotency_key=_KEY)["id"]
    y = board.create_feature("Handle producer completion (copy)", spec="s")["id"]
    board._run("update", y, "--add-label", idempotency_label(_KEY))
    assert board.idempotency_canonical(_KEY) == x
    return x, y


def test_the_key_label_is_stable_hashed_and_label_safe():
    a = idempotency_label(_KEY)
    assert a == idempotency_label(f"  {_KEY} ") and a.startswith("idem:")
    assert a != idempotency_label(_KEY + "x")
    assert idempotency_label("") == "" and idempotency_label("   ") == ""
    assert all(c.isalnum() or c in "-_:" for c in a)
    assert dup_of_label("bd-0ia51.33") == "dup-of:bd-0ia51_33"


# ── r1: concurrent creates with one key ─────────────────────────────────────────────


@requires_br
def test_two_concurrent_creates_with_one_key_make_exactly_one_card(tmp_path):
    board = _board(tmp_path)
    a, b = _run_together(
        lambda: board.create_feature("Turn A's title", spec="s", idempotency_key=_KEY),
        lambda: board.create_feature("Turn B's title", spec="s", idempotency_key=_KEY),
    )
    assert isinstance(a, dict) and isinstance(b, dict), (a, b)
    rows = board.list_features(include_archived=True)
    assert len(rows) == 1
    assert a["id"] == b["id"] == rows[0]["id"]
    assert idempotency_label(_KEY) in rows[0]["labels"]
    assert sorted([bool(a.get("idempotent_existing")), bool(b.get("idempotent_existing"))]) == [False, True]


@requires_br
def test_many_racing_creates_per_key_still_make_one_card_per_key(tmp_path):
    """Several racers on each of several keys, all released at once — widens the window a
    look-then-create would lose in, so the test fails without the lock."""
    board = _board(tmp_path)
    keys = [f"event-{n}" for n in range(3)]
    out = _run_together(
        *[
            (lambda k=k, n=n: board.create_feature(f"{k} racer {n}", spec="s", idempotency_key=k))
            for k in keys
            for n in range(4)
        ]
    )
    assert all(isinstance(r, dict) for r in out), out
    rows = board.list_features(include_archived=True)
    assert len(rows) == len(keys)
    for i, k in enumerate(keys):
        ids = {r["id"] for r in out[i * 4 : i * 4 + 4]}
        assert ids == {board.idempotency_canonical(k)}


# ── r2: an existing card (any state) is returned; force does not change it ─────────────


@requires_br
@pytest.mark.parametrize("terminal", ["cancelled", "done"])
def test_an_existing_keyed_card_in_any_state_is_returned_even_with_force(tmp_path, monkeypatch, terminal):
    board = _board(tmp_path)
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)
    create = _tool("board_create_feature")
    first = json.loads(create.invoke({"title": "Original", "spec": "s", "idempotency_key": _KEY}))
    fid = first["id"]
    if terminal == "cancelled":
        board.cancel_feature(fid, "scope cut")
    else:
        board._run("close", fid, "-r", "shipped")
    assert board.get_feature(fid)["board_state"] == terminal

    for force in (False, True):
        reply = json.loads(create.invoke({"title": "Retry", "spec": "s", "idempotency_key": _KEY, "force": force}))
        assert reply["id"] == fid and reply["created"] is False and reply["existing"] is True
        assert fid in reply["message"] and "no new card was created" in reply["message"]
    direct = board.create_feature("Retry direct", spec="s", idempotency_key=_KEY)
    assert direct["id"] == fid and direct["idempotent_existing"] is True
    assert [r["id"] for r in board.list_features(include_archived=True)] == [fid]


@requires_br
def test_the_task_tool_honours_the_key_too(tmp_path, monkeypatch):
    board = _board(tmp_path)
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)
    create = _tool("board_create_task")
    first = json.loads(create.invoke({"title": "Write the note", "spec": "s", "idempotency_key": _KEY}))
    again = json.loads(create.invoke({"title": "Write the note", "spec": "s", "idempotency_key": _KEY}))
    assert again["id"] == first["id"] and again["created"] is False
    assert len(board.list_features(include_archived=True)) == 1


# ── r3: durable across a restart ────────────────────────────────────────────────────


@requires_br
def test_a_fresh_store_over_the_same_db_returns_the_existing_card(tmp_path):
    fid = _board(tmp_path).create_feature("Original", spec="s", idempotency_key=_KEY)["id"]
    fresh = _board(tmp_path)
    again = fresh.create_feature("After restart", spec="s", idempotency_key=_KEY)
    assert again["id"] == fid and again["idempotent_existing"] is True
    assert len(fresh.list_features(include_archived=True)) == 1


# ── r4: different work stays separate ───────────────────────────────────────────────


@requires_br
def test_different_keys_or_no_key_create_separate_cards(tmp_path):
    board = _board(tmp_path)
    a = board.create_feature("Work", spec="s", idempotency_key="event-1")
    b = board.create_feature("Work", spec="s", idempotency_key="event-2")
    c = board.create_feature("Work", spec="s")
    d = board.create_feature("Work", spec="s")
    assert len({a["id"], b["id"], c["id"], d["id"]}) == 4
    assert not any(f.get("idempotent_existing") for f in (a, b, c, d))
    assert not [l for l in board.get_feature(c["id"])["labels"] if l.startswith("idem:")]


# ── r5: opposing duplicate cancels ──────────────────────────────────────────────────


@requires_br
def test_opposing_concurrent_duplicate_cancels_leave_the_canonical_card_live(tmp_path):
    board = _board(tmp_path)
    x, y = _sharing_pair(board)
    out = _run_together(
        lambda: board.cancel_feature(y, "dup", duplicate_of=x),
        lambda: board.cancel_feature(x, "dup", duplicate_of=y),
    )
    assert _live(board, x, y) == [x]
    assert isinstance(out[0], dict) and isinstance(out[1], BoardError), out
    got = board.get_feature(y)
    assert dup_of_label(x) in got["labels"]


@requires_br
def test_opposing_concurrent_cancels_of_unkeyed_cards_leave_one_live(tmp_path):
    board = _board(tmp_path)
    a = board.create_feature("Same work", spec="s")["id"]
    b = board.create_feature("Same work", spec="s")["id"]
    _run_together(
        lambda: board.cancel_feature(a, duplicate_of=b),
        lambda: board.cancel_feature(b, duplicate_of=a),
    )
    assert len(_live(board, a, b)) == 1


@requires_br
def test_cancelling_the_canonical_card_as_a_duplicate_is_refused_by_name(tmp_path, monkeypatch):
    board = _board(tmp_path)
    x, y = _sharing_pair(board)
    with pytest.raises(BoardError) as exc:
        board.cancel_feature(x, duplicate_of=y)
    assert f"{x} is the canonical card" in str(exc.value)
    assert _live(board, x, y) == [x, y]
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)
    out = _tool("board_cancel_feature").invoke({"feature_id": x, "duplicate_of": y})
    assert out.startswith("Error:") and f"{x} is the canonical card" in out
    assert _live(board, x, y) == [x, y]


@requires_br
def test_a_guarded_cancel_records_duplicate_of_in_its_audit_reason(tmp_path):
    board = _board(tmp_path)
    x, y = _sharing_pair(board)
    board.cancel_feature(y, "same completion", duplicate_of=x)
    shown = board._run("show", y, want_json=True)
    shown = shown[0] if isinstance(shown, list) else shown
    assert shown.get("close_reason") == f"cancelled: duplicate of {x}: same completion"
    assert board.get_feature(y)["board_state"] == "cancelled"


# ── r6: refusals on a dead or opposing duplicate_of ─────────────────────────────────


@requires_br
def test_a_duplicate_of_that_is_cancelled_is_refused(tmp_path):
    board = _board(tmp_path)
    a = board.create_feature("A", spec="s")["id"]
    b = board.create_feature("B", spec="s")["id"]
    board.cancel_feature(b, "scope cut")
    with pytest.raises(BoardError, match=rf"duplicate_of {b} is itself cancelled"):
        board.cancel_feature(a, duplicate_of=b)
    assert _live(board, a) == [a]


@requires_br
def test_a_duplicate_of_already_recorded_as_a_duplicate_of_the_target_is_refused(tmp_path):
    board = _board(tmp_path)
    a = board.create_feature("A", spec="s")["id"]
    b = board.create_feature("B", spec="s")["id"]
    board._run("update", b, "--add-label", dup_of_label(a))  # b recorded as a dup of a, still live
    with pytest.raises(BoardError, match=rf"{b} is already recorded as a duplicate of {a}"):
        board.cancel_feature(a, duplicate_of=b)
    assert _live(board, a, b) == [a, b]


@requires_br
def test_a_duplicate_of_that_names_no_card_or_itself_is_refused(tmp_path):
    board = _board(tmp_path)
    a = board.create_feature("A", spec="s")["id"]
    with pytest.raises(BoardError, match="itself"):
        board.cancel_feature(a, duplicate_of=a)
    with pytest.raises(BoardError, match="not a card"):
        board.cancel_feature(a, duplicate_of="bd-nope")
    assert _live(board, a) == [a]


# ── r7: no key / no duplicate_of → unchanged calls ───────────────────────────────────


def test_the_tools_pass_the_new_arguments_only_when_given(monkeypatch):
    seen: dict = {}

    class _Store:
        def list_features(self, state=None):
            return []

        def get_feature(self, fid):
            return {"id": fid, "title": "T", "board_state": "backlog"}

        def create_feature(self, title, **kw):
            seen["create"] = kw
            return {"id": "bd-1", "title": title, "board_state": "backlog"}

        def cancel_feature(self, fid, reason="", **kw):
            seen["cancel"] = kw
            return {"id": fid, "title": "T", "board_state": "cancelled"}

    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: _Store())
    monkeypatch.setattr("project_board.loop.cancel_side_effects", lambda *a, **k: {})
    _tool("board_create_feature").invoke({"title": "T"})
    assert "idempotency_key" not in seen["create"]
    _tool("board_create_task").invoke({"title": "T"})
    assert "idempotency_key" not in seen["create"]
    _tool("board_cancel_feature").invoke({"feature_id": "bd-1", "reason": "r"})
    assert seen["cancel"] == {}
    _tool("board_cancel_feature").invoke({"feature_id": "bd-1", "duplicate_of": "bd-2"})
    assert seen["cancel"] == {"duplicate_of": "bd-2"}


def test_lookup_avoids_combined_br_filters_and_orders_actual_matches(tmp_path, monkeypatch):
    """0.1.23's combined filters can hide every keyed card; labels are matched locally."""
    board = _board(tmp_path)
    label = idempotency_label(_KEY)
    rows = [
        {"id": "z", "created_at": "2026-01-01", "labels": [label, "archived"]},
        {"id": "new", "created_at": "2026-01-02", "labels": [label]},
        {"id": "a", "created_at": "2026-01-01", "labels": [label]},
        {"id": "unrelated", "created_at": "2025-01-01"},
    ]

    def run(*args, **kwargs):
        assert args[0] == "list"
        assert "--label" not in args
        assert "closed" in args and "deferred" in args
        assert args[-2:] == ("--limit", "0")
        assert kwargs == {"want_json": True, "with_has_more": True}
        return rows, False

    monkeypatch.setattr(board, "_run", run)
    assert board.idempotency_canonical(_KEY) == "a"


@pytest.mark.parametrize("rows,has_more", [(None, False), ([{}], False), ([], True)])
def test_incomplete_lookup_refuses_create(tmp_path, monkeypatch, rows, has_more):
    board = _board(tmp_path)
    monkeypatch.setattr(board, "_run", lambda *a, **kw: (rows, has_more))
    monkeypatch.setattr(board, "_create_feature_chained", lambda *a, **kw: pytest.fail("unsafe create"))
    with pytest.raises(BoardError, match="idempotency listing"):
        board.create_feature("Retry", idempotency_key=_KEY)


@requires_br
@pytest.mark.parametrize("tool_name", ["board_create_feature", "board_create_task"])
def test_label_prefixed_caller_key_survives_archival_and_restart(tmp_path, monkeypatch, tool_name):
    board = _board(tmp_path)
    monkeypatch.setattr(store_mod, "get_store", lambda **kw: board)
    create = _tool(tool_name)
    key = "idem:caller-chosen-event"
    first = json.loads(create.invoke({"title": "Original", "idempotency_key": key}))
    board.cancel_feature(first["id"])
    board._run("update", first["id"], "--add-label", "archived")
    board = _board(tmp_path)
    again = json.loads(create.invoke({"title": "Retry", "idempotency_key": key, "force": True}))
    assert again["id"] == first["id"] and again["created"] is False
    assert len(board.list_features(include_archived=True)) == 1
