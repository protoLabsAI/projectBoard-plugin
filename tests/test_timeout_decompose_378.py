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

And from the review of the first cut: only a FRESH build's timeout that reached the model
is a size signal (an infra timeout, or a fix round on a card that already built, is not);
the park must say what it actually filed; an operator unblock must give a real retry; and
the split's own steps must pass the gates in the order they are written.
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
requires_br = pytest.mark.skipif(shutil.which(store_mod.BR) is None, reason="real `br` (beads) CLI not on PATH")


class _SplitStore(_EscalatingStore):
    """Climbs through `tiers`, keeps budget labels durably, answers the decompose ask with
    `answer` (a task, or {} for "nothing filed"), and records the task's release."""

    def __init__(self, tiers=(), *, answer=None):
        super().__init__(list(tiers))
        self.asked: list[tuple[str, int]] = []
        self.budgets: dict[str, int] = {}
        self._answer = {"id": "bd-split", "board_state": "backlog"} if answer is None else answer

    def record_budget(self, fid, kind, n):
        self.budgets[f"{fid}:{kind}"] = n

    def clear_budgets(self, fid, kinds=None):
        for kind in kinds or [k.split(":", 1)[1] for k in self.budgets if k.startswith(f"{fid}:")]:
            self.budgets.pop(f"{fid}:{kind}", None)
        self.calls.append(("clear_budgets", fid, tuple(kinds or ())))

    def get_feature(self, fid):
        labels = [f"budget:{k.split(':', 1)[1]}:{v}" for k, v in self.budgets.items() if k.startswith(f"{fid}:")]
        return {"id": fid, "labels": labels, "board_state": "in_progress"}

    def request_decomposition(self, fid, *, timeouts):
        self.asked.append((fid, timeouts))
        self.calls.append(("request_decomposition", fid, timeouts))
        return self._answer or None

    def mark_ready(self, fid):
        self.calls.append(("mark_ready", fid))
        return {"id": fid, "board_state": "ready"}


def _blocks(store):
    return [c for c in store.calls if c[0] == "flag_blocked"]


def _timing_out_board(monkeypatch, store, cfg, *, start_tier="smart", model_worked=True):
    """A drive whose every dispatch times out — after real model work (a tool call reaches
    the ring buffer) unless `model_worked=False`, the pre-first-token infra timeout."""
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)
    dispatched: list[str] = []

    async def _create(repo, base, fid, root, title="", **_kw):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _noop(*_a, **_kw):
        return None

    async def _dispatch(coder, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatched.append(coder)
        if model_worked:
            coder_seam.progress_tool("bd-1", 1, {"phase": "start", "name": "Edit", "id": "t1", "input": {"path": "a"}})
        raise worktree.CoderTimeout(_TIMEOUT)

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "remove_worktree", _noop)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _noop)
    loop = BoardLoop(cfg)
    monkeypatch.setattr(store, "current_tier", lambda fid: start_tier, raising=False)
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: name)
    return loop, dispatched


_CARD = {"id": "bd-1", "title": "Wide card", "spec": "s"}


# ── the park: when, and in what order ────────────────────────────────────────────────


async def test_the_second_timeout_asks_for_a_split_instead_of_climbing_again(monkeypatch):
    """The first timeout climbs, carrying #146's timeout context — one can be an unlucky
    gate run. The second is the size signal: park and ask. It must not spend the top rung
    on a card the loop has just concluded is too wide. The park names the task, and lands
    between filing it (backlog, invisible to the puller) and releasing it (ready): the
    agent that picks it up cancels this card, which must never be found in flight."""
    store = _SplitStore(tiers=["reasoning", "opus"])
    loop, dispatched = _timing_out_board(monkeypatch, store, {"coders": {"smart": "a", "reasoning": "b", "opus": "c"}})
    await loop._drive(_CARD)

    assert dispatched == ["a", "b"], f"the threshold timeout climbed instead of asking: {dispatched}"
    assert store.asked == [("bd-1", 2)] and len(store.escalated) == 1
    (_, _, reason, category) = _blocks(store)[-1]
    assert category == "too-wide" and "timed out 2x" in reason and "bd-split" in reason
    names = store.names()
    assert names.index("request_decomposition") < names.index("flag_blocked") < names.index("mark_ready")
    assert ("mark_ready", "bd-split") in store.calls


