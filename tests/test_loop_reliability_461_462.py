"""#461 (a tree reaped under its drive) and #462 (the tick starving the claim scan).

#461: the health sweep reaped the worktree of a card whose drive was still in flight, and
the pre-PR gate's FileNotFoundError on the missing cwd was then "treated as pass". The
sweep now asks everything that can hold a tree before it reaps: the live-drive registry,
the card's running reconcile or review, and the OS (a live process whose cwd is inside the
tree). A drive still running for a card that has closed is cancelled, and the tree goes
on a later sweep. A gate over a tree that no longer exists fails the drive, "worktree
missing", and nothing is published.

#462: the tick ran each in-review card's rebase / merged-state gate / CI / review / merge
inline, ahead of the claim scan. One 600 s gate plus a hung review call, and the board
claimed nothing for four hours. Each card's reconcile is now its own tracked task, the
review call has a hard cap of the board's own, and a stalled claim scan shows on /status.

The process-shaped claims are tested with REAL processes: a real `sleep` sitting in a
real worktree, a real gate subprocess that outlives the tick.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
import types

import pytest

from project_board import health, setup_check, worktree
from project_board.loop import BoardLoop, _register_drive, _unregister_drive

ROOT = ".worktrees"


# ── fixtures ────────────────────────────────────────────────────────────────────────


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "Application Support" / "projects" / "protoContent"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "board-test@localhost", cwd=repo)
    _git("config", "user.name", "Board Test", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    (repo / "README.md").write_text("seed\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    return repo


def _tree(repo, fid, title=""):
    rel = f"{ROOT}/{worktree.worktree_dir(fid, title)}"
    _git("worktree", "add", "-q", "-b", worktree.branch_name(fid, title), rel, "main", cwd=repo)
    return repo / rel


class _SweepStore:
    def __init__(self, states):
        self.states = states

    def get_feature(self, fid):
        st = self.states.get(fid)
        return {"id": fid, "board_state": st} if st else None

    def list_features(self, state=None, **_kw):
        return []

    def archive_stale(self, archive_after_days=7):
        return []

    def live_cards(self):
        return []


def _sleeper(cwd):
    """A real process whose cwd is inside ``cwd`` — the coder's ACP session, say."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=str(cwd))


# ── #461: the sweep never reaps a tree something is still working in ────────────────


async def test_processes_in_trees_finds_a_real_process_by_its_cwd(tmp_path):
    inside = tmp_path / "tree with space" / "src"
    inside.mkdir(parents=True)
    other = tmp_path / "elsewhere"
    other.mkdir()
    proc = _sleeper(inside)
    try:
        found = await _wait_for_cwd(str(tmp_path / "tree with space"), proc.pid)
        assert proc.pid in found.get(str(tmp_path / "tree with space"), [])
        assert str(other) not in found
    finally:
        proc.kill()
        proc.wait()
    assert await worktree.processes_in_trees([str(other)]) == {}
    assert await worktree.processes_in_trees([]) == {}


async def _wait_for_cwd(path, pid, timeout=10.0):
    """The child has to have exec'd and chdir'd before the probe can see its cwd."""
    deadline = time.monotonic() + timeout
    while True:
        found = await worktree.processes_in_trees([path])
        if pid in found.get(path, []) or time.monotonic() > deadline:
            return found
        await asyncio.sleep(0.1)


async def test_a_live_process_in_a_closed_cards_tree_prevents_the_reap(monkeypatch, tmp_path):
    """The card is done and no drive is registered — every bookkeeping check says orphan.
    A real process still sitting in the tree keeps it; once that process is gone the next
    sweep reaps it."""
    repo = _repo(tmp_path)
    tree = _tree(repo, "ds-old", "shipped card")
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: _SweepStore({"ds-old": "done"}))
    loop = BoardLoop({"repo": str(repo), "worktrees_root": ROOT})
    proc = _sleeper(tree)
    try:
        await _wait_for_cwd(str(tree), proc.pid)
        await loop._sweep()
        assert tree.is_dir(), "a tree a live process is working in was reaped"
    finally:
        proc.kill()
        proc.wait()
    await loop._sweep()
    assert not tree.exists()


