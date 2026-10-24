"""#495 slice 2: a PR GitHub holds not-clean for hours shows on /status health.

The merge edge correctly refuses anything but CLEAN, and #495 slice 1 made such a card READ
`held: PR not clean on GitHub (UNSTABLE)`. But nothing surfaced a card that had been stuck
that way for hours — a `QA panel` check that never settles, an unresolved review thread. The
merge-state hold already stamps a first-seen `since`; the reconcile pass now publishes the
in_review cards held longer than `merge_hold_alert_s` to /status, like a claim stall, and
clears the entry when the hold clears or the PR merges or closes.
"""

from __future__ import annotations

import time

import pytest

from project_board import health, merge_state_hold, store as store_mod, worktree
from project_board.loop import BoardLoop

pytestmark = pytest.mark.asyncio

PR = "https://github.com/o/r/pull/42"
REVIEW = [{"id": "bd-1", "pr_url": PR, "board_state": "in_review"}]


@pytest.fixture(autouse=True)
def _no_leaked_merge_holds():
    """The health entry is process-stable like the preflight one; start and end clean so a
    hold published here never shows up in another test's /status read."""
    health._health.pop("merge_holds", None)
    yield
    health._health.pop("merge_holds", None)


def _aged_hold(fid: str, age_s: float, *, status: str = "UNSTABLE", checks=("QA panel: in progress",)) -> None:
    """Record a merge-state hold and backdate its first-seen stamp by ``age_s`` seconds."""
    merge_state_hold.set_hold(fid, status, list(checks), PR)
    merge_state_hold._HOLDS[fid]["since"] = time.time() - age_s


async def test_a_hold_older_than_the_threshold_is_published_with_text_and_age():
    """r1: held past merge_hold_alert_s → listed, with the operator hold text and its age."""
    loop = BoardLoop({"coder": "proto", "auto_merge": True})  # merge_hold_alert_s defaults to 3600
    _aged_hold("bd-1", 7200)

    loop._publish_merge_holds(REVIEW)

    (row,) = health.merge_holds_snapshot()
    assert row["id"] == "bd-1"
    assert row["hold"] == "held: PR not clean on GitHub (UNSTABLE) — QA panel: in progress"
    assert row["status"] == "UNSTABLE"
    assert row["pr_url"] == PR
    assert row["age_s"] >= 3600


async def test_a_hold_under_the_threshold_is_not_listed():
    """r3: a hold younger than the threshold is normal (every fresh PR), so it is not listed."""
    loop = BoardLoop({"coder": "proto", "auto_merge": True})
    _aged_hold("bd-1", 60)  # a minute old

    loop._publish_merge_holds(REVIEW)

    assert health.merge_holds_snapshot() == []


async def test_the_entry_clears_when_the_hold_clears():
    """r2: a PR that goes CLEAN clears the hold (the merge edge's clear_hold), and the next
    pass drops it from the health entry."""
    loop = BoardLoop({"coder": "proto", "auto_merge": True})
    _aged_hold("bd-1", 7200)
    loop._publish_merge_holds(REVIEW)
    assert [r["id"] for r in health.merge_holds_snapshot()] == ["bd-1"]

    # The real clean-transition path: mergeStateStatus is no longer a held status.
    loop._note_merge_state_hold("bd-1", PR, ["github mergeStateStatus=CLEAN"], None)
    assert merge_state_hold.hold_for("bd-1") is None

    loop._publish_merge_holds(REVIEW)
    assert health.merge_holds_snapshot() == []


class _MergeStore:
    """Minimal store for driving the terminal reconcile edges — record_merge short-circuits
    (the hold is cleared before it is consulted), flag_blocked is a no-op record."""

    def record_merge(self, pr_url=None):
        return False

    def flag_blocked(self, *a, **k):
        return None


async def test_the_entry_clears_when_the_pr_merges(monkeypatch):
    """r2: driving the real MERGED reconcile edge clears the hold, and the next pass drops it."""
    loop = BoardLoop({"coder": "proto", "auto_merge": True})
    _aged_hold("bd-1", 7200)
    loop._publish_merge_holds(REVIEW)
    assert [r["id"] for r in health.merge_holds_snapshot()] == ["bd-1"]

    f = {"id": "bd-1", "pr_url": PR, "board_state": "in_review"}
    await loop._reconcile_pr(_MergeStore(), f, known_state="MERGED")
    assert merge_state_hold.hold_for("bd-1") is None

    loop._publish_merge_holds(REVIEW)
    assert health.merge_holds_snapshot() == []


async def test_the_entry_clears_when_the_pr_closes(monkeypatch):
    """r2: a PR closed unmerged takes the same clear_hold before its triage block/reap."""
    loop = BoardLoop({"coder": "proto", "auto_merge": True})

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(worktree, "reap_feature_worktree", _noop)
    monkeypatch.setattr(loop, "_budget_reset", _noop)
    _aged_hold("bd-1", 7200, status="BLOCKED", checks=())
    loop._publish_merge_holds(REVIEW)
    assert [r["id"] for r in health.merge_holds_snapshot()] == ["bd-1"]

    f = {"id": "bd-1", "pr_url": PR, "board_state": "in_review"}
    await loop._reconcile_pr(_MergeStore(), f, known_state="CLOSED")
    assert merge_state_hold.hold_for("bd-1") is None

    loop._publish_merge_holds(REVIEW)
    assert health.merge_holds_snapshot() == []