async def test_a_card_parked_for_its_split_is_not_rebuilt_by_the_blocked_sweep(monkeypatch):
    """End to end: the block the drive writes, read back by the sweep. The card the loop
    just asked to split stays parked — the operator hears about it once — instead of being
    requeued and rebuilt whole while its decomposition is under way."""
    store = _SplitStore()
    store.budgets["bd-1:timeout"] = 1  # a one-coder board: the first timeout already blocked
    loop, _ = _timing_out_board(monkeypatch, store, {"coder": "proto"})
    await loop._drive(_CARD)
    (_, fid, reason, category) = _blocks(store)[-1]

    lane = _BlockedStore([_blocked(fid, category.replace("_", "-"), reason=reason, title="Wide card")])
    sweep = BoardLoop({"coder": "proto"})
    told: list[str] = []
    monkeypatch.setattr(sweep, "_notify_operator", lambda _fid, text, **_kw: told.append(text))
    await sweep._recover_blocked(lane)

    assert lane.requeued == [] and lane.cleared == [], f"the sweep rebuilt a card parked for its split ({category})"
    assert len(told) == 1 and "bd-split" in told[0] and "Wide card" in told[0]
    assert category.replace("_", "-") not in loop_mod._SELF_HEALING_BLOCKS


async def test_a_re_park_that_files_nothing_says_so_and_what_to_do(monkeypatch):
    """Nothing was filed this time — the card was asked once and requeued by hand since, or
    the store refused. It is exactly as wide, so it is still parked, but the reason (and
    so the operator's alert) must not claim a split is on its way."""
    store = _SplitStore(tiers=["reasoning"], answer={})
    store.budgets["bd-1:timeout"] = 3
    loop, dispatched = _timing_out_board(monkeypatch, store, {"coders": {"smart": "a", "reasoning": "b"}})
    await loop._drive(_CARD)

    assert dispatched == ["a"] and store.escalated == [], dispatched
    (_, _, reason, category) = _blocks(store)[-1]
    assert category == "too-wide"
    assert "NO split task was filed" in reason and "split it by hand" in reason and "coder_timeout_s" in reason
    assert "mark_ready" not in store.names()


# ── what counts ──────────────────────────────────────────────────────────────────────


async def test_a_pre_first_token_timeout_is_never_counted(monkeypatch):
    """A timeout with no model work is a wedged adapter (#339): infra, blocked for triage,
    and NOT a size signal. It used to be counted all the same, so after the operator fixed
    the infra, the card's first genuine timeout parked it and asked for a split."""
    store = _SplitStore()
    loop, _ = _timing_out_board(monkeypatch, store, {"coder": "proto"}, model_worked=False)
    await loop._drive(_CARD)
    assert _blocks(store)[-1][3] == "dispatch-infra"
    assert "bd-1:timeout" not in store.budgets and "bd-1" not in loop._timeout_attempts

    loop2, _ = _timing_out_board(monkeypatch, store, {"coder": "proto"})  # infra fixed; one real timeout
    await loop2._drive(_CARD)
    assert store.asked == [] and store.budgets["bd-1:timeout"] == 1
    assert _blocks(store)[-1][3] == "transient"  # the ordinary first-timeout block


async def test_a_fix_round_on_a_card_with_an_open_pr_is_never_parked(monkeypatch):
    """A card whose PR is open already BUILT in one dispatch — it is not too wide. A fix
    round on it (a CI bounce) that times out takes the ordinary path. Parked, it would be
    asked to split and cancel, and the cancel would close its open PR."""
    store = _SplitStore(tiers=["reasoning"])
    store.budgets["bd-1:timeout"] = 5  # well past the threshold
    loop, dispatched = _timing_out_board(monkeypatch, store, {"coders": {"smart": "a", "reasoning": "b"}})
    loop._ci_feedback["bd-1"] = "CI failed: test_x"
    await loop._drive({**_CARD, "pr_url": "https://example/pr/1"})

    assert store.asked == [] and all(c[3] != "too-wide" for c in _blocks(store))
    assert store.budgets["bd-1:timeout"] == 5, "a fix-round timeout is not a size signal"
    assert store.escalated, "the ordinary timeout path climbs"


