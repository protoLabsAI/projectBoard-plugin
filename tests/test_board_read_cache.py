"""Board reads are cached and never queue behind `br` (protoAgent v0.231.0 stress test).

On protoEngineer's board, ``GET /cards`` took 5-19 s for a 419-byte answer and timed out
with three clients, and six concurrent ``GET /features`` staggered to more than 10 s: every
request ran its own ``list_features`` (``br list`` over the whole history plus ``br show``
batches), each call queued on the process ``br`` lock behind the drive loop. These pin the
fix (read_cache.py): one snapshot, single-flight refresh, write invalidation, and a bounded
wait when the store is busy.

Most tests use a fake ``list_features`` that blocks on an Event to stand in for the
drive loop holding the ``br`` lock. The concurrent-create regression uses a real board.
"""

from __future__ import annotations

import asyncio
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from project_board import api, read_cache, work_snapshot
from project_board.store import BR, BeadsBoard, BoardError, BoardTimeout


class SlowStore:
    """A board whose listing costs ``delay`` seconds, or blocks while ``gate`` is clear."""

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.gate = threading.Event()
        self.gate.set()
        self.rows = [
            {"id": "bd-1", "title": "One", "board_state": "ready", "priority": 2, "project": "a"},
            {"id": "bd-2", "title": "Two", "board_state": "in_progress", "priority": 1, "project": "b"},
            {"id": "bd-3", "title": "Old", "board_state": "done", "priority": 2, "project": "a"},
        ]
        self.list_calls = 0
        self.lock = threading.Lock()
        self.projects = {}
        self.default_project = ""

    def list_features(self, state=None, include_archived=False):
        with self.lock:
            self.list_calls += 1
        if self.delay:
            time.sleep(self.delay)
        assert self.gate.wait(30), "test never released the store"
        rows = [dict(r) for r in self.rows]
        return [r for r in rows if r["board_state"] == state] if state else rows

    def get_feature(self, fid):
        return next((dict(r) for r in self.rows if r["id"] == fid), None)

    def create_feature(self, **k):
        f = {"id": f"bd-{len(self.rows) + 1}", "title": k.get("title", ""), "board_state": "backlog", "priority": 2}
        self.rows.append(f)
        return dict(f)

    def mark_ready(self, fid):
        for r in self.rows:
            if r["id"] == fid:
                r["board_state"] = "ready"
        return self.get_feature(fid)


def _app(monkeypatch, store) -> FastAPI:
    monkeypatch.setattr(api, "get_store", lambda **_kw: store)
    app = FastAPI()
    app.include_router(api.build_data_router({}), prefix="/api/plugins/project_board")
    return app


BASE = "/api/plugins/project_board"


def _ids(body):
    return [f["id"] for f in body["features"]]


def _titles(body):
    return {f["id"]: f["title"] for f in body["features"]}


@pytest.mark.skipif(shutil.which(BR) is None, reason="real br CLI required")
def test_real_board_concurrent_creates_and_default_listings(monkeypatch, tmp_path):
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    client = TestClient(_app(monkeypatch, board))
    assert client.get(BASE + "/features").json()["features"] == []
    original = board.list_features
    entered = threading.Event()
    release = threading.Event()

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(read_cache, "STALE_WAIT_S", 0.01)
    monkeypatch.setattr(board, "list_features", delayed)
    first = board.create_feature("Before the listing")
    with ThreadPoolExecutor(max_workers=2) as pool:
        listing = pool.submit(client.get, BASE + "/features")
        try:
            assert entered.wait(5)
            # A create overlaps the listing refresh on the real board.
            second = pool.submit(board.create_feature, "During the listing").result(timeout=10)
            time.sleep(0.05)
        finally:
            release.set()
        response = listing.result(timeout=10)
        assert first["id"] in _ids(response.json())
        known = {first["id"], second["id"]}
        for index in range(3):
            before = set(known)
            create = pool.submit(board.create_feature, f"Concurrent {index}")
            response = client.get(BASE + "/features")
            assert before <= set(_ids(response.json()))
            known.add(create.result(timeout=10)["id"])


