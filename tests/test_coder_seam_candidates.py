"""Regression tests for bd-gzl4: a candidate whose `.g<n>` worktree goes MISSING between
create and dispatch must not sink the whole `coder.solve` ladder.

Two defects observed on card bd-gzl4 (protoContent):
  1. A candidate was dispatched into a worktree that no longer existed —
     `coder dispatch failed: workdir does not exist: …/.worktrees/feat-bd-gzl4.g3`.
  2. That ONE candidate's infrastructure failure propagated out of `solve()` (which wraps
     neither `generate()` nor `verify()`), reaped EVERY candidate — including completed,
     would-pass siblings — and escalated the model-capability tier. ~80 minutes of correct
     coder work was discarded.

The fix lives in `_WorktreeSolveAdapter`: a worktree-missing dispatch failure recreates the
tree ONCE and retries; if it still can't dispatch, it fails ONLY that candidate (verify()
fails it without running tests) instead of raising out of the ladder. A short verify tail is
also logged per failed candidate. These tests inject fakes for `plugins.coder.solve` and the
worktree ops, so none of this needs the (separately installed) `coder` plugin or a real git
repo."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import pytest

from project_board import coder_seam, worktree
from project_board.coder_seam import SolveExhausted, _VERDICT_FAIL, _WorktreeSolveAdapter, dispatch


# ── fakes standing in for plugins.coder.solve (never imported here) ─────────────


@dataclass
class _FakeVerdict:
    passed: bool
    total: int = 0
    failed: int = 0
    failing: list = field(default_factory=list)
    output: str = ""

    def feedback(self) -> str:
        return "" if self.passed else f"{self.failed}/{self.total} failing: {self.output}"


@dataclass
class _FakeResult:
    solution: str | None
    passed: bool | None
    rung: str
    gens_spent: int
    candidates_tried: int
    verdict: _FakeVerdict | None = None
    note: str = ""


class _FakeBudget:
    def __init__(self, total):
        self.total = total


def _make_create():
    """A `create_worktree` stub returning a STABLE path per candidate id, so a recreate of
    the same `.g<n>` returns exactly the path the first create did. Records every id created
    — a recreate shows up as the id appearing twice."""
    created: list[str] = []

    async def _create(repo, base, cid, root, **_kw):
        created.append(cid)
        return (f"/wt/feat-{cid}", f"feat/{cid}")

    return _create, created


def _make_dispatch(missing_rounds: dict[str, int]):
    """A `dispatch_coder_tapped` stub. ``missing_rounds[basename]`` = how many LEADING calls
    for that worktree raise the bd-gzl4 ``workdir does not exist`` error before one succeeds.
    Records each call's worktree basename in order."""
    calls: list[str] = []
    seen: dict[str, int] = {}

    async def _fake(coder, wt, prompt, *, fid=None, gen=None, tier="", timeout=None, env_passthrough=()):
        name = os.path.basename(wt)
        n = seen.get(name, 0)
        seen[name] = n + 1
        calls.append(name)
        if n < missing_rounds.get(name, 0):
            raise worktree.WorktreeError(f"coder dispatch failed: workdir does not exist: {wt}")
        return f"reply from {name}"

    return _fake, calls


def _make_run_tests(passed_by_name: dict[str, bool]):
    """A class-level `_run_acceptance_tests` stub: pass/fail per candidate worktree basename,
    with a multi-line failing output so the per-candidate verify-tail log has something to
    quote."""

    async def _run(self, candidate_wt):
        name = os.path.basename(candidate_wt)
        ok = passed_by_name[name]
        out = "all tests passed" if ok else f"FAILED {name}\nE   assert add(1, 1) == 3\nE   AssertionError"
        self._oracle_by_wt[candidate_wt] = self.test_cmd
        return _FakeVerdict(passed=ok, total=1, failed=0 if ok else 1, output=out)

    return _run


