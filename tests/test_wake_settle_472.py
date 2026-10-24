"""Wake settling holds both claim entry points without pausing maintenance (#472)."""

import logging
from unittest.mock import AsyncMock, Mock

import pytest

from project_board.loop import BoardLoop


class Clocks:
    wall = 1000.0
    mono = 100.0

    def advance(self, wall=30, mono=30):
        self.wall += wall
        self.mono += mono


def make_loop(monkeypatch, **cfg):
    clocks = Clocks()
    loop = BoardLoop(
        {"loop_enabled": True, **cfg},
        wall_clock=lambda: clocks.wall,
        monotonic_clock=lambda: clocks.mono,
    )
    for name in ("_maybe_reconcile", "_maybe_preflight"):
        monkeypatch.setattr(loop, name, AsyncMock())

    async def sweep():
        loop._last_sweep += 1

    monkeypatch.setattr(loop, "_maybe_sweep", AsyncMock(side_effect=sweep))
    monkeypatch.setattr(loop, "_start_location_sweep", Mock())

    async def claim():
        loop._last_claim_decision = {"selected": ["bd-ready"]}
        return True

    monkeypatch.setattr(loop, "_spawn_ready", AsyncMock(side_effect=claim))
    return loop, clocks


async def test_sleep_holds_two_ticks_and_dispatch_then_resumes(monkeypatch, caplog):
    loop, clocks = make_loop(monkeypatch)
    assert await loop._tick() is True
    clocks.advance(wall=3630)
    with caplog.at_level(logging.INFO):
        for _ in range(2):
            assert await loop._tick() is False
            for _ in range(2):
                record = await loop.dispatch_now()
                assert record["outcome"] == "settling-after-wake"
                assert record["dispatched"] == []
                assert "held" in record["detail"]
            assert loop._spawn_ready.await_count == 1
            assert loop._claim_stall_reason(now=100000) == ""
            clocks.advance()
    assert len([r for r in caplog.records if "settling-after-wake" in r.message]) == 1
    assert await loop._tick() is True
    assert loop._spawn_ready.await_count == 2
    await loop.dispatch_now()
    assert loop._spawn_ready.await_count == 3


@pytest.mark.parametrize("gap", [0, 60, -3600])
async def test_matching_gaps_threshold_and_backward_wall_jump_do_not_hold(monkeypatch, gap):
    loop, clocks = make_loop(monkeypatch)
    for _ in range(3):
        assert await loop._tick() is True
        clocks.advance(wall=300 + gap, mono=300)
    assert loop._spawn_ready.await_count == 3


async def test_non_claim_phases_continue_during_settle(monkeypatch):
    loop, clocks = make_loop(monkeypatch)
    await loop._tick()
    clocks.advance(wall=3630)
    for _ in range(2):
        await loop._tick()
        clocks.advance()
    assert loop._maybe_reconcile.await_count == 3
    assert loop._maybe_sweep.await_count == 3
    assert loop._maybe_preflight.await_count == 3
    assert loop._start_location_sweep.call_count == 3
    assert loop._spawn_ready.await_count == 1


@pytest.mark.parametrize("ticks", [0, 1, 3])
async def test_configured_window_and_repeated_sleep(monkeypatch, ticks):
    loop, clocks = make_loop(monkeypatch, wake_settle_gap_s=10, wake_settle_ticks=ticks)
    await loop._tick()
    for _ in range(2):
        clocks.advance(wall=41)
        assert await loop._tick() is (ticks == 0)
    for _ in range(max(0, ticks - 1)):
        clocks.advance()
        assert await loop._tick() is False
    clocks.advance()
    assert await loop._tick() is True