async def test_a_drive_still_running_for_a_closed_card_is_cancelled_then_reaped(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    tree = _tree(repo, "ds-zom", "zombie drive")
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: _SweepStore({"ds-zom": "cancelled"}))
    loop = BoardLoop({"repo": str(repo), "worktrees_root": ROOT})
    drive = asyncio.create_task(asyncio.sleep(3600), name="pb-drive-ds-zom")
    _register_drive("ds-zom", drive)
    try:
        await loop._sweep()
        assert tree.is_dir()  # the drive is cancelled first; its tree is not reaped under it
        await asyncio.gather(drive, return_exceptions=True)
        assert drive.cancelled()
    finally:
        _unregister_drive("ds-zom", drive)
    await loop._sweep()  # no drive holds it any more
    assert not tree.exists()


async def test_a_live_drive_on_an_open_card_is_never_cancelled_or_reaped(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    tree = _tree(repo, "ds-run", "running drive")
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: _SweepStore({"ds-run": "in_progress"}))
    loop = BoardLoop({"repo": str(repo), "worktrees_root": ROOT})
    drive = asyncio.create_task(asyncio.sleep(3600), name="pb-drive-ds-run")
    _register_drive("ds-run", drive)  # registered, but its _inflight_files entry already gone
    try:
        await loop._sweep()
        assert tree.is_dir() and not drive.done()
    finally:
        _unregister_drive("ds-run", drive)
        drive.cancel()
        await asyncio.gather(drive, return_exceptions=True)


async def test_a_card_whose_merge_gate_is_running_keeps_its_tree(monkeypatch, tmp_path):
    """Even a card the store reports done (merged under the gate) keeps its tree while its
    own reconcile task is still running."""
    repo = _repo(tmp_path)
    tree = _tree(repo, "ds-mrg", "merge gating")
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: _SweepStore({"ds-mrg": "done"}))
    loop = BoardLoop({"repo": str(repo), "worktrees_root": ROOT})
    gate = asyncio.create_task(asyncio.sleep(3600))
    loop._card_tasks["ds-mrg"] = gate
    try:
        await loop._sweep()
        assert tree.is_dir()
    finally:
        gate.cancel()
        await asyncio.gather(gate, return_exceptions=True)


# ── #461: a gate over a tree that is gone is not a pass ─────────────────────────────


async def test_a_pre_pr_gate_in_a_missing_tree_raises_worktree_missing(tmp_path):
    gone = tmp_path / "feat-ds-1-reaped"
    with pytest.raises(worktree.WorktreeMissing, match="worktree missing"):
        await BoardLoop({"local_gate_cmd": "true"})._run_local_gate(str(gone))


async def test_a_tree_reaped_while_its_gate_runs_is_not_a_verdict(tmp_path):
    """A REAL gate whose tree vanishes under it (here it removes its own cwd, as the sweep
    did) exits red — which judged nothing."""
    tree = tmp_path / "feat-ds-2"
    tree.mkdir()
    loop = BoardLoop({"local_gate_cmd": f'rm -rf "{tree}"; exit 1'})
    with pytest.raises(worktree.WorktreeMissing):
        await loop._run_local_gate(str(tree))


async def test_a_gate_timing_out_on_a_healthy_tree_still_passes(tmp_path):
    loop = BoardLoop({"local_gate_cmd": "sleep 30", "local_gate_timeout_s": 0.5})
    assert await loop._run_local_gate(str(tmp_path)) is None  # CI still gates


class _DriveStore:
    def __init__(self):
        self.calls = []

    def current_tier(self, fid):
        return ""

    def get_feature(self, fid):
        return {"id": fid, "board_state": "in_progress"}

    def list_features(self, state=None, **_kw):
        return []

    def flag_blocked(self, fid, reason, category=""):
        self.calls.append(("flag_blocked", fid, reason, category))
        return {"id": fid}

    def open_review(self, fid, *, pr_url):
        self.calls.append(("open_review", fid, pr_url))

    def comment(self, fid, text):
        self.calls.append(("comment", fid, text))

    def record_budget(self, fid, kind, n):
        pass

    def clear_budgets(self, fid, kinds=None):
        pass


