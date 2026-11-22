"""#753 (1 of 2): a card can carry its own coder timeout, and its first timeout earns the
next dispatch a longer one.

Every coder dispatch used the board-wide `coder_timeout_s`, and the re-dispatch after a
`CoderTimeout` ran under the same budget that had just expired. mythxengine card br-l23, a
long measurement run, timed out, was blocked, and the lead finished it by hand.

Now:
1. `board_create_feature` / `board_update_feature` take `coder_timeout_s` (positive
   seconds), stored as a single replaced `coder-timeout:<n>` label; `board_get_feature`
   shows it. Zero, a negative or a non-number is refused with a plain error and the card is
   left unchanged; an empty value on update leaves the field alone.
2. Every dispatch resolves its budget through `BoardLoop._coder_timeout_for`: the card's own
   value, else the global.
3. A card's FIRST timeout grants its later dispatches 1.5x the budget that expired — once
   per card, durably (`budget:timeout-raise-ms:<n>`). A second timeout never raises again.
"""

from __future__ import annotations

import json
import shutil

import pytest

import project_board as pb
from project_board import coder_seam, worktree
from project_board import store as store_mod
from project_board.loop import BoardLoop
from project_board.store import BeadsBoard, BoardError, coder_timeout_from_labels, validate_coder_timeout

from test_loop import _no_sleep
from test_timeout_decompose_378 import _SplitStore

requires_br = pytest.mark.skipif(shutil.which(store_mod.BR) is None, reason="real `br` (beads) CLI not on PATH")

_TIMEOUT = "coder timed out"
_AC = "- WHEN x THE SYSTEM SHALL y"


def _tool(name):
    return {t.name: t for t in pb._board_tools({})}[name]


# ── 1/2: the field — validation, storage, projection ────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected", [(3600, 3600), ("3600", 3600), (" 90 ", 90), (1.2, 2), (None, None), ("", None), ("  ", None)]
)
def test_validate_coder_timeout_accepts_positive_seconds_and_treats_blank_as_not_given(raw, expected):
    assert validate_coder_timeout(raw) == expected


@pytest.mark.parametrize("raw", [0, -5, "0", "-1", "abc", float("nan"), float("inf"), True])
def test_validate_coder_timeout_refuses_a_non_positive_or_non_numeric_value(raw):
    with pytest.raises(BoardError, match="coder_timeout_s must be a positive number"):
        validate_coder_timeout(raw)


def test_the_label_decodes_back_and_a_malformed_one_is_ignored():
    assert coder_timeout_from_labels(["diff:small", "coder-timeout:3600"]) == 3600
    assert coder_timeout_from_labels(["coder-timeout:abc", "coder-timeout:0"]) is None
    assert coder_timeout_from_labels([]) is None


def test_update_feature_replaces_the_single_label(make_board):
    calls = []

    def _run(*args, want_json=False, with_has_more=False):
        calls.append(args)
        if args[0] == "show":
            return [{"id": "bd-1", "status": "open", "labels": ["coder-timeout:1200"]}]
        return [] if want_json else ""

    b = make_board(_run)
    b.update_feature("bd-1", coder_timeout_s=3600)
    upd = next(a for a in calls if a[0] == "update")
    assert "--add-label" in upd and "coder-timeout:3600" in upd
    assert "--remove-label" in upd and "coder-timeout:1200" in upd


def test_update_feature_refuses_a_bad_value_before_any_write(make_board):
    calls = []
    b = make_board(lambda *a, **k: calls.append(a) or ([] if k.get("want_json") else ""))
    for bad in (0, -1, "abc"):
        with pytest.raises(BoardError):
            b.update_feature("bd-1", coder_timeout_s=bad)
    assert not [a for a in calls if a and a[0] == "update"]


