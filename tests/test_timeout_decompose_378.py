"""#378: a card that keeps timing out is too wide, and the loop must stop re-running it.

#379 capped `large` in the breadth gate and #380 made a repeated timeout ask the board's
own agent to split the card. What was left, each one reproduced on origin/main:

1. The ask was never picked up. `request_decomposition` filed its task in `backlog` like
   every new bead, and the puller only pulls `ready`, so "the board's own agent" was
   asked by nobody (real `br`: the task is `backlog` and absent from `ready_queue`).
2. The ask waited for the ladder to run out. It lived on the ladder-exhausted block, so
   a card on a three-rung ladder timed out three times — the last on the priciest model
   — before anything asked for a split.
3. The card was then rebuilt anyway. The block beside the ask was the self-healing
   `transient`, so the sweep requeued the card it had just asked to split and rebuilt
   it whole, up to two more full timeouts, racing its own decomposition.
"""

from __future__ import annotations

import shutil

import pytest

from project_board import coder_seam, worktree
from project_board import store as store_mod
import project_board.loop as loop_mod
from project_board.loop import BoardLoop
from project_board.store import BeadsBoard, BoardError

from test_loop import _BlockedStore, _EscalatingStore, _blocked, _no_sleep

_TIMEOUT = "coder timed out after 1800s"


class _SplitStore(_EscalatingStore):
    """Climbs through `tiers`, keeps budget labels durably, and answers the decompose ask
    with `answer` (a task dict, or None for "already asked / could not file")."""

    def __init__(self, tiers=(), *, answer=None):
        super().__init__(list(tiers))
        self.asked: list[tuple[str, int]] = []
        self.budgets: dict[str, int] = {}
        self._answer = {"id": "bd-split"} if answer is None else answer

    def record_budget(self, fid, kind, n):
        self.budgets[f"{fid}:{kind}"] = n

    def clear_budgets(self, fid, kinds=None):
        pass

    def get_feature(self, fid):
        labels = [f"budget:{k.split(':', 1)[1]}:{v}" for k, v in self.budgets.items() if k.startswith(f"{fid}:")]
        return {"id": fid, "labels": labels, "board_state": "in_progress"}

    def request_decomposition(self, fid, *, timeouts):
        self.asked.append((fid, timeouts))
        self.calls.append(("request_decomposition", fid, timeouts))
        return self._answer or None


def _blocks(store):
    return [c for c in store.calls if c[0] == "flag_blocked"]


def _timing_out_board(monkeypatch, store, cfg, *, start_tier="smart"):
    """A drive whose every dispatch WORKS (a tool call reaches the ring buffer — this is a
    size signal, not the pre-first-token infra timeout #339 blocks) and then times out."""
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)
    dispatched: list[str] = []

    async def _create(repo, base, fid, root, title="", **_kw):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _noop(*_a, **_kw):
        return None

    async def _dispatch(coder, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatched.append(coder)
        coder_seam.progress_tool("bd-1", 1, {"phase": "start", "name": "Edit", "id": "t1", "input": {"path": "a.py"}})
        raise worktree.CoderTimeout(_TIMEOUT)

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "remove_worktree", _noop)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _noop)
    loop = BoardLoop(cfg)
    monkeypatch.setattr(store, "current_tier", lambda fid: start_tier, raising=False)
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: name)
    return loop, dispatched


async def test_the_second_timeout_asks_for_a_split_instead_of_climbing_again(monkeypatch):
    """Gap 2. The first timeout climbs, carrying #146's timeout context — one can be an
    unlucky gate run. The second is the size signal: ask, and stop. It must not spend the
    top rung on a card the loop has just concluded is too wide."""
    store = _SplitStore(tiers=["reasoning", "opus"])
    loop, dispatched = _timing_out_board(monkeypatch, store, {"coders": {"smart": "a", "reasoning": "b", "opus": "c"}})
    await loop._drive({"id": "bd-1", "title": "Wide card", "spec": "s"})

    assert dispatched == ["a", "b"], f"the threshold timeout climbed instead of asking: {dispatched}"
    assert store.asked == [("bd-1", 2)]
    assert len(store.escalated) == 1  # the first timeout's climb, and only that
    blocked = _blocks(store)
    assert len(blocked) == 1 and "timed out 2x" in blocked[0][2]
    # Parked BEFORE the ask: the ask files its task `ready`, and the agent that picks it up
    # cancels this card — it must never find the card still in flight.
    names = store.names()
    assert names.index("flag_blocked") < names.index("request_decomposition")


