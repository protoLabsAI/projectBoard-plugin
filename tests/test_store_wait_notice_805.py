"""A board tool that waited on the busy board store says so in its reply (#805).

Every ``br`` command takes one process-wide lock (``store._br_lock``), so a tool call
made while the loop or the sweep is using the store queues behind it. Before this change
the reply said nothing about that and only the ``br`` run time was ever logged, so a
queued call looked the same as a hung one.

These tests hold the REAL ``_br_lock`` from another thread and run the REAL
``BeadsBoard._run`` path; only the ``br`` subprocess itself is replaced. The notice
threshold is lowered so the suite waits tenths of a second, not seconds.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import threading
import time

import pytest

import project_board as pb
from project_board import store as store_mod

THRESHOLD = 0.2
HOLD = 0.6
WAIT_LINE = re.compile(
    r"\n\nWaited (\d+\.\d) s for the board store \(busy with other board work\); the answer is current\.$"
)


@pytest.fixture
def real_run_board(monkeypatch):
    """A ``BeadsBoard`` whose ``_run`` is the real one (lock, timing, retries, parsing).
    Only the ``br`` process is faked: it records its args and can take ``br_seconds``."""
    monkeypatch.setattr(store_mod.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    monkeypatch.setattr(store_mod, "_BR_WAIT_NOTICE_S", THRESHOLD)
    board = store_mod.BeadsBoard(db=None, repo="/repo", base_branch="main")
    board._workspace_ready = True  # no `br init` — there is no real workspace here
    board.br_seconds = 0.0
    board.br_calls = []

    def _fake_br(cmd, *, cwd, args):
        board.br_calls.append(args)
        if board.br_seconds:
            time.sleep(board.br_seconds)
        return subprocess.CompletedProcess(cmd, 0, "[]", "")

    monkeypatch.setattr(store_mod, "_run_br_process", _fake_br)
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)
    return board


def _tool(name):
    return {t.name: t for t in pb._board_tools({})}[name]


class _HeldLock:
    """Hold the real process-wide ``br`` lock from ANOTHER thread for ``seconds``."""

    def __init__(self, seconds):
        self.seconds = seconds
        self._held = threading.Event()
        self._thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self):
        with store_mod._br_lock():
            self._held.set()
            time.sleep(self.seconds)

    def __enter__(self):
        self._thread.start()
        assert self._held.wait(5), "the holder thread never got the br lock"
        return self

    def __exit__(self, *_exc):
        self._thread.join(5)


def test_tool_reply_says_it_waited_when_the_lock_was_held(real_run_board):
    board_list = _tool("board_list")
    plain = board_list.invoke({})
    assert "Waited" not in plain

    started = time.monotonic()
    with _HeldLock(HOLD):
        reply = board_list.invoke({})
    took = time.monotonic() - started

    match = WAIT_LINE.search(reply)
    assert match, reply
    # The reported time is the wait itself: at least the threshold, never more than the call took.
    assert THRESHOLD <= float(match.group(1)) <= took + 0.05
    assert float(match.group(1)) >= HOLD - 0.15
    # Everything before the line is the reply the tool gives when it does not wait.
    assert reply[: match.start()] == plain
    assert reply.count("Waited") == 1


def test_tool_that_did_not_wait_replies_exactly_as_before(real_run_board):
    board_list = _tool("board_list")
    assert board_list.invoke({}) == "[]"
    assert real_run_board.br_calls, "the call must have gone through the real _run"

    # A wait under the threshold is not worth a line either.
    with _HeldLock(THRESHOLD / 4):
        assert board_list.invoke({}) == "[]"


def test_slow_br_is_not_reported_as_waiting(real_run_board, monkeypatch, caplog):
    """`br` run time is not lock wait: a slow `br` with a free lock adds no line and
    logs the slow-`br` warning only."""
    monkeypatch.setattr(store_mod, "_BR_SLOW_S", THRESHOLD)
    real_run_board.br_seconds = HOLD
    with caplog.at_level(logging.WARNING), store_mod.track_br_wait() as wait:
        reply = _tool("board_list").func()
    assert reply == "[]"
    assert wait.seconds < THRESHOLD
    messages = [r.getMessage() for r in caplog.records]
    assert any("`br list" in m and "took" in m for m in messages)
    assert not any("waited" in m for m in messages)


def test_lock_wait_and_br_run_time_are_measured_separately(real_run_board, monkeypatch, caplog):
    """Lock held for HOLD, then `br` itself runs for HOLD: the counted wait is the first
    part only, and each part gets its own WARNING naming the command."""
    monkeypatch.setattr(store_mod, "_BR_SLOW_S", THRESHOLD)
    real_run_board.br_seconds = HOLD
    with caplog.at_level(logging.WARNING), store_mod.track_br_wait() as wait, _HeldLock(HOLD):
        real_run_board._run("show", "bd-1", want_json=True)

    assert HOLD - 0.15 <= wait.seconds < 2 * HOLD - 0.15  # the lock wait, without `br`'s own HOLD

    waited = [r for r in caplog.records if "waited" in r.getMessage()]
    slow = [r for r in caplog.records if "took" in r.getMessage()]
    assert len(waited) == 1 and len(slow) == 1
    assert waited[0].levelno == logging.WARNING
    assert "`br show bd-1` waited" in waited[0].getMessage()
    assert "board store lock" in waited[0].getMessage()
    logged_wait = float(re.search(r"waited (\d+\.\d)s", waited[0].getMessage()).group(1))
    logged_run = float(re.search(r"took (\d+\.\d)s", slow[0].getMessage()).group(1))
    assert logged_wait < 2 * HOLD - 0.15 and logged_run < 2 * HOLD - 0.15


def test_short_lock_wait_is_not_logged(real_run_board, caplog):
    with caplog.at_level(logging.WARNING), _HeldLock(THRESHOLD / 4):
        real_run_board._run("show", "bd-1", want_json=True)
    assert not any("waited" in r.getMessage() for r in caplog.records)


def test_wait_does_not_leak_into_a_call_on_another_thread(real_run_board, monkeypatch, make_board):
    """While one tool call is queued on the lock, a call on another thread that never
    touches the lock finishes with a clean reply; and the thread that waited does not
    carry its wait into its next call."""
    # `_run` faked, so this board never takes the br lock.
    free_board = make_board(lambda *a, **k: ([], False) if k.get("with_has_more") else [])
    waiter = threading.current_thread()
    monkeypatch.setattr(
        "project_board.store.get_store",
        lambda **_kw: real_run_board if threading.current_thread() is waiter else free_board,
    )
    board_list = _tool("board_list")
    other: dict = {}

    def _other_call():
        other["reply"] = board_list.invoke({})
        other["while_waiter_queued"] = not real_run_board.br_calls

    with _HeldLock(HOLD):
        thread = threading.Thread(target=_other_call)
        thread.start()
        waited_reply = board_list.invoke({})
        thread.join(5)

    assert WAIT_LINE.search(waited_reply)
    assert other["reply"] == "[]"
    assert other["while_waiter_queued"], "the other call was meant to finish during the wait"
    assert board_list.invoke({}) == "[]"  # same thread, next call: nothing carried over


def test_counters_are_per_call_across_threads():
    """Two tracked calls open at the same time on two threads each see only their own wait."""
    both_open = threading.Barrier(2)
    both_noted = threading.Barrier(2)
    seen: dict = {}

    def _call(name, seconds):
        with store_mod.track_br_wait() as wait:
            both_open.wait(5)
            store_mod._note_br_lock_wait(("list",), seconds)
            both_noted.wait(5)
            seen[name] = wait.seconds

    threads = [threading.Thread(target=_call, args=(n, s)) for n, s in (("a", 0.05), ("b", 0.01))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert seen == {"a": 0.05, "b": 0.01}
    assert store_mod._BR_WAIT.get() is None  # nothing left behind outside a tracked call


def test_untracked_callers_are_not_counted():
    """The loop and the sweep run outside any tracked call: noting a wait there is a no-op."""
    store_mod._note_br_lock_wait(("list",), 0.01)
    with store_mod.track_br_wait() as wait:
        pass
    assert wait.seconds == 0.0


def test_work_offloaded_with_to_thread_counts_toward_the_same_call():
    """An async tool offloads its store calls with ``asyncio.to_thread``; the worker
    thread's wait still belongs to that tool call."""

    async def _call():
        with store_mod.track_br_wait() as wait:
            await asyncio.to_thread(store_mod._note_br_lock_wait, ("show", "bd-1"), 0.03)
            await asyncio.to_thread(store_mod._note_br_lock_wait, ("comments", "bd-1"), 0.04)
        return wait.seconds

    assert asyncio.run(_call()) == pytest.approx(0.07)


