"""#794: the health sweep never holds the claim scan.

On 2026-11-18 one sweep held the claim scan twice, 303 s and 528 s, with ready cards and
free slots. The log was silent for the whole of it, so nobody could tell which of the
sweep's steps was slow. Its bounded GitHub calls had simply added up, inline in the tick.

Now the sweep is its own single-flight task. The tick waits ``sweep_tick_budget_s`` for it
and then goes on to the preflight and the claim scan. Every step is timed: a slow one logs
its name and seconds, and the claim-stall signal names the step in flight.
"""

from __future__ import annotations

import asyncio
import logging
import time
import types

from project_board import store as store_mod
from project_board.loop import BoardLoop
from project_board.loop import reconcile as reconcile_mod

LOGGER = "protoagent.plugins.project_board"


class _Store:
    """One in_progress card with no drive (the sweep's orphan) and one ready card to claim."""

    def __init__(self, orphans=("bd-x",)):
        self.orphans = list(orphans)
        self.claimed: list[str] = []

    def list_features(self, state=None, **_kw):
        if state == "in_progress":
            return [{"id": fid, "board_state": "in_progress", "labels": []} for fid in self.orphans]
        return []

    def get_feature(self, fid):
        return {"id": fid, "board_state": "in_progress", "labels": []}

    def live_cards(self):
        return []

    def archive_stale(self, _days, *, keep_done=0):
        return []

    def ready_queue(self, relaxed=False):
        return [] if self.claimed else [{"id": "bd-ready", "board_state": "ready", "files_to_modify": []}]

    def claim(self, fid, assignee=""):
        self.claimed.append(fid)
        return {"id": fid, "board_state": "in_progress"}


def _loop(monkeypatch, store, **cfg):
    """A loop whose tick is real — real ``_maybe_sweep``, real ``_sweep``, real claim scan.
    Only the sweep's repo and network steps are stubbed, and the drive a claim starts."""
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop(
        {
            "coder": "proto",
            "loop_enabled": True,
            "loop_interval_s": 30,
            "claim_stall_ticks": 4,
            "merge_poll": False,
            "max_pending_reviews": 0,
            "max_concurrent": 2,
            "health_sweep_interval_s": 300,
            **cfg,
        }
    )
    loop.sweep_tick_budget_s = 0.05
    drove: list[str] = []
    location_sweeps: list[bool] = []

    async def _drive(feature):
        drove.append(feature["id"])

    async def _nothing(*_a, **_kw):
        return None

    monkeypatch.setattr(loop, "_drive", _drive)
    monkeypatch.setattr(loop, "_maybe_preflight", _nothing)
    monkeypatch.setattr(loop, "_sweep_worktrees", _nothing)
    monkeypatch.setattr(loop, "_publish_orphaned_cards", _nothing)
    monkeypatch.setattr(loop, "_recover_blocked", _nothing)
    monkeypatch.setattr(loop, "_start_base_refresh", lambda: None)
    monkeypatch.setattr(loop, "_start_location_sweep", lambda: location_sweeps.append(True))
    return loop, drove, location_sweeps


async def _end(loop):
    """Stop whatever the test left running."""
    tasks = [t for t in (loop._sweep_task, *loop._drives) if t is not None]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _hang(loop, monkeypatch, step="_reconcile_orphan"):
    """Make one sweep step wait on an event nobody sets — a GitHub call that never answers."""
    started, release = asyncio.Event(), asyncio.Event()

    async def _hung(*_a, **_kw):
        started.set()
        await release.wait()

    monkeypatch.setattr(loop, step, _hung)
    return started, release


# ── r1 / r2: the claim happens while the slow step is still running ──────────────────


async def test_a_slow_sweep_step_does_not_hold_the_claim_scan(monkeypatch):
    store = _Store()
    loop, drove, _ = _loop(monkeypatch, store)
    started, _release = _hang(loop, monkeypatch)
    try:
        t0 = time.monotonic()
        assert await asyncio.wait_for(loop._tick(), timeout=5) is True  # ONE tick claims
        await asyncio.sleep(0)  # let the claimed drive start

        assert started.is_set() and not loop._sweep_task.done(), "the slow step is still running"
        assert store.claimed == ["bd-ready"] and drove == ["bd-ready"]
        assert loop._last_claim_at is not None and loop._last_claim_at >= t0
        assert loop._last_claim_decision["selected"] == ["bd-ready"]
        assert loop._tick_phase_now is None  # the tick is over; only the sweep goes on
        assert loop._sweep_step_now[0] == "orphan reconcile bd-x"
    finally:
        await _end(loop)


