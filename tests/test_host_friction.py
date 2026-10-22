"""The board loop's failures reach the host agent's friction ledger.

protoEngineer's friction log recorded nothing from 09-28 on, through epic #4076's heavy
board trouble: coder drives hanging on dropped model connections, an epic base missing
locally ("no commits vs base"), stuck worktrees, preflight holds. All of it happened in
this loop, outside any agent turn, where the agent's own ``record_friction`` tool never
looks. The host's ``graph.sdk.record_friction`` seam (protoAgent) writes into the same
ledger; ``host_friction`` is the loop's door to it: stable summaries, rate-limited, and a
no-op on a core without the seam.
"""

from __future__ import annotations

import sys

import pytest

from project_board import coder_seam, host_friction, worktree
from project_board.loop import BoardLoop


class _Ledger:
    """A stand-in for ``graph.sdk.record_friction``: records each call."""

    def __init__(self):
        self.rows = []

    def __call__(self, kind, summary, detail="", severity="minor", *, source):
        self.rows.append({"kind": kind, "summary": summary, "detail": detail, "severity": severity, "source": source})
        return "abcd1234"

    def summaries(self):
        return [r["summary"] for r in self.rows]


@pytest.fixture
def ledger(monkeypatch):
    led = _Ledger()
    monkeypatch.setattr(sys.modules["graph.sdk"], "record_friction", led, raising=False)
    host_friction.reset()
    yield led
    host_friction.reset()


# ── the door: seam guard, rate limit, stable summaries ────────────────────────────────


def test_an_older_core_without_the_seam_is_a_no_op(monkeypatch):
    monkeypatch.delattr(sys.modules["graph.sdk"], "record_friction", raising=False)
    host_friction.reset()
    assert host_friction.record("anything") is False


def test_a_record_carries_the_plugin_and_harness_kind(ledger):
    assert host_friction.record("coder drive timed out", "card bd-1", severity="major") is True
    (row,) = ledger.rows
    assert row == {
        "kind": "harness",
        "summary": "coder drive timed out",
        "detail": "card bd-1",
        "severity": "major",
        "source": "project_board",
    }