async def test_a_keep_worktree_fix_round_that_times_out_is_not_counted(monkeypatch):
    """Same rule inside one drive: the build returned a diff, the goal check found a gap,
    and the fix round on the KEPT worktree timed out. The card built in one dispatch."""
    store = _SplitStore()
    store.budgets["bd-1:timeout"] = 1  # one short of the threshold
    loop, dispatched = _timing_out_board(monkeypatch, store, {"coder": "proto"})
    calls = {"n": 0}

    async def _dispatch(coder, wt, prompt, *, timeout=None, env_passthrough=()):
        calls["n"] += 1
        dispatched.append(coder)
        coder_seam.progress_tool("bd-1", 1, {"phase": "start", "name": "Edit", "id": "t1", "input": {}})
        if calls["n"] == 1:
            return "built it"
        raise worktree.CoderTimeout(_TIMEOUT)  # the fix round on the kept worktree

    gaps = iter(["missing tests"])

    async def _gap(feature, wt, base, reply=""):
        return next(gaps, None)

    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(loop, "_verify_goal", _gap)
    loop.goal_verify = True
    await loop._drive(_CARD)

    assert store.asked == [] and store.budgets["bd-1:timeout"] == 1
    assert all(c[3] != "too-wide" for c in _blocks(store))


async def test_a_build_that_reaches_review_resets_the_count(monkeypatch):
    """A card that built once is demonstrably not too wide: the timeout count is cleared
    when a build reaches review, so an old timeout can never combine with a later one."""
    store = _SplitStore(tiers=["reasoning"])
    loop, dispatched = _timing_out_board(monkeypatch, store, {"coders": {"smart": "a", "reasoning": "b"}})
    calls = {"n": 0}

    async def _dispatch(coder, wt, prompt, *, timeout=None, env_passthrough=()):
        calls["n"] += 1
        coder_seam.progress_tool("bd-1", 1, {"phase": "start", "name": "Edit", "id": "t1", "input": {}})
        if calls["n"] == 1:
            raise worktree.CoderTimeout(_TIMEOUT)
        return "built it"

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        return "https://example/pr/1"

    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    await loop._drive(_CARD)

    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    assert "bd-1:timeout" not in store.budgets and loop._timeout_attempts.get("bd-1") == 0


async def test_an_operator_unblock_gives_a_parked_card_a_real_retry(monkeypatch):
    """The operator raised `coder_timeout_s` and unblocked the parked card. The store resets
    the persisted count; the running loop's cached count must go too, or the retry's first
    timeout re-parks the card at once (#259: the cache wins over the labels)."""
    store = _SplitStore()
    store.budgets["bd-1:timeout"] = 1
    loop, _ = _timing_out_board(monkeypatch, store, {"coder": "proto"})
    await loop._drive(_CARD)
    assert _blocks(store)[-1][3] == "too-wide" and loop._timeout_attempts["bd-1"] == 2

    store.budgets.pop("bd-1:timeout")  # clear_blocked on a too-wide park (store half, pinned below)
    monkeypatch.setattr(loop_mod._common, "live_loop", lambda: loop)
    assert loop_mod.forget_timeout_count("bd-1") is True
    await loop._drive(_CARD)  # the retry times out once more

    assert _blocks(store)[-1][3] == "transient", "the retry re-parked on its first timeout"
    assert store.asked == [("bd-1", 2)]


def test_clear_blocked_resets_the_count_only_for_a_too_wide_park(make_board, monkeypatch):
    """The store half. Only the park's own class resets the count: the blocked sweep also
    clears blocks (its self-heal), and resetting there would stop a one-coder board's
    timeouts from ever adding up."""
    calls: list[tuple] = []
    b = make_board(lambda *args, want_json=False: calls.append(args) or {})
    labels = {"too-wide": ["blocked", "blocked-class:too-wide", "budget:timeout:2", "budget:goal-fix:1"]}
    labels["transient"] = ["blocked", "blocked-class:transient", "budget:timeout:1"]
    for cls, row in labels.items():
        calls.clear()
        monkeypatch.setattr(b, "get_feature", lambda fid, row=row: {"id": fid, "labels": row, "board_state": "blocked"})
        b.clear_blocked("bd-9")
        update = next(c for c in calls if c[0] == "update")
        if cls == "too-wide":
            assert "budget:timeout:2" in update and "blocked-class:too-wide" in update
            assert "budget:goal-fix:1" not in update
        else:
            assert not any(str(a).startswith("budget:") for a in update)


# ── the ask itself ───────────────────────────────────────────────────────────────────


