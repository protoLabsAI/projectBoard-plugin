"""A revision-keyed, single-flight cache for the board's listing reads (projectBoard#686, stopgap).

Every ``/features`` and ``/cards*`` read used to shell ``br`` over the whole board history and
queue behind the drive loop's ``br`` calls: under load a 419-byte ``/cards`` took 5-19 s and the
board view's ``/features`` poll hit the hub's 20 s proxy timeout, so the view errored whenever the
board was busy (2026-11-07). The listing only changes when the store does, and ``store_rev`` (a
few ``stat`` calls) says when that is, so:

- **Same revision** → the cached listing is served at once.
- **Revision moved** → ONE refresh runs (concurrent readers share it). A reader waits for it up to
  ``fresh_wait_s``; past that it gets the last good listing marked ``stale=True`` and the refresh
  carries on in the background, so a slow ``br`` never turns into a timed-out request.
- **No listing yet** → the reader waits for the first refresh (there is nothing to serve).
- **A refresh fails** → readers keep the last good listing (``stale=True``); with none, the
  error propagates (the route's existing BoardError → 400/503 mapping applies).

The revision is captured BEFORE the read, so a write that lands during a refresh moves the stamp
and the next reader refreshes again — a listing is never filed under a newer revision than the
data it holds.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Hashable

log = logging.getLogger("protoagent.plugins.project_board")


@dataclass
class _Entry:
    rev: str | None = None
    value: Any = None
    has_value: bool = False
    at: float = 0.0
    task: asyncio.Task | None = None
    task_rev: str | None = None
    errors: int = 0


@dataclass
class ReadCache:
    """``get(key, rev, load)`` → ``(value, stale)``. ``load`` is an async thunk doing the real read."""

    fresh_wait_s: float = 2.0
    _entries: dict[Hashable, _Entry] = field(default_factory=dict)

    def _entry(self, key: Hashable) -> _Entry:
        e = self._entries.get(key)
        if e is None:
            e = self._entries[key] = _Entry()
        return e

    def _start(self, key: Hashable, e: _Entry, rev: str, load: Callable[[], Awaitable[Any]]) -> asyncio.Task:
        if e.task is not None and not e.task.done() and e.task_rev == rev:
            return e.task  # single-flight: a refresh for this revision is already running

        async def _refresh():
            value = await load()
            e.value, e.rev, e.has_value, e.at, e.errors = value, rev, True, time.monotonic(), 0
            return value

        task = asyncio.ensure_future(_refresh())
        e.task, e.task_rev = task, rev

        def _done(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                e.errors += 1
                log.warning(
                    "[project_board] board read refresh for %s failed (%s); serving the last good listing", key, exc
                )

        task.add_done_callback(_done)
        return task

    async def get(self, key: Hashable, rev: str, load: Callable[[], Awaitable[Any]]) -> tuple[Any, bool]:
        e = self._entry(key)
        if e.has_value and e.rev == rev:
            return e.value, False
        task = self._start(key, e, rev, load)
        if not e.has_value:
            return await asyncio.shield(task), False  # nothing to serve yet: wait (errors propagate)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=self.fresh_wait_s), False
        except asyncio.TimeoutError:
            return e.value, True  # slow refresh: last good listing now, refresh continues
        except Exception:  # noqa: BLE001 — refresh failed: keep serving the last good listing
            return e.value, True
