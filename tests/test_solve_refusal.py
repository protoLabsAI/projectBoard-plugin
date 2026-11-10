"""A best-of-k candidate's deliberate `BLOCKED-ON-<WORD>:` refusal is a claim, not a test failure.

bd-nfd6g.16 (protoAgent#5068): g1 ended `Spec repair is required … Please expand scope` with no
diff. The ladder recorded that as a plain `solve-test: no changes vs base` failure, so the
siblings saw "no changes" instead of the reasoning — and every refusal observed on the board
(bd-nfd6g.16, bd-bqyfs, bd-buhc.1/.2) was later refuted by a sibling that built the same spec.

- One refusing candidate → its refusal becomes sibling feedback asking the next candidate to
  check or refute it; the ladder carries on.
- EVERY judged candidate refusing → `SolveRefused` (a `SolveExhausted`), which the drive blocks
  once under the `design` class for the project manager: no tier climb, no retry.
- A refusal mixed with an ordinary test failure is an ordinary exhaustion.
"""

from __future__ import annotations

import os

import pytest

from project_board import coder_seam, worktree
from project_board.coder_seam import SolveExhausted, SolveRefused, dispatch
from project_board.failures import DESIGN_BLOCK_CLASS

from test_coder_seam_candidates import _DISPATCH_KW, _FakeResult, _FakeVerdict, _make_create, _noop_reap, _stub
from test_loop import _drive_with

REFUSAL = "BLOCKED-ON-SPEC: navigation intent rejects sessions absent from the local store — checked nav.ts"


def _replies(by_name: dict[str, str]):
    """A dispatch stub whose final reply is chosen per candidate worktree basename."""
    prompts: dict[str, str] = {}

    async def _fake(coder, wt, prompt, *, fid=None, gen=None, tier="", timeout=None, env_passthrough=()):
        name = os.path.basename(wt)
        prompts[name] = prompt
        return by_name.get(name, f"built it in {name}")

    return _fake, prompts


def _tests(passed_by_name: dict[str, bool]):
    async def _run(self, candidate_wt):
        name = os.path.basename(candidate_wt)
        ok = passed_by_name[name]
        self._oracle_by_wt[candidate_wt] = self.test_cmd
        out = "all tests passed" if ok else "solve-test: no changes vs abc123"
        return _FakeVerdict(passed=ok, total=1, failed=0 if ok else 1, output=out)

    return _run


def _sequential_solve(n: int, *, win_last: bool):
    async def _solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        last_wt, last = None, None
        for _ in range(n):
            last_wt = await generate(task, feedback=None)
            last = await verify(last_wt)
        if win_last:
            return _FakeResult(
                solution=last_wt, passed=True, rung="best-of-k", gens_spent=n, candidates_tried=n, verdict=last
            )
        return _FakeResult(
            solution=None, passed=False, rung="best-of-k", gens_spent=n, candidates_tried=n, verdict=last
        )

    return _solve


async def test_a_refusal_reaches_the_sibling_as_a_claim_to_check(monkeypatch):
    dispatch_fake, prompts = _replies({"feat-bd-9.g1": "I stopped.\n" + REFUSAL})
    _stub(
        monkeypatch,
        dispatch_fake=dispatch_fake,
        run_tests=_tests({"feat-bd-9.g1": False, "feat-bd-9.g2": True}),
        create_fake=_make_create()[0],
    )
    await dispatch(task="t", fid="bd-9", _solve=_sequential_solve(2, win_last=True), **_DISPATCH_KW)

    sibling = prompts["feat-bd-9.g2"]
    assert "stopped and declared the task unbuildable" in sibling
    assert "navigation intent rejects sessions absent from the local store" in sibling
    assert "claim to check, not a fact" in sibling
    # the bare "no changes" tail is NOT what the sibling learns from a refusal
    assert "no changes vs" not in sibling


async def test_every_candidate_refusing_raises_solve_refused(monkeypatch):
    dispatch_fake, _ = _replies({f"feat-bd-9.g{n}": "Can't.\n" + REFUSAL for n in (1, 2, 3)})
    _stub(
        monkeypatch,
        dispatch_fake=dispatch_fake,
        run_tests=_tests({f"feat-bd-9.g{n}": False for n in (1, 2, 3)}),
        create_fake=_make_create()[0],
    )
    monkeypatch.setattr(worktree, "reap_candidate_branches", _noop_reap)
    with pytest.raises(SolveRefused) as ei:
        await dispatch(task="t", fid="bd-9", _solve=_sequential_solve(3, win_last=False), **_DISPATCH_KW)
    assert isinstance(ei.value, SolveExhausted)  # the loop's existing capability catch still sees it
    assert ei.value.refusal.startswith("BLOCKED-ON-SPEC:")


async def test_a_refusal_mixed_with_a_test_failure_is_an_ordinary_exhaustion(monkeypatch):
    dispatch_fake, _ = _replies({"feat-bd-9.g1": REFUSAL})  # g2 replies normally, then fails tests
    _stub(
        monkeypatch,
        dispatch_fake=dispatch_fake,
        run_tests=_tests({"feat-bd-9.g1": False, "feat-bd-9.g2": False}),
        create_fake=_make_create()[0],
    )
    monkeypatch.setattr(worktree, "reap_candidate_branches", _noop_reap)
    with pytest.raises(SolveExhausted) as ei:
        await dispatch(task="t", fid="bd-9", _solve=_sequential_solve(2, win_last=False), **_DISPATCH_KW)
    assert not isinstance(ei.value, SolveRefused)


async def test_drive_blocks_an_all_refused_solve_for_the_pm_without_climbing(monkeypatch):
    async def _refused(**kw):
        raise SolveRefused("coder.solve: all 3 generation(s) refused the spec: " + REFUSAL, refusal=REFUSAL)

    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())
    monkeypatch.setattr(coder_seam, "dispatch", _refused)

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True):
        raise AssertionError("no PR on a refused spec")

    loop, store = await _drive_with(
        monkeypatch, open_pr=_open_pr, cfg={"coder": "proto", "local_gate_cmd": "pytest -q"}
    )
    blocked = [c for c in store.calls if c[0] == "flag_blocked"]
    assert len(blocked) == 1
    _name, _fid, reason, category = blocked[0]
    assert category == DESIGN_BLOCK_CLASS
    assert REFUSAL in reason and "every coder attempt refused" in reason
    assert [c for c in store.calls if c[0] == "record_attempt"] == []
    assert loop._inflight == {}