# ── the routes ──────────────────────────────────────────────────────────────────


def test_repeat_reads_of_an_unchanged_board_run_no_listing(monkeypatch):
    store = SlowStore()
    c = TestClient(_app(monkeypatch, store))
    for path in ("/features", "/features", "/cards", "/cards/ready", "/cards/open/a", "/features?state=done"):
        assert c.get(BASE + path).status_code == 200, path
    assert store.list_calls == 1


def test_scoped_reads_from_the_snapshot_match_the_state_filtered_listing(monkeypatch):
    store = SlowStore()
    c = TestClient(_app(monkeypatch, store))
    assert _ids(c.get(BASE + "/features?state=done").json()) == ["bd-3"]
    assert _ids(c.get(BASE + "/features?project=b").json()) == ["bd-2"]
    assert _ids(c.get(BASE + "/cards").json()) == ["bd-2", "bd-1"]  # open work, most urgent first
    assert _ids(c.get(BASE + "/cards/ready").json()) == ["bd-1"]
    assert _ids(c.get(BASE + "/cards/open/a").json()) == ["bd-1"]
    # annotation stamps a per-request copy; the cached rows are never mutated
    assert all(set(r) == {"id", "title", "board_state", "priority", "project"} for r in store.rows)


def test_etag_still_answers_304_and_the_body_is_json(monkeypatch):
    c = TestClient(_app(monkeypatch, SlowStore()))
    r = c.get(BASE + "/features")
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["x-board-snapshot"] == "fresh"
    tag = r.headers["etag"]
    assert c.get(BASE + "/features", headers={"If-None-Match": f"W/{tag}"}).status_code == 304