async def test_a_card_parked_for_its_split_is_not_rebuilt_by_the_blocked_sweep(monkeypatch):
    """Gap 3, end to end: the block the drive writes, read back by the sweep. The card the
    loop just asked to split must stay parked — the operator hears about it once — instead
    of being requeued and rebuilt whole while its decomposition is under way."""
    store = _SplitStore()
    store.budgets["bd-1:timeout"] = 1  # a one-coder board: the first timeout already blocked
    loop, dispatched = _timing_out_board(monkeypatch, store, {"coder": "proto"})
    await loop._drive({"id": "bd-1", "title": "Wide card", "spec": "s"})
    assert store.asked == [("bd-1", 2)]
    (_, fid, reason, category) = _blocks(store)[-1]

    lane = _BlockedStore([_blocked(fid, category.replace("_", "-"), reason=reason, title="Wide card")])
    sweep = BoardLoop({"coder": "proto"})
    told: list[str] = []
    monkeypatch.setattr(sweep, "_notify_operator", lambda _fid, text, **_kw: told.append(text))
    await sweep._recover_blocked(lane)

    assert lane.requeued == [] and lane.cleared == [], f"the sweep rebuilt a card parked for its split ({category})"
    assert len(told) == 1 and "timed out 2x" in told[0] and "Wide card" in told[0]
    assert category.replace("_", "-") not in loop_mod._SELF_HEALING_BLOCKS


async def test_a_card_past_the_threshold_is_parked_even_when_no_new_ask_is_filed(monkeypatch):
    """The park follows from the card, not from the filing. Here the store files nothing —
    the card was asked once already and an operator requeued it since, or the store could
    not file. It timed out past the threshold again: it is exactly as wide, so it is parked
    for the operator, not climbed onto a pricier rung or handed back to the sweep."""
    store = _SplitStore(tiers=["reasoning"], answer={})
    store.budgets["bd-1:timeout"] = 3  # well past the threshold
    loop, dispatched = _timing_out_board(monkeypatch, store, {"coders": {"smart": "a", "reasoning": "b"}})
    await loop._drive({"id": "bd-1", "title": "Wide card", "spec": "s"})

    assert dispatched == ["a"] and store.escalated == [], dispatched
    assert store.asked == [("bd-1", 4)]  # still asked — the store decides whether anything is filed
    blocked = _blocks(store)
    assert len(blocked) == 1 and blocked[0][3] not in ("transient", "rate_limit", "merge_conflict")


# ── gap 1: the ask has to reach the puller ────────────────────────────────────────────


@pytest.mark.skipif(shutil.which(store_mod.BR) is None, reason="real `br` (beads) CLI not on PATH")
def test_the_decompose_ask_is_filed_ready_so_the_agent_actually_gets_it(tmp_path):
    """Against REAL `br`: the puller's queue is `br ready --label ready`. A task filed in
    `backlog` is never dispatched, whatever its assignee — so the ask must come out of
    `request_decomposition` already promoted, through the ordinary Ready gate."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    wide = board.create_feature(
        "Wide card that keeps timing out",
        spec="build the whole subsystem",
        acceptance_criteria="- WHEN done THE SYSTEM SHALL work",
        files_to_modify=["a.py (new)", "b.py (new)"],
    )
    task = board.request_decomposition(wide["id"], timeouts=2)

    assert task is not None
    assert board.get_feature(task["id"])["board_state"] == "ready"
    assert task["id"] in [f["id"] for f in board.ready_queue()], "the self-dispatch path never sees the ask"


def test_a_refused_promotion_still_counts_as_asked(make_board, monkeypatch):
    """If the Ready gate refuses the task, it stays filed in backlog for a human — the card
    was still asked (its once-per-card label is on), and the promotion failure is logged,
    not raised: the ask never raises into the drive."""
    calls: list[tuple] = []

    def _br(*args, want_json=False):
        calls.append(args)
        return {}

    b = make_board(_br)
    monkeypatch.setattr(
        b, "get_feature", lambda fid: {"id": fid, "title": "Wide", "labels": [], "issue_type": "feature"}
    )
    monkeypatch.setattr(b, "create_feature", lambda title, **kw: {"id": "bd-9"})
    monkeypatch.setattr(b, "comment", lambda fid, text: None)

    def _refuse(fid):
        raise BoardError(f"Ready gate: {fid} refused")

    monkeypatch.setattr(b, "mark_ready", _refuse)
    assert b.request_decomposition("bd-8", timeouts=2) == {"id": "bd-9"}
    assert ("update", "bd-8", "--add-label", "decompose-asked") in calls


def test_the_ask_is_promoted_after_the_once_per_card_label(make_board, monkeypatch):
    """Order matters: the label is what stops a second ask. Promoting first would let a
    promotion crash leave a filed task with no label, and the next timeout would file a
    duplicate."""
    order: list[str] = []

    def _br(*args, want_json=False):
        if "decompose-asked" in args:
            order.append("label")
        return {}

    b = make_board(_br)
    monkeypatch.setattr(
        b, "get_feature", lambda fid: {"id": fid, "title": "Wide", "labels": [], "issue_type": "feature"}
    )
    monkeypatch.setattr(b, "create_feature", lambda title, **kw: {"id": "bd-9"})
    monkeypatch.setattr(b, "comment", lambda fid, text: None)
    monkeypatch.setattr(b, "mark_ready", lambda fid: order.append("ready") or {"id": fid, "board_state": "ready"})
    out = b.request_decomposition("bd-8", timeouts=2)

    assert order == ["label", "ready"]
    assert out == {"id": "bd-9", "board_state": "ready"}