def _stub(monkeypatch, *, dispatch_fake, run_tests, create_fake=None):
    create_fake = create_fake or _make_create()[0]
    removed: list[str] = []
    promoted: list[tuple] = []

    async def _remove(repo, wt, branch=""):
        removed.append(wt)

    async def _promote(repo, src_wt, src_branch, fid, root=".worktrees", title=""):
        promoted.append((src_wt, src_branch, fid))
        return (f"/wt/feat-{fid}", f"feat/{fid}")

    monkeypatch.setattr(coder_seam, "dispatch_coder_tapped", dispatch_fake)
    monkeypatch.setattr(worktree, "create_worktree", create_fake)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "promote_worktree", _promote)
    monkeypatch.setattr(_WorktreeSolveAdapter, "_run_acceptance_tests", run_tests)
    return removed, promoted


_DISPATCH_KW = dict(
    coder=object(),
    repo="/repo",
    base="main",
    root=".worktrees",
    dispatch_timeout=None,
    test_cmd="pytest -q",
    test_timeout=30,
    budget=6,
    k=3,
    tree_depth=2,
    _budget_cls=_FakeBudget,
    _verdict_cls=_FakeVerdict,
)


# ── defect 1 + 3: the mandated 3-sequential regression (first two fail verify) ──────


async def test_missing_g3_worktree_is_recreated_once_and_the_ladder_survives(monkeypatch):
    """Three candidates run in sequence; the first two fail verify so the ladder reaches the
    third, whose worktree is MISSING at dispatch. It is recreated ONCE and the retry wins —
    the vanished tree never sinks the ladder, and its siblings are reaped only as ordinary
    losers at the end."""
    create_fake, created = _make_create()
    dispatch_fake, calls = _make_dispatch({"feat-bd-9.g3": 1})  # g3's first dispatch 404s, retry works
    run_tests = _make_run_tests({"feat-bd-9.g1": False, "feat-bd-9.g2": False, "feat-bd-9.g3": True})
    removed, promoted = _stub(monkeypatch, dispatch_fake=dispatch_fake, run_tests=run_tests, create_fake=create_fake)

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        c1 = await generate(task, feedback=None)
        assert not (await verify(c1)).passed
        c2 = await generate(task, feedback="g1 failed")
        assert not (await verify(c2)).passed
        c3 = await generate(task, feedback="g1,g2 failed")
        v3 = await verify(c3)
        assert v3.passed
        return _FakeResult(solution=c3, passed=True, rung="tree-search", gens_spent=3, candidates_tried=3, verdict=v3)

    wt, branch, result = await dispatch(task="t", fid="bd-9", _solve=_fake_solve, **_DISPATCH_KW)

    # g3 was created TWICE (original + the one recreate), g1/g2 once each.
    assert created == ["bd-9.g1", "bd-9.g2", "bd-9.g3", "bd-9.g3"]
    # g3 was dispatched twice (the 404, then the recreated retry); nothing else re-dispatched.
    assert calls == ["feat-bd-9.g1", "feat-bd-9.g2", "feat-bd-9.g3", "feat-bd-9.g3"]
    # The recreated g3 won and was promoted; only the two losing siblings were reaped.
    assert promoted == [("/wt/feat-bd-9.g3", "feat/bd-9.g3", "bd-9")]
    assert set(removed) == {"/wt/feat-bd-9.g1", "/wt/feat-bd-9.g2"}
    assert (wt, branch) == ("/wt/feat-bd-9", "feat/bd-9")
    assert result == "reply from feat-bd-9.g3"


async def test_failed_candidate_verify_reason_is_logged(monkeypatch, caplog):
    """Each failed candidate's verify output is surfaced to agent.log (bd-gzl4: it was
    invisible there before — only the prompt-feedback accumulator and the live monitor saw
    it)."""
    create_fake, _created = _make_create()
    dispatch_fake, _calls = _make_dispatch({})
    run_tests = _make_run_tests({"feat-bd-9.g1": False, "feat-bd-9.g2": True})
    _stub(monkeypatch, dispatch_fake=dispatch_fake, run_tests=run_tests, create_fake=create_fake)

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        c1 = await generate(task, feedback=None)
        await verify(c1)
        c2 = await generate(task, feedback="g1 failed")
        v2 = await verify(c2)
        return _FakeResult(solution=c2, passed=True, rung="best-of-k", gens_spent=2, candidates_tried=2, verdict=v2)

    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        await dispatch(task="t", fid="bd-9", _solve=_fake_solve, **_DISPATCH_KW)

    failed_logs = [r.getMessage() for r in caplog.records if "FAILED verify" in r.getMessage()]
    assert any("g1" in m for m in failed_logs)
    # The tail of the failing output is quoted so an operator can see WHY it failed.
    assert any("AssertionError" in m for m in failed_logs)
    # Only the failing candidate is logged, not the passing one.
    assert not any("g2" in m for m in failed_logs)