async def test_the_claim_stall_reason_names_the_sweep_step_in_flight(monkeypatch):
    loop, _drove, _ = _loop(monkeypatch, _Store())
    _hang(loop, monkeypatch)
    try:
        await asyncio.wait_for(loop._tick(), timeout=5)
        await asyncio.sleep(0.01)  # the drive stub ends and frees its slot
        now = time.monotonic()
        loop._ready_count = 7
        loop._last_claim_at = now - 528  # forced past the threshold

        reason = loop._claim_stall_reason(now=now + 40)
        assert "no claim decision for 568s" in reason and "7 ready card(s)" in reason
        assert "the health sweep is in its orphan reconcile bd-x step for 40s" in reason

        # A tick stuck in ANOTHER phase keeps its phase, and the sweep's step follows it.
        loop._tick_phase_now = ("gate preflight", now)
        reason = loop._claim_stall_reason(now=now + 40)
        assert "gate preflight phase for 40s; the health sweep is in its orphan reconcile bd-x step" in reason
    finally:
        loop._tick_phase_now = None
        await _end(loop)


def test_the_claim_stall_reason_is_unchanged_with_no_sweep_running():
    loop = BoardLoop({"loop_enabled": True, "loop_interval_s": 30, "claim_stall_ticks": 4})
    now = time.monotonic()
    loop._ready_count, loop._last_claim_at = 1, now - 600
    assert loop._claim_stall_reason(now=now).endswith("the tick is not reaching its claim scan")
    loop._tick_phase_now = ("health sweep", now - 275)
    assert loop._claim_stall_reason(now=now).endswith("the tick has been in its health sweep phase for 275s")


async def test_a_blocked_card_is_named_while_its_recovery_runs(monkeypatch):
    """The blocked pass walks many cards inside one step, so the step is renamed per card."""
    loop = BoardLoop({})
    seen: list[str] = []

    async def _blocked_pass():
        for fid in ("bd-a", "bd-b"):
            loop._sweep_step_enter(f"blocked recovery {fid}")
            seen.append(loop._sweep_step_now[0])

    await loop._sweep_step("blocked recovery", _blocked_pass())
    assert seen == ["blocked recovery bd-a", "blocked recovery bd-b"] and loop._sweep_step_now is None
    loop._sweep_step_enter("blocked recovery bd-c")  # outside a sweep step: nothing is recorded
    assert loop._sweep_step_now is None


# ── r3: a slow step is logged by name with its seconds; a fast one is silent ─────────


async def test_a_slow_step_logs_its_name_and_seconds_and_a_fast_step_logs_nothing(monkeypatch, caplog):
    loop = BoardLoop({})
    clock = {"t": 1000.0}
    monkeypatch.setattr(reconcile_mod, "time", types.SimpleNamespace(monotonic=lambda: clock["t"]))

    async def _takes(seconds, result=None):
        clock["t"] += seconds
        return result

    async def _raises_after(seconds):
        clock["t"] += seconds
        raise RuntimeError("gh pr merge timed out")

    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert await loop._sweep_step("archive", _takes(4, "fast")) == "fast"
        assert await loop._sweep_step("worktree reap /repos/app", _takes(121, "slow")) == "slow"
        try:  # a step that fails after a long wait says how long it waited, too
            await loop._sweep_step("orphan reconcile bd-x", _raises_after(120))
        except RuntimeError:
            pass

    assert [r.message for r in caplog.records if "sweep step" in r.message] == [
        "[project_board] sweep step worktree reap /repos/app took 121s",
        "[project_board] sweep step orphan reconcile bd-x took 120s",
    ]
    assert all(r.levelno == logging.INFO for r in caplog.records if "sweep step" in r.message)
    assert loop._sweep_step_now is None


async def test_a_real_sweep_names_each_step_it_runs(monkeypatch):
    """Every awaited step of ``_sweep`` goes through the helper, per card and per repo."""
    loop, _drove, _ = _loop(monkeypatch, _Store())
    names: list[str] = []
    real = loop._sweep_step

    async def _record(name, step):
        names.append(name)
        return await real(name, step)

    async def _reconciled(_fid):
        return None

    monkeypatch.setattr(loop, "_sweep_step", _record)
    monkeypatch.setattr(loop, "_reconcile_orphan", _reconciled)
    monkeypatch.setattr(loop, "_all_repos", lambda: ["/repos/app", "/repos/site"])
    await loop._sweep()
    assert names == [
        "in_progress read",
        "orphan reconcile bd-x",
        "worktree reap /repos/app",
        "worktree reap /repos/site",
        "done read",
        "orphaned cards publish",
        "blocked recovery",
        "archive",
        "work snapshot publish",
    ]


# ── r4: failures stay isolated, the cadence holds, and shutdown leaves nothing running ─


async def test_a_step_that_raises_inside_the_tick_is_this_phases_failure_and_the_claim_still_runs(monkeypatch, caplog):
    store = _Store(orphans=())
    loop, drove, location_sweeps = _loop(monkeypatch, store)
    sweeps: list[int] = []

    async def _bug(_store):
        sweeps.append(1)
        raise KeyError("id")

    monkeypatch.setattr(loop, "_publish_orphaned_cards", _bug)
    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert await loop._tick() is True
        [error] = [r for r in caplog.records if "loop tick:" in r.message]
        assert "health sweep failed" in error.message and error.exc_info is not None
        assert store.claimed == ["bd-ready"] and location_sweeps == [True]
        assert loop._sweep_task.done() and loop._sweep_step_now is None

        await loop._maybe_sweep()  # not due: the cadence clock was set when it started
        assert len(sweeps) == 1
        loop._last_sweep -= loop.sweep_interval  # the interval passes
        try:
            await loop._maybe_sweep()
        except KeyError:
            pass
        assert len(sweeps) == 2
    finally:
        await _end(loop)


