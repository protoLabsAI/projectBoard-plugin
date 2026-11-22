"""#483: a local gate that TIMES OUT is a fail-open "pass" that verified nothing. Re-running
it on every base move, one card at a time under the per-repo lock, held protoAgent PRs for
hours (a 600 s gate x ~6 in-review cards per base move). Once it has timed out, the
merged-state re-verify stamps the base as a timed-out run would, without running it again,
until the command or the timeout changes, or the gate is seen to finish."""

from __future__ import annotations

import pytest

from project_board import health, worktree
from project_board.loop import BoardLoop
from tests.test_loop import _aret, _VerifyStore, _vloop

pytestmark = pytest.mark.asyncio


async def test_a_real_gate_that_times_out_is_remembered_and_one_that_finishes_clears_it(tmp_path):
    marker = tmp_path / "fast"
    cmd = f"test -f {marker} || sleep 5"  # slow until the marker exists
    loop = BoardLoop({"coder": "proto", "local_gate_cmd": cmd, "local_gate_timeout_s": 0.3})
    feature = {"id": "bd-1", "labels": []}
    gate = loop._local_gate_cmd_for(feature)
    assert await loop._run_local_gate(str(tmp_path), feature) is None  # timed out → fail-open pass
    assert loop._gate_known_slow(feature, gate)
    assert "timed out" in health.advisory_hint() and "local_gate_cmd" in health.advisory_hint()
    assert not loop._gate_known_slow(feature, "true")  # a different command is not known slow
    marker.write_text("")
    assert await loop._run_local_gate(str(tmp_path), feature) is None  # now it finishes
    assert loop._slow_gates == {}
    assert "timed out" not in health.advisory_hint()


async def test_merged_verify_skips_a_known_slow_gate_but_still_stamps(monkeypatch):
    monkeypatch.setattr(worktree, "origin_head_sha", _aret("def456abcdef99"))
    built = []
    monkeypatch.setattr(worktree, "merged_state_worktree", lambda *a, **k: built.append(1))
    store = _VerifyStore({"id": "bd-1"})
    loop = _vloop()
    feature = {"id": "bd-1", "labels": ["merged-verified:oldsha"]}
    loop._note_gate_speed(feature, loop._local_gate_cmd_for(feature), timed_out=True)
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
    assert built == []  # no merged tree, no 10-minute gate under the repo lock
    assert store.verified == [("bd-1", "def456abcdef")]  # the merge edge isn't held on a stale stamp
    assert await loop._budget_get(store, "bd-1", "merged-verify", feature) == 0  # nothing ran, nothing spent


async def test_a_changed_timeout_rearms_the_gate(monkeypatch):
    loop = _vloop()
    feature = {"id": "bd-1", "labels": []}
    cmd = loop._local_gate_cmd_for(feature)
    loop._note_gate_speed(feature, cmd, timed_out=True)
    assert loop._gate_known_slow(feature, cmd)
    loop.local_gate_timeout = loop.local_gate_timeout * 2  # operator raised it
    assert not loop._gate_known_slow(feature, cmd)