class _ReviewStore:
    def __init__(self):
        self.cards = [dict(f) for f in REVIEW]

    def list_features(self, state=None):
        return [f for f in self.cards if f["board_state"] == state]


async def test_the_reconcile_pass_publishes_the_holds(monkeypatch):
    """The wiring: _reconcile_prs itself publishes, so the health entry appears without any
    explicit call — it reads the holds the prior pass's reconciles left."""
    loop = BoardLoop({"coder": "proto", "auto_merge": True})
    monkeypatch.setattr(loop, "_store", lambda: _ReviewStore())
    monkeypatch.setattr(loop, "_start_card_work", lambda *a: None)
    _aged_hold("bd-1", 7200)

    await loop._reconcile_prs(detach=True)

    assert [r["id"] for r in health.merge_holds_snapshot()] == ["bd-1"]


async def test_zero_turns_the_health_entry_off():
    """merge_hold_alert_s=0 disables the entry entirely (the per-card next_action still shows)."""
    loop = BoardLoop({"coder": "proto", "auto_merge": True, "merge_hold_alert_s": 0})
    _aged_hold("bd-1", 999999)

    loop._publish_merge_holds(REVIEW)

    assert health.merge_holds_snapshot() == []
    assert merge_state_hold.hold_for("bd-1") is not None  # the hold itself is untouched


@pytest.mark.parametrize("state", ["blocked", "ready", "in_progress", "done", "cancelled", None])
@pytest.mark.parametrize("threshold", [3600, 0])
async def test_leaving_review_clears_the_hold_and_reentering_starts_a_new_clock(monkeypatch, state, threshold):
    loop = BoardLoop({"coder": "proto", "auto_merge": True, "merge_hold_alert_s": threshold})
    store = _ReviewStore()
    monkeypatch.setattr(loop, "_store", lambda: store)
    monkeypatch.setattr(loop, "_start_card_work", lambda *a: None)
    _aged_hold("bd-1", 7200)
    await loop._reconcile_prs()
    assert bool(health.merge_holds_snapshot()) == bool(threshold)

    store.cards = [] if state is None else [dict(REVIEW[0], board_state=state)]
    await loop._reconcile_prs()
    assert health.merge_holds_snapshot() == []
    assert merge_state_hold.hold_for("bd-1") is None

    store.cards = [dict(REVIEW[0])]
    before = time.time()
    loop._note_merge_state_hold("bd-1", PR, ["github mergeStateStatus=UNSTABLE"], None)
    assert merge_state_hold.hold_for("bd-1")["since"] >= before
    await loop._reconcile_prs()
    assert health.merge_holds_snapshot() == []


async def test_disabling_auto_merge_live_clears_the_hold(monkeypatch):
    loop = BoardLoop({"coder": "proto", "auto_merge": True})
    monkeypatch.setattr(loop, "_store", lambda: _ReviewStore())
    monkeypatch.setattr(loop, "_start_card_work", lambda *a: None)
    _aged_hold("bd-1", 7200)
    await loop._reconcile_prs()
    assert health.merge_holds_snapshot()

    loop.auto_merge = False
    await loop._reconcile_prs()
    assert health.merge_holds_snapshot() == []
    assert merge_state_hold.hold_for("bd-1") is None


@pytest.mark.parametrize("pr_url", ["", "https://github.com/o/r/pull/43"])
async def test_removing_or_replacing_the_pr_clears_its_old_hold(pr_url):
    loop = BoardLoop({"coder": "proto", "auto_merge": True})
    _aged_hold("bd-1", 7200)
    loop._publish_merge_holds([dict(REVIEW[0], pr_url=pr_url)])
    assert health.merge_holds_snapshot() == []
    assert merge_state_hold.hold_for("bd-1") is None


async def test_failed_review_read_preserves_the_hold_clock(monkeypatch):
    loop = BoardLoop({"coder": "proto", "auto_merge": True})
    store = _ReviewStore()
    monkeypatch.setattr(loop, "_store", lambda: store)
    monkeypatch.setattr(loop, "_start_card_work", lambda *a: None)
    _aged_hold("bd-1", 7200)
    await loop._reconcile_prs()
    since = merge_state_hold.hold_for("bd-1")["since"]

    def failed_read(state=None):
        if state == "in_review":
            raise store_mod.BoardError("unavailable")
        return []

    monkeypatch.setattr(store, "list_features", failed_read)
    await loop._reconcile_prs()
    assert merge_state_hold.hold_for("bd-1")["since"] == since
    assert [r["id"] for r in health.merge_holds_snapshot()] == ["bd-1"]