async def test_concurrent_reads_on_a_cold_board_share_one_listing(monkeypatch):
    store = SlowStore(delay=0.3)
    app = _app(monkeypatch, store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        paths = ["/features", "/cards", "/cards/ready", "/features?project=a"] * 3
        rs = await asyncio.gather(*(c.get(BASE + p) for p in paths))
    assert [r.status_code for r in rs] == [200] * len(paths)
    assert store.list_calls == 1


def test_a_write_through_the_api_is_in_the_next_read(monkeypatch):
    store = SlowStore()
    c = TestClient(_app(monkeypatch, store))
    assert _ids(c.get(BASE + "/features").json()) == ["bd-1", "bd-2", "bd-3"]
    assert c.post(BASE + "/features", json={"title": "New"}).status_code == 200
    assert "bd-4" in _ids(c.get(BASE + "/features").json())
    assert c.post(BASE + "/features/bd-4/ready").status_code == 200
    assert "bd-4" in _ids(c.get(BASE + "/cards/ready").json())


def test_a_write_elsewhere_in_the_process_or_outside_it_is_picked_up(monkeypatch):
    store = SlowStore()
    stamp = {"rev": "r1"}
    monkeypatch.setattr(api, "store_rev", lambda _db: stamp["rev"])
    c = TestClient(_app(monkeypatch, store))
    c.get(BASE + "/features")
    store.rows[0]["title"] = "Renamed by the loop"
    work_snapshot.note_board_write()  # what BeadsBoard._run does after a loop/tool write
    assert _titles(c.get(BASE + "/features").json())["bd-1"] == "Renamed by the loop"
    store.rows[0]["title"] = "Renamed by a hand-run br"
    stamp["rev"] = "r2"  # the store's file stamp moved; no in-process revision did
    assert _titles(c.get(BASE + "/features").json())["bd-1"] == "Renamed by a hand-run br"
    assert store.list_calls == 3


async def test_reads_answer_inside_the_bound_while_the_store_is_busy(monkeypatch):
    """The latency bound: with a snapshot cached and the drive loop holding `br`, reads come
    back within STALE_WAIT_S, marked stale, and the busy store sees ONE listing."""
    monkeypatch.setattr(read_cache, "STALE_WAIT_S", 0.2)
    store = SlowStore()
    app = _app(monkeypatch, store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get(BASE + "/features")).status_code == 200
        store.gate.clear()  # the loop holds the br lock from here on
        work_snapshot.note_board_write()  # …and has just written
        try:
            started = time.monotonic()
            rs = await asyncio.wait_for(
                asyncio.gather(*(c.get(BASE + p) for p in ["/cards", "/features", "/cards/ready"] * 2)), 5
            )
            elapsed = time.monotonic() - started
        finally:
            store.gate.set()
    assert [r.status_code for r in rs] == [200] * 6
    assert {r.headers["x-board-snapshot"] for r in rs} == {"stale"}
    assert elapsed < 2.0, elapsed
    assert store.list_calls == 2  # the priming read + one refresh for all six readers


def test_a_read_after_an_api_write_waits_for_post_write_data(monkeypatch):
    """Never a pre-write answer to the operator who just wrote, even while `br` is busy."""
    monkeypatch.setattr(read_cache, "STALE_WAIT_S", 0.05)
    store = SlowStore()
    c = TestClient(_app(monkeypatch, store))
    c.get(BASE + "/features")
    store.gate.clear()  # `br` is busy from here: the refresh the write starts can't land
    assert c.post(BASE + "/features", json={"title": "New"}).status_code == 200
    got = {}
    t = threading.Thread(target=lambda: got.setdefault("r", c.get(BASE + "/features")))
    try:
        t.start()
        t.join(0.5)
        assert "r" not in got, "a read after an API write was answered from the pre-write snapshot"
    finally:
        store.gate.set()
        t.join(5)
    assert got["r"].headers["x-board-snapshot"] == "fresh"
    assert "bd-4" in _ids(got["r"].json())


def test_an_unusable_board_still_reads_as_json_400(monkeypatch):
    class Broken(SlowStore):
        def list_features(self, state=None, include_archived=False):
            raise BoardError("repo '.' has no beads workspace — set project_board.repo")

    c = TestClient(_app(monkeypatch, Broken()))
    for path in ("/features", "/cards", "/cards/ready"):
        r = c.get(BASE + path)
        assert r.status_code == 400, path
        assert "beads workspace" in r.json()["detail"]


# ── the cache itself ────────────────────────────────────────────────────────────


def _cache(load, token=lambda: 0):
    return read_cache.BoardReadCache(load, token)


def test_cache_single_flights_concurrent_cold_reads():
    calls = []

    def load(archived):
        calls.append(archived)
        time.sleep(0.2)
        return [{"id": "x"}]

    cache = _cache(load)
    out = []
    ts = [threading.Thread(target=lambda: out.append(cache.snapshot())) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    assert len(out) == 8 and calls == [False]
    assert all(s.rows == [{"id": "x"}] and not s.stale for s in out)
    cache.snapshot(include_archived=True)
    assert calls == [False, True]  # the archived listing is its own snapshot


def test_cache_queues_one_refresh_behind_a_running_one():
    """A burst of changes while a listing runs costs one more listing, not one per reader."""
    gate = threading.Event()
    tok = {"v": 0}
    calls = []

    def load(_):
        calls.append(tok["v"])
        gate.wait(5)
        return [tok["v"]]

    cache = _cache(load, lambda: tok["v"])
    first = threading.Thread(target=cache.snapshot)
    first.start()
    while not calls:
        time.sleep(0.01)
    tok["v"] = 1  # the board moved while the first listing was running
    waiters = [threading.Thread(target=cache.snapshot) for _ in range(5)]
    for t in waiters:
        t.start()
    time.sleep(0.1)
    gate.set()
    for t in [first, *waiters]:
        t.join(5)
    assert calls == [0, 1]
    assert cache.snapshot().rows == [1]


def test_cache_serves_the_previous_snapshot_when_the_refresh_stalls(monkeypatch):
    monkeypatch.setattr(read_cache, "STALE_WAIT_S", 0.05)
    tok = {"v": 0}
    mode = {"raise": None}

    def load(_):
        if mode["raise"]:
            raise mode["raise"]
        return [tok["v"]]

    cache = _cache(load, lambda: tok["v"])
    assert cache.snapshot().rows == [0]
    tok["v"] = 1
    mode["raise"] = BoardTimeout("`br list` timed out")
    snap = cache.snapshot()
    assert snap.stale and snap.rows == [0]
    assert cache._entries[False].rows == [0]
    mode["raise"] = BoardError("`br list` failed: no such table")
    with pytest.raises(BoardError, match="no such table"):
        cache.snapshot()  # a real failure is reported, not hidden behind old data
    assert cache._entries[False].rows == [0]
    mode["raise"] = None
    assert cache.snapshot().rows == [1]


@pytest.mark.parametrize("payload", [None, {}])
def test_invalid_store_listing_does_not_replace_nonempty_snapshot(monkeypatch, tmp_path, payload):
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    revision = [0]
    cache = _cache(lambda _: board.list_features(), lambda: revision[0])
    monkeypatch.setattr(board, "list_features", lambda: [{"id": "existing"}])
    assert cache.snapshot().rows == [{"id": "existing"}]
    monkeypatch.setattr(board, "list_features", BeadsBoard.list_features.__get__(board))
    monkeypatch.setattr(board, "_run", lambda *a, **kw: (payload, None))
    revision[0] += 1
    with pytest.raises(BoardError, match="listing"):
        cache.snapshot()
    assert cache._entries[False].rows == [{"id": "existing"}]


@pytest.mark.parametrize("rows", [[], [{"id": "old", "board_state": "done"}]])
def test_snapshot_without_open_work_waits_for_refresh(monkeypatch, rows):
    monkeypatch.setattr(read_cache, "STALE_WAIT_S", 0.001)
    revision = [0]
    entered = threading.Event()
    release = threading.Event()

    def load(_):
        if revision[0]:
            entered.set()
            assert release.wait(5)
            return [{"id": "new", "board_state": "backlog"}]
        return rows

    cache = _cache(load, lambda: revision[0])
    assert cache.snapshot().rows == rows
    revision[0] += 1
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(cache.snapshot)
        try:
            assert entered.wait(5)
            time.sleep(0.03)
            assert not result.done()
        finally:
            release.set()
        assert result.result(timeout=5).rows[0]["id"] == "new"


def test_successful_empty_read_can_replace_previous_snapshot():
    rows = [{"id": "old"}]
    revision = [0]
    cache = _cache(lambda _: list(rows), lambda: revision[0])
    assert cache.snapshot().rows == rows
    rows.clear()
    revision[0] += 1
    snap = cache.snapshot()
    assert snap.rows == [] and not snap.stale


def test_cache_note_write_forbids_the_pre_write_snapshot(monkeypatch):
    monkeypatch.setattr(read_cache, "STALE_WAIT_S", 0.01)
    gate = threading.Event()
    gate.set()
    state = {"rows": ["before"]}

    def load(_):
        gate.wait(5)
        return list(state["rows"])

    cache = _cache(load)
    assert cache.snapshot().rows == ["before"]
    state["rows"] = ["after"]
    gate.clear()
    cache.note_write()
    out = []
    t = threading.Thread(target=lambda: out.append(cache.snapshot()))
    t.start()
    t.join(0.2)
    assert not out
    gate.set()
    t.join(5)
    assert out[0].rows == ["after"] and not out[0].stale


def test_the_board_view_re_reads_after_a_stale_answer():
    """A stale answer must not become the view's baseline: it forgets the /rev stamp, so the
    next 2 s tick reads again instead of waiting for the 15 s full refresh."""
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "view" / "board.js").read_text(encoding="utf-8")
    assert 'res.headers.get("x-board-snapshot") === "stale") REV = null' in js