async def _drive_over_a_reaped_tree(monkeypatch, tmp_path, *, gate):
    """Run a real drive whose worktree is a real directory the test removes mid-drive."""
    store = _DriveStore()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    tree = tmp_path / "Application Support" / ".worktrees" / "feat-ds-9-thing"
    dispatches, opened = [], []

    async def _create(repo, base, fid, root, title="", **_kw):
        tree.mkdir(parents=True, exist_ok=True)
        return (str(tree), f"feat/{fid}-thing")

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatches.append(wt)
        return "## Summary\n\n- did it"

    async def _open_pr(*a, **k):
        opened.append(a)
        return "https://example/pr/1"

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _noop)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _noop)

    async def _nothing_stranded(*a, **k):
        return [], []

    monkeypatch.setattr(worktree, "set_aside_stranded_worktrees", _nothing_stranded)
    loop = BoardLoop({"coder": "proto", "local_gate_cmd": "true", "local_gate_max": 2})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    if gate is not None:
        monkeypatch.setattr(loop, "_run_local_gate", gate(tree, loop._run_local_gate))
    feature = {"id": "ds-9", "title": "thing", "repo": str(tmp_path), "base_branch": "main", "spec": "x"}
    await loop._drive(feature)
    return store, dispatches, opened


async def test_a_drive_whose_tree_was_reaped_before_its_gate_fails_clearly(monkeypatch, tmp_path):
    def gate(tree, real):
        async def _gate(wt, feature=None):
            shutil.rmtree(tree)  # the sweep, between the coder's reply and the gate
            return await real(wt, feature)  # the REAL gate, against the missing cwd

        return _gate

    store, dispatches, opened = await _drive_over_a_reaped_tree(monkeypatch, tmp_path, gate=gate)
    blocks = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocks) == 1 and blocks[0][2].startswith("worktree missing:"), blocks
    assert blocks[0][3] == "transient"
    assert opened == [] and not any(c[0] == "open_review" for c in store.calls)
    assert len(dispatches) == 1


async def test_a_keep_worktree_redispatch_into_a_reaped_tree_fails_clearly(monkeypatch, tmp_path):
    """The gate ran and was red, so the drive would re-dispatch into the same tree — which
    the sweep has removed. It must not dispatch a coder into nothing."""

    def gate(tree, real):
        async def _gate(wt, feature=None):
            shutil.rmtree(tree, ignore_errors=True)
            return "1 failed"  # red, before the tree went

        return _gate

    store, dispatches, opened = await _drive_over_a_reaped_tree(monkeypatch, tmp_path, gate=gate)
    blocks = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocks) == 1 and "worktree missing" in blocks[0][2] and "keep-worktree" in blocks[0][2]
    assert len(dispatches) == 1  # no second coder into a missing workdir
    assert opened == []


async def test_a_tree_reaped_while_the_coder_ran_fails_before_any_gate_or_pr(monkeypatch, tmp_path):
    """With NO gate configured there is no gate to notice: the drive itself checks its tree
    once the coder returns, and publishes nothing from a directory that is gone."""
    store = _DriveStore()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    tree = tmp_path / "feat-ds-8-thing"
    opened = []

    async def _create(repo, base, fid, root, title="", **_kw):
        tree.mkdir(parents=True, exist_ok=True)
        return (str(tree), f"feat/{fid}-thing")

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        shutil.rmtree(wt)  # the sweep, mid-dispatch (a stalled coder, #461)
        return "## Summary\n\n- did it"

    async def _open_pr(*a, **k):
        opened.append(a)
        return "https://example/pr/1"

    async def _nothing_stranded(*a, **k):
        return [], []

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "set_aside_stranded_worktrees", _nothing_stranded)
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    await loop._drive({"id": "ds-8", "title": "thing", "repo": str(tmp_path), "base_branch": "main", "spec": "x"})
    blocks = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocks) == 1 and "removed while its coder ran" in blocks[0][2]
    assert opened == []