def test_a_flapping_failure_is_recorded_once_per_window(ledger, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(host_friction.time, "monotonic", lambda: clock[0])
    for _ in range(50):
        host_friction.record("board loop phase crashed: PR reconcile (RuntimeError)")
    assert len(ledger.rows) == 1
    # Another card is its own key when the caller scopes it so.
    assert host_friction.record("s", key="s|bd-2") and host_friction.record("s", key="s|bd-3")
    clock[0] += host_friction.WINDOW_S + 1
    assert host_friction.record("board loop phase crashed: PR reconcile (RuntimeError)")


def test_the_window_has_a_global_cap(ledger, monkeypatch):
    monkeypatch.setattr(host_friction, "MAX_PER_WINDOW", 3)
    for i in range(10):
        host_friction.record(f"distinct failure {chr(97 + i)}")
    assert len(ledger.rows) == 3


def test_a_seam_that_raises_never_reaches_the_loop(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("ledger on fire")

    monkeypatch.setattr(sys.modules["graph.sdk"], "record_friction", _boom, raising=False)
    host_friction.reset()
    assert host_friction.record("x") is False


@pytest.mark.parametrize(
    ("reason", "drop", "phrase"),
    [
        (
            "gate FAILED on the merged state (branch + main@abc1234) — the PR merges clean: https://github.com/o/r/pull/12",
            (),
            "gate FAILED on the merged state",
        ),
        ("PR closed without merging — needs triage: https://github.com/o/r/pull/9", (), "PR closed without merging"),
        (
            "bd-1zp: CI failing at the top model tier after 3 same-tier fix(es)",
            ("bd-1zp",),
            "CI failing at the top model tier after N same-tier fix",
        ),
        ("unexpected: KeyError: 'x'", (), "unexpected"),
        ("dependency bd-qq12 is not merged yet", (), "dependency is not merged yet"),
        ("error: pathspec 'src/Foo Bar.ts' did not match any file", (), "error: pathspec … did not match any file"),
        ("git push of feat/bd-1-x to origin refused by src/x/y.py hook", (), "git push of to origin refused by hook"),
        ("merged-state verify re-run needed", (), "merged-state verify re-run needed"),
        ("", (), "unclassified failure"),
    ],
)
def test_a_stable_phrase_carries_no_ids(reason, drop, phrase):
    assert host_friction.stable_phrase(reason, drop=drop) == phrase


def test_two_cards_failing_the_same_way_share_a_summary():
    a = host_friction.stable_phrase("CI failing at the top tier after 2 fixes — needs triage: https://x/pull/1")
    b = host_friction.stable_phrase("CI failing at the top tier after 5 fixes — needs triage: https://x/pull/77")
    assert a == b


# ── the loop's failure points ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "summary", "severity"),
    [
        (
            worktree.CoderTimeout("coder timed out after 1800s"),
            "coder drive timed out and was killed (hung or too slow)",
            "major",
        ),
        (
            worktree.WorktreeError("coder dispatch failed: ACP connection closed (EOF)"),
            "coder dispatch lost its connection (ACP or model stream dropped)",
            "major",
        ),
        (
            worktree.WorktreeError("coder dispatch failed: dispatch_tapped returned NoneType, expected a result"),
            "coder dispatch returned no result",
            "major",
        ),
        (
            worktree.NoChangesError("coder produced no commits vs base — nothing to PR"),
            "coder produced no commits vs base (nothing to open a PR from)",
            "minor",
        ),
        (
            worktree.WorktreeMissing("/w/.worktrees/feat-bd-1", "the pre-PR gate"),
            "a drive's worktree went missing under it",
            "major",
        ),
        (
            coder_seam.OracleTimeout("acceptance tests timed out after 300s"),
            "coder.solve() acceptance oracle timed out",
            "minor",
        ),
    ],
)
def test_a_drive_failure_is_recorded_by_kind_not_by_card(ledger, exc, summary, severity):
    lp = BoardLoop({})
    lp._friction_drive_failure("bd-1", exc, {"id": "bd-1"}, base="epic/x", branch="feat/bd-1-x")
    lp._friction_drive_failure("bd-2", exc, {"id": "bd-2"}, base="epic/x", branch="feat/bd-2-x")
    assert ledger.summaries() == [summary, summary]  # one row in the ledger, two occurrences
    assert ledger.rows[0]["severity"] == severity
    assert "bd-1" in ledger.rows[0]["detail"] and "epic/x" in ledger.rows[0]["detail"]
    # The same card failing the same way again inside the window adds nothing.
    lp._friction_drive_failure("bd-1", exc, {"id": "bd-1"}, base="epic/x", branch="feat/bd-1-x")
    assert len(ledger.rows) == 2


class _BlockStore:
    def __init__(self):
        self.calls = []

    def flag_blocked(self, fid, reason, category="", *, automatic=False):
        self.calls.append((fid, reason, category))
        return {"id": fid}


@pytest.mark.parametrize(
    "reason",
    [
        "PR closed without merging — needs triage: https://github.com/o/r/pull/9",
        "external review FAILED at head abc1234 (QA panel) after 2 review fix round(s) — needs human review: https://x/pull/1",
        "review findings persist after 3 fix attempt(s) — needs human review: https://x/pull/2",
        "gate FAILED on the merged state (branch + main@abc1234) — the PR merges clean but the RESULT is broken; "
        "needs triage: https://x/pull/3\nE   pytest timed out after 30s",
        "rebase conflict with main after 3 attempt(s) — needs a manual rebase: https://x/pull/4",
        "CI failing at the top model tier after 2 same-tier fix(es) — needs triage: https://x/pull/5",
    ],
)
def test_a_gate_or_review_outcome_block_is_not_friction(ledger, reason):
    """A block that is a gate doing its job, or a human's act, is not the board's machinery
    getting in the way: it never reaches the friction ledger (the output's own words, like
    `timed out after` in a failing test, don't count either)."""
    lp = BoardLoop({})
    store = _BlockStore()
    lp._flag_blocked(store, "bd-1", reason)
    assert store.calls == [("bd-1", reason, "")]  # the block itself is unchanged
    assert ledger.rows == []


@pytest.mark.parametrize(
    ("reason", "summary"),
    [
        (
            "ready-queue livelock: skipped 30 consecutive ticks as 'hot-file' and never claimed",
            "board loop blocked a card: a ready card livelocked in the queue (never claimed)",
        ),
        (
            "review gate could not complete after 3 attempt(s) — reviewer 'quinn' did not answer — needs operator attention",
            "board loop blocked a card: the review gate could not complete (reviewer failing)",
        ),
        (
            "coder dispatch failed: delegate 'proto' not configured",
            "board loop blocked a card: the coder dispatch failed",
        ),
    ],
)
def test_a_machinery_block_is_friction(ledger, reason, summary):
    lp = BoardLoop({})
    store = _BlockStore()
    lp._flag_blocked(store, "bd-1", reason, category="terminal")
    assert store.calls == [("bd-1", reason, "terminal")]
    assert ledger.summaries() == [summary] and ledger.rows[0]["severity"] == "major"


def test_a_drive_block_is_not_recorded_twice(ledger):
    """A drive's failure is recorded by kind where it happens; its block passes friction=False."""
    lp = BoardLoop({})
    lp._flag_blocked(_BlockStore(), "bd-1", "coder dispatch failed: ACP connection closed", friction=False)
    assert ledger.rows == []


def test_one_summary_cannot_use_up_the_window(ledger, monkeypatch):
    monkeypatch.setattr(host_friction, "MAX_PER_SUMMARY", 3)
    for i in range(10):
        host_friction.record("coder drive timed out", key=f"x|bd-{i}")
    assert len(ledger.rows) == 3
    assert host_friction.record("a worktree went missing")  # another kind still gets through


def test_details_and_summaries_are_redacted(ledger):
    leak = "NPM_" + "TOKEN=" + "npm" + "_" + "abcdefghijklmnopqrstuvwxyz0123456789"
    host_friction.record("drive failed", "card bd-1: " + leak)
    assert "abcdefghij" not in ledger.rows[0]["detail"] and "[REDACTED]" in ledger.rows[0]["detail"]


async def test_a_crashed_loop_phase_is_recorded(ledger):
    lp = BoardLoop({})

    async def _boom():
        raise RuntimeError("gh exploded")

    outcome, _ = await lp._tick_phase("PR reconcile", _boom)
    assert outcome == "failed"
    assert ledger.summaries() == ["board loop phase crashed: PR reconcile (RuntimeError)"]


class _Proc:
    def __init__(self, rc, out):
        self.returncode, self._out, self.pid = rc, out, 0

    async def communicate(self):
        return self._out, b""


async def test_a_preflight_hold_is_recorded_once_per_distinct_failure(ledger, monkeypatch):
    async def _quiet(*_a, **_k):
        return ""

    monkeypatch.setattr(worktree, "base_checkout_dirt", _quiet)
    monkeypatch.setattr(worktree, "checkout_head_sha", _quiet)
    outputs = []

    async def _shell(cmd, **_k):
        rc, out = outputs.pop(0)
        return _Proc(rc, out)

    monkeypatch.setattr(worktree, "spawn_shell", _shell)
    lp = BoardLoop({"local_gate_cmd": "pnpm -r typecheck"})
    fail = b" ERR_PNPM_RECURSIVE_RUN_FIRST_FAIL  @ds/core@0.4.1 typecheck: `tsc`\nExit status 1"
    for out in (fail, b"Done in 2.1s\n" + fail, b"Error: Cannot find module 'left-pad'\nExit status 1"):
        outputs.append((1, out))
        await lp._preflight("ds", "pnpm -r typecheck", "/repo")
    summary = "gate preflight fails on clean base, so the project's cards are held"
    assert ledger.summaries() == [summary, summary]  # the repeat was quiet; the new failure was not
    assert "ERR_PNPM_RECURSIVE_RUN_FIRST_FAIL" in ledger.rows[0]["detail"] and "project ds" in ledger.rows[0]["detail"]
    assert "left-pad" in ledger.rows[1]["detail"]

    outputs.append((0, b""))
    await lp._preflight("ds", "pnpm -r typecheck", "/repo")  # recovered: a later failure is new again
    assert "ds" not in lp._preflight_friction


async def test_a_coder_delegate_that_is_not_configured_is_friction(ledger):
    """Not a raised drive failure, so only the block itself can record it: the drive's
    block passes friction=True for this one machinery cause."""
    lp = BoardLoop({})
    store = _BlockStore()
    lp._moved_under_drive = lambda *_a, **_k: ""
    blocked = await lp._block_or_stand_aside(
        store, "bd-1", "coder delegate 'proto' not configured/enabled", repo="/r", wt=None, branch=None, friction=True
    )
    assert blocked and store.calls == [("bd-1", "coder delegate 'proto' not configured/enabled", "")]
    assert ledger.summaries() == ["board loop blocked a card: the coder dispatch failed"]