async def test_a_store_stall_after_the_tick_moved_on_ends_that_sweep_and_the_next_tick(monkeypatch, caplog):
    store = _Store()
    loop, _drove, location_sweeps = _loop(monkeypatch, store)
    started, release = asyncio.Event(), asyncio.Event()
    sweeps: list[int] = []

    async def _stalls(_fid):
        sweeps.append(1)
        started.set()
        await release.wait()
        if len(sweeps) == 1:  # the store answers again for the next sweep
            raise store_mod.BoardTimeout("`br show bd-x` timed out after 45s and was stopped")

    monkeypatch.setattr(loop, "_reconcile_orphan", _stalls)
    try:
        with caplog.at_level(logging.INFO, logger=LOGGER):
            await asyncio.wait_for(loop._tick(), timeout=5)
            assert store.claimed == ["bd-ready"] and not loop._sweep_task.done()
            [moved_on] = [r for r in caplog.records if "health sweep is still running" in r.message]
            assert "orphan reconcile bd-x step" in moved_on.message

            await loop._maybe_sweep()  # single-flight: a due call starts no second sweep
            loop._last_sweep -= loop.sweep_interval
            await loop._maybe_sweep()
            assert len(sweeps) == 1

            release.set()
            await asyncio.gather(loop._sweep_task, return_exceptions=True)
            await asyncio.sleep(0)  # the done-callback
        [stall] = [r for r in caplog.records if "health sweep stalled on the board store" in r.message]
        assert "timed out after 45s" in stall.message and stall.exc_info is None
        assert location_sweeps == [] and loop._sweep_step_now is None

        # The next tick ends on the stall, as a card reconcile's stall ends it (#404)...
        store.claimed.clear()
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert await loop._tick() is False
        assert store.claimed == [] and len(sweeps) == 1
        [ended] = [r for r in caplog.records if "loop tick:" in r.message]
        assert "health sweep stalled on the board store" in ended.message
        # ...and the one after starts the next sweep, on its normal cadence.
        assert await asyncio.wait_for(loop._tick(), timeout=5) is True
        assert len(sweeps) == 2
    finally:
        release.set()
        await _end(loop)


async def test_a_crash_after_the_tick_moved_on_is_logged_and_the_next_sweep_runs(monkeypatch, caplog):
    loop, _drove, location_sweeps = _loop(monkeypatch, _Store())
    started, release = asyncio.Event(), asyncio.Event()
    outcomes = [RuntimeError("gh exploded"), None]
    sweeps: list[int] = []

    async def _slow(_fid):
        sweeps.append(1)
        started.set()
        await release.wait()
        outcome = outcomes[len(sweeps) - 1]
        if outcome is not None:
            raise outcome

    # The orphan reconcile's own failure is the card's; a crash in the blocked pass is the sweep's.
    monkeypatch.setattr(loop, "_recover_blocked", _slow)
    monkeypatch.setattr(loop, "_reconcile_orphan", lambda _fid: asyncio.sleep(0))
    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await asyncio.wait_for(loop._tick(), timeout=5)
            assert loop._sweep_step_now[0] == "blocked recovery"
            release.set()
            await asyncio.gather(loop._sweep_task, return_exceptions=True)
            await asyncio.sleep(0)
        [crash] = [r for r in caplog.records if "health sweep failed" in r.message]
        assert crash.exc_info is not None and "normal cadence" in crash.message
        assert location_sweeps == [True]  # the work-location sweep still follows a failed sweep
        assert loop._sweep_stall is None

        release.clear()
        loop._last_sweep -= loop.sweep_interval
        await asyncio.wait_for(loop._tick(), timeout=5)  # a clean sweep that also outlives its tick
        release.set()
        await asyncio.gather(loop._sweep_task)
        await asyncio.sleep(0)
        assert len(sweeps) == 2 and location_sweeps == [True, True]
    finally:
        release.set()
        await _end(loop)


async def test_shutdown_leaves_no_running_sweep(monkeypatch):
    loop, _drove, _ = _loop(monkeypatch, _Store())
    started, _release = _hang(loop, monkeypatch)
    await asyncio.wait_for(loop._tick(), timeout=5)
    task = loop._sweep_task
    assert started.is_set() and not task.done()

    await asyncio.wait_for(loop.stop(), timeout=5)

    assert task.cancelled() and loop._sweep_step_now is None
    await loop._maybe_sweep()  # a stopped loop starts no sweep
    assert loop._sweep_task is task