# ── #462: the claim scan never waits on per-card work ────────────────────────────────


class _TickStore:
    """An in-review card with a PR, and a ready card to claim."""

    def __init__(self):
        self.claimed = []

    def list_features(self, state=None, **_kw):
        if state == "in_review":
            return [
                {"id": "ds-rev", "board_state": "in_review", "pr_url": "https://github.com/o/r/pull/1", "labels": []}
            ]
        return []

    def get_feature(self, fid):
        return {"id": fid, "board_state": "in_review", "labels": []}

    def ready_queue(self, relaxed=False):
        return [] if self.claimed else [{"id": "ds-new", "board_state": "ready", "files_to_modify": []}]

    def claim(self, fid, assignee=""):
        self.claimed.append(fid)
        return {"id": fid, "board_state": "in_progress"}


def _tick_loop(monkeypatch, store, **cfg):
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"coder": "proto", "merge_poll": True, "max_pending_reviews": 0, "max_concurrent": 2, **cfg})
    drove = []

    async def _drive(feature):
        drove.append(feature["id"])

    async def _nothing():
        return None

    monkeypatch.setattr(loop, "_drive", _drive)
    monkeypatch.setattr(loop, "_maybe_sweep", _nothing)
    monkeypatch.setattr(loop, "_maybe_preflight", _nothing)
    return loop, drove


async def _open_pr_state(url, cwd="."):
    return "OPEN"


async def _cancel_cards(loop):
    tasks = list(loop._card_tasks.values())
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_a_slow_review_gate_does_not_delay_a_claim(monkeypatch):
    store = _TickStore()
    loop, drove = _tick_loop(monkeypatch, store)
    started = asyncio.Event()

    async def _hung_review(store_, f, **_kw):  # a review-gate model call whose stream never ends
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(loop, "_reconcile_pr", _hung_review)
    try:
        t0 = time.monotonic()
        await asyncio.wait_for(loop._tick(), timeout=5)
        await asyncio.sleep(0)  # let the claimed drive start
        assert time.monotonic() - t0 < 5
        assert started.is_set()  # the review really is running…
        assert store.claimed == ["ds-new"] and drove == ["ds-new"]  # …and the claim went ahead
        assert "ds-rev" in loop._card_tasks and not loop._card_tasks["ds-rev"].done()
        # The next poll does not stack a second reconcile on the card still in one.
        loop._last_poll = 0.0
        before = loop._card_tasks["ds-rev"]
        await loop._maybe_reconcile()
        assert loop._card_tasks["ds-rev"] is before
    finally:
        await _cancel_cards(loop)


async def test_a_real_gate_subprocess_on_an_in_review_card_does_not_delay_a_claim(monkeypatch, tmp_path):
    """The same, with the slow step a REAL 600 s-style gate process (the merged-state gate):
    the tick returns while it runs, and stop() takes the whole tree down."""
    store = _TickStore()
    loop, drove = _tick_loop(monkeypatch, store, local_gate_cmd="sleep 60", local_gate_timeout_s=600)
    gate_pids = []
    real_spawn = worktree.spawn_shell

    async def _spawn(cmd, **kw):
        proc = await real_spawn(cmd, **kw)
        gate_pids.append(proc.pid)
        return proc

    monkeypatch.setattr(worktree, "spawn_shell", _spawn)

    async def _merged_state_gate(store_, f, **_kw):
        await loop._run_local_gate(str(tmp_path))

    monkeypatch.setattr(loop, "_reconcile_pr", _merged_state_gate)
    try:
        await asyncio.wait_for(loop._tick(), timeout=10)
        await asyncio.sleep(0.2)
        assert store.claimed == ["ds-new"]
        assert gate_pids, "the gate process never started"
        os.kill(gate_pids[0], 0)  # still running after the tick returned
    finally:
        await loop.stop()
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(gate_pids[0], 0)