# ── defect 2: one candidate's infra failure never discards siblings or escalates ────


async def test_persistent_missing_worktree_fails_only_that_candidate(monkeypatch):
    """When a candidate's worktree stays missing even after the one recreate, that candidate
    alone fails — a completed sibling still wins and is promoted, and `dispatch()` returns
    normally (no ``coder dispatch failed`` WorktreeError to escalate the tier)."""
    create_fake, created = _make_create()
    dispatch_fake, calls = _make_dispatch({"feat-bd-9.g3": 2})  # 404 on BOTH the first try and the recreate
    # g3 never reaches _run_acceptance_tests (verify short-circuits it), so no entry needed.
    run_tests = _make_run_tests({"feat-bd-9.g1": True, "feat-bd-9.g2": False})
    removed, promoted = _stub(monkeypatch, dispatch_fake=dispatch_fake, run_tests=run_tests, create_fake=create_fake)

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        c1 = await generate(task, feedback=None)
        v1 = await verify(c1)
        assert v1.passed  # a completed sibling that passed
        c2 = await generate(task, feedback=None)
        assert not (await verify(c2)).passed
        c3 = await generate(task, feedback=None)  # worktree missing + un-recreatable
        v3 = await verify(c3)
        assert not v3.passed  # failed as an infra miss, never ran the oracle
        assert "not dispatched" in v3.output
        return _FakeResult(solution=c1, passed=True, rung="best-of-k", gens_spent=3, candidates_tried=3, verdict=v1)

    wt, branch, result = await dispatch(task="t", fid="bd-9", _solve=_fake_solve, **_DISPATCH_KW)

    # g3 was dispatched exactly twice (first + the single recreate), then given up — not looped.
    assert calls.count("feat-bd-9.g3") == 2
    assert created.count("bd-9.g3") == 2  # recreated exactly once
    # The completed sibling g1 won and was promoted; g1's tree was NOT reaped by an infra error.
    assert promoted == [("/wt/feat-bd-9.g1", "feat/bd-9.g1", "bd-9")]
    assert "/wt/feat-bd-9.g1" not in removed
    assert {"/wt/feat-bd-9.g2", "/wt/feat-bd-9.g3"} <= set(removed)
    assert (wt, branch) == ("/wt/feat-bd-9", "feat/bd-9")


async def test_all_candidates_missing_surfaces_as_solve_exhausted_not_raw_dispatch_error(monkeypatch):
    """Even in the pathological case where EVERY candidate's worktree is missing and
    un-recreatable, the mid-ladder raw ``coder dispatch failed: workdir does not exist``
    WorktreeError no longer escapes `solve()`. The search exhausts as an ordinary
    ``SolveExhausted`` — the capability-exhaustion path the loop already handles — so the
    drive's ``str(exc).startswith("coder dispatch failed")`` branch (which burned the tier
    ladder in seconds on bd-gzl4) is never taken."""
    create_fake, _created = _make_create()
    dispatch_fake, _calls = _make_dispatch({f"feat-bd-9.g{n}": 2 for n in range(1, 9)})
    run_tests = _make_run_tests({})  # never reached — every candidate is an infra miss

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        last = None
        for _ in range(2):
            c = await generate(task, feedback=None)
            last = await verify(c)
        return _FakeResult(
            solution=None, passed=False, rung="best-of-k", gens_spent=2, candidates_tried=2, verdict=last
        )

    monkeypatch.setattr(worktree, "reap_candidate_branches", _noop_reap)
    _stub(monkeypatch, dispatch_fake=dispatch_fake, run_tests=run_tests, create_fake=create_fake)

    with pytest.raises(SolveExhausted) as ei:
        await dispatch(task="t", fid="bd-9", _solve=_fake_solve, **_DISPATCH_KW)
    # It is the graceful capability-exhaustion message, NOT the raw dispatch-failed prefix the
    # drive escalates on — even though the exhaustion detail may quote the last candidate's reason.
    assert not str(ei.value).startswith("coder dispatch failed")
    assert str(ei.value).startswith("coder.solve exhausted")


