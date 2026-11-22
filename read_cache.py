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
  this process: loop, tools, routes), the store's content stamp (:func:`listing_rev`,
  which moves for a writer outside this process such as a hand-run ``br``, and does not
  move when a ``br`` read rewrites the database's header counters), the live project
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
  for the refresh the write started. The board loop's writes are visible within
  :data:`STALE_WAIT_S` or as a snapshot marked stale.

- **The ``board_list`` tool reads the same snapshot** (#805). The router publishes its cache
  under the board's database file (:func:`publish`); the tool looks it up there
  (:func:`published`). A board tool call that wrote to the store bumps the epoch when it
  ends (:func:`note_shared_write`), exactly like an API write, so an agent's own write is
  never answered with a pre-write listing.

The cache stores the store's projected rows, unannotated. ``next_action`` and the other
hints read live loop state and config, so the routes annotate a shallow copy per request
(cheap Python, no ``br``) and the cached rows are never mutated.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
import weakref
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
    # Seconds THIS reader was held up by the listing's queueing for the `br` lock: at most
    # the time it spent blocked here, 0.0 for a read answered from the cache.
    waited_s: float = 0.0

    def age_s(self, now: float | None = None) -> float:
        return max(0.0, (time.monotonic() if now is None else now) - self.taken_at)


@dataclass
class _Entry:
    rows: list
    token: Hashable
    epoch: int
    taken_at: float
    lock_wait_s: float = 0.0  # what the listing itself queued for the `br` lock


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
        blocked = time.monotonic()

        def _fresh(entry: _Entry) -> Snapshot:
            waited = min(time.monotonic() - blocked, entry.lock_wait_s)
            return Snapshot(entry.rows, stale=False, taken_at=entry.taken_at, token=entry.token, waited_s=waited)

        if fallback is None:
            return _fresh(run.future.result())  # first read, or read-after-write: wait for it
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
        return _fresh(entry)

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
            # This thread has its own context, so the reader's `br`-wait counter (#805,
            # store.track_br_wait) does not see the listing's queueing. Count it here and
            # hand it back on the snapshot.
            from .store import track_br_wait

            with track_br_wait() as wait:
                rows = self._load(key)
            entry = _Entry(
                rows=list(rows or []), token=token, epoch=token[1], taken_at=started, lock_wait_s=wait.seconds
            )
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


# ── the cache the routes and the board_list tool share (#805) ─────────────────────────


def store_rev(db: str) -> str:
    """A cheap change stamp for the beads store at ``db``: the size + mtime of the
    database, its WAL and the ``.beads`` JSONL exports. Every ``br`` write lands in one of
    them, so the stamp moves on any write by any process — and reading it is a few
    ``stat`` calls. A missing file contributes ``-``; an unreadable directory is skipped."""
    parts: list[str] = []
    paths = [db, db + "-wal"]
    beads_dir = os.path.dirname(db)
    try:
        paths += sorted(os.path.join(beads_dir, n) for n in os.listdir(beads_dir) if n.endswith(".jsonl"))
    except OSError:
        pass
    for path in paths:
        try:
            st = os.stat(path)
            parts.append(f"{st.st_size}.{st.st_mtime_ns}")
        except OSError:
            parts.append("-")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


_SQLITE_MAGIC = b"SQLite format 3\x00"
_WAL_HEADER = 32
# The listing stamp last computed per database file: (file stamp it was computed at, stamp).
_listing_revs: dict[str, tuple[str, str]] = {}
_listing_revs_lock = threading.Lock()


def _store_content(db: str) -> str | None:
    """A digest of what the store at ``db`` HOLDS, or None when the database cannot be read.

    The database's bytes minus the two SQLite header counters that count transactions
    (the file change counter at offset 24 and version-valid-for at 92), plus any frames
    in the WAL past its header. A `br` that opens a write transaction to read (0.1.x)
    moves those counters, the WAL header's salts and both files' mtimes, and nothing
    else; a real write changes page content or leaves frames in the WAL."""
    digest = hashlib.blake2b(digest_size=16)
    try:
        with open(db, "rb") as fh:
            head = bytearray(fh.read(100))
            if head.startswith(_SQLITE_MAGIC) and len(head) == 100:
                head[24:28] = head[92:96] = b"\x00\x00\x00\x00"
            digest.update(head)
            while chunk := fh.read(1 << 20):
                digest.update(chunk)
    except OSError:
        return None
    try:
        with open(db + "-wal", "rb") as fh:
            fh.seek(_WAL_HEADER)
            while chunk := fh.read(1 << 20):
                digest.update(chunk)
    except OSError:
        pass  # no WAL: nothing waiting to be checkpointed
    return digest.hexdigest()


def listing_rev(db: str, stamp: str | None = None) -> str:
    """The change stamp a cached listing is current against: :func:`store_rev`, made
    proof against a `br` READ (#805).

    `br` 0.1.x rewrites the SQLite header counters on every ``list``/``show``/``ready``,
    so the file stamp moved after each listing and a cached listing was never current:
    every read listed the store again. This stamp is taken from the store's content
    (:func:`_store_content`) and the JSONL exports' file state, so only a write moves it.

    Still cheap: the content is read only when the file stamp (``stamp``, default
    ``store_rev(db)``) differs from the one this was last computed at. With a `br` whose
    reads leave the files alone that is once per write. A database that cannot be read,
    or that changed while it was read, has no content to compare: the file stamp itself
    is the answer."""
    if stamp is None:
        stamp = store_rev(db)
    with _listing_revs_lock:
        known = _listing_revs.get(db)
    if known is not None and known[0] == stamp:
        return known[1]
    content = _store_content(db)
    if content is None:
        return stamp
    parts = [content]
    beads_dir = os.path.dirname(db)
    try:
        names = sorted(n for n in os.listdir(beads_dir) if n.endswith(".jsonl"))
    except OSError:
        names = []
    for name in names:
        try:
            st = os.stat(os.path.join(beads_dir, name))
            parts.append(f"{name}:{st.st_size}.{st.st_mtime_ns}")
        except OSError:
            parts.append(f"{name}:-")
    rev = hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]
    # Something touched the files while they were read (a write in flight): what was read
    # may be half of it, so answer with the file stamp, which is out of date the moment
    # the write ends, and take the content again then.
    if store_rev(db) != stamp:
        return stamp
    with _listing_revs_lock:
        _listing_revs[db] = (stamp, rev)
    return rev