def _asking_board(make_board, monkeypatch, *, rows, card_labels=()):
    calls: list[tuple] = []
    b = make_board(lambda *args, want_json=False: calls.append(args) or ([] if want_json else ""))
    card = {"id": "bd-8", "title": "Wide", "labels": list(card_labels), "issue_type": "feature", "spec": "s"}
    monkeypatch.setattr(b, "get_feature", lambda fid: card)
    monkeypatch.setattr(b, "list_features", lambda *a, **k: rows)
    monkeypatch.setattr(b, "comment", lambda fid, text: None)
    created: list[dict] = []
    monkeypatch.setattr(
        b, "create_feature", lambda title, **kw: created.append({"title": title, **kw}) or {"id": "bd-9"}
    )
    monkeypatch.setattr(b, "mark_ready", lambda fid: pytest.fail("the ask must not release its own task"))
    return b, calls, created


def test_the_ask_files_in_backlog_with_the_dependents_and_the_safe_order(make_board, monkeypatch):
    """Filed, not released: the caller parks the card first. The spec names the card's
    dependents and orders the steps so each passes the gates the next relies on."""
    dependent = {"id": "bd-d", "issue_type": "feature", "depends_on": ["bd-8"], "board_state": "ready", "title": "D"}
    b, calls, created = _asking_board(make_board, monkeypatch, rows=[dependent])
    assert b.request_decomposition("bd-8", timeouts=2) == {"id": "bd-9"}

    spec = created[0]["spec"]
    assert "- bd-d" in spec
    steps = ["board_create_feature", "board_update_feature", "board_cancel_feature", "board_mark_ready"]
    assert [spec.index(s) for s in steps] == sorted(spec.index(s) for s in steps)
    assert "LEAVE THEM IN BACKLOG" in spec
    assert ("update", "bd-8", "--add-label", "decompose-asked") in calls


def test_the_ask_is_idempotent_on_the_task_not_only_the_label(make_board, monkeypatch):
    """The label is written AFTER the create. If that write is lost, the next park must find
    the task it already filed — not file a duplicate and orphan the first — and heal the
    label. An open task is handed back so the re-park can name and release it."""
    filed = {"id": "bd-9", "issue_type": "task", "title": "Decompose bd-8 — timed out 2x, too wide to build"}
    b, calls, created = _asking_board(make_board, monkeypatch, rows=[{**filed, "board_state": "backlog"}])
    assert b.request_decomposition("bd-8", timeouts=3)["id"] == "bd-9"
    assert created == [] and ("update", "bd-8", "--add-label", "decompose-asked") in calls

    b, calls, created = _asking_board(make_board, monkeypatch, rows=[{**filed, "board_state": "done"}])
    assert b.request_decomposition("bd-8", timeouts=3) is None  # its split already ran
    assert created == []


# ── against real `br` ────────────────────────────────────────────────────────────────


