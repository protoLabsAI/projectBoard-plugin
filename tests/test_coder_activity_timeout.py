"""A coder still streaming when its budget runs out is extended, up to a ceiling; one that has
gone silent is killed on time."""

import asyncio

import pytest

from project_board import coder_seam, worktree


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(worktree, "CODER_EXTEND_SLICE_S", 0.05)
    monkeypatch.setattr(worktree, "CODER_IDLE_GRACE_S", 10.0)


async def test_active_coder_is_extended_until_it_finishes(fast):
    async def work():
        await asyncio.sleep(0.15)
        return "built"

    assert await worktree.coder_bound(work(), 0.1, idle_s=lambda: 1.0) == "built"


async def test_active_coder_still_stops_at_the_ceiling(fast):
    cancelled = asyncio.Event()

    async def work():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(asyncio.TimeoutError):
        await worktree.coder_bound(work(), 0.1, idle_s=lambda: 1.0)
    assert 0.18 <= loop.time() - start < 1.0  # 2 × the budget
    assert cancelled.is_set()


@pytest.mark.parametrize("idle", [10.0, None])
async def test_silent_or_unknown_coder_is_killed_at_the_budget(fast, idle):
    async def work():
        await asyncio.sleep(10)

    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(asyncio.TimeoutError):
        await worktree.coder_bound(work(), 0.1, idle_s=lambda: idle)
    assert loop.time() - start < 0.18


async def test_coder_error_propagates_unchanged(fast):
    async def work():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await worktree.coder_bound(work(), 0.1, idle_s=lambda: 0.0)


def test_ceiling_is_twice_the_budget_and_unbounded_stays_unbounded():
    assert worktree.coder_ceiling(1800) == 3600
    assert worktree.coder_ceiling(None) is None


def test_timeout_message_keeps_the_parsed_prefix_and_names_an_extension(monkeypatch):
    coder_seam.progress_begin("bd-t", 1)
    buf = coder_seam._buf("bd-t", 1)
    assert coder_seam._timeout_message("bd-t", 1, 1800) == "coder timed out after 1800s"
    monkeypatch.setattr(coder_seam, "_monotonic", lambda: buf.started + 2400)
    msg = coder_seam._timeout_message("bd-t", 1, 1800)
    assert msg.startswith("coder timed out after 1800s") and "extended to 2400s" in msg
    assert coder_seam._idle_probe("bd-t", 1)() == pytest.approx(2400, abs=1)
    assert coder_seam._idle_probe("bd-none", 1)() is None
