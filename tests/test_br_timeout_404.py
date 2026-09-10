"""A stalled `br` must cost one read, not the loop (#404).

Live incident (protoEngineer, 2026-09-07): four `loop tick failed` tracebacks in ten
minutes, every one a `subprocess.TimeoutExpired` out of `store._run` — a `br list`, or
the `br show` that `list_features` made with ~350 ids in one call — raised by the PR
reconcile, the FIRST phase of the tick. `TimeoutExpired` is no kind of BoardError, so it
went straight past every handler to the tick's catch-all. A `br list` measured 0.058s
straight afterwards: the store was contended, not slow.

What holds now: a stall is a named `BoardTimeout` with the stalled tree stopped; the
reads that stalled are bounded; a FAILED phase costs only itself; a STALL ends the tick
after one call; and no handler reads a stall as a definite answer — not a claim race, a
cancel's undo, a create's dedup, an HTTP 400, or a block whose reason was never written.

The seam is an external process, so every stall here is a REAL one — a `br` wrapper that
runs the real binary until told to stall (`PB_STALL=<mode>:<verb>`), never a mocked
`_run` raising on cue.
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

import project_board as pb
import project_board.loop as loop_mod
from project_board import api
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
    """The real `br`, until told to stall. ``PB_STALL=<mode>:<verb>``:

    - ``verb`` — every call of that verb hangs (``exec sleep``), recording its pid;
    - ``once`` — only the first call of that verb hangs;
    - ``claim`` — any ``--claim`` write hangs;
    - ``all`` — every call hangs: a wedged store;
    - ``after`` — the verb RUNS (its write commits), then the process hangs;
    - ``tree`` — a shell that records a SIGTERM, with a TERM-immune grandchild.

    Every invocation's argv is logged (``lines(verb)``). The timeout drops to 1s and the
    TERM grace to 0.3s, so a stall costs a second, not 45; `raising=False` keeps the
    fixture usable against a store without the knobs (the red-check run), and the first
    test fails if the timeout knob is dead, because its message would then say 45s."""
    real = shutil.which(store_mod.BR)
    calls, pidfile, once, termfile = (tmp_path / n for n in ("br-calls.log", "stalled.pid", "once", "term"))
    script = tmp_path / "br-stallable"
    script.write_text(
        f"""#!/bin/sh
echo "$*" >> "{calls}"
mode="${{PB_STALL%%:*}}"; verb="${{PB_STALL#*:}}"
case "$mode" in
  verb) if [ "$1" = "$verb" ]; then echo $$ > "{pidfile}"; exec sleep 60; fi ;;
  once) if [ "$1" = "$verb" ] && [ ! -e "{once}" ]; then touch "{once}"; exec sleep 60; fi ;;
  claim) case " $* " in *" --claim "*) exec sleep 60 ;; esac ;;
  all) exec sleep 60 ;;
  after) if [ "$1" = "$verb" ]; then "{real}" "$@" >/dev/null 2>&1; exec sleep 60; fi ;;
  tree) if [ "$1" = "$verb" ]; then
          trap 'echo term > "{termfile}"; exit 143' TERM
          (trap '' TERM; exec sleep 60) & echo $! > "{pidfile}"
          wait
        fi ;;
