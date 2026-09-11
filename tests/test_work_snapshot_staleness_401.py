"""#401: the agent's working state showed board cards in states they had already left.

Live on protoEngineer, 2026-09-07 07:15–07:17Z (audit log + agent.log):

* 07:15:08 — the sweep published the snapshot, at the START of the sweep: bd-ezs7
  `in_progress`, bd-p8ft `blocked` (terminal).
* 07:16:40 — the PM itself called `board_unblock_feature(bd-p8ft)` → `ready`.
* 07:17:02 — the PM itself called `board_block_feature(bd-ezs7)` → `blocked`.
* 07:17:20 — the PM recorded "bd-p8ft is confirmed board_state=blocked / blocked_class=terminal"
  and planned around it. Its injected working state still said so, and would until the next
  sweep (300s later). It had made the transition itself 40 seconds earlier.

The host reads the provider fresh on every turn (graph/work_providers.py). Nothing is cached
host-side. The staleness is the plugin's: the snapshot was refreshed only on the sweep, it
was taken before the sweep's own transitions, and nothing in it said how old it was. So two
cards that moved in OPPOSITE directions both read back in their old states.

Now every board write this process makes (tool, route or loop, which all go through
`BeadsBoard._run`) bumps a revision. A snapshot read before that revision says it is STALE
instead of passing old states off as current. The loop's refresher republishes soon after,
and the sweep publishes after its own transitions. These drive the real `br` where the seam is
`br`. The review round's findings are in tests/test_work_snapshot_review_401.py.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys

import pytest

from project_board import store as store_mod
from project_board import work_snapshot, worktree
from project_board.loop import BoardLoop
from project_board.store import BeadsBoard

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


def _cards(items) -> dict[str, str]:
    return {i["id"]: i["state"] for i in items if isinstance(i, dict)}


def _status_line(items) -> str:
    return items[0] if items and isinstance(items[0], str) else ""


def _ready(board: BeadsBoard, repo, title: str, path: str) -> str:
    (repo / path).write_text("x = 1\n")
    fid = board.create_feature(title, spec="s", acceptance_criteria=_AC, files_to_modify=[path])["id"]
    board.mark_ready(fid)
    return fid


@requires_br
async def test_the_agents_own_transitions_never_read_back_as_the_old_state(tmp_path, monkeypatch):
    """The incident through real `br`: publish the board the way the sweep does, have the
    PM's own tool edges move two cards in opposite directions, and read the working state
    the PM's next turn would get."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    ezs7 = _ready(board, tmp_path, "make poll timeout a no-progress bound", "adapters.py")
    p8ft = _ready(board, tmp_path, "record continuity entries", "conversations.py")
    board.flag_blocked(p8ft, "open_review expects in_progress, got 'ready'", category="terminal")

    work_snapshot.publish(board.list_features())  # exactly what the sweep publishes
    assert _cards(work_snapshot.provider()) == {ezs7: "ready", p8ft: "blocked"}
    assert not _status_line(work_snapshot.provider())  # current → just the cards

    # What board_unblock_feature / board_block_feature call, 40 seconds apart.
    board.clear_blocked(p8ft)
    board.flag_blocked(ezs7, "zombie drive: no process, no branch, no PR", category="terminal")

    shown = work_snapshot.provider()
    assert _status_line(shown).startswith("STALE"), (
        f"the working state presented pre-transition states as current: {shown}"
    )
    assert "board_list" in _status_line(shown)  # and says where the live record is

    # The loop's refresher brings it current: the cards as they ARE, no STALE line.
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)
    loop = BoardLoop({})
    assert loop._work_snapshot_due()
    await loop._publish_work_snapshot()
    shown = work_snapshot.provider()
    assert not _status_line(shown)
    assert _cards(shown) == {ezs7: "blocked", p8ft: "ready"}


def test_a_write_that_lands_while_the_board_is_read_leaves_the_snapshot_stale():
    """The ordering the refresh relies on: the revision is read BEFORE the board. A write
    that lands mid-read may or may not be in the rows, so the snapshot must not claim it."""
    revision = work_snapshot.board_revision()  # the loop reads the revision first…
    work_snapshot.note_board_write()  # …a tool writes while the board is being listed…
    work_snapshot.publish([{"id": "bd-1", "board_state": "ready", "title": "t"}], revision=revision)

    assert _status_line(work_snapshot.provider()).startswith("STALE")
    assert work_snapshot.needs_refresh()