async def test_the_card_reconciles_are_bounded_by_reconcile_concurrency(monkeypatch):
    class _Many(_TickStore):
        rows = {
            f"ds-{i}": {
                "id": f"ds-{i}",
                "board_state": "in_review",
                "pr_url": f"https://github.com/o/r{i}/pull/{i}",
                "repo": f"/repos/r{i}",  # five repos: concurrency is ACROSS repos
                "labels": [],
            }
            for i in range(5)
        }

        def list_features(self, state=None, **_kw):
            return [dict(r) for r in self.rows.values()] if state == "in_review" else []

        def get_feature(self, fid):
            return dict(self.rows[fid])

    loop, _drove = _tick_loop(monkeypatch, _Many(), reconcile_concurrency=2)
    monkeypatch.setattr(worktree, "pr_state", _open_pr_state)
    running, peak, release = set(), [0], asyncio.Event()

    async def _slow(store_, f, **_kw):
        running.add(f["id"])
        peak[0] = max(peak[0], len(running))
        await release.wait()
        running.discard(f["id"])

    monkeypatch.setattr(loop, "_reconcile_pr", _slow)
    try:
        await loop._reconcile_prs(detach=True)
        await asyncio.sleep(0.05)
        assert len(loop._card_tasks) == 5 and peak[0] == 2
        release.set()
        await asyncio.gather(*loop._card_tasks.values())
        assert peak[0] == 2 and loop._card_tasks == {}
    finally:
        await _cancel_cards(loop)


# ── #462: the review-gate model call has a hard cap of its own ───────────────────────


def _inject_runner(monkeypatch, runner):
    rt = types.ModuleType("runtime")
    rt_state = types.ModuleType("runtime.state")
    rt_state.STATE = types.SimpleNamespace(workflow_run=runner)
    rt.state = rt_state
    monkeypatch.setitem(sys.modules, "runtime", rt)
    monkeypatch.setitem(sys.modules, "runtime.state", rt_state)


async def test_a_hung_review_call_that_ignores_its_cancel_is_abandoned_on_time(monkeypatch):
    """protoAgent#3699: the host client's request_timeout did not apply to a hung stream.
    The board's cap must hold even for a call that swallows its cancel."""
    swallowed = []

    async def _hung(workflow, inputs):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            swallowed.append(1)  # a client that eats the cancel and keeps waiting…
            await asyncio.sleep(2)  # …long past the cap (wait_for would have waited this out)
        return "a verdict nobody is waiting for any more"

    _inject_runner(monkeypatch, _hung)
    loop = BoardLoop({"review_gate": True, "review_gate_timeout_s": 0.3})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: None)
    t0 = time.monotonic()
    output, why = await asyncio.wait_for(loop._run_review_workflow("ds-1", "https://github.com/o/r/pull/7"), 5)
    assert time.monotonic() - t0 < 1.5  # the cap, not the 2 s the call spent ignoring its cancel
    assert output is None and "review_gate_timeout_s=0.3" in why
    await asyncio.sleep(2.2)  # let the abandoned call finish before the test's loop closes
    assert swallowed == [1]  # it was cancelled, and ignored it


def test_review_gate_timeout_default_and_floor():
    assert BoardLoop({}).review_gate_timeout == 1800.0
    assert BoardLoop({"review_gate_timeout_s": 120}).review_gate_timeout == 120.0
    assert BoardLoop({"review_gate_timeout_s": 0}).review_gate_timeout == 1800.0  # not disableable
    assert BoardLoop({"review_gate_timeout_s": "junk"}).review_gate_timeout == 1800.0


# ── #462: a stalled claim scan is a health gap on /status ─────────────────────────────


class _Reporter:
    def __init__(self):
        self.calls = []

    def report_setup_gap(self, key, message, *, label=None):
        self.calls.append((key, message))


def _stall_loop(**cfg):
    reg = _Reporter()
    loop = BoardLoop(
        {"loop_enabled": True, "loop_interval_s": 30, "max_concurrent": 2, "claim_stall_ticks": 4, **cfg},
        gap_reporter=setup_check.GapReporter(reg),
    )
    return loop, reg


