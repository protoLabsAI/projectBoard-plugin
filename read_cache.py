"""One cached board listing that every board read endpoint serves from.

Why this exists (protoAgent v0.231.0 stress test, 2026-11-07): on a busy PM board,
``GET /cards`` took 5-19 s for a 419-byte answer and timed out with three viewers;
six concurrent ``GET /features`` staggered to more than 10 s. Every request ran its own
``list_features`` — one ``br list`` over the whole board history plus a ``br show`` per
100 rows — and each of those ``br`` calls queued for the process-wide ``br`` lock behind
the drive loop. The ``/features`` ETag saved bytes on the wire, never the work.

Now the routes read a :class:`BoardReadCache`:

- **One snapshot per listing kind** (live board, or with archived rows). A read whose
  snapshot is current costs a few ``stat`` calls and no ``br`` at all.
- **Current means the change token still matches.** The token is the board's in-process
  write revision (``work_snapshot.board_revision``, bumped by every projection write in
  this process: loop, tools, routes), the store's file stamp (``api.store_rev``, which
  moves for a writer outside this process such as a hand-run ``br``), the live project
  routing, and this cache's own API-write epoch (:meth:`BoardReadCache.note_write`).
- **Refreshes are single-flight.** However many readers find the snapshot out of date,
  one listing runs, on its own thread; the others join it. A refresh requested while one
  is running is queued once and starts when it finishes, so a burst of writes costs at
  most two listings.
- **A reader never queues behind the drive loop's ``br`` lock.** When a snapshot exists
  and the refresh does not land within :data:`STALE_WAIT_S`, the reader gets the previous
  snapshot marked stale (the route says so in ``X-Board-Snapshot``) and the refresh keeps
  running for the next reader. Only the very first read of a board, with nothing cached
  yet, waits for its listing.
- **An API write is never answered with pre-write data.** A write route bumps the epoch
  after it returns (the router's route class does this for every non-GET route), and a
  snapshot taken before the newest API write is never served as stale: the next read waits
  for the refresh the write started. The board loop's and tools' writes are visible
  within :data:`STALE_WAIT_S` or as a snapshot marked stale.

The cache stores the store's projected rows, unannotated. ``next_action`` and the other
hints read live loop state and config, so the routes annotate a shallow copy per request
(cheap Python, no ``br``) and the cached rows are never mutated.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Callable, Hashable

log = logging.getLogger("protoagent.plugins.project_board")

# How long a reader waits for an in-flight refresh before it is answered from the previous
# snapshot (marked stale). Long enough that an uncontended refresh lands inside it, short
# enough that a viewer never feels the drive loop's `br` traffic. Read at call time, so a
# test can shorten it.
STALE_WAIT_S = 1.5


@dataclass(frozen=True)
class Snapshot:
    """What a read is answered from. ``rows`` is shared: copy a row before changing it."""

    rows: list
    stale: bool
    taken_at: float  # time.monotonic() when the listing that produced it started
    token: Hashable = None

    def age_s(self, now: float | None = None) -> float:
        return max(0.0, (time.monotonic() if now is None else now) - self.taken_at)


@dataclass
class _Entry:
    rows: list
    token: Hashable
    epoch: int
    taken_at: float


@dataclass
class _Run:
    future: Future = field(default_factory=Future)
    # The change token this listing started at. None until the refresh thread reads it,
    # which happens AFTER any reader that joined the run observed the board, so a joiner
    # always gets data at least as new as what it saw.
    token: Hashable = None


class BoardReadCache:
    """Single-flight, write-invalidated cache over ``load(include_archived)``.

    ``load`` is the store listing (it blocks on ``br``). ``token`` returns the board's
    current change token (it must be cheap: a counter and a few ``stat`` calls). Both run
    on threads, never on the event loop.
    """

    def __init__(self, load: Callable[[bool], list], token: Callable[[], Hashable]):
        self._load = load
        self._token = token
        self._lock = threading.Lock()
        self._entries: dict[bool, _Entry] = {}
        self._runs: dict[bool, _Run] = {}  # the listing in flight, per kind
        self._queued: dict[bool, _Run] = {}  # the one listing to run after it
        self._epoch = 0

    # ── reads ─────────────────────────────────────────────────────────────────
    def snapshot(self, include_archived: bool = False) -> Snapshot:
        """The listing to answer a read from. Blocking (call it off the event loop), but
        bounded by :data:`STALE_WAIT_S` whenever a usable snapshot already exists."""
        key = bool(include_archived)
        token = (self._token(), self._epoch_now())
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.token == token:
                return Snapshot(entry.rows, stale=False, taken_at=entry.taken_at, token=entry.token)
            run = self._join_or_start(key, token)
            # A snapshot from before the newest API write is never served: the caller (or
            # whoever it is showing the board to) just changed it.
            fallback = entry if entry is not None and entry.epoch == self._epoch else None
        if fallback is None:
            entry = run.future.result()  # first read, or read-after-write: wait for it
            return Snapshot(entry.rows, stale=False, taken_at=entry.taken_at, token=entry.token)
        try:
            entry = run.future.result(timeout=STALE_WAIT_S)
        except FutureTimeout:
            return Snapshot(fallback.rows, stale=True, taken_at=fallback.taken_at, token=fallback.token)
        except Exception as exc:  # noqa: BLE001 — a stalled `br` must not fail a read that has data
            from .store import BoardTimeout

            if not isinstance(exc, BoardTimeout):
                raise
            log.warning("[project_board] board refresh stalled; serving the previous snapshot: %s", exc)
            return Snapshot(fallback.rows, stale=True, taken_at=fallback.taken_at, token=fallback.token)
        return Snapshot(entry.rows, stale=False, taken_at=entry.taken_at, token=entry.token)

    # ── writes ────────────────────────────────────────────────────────────────
    def note_write(self) -> None:
        """An API write just finished (or failed, and may have landed). Every snapshot is
        now out of date and none may be served as stale; start the refreshes now so the
        next read finds them under way. Never blocks."""
        with self._lock:
            self._epoch += 1
            for key in list(self._entries):
                self._join_or_start(key, None)

    # ── internals ─────────────────────────────────────────────────────────────
    def _epoch_now(self) -> int:
        with self._lock:
            return self._epoch

    def _join_or_start(self, key: bool, token) -> _Run:
        """The run a reader that observed ``token`` should wait on. Caller holds the lock.
        ``token`` None means "anything that starts from now on" (a write)."""
        run = self._runs.get(key)
        if run is None:
            run = _Run()
            self._start(key, run)
            return run
        if token is not None and (run.token is None or run.token == token):
            return run  # it started at (or will read) the state this reader saw
        queued = self._queued.get(key)
        if queued is None:
            queued = self._queued[key] = _Run()
        return queued

    def _start(self, key: bool, run: _Run) -> None:
        self._runs[key] = run
        run.future.set_running_or_notify_cancel()
        threading.Thread(target=self._refresh, args=(key, run), name="board-read-refresh", daemon=True).start()

    def _refresh(self, key: bool, run: _Run) -> None:
        entry = exc = None
        try:
            token = (self._token(), self._epoch_now())
            with self._lock:
                run.token = token
            started = time.monotonic()
            rows = self._load(key)
            entry = _Entry(rows=list(rows or []), token=token, epoch=token[1], taken_at=started)
        except BaseException as e:  # noqa: BLE001 — handed to every waiter
            exc = e
        with self._lock:
            if entry is not None:
                self._entries[key] = entry
            queued = self._queued.pop(key, None)
            if queued is not None:
                self._start(key, queued)
            else:
                self._runs.pop(key, None)
        if entry is not None:
            run.future.set_result(entry)
        else:
            run.future.set_exception(exc)
