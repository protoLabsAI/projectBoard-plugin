"""Review findings on the working-state snapshot fix (#401, PR #433).

1. A failed refresh logged a traceback on every attempt and retried with no backoff.
2. The refresh was a full-board read (`br list` of every status + a `br show` of every id +
   `br ready`), the ~350-id call #404 caught stalling, run after every write.
3. Key wiring was untested: removing the refresh call, reading the revision after the board,
   or cutting the write set to {"update"} all passed the suite.
4. Nothing bounded staleness except in-process writes: a cross-process write was never seen,
   and with `health_sweep_interval_s: 0` the snapshot never refreshed.
5. The refresh rode the claim tick, so a loop paused at its setup gate stayed STALE. A live
   config reload that changes what the hints say didn't mark it stale. Blocked cards, ranked
   first, had blank hints.

Plus: `block_from_review` stamped no class, so an old one could resurface; the snapshot's
counter lived in module globals a reload could split; and boot released an escalation block
as an "orphaned preflight hold" when the card had once been preflight-held.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import shutil
import subprocess

import pytest

from project_board import store as store_mod
from project_board import work_snapshot
from project_board.loop import BoardLoop
from project_board.loop import reconcile as reconcile_mod
from project_board.store import BeadsBoard, BoardError

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH — CI installs it and sets PB_REQUIRE_BR=1",
)

_AC = "- WHEN x THE SYSTEM SHALL y"
_LOG = "protoagent.plugins.project_board"


@pytest.fixture(autouse=True)
def _fresh_snapshot():
    work_snapshot.reset()
    yield
    work_snapshot.reset()


def _ready(board: BeadsBoard, repo, title: str, path: str) -> str:
    (repo / path).write_text("x = 1\n")
    fid = board.create_feature(title, spec="s", acceptance_criteria=_AC, files_to_modify=[path])["id"]
    board.mark_ready(fid)
    return fid


def _stale(items) -> bool:
    return bool(items) and isinstance(items[0], str) and items[0].startswith("STALE")


class _Cards:
    """A store serving `live_cards`, counting the reads."""

    def __init__(self, rows=None, *, fail: Exception | None = None):
        self.rows = rows if rows is not None else [{"id": "bd-1", "title": "t", "board_state": "ready"}]
        self.reads = 0
        self.fail = fail

    def live_cards(self):
        self.reads += 1
        if self.fail is not None:
            raise self.fail
        return [dict(r) for r in self.rows]


# ── 1: a failed refresh is one line, and backs off ─────────────────────────────────────


@pytest.mark.parametrize(
    "failure",
    [
        store_mod.BoardTimeout("`br list …` timed out after 45s and was stopped — the board store did not answer"),
        BoardError("`br list` failed: DATABASE_ERROR: database is locked"),
    ],
    ids=["stalled", "failed"],
)
async def test_a_failed_refresh_logs_one_line_and_backs_off(monkeypatch, caplog, failure):
    """A stalled `br list` used to spew a traceback on every attempt, and the refresh was
    retried with no backoff (26 reads in 0.3s). Now a BoardError, #431's BoardTimeout (a
    stall, stopped) included, is one line, like `_tick_phase`, and the refresher waits out
    an exponential backoff before the next read. Outside a tick a stall ends nothing."""
    store = _Cards(fail=failure)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(reconcile_mod, "_SNAPSHOT_POLL_S", 0.01)
    loop = BoardLoop({})

    with caplog.at_level(logging.WARNING, logger=_LOG):
        refresher = asyncio.create_task(loop._keep_work_snapshot_current())
        await asyncio.sleep(0.3)
        loop._stop.set()
        await refresher

    assert store.reads == 1, f"a failing store was read {store.reads} times in 0.3s"
    failures = [r for r in caplog.records if "work snapshot refresh failed" in r.message]
    assert len(failures) == 1 and failures[0].exc_info is None, "a BoardError must be one line, no traceback"
    assert loop._snapshot_failures == 1 and not loop._work_snapshot_due()


# ── 2: the snapshot's read is the light one ────────────────────────────────────────────


@requires_br
def test_the_snapshot_reads_only_the_open_cards_and_shows_only_the_blocked_ones(tmp_path, monkeypatch):
    """The snapshot needs id, title, state, labels, pr_url, issue_type and assignee, all on
    `br list` rows. It used to take the full-board read: every status, a `br show` of EVERY
    id, and a `br ready` scan. Now it takes one `br list` of the open statuses, plus a batched
    `br show` of only the cards whose hint needs more: here, the blocked one, for its reason."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    ready = _ready(board, tmp_path, "ready one", "a.py")
    stuck = _ready(board, tmp_path, "stuck one", "b.py")
    done = _ready(board, tmp_path, "done one", "c.py")
    board.flag_blocked(stuck, "open_review expects in_progress, got 'ready'", category="terminal")
    board.mark_done(board.claim(done)["id"], reason="shipped by hand")
    calls = []
    real = board._shell_br

    def _record(*args, **kwargs):
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(board, "_shell_br", _record)
    rows = {f["id"]: f for f in board.live_cards()}

    assert calls == ["list", "show"], calls  # no whole-board show, no `br ready`
    assert set(rows) == {ready, stuck}  # the closed card is not read at all
    assert rows[stuck]["blocked_reason"] == "open_review expects in_progress, got 'ready'"


