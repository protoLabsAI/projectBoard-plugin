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

from project_board import health, merge_state_hold, worktree
from project_board.loop import BoardLoop

pytestmark = pytest.mark.asyncio

PR = "https://github.com/o/r/pull/42"


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
    loop = BoardLoop({"coder": "proto"})  # merge_hold_alert_s defaults to 3600
    _aged_hold("bd-1", 7200)

    loop._publish_merge_holds()

    (row,) = health.merge_holds_snapshot()
    assert row["id"] == "bd-1"
    assert row["hold"] == "held: PR not clean on GitHub (UNSTABLE) — QA panel: in progress"
    assert row["status"] == "UNSTABLE"
    assert row["pr_url"] == PR
    assert row["age_s"] >= 3600


async def test_a_hold_under_the_threshold_is_not_listed():
    """r3: a hold younger than the threshold is normal (every fresh PR), so it is not listed."""
    loop = BoardLoop({"coder": "proto"})
    _aged_hold("bd-1", 60)  # a minute old

    loop._publish_merge_holds()

    assert health.merge_holds_snapshot() == []


async def test_the_entry_clears_when_the_hold_clears():
    """r2: a PR that goes CLEAN clears the hold (the merge edge's clear_hold), and the next
    pass drops it from the health entry."""
    loop = BoardLoop({"coder": "proto"})
    _aged_hold("bd-1", 7200)
    loop._publish_merge_holds()
    assert [r["id"] for r in health.merge_holds_snapshot()] == ["bd-1"]

    # The real clean-transition path: mergeStateStatus is no longer a held status.
    loop._note_merge_state_hold("bd-1", PR, ["github mergeStateStatus=CLEAN"], None)
    assert merge_state_hold.hold_for("bd-1") is None

    loop._publish_merge_holds()
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
    loop = BoardLoop({"coder": "proto"})
    _aged_hold("bd-1", 7200)
    loop._publish_merge_holds()
    assert [r["id"] for r in health.merge_holds_snapshot()] == ["bd-1"]

    f = {"id": "bd-1", "pr_url": PR, "board_state": "in_review"}
    await loop._reconcile_pr(_MergeStore(), f, known_state="MERGED")
    assert merge_state_hold.hold_for("bd-1") is None

    loop._publish_merge_holds()
    assert health.merge_holds_snapshot() == []


async def test_the_entry_clears_when_the_pr_closes(monkeypatch):
    """r2: a PR closed unmerged takes the same clear_hold before its triage block/reap."""
    loop = BoardLoop({"coder": "proto"})

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(worktree, "reap_feature_worktree", _noop)
    monkeypatch.setattr(loop, "_budget_reset", _noop)
    _aged_hold("bd-1", 7200, status="BLOCKED", checks=())
    loop._publish_merge_holds()
    assert [r["id"] for r in health.merge_holds_snapshot()] == ["bd-1"]

    f = {"id": "bd-1", "pr_url": PR, "board_state": "in_review"}
    await loop._reconcile_pr(_MergeStore(), f, known_state="CLOSED")
    assert merge_state_hold.hold_for("bd-1") is None

    loop._publish_merge_holds()
    assert health.merge_holds_snapshot() == []


class _EmptyStore:
    def list_features(self, state=None):
        return []


async def test_the_reconcile_pass_publishes_the_holds(monkeypatch):
    """The wiring: _reconcile_prs itself publishes, so the health entry appears without any
    explicit call — it reads the holds the prior pass's reconciles left."""
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop, "_store", lambda: _EmptyStore())
    _aged_hold("bd-1", 7200)

    await loop._reconcile_prs(detach=True)

    assert [r["id"] for r in health.merge_holds_snapshot()] == ["bd-1"]


async def test_zero_turns_the_health_entry_off():
    """merge_hold_alert_s=0 disables the entry entirely (the per-card next_action still shows)."""
    loop = BoardLoop({"coder": "proto", "merge_hold_alert_s": 0})
    _aged_hold("bd-1", 999999)

    loop._publish_merge_holds()

    assert health.merge_holds_snapshot() == []
    assert merge_state_hold.hold_for("bd-1") is not None  # the hold itself is untouched