def test_a_stuck_tick_with_ready_work_reports_a_claim_stall_naming_the_phase():
    loop, reg = _stall_loop()
    now = time.monotonic()
    loop._ready_count = 32
    loop._last_claim_at = now - 600  # 20 ticks
    loop._tick_phase_now = ("PR reconcile", now - 590)
    try:
        loop._check_claim_stall()
        hint = health.claim_stall_hint()
        assert "32 ready card(s)" in hint and "0/2 drive slot(s)" in hint and "PR reconcile phase" in hint
        assert setup_check.setup_status({})["claim_stall_hint"] == hint
        assert reg.calls == [(setup_check.CLAIM_STALL_KEY, hint)]
        loop._check_claim_stall()  # steady state: nothing re-sent
        assert len(reg.calls) == 1
        loop._last_claim_at = time.monotonic()  # the claim scan ran again
        loop._check_claim_stall()
        assert health.claim_stall_hint() == ""
        assert reg.calls[-1] == (setup_check.CLAIM_STALL_KEY, None)
    finally:
        health.publish_claim_stall("")


@pytest.mark.parametrize(
    "setup",
    [
        lambda loop: setattr(loop, "_ready_count", 0),  # nothing to claim
        lambda loop: setattr(loop, "_drives", {object(), object()}),  # every slot busy
        lambda loop: setattr(loop, "_last_claim_at", time.monotonic() - 60),  # only 2 ticks
        lambda loop: setattr(loop, "_setup_paused", True),  # the setup gate's own gap says why
        lambda loop: setattr(loop, "claim_stall_ticks", 0),  # signal off
    ],
)
def test_no_claim_stall_when_the_board_is_not_stuck(setup):
    loop, _reg = _stall_loop()
    loop._ready_count = 5
    loop._last_claim_at = time.monotonic() - 3600
    setup(loop)
    assert loop._claim_stall_reason() == ""


async def test_the_tick_records_each_finished_claim_scan(monkeypatch):
    store = _TickStore()
    loop, _drove = _tick_loop(monkeypatch, store)

    async def _quick(store_, f, **_kw):
        return None

    monkeypatch.setattr(loop, "_reconcile_pr", _quick)
    assert loop._last_claim_at is None
    await loop._tick()
    assert loop._last_claim_at is not None and loop._tick_phase_now is None
    await _cancel_cards(loop)


# ── #471 review: serial within a repo, re-read after queueing, bounded side effects ──────

X = "a" * 40
Y = "b" * 40


class _ReviewStore:
    def __init__(self, cards):
        self.cards = {c["id"]: c for c in cards}

    def list_features(self, state=None, **_kw):
        return [dict(c) for c in self.cards.values() if c["board_state"] == state]

    def get_feature(self, fid):
        c = self.cards.get(fid)
        return dict(c) if c else None

    def requeue(self, fid):
        self.cards[fid]["board_state"] = "ready"

    def __getattr__(self, name):  # any other bookkeeping write is a no-op
        return lambda *a, **k: None


def _in_review(n, **extra):
    return {
        "id": f"ds-{n}",
        "board_state": "in_review",
        "pr_url": f"https://github.com/o/r/pull/{n}",
        "labels": [],
        "title": f"t{n}",
        **extra,
    }