# ── scoping: only worktree-missing errors are swallowed; everything else propagates ──


async def test_provider_dispatch_error_is_not_swallowed(monkeypatch):
    """A NON-worktree dispatch failure (a provider rate-limit / refusal) must still propagate
    out of `generate()` so the drive's provider rotation and capability classification fire —
    the recreate-and-retry path is scoped to worktree-missing errors only."""

    async def _dispatch_ratelimited(
        coder, wt, prompt, *, fid=None, gen=None, tier="", timeout=None, env_passthrough=()
    ):
        raise worktree.WorktreeError("coder dispatch failed: 429 rate limit exceeded, retry later")

    create_fake, created = _make_create()
    monkeypatch.setattr(coder_seam, "dispatch_coder_tapped", _dispatch_ratelimited)
    monkeypatch.setattr(worktree, "create_worktree", create_fake)

    adapter = _make_adapter()
    with pytest.raises(worktree.WorktreeError) as ei:
        await adapter.generate("t")
    assert "rate limit" in str(ei.value)
    assert created == ["bd-9.g1"]  # created once — NOT recreated for a provider error


async def test_coder_timeout_is_not_treated_as_a_missing_worktree(monkeypatch):
    """A `CoderTimeout` (a real attempt that ran long — its tree exists) propagates unchanged
    and is never mistaken for a missing worktree, so the ladder's timeout handling is intact."""

    async def _dispatch_timeout(coder, wt, prompt, *, fid=None, gen=None, tier="", timeout=None, env_passthrough=()):
        raise worktree.CoderTimeout("coder exceeded its activity budget")

    create_fake, created = _make_create()
    monkeypatch.setattr(coder_seam, "dispatch_coder_tapped", _dispatch_timeout)
    monkeypatch.setattr(worktree, "create_worktree", create_fake)

    adapter = _make_adapter()
    with pytest.raises(worktree.CoderTimeout):
        await adapter.generate("t")
    assert created == ["bd-9.g1"]  # created once — a timeout never recreates the tree


# ── adapter-level: verify() short-circuits an infra-failed candidate ────────────────


async def test_verify_fails_infra_candidate_without_running_tests(monkeypatch):
    """A candidate recorded in `_infra_failed` fails verify WITHOUT running the oracle — its
    tree is empty/fresh and could otherwise pass off base and be promoted as a non-solution —
    and it is marked a definitive FAIL (never salvaged, #538)."""
    adapter = _make_adapter()
    wt = "/wt/feat-bd-9.g3"
    adapter._gen_by_wt[wt] = 3
    adapter._infra_failed[wt] = "workdir does not exist: /wt/feat-bd-9.g3"

    async def _must_not_run(self, candidate_wt):
        raise AssertionError("the oracle must never run for an infra-failed candidate")

    monkeypatch.setattr(_WorktreeSolveAdapter, "_run_acceptance_tests", _must_not_run)

    verdict = await adapter.verify(wt)
    assert verdict.passed is False
    assert "not dispatched" in verdict.output
    assert adapter._verdict_by_wt[wt] == _VERDICT_FAIL
    # It must not pollute the circuit-breaker / sibling-feedback state — that is about code.
    assert adapter._completed_failures == []
    assert adapter._failure_signatures == {}


# ── shared adapter / stub helpers ───────────────────────────────────────────────────


def _make_adapter() -> _WorktreeSolveAdapter:
    return _WorktreeSolveAdapter(
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-9",
        coder=object(),
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        verdict_cls=_FakeVerdict,
    )


async def _noop_reap(repo, fid, *, base="", test_rung=True):
    return []
