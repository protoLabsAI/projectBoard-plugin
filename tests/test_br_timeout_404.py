"""A stalled `br` must cost one read, not the loop tick (#404).

Live incident (protoEngineer, 2026-09-07): four `loop tick failed` tracebacks in ten
minutes, every one a `subprocess.TimeoutExpired` out of `store._run` — a `br list`, or
the `br show` that `list_features` made with ~350 ids in one call — raised by the PR
reconcile, the FIRST phase of the tick. `TimeoutExpired` is no kind of BoardError, so it
went straight past every handler to the tick's catch-all, and the sweep, the preflight
and the claim scan behind the reconcile never ran. A `br list` measured 0.058s straight
afterwards: the store was contended, not slow.

The seam is an external process, so the stall here is a REAL one — a `br` wrapper that
execs the real binary until told to hang on one verb, when it `exec`s a `sleep` that
outlives the timeout — never a mocked `_run` raising on cue.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from types import SimpleNamespace

import pytest

import project_board.loop as loop_mod
from project_board import store as store_mod
from project_board.loop import BoardLoop
from project_board.store import BeadsBoard, BoardError

pytestmark = [
    pytest.mark.skipif(
        shutil.which(store_mod.BR) is None,
        reason="real `br` (beads) CLI not on PATH (CI installs it and sets PB_REQUIRE_BR=1)",
    ),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell wrapper"),
]

LOGGER = "protoagent.plugins.project_board"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def stallable_br(tmp_path, monkeypatch):
    """The real `br`, until ``stall(verb)`` — then every call of that verb hangs (a real
    process, blocked in `sleep`) and records its pid so a test can check it was killed.
    Every invocation's argv is logged, read back per verb by ``lines(verb)``. The timeout
    drops to 1s so a stall costs a second, not thirty; `raising=False` keeps the fixture
    usable against a store without the knob (the red-check run on origin/main), and the
    first test below fails if the knob is dead, because its message would then say 30s."""
    real = shutil.which(store_mod.BR)
    calls = tmp_path / "br-calls.log"
    pidfile = tmp_path / "stalled.pid"
    script = tmp_path / "br-stallable"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{calls}"\n'
        'if [ -n "$PB_STALL_VERB" ] && [ "$1" = "$PB_STALL_VERB" ]; then\n'
        f'  echo $$ > "{pidfile}"\n'
        "  exec sleep 60\n"
        "fi\n"
        f'exec "{real}" "$@"\n'
    )
    script.chmod(0o755)
    monkeypatch.setattr(store_mod, "BR", str(script))
    monkeypatch.setattr(store_mod, "_BR_TIMEOUT_S", 1.0, raising=False)
    monkeypatch.delenv("PB_STALL_VERB", raising=False)

    def lines(verb: str) -> list[list[str]]:
        text = calls.read_text() if calls.exists() else ""
        return [line.split() for line in text.splitlines() if line.split()[:1] == [verb]]

    return SimpleNamespace(
        stall=lambda verb: monkeypatch.setenv("PB_STALL_VERB", verb),
        reset=lambda: calls.write_text(""),
        lines=lines,
        pidfile=pidfile,
    )


@pytest.fixture
def board(tmp_path, stallable_br):
    return BeadsBoard(repo=str(tmp_path), actor="test")


def _show_ids(argv: list[str]) -> list[str]:
    """The ids one logged `br show …` call carried (everything before the global flags)."""
    ids = []
    for arg in argv[1:]:
        if arg.startswith("--"):
            break
        ids.append(arg)
    return ids


def _ready_card(board, tmp_path, title="Ready card"):
    (tmp_path / "target.py").write_text("x = 1\n")
    f = board.create_feature(
        title, spec="s", acceptance_criteria="- WHEN x THE SYSTEM SHALL y", files_to_modify=["target.py"]
    )
    return board.mark_ready(f["id"])


# ── the store: a stall is a named BoardError, and the stalled child is gone ──────────


def test_a_stalled_br_call_raises_a_board_timeout_and_the_child_is_killed(board, stallable_br):
    fid = board.create_feature("A card", spec="s")["id"]
    stallable_br.stall("show")

    started = time.monotonic()
    with pytest.raises(BoardError) as caught:  # a BoardError — every handler already catches those
        board.get_feature(fid)

    assert time.monotonic() - started < 10  # the 1s timeout held, not the 60s sleep
    assert isinstance(caught.value, store_mod.BoardTimeout)
    msg = str(caught.value)
    assert msg.startswith("`br show ") and "timed out after 1s" in msg
    # The hung process is killed and reaped, not left behind holding the store.
    pid = int(stallable_br.pidfile.read_text().strip())
    assert not _alive(pid)


def test_a_timeout_names_the_call_without_dumping_every_id(board, stallable_br):
    """The incident's traceback carried all ~350 ids of the show that stalled. The message
    is there to say WHICH call failed; it names the verb and a few ids, then counts."""
    ids = [f"bd-{n:04d}" for n in range(200)]
    stallable_br.stall("show")
    with pytest.raises(BoardError) as caught:
        board._run("show", *ids, want_json=True)
    msg = str(caught.value)
    assert "bd-0000" in msg and "bd-0199" not in msg and "more)" in msg
    assert len(msg) < 400


# ── list_features: detail is read in bounded batches, and only for returned rows ─────


def test_list_features_reads_detail_in_bounded_batches(board, stallable_br, monkeypatch):
    monkeypatch.setattr(store_mod, "_SHOW_BATCH", 2, raising=False)
    ids = [board.create_feature(f"Card {n}", spec="s")["id"] for n in range(5)]
    board.add_dependency(ids[4], ids[0])  # an edge on a row that lands in the LAST batch
    stallable_br.reset()

    feats = {f["id"]: f for f in board.list_features()}

    sizes = [len(_show_ids(argv)) for argv in stallable_br.lines("show")]
    assert sizes and max(sizes) <= 2, sizes
    assert sum(sizes) == 5  # every row read once, none twice
    # Real `br` answered each batch: the edge still projects, whichever batch it rode.
    assert feats[ids[4]]["depends_on"] == [ids[0]]
    assert feats[ids[4]]["open_depends_on"] == [ids[0]]


def test_a_state_filtered_read_only_fetches_detail_for_that_state(board, stallable_br):
    """The loop's own reads are all state-filtered. Detail for rows the call then throws
    away was the bulk of the incident's 350-id show; and with no ready row to mark there
    is no `br ready` cross-reference to make either."""
    for n in range(4):
        board.create_feature(f"Backlog {n}", spec="s")
    blocked = board.create_feature("Blocked", spec="s")["id"]
    board.flag_blocked(blocked, "waiting on the vendor", category="terminal")
    stallable_br.reset()

    assert board.list_features(state="in_review") == []
    assert stallable_br.lines("show") == [] and stallable_br.lines("ready") == []

    rows = board.list_features(state="blocked")
    assert [f["id"] for f in rows] == [blocked]
    assert rows[0]["blocked_reason"] == "waiting on the vendor"  # #414's comment carry still works
    assert [_show_ids(argv) for argv in stallable_br.lines("show")] == [[blocked]]


# ── the loop: a stalled read in one phase leaves the rest of the tick running ────────


async def test_a_stalled_reconcile_read_does_not_cost_the_claim_scan(
    board, stallable_br, tmp_path, monkeypatch, caplog
):
    """The incident, end to end against real `br`: the PR reconcile's `br list` stalls,
    and the ready card is still claimed IN THE SAME TICK. Before the fix the stall killed
    the tick, and an idle loop then slept its whole interval (60s here) before trying
    again — this waits 20s."""
    ready = _ready_card(board, tmp_path)
    loop = BoardLoop(
        {
            "coder": "proto",
            "repo": str(tmp_path),
            "loop_enabled": True,
            "loop_interval_s": 60,
            "merge_poll": True,
            # Due on the first tick whatever the clock says: the poll is gated on
            # `time.monotonic()`, which on Linux counts from BOOT, and a fresh CI VM can be
            # younger than the default 60s interval, so the reconcile silently never ran.
            "merge_poll_interval_s": 0,
            "health_sweep_interval_s": 0,
            "preflight": False,
            "max_pending_reviews": 0,
        }
    )
    monkeypatch.setattr(loop_mod, "get_store", lambda **_kw: board)

    async def _setup_ok():
        return True

    async def _no_recovery():
        return None

    dispatched: list[str] = []

    async def _drive(feature):
        dispatched.append(feature["id"])
        loop._stop.set()

    monkeypatch.setattr(loop, "_setup_gate", _setup_ok)
    monkeypatch.setattr(loop, "_recover", _no_recovery)
    monkeypatch.setattr(loop, "_drive", _drive)
    stallable_br.stall("list")  # the reconcile's reads; the claim scan reads `br ready`

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await asyncio.wait_for(loop._run(), timeout=20)

    assert dispatched == [ready["id"]]
    assert (board.get_feature(ready["id"]) or {}).get("board_state") == "in_progress"
    stalls = [r.message for r in caplog.records if "timed out after 1s" in r.message]
    assert stalls and all("PR reconcile" in m for m in stalls)
    assert not any(r.exc_info for r in caplog.records)  # an understood stall, not a traceback


def _one_tick(monkeypatch, loop, *, reconcile=None, sweep=None, preflight=None):
    """Drive ``loop._run`` for ONE tick with every phase replaced by a recorder; a phase
    given a callable runs it instead (to raise). The claim scan ends the tick."""
    calls: list[str] = []

    def phase(name, custom):
        async def _step():
            calls.append(name)
            if custom is not None:
                custom()
            return False

        return _step

    async def _ok():
        return True

    async def _spawn():
        calls.append("claim scan")
        loop._stop.set()
        return False

    monkeypatch.setattr(loop, "_setup_gate", _ok)
    monkeypatch.setattr(loop, "_recover", phase("recover", None))
    monkeypatch.setattr(loop, "_maybe_reconcile", phase("reconcile", reconcile))
    monkeypatch.setattr(loop, "_maybe_sweep", phase("sweep", sweep))
    monkeypatch.setattr(loop, "_maybe_preflight", phase("preflight", preflight))
    monkeypatch.setattr(loop, "_spawn_ready", _spawn)
    return calls


async def test_a_failed_phase_is_logged_by_name_and_the_later_phases_still_run(monkeypatch, caplog):
    loop = BoardLoop({"coder": "proto", "loop_enabled": True, "loop_interval_s": 60})

    def _stall():
        raise store_mod.BoardTimeout("`br list --limit 0` timed out after 30s and was killed")

    calls = _one_tick(monkeypatch, loop, reconcile=_stall)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await asyncio.wait_for(loop._run(), timeout=5)

    assert calls == ["recover", "reconcile", "sweep", "preflight", "claim scan"]
    [warning] = [r for r in caplog.records if "loop tick:" in r.message]
    assert "PR reconcile failed" in warning.message and "`br list --limit 0` timed out" in warning.message
    assert warning.exc_info is None  # a BoardError is an understood outcome — no traceback


async def test_an_unexpected_phase_error_keeps_its_traceback_and_the_tick_goes_on(monkeypatch, caplog):
    loop = BoardLoop({"coder": "proto", "loop_enabled": True, "loop_interval_s": 60})

    def _bug():
        raise KeyError("id")

    calls = _one_tick(monkeypatch, loop, sweep=_bug)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await asyncio.wait_for(loop._run(), timeout=5)

    assert calls == ["recover", "reconcile", "sweep", "preflight", "claim scan"]
    [error] = [r for r in caplog.records if "loop tick:" in r.message]
    assert "health sweep failed" in error.message and error.exc_info is not None


async def test_a_failed_preflight_still_holds_the_claim_scan(monkeypatch):
    """Isolation stops at the fail-closed gate. A project the preflight never got to
    smoke reads as runnable to the claim scan, so claiming after a failed preflight would
    dispatch exactly the work it exists to hold (dispatch_now stops at the same point).
    Pins behaviour origin/main already had — by accident, as part of the whole tick
    dying — so the isolation above cannot quietly open it."""
    loop = BoardLoop({"coder": "proto", "loop_enabled": True, "loop_interval_s": 60})

    def _stall_then_stop():
        loop._stop.set()  # no claim scan will run to end the tick
        raise store_mod.BoardTimeout("`br list` timed out after 30s and was killed")

    calls = _one_tick(monkeypatch, loop, preflight=_stall_then_stop)
    await asyncio.wait_for(loop._run(), timeout=5)

    assert calls == ["recover", "reconcile", "sweep", "preflight"]  # no claim scan