class _Board:
    """A fake store whose writes move real (in-memory) state, so the snapshot can be
    checked against what the board actually says after the sweep."""

    def __init__(self, rows: dict[str, dict]):
        self.rows = rows

    def list_features(self, state=None):
        rows = [dict(r, id=fid) for fid, r in self.rows.items()]
        return [r for r in rows if state is None or r["board_state"] == state]

    def live_cards(self):
        return [r for r in self.list_features() if r["board_state"] not in ("done", "cancelled")]

    def get_feature(self, fid):
        return dict(self.rows[fid], id=fid) if fid in self.rows else None

    def requeue(self, fid):
        self.rows[fid]["board_state"] = "ready"

    def archive_stale(self, archive_after_days=7):
        return []


def _no_repo_side_effects(monkeypatch):
    async def _no_pr(branch, *, cwd="."):
        return ""

    monkeypatch.setattr(worktree, "pr_url_for_branch", _no_pr)
    monkeypatch.setattr(worktree, "list_feature_worktrees", lambda repo, root: [])


async def test_the_sweep_publishes_after_its_own_transitions(monkeypatch):
    """The sweep resets an orphaned in_progress card (no live drive, no PR) to ready. The
    snapshot was taken BEFORE that, so the working state showed the card still being built
    for a whole sweep interval."""
    board = _Board({"bd-orphan": {"title": "orphaned build", "board_state": "in_progress"}})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)
    _no_repo_side_effects(monkeypatch)

    await BoardLoop({})._sweep()

    assert board.rows["bd-orphan"]["board_state"] == "ready"  # the sweep really moved it
    assert _cards(work_snapshot.provider()) == {"bd-orphan": "ready"}


@pytest.fixture
def stalling_br(tmp_path, monkeypatch):
    """The real `br`, until told to stall, the way #404's stalls looked. ``PB_STALL=<verb>``
    hangs that verb before it runs, and ``PB_STALL_ONCE=<verb>`` only its first call;
    ``PB_STALL_AFTER=<verb>`` RUNS it (its write commits) and then hangs. The timeout drops to 1s and the TERM grace to 0.3s, so a stall costs a second
    of #431's 45, and the store stops the child and raises ``BoardTimeout`` as it would live."""
    real = shutil.which(store_mod.BR)
    once = tmp_path / "stalled-once"
    script = tmp_path / "br-stalling"
    script.write_text(
        f"""#!/bin/sh
if [ -n "$PB_STALL" ] && [ "$1" = "$PB_STALL" ]; then exec sleep 60; fi
if [ -n "$PB_STALL_AFTER" ] && [ "$1" = "$PB_STALL_AFTER" ]; then "{real}" "$@" >/dev/null 2>&1; exec sleep 60; fi
if [ -n "$PB_STALL_ONCE" ] && [ "$1" = "$PB_STALL_ONCE" ] && [ ! -e "{once}" ]; then touch "{once}"; exec sleep 60; fi
exec "{real}" "$@"
"""
    )
    script.chmod(0o755)
    monkeypatch.setattr(store_mod, "BR", str(script))
    monkeypatch.setattr(store_mod, "_BR_TIMEOUT_S", 1.0)
    monkeypatch.setattr(store_mod, "_BR_TERM_GRACE_S", 0.3)
    monkeypatch.delenv("PB_STALL", raising=False)
    monkeypatch.delenv("PB_STALL_AFTER", raising=False)
    monkeypatch.delenv("PB_STALL_ONCE", raising=False)


@requires_br
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell wrapper")
async def test_a_write_that_stalls_after_landing_still_marks_the_snapshot_stale(tmp_path, monkeypatch, stalling_br):
    """A `br` write can commit and then hang (#404). The store stops it and raises
    ``BoardTimeout``, and the card HAS moved. The stale mark rides a `finally` in `_run`, so
    the working state says it may be out of date instead of showing the card as it was. A
    read that stalls moves nothing and marks nothing."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    fid = _ready(board, tmp_path, "card the PM blocks", "a.py")
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)
    loop = BoardLoop({})
    assert await loop._publish_work_snapshot()
    assert _cards(work_snapshot.provider()) == {fid: "ready"} and not _status_line(work_snapshot.provider())

    before = work_snapshot.board_revision()
    monkeypatch.setenv("PB_STALL", "list")
    with pytest.raises(store_mod.BoardTimeout):
        board.live_cards()
    assert work_snapshot.board_revision() == before  # a stalled read changes nothing
    monkeypatch.delenv("PB_STALL")

    monkeypatch.setenv("PB_STALL_AFTER", "update")
    with pytest.raises(store_mod.BoardTimeout):
        board.flag_blocked(fid, "zombie drive: no process, no branch, no PR", category="terminal")
    monkeypatch.delenv("PB_STALL_AFTER")

    assert board.get_feature(fid)["board_state"] == "blocked"  # the write landed before the stall
    assert _status_line(work_snapshot.provider()).startswith("STALE"), "a stalled write read back as current"
    assert await loop._publish_work_snapshot()
    assert _cards(work_snapshot.provider()) == {fid: "blocked"} and not _status_line(work_snapshot.provider())


# ── with #431's stall handling (#404): the first stall ends the tick ────────────────────


class _StalledSnapshot(_Board):
    """A board every sweep pass reads fine, until the sweep's snapshot read stalls."""

    def live_cards(self):
        raise store_mod.BoardTimeout("`br list …` timed out after 45s and was stopped — the board store did not answer")


