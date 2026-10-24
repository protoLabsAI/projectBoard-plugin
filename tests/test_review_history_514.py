"""#514 slice 1: the review gate keeps the whole card's finding history.

``_review_prior[fid]`` used to be OVERWRITTEN every round, so the re-review and the
fix prompt only ever saw the latest round — and fix rounds flipped back and forth
(bd-49yo / #513: round 2 reverted round 1's fix, round 3 asked for it back). It is
now an append-only list of ``{round, head, findings}`` rounds: the re-review is
handed the whole history as prior_findings, and the fix prompt leads with every
round under a satisfy-ALL instruction. A legacy single-round value (a bare findings
list) is still read, and the history clears wherever the single prior used to pop.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from project_board import worktree
from project_board.loop import BoardLoop

from tests.test_loop import _GateStore, _ReconcileStore, _inject_fake_findings


def _blocker(claim: str, *, file: str = "a.py", line: int = 3) -> str:
    """A one-finding blocker JSON array; `evidence` is short so grounding fails OPEN
    (the #381 guard only demotes a quote the diff demonstrably lacks)."""
    return json.dumps(
        [{"file": file, "line": line, "severity": "blocker", "claim": claim, "evidence": "x", "verdict": "confirmed"}]
    )


@pytest.fixture(autouse=True)
def _pin_gate_seams(monkeypatch):
    """This module has none of test_loop.py's module-local autouse guards, so pin the
    seams the gate touches: the reviewed-head read (default unreadable "") and the #354
    publication seam (commit status + findings comment)."""

    async def _blank_head(pr_url, *, cwd="."):
        return ""

    async def _status(repo_slug, head_sha, *, state, description, target_url="", context="", cwd="."):
        return True

    async def _comment(pr_url, body, *, marker="", cwd="."):
        return True

    monkeypatch.setattr(worktree, "pr_head_sha", _blank_head)
    monkeypatch.setattr(worktree, "post_review_status", _status)
    monkeypatch.setattr(worktree, "post_or_update_pr_comment", _comment)


def _seq_loop(monkeypatch, outputs, *, cfg=None):
    """A review_gate loop whose ``_run_review_workflow`` returns `outputs` in order
    (the last repeats), with the PR-diff stubbed to a short diff findings ground in."""
    loop = BoardLoop({"review_gate": True, "review_fix_max": 5, **(cfg or {})})
    idx = {"i": 0}

    async def _run(fid, pr_url):
        out = outputs[min(idx["i"], len(outputs) - 1)]
        idx["i"] += 1
        return f"review…\n```json\n{out}\n```", None

    async def _diff(pr_url, cwd=".", *, max_chars=4000):
        return "diff --git a/a.py b/a.py"

    monkeypatch.setattr(loop, "_run_review_workflow", _run)
    monkeypatch.setattr(worktree, "pr_diff", _diff)
    return loop


def _runtime_loop(monkeypatch, outputs):
    """A review_gate loop that drives the REAL ``_run_review_workflow`` over a stubbed
    host ``runtime.state.STATE.workflow_run`` — so the workflow inputs (prior_findings)
    are the ones the gate actually builds. Returns ``(loop, seen_inputs)``."""
    seen: list[dict] = []
    idx = {"i": 0}

    async def _runner(name, inputs):
        seen.append(dict(inputs))
        out = outputs[min(idx["i"], len(outputs) - 1)]
        idx["i"] += 1
        return {"output": f"review…\n```json\n{out}\n```", "steps": {}, "failed": []}

    rt = types.ModuleType("runtime")
    rt_state = types.ModuleType("runtime.state")
    rt_state.STATE = types.SimpleNamespace(workflow_run=_runner)
    rt.state = rt_state
    monkeypatch.setitem(sys.modules, "runtime", rt)
    monkeypatch.setitem(sys.modules, "runtime.state", rt_state)

    async def _diff(pr_url, cwd=".", *, max_chars=4000):
        return "diff --git a/a.py b/a.py"

    monkeypatch.setattr(worktree, "pr_diff", _diff)
    loop = BoardLoop({"review_gate": True, "review_fix_max": 5})
    return loop, seen


_PR = "https://github.com/o/r/pull/9"


async def test_two_adverse_rounds_record_both_rounds_with_their_heads(monkeypatch):
    """r1: after two adverse rounds the history holds BOTH rounds, each stamped with the
    head it reviewed — not just the latest round (which is all the overwrite ever kept)."""
    _inject_fake_findings(monkeypatch)
    loop = _seq_loop(monkeypatch, [_blocker("round one finding"), _blocker("round two finding")])

    heads = iter(["head1aaaaaaaa", "head2bbbbbbbb"])

    async def _head(pr_url, *, cwd="."):
        return next(heads)

    monkeypatch.setattr(worktree, "pr_head_sha", _head)

    store = _GateStore()
    await loop._review_gate(store, "bd-1", _PR, "/repo")
    await loop._review_gate(store, "bd-1", _PR, "/repo")

    history = loop._review_history("bd-1")
    assert [r["round"] for r in history] == [1, 2]
    assert [r["head"] for r in history] == ["head1aaaaaaaa", "head2bbbbbbbb"]
    assert history[0]["findings"][0]["claim"] == "round one finding"
    assert history[1]["findings"][0]["claim"] == "round two finding"


async def test_re_review_receives_every_rounds_findings_as_priors(monkeypatch):
    """r2: the third review's prior_findings carries BOTH prior rounds, round-marked —
    so a delta re-review can tell a fixed finding from one an earlier fix regressed."""
    _inject_fake_findings(monkeypatch)
    loop, seen = _runtime_loop(monkeypatch, [_blocker("alpha broke"), _blocker("beta broke"), _blocker("gamma broke")])

    store = _GateStore()
    await loop._review_gate(store, "bd-1", _PR, "/repo")  # round 1 — nothing prior
    await loop._review_gate(store, "bd-1", _PR, "/repo")  # round 2 — prior = {r1}
    await loop._review_gate(store, "bd-1", _PR, "/repo")  # round 3 — prior = {r1, r2}

    assert "prior_findings" not in seen[0]
    carried = json.loads(seen[2]["prior_findings"])
    assert [r["round"] for r in carried] == [1, 2]
    assert "alpha broke" in seen[2]["prior_findings"]
    assert "beta broke" in seen[2]["prior_findings"]


async def test_fix_prompt_leads_with_every_round_and_the_satisfy_all_instruction(monkeypatch):
    """r3: the retry prompt contains BOTH rounds' findings under 'satisfy ALL of these at
    once; do not trade one for another' — not just the latest round."""
    _inject_fake_findings(monkeypatch)
    loop = _seq_loop(monkeypatch, [_blocker("round one finding"), _blocker("round two finding")])

    store = _GateStore()
    await loop._review_gate(store, "bd-1", _PR, "/repo")
    await loop._review_gate(store, "bd-1", _PR, "/repo")

    prompt = loop._ci_feedback["bd-1"]
    assert "round one finding" in prompt  # the EARLIER round survives into the prompt
    assert "round two finding" in prompt
    assert "Satisfy ALL of these at once" in prompt
    assert "do not trade one for another" in prompt
    assert "### Review round 1" in prompt and "### Review round 2" in prompt


async def test_history_is_cleared_on_merge_and_on_close(monkeypatch):
    """r4: the history lives in ``_review_prior``, so the merge and closed-unmerged edges
    that pop the single prior today clear the whole history too."""
    store = _ReconcileStore(
        [
            {"id": "bd-m", "pr_url": "https://example/pr/1"},
            {"id": "bd-c", "pr_url": "https://example/pr/2"},
        ]
    )
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    states = {"https://example/pr/1": "MERGED", "https://example/pr/2": "CLOSED"}

    async def _pr_state(url, *, cwd="."):
        return states[url]

    async def _reap(repo, root, fid):
        return None

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    loop = BoardLoop({})
    loop._review_prior["bd-m"] = json.dumps([{"round": 1, "head": "h", "findings": [{"claim": "x"}]}])
    loop._review_prior["bd-c"] = json.dumps([{"round": 1, "head": "h", "findings": [{"claim": "y"}]}])

    await loop._reconcile_prs()

    assert "bd-m" not in loop._review_prior  # merge edge cleared it
    assert "bd-c" not in loop._review_prior  # closed-unmerged edge cleared it


def test_legacy_single_round_value_is_read_as_round_one():
    """r4: a value a pre-#514 process left behind — a bare findings list, no round
    wrapper — is still read, wrapped as round 1."""
    loop = BoardLoop({})
    loop._review_prior["bd-1"] = json.dumps(
        [{"file": "a.py", "line": 3, "severity": "major", "claim": "legacy finding", "verdict": "confirmed"}]
    )
    history = loop._review_history("bd-1")
    assert [r["round"] for r in history] == [1]
    assert history[0]["head"] == ""
    assert history[0]["findings"][0]["claim"] == "legacy finding"
    # …and it renders into the fix prompt like any other round.
    assert "legacy finding" in loop._render_review_history("bd-1")


async def test_legacy_prior_rides_into_the_re_review(monkeypatch):
    """r4: the first re-review after a legacy single-round value receives that value as a
    round-marked prior, then the new round is appended in the #514 shape."""
    _inject_fake_findings(monkeypatch)
    loop, seen = _runtime_loop(monkeypatch, [_blocker("fresh finding")])
    loop._review_prior["bd-1"] = json.dumps(
        [{"file": "a.py", "line": 3, "severity": "major", "claim": "legacy finding", "verdict": "confirmed"}]
    )

    store = _GateStore()
    await loop._review_gate(store, "bd-1", _PR, "/repo")

    assert "legacy finding" in seen[0]["prior_findings"]  # the legacy value was read
    carried = json.loads(seen[0]["prior_findings"])
    assert carried[0]["round"] == 1  # wrapped as round 1
    # the gate then appended THIS round in the new shape → history is now two rounds.
    history = loop._review_history("bd-1")
    assert [r["round"] for r in history] == [1, 2]
    assert history[1]["findings"][0]["claim"] == "fresh finding"