def db_file(db: str | None, repo: str | None) -> str:
    """The board's database file, as the key the shared cache is published under. `br`
    runs in the repo, so a relative ``db`` is the repo's."""
    from .store import default_db_path

    db = db or default_db_path()
    if not os.path.isabs(db):
        db = os.path.join(str(repo or "."), db)
    return db


_shared_lock = threading.Lock()
# The data router's cache, by database file. One board has one router, so one entry.
_published: dict[str, BoardReadCache] = {}
# A board with no published cache (the API did not mount) gets its own, kept as long as
# the board object is.
_own: "weakref.WeakKeyDictionary[object, BoardReadCache]" = weakref.WeakKeyDictionary()


def publish(key: str, cache: BoardReadCache) -> None:
    """Make ``cache`` the one every reader of the board at ``key`` (:func:`db_file`) uses.
    The data router calls this with the cache its routes read; a rebuilt router replaces
    the previous one's."""
    with _shared_lock:
        _published[key] = cache


def published(key: str) -> BoardReadCache | None:
    """The cache the routes read for the board at ``key``, or None when no router is up."""
    with _shared_lock:
        return _published.get(key)


def own_cache(board, key: str) -> BoardReadCache:
    """``board``'s own read cache, for a board whose routes published none: its listing,
    and the routes' change token minus the API's live routing reload."""
    with _shared_lock:
        cache = _own.get(board)
        if cache is None:
            from . import work_snapshot

            def _token():
                routing = repr((getattr(board, "projects", None), getattr(board, "default_project", None)))
                return (work_snapshot.board_revision(), listing_rev(key), routing)

            cache = _own[board] = BoardReadCache(
                lambda include_archived: board.list_features(include_archived=include_archived), _token
            )
        return cache


def note_shared_write() -> None:
    """A board tool call that wrote to the store just ended: no reader of any shared cache
    may be answered with a listing from before it (:meth:`BoardReadCache.note_write`)."""
    with _shared_lock:
        caches = list(_published.values()) + list(_own.values())
    for cache in caches:
        cache.note_write()


def reset_shared() -> None:
    """Forget every shared cache (tests: one test's board must not answer the next's)."""
    with _shared_lock:
        _published.clear()
        _own.clear()
    with _listing_revs_lock:
        _listing_revs.clear()