@requires_br
async def test_a_real_card_is_parked_naming_its_split_and_the_split_is_dispatchable(tmp_path, monkeypatch):
    """The drive against a REAL board: the card is parked `too-wide` with the task named,
    and the task is `ready` — in the puller's queue — while the card is not."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    wide = board.create_feature(
        "Wide card", spec="s", acceptance_criteria="- WHEN x THE SYSTEM SHALL y", files_to_modify=["a.py (new)"]
    )
    fid = wide["id"]
    board.mark_ready(fid)
    board.record_budget(fid, "timeout", 1)
    claimed = board.claim(fid, assignee="proto")
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)
    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)

    async def _create(repo, base, f, root, title="", **_kw):
        return ("/wt/feat-" + f, "feat/" + f)

    async def _noop(*_a, **_kw):
        return None

    async def _dispatch(coder, wt, prompt, *, timeout=None, env_passthrough=()):
        coder_seam.progress_tool(fid, 1, {"phase": "start", "name": "Edit", "id": "t1", "input": {}})
        raise worktree.CoderTimeout(_TIMEOUT)

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "remove_worktree", _noop)
    loop = BoardLoop({"coder": "proto", "kg_lessons": False})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    await loop._drive(claimed)

    card = board.get_feature(fid)
    assert card["board_state"] == "blocked" and card["blocked_class"] == "too-wide"
    task = next(t for t in board.list_features() if t.get("issue_type") == "task")
    assert task["id"] in card["blocked_reason"]
    queue = board.ready_queue()
    assert task["id"] in [f["id"] for f in queue] and fid not in [f["id"] for f in queue]

    # …and the puller hands it to the board's OWN agent (#311), assignee intact.
    async def _invoke(prompt, session_id):
        return "split"

    started: list[str] = []

    async def _drive_self_task(feature, invoke, session_id):
        started.append(session_id)

    monkeypatch.setattr(coder_seam, "resolve_self_invoke", lambda: _invoke)
    monkeypatch.setattr(coder_seam, "host_invoke_busy", lambda: False)
    monkeypatch.setattr(loop, "_drive_self_task", _drive_self_task)
    assert await loop._dispatch_task(board, next(f for f in queue if f["id"] == task["id"])) == "self"
    for t in list(loop._drives):
        await t
    assert started == [f"board-self-{task['id']}"]
    assert board.get_feature(task["id"])["assignee"] == "agent"


@requires_br
def test_the_split_steps_pass_the_gates_end_to_end_in_the_order_written(tmp_path):
    """Executes the filed task's steps against a REAL board, as the agent would, in its
    order: slices in backlog → dependents re-pointed → original cancelled → slices ready.
    Every gate passes; the dependent is never released early; it is released once the
    slices land. And the two ways the first cut's order broke are pinned alongside."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    ac = "- WHEN x THE SYSTEM SHALL y"
    wide = board.create_feature("Wide", spec="s", acceptance_criteria=ac, files_to_modify=["a.py (new)", "b.py (new)"])
    w = wide["id"]
    board.mark_ready(w)
    dep = board.create_feature("Dependent", spec="s", acceptance_criteria=ac, files_to_modify=["c.py (new)"])
    board.add_dependency(dep["id"], w)
    board.mark_ready(dep["id"])
    board.flag_blocked(w, "too wide — parked", category="too-wide")
    task = board.request_decomposition(w, timeouts=2)
    assert f"- {dep['id']}" in task["spec"]

    # 1. slices, left in backlog
    s1 = board.create_feature("Slice 1", spec="s", acceptance_criteria=ac, files_to_modify=["a.py (new)"])["id"]
    s2 = board.create_feature("Slice 2", spec="s", acceptance_criteria=ac, files_to_modify=["b.py (new)"])["id"]
    with pytest.raises(BoardError, match="Shared-file gate"):  # why step 4 waits for step 3
        board.mark_ready(s1)
    # 2. re-point the dependent onto the slices it needs
    board.update_feature(dep["id"], depends_on=[s1, s2])
    # 3. cancel the original
    board.cancel_feature(w, f"superseded by {s1}, {s2}")
    assert dep["id"] not in [f["id"] for f in board.ready_queue()], "the cancel released the dependent early"
    # 4. slices ready
    board.mark_ready(s1)
    board.mark_ready(s2)

    queue = [f["id"] for f in board.ready_queue()]
    assert s1 in queue and s2 in queue and dep["id"] not in queue
    for s in (s1, s2):
        board.claim(s, assignee="proto")
        board.mark_done(s, reason="shipped")
    assert dep["id"] in [f["id"] for f in board.ready_queue()]  # released once the slices landed


@requires_br
def test_a_lost_label_write_cannot_file_a_duplicate_split(tmp_path):
    """Real `br`: the task exists but the card's `decompose-asked` label does not — the
    write after the create was lost. The next ask returns the SAME task and heals the
    label; there is still exactly one decompose task."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    wide = board.create_feature(
        "Wide", spec="s", acceptance_criteria="- WHEN x THE SYSTEM SHALL y", files_to_modify=["a.py (new)"]
    )
    first = board.request_decomposition(wide["id"], timeouts=2)
    board._run("update", wide["id"], "--remove-label", "decompose-asked")

    again = board.request_decomposition(wide["id"], timeouts=3)
    assert again is not None and again["id"] == first["id"]
    assert "decompose-asked" in board.get_feature(wide["id"])["labels"]
    assert [t["id"] for t in board.list_features() if t.get("issue_type") == "task"] == [first["id"]]


@requires_br
def test_an_operator_unblock_resets_a_real_parked_cards_count(tmp_path):
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    wide = board.create_feature(
        "Wide", spec="s", acceptance_criteria="- WHEN x THE SYSTEM SHALL y", files_to_modify=["a.py (new)"]
    )
    fid = wide["id"]
    board.record_budget(fid, "timeout", 2)
    board.flag_blocked(fid, "too wide — parked", category="too-wide")
    board.clear_blocked(fid)

    labels = board.get_feature(fid)["labels"]
    assert not any(label.startswith("budget:timeout:") for label in labels)
    assert "blocked-class:too-wide" not in labels and "blocked" not in labels