def _no_gate_runs(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("the stale-entry check must not run the gate")

    monkeypatch.setattr(BoardLoop, "_run_local_gate", boom)


async def test_a_raised_timeout_clears_the_slow_gate_advisory_without_a_gate_run(monkeypatch):
    """#728: an always-slow gate never finishes, so only a config change can clear it."""
    _no_gate_runs(monkeypatch)
    loop = _vloop()
    feature = {"id": "bd-1", "labels": []}
    project = loop._project_name(feature)
    loop._note_gate_speed(feature, loop._local_gate_cmd_for(feature), timed_out=True)
    assert project in health.slow_gates_snapshot()
    assert "timed out" in health.advisory_hint()
    loop.local_gate_timeout = loop.local_gate_timeout * 2  # operator raised local_gate_timeout_s
    assert loop._forget_stale_slow_gates() is True
    assert loop._slow_gates == {}
    assert project not in health.slow_gates_snapshot()
    assert "timed out" not in health.advisory_hint()
    assert loop._forget_stale_slow_gates() is False  # nothing left to change


async def test_a_changed_gate_command_clears_the_slow_gate_advisory_without_a_gate_run(monkeypatch):
    _no_gate_runs(monkeypatch)
    loop = _vloop()
    feature = {"id": "bd-1", "labels": []}
    project = loop._project_name(feature)
    loop._note_gate_speed(feature, loop._local_gate_cmd_for(feature), timed_out=True)
    # The operator changed local_gate_cmd; the loop restarts with it (a restart knob).
    restarted = _vloop(local_gate_cmd="pytest -q -x tests/fast")
    assert restarted._local_gate_cmd_for(feature) != loop._slow_gates[project]["cmd"]
    assert restarted._forget_stale_slow_gates() is True
    assert project not in health.slow_gates_snapshot()
    assert "timed out" not in health.advisory_hint()
    # The same check on the loop that recorded it, with its project's command changed in place.
    loop._projects[project] = {**loop._projects[project], "local_gate_cmd": "pytest -q -x tests/fast"}
    assert loop._forget_stale_slow_gates() is False  # already unpublished
    assert loop._slow_gates == {}


async def test_an_unchanged_slow_gate_stays_and_the_merged_verify_still_skips_it(monkeypatch):
    monkeypatch.setattr(worktree, "origin_head_sha", _aret("def456abcdef99"))
    built = []
    monkeypatch.setattr(worktree, "merged_state_worktree", lambda *a, **k: built.append(1))
    store = _VerifyStore({"id": "bd-1"})
    loop = _vloop()
    feature = {"id": "bd-1", "labels": ["merged-verified:oldsha"]}
    loop._note_gate_speed(feature, loop._local_gate_cmd_for(feature), timed_out=True)
    before = health.slow_gates_snapshot()
    assert loop._forget_stale_slow_gates() is False
    assert health.slow_gates_snapshot() == before and "timed out" in health.advisory_hint()
    assert await loop._verify_merged_state(store, feature, "pr", "/repo") is False
    assert built == []


async def test_a_restarted_loop_with_a_fixed_gate_clears_the_entry_an_earlier_loop_published(monkeypatch):
    """local_gate_cmd / local_gate_timeout_s are restart knobs, and the health state outlives
    the loop: the new loop's check clears the old entry, but keeps one still accurate."""
    _no_gate_runs(monkeypatch)
    old = _vloop(local_gate_timeout_s=60)
    feature = {"id": "bd-1", "labels": []}
    old._note_gate_speed(feature, old._local_gate_cmd_for(feature), timed_out=True)
    same = _vloop(local_gate_timeout_s=60)
    assert same._forget_stale_slow_gates() is False
    assert old._project_name(feature) in health.slow_gates_snapshot()
    fixed = _vloop(local_gate_timeout_s=1200)
    assert fixed._forget_stale_slow_gates() is True
    assert health.slow_gates_snapshot() == {}


async def test_a_project_gate_change_on_reload_clears_its_slow_gate_entry(monkeypatch):
    _no_gate_runs(monkeypatch)
    loop = _vloop(projects={"app": {"repo": "/repo", "local_gate_cmd": "make slow"}}, default_project="app")
    feature = {"id": "bd-1", "project": "app", "labels": []}
    loop._note_gate_speed(feature, loop._local_gate_cmd_for(feature), timed_out=True)
    assert "app" in health.slow_gates_snapshot()
    loop.reload({"projects": {"app": {"repo": "/repo", "local_gate_cmd": "make fast"}}, "default_project": "app"})
    assert loop._local_gate_cmd_for(feature) == "make fast"
    assert "app" not in health.slow_gates_snapshot()
    assert "timed out" not in health.advisory_hint()


async def test_each_tick_runs_the_stale_entry_check(monkeypatch):
    loop = _vloop()
    calls = []
    monkeypatch.setattr(loop, "_forget_stale_slow_gates", lambda: calls.append(1))

    async def ok():
        return None

    for name in ("_maybe_reconcile", "_maybe_sweep", "_host_disk_tick", "_maybe_preflight", "_spawn_ready"):
        monkeypatch.setattr(loop, name, ok)
    await loop._tick()
    assert calls == [1]
