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