async def test_a_stall_in_the_sweeps_snapshot_read_ends_the_tick(monkeypatch, caplog):
    """#431: a stalled store ends the tick at its first stall, because every later phase
    would stall on it too, one timeout each. The sweep's own snapshot read swallowed its
    stall as one more refresh failure, and the tick went on to the preflight and the claim
    scan. It is the sweep's stall now: logged by the tick as one, and counted by the
    refresher's backoff."""
    board = _StalledSnapshot({"bd-1": {"title": "ready card", "board_state": "ready"}})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)
    _no_repo_side_effects(monkeypatch)
    loop = BoardLoop({"merge_poll": False, "health_sweep_interval_s": 0.001})
    later: list[str] = []

    async def _preflight():
        later.append("preflight")

    async def _claim_scan():
        later.append("claim scan")
        return False

    monkeypatch.setattr(loop, "_maybe_preflight", _preflight)
    monkeypatch.setattr(loop, "_spawn_ready", _claim_scan)

    with caplog.at_level(logging.WARNING, logger=_LOG):
        assert await loop._tick() is False

    assert later == [], f"the tick went on past a stalled store: {later}"
    [stall] = [r for r in caplog.records if "stalled on the board store" in r.message]
    assert "health sweep" in stall.message and not any(r.exc_info for r in caplog.records)
    assert loop._snapshot_failures == 1


@requires_br
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell wrapper")
async def test_a_stalled_refresher_read_does_not_cost_the_claim_scan(tmp_path, monkeypatch, stalling_br, caplog):
    """The refresher runs beside the ticks, as `start()` runs it, so its read can be the one
    that stalls. That stall is the refresher's alone: one line, no traceback, a backoff, and
    the refresher keeps running. The next tick still claims the card, and once the store
    answers the snapshot comes back current. (#431's own end-to-end case, a stalled PR
    reconcile read, is tests/test_br_timeout_404.py::test_after_a_stall_the_next_tick_claims.)"""
    from project_board.loop import reconcile as reconcile_mod

    board = BeadsBoard(repo=str(tmp_path), actor="test")
    fid = _ready(board, tmp_path, "the card to claim", "target.py")
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)
    monkeypatch.setattr(reconcile_mod, "_SNAPSHOT_POLL_S", 0.01)
    monkeypatch.setattr(work_snapshot, "MIN_INTERVAL_S", 0.0)
    loop = BoardLoop(
        {
            "coder": "proto",
            "repo": str(tmp_path),
            "loop_enabled": True,
            "merge_poll": True,
            "merge_poll_interval_s": 0,
            "health_sweep_interval_s": 0,
            "preflight": False,
            "max_pending_reviews": 0,
        }
    )
    dispatched: list[str] = []

    async def _drive(feature):
        dispatched.append(feature["id"])

    monkeypatch.setattr(loop, "_drive", _drive)
    monkeypatch.setenv("PB_STALL_ONCE", "list")

    with caplog.at_level(logging.WARNING, logger=_LOG):
        refresher = asyncio.create_task(loop._keep_work_snapshot_current())
        try:
            for _ in range(500):  # the refresher reads first, and eats the one stall
                if loop._snapshot_failures:
                    break
                await asyncio.sleep(0.01)
            assert loop._snapshot_failures == 1 and not refresher.done()
            claimed = await loop._tick()
            await asyncio.gather(*loop._drives)
            for _ in range(500):
                if work_snapshot.taken_at() is not None and not work_snapshot.needs_refresh():
                    break
                await asyncio.sleep(0.01)
            shown = work_snapshot.provider()
        finally:
            loop._stop.set()
            await refresher

    assert claimed is True and dispatched == [fid]
    assert not _status_line(shown) and _cards(shown) == {fid: "in_progress"}
    [stall] = [r for r in caplog.records if "timed out after 1s" in r.message]
    assert "work snapshot refresh failed" in stall.message
    assert not any(r.exc_info for r in caplog.records)
