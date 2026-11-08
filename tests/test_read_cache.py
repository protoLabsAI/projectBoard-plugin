"""Board listing reads are cached and never queue behind a slow `br` (projectBoard#686 stopgap).

2026-11-07 on protoEngineer: ``/cards`` took 5-19 s for 419 bytes and the view's ``/features``
poll hit the hub's 20 s proxy timeout whenever the drive loop held ``br``.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from project_board import api, store as store_mod
from project_board.read_cache import ReadCache


# ── ReadCache unit ─────────────────────────────────────────────────────────────


def _loader(calls, value, delay=0.0, fail=False):
    async def load():
        calls.append(1)
        if delay:
            await asyncio.sleep(delay)
        if fail:
            raise RuntimeError("br exploded")
        return value

    return load


def test_same_revision_is_served_from_cache():
    async def main():
        c, calls = ReadCache(), []
        assert await c.get("k", "r1", _loader(calls, "A")) == ("A", False)
        assert await c.get("k", "r1", _loader(calls, "B")) == ("A", False)
        assert len(calls) == 1

    asyncio.run(main())


def test_a_moved_revision_refreshes():
    async def main():
        c, calls = ReadCache(), []
        await c.get("k", "r1", _loader(calls, "A"))
        assert await c.get("k", "r2", _loader(calls, "B")) == ("B", False)
        assert len(calls) == 2

    asyncio.run(main())


def test_concurrent_readers_share_one_refresh():
    async def main():
        c, calls = ReadCache(), []
        load = _loader(calls, "A", delay=0.05)
        results = await asyncio.gather(*(c.get("k", "r1", load) for _ in range(20)))
        assert all(r == ("A", False) for r in results)
        assert len(calls) == 1

    asyncio.run(main())


def test_a_slow_refresh_serves_the_last_good_listing_as_stale_and_keeps_going():
    async def main():
        c, calls = ReadCache(fresh_wait_s=0.05), []
        await c.get("k", "r1", _loader(calls, "A"))
        t0 = time.monotonic()
        value, stale = await c.get("k", "r2", _loader(calls, "B", delay=0.5))
        assert (value, stale) == ("A", True)
        assert time.monotonic() - t0 < 0.3  # never waits on the slow read
        await asyncio.sleep(0.6)  # the refresh finished in the background
        assert await c.get("k", "r2", _loader(calls, "C")) == ("B", False)

    asyncio.run(main())


def test_a_failed_refresh_keeps_the_last_good_listing():
    async def main():
        c, calls = ReadCache(), []
        await c.get("k", "r1", _loader(calls, "A"))
        assert await c.get("k", "r2", _loader(calls, None, fail=True)) == ("A", True)

    asyncio.run(main())


def test_the_first_read_waits_and_its_error_propagates():
    async def main():
        c = ReadCache()
        with pytest.raises(RuntimeError):
            await c.get("k", "r1", _loader([], None, fail=True))

    asyncio.run(main())


# ── routes ─────────────────────────────────────────────────────────────────────


class SlowStore:
    """``list_features`` blocks like a contended `br`; counts calls; one write route."""

    def __init__(self, delay=0.0):
        self.delay, self.lock, self.calls, self.title = delay, threading.Lock(), 0, "T1"

    def list_features(self, state=None, include_archived=False):
        with self.lock:
            self.calls += 1
        time.sleep(self.delay)
        return [{"id": "bd-1", "title": self.title, "board_state": "ready", "priority": 2}]

    def get_feature(self, fid):
        return None  # the PATCH below 404s — the router-level write bump has already run


def _client(monkeypatch, s):
    monkeypatch.setattr(api, "get_store", lambda **_kw: s)
    app = FastAPI()
    app.include_router(api.build_data_router({}), prefix="/api/plugins/project_board")
    return TestClient(app)


def test_repeated_reads_of_an_unchanged_board_call_br_once(monkeypatch):
    s = SlowStore()
    c = _client(monkeypatch, s)
    for path in ("/features", "/features", "/cards", "/cards", "/cards/open"):
        assert c.get("/api/plugins/project_board" + path).status_code == 200
    # /features and /cards and /cards/open all read the live (state=None, not archived) listing
    assert s.calls == 1


def test_concurrent_reads_do_not_queue_behind_a_slow_store(monkeypatch):
    s = SlowStore(delay=0.4)
    c = _client(monkeypatch, s)
    assert c.get("/api/plugins/project_board/features").status_code == 200  # warm
    s.calls = 0
    t0 = time.monotonic()
    threads = [threading.Thread(target=lambda: c.get("/api/plugins/project_board/cards")) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # eight reads of an unchanged board: one stat each, no `br` — not 8 × 0.4 s in a queue
    assert time.monotonic() - t0 < 1.5
    assert s.calls == 0


def test_a_write_through_the_api_invalidates_the_listing(monkeypatch):
    s = SlowStore()
    c = _client(monkeypatch, s)
    first = c.get("/api/plugins/project_board/features").json()["features"][0]["title"]
    s.title = "T2"  # the store changed, but the db files a fake store has don't move
    # any mutating request a route matches bumps the generation (before and after the handler)
    c.patch("/api/plugins/project_board/features/bd-unknown", json={"title": "x"})
    second = c.get("/api/plugins/project_board/features").json()["features"][0]["title"]
    assert (first, second) == ("T1", "T2")


def test_cached_rows_are_not_mutated_by_annotation(monkeypatch):
    s = SlowStore()
    c = _client(monkeypatch, s)
    a = c.get("/api/plugins/project_board/features").json()
    b = c.get("/api/plugins/project_board/features").json()
    assert a == b


# ── br history throttle ────────────────────────────────────────────────────────


def test_br_runs_with_a_history_snapshot_interval_by_default(monkeypatch):
    monkeypatch.delenv("BR_HISTORY_MIN_INTERVAL_SECS", raising=False)
    assert store_mod._br_env()["BR_HISTORY_MIN_INTERVAL_SECS"] == "3600"


def test_an_operator_set_history_interval_wins(monkeypatch):
    monkeypatch.setenv("BR_HISTORY_MIN_INTERVAL_SECS", "60")
    assert store_mod._br_env()["BR_HISTORY_MIN_INTERVAL_SECS"] == "60"