def test_async_tool_reply_carries_the_line_and_keeps_its_signature(monkeypatch):
    """The wrapper covers async tools too, and leaves what langchain inspects alone."""
    monkeypatch.setattr(store_mod, "_BR_WAIT_NOTICE_S", THRESHOLD)
    from langchain_core.tools import tool

    @tool
    async def board_probe(feature_id: str) -> str:
        """Probe."""
        await asyncio.to_thread(store_mod._note_br_lock_wait, ("show", feature_id), 6.2)
        return "ok"

    @tool
    async def board_quiet(feature_id: str) -> str:
        """Probe that does not wait."""
        return "ok"

    pb._report_store_wait(board_probe)
    pb._report_store_wait(board_quiet)
    assert asyncio.run(board_probe.ainvoke({"feature_id": "bd-1"})) == (
        "ok\n\nWaited 6.2 s for the board store (busy with other board work); the answer is current."
    )
    assert asyncio.run(board_quiet.ainvoke({"feature_id": "bd-1"})) == "ok"
    assert asyncio.iscoroutinefunction(board_probe.coroutine)


def test_an_error_reply_says_the_wait_came_before_the_error(monkeypatch):
    monkeypatch.setattr(store_mod, "_BR_WAIT_NOTICE_S", THRESHOLD)
    assert pb._with_store_wait("Error: board is down", 3.04) == (
        "Error: board is down\n\nWaited 3.0 s for the board store (busy with other board work) "
        "before this error; the wait was a queue, not a hang."
    )
    assert pb._with_store_wait("Error: board is down", THRESHOLD / 2) == "Error: board is down"
    payload = {"not": "a string"}
    assert pb._with_store_wait(payload, 9.0) is payload


def test_every_board_tool_keeps_its_arguments(real_run_board):
    """Wrapping must not change a tool's schema: the wrappers carry the original signature."""
    import inspect

    for t in pb._board_tools({}):
        for fn in (t.func, t.coroutine):
            if fn is not None:
                assert inspect.signature(fn) == inspect.signature(fn.__wrapped__), t.name