async def test_sibling_prs_of_one_repo_never_merge_on_a_stamp_the_other_merge_made_stale(monkeypatch):
    """B1: two in_review PRs in ONE repo, both stamped merged-verified against base X. The
    first merge moves base to Y; the second must not merge on its X verdict (#131). Two
    concurrent card tasks did, because the stamp check and the merge are gh round trips
    apart. One repo's reconciles are serial again."""
    store = _ReviewStore([_in_review(n, labels=[f"merged-verified:{X[:12]}"]) for n in (1, 2)])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    base, merged = {"sha": X}, []

    async def pr_state(url, cwd="."):
        return "OPEN"

    async def pr_merge_state(url, cwd="."):
        return "CLEAN"

    async def origin_head_sha(repo, ref):
        await asyncio.sleep(0.01)
        return base["sha"]

    async def pr_merge_info(url, cwd="."):
        await asyncio.sleep(0.2)  # one gh round trip, during which the sibling merges
        return {"mergeStateStatus": "CLEAN", "isDraft": False}

    async def merge_pr(url, method="squash", cwd=".", expected_head=""):
        merged.append(("ds-" + url.rsplit("/", 1)[1], base["sha"]))
        base["sha"] = Y
        return True, ""

    async def merged_state_worktree(*a, **k):
        return ("error", "no real repo here")

    async def _noop(*a, **k):
        return True

    for name, fn in {
        "pr_state": pr_state,
        "pr_merge_state": pr_merge_state,
        "origin_head_sha": origin_head_sha,
        "pr_merge_info": pr_merge_info,
        "merge_pr": merge_pr,
        "merged_state_worktree": merged_state_worktree,
        "reap_feature_worktree": _noop,
        "delete_remote_branch": _noop,
    }.items():
        monkeypatch.setattr(worktree, name, fn)
    loop = BoardLoop(
        {
            "coder": "proto",
            "auto_merge": True,
            "auto_rebase": True,
            "ci_poll": False,
            "review_gate": False,
            "local_gate_cmd": "true",
            "reconcile_concurrency": 2,
        }
    )

    async def _no_freeze(*a, **k):
        return ""

    monkeypatch.setattr(loop, "_release_freeze_evidence", _no_freeze)
    await loop._reconcile_prs()
    assert len(merged) == 1 and merged[0][1] == X, merged  # the second held on its stale stamp


async def test_a_queued_card_task_never_rebases_a_card_a_drive_now_owns(monkeypatch):
    """M1: ds-2's reconcile queues behind ds-1's slow one. Meanwhile ds-2 is requeued and
    claimed (a live fix-round drive). When it gets its turn it is re-read, and it must not
    force-push a rebase of the branch the drive is working on."""
    store = _ReviewStore([_in_review(1), _in_review(2)])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    gate, rebased = asyncio.Event(), []

    async def pr_state(url, cwd="."):
        if url.endswith("/1"):
            await gate.wait()  # ds-1: a slow gate holding the only slot
        return "OPEN"

    async def pr_merge_state(url, cwd="."):
        return "BEHIND"

    async def rebase_onto_base(repo, branch, base, root=".worktrees"):
        rebased.append(branch)
        return ("clean", "")

    monkeypatch.setattr(worktree, "pr_state", pr_state)
    monkeypatch.setattr(worktree, "pr_merge_state", pr_merge_state)
    monkeypatch.setattr(worktree, "rebase_onto_base", rebase_onto_base)
    loop = BoardLoop(
        {
            "coder": "proto",
            "auto_rebase": True,
            "ci_poll": False,
            "review_gate": False,
            "auto_merge": False,
            "reconcile_concurrency": 1,
        }
    )
    await loop._reconcile_prs(detach=True)
    await asyncio.sleep(0.05)
    store.cards["ds-2"]["board_state"] = "in_progress"  # requeued and claimed meanwhile
    drive = asyncio.create_task(asyncio.Event().wait())
    _register_drive("ds-2", drive)
    try:
        gate.set()
        await asyncio.gather(*loop._card_tasks.values(), return_exceptions=True)
        assert not any("ds-2" in b for b in rebased), rebased
        assert any("ds-1" in b for b in rebased)  # the card still in review was served
    finally:
        _unregister_drive("ds-2", drive)
        drive.cancel()
        await asyncio.gather(drive, return_exceptions=True)