esac
exec "{real}" "$@"
"""
    )
    script.chmod(0o755)
    monkeypatch.setattr(store_mod, "BR", str(script))
    monkeypatch.setattr(store_mod, "_BR_TIMEOUT_S", 1.0, raising=False)
    monkeypatch.setattr(store_mod, "_BR_TERM_GRACE_S", 0.3, raising=False)
    monkeypatch.delenv("PB_STALL", raising=False)

    def lines(verb: str) -> list[list[str]]:
        text = calls.read_text() if calls.exists() else ""
        return [line.split() for line in text.splitlines() if line.split()[:1] == [verb]]

    return SimpleNamespace(
        stall=lambda verb, mode="verb": monkeypatch.setenv("PB_STALL", f"{mode}:{verb}"),
        calm=lambda: monkeypatch.delenv("PB_STALL", raising=False),
        reset=lambda: calls.write_text(""),
        count=lambda: len([ln for ln in (calls.read_text() if calls.exists() else "").splitlines() if ln.strip()]),
        lines=lines,
        pidfile=pidfile,
        termfile=termfile,
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


def _ready_card(board, tmp_path, title="Ready card", name="target.py"):
    (tmp_path / name).write_text("x = 1\n")
    f = board.create_feature(title, spec="s", acceptance_criteria="- WHEN x THE SYSTEM SHALL y", files_to_modify=[name])
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


# ── the process: asked to stop, then killed — the whole tree ─────────────────────────


def test_a_stalled_br_is_asked_to_stop_then_killed_with_everything_it_forked(board, stallable_br):
    """Only the direct child used to be killed: a `br` behind a shim that forks left its
    child running. Now the whole group gets SIGTERM (br handles it, so a write is not cut
    mid-frame), then SIGKILL after a grace — which a TERM-immune grandchild needs."""
    stallable_br.stall("list", mode="tree")

    with pytest.raises(store_mod.BoardTimeout):
        board.list_features()

    assert stallable_br.termfile.read_text().strip() == "term"  # asked first
    grandchild = int(stallable_br.pidfile.read_text().strip())
    for _ in range(40):  # SIGKILLed children are reaped by init asynchronously
        if not _alive(grandchild):
            break
        time.sleep(0.05)
    assert not _alive(grandchild)  # then the whole group was killed


def test_the_store_outwaits_brs_own_lock_wait():
    """At 30s the store stopped `br` milliseconds before br 0.2.16's own 30s write-lock
    wait would have reported DATABASE_ERROR — which `_run` retries — so contention came
    back as an opaque stall instead."""
    assert store_mod._BR_TIMEOUT_S > 30


def test_a_stalled_br_init_is_a_board_timeout_too(tmp_path, stallable_br):
    stallable_br.stall("init")
    board = BeadsBoard(repo=str(tmp_path / "fresh"), actor="test")
    (tmp_path / "fresh").mkdir()

    with pytest.raises(store_mod.BoardTimeout):
        board.create_feature("First card on a fresh repo", spec="s")


# ── a stall is not a definite answer ─────────────────────────────────────────────────


async def test_a_stalled_claim_is_not_a_lost_race_and_the_scan_stops_at_it(board, stallable_br, tmp_path, monkeypatch):
    """A claim that stalled was folded into "not claimable": each counted as a lost claim
    race, five of them terminal-blocked a healthy card as a ready-queue livelock, and one
    scan paid a full timeout per ready card while holding the claim lock."""
    cards = [_ready_card(board, tmp_path, f"Ready {n}", f"t{n}.py")["id"] for n in range(4)]
    loop = BoardLoop(
        {"coder": "proto", "repo": str(tmp_path), "loop_enabled": True, "preflight": False, "max_pending_reviews": 0}
    )
    loop.max_concurrent = 4
    monkeypatch.setattr(loop_mod, "get_store", lambda **_kw: board)
    stallable_br.stall("", mode="claim")

    for _ in range(6):  # more scans than the livelock bound (5)
        stallable_br.reset()
        with pytest.raises(store_mod.BoardTimeout):
            await loop._spawn_ready()
        assert len([argv for argv in stallable_br.lines("update") if "--claim" in argv]) == 1  # stopped at it

    stallable_br.calm()
    assert not any(board.get_feature(fid)["blocked"] for fid in cards)
    assert getattr(loop, "_ready_skips", {}) == {}  # a stall is never counted toward a livelock


def test_a_cancel_whose_close_landed_then_stalled_stays_cancelled(board, stallable_br):
    """The close committed, then `br` hung. The undo used to run anyway and strip
    `cancelled` from a CLOSED bead, which then read as `done` — shipped work."""
    fid = board.create_feature("Duplicate card", spec="s")["id"]
    stallable_br.stall("close", mode="after")

    f = board.cancel_feature(fid, "duplicate of bd-1")

    stallable_br.calm()
    assert f["board_state"] == "cancelled" == board.get_feature(fid)["board_state"]


def test_a_cancel_whose_close_stalled_before_landing_is_undone(board, stallable_br):
    fid = board.create_feature("Card", spec="s")["id"]
    stallable_br.stall("close")

    with pytest.raises(store_mod.BoardTimeout):
        board.cancel_feature(fid, "scope cut")

    stallable_br.calm()
    g = board.get_feature(fid)
    assert g["board_state"] == "backlog" and "cancelled" not in g["labels"]  # exactly as before the cancel


def test_the_cancel_route_changes_nothing_when_its_pre_read_stalls(board, stallable_br, monkeypatch):
    """The pre-read is what finds the card's open PR. A stalled one was swallowed, the
    cancel went on with no PR url, and the PR was never closed (#211)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    fid = board.create_feature("Card with a PR", spec="s")["id"]
    board._run("update", fid, "--external-ref", "https://github.com/o/r/pull/7")
    monkeypatch.setattr(api, "get_store", lambda **_kw: board)
    app = FastAPI()
    app.include_router(api.build_data_router({}), prefix="/api/plugins/project_board")
    stallable_br.stall("show", mode="once")

    r = TestClient(app).post(f"/api/plugins/project_board/features/{fid}/cancel", json={"reason": "scope cut"})

    assert r.status_code == 503 and "timed out" in r.text  # a stall is 503 — retry after checking — not 400
    assert board.get_feature(fid)["board_state"] == "backlog"  # and nothing changed


def test_create_does_not_go_ahead_blind_when_its_dedup_read_stalls(board, stallable_br, monkeypatch):
    """The likeliest reason a caller creates again is that its last create timed out, and
    that one may have committed. A stalled dedup read used to count as "nothing to dedup
    against", and the create went ahead: a duplicate."""
    monkeypatch.setattr(store_mod, "get_store", lambda **_kw: board)
    stallable_br.stall("list")
    stallable_br.reset()

    reply = {t.name: t for t in pb._board_tools({})}["board_create_feature"].invoke(
        {"title": "Regenerate THIRD_PARTY_LICENSES", "spec": "s"}
    )

    assert reply.startswith("Error: ") and "timed out" in reply and "re-read" in reply
    assert stallable_br.lines("create") == []  # nothing created blind


def test_a_block_is_never_recorded_without_its_reason(board, stallable_br):
    """`flag_blocked` wrote its reason through the best-effort `comment()`, which swallows
    a failed write — a stall included — so the label could land with no reason: the
    reason-less terminal block #414 forbids. The reason now goes first and must land."""
    fid = board.create_feature("Card", spec="s")["id"]
    stallable_br.stall("comments")

    with pytest.raises(store_mod.BoardTimeout):
        board.flag_blocked(fid, "the gate command does not exist on this repo", "terminal")

    stallable_br.calm()
    assert not board.get_feature(fid)["blocked"]


# ── the loop: a failed phase costs itself; a stall ends the tick ─────────────────────


def _tick_loop(board, tmp_path, monkeypatch, **cfg):
    loop = BoardLoop(
        {
            "coder": "proto",
            "repo": str(tmp_path),
            "loop_enabled": True,
            "merge_poll": True,
            # Due on the first tick whatever the clock says: the poll is gated on
            # `time.monotonic()`, which on Linux counts from BOOT, and a fresh CI VM can be
            # younger than the default 60s interval, so the reconcile silently never ran.
            "merge_poll_interval_s": 0,
            "health_sweep_interval_s": 0,
            "preflight": False,
            "max_pending_reviews": 0,
            **cfg,
        }
    )
    monkeypatch.setattr(loop_mod, "get_store", lambda **_kw: board)
    return loop


async def test_a_wedged_store_costs_one_stalled_call_per_tick(board, stallable_br, tmp_path, monkeypatch):
    """Every phase of a tick reads the store. Isolating phases let a wedged store stall
    once PER CALL — seven stalled calls in one tick, each holding the single-flight lock
    every other board read waits behind. The first stall now ends the tick."""
    _ready_card(board, tmp_path)
    loop = _tick_loop(
        board, tmp_path, monkeypatch, health_sweep_interval_s=0.001, preflight=True, max_pending_reviews=3
    )
    stallable_br.reset()
    stallable_br.stall("", mode="all")  # the store stops answering at all

    started = time.monotonic()
    spawned = await loop._tick()

    assert spawned is False
    assert stallable_br.count() == 1  # one stalled call, then the tick stopped
    assert time.monotonic() - started < 5


async def test_after_a_stall_the_next_tick_claims(board, stallable_br, tmp_path, monkeypatch, caplog):
    """The incident, end to end against real `br`: the PR reconcile's read stalls once. That
    tick ends with a logged stall, not a traceback, and the next tick claims the card."""
    ready = _ready_card(board, tmp_path)
    loop = _tick_loop(board, tmp_path, monkeypatch)
    dispatched: list[str] = []

    async def _drive(feature):
        dispatched.append(feature["id"])

    monkeypatch.setattr(loop, "_drive", _drive)
    stallable_br.stall("list", mode="once")

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        first = await loop._tick()
        second = await loop._tick()
        await asyncio.gather(*loop._drives)

    assert (first, second) == (False, True) and dispatched == [ready["id"]]
    [stall] = [r for r in caplog.records if "stalled on the board store" in r.message]
    assert "PR reconcile" in stall.message and "timed out after 1s" in stall.message
    assert not any(r.exc_info for r in caplog.records)  # an understood stall, not a traceback


def _fake_phases(monkeypatch, loop, *, reconcile=None, sweep=None, preflight=None):
    """Every phase of ``loop._tick`` replaced by a recorder; a phase given a callable runs
    it instead (to raise)."""
    calls: list[str] = []

    def phase(name, custom):
        async def _step():
            calls.append(name)
            if custom is not None:
                custom()
            return False

        return _step

    monkeypatch.setattr(loop, "_maybe_reconcile", phase("reconcile", reconcile))
    monkeypatch.setattr(loop, "_maybe_sweep", phase("sweep", sweep))
    monkeypatch.setattr(loop, "_maybe_preflight", phase("preflight", preflight))
    monkeypatch.setattr(loop, "_spawn_ready", phase("claim scan", None))
    return calls


async def test_a_failed_phase_is_logged_by_name_and_the_later_phases_still_run(monkeypatch, caplog):
    loop = BoardLoop({"coder": "proto", "loop_enabled": True})

    def _refused():
        raise BoardError("`br list --limit 0` failed: VALIDATION_FAILED")

    calls = _fake_phases(monkeypatch, loop, reconcile=_refused)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await loop._tick()

    assert calls == ["reconcile", "sweep", "preflight", "claim scan"]
    [warning] = [r for r in caplog.records if "loop tick:" in r.message]
    assert "PR reconcile failed" in warning.message and "VALIDATION_FAILED" in warning.message
    assert warning.exc_info is None  # a BoardError is an understood outcome — no traceback


async def test_a_stalled_phase_ends_the_tick(monkeypatch, caplog):
    loop = BoardLoop({"coder": "proto", "loop_enabled": True})

    def _stall():
        raise store_mod.BoardTimeout("`br list --limit 0` timed out after 45s and was stopped")

    calls = _fake_phases(monkeypatch, loop, reconcile=_stall)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await loop._tick()

    assert calls == ["reconcile"]  # the sweep, preflight and claim scan wait for the next tick
    [warning] = [r for r in caplog.records if "loop tick:" in r.message]
    assert "PR reconcile stalled on the board store" in warning.message and warning.exc_info is None


async def test_an_unexpected_phase_error_keeps_its_traceback_and_the_tick_goes_on(monkeypatch, caplog):
    loop = BoardLoop({"coder": "proto", "loop_enabled": True})

    def _bug():
        raise KeyError("id")

    calls = _fake_phases(monkeypatch, loop, sweep=_bug)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await loop._tick()

    assert calls == ["reconcile", "sweep", "preflight", "claim scan"]
    [error] = [r for r in caplog.records if "loop tick:" in r.message]
    assert "health sweep failed" in error.message and error.exc_info is not None


async def test_a_failed_preflight_still_holds_the_claim_scan(monkeypatch):
    """Isolation stops at the fail-closed gate. A project the preflight never got to
    smoke reads as runnable to the claim scan, so claiming after a failed preflight would
    dispatch exactly the work it exists to hold (dispatch_now stops at the same point).
    Pins behaviour origin/main already had — by accident, as part of the whole tick
    dying — so the isolation above cannot quietly open it."""
    loop = BoardLoop({"coder": "proto", "loop_enabled": True})

    def _refused():
        raise BoardError("`br list` failed: locked")

    calls = _fake_phases(monkeypatch, loop, preflight=_refused)
    await loop._tick()

    assert calls == ["reconcile", "sweep", "preflight"]  # no claim scan