@requires_br
def test_a_real_card_stores_its_timeout_through_the_tools(tmp_path, monkeypatch):
    """r1/r2 end to end on real `br`: create with 3600, read 3600 back; update to 5400;
    every bad value is refused with a plain error and leaves the card as it was; an empty
    value on update leaves the field alone."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)
    create, update, get = _tool("board_create_feature"), _tool("board_update_feature"), _tool("board_get_feature")

    created = json.loads(
        create.invoke(
            {
                "title": "Long measurement",
                "spec": "s",
                "acceptance_criteria": _AC,
                "files_to_modify": "a.py (new)",
                "coder_timeout_s": 3600,
            }
        )
    )
    fid = created["id"]
    assert json.loads(get.invoke({"feature_id": fid}))["coder_timeout_s"] == 3600

    for bad in (0, -60, "abc"):
        out = update.invoke({"feature_id": fid, "coder_timeout_s": bad})
        assert out.startswith("Error:") and "coder_timeout_s must be a positive number" in out
        assert json.loads(get.invoke({"feature_id": fid}))["coder_timeout_s"] == 3600

    update.invoke({"feature_id": fid, "coder_timeout_s": "", "design": "d"})
    assert json.loads(get.invoke({"feature_id": fid}))["coder_timeout_s"] == 3600

    update.invoke({"feature_id": fid, "coder_timeout_s": 5400})
    got = json.loads(get.invoke({"feature_id": fid}))
    assert got["coder_timeout_s"] == 5400
    assert [l for l in got["labels"] if l.startswith("coder-timeout:")] == ["coder-timeout:5400"]

    plain = json.loads(
        create.invoke({"title": "Plain", "spec": "s", "acceptance_criteria": _AC, "files_to_modify": "b.py (new)"})
    )
    assert json.loads(get.invoke({"feature_id": plain["id"]}))["coder_timeout_s"] is None


@requires_br
def test_a_real_create_with_a_bad_timeout_mints_no_card(tmp_path, monkeypatch):
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)
    create = _tool("board_create_feature")
    for bad in (0, -1, "abc"):
        out = create.invoke({"title": f"Bad {bad}", "spec": "s", "acceptance_criteria": _AC, "coder_timeout_s": bad})
        assert out.startswith("Error:") and "coder_timeout_s" in out
    assert board.list_features() == []
    with pytest.raises(BoardError):
        board.create_feature("Direct", spec="s", coder_timeout_s=0)
    assert board.list_features() == []


def test_the_tools_forward_the_value_only_when_given(monkeypatch):
    seen = {}

    class _Store:
        def list_features(self, state=None):
            return []

        def create_feature(self, title, **kw):
            seen["create"] = kw
            return {"id": "bd-1", "title": title, "board_state": "backlog"}

        def update_feature(self, fid, **kw):
            seen["update"] = kw
            return {"id": fid, "title": "T", "board_state": "backlog"}

    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: _Store())
    _tool("board_create_feature").invoke({"title": "T", "coder_timeout_s": "3600"})
    assert seen["create"]["coder_timeout_s"] == 3600
    _tool("board_create_feature").invoke({"title": "U"})
    assert "coder_timeout_s" not in seen["create"]
    _tool("board_update_feature").invoke({"feature_id": "bd-1", "design": "d"})
    assert "coder_timeout_s" not in seen["update"]
    seen.clear()
    out = _tool("board_update_feature").invoke({"feature_id": "bd-1", "coder_timeout_s": "abc"})
    assert out.startswith("Error:") and "update" not in seen


# ── 3/4: the dispatch budget ───────────────────────────────────────────────────────────


def _timing_out_board(monkeypatch, store, cfg):
    """A drive whose every dispatch times out after real model work, recording the timeout
    each dispatch was given."""
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)
    timeouts: list = []

    async def _create(repo, base, fid, root, title="", **_kw):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _noop(*_a, **_kw):
        return None

    async def _dispatch(coder, wt, prompt, *, timeout=None, env_passthrough=()):
        timeouts.append(timeout)
        coder_seam.progress_tool("bd-1", 1, {"phase": "start", "name": "Edit", "id": "t1", "input": {"path": "a"}})
        raise worktree.CoderTimeout(f"{_TIMEOUT} after {timeout}s")

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "remove_worktree", _noop)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _noop)
    loop = BoardLoop(cfg)
    monkeypatch.setattr(store, "current_tier", lambda fid: "standard", raising=False)
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: name)
    return loop, timeouts


_LADDER = {"coders": {"smart": "a", "reasoning": "b", "opus": "c"}, "coder_timeout_s": 1800}


def test_the_helper_prefers_the_cards_own_timeout():
    loop = BoardLoop({"coder_timeout_s": 1800})
    assert loop._coder_timeout_for({"id": "bd-1", "coder_timeout_s": 3600}) == 3600
    assert loop._coder_timeout_for({"id": "bd-2"}) == 1800
    assert loop._coder_timeout_for({"id": "bd-3", "labels": ["coder-timeout:2400"]}) == 2400
    assert BoardLoop({"coder_timeout_s": 0})._coder_timeout_for({"id": "bd-4"}) is None


async def test_a_card_with_its_own_timeout_dispatches_with_it(monkeypatch):
    """r3: global 1800, card 3600 → the dispatch gets 3600; a card without it gets 1800."""
    store = _SplitStore()
    loop, timeouts = _timing_out_board(monkeypatch, store, {**_LADDER, "decompose_after_timeouts": 0})
    await loop._drive({"id": "bd-1", "title": "Long run", "spec": "s", "coder_timeout_s": 3600})
    assert timeouts[0] == 3600

    store = _SplitStore()
    loop, timeouts = _timing_out_board(monkeypatch, store, {**_LADDER, "decompose_after_timeouts": 0})
    await loop._drive({"id": "bd-1", "title": "Plain", "spec": "s"})
    assert timeouts[0] == 1800


async def test_the_first_timeout_raises_the_next_dispatch_once(monkeypatch):
    """r4: timed out at 1800 → the climb's dispatch gets 2700; timed out again → the next
    dispatch stays at 2700, never 4050. The park threshold is off so the ladder runs out."""
    store = _SplitStore(tiers=["reasoning", "advanced"])
    loop, timeouts = _timing_out_board(monkeypatch, store, {**_LADDER, "decompose_after_timeouts": 0})
    await loop._drive({"id": "bd-1", "title": "Long run", "spec": "s"})
    assert timeouts == [1800, 2700, 2700]
    assert store.budgets["bd-1:timeout-raise-ms"] == 2_700_000


async def test_the_raise_is_one_and_a_half_times_the_cards_own_timeout(monkeypatch):
    store = _SplitStore(tiers=["reasoning"])
    loop, timeouts = _timing_out_board(monkeypatch, store, {**_LADDER, "decompose_after_timeouts": 0})
    await loop._drive({"id": "bd-1", "title": "Long run", "spec": "s", "coder_timeout_s": 3600})
    assert timeouts == [3600, 5400]


async def test_the_raise_survives_a_restart_and_is_not_granted_twice(monkeypatch):
    """A fresh loop reads the durable raise off the bead: its first dispatch gets 2700, and
    a timeout there does not raise again."""
    store = _SplitStore(tiers=[])
    loop, timeouts = _timing_out_board(monkeypatch, store, {**_LADDER, "decompose_after_timeouts": 0})
    await loop._drive({"id": "bd-1", "title": "Long run", "spec": "s", "labels": ["budget:timeout-raise-ms:2700000"]})
    assert timeouts == [2700]
    assert "bd-1:timeout-raise-ms" not in store.budgets  # no second raise written


async def test_the_park_threshold_and_its_count_are_unchanged(monkeypatch):
    """The default too-wide threshold (2) still parks on the second fresh timeout, after the
    raised retry ran."""
    store = _SplitStore(tiers=["reasoning", "advanced"])
    loop, timeouts = _timing_out_board(monkeypatch, store, dict(_LADDER))
    await loop._drive({"id": "bd-1", "title": "Wide", "spec": "s"})
    assert timeouts == [1800, 2700]
    assert store.asked == [("bd-1", 2)]
    assert store.budgets["bd-1:timeout"] == 2