async def test_a_merged_pr_is_settled_without_queueing_behind_a_hung_review(monkeypatch):
    """Minor 2: ds-1's review hangs in the only slot; ds-2's PR merged. ds-2 must reach done
    now, not after the review cap."""
    store = _ReviewStore([_in_review(1), _in_review(2)])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    settled = []

    async def pr_state(url, cwd="."):
        return "MERGED" if url.endswith("/2") else "OPEN"

    monkeypatch.setattr(worktree, "pr_state", pr_state)
    loop = BoardLoop({"coder": "proto", "reconcile_concurrency": 1})
    hung = asyncio.Event()
    real = loop._reconcile_pr

    async def _body(store_, f, **kw):
        if f["id"] == "ds-1":
            await hung.wait()  # a review call that never returns
            return
        settled.append((f["id"], kw.get("known_state")))
        return await real(store_, f, **kw)

    monkeypatch.setattr(loop, "_reconcile_pr", _body)
    try:
        await loop._reconcile_prs(detach=True)
        await asyncio.wait_for(loop._card_tasks["ds-2"], timeout=5)
        assert settled == [("ds-2", "MERGED")]
    finally:
        await _cancel_cards(loop)


async def test_a_card_tasks_store_stall_ends_the_next_tick(monkeypatch):
    """Minor 4: #404's "the first store stall ends the tick" holds for detached card work."""
    from project_board import store as store_mod

    store = _TickStore()
    loop, drove = _tick_loop(monkeypatch, store)

    async def _stall(store_, f, **_kw):
        raise store_mod.BoardTimeout("br show timed out after 1s")

    monkeypatch.setattr(loop, "_reconcile_pr", _stall)
    await loop._tick()  # starts the card task; the claim scan runs
    await asyncio.gather(*loop._card_tasks.values(), return_exceptions=True)
    claimed_before = list(store.claimed)
    store.claimed.clear()
    loop._last_poll = 0.0
    await loop._tick()  # this tick ends on the recorded stall
    assert store.claimed == [] and claimed_before == ["ds-new"]
    assert loop._card_stall is None  # consumed: the tick after runs normally
    await _cancel_cards(loop)


async def test_a_timed_out_review_neither_spends_the_run_budget_nor_stacks_a_second_call(monkeypatch):
    """Minor 3: an abandoned review call that is still running blocks a new review of the
    card until it returns, and a timeout is not an unrunnable review (review_run_max)."""
    release = asyncio.Event()
    calls = []

    async def _slow(workflow, inputs):
        calls.append(1)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await release.wait()  # ignores its cancel until released
        return {"output": "[]"}

    _inject_runner(monkeypatch, _slow)
    loop = BoardLoop({"review_gate": True, "review_gate_timeout_s": 0.2, "review_run_max": 1})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: None)
    store = _ReviewStore([_in_review(1)])
    budgets = []
    monkeypatch.setattr(loop, "_budget_set", lambda *a, **k: _async_append(budgets, a))

    async def _head(*a, **k):
        return X

    async def _publish(*a, **k):
        return None

    monkeypatch.setattr(worktree, "pr_head_sha", _head)
    monkeypatch.setattr(loop, "_publish_gate_verdict", _publish)
    url = "https://github.com/o/r/pull/1"
    await loop._review_gate(store, "ds-1", url, ".")
    assert len(calls) == 1 and "ds-1" in loop._review_zombies
    assert not [b for b in budgets if "review-run" in b]  # no unrunnable-review budget spent
    await loop._review_gate(store, "ds-1", url, ".")  # the zombie still runs → not re-run
    assert len(calls) == 1
    release.set()
    await asyncio.sleep(0.05)
    assert loop._review_zombies["ds-1"].done()


async def _async_append(bucket, item):
    bucket.append(item)


async def test_one_candidate_gate_failing_cancels_its_siblings(tmp_path):
    """Minor 6: Max-Mode's candidate gates run together; one tree gone must not orphan the
    other gates' real process trees."""
    live = tmp_path / "live"
    live.mkdir()
    loop = BoardLoop({"local_gate_cmd": "sleep 30", "local_gate_timeout_s": 60})
    t0 = time.monotonic()
    with pytest.raises(worktree.WorktreeMissing):
        await asyncio.wait_for(loop._gate_all([str(tmp_path / "gone"), str(live)], {}), timeout=15)
    assert time.monotonic() - t0 < 10  # the `sleep 30` sibling was cancelled, not waited out