@requires_br
async def test_the_refreshers_snapshot_names_a_stranded_card_and_a_cancelled_dependency(tmp_path, monkeypatch):
    """#406 (merged after this PR opened) put a backlog card whose dependencies have all
    closed in the working state, with the verb that moves it, off the full-board read. The
    light read has no dependency edges (`br list` omits them), so it lost that card, and it
    has no closed cards, so a dependency that was CANCELLED read as delivered: the hint told
    the agent to promote work whose premise had been cut. The light read now shows the
    backlog cards that have dependencies, and fetches the closed ones only when a card could
    be stranded. A backlog card with no dependencies costs no `br show`."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    merged = board.create_feature("Record the origin session", spec="s")["id"]
    cut = board.create_feature("Old cleanup path", spec="s")["id"]
    stranded = board.create_feature("Wire cleanup through DELETE", spec="s", depends_on=[merged])["id"]
    premise_cut = board.create_feature("Extend the old cleanup path", spec="s", depends_on=[cut])["id"]
    loose = board.create_feature("Ordinary backlog", spec="s")["id"]
    board._run("close", merged, "-r", "merged: https://github.com/o/r/pull/1")
    board.cancel_feature(cut, "scope cut")
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)
    shown_ids: list[str] = []
    real = board._shell_br

    def _record(*args, **kwargs):
        if args[0] == "show":
            shown_ids.extend(a for a in args[1:] if not a.startswith("-"))
        return real(*args, **kwargs)

    monkeypatch.setattr(board, "_shell_br", _record)

    assert await BoardLoop({})._publish_work_snapshot()  # the refresher's path, not the sweep

    items = {i["id"]: i for i in work_snapshot.provider() if isinstance(i, dict)}
    assert items[stranded]["state"] == "backlog" and f"board_mark_ready({stranded})" in items[stranded]["hint"]
    assert "CANCELLED" not in items[stranded]["hint"]
    assert f"{cut} was CANCELLED, not merged" in items[premise_cut]["hint"]
    assert loose not in items and cut not in items  # nothing owed; a cancelled card is never shown
    assert loose not in shown_ids, "a backlog card with no dependencies was shown"


def test_the_refresh_does_not_take_the_full_board_read():
    class _Store(_Cards):
        def list_features(self, *a, **k):
            raise AssertionError("the snapshot took the full-board read")

    store = _Store()
    BoardLoop({})._take_work_snapshot(store)
    assert store.reads == 1 and [c["id"] for c in work_snapshot.provider()] == ["bd-1"]


# ── 3: the wiring, pinned against the mutations review found survived ──────────────────


async def test_the_refresher_runs_while_the_loop_is_paused_and_picks_up_a_write(monkeypatch):
    """Mutation killed: removing the refresher from `start()`. And #5a: the refresh used to
    ride the claim tick, and a loop paused at its setup gate runs none."""
    store = _Cards()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(reconcile_mod, "_SNAPSHOT_POLL_S", 0.01)
    monkeypatch.setattr(work_snapshot, "MIN_INTERVAL_S", 0.0)
    loop = BoardLoop({"loop_enabled": True})

    async def _paused():  # the setup gate: no ticks at all
        await asyncio.Event().wait()

    monkeypatch.setattr(loop, "_run", _paused)
    loop.start()
    try:
        for _ in range(100):
            if work_snapshot.taken_at() is not None:
                break
            await asyncio.sleep(0.01)
        store.rows = [{"id": "bd-2", "title": "moved", "board_state": "blocked"}]
        work_snapshot.note_board_write()
        for _ in range(100):
            if not work_snapshot.needs_refresh():
                break
            await asyncio.sleep(0.01)
        shown = work_snapshot.provider()
    finally:
        await loop.stop()

    assert not _stale(shown) and [c["id"] for c in shown] == ["bd-2"], shown


async def test_one_snapshot_read_at_a_time(monkeypatch):
    """The refresher and the sweep can both reach `_publish_work_snapshot`. A second read
    while one is in flight is skipped, not queued behind it on the single-flight `br` lock:
    the one in flight publishes, and a write since it began leaves that snapshot STALE for
    the refresher to re-read."""
    import threading

    release = threading.Event()

    class _Slow(_Cards):
        def live_cards(self):
            assert release.wait(5), "the first read was never released"
            return super().live_cards()

    store = _Slow()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({})

    first = asyncio.create_task(loop._publish_work_snapshot())
    for _ in range(100):
        if loop._snapshot_reading:
            break
        await asyncio.sleep(0.01)
    second = await loop._publish_work_snapshot()
    release.set()

    assert second is False and await first is True
    assert store.reads == 1 and [c["id"] for c in work_snapshot.provider()] == ["bd-1"]


def test_a_write_during_the_read_leaves_the_snapshot_stale():
    """Mutation killed: reading the revision AFTER the board. A write that lands mid-read
    may or may not be in the rows, so the snapshot must not claim it."""

    class _Racing(_Cards):
        def live_cards(self):
            rows = super().live_cards()
            work_snapshot.note_board_write()  # a tool writes while the board is being read
            return rows

    BoardLoop({})._take_work_snapshot(_Racing())
    assert work_snapshot.needs_refresh() and _stale(work_snapshot.provider())


def _stubbed_board(tmp_path, monkeypatch, *, stdout: str) -> BeadsBoard:
    """A board whose `br` process (#431's ``_run_br_process``, the one place a `br` runs)
    answers ``stdout`` at once: these pin which SUBCOMMANDS mark the snapshot, not `br`."""
    monkeypatch.setattr(store_mod.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    board = BeadsBoard(db=None, repo=str(tmp_path))
    monkeypatch.setattr(board, "_ensure_workspace", lambda: None)
    monkeypatch.setattr(
        store_mod,
        "_run_br_process",
        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=""),
    )
    return board


@pytest.mark.parametrize("subcommand", ["create", "update", "close", "reopen", "delete"])
def test_every_write_that_can_move_a_card_marks_the_snapshot_stale(tmp_path, monkeypatch, subcommand):
    """Mutation killed: cutting `_PROJECTION_WRITES` to {"update"}. A card is created, closed,
    reopened and deleted by subcommands other than `update`."""
    board = _stubbed_board(tmp_path, monkeypatch, stdout="bd-1\n")
    before = work_snapshot.board_revision()
    board._run(subcommand, "bd-1")
    assert work_snapshot.board_revision() > before


def test_reads_and_comments_do_not_mark_the_snapshot_stale(tmp_path, monkeypatch):
    board = _stubbed_board(tmp_path, monkeypatch, stdout="[]")
    before = work_snapshot.board_revision()
    for args in (("list",), ("show", "bd-1"), ("ready",), ("comments", "add", "bd-1", "x"), ("dep", "add", "a", "b")):
        board._run(*args)
    assert work_snapshot.board_revision() == before


# ── 4: staleness is bounded whatever this process writes ───────────────────────────────


async def test_a_quiet_board_is_still_re_read_on_the_age_bound(monkeypatch):
    """A writer outside this process (a hand-run `br`) never bumps the revision, and with
    `health_sweep_interval_s: 0` nothing re-read the board at all. The refresher re-reads a
    quiet board every MAX_AGE_S."""
    store = _Cards()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(reconcile_mod, "_SNAPSHOT_POLL_S", 0.01)
    monkeypatch.setattr(work_snapshot, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(work_snapshot, "MAX_AGE_S", 0.1)
    loop = BoardLoop({"health_sweep_interval_s": 0})

    refresher = asyncio.create_task(loop._keep_work_snapshot_current())
    await asyncio.sleep(0.45)
    loop._stop.set()
    await refresher

    assert store.reads >= 3, f"a quiet board was read {store.reads} time(s) in 0.45s with MAX_AGE_S=0.1"


def test_a_snapshot_the_refresher_stopped_renewing_says_it_is_stale(monkeypatch):
    work_snapshot.publish([{"id": "bd-1", "title": "t", "board_state": "ready"}])
    assert not _stale(work_snapshot.provider())
    monkeypatch.setattr(work_snapshot, "STALE_AFTER_S", -1.0)  # older than the bound
    assert _stale(work_snapshot.provider())


# ── 5b / 5c: what the snapshot renders ─────────────────────────────────────────────────


def test_a_live_config_change_the_hints_read_marks_the_snapshot_stale():
    """`auto_merge` decides whether an in_review card's hint says "merge #N" or "auto-merge
    pending". A live reload changed it without a board write, and the snapshot kept the old
    hint as current."""
    loop = BoardLoop({"auto_merge": False})
    loop._take_work_snapshot(_Cards())
    assert not work_snapshot.needs_refresh()

    assert loop.reload({"auto_merge": True}) == {"auto_merge": (False, True)}

    assert work_snapshot.needs_refresh() and _stale(work_snapshot.provider())


def test_a_blocked_card_carries_its_reason_and_what_moves_it():
    """Blocked cards are ranked first, and they are the cards an agent most needs to act
    on. Their hints were blank."""
    rows = [
        {
            "id": "bd-t",
            "title": "t",
            "board_state": "blocked",
            "blocked": True,
            "blocked_class": "terminal",
            "blocked_reason": "zombie drive",
        },
        {
            "id": "bd-s",
            "title": "s",
            "board_state": "blocked",
            "blocked": True,
            "blocked_class": "transient",
            "blocked_reason": "coder timed out",
        },
    ]
    BoardLoop({})._take_work_snapshot(_Cards(rows))
    hints = {c["id"]: c["hint"] for c in work_snapshot.provider()}
    assert hints == {
        "bd-t": "needs a human (terminal): zombie drive",
        "bd-s": "retries on its own (transient): coder timed out",
    }


# ── the small items ────────────────────────────────────────────────────────────────────


@requires_br
async def test_an_escalation_block_stamps_its_own_class_over_a_stale_one(tmp_path):
    """(i) `block_from_review` added the flag and no class, so a stale `transient` left on
    the card read as this block's class, and the sweep auto-healed a card whose automated
    fixes were spent, when it should have paged a human."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    fid = _ready(board, tmp_path, "escalated", "e.py")
    board.claim(fid)
    board.open_review(fid, pr_url="https://github.com/o/r/pull/1")
    board._run("update", fid, "--add-label", "blocked-class:transient")  # left by a pre-fix unblock

    f = board.block_from_review(fid, "ci-fail: tests red at the top tier")

    assert f["blocked"] and f["blocked_class"] == "terminal"
    assert [label for label in f["labels"] if label.startswith("blocked-class:")] == ["blocked-class:terminal"]
    assert f["blocked_reason"] == "escalation exhausted: ci-fail: tests red at the top tier"
    notified = []
    loop = BoardLoop({})
    loop._notify_operator = lambda fid_, text, **_kw: notified.append(fid_)
    await loop._recover_blocked(board)
    assert notified == [fid] and board.get_feature(fid)["blocked"]


def test_the_counter_survives_a_module_reload():
    """(ii) The revision lived in module globals. A plugin reload re-imports the module, and
    the store (bumping) and the provider (reading) could end up on different counters."""
    work_snapshot.note_board_write()
    before = work_snapshot.board_revision()
    importlib.reload(work_snapshot)
    assert work_snapshot.board_revision() == before


@requires_br
def test_boot_does_not_release_an_escalation_block_as_a_preflight_hold(tmp_path):
    """(iv) The card was once held by a red preflight. Later its CI escalation was exhausted
    and `block_from_review` blocked it. That wrote `escalation exhausted:`, not `blocked:`,
    so the latest `blocked:` reason was still the old preflight hold, and a restart released
    the card as an orphaned hold."""
    from project_board.loop import PREFLIGHT_BLOCK_PREFIX

    board = BeadsBoard(repo=str(tmp_path), actor="test")
    fid = _ready(board, tmp_path, "once held", "h.py")
    board.flag_blocked(fid, f"{PREFLIGHT_BLOCK_PREFIX} — the coder environment can't run the gate: tsc missing")
    board.clear_blocked(fid)
    board.claim(fid)
    board.open_review(fid, pr_url="https://github.com/o/r/pull/2")
    board.block_from_review(fid, "ci-fail: red at the top tier")

    BoardLoop({})._recover_preflight_holds(board)

    assert board.get_feature(fid)["blocked"], "an escalation block was released at boot as a preflight hold"
