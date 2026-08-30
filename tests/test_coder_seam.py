"""Tests for the ADR 0064 P2 board seam (coder_seam.py).

Covers the dispatch DECISION (``should_use_solve``) — the honest-degrade gate that
must fire false the instant any one of coder/acceptance/test-cmd is missing — and
``dispatch()``'s own orchestration (worktree-per-candidate, promote the winner, reap
the losers, surface gens-spent, raise ``SolveExhausted`` on a spent budget) with the
`coder` plugin's ``solve``/``Budget``/``Verdict`` injected as fakes, so none of this
needs the (separate, git-URL-installed) `coder` plugin to be present."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from project_board import coder_seam, worktree
from project_board.coder_seam import SolveExhausted, _WorktreeSolveAdapter, dispatch, should_use_solve

FEATURE_WITH_AC = {"id": "bd-1", "acceptance_criteria": "WHEN x THE SYSTEM SHALL y"}
FEATURE_NO_AC = {"id": "bd-2", "acceptance_criteria": ""}


# ── the dispatch decision (honest degrade) ───────────────────────────────────────


def test_should_use_solve_true_when_all_three_gates_hold():
    assert should_use_solve(FEATURE_WITH_AC, test_cmd="pytest -q", _solve_mod=object()) is True


def test_should_use_solve_false_when_coder_plugin_unavailable():
    assert should_use_solve(FEATURE_WITH_AC, test_cmd="pytest -q", _solve_mod=None) is False


def test_should_use_solve_false_without_acceptance_criteria():
    assert should_use_solve(FEATURE_NO_AC, test_cmd="pytest -q", _solve_mod=object()) is False


def test_should_use_solve_false_without_a_test_command():
    assert should_use_solve(FEATURE_WITH_AC, test_cmd="", _solve_mod=object()) is False
    assert should_use_solve(FEATURE_WITH_AC, test_cmd="   ", _solve_mod=object()) is False


def test_import_solve_returns_none_when_the_coder_plugin_is_absent():
    """`coder` is a separate plugin repo — genuinely absent in this standalone test
    env, which IS the honest-degrade case in production too (not a mock)."""
    assert coder_seam._import_solve() is None


def test_solve_exhausted_is_a_worktree_error():
    """So `_drive`'s existing ``except (worktree.NoChangesError, worktree.WorktreeError)``
    catches it with no changes to the except clause itself."""
    assert issubclass(SolveExhausted, worktree.WorktreeError)


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


def _stub_worktree(monkeypatch, *, created=None, removed=None, promoted=None):
    created = created if created is not None else []
    removed = removed if removed is not None else []
    promoted = promoted if promoted is not None else []

    async def _create(repo, base, cid, root):
        created.append(cid)
        return (f"/wt/feat-{cid}", f"feat/{cid}")

    async def _dispatch(coder, wt, prompt, *, timeout=None, env_passthrough=()):
        return f"reply from {wt}"

    async def _remove(repo, wt, branch=""):
        removed.append(wt)

    async def _promote(repo, src_wt, src_branch, fid, root=".worktrees", title=""):
        promoted.append((src_wt, src_branch, fid))
        return (f"/wt/feat-{fid}", f"feat/{fid}")

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "promote_worktree", _promote)
    return created, removed, promoted


# ── dispatch(): the winning-candidate path ───────────────────────────────────────


async def test_dispatch_promotes_the_winner_and_reaps_the_losers(monkeypatch):
    created, removed, promoted = _stub_worktree(monkeypatch)

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        # exercise the adapter for real: two candidates, the second "wins".
        await generate(task, feedback=None)
        c1 = await generate(task, feedback=None)
        return _FakeResult(solution=c1, passed=True, rung="best-of-k", gens_spent=2, candidates_tried=2)

    gens = []
    wt, branch, result = await dispatch(
        task="do the thing",
        coder=object(),
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-1",
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        budget=6,
        k=3,
        tree_depth=2,
        record_gens=gens.append,
        _solve=_fake_solve,
        _budget_cls=_FakeBudget,
        _verdict_cls=_FakeVerdict,
    )
    assert created == ["bd-1.g1", "bd-1.g2"]
    assert promoted == [("/wt/feat-bd-1.g2", "feat/bd-1.g2", "bd-1")]
    assert removed == ["/wt/feat-bd-1.g1"]  # only the loser reaped
    assert (wt, branch) == ("/wt/feat-bd-1", "feat/bd-1")  # canonical name
    # The winning candidate's OWN reply is the result — not an internal rung/gens
    # diagnostic string. loop.py uses this verbatim as the PR body; _verify_goal
    # reads it for the NO_TEST_NEEDED escape hatch. Losing candidate g1's reply
    # must NOT leak through — only g2 (the winner) is used.
    assert result == "reply from /wt/feat-bd-1.g2"
    assert gens == [2]  # cost surfaced exactly once


async def test_dispatch_falls_back_to_a_diagnostic_string_when_the_winner_has_no_reply(monkeypatch, tmp_path):
    """A fusion win (a plain completion, not a summary) — or any candidate whose
    reply somehow never got captured — has nothing human-authored to report, so
    dispatch() falls back to the rung/gens diagnostic string rather than an empty
    PR body."""
    _, removed, promoted = _stub_worktree(monkeypatch)

    async def _create_in_tmp(repo, base, cid, root):
        d = tmp_path / cid
        d.mkdir(parents=True, exist_ok=True)
        return (str(d), f"feat/{cid}")

    monkeypatch.setattr(worktree, "create_worktree", _create_in_tmp)

    async def _fake_openai_dispatch(delegate, prompt, *, timeout=None):
        return "### x.py\n```\nhi\n```"

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        c0 = await fusion_generate(task, feedback=None)  # the REAL adapter.generate_fusion
        return _FakeResult(solution=c0, passed=True, rung="fusion", gens_spent=1, candidates_tried=1, note="solved")

    _wt, _branch, result = await dispatch(
        task="do the thing",
        coder=object(),
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-2",
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        budget=6,
        k=3,
        tree_depth=2,
        fusion_delegate=object(),
        record_gens=lambda n: None,
        _solve=_fake_solve,
        _budget_cls=_FakeBudget,
        _verdict_cls=_FakeVerdict,
        _fusion_dispatch=_fake_openai_dispatch,
    )
    assert result == "[coder.solve rung=fusion gens=1] solved"


async def test_dispatch_records_gens_even_on_a_single_greedy_win(monkeypatch):
    _stub_worktree(monkeypatch)

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        c0 = await generate(task, feedback=None)
        return _FakeResult(solution=c0, passed=True, rung="greedy", gens_spent=1, candidates_tried=1)

    gens = []
    await dispatch(
        task="t",
        coder=object(),
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-9",
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        budget=6,
        k=3,
        tree_depth=2,
        record_gens=gens.append,
        _solve=_fake_solve,
        _budget_cls=_FakeBudget,
        _verdict_cls=_FakeVerdict,
    )
    assert gens == [1]


async def test_dispatch_records_the_verified_candidate_at_the_verify_boundary(monkeypatch):
    """#91: once a candidate PASSES its tests and is promoted, dispatch() commits the
    verified tree (so its content has a real sha — the loop's PR title keeps the
    shipped commit message unchanged) and hands {branch, sha, worktree} to
    ``record_verified`` — the crash-salvage record recovery resumes from if the
    process dies before open_pr."""
    _stub_worktree(monkeypatch)
    committed = []

    async def _commit(wt, message):
        committed.append((wt, message))

    monkeypatch.setattr(worktree, "commit_worktree", _commit)

    async def _git(wt, *args, timeout=60):
        assert args == ("rev-parse", "HEAD") and wt == "/wt/feat-bd-1"
        return (0, "abc123\n", "")

    monkeypatch.setattr(worktree, "_git", _git)

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        c0 = await generate(task, feedback=None)
        return _FakeResult(solution=c0, passed=True, rung="greedy", gens_spent=1, candidates_tried=1)

    recorded = []
    wt, branch, _result = await dispatch(
        task="t",
        coder=object(),
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-1",
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        budget=6,
        k=3,
        tree_depth=2,
        record_verified=lambda b, s, w: recorded.append((b, s, w)),
        commit_message="feat: the title",
        _solve=_fake_solve,
        _budget_cls=_FakeBudget,
        _verdict_cls=_FakeVerdict,
    )
    assert committed == [("/wt/feat-bd-1", "feat: the title")]  # the CANONICAL (promoted) tree
    assert recorded == [("feat/bd-1", "abc123", "/wt/feat-bd-1")]
    assert (wt, branch) == ("/wt/feat-bd-1", "feat/bd-1")


async def test_dispatch_returns_the_winner_even_if_record_verified_raises(monkeypatch):
    """The salvage record is fire-and-forget bookkeeping (like record_gens): a `br`
    hiccup persisting it must never discard a build whose tests already passed."""
    _stub_worktree(monkeypatch)

    async def _commit(wt, message):
        pass

    monkeypatch.setattr(worktree, "commit_worktree", _commit)

    async def _git(wt, *args, timeout=60):
        return (0, "abc123\n", "")

    monkeypatch.setattr(worktree, "_git", _git)

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        c0 = await generate(task, feedback=None)
        return _FakeResult(solution=c0, passed=True, rung="greedy", gens_spent=1, candidates_tried=1)

    def _boom(b, s, w):
        raise RuntimeError("br hiccup: lock contention")

    wt, branch, _result = await dispatch(
        task="t",
        coder=object(),
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-1",
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        budget=6,
        k=3,
        tree_depth=2,
        record_verified=_boom,
        _solve=_fake_solve,
        _budget_cls=_FakeBudget,
        _verdict_cls=_FakeVerdict,
    )
    assert (wt, branch) == ("/wt/feat-bd-1", "feat/bd-1")  # dispatch() itself never raised


async def test_dispatch_promotes_the_winner_even_if_record_gens_raises(monkeypatch):
    """`store.record_gens_spent` documents itself as fire-and-forget ("a br hiccup
    here must never fail the build the way a missing PR would") — a `BoardError`
    (lock contention, a flaky `br` call) out of `record_gens` must never discard an
    already-verified winning candidate or leak it un-promoted."""
    created, removed, promoted = _stub_worktree(monkeypatch)

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        c0 = await generate(task, feedback=None)
        return _FakeResult(solution=c0, passed=True, rung="greedy", gens_spent=1, candidates_tried=1)

    def _boom_record(n):
        raise RuntimeError("br hiccup: lock contention")

    wt, branch, result = await dispatch(
        task="t",
        coder=object(),
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-10",
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        budget=6,
        k=3,
        tree_depth=2,
        record_gens=_boom_record,
        _solve=_fake_solve,
        _budget_cls=_FakeBudget,
        _verdict_cls=_FakeVerdict,
    )
    assert created == ["bd-10.g1"]
    assert promoted == [("/wt/feat-bd-10.g1", "feat/bd-10.g1", "bd-10")]  # still promoted
    assert (wt, branch) == ("/wt/feat-bd-10", "feat/bd-10")  # dispatch() itself never raised


# ── dispatch(): the exhausted (no passing candidate) path ───────────────────────


async def test_dispatch_raises_solve_exhausted_and_reaps_every_candidate(monkeypatch):
    created, removed, promoted = _stub_worktree(monkeypatch)

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        await generate(task, feedback=None)
        c1 = await generate(task, feedback="prior failure")
        v = _FakeVerdict(passed=False, total=1, failed=1, output="AssertionError: nope")
        return _FakeResult(solution=c1, passed=False, rung="best-partial", gens_spent=2, candidates_tried=2, verdict=v)

    gens = []
    try:
        await dispatch(
            task="t",
            coder=object(),
            repo="/repo",
            base="main",
            root=".worktrees",
            fid="bd-2",
            dispatch_timeout=None,
            test_cmd="pytest -q",
            test_timeout=30,
            budget=6,
            k=3,
            tree_depth=2,
            record_gens=gens.append,
            _solve=_fake_solve,
            _budget_cls=_FakeBudget,
            _verdict_cls=_FakeVerdict,
        )
        raised = False
    except SolveExhausted as exc:
        raised = True
        assert "2 generation(s)" in str(exc)
        assert "best-partial" in str(exc)
    assert raised
    assert promoted == []  # nothing promoted — never opens a PR on an unverified partial
    assert set(removed) == {"/wt/feat-bd-2.g1", "/wt/feat-bd-2.g2"}  # every candidate reaped
    assert gens == [2]  # cost surfaced even though the search failed


async def test_dispatch_exhausted_with_no_candidates_at_all(monkeypatch):
    """Budget exhausted before even one generation (an edge solve() itself covers) —
    dispatch() must still raise cleanly with nothing to reap."""
    _created, removed, promoted = _stub_worktree(monkeypatch)

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        return _FakeResult(
            solution=None, passed=None, rung="none", gens_spent=0, candidates_tried=0, note="budget exhausted"
        )

    try:
        await dispatch(
            task="t",
            coder=object(),
            repo="/repo",
            base="main",
            root=".worktrees",
            fid="bd-3",
            dispatch_timeout=None,
            test_cmd="pytest -q",
            test_timeout=30,
            budget=0,
            k=3,
            tree_depth=2,
            _solve=_fake_solve,
            _budget_cls=_FakeBudget,
            _verdict_cls=_FakeVerdict,
        )
        raised = False
    except SolveExhausted:
        raised = True
    assert raised
    assert removed == [] and promoted == []


async def test_dispatch_still_reaps_and_raises_solve_exhausted_when_record_gens_raises(monkeypatch):
    """Same fire-and-forget contract on the exhausted path: a `record_gens` failure
    must not prevent every reaped candidate from actually being reaped, nor swallow
    the (honest) `SolveExhausted` the caller needs to see."""
    created, removed, promoted = _stub_worktree(monkeypatch)

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        await generate(task, feedback=None)
        c1 = await generate(task, feedback="prior failure")
        v = _FakeVerdict(passed=False, total=1, failed=1, output="AssertionError: nope")
        return _FakeResult(solution=c1, passed=False, rung="best-partial", gens_spent=2, candidates_tried=2, verdict=v)

    def _boom_record(n):
        raise RuntimeError("br hiccup: flaky CLI invocation")

    try:
        await dispatch(
            task="t",
            coder=object(),
            repo="/repo",
            base="main",
            root=".worktrees",
            fid="bd-11",
            dispatch_timeout=None,
            test_cmd="pytest -q",
            test_timeout=30,
            budget=6,
            k=3,
            tree_depth=2,
            record_gens=_boom_record,
            _solve=_fake_solve,
            _budget_cls=_FakeBudget,
            _verdict_cls=_FakeVerdict,
        )
        raised = False
    except SolveExhausted:
        raised = True
    assert raised  # the honest SolveExhausted still surfaces, not the record_gens RuntimeError
    assert created == ["bd-11.g1", "bd-11.g2"]
    assert set(removed) == {"/wt/feat-bd-11.g1", "/wt/feat-bd-11.g2"}  # both still reaped
    assert promoted == []


# ── dispatch(): solve() itself raises mid-ladder (not just returns unpassed) ────


async def test_dispatch_reaps_candidates_when_solve_raises_and_reraises_original(monkeypatch):
    """`solve()` (the `coder` plugin's ladder) has no try/except of its own around
    `generate`/`verify` — a real candidate failure (e.g. `CoderTimeout` on one
    best-of-k candidate, or a worktree op erroring) propagates straight out instead
    of being scored as a loss. Every worktree already created before the raise must
    still be reaped — untracked in `_inflight` until `dispatch()` returns, and
    invisible to the health sweep (a `.gN` id isn't a real board feature) — or a
    single flaky candidate leaks a worktree forever. The original exception must
    surface unchanged so the loop's existing capability-failure handling classifies
    it correctly (e.g. a `CoderTimeout` still escalates/blocks like it always has)."""
    created, removed, promoted = _stub_worktree(monkeypatch)

    class _Boom(RuntimeError):
        pass

    calls = {"n": 0}

    async def _dispatch(coder, wt, prompt, *, timeout=None, env_passthrough=()):
        calls["n"] += 1
        if calls["n"] == 2:
            raise _Boom("candidate coder timed out")
        return "ok"

    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        await generate(task, feedback=None)  # candidate 1: dispatch succeeds
        await generate(task, feedback=None)  # candidate 2: dispatch raises — uncaught by solve()

    gens = []
    try:
        await dispatch(
            task="t",
            coder=object(),
            repo="/repo",
            base="main",
            root=".worktrees",
            fid="bd-5",
            dispatch_timeout=None,
            test_cmd="pytest -q",
            test_timeout=30,
            budget=6,
            k=3,
            tree_depth=2,
            record_gens=gens.append,
            _solve=_fake_solve,
            _budget_cls=_FakeBudget,
            _verdict_cls=_FakeVerdict,
        )
        raised = False
    except _Boom:
        raised = True
    assert raised  # the ORIGINAL exception surfaces, not something dispatch() invented
    assert created == ["bd-5.g1", "bd-5.g2"]
    assert set(removed) == {"/wt/feat-bd-5.g1", "/wt/feat-bd-5.g2"}  # both reaped, none leaked
    assert promoted == []
    assert gens == [2]  # the attempted-candidate count still surfaces as spent cost


async def test_dispatch_reraises_the_original_mid_ladder_exception_even_if_record_gens_also_raises(monkeypatch):
    """If `record_gens` itself blows up (e.g. `BoardError` from a `br` hiccup) while
    handling a REAL mid-ladder failure, the original exception must still be what
    the caller sees — not the bookkeeping failure, and not silently swallowed."""
    created, removed, promoted = _stub_worktree(monkeypatch)

    class _Boom(RuntimeError):
        pass

    calls = {"n": 0}

    async def _dispatch(coder, wt, prompt, *, timeout=None, env_passthrough=()):
        calls["n"] += 1
        if calls["n"] == 2:
            raise _Boom("candidate coder timed out")
        return "ok"

    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        await generate(task, feedback=None)
        await generate(task, feedback=None)

    def _boom_record(n):
        raise RuntimeError("br hiccup: concurrent label write")

    try:
        await dispatch(
            task="t",
            coder=object(),
            repo="/repo",
            base="main",
            root=".worktrees",
            fid="bd-12",
            dispatch_timeout=None,
            test_cmd="pytest -q",
            test_timeout=30,
            budget=6,
            k=3,
            tree_depth=2,
            record_gens=_boom_record,
            _solve=_fake_solve,
            _budget_cls=_FakeBudget,
            _verdict_cls=_FakeVerdict,
        )
        raised_boom = False
    except _Boom:
        raised_boom = True
    assert raised_boom  # the ORIGINAL _Boom surfaces, not record_gens's RuntimeError
    assert created == ["bd-12.g1", "bd-12.g2"]
    assert set(removed) == {"/wt/feat-bd-12.g1", "/wt/feat-bd-12.g2"}  # still reaped
    assert promoted == []


async def test_dispatch_raise_with_no_candidates_created_yet_skips_record_gens(monkeypatch):
    """A raise before any `generate()` call completed (e.g. `Budget()` itself blew
    up) has nothing to reap and nothing real to cost-account — `record_gens` must
    not be called with a bogus zero."""
    _created, removed, promoted = _stub_worktree(monkeypatch)

    class _Boom(RuntimeError):
        pass

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        raise _Boom("blew up before any generation")

    gens = []
    try:
        await dispatch(
            task="t",
            coder=object(),
            repo="/repo",
            base="main",
            root=".worktrees",
            fid="bd-6",
            dispatch_timeout=None,
            test_cmd="pytest -q",
            test_timeout=30,
            budget=6,
            k=3,
            tree_depth=2,
            record_gens=gens.append,
            _solve=_fake_solve,
            _budget_cls=_FakeBudget,
            _verdict_cls=_FakeVerdict,
        )
        raised = False
    except _Boom:
        raised = True
    assert raised
    assert removed == [] and promoted == []
    assert gens == []  # nothing attempted — never fabricate a cost


# ── the adapter itself: generate() creates a worktree per candidate, verify() runs
#    the acceptance-test command and reports real pass/fail ─────────────────────


async def test_adapter_generate_creates_a_fresh_worktree_per_call(monkeypatch):
    created, _removed, _promoted = _stub_worktree(monkeypatch)
    prompts = []

    async def _dispatch(coder, wt, prompt, *, timeout=None, env_passthrough=()):
        prompts.append(prompt)
        return "ok"

    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    adapter = _WorktreeSolveAdapter(
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-7",
        coder=object(),
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        verdict_cls=_FakeVerdict,
    )
    wt1 = await adapter.generate("do the thing", feedback=None)
    wt2 = await adapter.generate("do the thing", feedback="tests X failed")
    assert created == ["bd-7.g1", "bd-7.g2"]
    assert wt1 != wt2
    assert adapter.candidates == [("/wt/feat-bd-7.g1", "feat/bd-7.g1"), ("/wt/feat-bd-7.g2", "feat/bd-7.g2")]
    assert "tests X failed" not in prompts[0]
    assert "tests X failed" in prompts[1]  # feedback folded into the retry's prompt only
    assert "fresh worktree" in prompts[1].lower()


async def test_adapter_verify_passes_on_exit_zero(monkeypatch):
    async def _ok(*a, **k):
        class _Proc:
            returncode = 0

            async def communicate(self):
                return (b"3 passed in 0.01s\n", None)

        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_shell", _ok)
    adapter = _WorktreeSolveAdapter(
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-1",
        coder=object(),
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        verdict_cls=_FakeVerdict,
    )
    v = await adapter.verify("/wt/feat-bd-1.g1")
    assert v.passed is True and v.failed == 0


async def test_adapter_verify_fails_on_nonzero_exit(monkeypatch):
    async def _bad(*a, **k):
        class _Proc:
            returncode = 1

            async def communicate(self):
                return (b"1 failed, 2 passed in 0.01s\nAssertionError: boom", None)

        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_shell", _bad)
    adapter = _WorktreeSolveAdapter(
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-1",
        coder=object(),
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        verdict_cls=_FakeVerdict,
    )
    v = await adapter.verify("/wt/feat-bd-1.g1")
    assert v.passed is False and v.failed == 1
    assert "boom" in v.output


async def test_adapter_verify_times_out_as_failed_not_silently_passed(monkeypatch):
    """Unlike the pre-PR local gate (fail-open on timeout), the ladder's OWN oracle
    must never silently treat an unconfirmed candidate as passing."""
    import asyncio as real_asyncio

    class _Proc:
        returncode = None

        async def communicate(self):
            raise real_asyncio.TimeoutError()

        def kill(self):
            pass

        async def wait(self):
            return None

    async def _hang(*a, **k):
        return _Proc()

    async def _boom_wait_for(coro, timeout):
        coro.close()
        raise real_asyncio.TimeoutError()

    monkeypatch.setattr("asyncio.create_subprocess_shell", _hang)
    monkeypatch.setattr("project_board.coder_seam.asyncio.wait_for", _boom_wait_for)
    adapter = _WorktreeSolveAdapter(
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-1",
        coder=object(),
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=0.01,
        verdict_cls=_FakeVerdict,
    )
    v = await adapter.verify("/wt/feat-bd-1.g1")
    assert v.passed is False
    assert "timed out" in v.output


async def test_adapter_feeds_prior_candidate_verify_failures_to_the_next_candidate(monkeypatch):
    """#146: within a tier's best-of-k, a candidate that FAILS verify must feed its
    failure output into the sibling candidates still to run — otherwise every
    parallel candidate (dispatched with feedback=None) re-attacks the task blind to
    the exact assertion its sibling already tripped."""
    _stub_worktree(monkeypatch)
    prompts = []

    async def _dispatch(coder, wt, prompt, *, timeout=None, env_passthrough=()):
        prompts.append(prompt)
        return f"reply from {wt}"

    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)

    # Each verify run yields a DISTINCT failure so we can prove both are carried
    # forward and each is labeled with its own candidate number.
    outputs = iter(
        [
            b"E   AssertionError: expected 5 got 3\n1 failed in 0.01s",
            b"E   TypeError: 'NoneType' has no len()\n1 failed in 0.01s",
        ]
    )

    async def _failing(*a, **k):
        out = next(outputs)

        class _Proc:
            returncode = 1

            async def communicate(self):
                return (out, None)

        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_shell", _failing)
    adapter = _WorktreeSolveAdapter(
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-146",
        coder=object(),
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        verdict_cls=_FakeVerdict,
    )

    # Candidate 1 runs first — it can't have any sibling failure to learn from.
    c1 = await adapter.generate("do the thing", feedback=None)
    assert (await adapter.verify(c1)).passed is False
    assert "AssertionError: expected 5 got 3" not in prompts[0]

    # r1: candidate 2's prompt carries candidate 1's verify failure, clearly labeled
    # as a PRIOR CANDIDATE failure (not the ladder's own retry feedback).
    await adapter.generate("do the thing", feedback=None)
    assert "AssertionError: expected 5 got 3" in prompts[1]
    assert "candidate 1" in prompts[1]
    assert "Prior candidate failures" in prompts[1]

    # r2 + r3: with candidate 2 also failed, candidate 3 gets BOTH prior failures
    # (each labeled with its number) AND the ladder's own retry feedback, kept in
    # separate, distinctly-labeled sections — own feedback first, siblings below.
    assert (await adapter.verify("/wt/feat-bd-146.g2")).passed is False
    await adapter.generate("do the thing", feedback="ladder says: fix the import")
    p3 = prompts[2]
    assert "candidate 1" in p3 and "AssertionError: expected 5 got 3" in p3
    assert "candidate 2" in p3 and "TypeError: 'NoneType' has no len()" in p3
    assert "ladder says: fix the import" in p3
    # The ladder's own feedback is distinct from the sibling-failure block, and sits
    # above it (the sibling failures are appended below the ladder's own feedback).
    assert p3.index("ladder says: fix the import") < p3.index("Prior candidate failures")


# ── #146 circuit breaker: K candidates failing on the IDENTICAL assertion ────────


def test_failure_signature_pairs_test_node_and_assertion():
    """#146 r2: the signature is the pytest FAILED node id + the AssertionError line,
    matched whether the assertion is inline (short-summary form) or on its own
    ``E   AssertionError:`` traceback line — both forms collapse to the SAME key."""
    inline = "FAILED tests/test_x.py::test_y - AssertionError: expected 5 got 3\n1 failed"
    sig = coder_seam._failure_signature(inline)
    assert sig is not None
    assert "tests/test_x.py::test_y" in sig
    assert "AssertionError: expected 5 got 3" in sig

    traceback = (
        "    assert result == 5\n"
        "E   AssertionError: expected 5 got 3\n"
        "=== short test summary ===\n"
        "FAILED tests/test_x.py::test_y - AssertionError: expected 5 got 3"
    )
    assert coder_seam._failure_signature(traceback) == sig  # same failure → same key


def test_failure_signature_none_without_a_failed_line_or_assertion():
    """No recognizable FAILED node id and no AssertionError line ⇒ None, so a
    non-assertion error (import error, collection error, empty output) can never
    accumulate a signature or trip the breaker."""
    assert coder_seam._failure_signature("collected 0 items\nno tests ran in 0.01s") is None
    assert coder_seam._failure_signature("E   ImportError: no module named foo") is None
    assert coder_seam._failure_signature("") is None


def _breaker_adapter():
    return _WorktreeSolveAdapter(
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-146",
        coder=object(),
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        verdict_cls=_FakeVerdict,
    )


def _failing_proc_returning(outputs):
    """A fake ``asyncio.create_subprocess_shell`` yielding the next canned output on
    each call (a list is cycled by an iterator; a single bytes value repeats)."""
    it = iter(outputs) if isinstance(outputs, (list, tuple)) else None

    async def _spawn(*a, **k):
        out = next(it) if it is not None else outputs

        class _Proc:
            returncode = 1

            async def communicate(self):
                return (out, None)

        return _Proc()

    return _spawn


async def test_circuit_breaker_trips_when_k_candidates_fail_on_the_same_assertion(monkeypatch):
    """#146 r1/r4: when K (=3) candidates fail on the IDENTICAL assertion signature,
    verify() raises SolveExhausted EARLY (on the 3rd failure, not after the whole
    budget) with the repeated assertion quoted in the message."""
    _stub_worktree(monkeypatch)
    same = b"FAILED tests/test_x.py::test_y - AssertionError: expected 5 got 3\n1 failed in 0.01s"
    monkeypatch.setattr("asyncio.create_subprocess_shell", _failing_proc_returning(same))

    adapter = _breaker_adapter()
    # The first two identical failures accumulate the signature but do NOT trip it —
    # two of a kind isn't yet a spec smell, and each verify still returns a verdict.
    assert (await adapter.verify("/wt/a")).passed is False
    assert (await adapter.verify("/wt/b")).passed is False
    assert adapter._failure_signatures  # the signature is being tracked

    # The THIRD identical failure trips the breaker — raised early, naming the
    # repeated assertion (both the failing test node and the assertion text).
    try:
        await adapter.verify("/wt/c")
        raised = False
    except SolveExhausted as exc:
        raised = True
        assert "AssertionError: expected 5 got 3" in str(exc)
        assert "tests/test_x.py::test_y" in str(exc)
    assert raised


async def test_circuit_breaker_does_not_trip_on_different_assertions(monkeypatch):
    """#146 r3/r5: three candidates failing on DIFFERENT assertions are real search,
    not a spec smell — no single signature reaches the threshold, so none trips."""
    _stub_worktree(monkeypatch)
    outputs = [
        b"FAILED tests/test_x.py::test_a - AssertionError: expected 5 got 3\n1 failed",
        b"FAILED tests/test_x.py::test_b - AssertionError: expected 1 got 2\n1 failed",
        b"FAILED tests/test_y.py::test_c - AssertionError: lists differ [1] != [2]\n1 failed",
    ]
    monkeypatch.setattr("asyncio.create_subprocess_shell", _failing_proc_returning(outputs))

    adapter = _breaker_adapter()
    # Three DISTINCT signatures — each counts exactly once; none reaches K, so no raise.
    assert (await adapter.verify("/wt/a")).passed is False
    assert (await adapter.verify("/wt/b")).passed is False
    assert (await adapter.verify("/wt/c")).passed is False  # must NOT raise
    assert set(adapter._failure_signatures.values()) == {1}  # three keys, each seen once


async def test_dispatch_circuit_breaker_stops_early_and_reaps_every_candidate(monkeypatch):
    """#146 r1 end-to-end: through dispatch(), the breaker cuts the ladder short well
    before the budget is spent. SolveExhausted propagates out of solve() (which has no
    try/except around verify()), dispatch() reaps every candidate it created and
    re-raises, and nothing is promoted — a spec smell never opens a PR."""
    created, removed, promoted = _stub_worktree(monkeypatch)
    same = b"FAILED tests/test_x.py::test_y - AssertionError: expected 5 got 3\n1 failed"
    monkeypatch.setattr("asyncio.create_subprocess_shell", _failing_proc_returning(same))

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        # A ladder that would try the FULL budget of candidates if never interrupted —
        # the breaker must stop it at the 3rd identical failure, not the 20th gen.
        for _ in range(budget.total):
            c = await generate(task, feedback=None)
            await verify(c)  # raises SolveExhausted on the 3rd identical failure
        raise AssertionError("circuit breaker never tripped")

    gens = []
    try:
        await dispatch(
            task="t",
            coder=object(),
            repo="/repo",
            base="main",
            root=".worktrees",
            fid="bd-cb",
            dispatch_timeout=None,
            test_cmd="pytest -q",
            test_timeout=30,
            budget=20,
            k=3,
            tree_depth=2,
            record_gens=gens.append,
            _solve=_fake_solve,
            _budget_cls=_FakeBudget,
            _verdict_cls=_FakeVerdict,
        )
        raised = False
    except SolveExhausted as exc:
        raised = True
        assert "AssertionError: expected 5 got 3" in str(exc)
    assert raised
    # EARLY: exactly 3 candidates generated (not the budget of 20), every one reaped,
    # nothing promoted, and the attempted cost still surfaced.
    assert created == ["bd-cb.g1", "bd-cb.g2", "bd-cb.g3"]
    assert set(removed) == {"/wt/feat-bd-cb.g1", "/wt/feat-bd-cb.g2", "/wt/feat-bd-cb.g3"}
    assert promoted == []
    assert gens == [3]


# ── rung 4: fusion (ADR 0064 P3) — a plain completion, not an ACP session ────────


def test_parse_fusion_files_single_file():
    reply = "### foo/bar.py\n```python\nprint('hi')\n```"
    assert coder_seam._parse_fusion_files(reply) == {"foo/bar.py": "print('hi')\n"}


def test_parse_fusion_files_multiple_files():
    reply = "### a.py\n```\nAAA\n```\n\nsome prose in between\n\n### b/c.py\n```\nBBB\n```"
    assert coder_seam._parse_fusion_files(reply) == {"a.py": "AAA\n", "b/c.py": "BBB\n"}


def test_parse_fusion_files_no_match_returns_empty():
    assert coder_seam._parse_fusion_files("I looked at it but didn't change anything.") == {}
    assert coder_seam._parse_fusion_files("") == {}


def test_fusion_viable_for_files_true_when_under_both_caps(tmp_path):
    (tmp_path / "a.py").write_text("x" * 100)
    (tmp_path / "b.py").write_text("y" * 100)
    ok, reason = coder_seam.fusion_viable_for_files(
        str(tmp_path), ["a.py", "b.py"], max_file_chars=1_000, max_total_chars=1_000
    )
    assert ok is True
    assert reason == ""


def test_fusion_viable_for_files_false_over_per_file_cap(tmp_path):
    (tmp_path / "huge.py").write_text("x" * 500)
    ok, reason = coder_seam.fusion_viable_for_files(
        str(tmp_path), ["huge.py"], max_file_chars=100, max_total_chars=10_000
    )
    assert ok is False
    assert "huge.py" in reason
    assert "100-char per-file cap" in reason


def test_fusion_viable_for_files_false_over_combined_cap(tmp_path):
    (tmp_path / "a.py").write_text("x" * 100)
    (tmp_path / "b.py").write_text("y" * 100)
    ok, reason = coder_seam.fusion_viable_for_files(
        str(tmp_path), ["a.py", "b.py"], max_file_chars=1_000, max_total_chars=150
    )
    assert ok is False
    assert "combined cap" in reason


def test_fusion_viable_for_files_skips_files_that_do_not_exist_yet(tmp_path):
    # A feature creating a brand-new file has nothing on disk to be too large yet.
    ok, reason = coder_seam.fusion_viable_for_files(str(tmp_path), ["not_yet_created.py"])
    assert ok is True
    assert reason == ""


def test_fusion_prompt_includes_task_and_existing_file_content(tmp_path):
    (tmp_path / "existing.py").write_text("def old(): pass\n")
    prompt = coder_seam._fusion_prompt(
        "fix the thing", feedback=None, repo=str(tmp_path), files_to_modify=["existing.py"]
    )
    assert "fix the thing" in prompt
    assert "def old(): pass" in prompt
    assert "existing.py" in prompt


def test_fusion_prompt_notes_a_not_yet_created_file(tmp_path):
    prompt = coder_seam._fusion_prompt("add new.py", feedback=None, repo=str(tmp_path), files_to_modify=["new.py"])
    assert "does not exist yet" in prompt


def test_fusion_prompt_includes_feedback_when_refining(tmp_path):
    prompt = coder_seam._fusion_prompt(
        "fix it", feedback="2/3 failing: AssertionError", repo=str(tmp_path), files_to_modify=[]
    )
    assert "FAILED the acceptance tests" in prompt
    assert "AssertionError" in prompt


def test_fusion_prompt_truncation_is_visible_not_silent(tmp_path):
    """Defensive backstop only (real callers gate via `fusion_viable_for_files`
    first) — but if a caller ever skips that gate, a truncated read must tell
    fusion to skip the file rather than let it return a "complete" replacement
    of content it never actually saw in full."""
    (tmp_path / "big.py").write_text("x = 1\n" * 10)
    prompt = coder_seam._fusion_prompt(
        "fix it", feedback=None, repo=str(tmp_path), files_to_modify=["big.py"], max_file_chars=10
    )
    assert "TRUNCATED at 10 chars" in prompt
    assert "do NOT return this as a complete replacement" in prompt


def test_fusion_prompt_no_truncation_marker_when_file_fits(tmp_path):
    (tmp_path / "small.py").write_text("x = 1\n")
    prompt = coder_seam._fusion_prompt(
        "fix it", feedback=None, repo=str(tmp_path), files_to_modify=["small.py"], max_file_chars=10_000
    )
    assert "TRUNCATED" not in prompt


async def test_generate_fusion_writes_parsed_files_into_a_fresh_worktree(monkeypatch, tmp_path):
    created, *_ = _stub_worktree(monkeypatch)

    async def _fake_openai_dispatch(delegate, prompt, *, timeout=None):
        return "### sub/dir/new.py\n```\nCONTENT\n```"

    adapter = _WorktreeSolveAdapter(
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-1",
        coder=object(),
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        verdict_cls=_FakeVerdict,
        fusion_delegate=object(),  # any non-None placeholder — resolution is the caller's job
        files_to_modify=[],
        _fusion_dispatch=_fake_openai_dispatch,
    )

    # `_stub_worktree`'s fake `create_worktree` always returns "/wt/feat-<cid>" — redirect
    # it to a real tmp_path so the write actually lands somewhere we can inspect.
    async def _create_in_tmp(repo, base, cid, root):
        d = tmp_path / cid
        d.mkdir(parents=True, exist_ok=True)
        return (str(d), f"feat/{cid}")

    monkeypatch.setattr(worktree, "create_worktree", _create_in_tmp)

    wt = await adapter.generate_fusion("do the thing")
    assert (Path(wt) / "sub" / "dir" / "new.py").read_text() == "CONTENT\n"
    assert adapter.candidates == [(wt, "feat/bd-1.g1")]  # tracked like any other candidate


async def test_generate_fusion_rejects_a_path_traversal_attempt(monkeypatch, tmp_path):
    _stub_worktree(monkeypatch)

    async def _fake_openai_dispatch(delegate, prompt, *, timeout=None):
        return (
            "### ../../etc/passwd\n```\npwned\n```\n\n### /etc/shadow\n```\npwned2\n```\n\n### legit.py\n```\nfine\n```"
        )

    async def _create_in_tmp(repo, base, cid, root):
        d = tmp_path / cid
        d.mkdir(parents=True, exist_ok=True)
        return (str(d), f"feat/{cid}")

    monkeypatch.setattr(worktree, "create_worktree", _create_in_tmp)

    adapter = _WorktreeSolveAdapter(
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-1",
        coder=object(),
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        verdict_cls=_FakeVerdict,
        fusion_delegate=object(),
        files_to_modify=[],
        _fusion_dispatch=_fake_openai_dispatch,
    )
    wt = await adapter.generate_fusion("do the thing")
    # only the legitimate relative path was written; nothing escaped the worktree
    assert (Path(wt) / "legit.py").read_text() == "fine\n"
    assert not (Path(wt).parent / "etc").exists()
    assert not Path("/etc/shadow_THIS_MUST_NOT_EXIST_pwned2").exists()


async def test_generate_fusion_restricts_writes_to_declared_files_to_modify(monkeypatch, tmp_path):
    """Fusion has no tool access — it only ever sees the files we showed it. A
    path outside the feature's declared `files_to_modify` means a hallucinated
    file (or a parser mis-split); writing it would silently touch unrelated
    code with no test coverage backing the change."""
    _stub_worktree(monkeypatch)

    async def _fake_openai_dispatch(delegate, prompt, *, timeout=None):
        return "### declared.py\n```\nfine\n```\n\n### undeclared.py\n```\nsneaky\n```"

    async def _create_in_tmp(repo, base, cid, root):
        d = tmp_path / cid
        d.mkdir(parents=True, exist_ok=True)
        return (str(d), f"feat/{cid}")

    monkeypatch.setattr(worktree, "create_worktree", _create_in_tmp)

    adapter = _WorktreeSolveAdapter(
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-1",
        coder=object(),
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        verdict_cls=_FakeVerdict,
        fusion_delegate=object(),
        files_to_modify=["declared.py"],
        _fusion_dispatch=_fake_openai_dispatch,
    )
    wt = await adapter.generate_fusion("do the thing")
    assert (Path(wt) / "declared.py").read_text() == "fine\n"
    assert not (Path(wt) / "undeclared.py").exists()


async def test_generate_fusion_shrink_guard_refuses_a_suspiciously_smaller_rewrite(monkeypatch, tmp_path):
    """A whole-file "complete replacement" that comes back drastically smaller
    than the file it claims to replace is far more likely a truncated
    completion (delegate max_tokens ceiling) than an intentional big deletion."""
    _stub_worktree(monkeypatch)

    async def _fake_openai_dispatch(delegate, prompt, *, timeout=None):
        return "### big.py\n```\nx\n```"  # a few chars back for a 1000-char original

    async def _create_in_tmp(repo, base, cid, root):
        d = tmp_path / cid
        d.mkdir(parents=True, exist_ok=True)
        (d / "big.py").write_text("x = 1\n" * 200)  # 1200 chars, well over the min-original floor
        return (str(d), f"feat/{cid}")

    monkeypatch.setattr(worktree, "create_worktree", _create_in_tmp)

    adapter = _WorktreeSolveAdapter(
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-1",
        coder=object(),
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        verdict_cls=_FakeVerdict,
        fusion_delegate=object(),
        files_to_modify=["big.py"],
        _fusion_dispatch=_fake_openai_dispatch,
    )
    wt = await adapter.generate_fusion("do the thing")
    # refused — the pre-existing (larger) content must survive untouched
    assert (Path(wt) / "big.py").read_text() == "x = 1\n" * 200


async def test_generate_fusion_shrink_guard_allows_a_legitimately_smaller_edit(monkeypatch, tmp_path):
    """The guard only kicks in above `_SHRINK_GUARD_MIN_ORIGINAL_CHARS` and below
    `_SHRINK_GUARD_RATIO` — a real, modest trim must still go through."""
    _stub_worktree(monkeypatch)

    async def _fake_openai_dispatch(delegate, prompt, *, timeout=None):
        return "### small.py\n```\nx = 1\n```"

    async def _create_in_tmp(repo, base, cid, root):
        d = tmp_path / cid
        d.mkdir(parents=True, exist_ok=True)
        (d / "small.py").write_text("x = 1\ny = 2\n")  # tiny original, under the min-original floor
        return (str(d), f"feat/{cid}")

    monkeypatch.setattr(worktree, "create_worktree", _create_in_tmp)

    adapter = _WorktreeSolveAdapter(
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-1",
        coder=object(),
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        verdict_cls=_FakeVerdict,
        fusion_delegate=object(),
        files_to_modify=["small.py"],
        _fusion_dispatch=_fake_openai_dispatch,
    )
    wt = await adapter.generate_fusion("do the thing")
    assert (Path(wt) / "small.py").read_text() == "x = 1\n"


async def test_generate_fusion_empty_reply_writes_nothing_and_does_not_crash(monkeypatch, tmp_path):
    _stub_worktree(monkeypatch)

    async def _fake_openai_dispatch(delegate, prompt, *, timeout=None):
        return "I looked at the task but have no changes."

    async def _create_in_tmp(repo, base, cid, root):
        d = tmp_path / cid
        d.mkdir(parents=True, exist_ok=True)
        return (str(d), f"feat/{cid}")

    monkeypatch.setattr(worktree, "create_worktree", _create_in_tmp)

    adapter = _WorktreeSolveAdapter(
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-1",
        coder=object(),
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        verdict_cls=_FakeVerdict,
        fusion_delegate=object(),
        files_to_modify=[],
        _fusion_dispatch=_fake_openai_dispatch,
    )
    wt = await adapter.generate_fusion("do the thing")
    assert list(Path(wt).iterdir()) == []  # untouched — will just fail verify() like any empty candidate


# ── dispatch(): fusion end-to-end + honest degrade ───────────────────────────────


async def test_dispatch_reaches_fusion_when_cheaper_rungs_fail(monkeypatch, tmp_path):
    """A `_fake_solve` standing in for the REAL ladder: simulates greedy/best-of-k/
    tree-search all failing, then calls `fusion_generate` and wins — proving
    `dispatch()` wires `fusion_generate`/`fusion_k` through to `solve()` and that a
    fusion-produced candidate promotes exactly like an ACP one."""
    created, removed, promoted = _stub_worktree(monkeypatch)

    async def _create_in_tmp(repo, base, cid, root):
        d = tmp_path / cid
        d.mkdir(parents=True, exist_ok=True)
        return (str(d), f"feat/{cid}")

    monkeypatch.setattr(worktree, "create_worktree", _create_in_tmp)

    async def _fake_openai_dispatch(delegate, prompt, *, timeout=None):
        return "### fixed.py\n```\nfixed content\n```"

    seen_fusion_k = {}

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        seen_fusion_k["k"] = fusion_k
        assert fusion_generate is not None  # dispatch() must have wired it through
        c = await fusion_generate(task, feedback="2/2 failing")
        return _FakeResult(solution=c, passed=True, rung="fusion", gens_spent=5, candidates_tried=5)

    gens = []
    wt, branch, result = await dispatch(
        task="do the thing",
        coder=object(),
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-1",
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        budget=6,
        k=3,
        tree_depth=2,
        record_gens=gens.append,
        fusion_delegate=object(),
        fusion_k=4,
        files_to_modify=[],
        _solve=_fake_solve,
        _budget_cls=_FakeBudget,
        _verdict_cls=_FakeVerdict,
        _fusion_dispatch=_fake_openai_dispatch,
    )
    assert seen_fusion_k["k"] == 4
    assert "fusion" in result and "gens=5" in result
    assert promoted and promoted[0][2] == "bd-1"
    assert gens == [5]


async def test_dispatch_without_a_fusion_delegate_passes_none_through(monkeypatch):
    """Honest degrade (unchanged from before this rung existed): no fusion_delegate
    configured ⇒ solve() gets fusion_generate=None ⇒ the ladder stops at tree-search."""
    _stub_worktree(monkeypatch)
    seen = {}

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        seen["fusion_generate"] = fusion_generate
        c = await generate(task, feedback=None)
        return _FakeResult(solution=c, passed=True, rung="greedy", gens_spent=1, candidates_tried=1)

    await dispatch(
        task="do the thing",
        coder=object(),
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-1",
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        budget=6,
        k=3,
        tree_depth=2,
        # fusion_delegate omitted — defaults to None
        _solve=_fake_solve,
        _budget_cls=_FakeBudget,
        _verdict_cls=_FakeVerdict,
    )
    assert seen["fusion_generate"] is None


# ── test_rung(): operator-only diagnostic — always reaps, never promotes ────────


async def test_test_rung_always_reaps_even_on_a_pass(monkeypatch, tmp_path):
    """A passing test_rung() candidate must still be reaped — this is a diagnostic
    dry-run, never a real dispatch. (dispatch() PROMOTES a winner; test_rung() must
    not, or a 'just checking fusion works' call would silently ship a feature.)"""
    removed = []

    async def _create_in_tmp(repo, base, cid, root):
        d = tmp_path / cid
        d.mkdir(parents=True, exist_ok=True)
        return (str(d), f"feat/{cid}")

    async def _dispatch(coder, wt, prompt, *, timeout=None, env_passthrough=()):
        return "reply"

    async def _remove(repo, wt, branch=""):
        removed.append(wt)

    monkeypatch.setattr(worktree, "create_worktree", _create_in_tmp)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)

    async def _fake_solve(
        task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2, force_rung=None
    ):
        assert force_rung == "greedy"  # test_rung must pass force_rung through
        c = await generate(task, feedback=None)
        return _FakeResult(solution=c, passed=True, rung="greedy", gens_spent=1, candidates_tried=1)

    result = await coder_seam.test_rung(
        rung="greedy",
        task="do the thing",
        coder=object(),
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-1",
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        _solve=_fake_solve,
        _budget_cls=_FakeBudget,
        _verdict_cls=_FakeVerdict,
    )
    assert result == {
        "rung": "greedy",
        "passed": True,
        "gens_spent": 1,
        "candidates_tried": 1,
        "note": "",
        "verdict_output": "",
    }
    assert len(removed) == 1  # the winning candidate was reaped, NOT promoted


async def test_test_rung_reaps_on_a_fail_too(monkeypatch, tmp_path):
    removed = []

    async def _create_in_tmp(repo, base, cid, root):
        d = tmp_path / cid
        d.mkdir(parents=True, exist_ok=True)
        return (str(d), f"feat/{cid}")

    async def _dispatch(coder, wt, prompt, *, timeout=None, env_passthrough=()):
        return "reply"

    async def _remove(repo, wt, branch=""):
        removed.append(wt)

    monkeypatch.setattr(worktree, "create_worktree", _create_in_tmp)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)

    async def _fake_solve(
        task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2, force_rung=None
    ):
        c = await generate(task, feedback=None)
        v = _FakeVerdict(passed=False, total=2, failed=1, output="1 failed")
        return _FakeResult(
            solution=c,
            passed=False,
            rung="greedy",
            gens_spent=1,
            candidates_tried=1,
            verdict=v,
            note="forced greedy (test) — 1/2 failing",
        )

    result = await coder_seam.test_rung(
        rung="greedy",
        task="do the thing",
        coder=object(),
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-1",
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        _solve=_fake_solve,
        _budget_cls=_FakeBudget,
        _verdict_cls=_FakeVerdict,
    )
    assert result["passed"] is False
    assert result["verdict_output"] == "1 failed"
    assert len(removed) == 1  # still reaped despite the fail


async def test_test_rung_reaps_even_if_solve_raises(monkeypatch, tmp_path):
    removed = []

    async def _create_in_tmp(repo, base, cid, root):
        d = tmp_path / cid
        d.mkdir(parents=True, exist_ok=True)
        return (str(d), f"feat/{cid}")

    async def _dispatch(coder, wt, prompt, *, timeout=None, env_passthrough=()):
        return "reply"

    async def _remove(repo, wt, branch=""):
        removed.append(wt)

    monkeypatch.setattr(worktree, "create_worktree", _create_in_tmp)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)

    async def _fake_solve(
        task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2, force_rung=None
    ):
        await generate(task, feedback=None)
        raise worktree.CoderTimeout("boom")

    try:
        await coder_seam.test_rung(
            rung="greedy",
            task="t",
            coder=object(),
            repo="/repo",
            base="main",
            root=".worktrees",
            fid="bd-1",
            dispatch_timeout=None,
            test_cmd="pytest -q",
            test_timeout=30,
            _solve=_fake_solve,
            _budget_cls=_FakeBudget,
            _verdict_cls=_FakeVerdict,
        )
        raise AssertionError("expected CoderTimeout to propagate")
    except worktree.CoderTimeout:
        pass
    assert len(removed) == 1  # reaped even though solve() raised


async def test_test_rung_forwards_fusion_and_files_to_modify(monkeypatch, tmp_path):
    async def _create_in_tmp(repo, base, cid, root):
        d = tmp_path / cid
        d.mkdir(parents=True, exist_ok=True)
        return (str(d), f"feat/{cid}")

    monkeypatch.setattr(worktree, "create_worktree", _create_in_tmp)
    monkeypatch.setattr(worktree, "remove_worktree", lambda *a, **k: _noop())

    async def _noop():
        return None

    seen = {}

    async def _fake_solve(
        task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2, force_rung=None
    ):
        seen["force_rung"] = force_rung
        seen["fusion_generate_is_none"] = fusion_generate is None
        seen["fusion_k"] = fusion_k
        return _FakeResult(solution="x", passed=True, rung="fusion", gens_spent=1, candidates_tried=1)

    await coder_seam.test_rung(
        rung="fusion",
        task="t",
        coder=object(),
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-1",
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        fusion_delegate=object(),
        fusion_k=5,
        files_to_modify=["a.py"],
        _solve=_fake_solve,
        _budget_cls=_FakeBudget,
        _verdict_cls=_FakeVerdict,
        _fusion_dispatch=lambda *a, **k: _noop(),
    )
    assert seen["force_rung"] == "fusion"
    assert seen["fusion_generate_is_none"] is False  # fusion_delegate given → wired through
    assert seen["fusion_k"] == 5


# ── max_concurrent_sessions: semaphore-based within-drive concurrency cap ─────────


async def test_adapter_max_concurrent_sessions_zero_leaves_behaviour_unchanged(monkeypatch):
    """max_concurrent_sessions=0 (default) must not add a semaphore — behaviour is
    identical to the pre-cap baseline."""
    created, *_ = _stub_worktree(monkeypatch)
    adapter = _WorktreeSolveAdapter(
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-cs0",
        coder=object(),
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        verdict_cls=_FakeVerdict,
        max_concurrent_sessions=0,
    )
    assert adapter._session_sem is None  # no semaphore created
    await adapter.generate("task", feedback=None)
    assert created == ["bd-cs0.g1"]


async def test_adapter_max_concurrent_sessions_one_serialises_concurrent_dispatches(monkeypatch):
    """max_concurrent_sessions=1 limits the adapter to one in-flight ACP dispatch at
    a time — even when solve() calls generate() via asyncio.gather with k>1."""
    import asyncio as _asyncio

    _stub_worktree(monkeypatch)

    order: list[str] = []
    gate = _asyncio.Event()

    async def _slow_dispatch(coder, wt, prompt, *, timeout=None, env_passthrough=()):
        order.append(f"start:{wt}")
        await gate.wait()
        order.append(f"end:{wt}")
        return f"reply from {wt}"

    monkeypatch.setattr(worktree, "dispatch_coder", _slow_dispatch)

    adapter = _WorktreeSolveAdapter(
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-cs1",
        coder=object(),
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        verdict_cls=_FakeVerdict,
        max_concurrent_sessions=1,
    )
    assert adapter._session_sem is not None

    # Launch two concurrent generate() calls. With sem=1 only one can enter
    # dispatch_coder at a time. The gate lets us inspect the intermediate state.
    t1 = _asyncio.create_task(adapter.generate("task"))
    t2 = _asyncio.create_task(adapter.generate("task"))

    # Let the event loop run until at least one dispatch has started.
    await _asyncio.sleep(0)
    await _asyncio.sleep(0)

    # Only one dispatch should be in-flight (started but not ended) before the gate opens.
    started_before_gate = [e for e in order if e.startswith("start:")]
    assert len(started_before_gate) == 1, f"expected 1 in-flight dispatch, got: {order}"

    gate.set()
    await _asyncio.gather(t1, t2)

    # After both complete, the full sequence must show strict serialisation:
    # start g1 → end g1 → start g2 → end g2 (or the reverse g2/g1 ordering —
    # whichever task was scheduled first is irrelevant; what matters is no overlap).
    assert len(order) == 4
    for i in range(0, 4, 2):
        assert order[i].startswith("start:") and order[i + 1].startswith("end:")
        assert order[i].split(":")[1] == order[i + 1].split(":")[1]


async def test_dispatch_threads_max_concurrent_sessions_to_adapter(monkeypatch):
    """dispatch() must pass max_concurrent_sessions to the adapter so the
    semaphore is active when the caller sets it."""
    _stub_worktree(monkeypatch)
    seen_sem = {}

    original_init = _WorktreeSolveAdapter.__init__

    def _patched_init(self, **kwargs):
        seen_sem["value"] = kwargs.get("max_concurrent_sessions", -1)
        original_init(self, **kwargs)

    monkeypatch.setattr(_WorktreeSolveAdapter, "__init__", _patched_init)

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        c0 = await generate(task, feedback=None)
        return _FakeResult(solution=c0, passed=True, rung="greedy", gens_spent=1, candidates_tried=1)

    await dispatch(
        task="t",
        coder=object(),
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-mcs",
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        budget=6,
        k=3,
        tree_depth=2,
        max_concurrent_sessions=2,
        _solve=_fake_solve,
        _budget_cls=_FakeBudget,
        _verdict_cls=_FakeVerdict,
    )
    assert seen_sem["value"] == 2


# ── resolve_delegate: shared by loop.py and api.py's test-rung route ─────────────


def test_resolve_delegate_returns_none_when_delegates_plugin_absent():
    """`plugins.delegates` is genuinely absent in this standalone test env — the
    honest-degrade case in production too when the plugin's disabled."""
    assert coder_seam.resolve_delegate("anything", "acp") is None


# ── #304: dispatch_task selects the adapter by the delegate's type (acp | a2a) ─────


class _FakeAdapter:
    """A delegates ADAPTERS entry stand-in: records ``dispatch``/``teardown`` calls so a
    test can prove which adapter ran and that teardown fired on every exit."""

    def __init__(self, *, reply="ok", raises=None, hang=False):
        self.reply = reply
        self.raises = raises
        self.hang = hang
        self.dispatched: list = []
        self.torn_down: list = []

    async def dispatch(self, delegate, prompt, *, timeout=None):
        self.dispatched.append((delegate, prompt, timeout))
        if self.hang:
            import asyncio

            await asyncio.sleep(10)  # outlive the wait_for deadline → CoderTimeout
        if self.raises is not None:
            raise self.raises
        return self.reply

    async def teardown(self, delegate):
        self.torn_down.append(delegate)


def _inject_task_adapters(monkeypatch, **adapters):
    """Stand in for the HOST's plugins.delegates.adapters (absent in this suite) with an
    ADAPTERS registry + DelegateError, so ``dispatch_task`` can be exercised for real.
    Returns the DelegateError class so a test can build an adapter that raises it."""
    import sys
    import types as _types

    class DelegateError(Exception):
        pass

    mod = _types.ModuleType("plugins.delegates.adapters")
    mod.DelegateError = DelegateError
    mod.ADAPTERS = dict(adapters)
    plugins = _types.ModuleType("plugins")
    delegates = _types.ModuleType("plugins.delegates")
    plugins.delegates = delegates
    delegates.adapters = mod
    monkeypatch.setitem(sys.modules, "plugins", plugins)
    monkeypatch.setitem(sys.modules, "plugins.delegates", delegates)
    monkeypatch.setitem(sys.modules, "plugins.delegates.adapters", mod)
    return DelegateError


def _delegate(kind: str):
    import types as _types

    return _types.SimpleNamespace(type=kind, name=f"{kind}-agent")


async def test_dispatch_task_routes_an_a2a_delegate_through_the_a2a_adapter(monkeypatch):
    """#304 r1: a task delegate of type ``a2a`` is dispatched over ADAPTERS["a2a"] — the
    same transport review dispatch uses — NOT the acp adapter, bounded by the given
    timeout, and its reply is returned for record_delivery. Torn down on the way out."""
    acp = _FakeAdapter(reply="ACP")
    a2a = _FakeAdapter(reply="A2A deliverable")
    _inject_task_adapters(monkeypatch, acp=acp, a2a=a2a)

    delegate = _delegate("a2a")
    out = await coder_seam.dispatch_task(delegate, "audit the API", timeout=1800)

    assert out == "A2A deliverable"
    assert a2a.dispatched == [(delegate, "audit the API", 1800)]  # the a2a adapter ran…
    assert acp.dispatched == []  # …and the acp adapter did NOT
    assert a2a.torn_down == [delegate]  # torn down on the success exit (r5)


async def test_dispatch_task_routes_an_acp_delegate_through_the_acp_adapter(monkeypatch):
    """#304 r2 (seam-level regression pin): an ``acp`` task delegate still dispatches
    over ADAPTERS["acp"] exactly as before — the a2a branch doesn't touch the acp path."""
    acp = _FakeAdapter(reply="ACP deliverable")
    a2a = _FakeAdapter(reply="A2A")
    _inject_task_adapters(monkeypatch, acp=acp, a2a=a2a)

    delegate = _delegate("acp")
    out = await coder_seam.dispatch_task(delegate, "write the ADR", timeout=1800)

    assert out == "ACP deliverable"
    assert acp.dispatched == [(delegate, "write the ADR", 1800)]
    assert a2a.dispatched == []
    assert acp.torn_down == [delegate]


async def test_dispatch_task_defaults_a_typeless_delegate_to_acp(monkeypatch):
    """A delegate with no ``type`` attribute defaults to the acp adapter — the pre-#304
    behaviour, kept as a safety net for a typeless double / unset delegate."""
    import types as _types

    acp = _FakeAdapter(reply="ok")
    _inject_task_adapters(monkeypatch, acp=acp, a2a=_FakeAdapter())

    delegate = _types.SimpleNamespace(name="bare")  # no .type
    out = await coder_seam.dispatch_task(delegate, "do it")

    assert out == "ok"
    assert acp.dispatched and acp.dispatched[0][0] is delegate
    assert acp.torn_down == [delegate]


async def test_dispatch_task_normalizes_an_a2a_delegate_error_and_still_tears_down(monkeypatch):
    """#304 r4/r5: an A2A DelegateError surfaces as WorktreeError (so the loop's coder
    classifier blocks the card with a classified reason), and the delegate is still torn
    down on the error exit."""
    DelegateError = _inject_task_adapters(monkeypatch)
    import sys

    a2a = _FakeAdapter(raises=DelegateError("agent unreachable"))
    sys.modules["plugins.delegates.adapters"].ADAPTERS["a2a"] = a2a

    delegate = _delegate("a2a")
    raised = False
    try:
        await coder_seam.dispatch_task(delegate, "x", timeout=1800)
    except worktree.WorktreeError as exc:
        raised = True
        assert "agent unreachable" in str(exc)
    assert raised
    assert a2a.torn_down == [delegate]  # torn down even on the failure exit


async def test_dispatch_task_maps_an_a2a_timeout_to_coder_timeout_and_tears_down(monkeypatch):
    """#304 r4/r5: a task dispatch that exceeds ``timeout`` surfaces as CoderTimeout
    (mapped from asyncio.TimeoutError) and the delegate is still torn down."""
    _inject_task_adapters(monkeypatch)
    import sys

    a2a = _FakeAdapter(hang=True)
    sys.modules["plugins.delegates.adapters"].ADAPTERS["a2a"] = a2a

    delegate = _delegate("a2a")
    raised = False
    try:
        await coder_seam.dispatch_task(delegate, "x", timeout=0.01)
    except worktree.CoderTimeout:
        raised = True
    assert raised
    assert a2a.torn_down == [delegate]  # torn down on the timeout exit


# ── #311: first-party self-dispatch through the host's own invoke seam ─────────────


def test_resolve_self_invoke_feature_detects_a_callable_invoke():
    """#311 r2: the self seam is present ONLY when the host exposes a callable ``invoke``.
    A host without it, or with a non-callable ``invoke``, resolves to None — the loop parks
    the self task exactly as it parks a human/unassigned one. ``_host`` is the injection
    seam; production callers let it import ``graph.plugins.host.HOST``."""
    import types as _types

    def invoke(prompt, session_id, *, tool_fence=None):
        return "ok"

    assert coder_seam.resolve_self_invoke(_host=_types.SimpleNamespace(invoke=invoke)) is invoke
    assert coder_seam.resolve_self_invoke(_host=_types.SimpleNamespace()) is None  # no invoke attr
    assert coder_seam.resolve_self_invoke(_host=_types.SimpleNamespace(invoke="nope")) is None  # not callable


def test_resolve_self_invoke_returns_none_when_the_host_package_is_absent():
    """#311 r2: in the standalone suite there is no ``graph`` package, so ``_import_host``
    degrades to None and the seam resolves to None — the honest degrade to the existing
    park (mirrors ``test_import_solve_returns_none_when_the_coder_plugin_is_absent``)."""
    assert coder_seam.resolve_self_invoke() is None


async def test_dispatch_self_passes_tool_fence_none_when_the_seam_supports_it():
    """#311: ``tool_fence`` is FEATURE-DETECTED via inspect.signature, not assumed. A host
    whose ``invoke`` accepts it is called with ``tool_fence=None`` — self work is trusted
    first-party work, so it runs UNFENCED. The prompt + stable session id pass through."""
    seen = {}

    def invoke(prompt, session_id, *, tool_fence="sentinel"):
        seen["args"] = (prompt, session_id)
        seen["tool_fence"] = tool_fence
        return "deliverable"

    out = await coder_seam.dispatch_self(invoke, "do the task", "board-self-bd-1", timeout=1800)
    assert out == "deliverable"
    assert seen["args"] == ("do the task", "board-self-bd-1")
    assert seen["tool_fence"] is None  # explicitly unfenced — not the default sentinel


async def test_dispatch_self_omits_tool_fence_when_the_seam_predates_it():
    """#311: a host whose ``invoke`` has NO ``tool_fence`` parameter is called without it —
    no TypeError. That the call returns proves the parameter was not force-passed."""
    seen = {}

    def invoke(prompt, session_id):
        seen["args"] = (prompt, session_id)
        return "ok"

    assert await coder_seam.dispatch_self(invoke, "p", "s") == "ok"
    assert seen["args"] == ("p", "s")


async def test_dispatch_self_awaits_a_coroutine_invoke():
    """#311: a coroutine ``invoke`` is awaited directly (not offloaded), and tool_fence is
    still feature-detected and passed as None."""

    async def invoke(prompt, session_id, *, tool_fence=None):
        return f"async:{prompt}:{tool_fence}"

    assert await coder_seam.dispatch_self(invoke, "task", "sid") == "async:task:None"


async def test_dispatch_self_offloads_a_synchronous_invoke_to_a_thread():
    """#311: a synchronous ``invoke`` runs on a worker thread (``asyncio.to_thread``) so it
    never stalls the event loop — the reply comes back stringified through the seam."""

    def invoke(prompt, session_id, *, tool_fence=None):
        return "sync-ok"

    assert await coder_seam.dispatch_self(invoke, "p", "s") == "sync-ok"


async def test_dispatch_self_maps_a_timeout_to_coder_timeout():
    """#311 r5: a self-invoke that exceeds ``timeout`` surfaces as CoderTimeout (mapped from
    asyncio.TimeoutError), so the loop's classifier blocks the card like any coder timeout."""
    import asyncio as _asyncio

    async def invoke(prompt, session_id, *, tool_fence=None):
        await _asyncio.sleep(10)

    with pytest.raises(worktree.CoderTimeout):
        await coder_seam.dispatch_self(invoke, "p", "s", timeout=0.01)


async def test_dispatch_self_drains_a_timed_out_synchronous_worker_before_surfacing():
    """#311 review finding: a SYNCHRONOUS invoke runs on a worker thread that CANNOT be
    cancelled. A plain ``wait_for(to_thread(...))`` would surface the timeout while the thread
    kept running on the host — so after the loop cleared its one-in-flight guard a second self
    task could invoke the host CONCURRENTLY. dispatch_self instead DRAINS the thread to true
    completion before raising CoderTimeout: when the timeout surfaces the worker has genuinely
    finished, so no invoke is left running past the guard. Red-is-reachable: the abandon-on-
    timeout code leaves ``finished`` unset when CoderTimeout is raised."""
    import threading
    import time as _time

    started = threading.Event()
    finished = threading.Event()

    def invoke(prompt, session_id, *, tool_fence=None):
        started.set()
        _time.sleep(0.2)  # outlives the 0.01s timeout — the thread cannot be cancelled
        finished.set()
        return "late"

    with pytest.raises(worktree.CoderTimeout):
        await coder_seam.dispatch_self(invoke, "p", "s", timeout=0.01)
    assert started.is_set()
    assert finished.is_set()  # drained to completion — NOT abandoned mid-flight past the guard


async def test_dispatch_self_normalizes_an_error_to_worktree_error():
    """#311 r5: any other failure from ``invoke`` is normalised to WorktreeError — the SAME
    shape dispatch_task raises — so the loop's coder-failure classifier blocks it identically."""

    def invoke(prompt, session_id, *, tool_fence=None):
        raise RuntimeError("the host agent exploded")

    with pytest.raises(worktree.WorktreeError) as ei:
        await coder_seam.dispatch_self(invoke, "p", "s")
    assert "the host agent exploded" in str(ei.value)


async def test_dispatch_self_does_not_double_wrap_an_already_normalized_error():
    """#311: a ``CoderTimeout``/``WorktreeError`` raised BY the invoke passes through as-is —
    never re-wrapped in a second ``coder dispatch failed:`` layer."""

    async def invoke(prompt, session_id, *, tool_fence=None):
        raise worktree.CoderTimeout("host already timed out")

    with pytest.raises(worktree.CoderTimeout) as ei:
        await coder_seam.dispatch_self(invoke, "p", "s")
    assert "host already timed out" in str(ei.value)
    assert "coder dispatch failed" not in str(ei.value)  # not double-wrapped


# ── #226 S1: persist a finished gen's snapshot as a `coder-monitor:` bead comment ──


class _FakeStore:
    """Captures ``comment(fid, text)`` calls — stands in for the board store the
    persist factory returns, so no `br`/beads is needed."""

    def __init__(self):
        self.comments: list[tuple[str, str]] = []

    def comment(self, fid, text):
        self.comments.append((fid, text))


def test_progress_end_persists_a_coder_monitor_snapshot_comment(monkeypatch):
    """r1/r2: a finished gen writes ONE `coder-monitor: {…}` JSON comment whose payload
    carries the snapshot (tier, elapsed_s, recent_tools, plan, verify, stop_reason)."""
    cs = coder_seam
    cs.progress_new_run("bd-226a")
    clock = [100.0]
    monkeypatch.setattr(cs, "_monotonic", lambda: clock[0])
    store = _FakeStore()
    monkeypatch.setattr(cs, "_store_factory", lambda: store)

    cs.progress_begin("bd-226a", 1, "smart")
    cs.progress_tool("bd-226a", 1, {"phase": "start", "id": "t1", "name": "bash", "input": '{"command": "pytest"}'})
    cs.progress_plan("bd-226a", 1, [{"content": "do it", "status": "in_progress"}])
    cs.progress_verify("bd-226a", 1, test_cmd="pytest -q", output="1 passed", passed=True)
    cs.progress_stop_reason("bd-226a", 1, "end_turn")
    clock[0] = 107.5
    cs.progress_end("bd-226a", 1)

    assert len(store.comments) == 1
    fid, text = store.comments[0]
    assert fid == "bd-226a"
    assert text.startswith("coder-monitor: ")
    payload = json.loads(text[len("coder-monitor: ") :])
    assert payload["tier"] == "smart"
    assert payload["elapsed_s"] == 7.5  # frozen at progress_end, not 899.0
    assert payload["done"] is True
    assert payload["stop_reason"] == "end_turn"
    assert payload["verify"]["passed"] is True
    assert payload["verify"]["test_cmd"] == "pytest -q"
    assert payload["plan"][0]["content"] == "do it"
    assert isinstance(payload["recent_tools"], list) and payload["recent_tools"]


def test_progress_end_with_no_store_factory_completes_normally(monkeypatch):
    """r3: with no wired store factory (the standalone/test env) progress_end still
    finishes normally — done surfaces, the clock freezes, nothing is persisted."""
    cs = coder_seam
    cs.progress_new_run("bd-226b")
    clock = [10.0]
    monkeypatch.setattr(cs, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(cs, "_store_factory", None)  # explicit: standalone env

    cs.progress_begin("bd-226b", 1, "fast")
    clock[0] = 13.0
    cs.progress_end("bd-226b", 1)  # must not raise
    g = cs.progress_snapshot("bd-226b")["gens"][0]
    assert g["done"] is True
    assert g["elapsed_s"] == 3.0


def test_progress_end_swallows_a_failing_comment_write(monkeypatch):
    """r3: a store whose comment write blows up must never break a build — progress_end
    still completes (done set, clock frozen)."""
    cs = coder_seam
    cs.progress_new_run("bd-226c")
    clock = [0.0]
    monkeypatch.setattr(cs, "_monotonic", lambda: clock[0])

    class _BoomStore:
        def comment(self, fid, text):
            raise RuntimeError("br exploded")

    monkeypatch.setattr(cs, "_store_factory", lambda: _BoomStore())
    cs.progress_begin("bd-226c", 1, "smart")
    clock[0] = 4.0
    cs.progress_end("bd-226c", 1)  # must not raise
    g = cs.progress_snapshot("bd-226c")["gens"][0]
    assert g["done"] is True
    assert g["elapsed_s"] == 4.0


def test_progress_end_handles_a_factory_returning_no_store(monkeypatch):
    """r3: a factory that yields no store (get_store couldn't build one) is a no-op,
    not a crash."""
    cs = coder_seam
    cs.progress_new_run("bd-226d")
    monkeypatch.setattr(cs, "_store_factory", lambda: None)
    cs.progress_begin("bd-226d", 1)
    cs.progress_end("bd-226d", 1)  # must not raise
    assert cs.progress_snapshot("bd-226d")["gens"][0]["done"] is True


def test_progress_end_persists_once_across_duplicate_calls(monkeypatch):
    """r4: the persist rides the idempotent first-close (progress_end guards on `not
    b.done`), so a duplicate exit-path call writes no second comment."""
    cs = coder_seam
    cs.progress_new_run("bd-226e")
    store = _FakeStore()
    monkeypatch.setattr(cs, "_store_factory", lambda: store)
    cs.progress_begin("bd-226e", 1, "fast")
    cs.progress_end("bd-226e", 1)
    cs.progress_end("bd-226e", 1)  # second close: no-op, no second write
    assert len(store.comments) == 1


def test_set_store_factory_wires_then_clears_the_persist_accessor():
    """r4: the public setter register() calls — wiring a factory makes progress_end
    persist through it; clearing it (None) restores the standalone no-op."""
    cs = coder_seam
    store = _FakeStore()
    try:
        cs.set_store_factory(lambda: store)
        cs.progress_new_run("bd-226f")
        cs.progress_begin("bd-226f", 1, "smart")
        cs.progress_end("bd-226f", 1)
        assert len(store.comments) == 1

        cs.set_store_factory(None)  # cleared → a subsequent gen persists nothing
        cs.progress_new_run("bd-226g")
        cs.progress_begin("bd-226g", 1)
        cs.progress_end("bd-226g", 1)
        assert len(store.comments) == 1  # unchanged
    finally:
        cs.set_store_factory(None)


# ── F7: the tapped dispatch goes through C1's PUBLIC dispatch_tapped seam ─────────


def test_only_the_legacy_tap_reaches_host_internals():
    """r1 (F7), corrected: the PREFERRED path reaches no host internals — but the
    public C1 seam (`coding_agent.dispatch_tapped`) shipped in protoAgent AFTER this
    plugin's `min_protoagent_version` floor, so on every currently-released host it is
    absent. F7's first cut degraded straight to the UNTAPPED dispatch there, which
    records a gen with no tools/thoughts/plan — the coder monitor silently stopped
    working on a live board the day it landed.

    So the contract this pins is not "zero privates anywhere"; it is: every private
    reach lives INSIDE the one legacy fallback function, which exists to be deleted
    once a C1-carrying release is the floor. Everything outside it stays public-only."""
    import re
    from pathlib import Path

    src = Path(coder_seam.__file__).read_text()
    marker = "async def _dispatch_coder_tapped_legacy("
    assert marker in src, "the legacy tap is the only sanctioned home for private reaches"
    legacy_start = src.index(marker)
    nxt = src.find("\nasync def ", legacy_start + len(marker))
    legacy_end = nxt if nxt != -1 else len(src)
    outside = src[:legacy_start] + src[legacy_end:]

    leaked = []
    for m in re.finditer(r"from\s+plugins\.(?:coding_agent|delegates)[\w.]*\s+import\s+([^\n#]+)", outside):
        for name in re.split(r"[,\s()]+", m.group(1).strip()):
            if name and name.startswith("_"):
                leaked.append(name)
    assert leaked == [], f"private host imports outside the legacy tap: {leaked}"
    for forbidden in ("_client_for", "_drop_client", "_make_permission", "._spec(", ".kill_now(", "._permission"):
        assert forbidden not in outside, f"host internal {forbidden!r} reached outside the legacy tap"

    # The preferred path is still the public seam, and it is tried FIRST.
    assert "from plugins.coding_agent import dispatch_tapped" in src
    assert src.index("_import_dispatch_tapped()") < legacy_start


@dataclass(frozen=True)
class _TappedResult:
    """Stand-in for the host's ``plugins.coding_agent.acp_client.TappedResult`` — the
    frozen record C1's ``dispatch_tapped`` returns. The host is not importable from the
    plugin's test env, so the shape is mirrored here: the reply the board passes up,
    plus the wire signals (usage / plan / stop_reason / dead_end) the seam snapshots off
    the client at end-of-turn INSTEAD of forwarding through callbacks."""

    reply: str
    usage: dict | None = None
    plan: list | None = None
    stop_reason: str | None = None
    dead_end: str | None = None


@dataclass
class _FakeCoder:
    """A coder Delegate stand-in carrying the fields dispatch_coder_tapped scopes
    (workdir/manage_git/env), so the board-policy overrides are exercised for real."""

    workdir: str = ""
    manage_git: bool = True
    env: dict = field(default_factory=dict)


async def test_dispatch_coder_tapped_uses_the_public_seam_when_present(monkeypatch):
    """r2: with the public C1 seam present (injected as a fake), the tap drives IT —
    not the untapped fallback — hands it the scoped delegate (board owns git, worktree
    is the workdir), and every forwarded signal lands in the live buffer."""
    coder_seam._progress.clear()
    seen = {}

    async def _fake_seam(delegate, prompt, *, on_tool=None, on_thought=None, on_text=None, timeout=None):
        # MIRRORS THE REAL SEAM EXACTLY (plugins/coding_agent.dispatch_tapped): three
        # keyword-only stream callbacks + timeout, and NO **kwargs — so a rename on
        # either side raises TypeError here. The permissive fake this replaced accepted
        # `tool_callback=`/`usage_callback=`/… , names the seam never had, which is why
        # a green suite shipped a call that died on the first host that carried C1.
        seen["delegate"] = delegate
        seen["prompt"] = prompt
        seen["timeout"] = timeout
        await on_thought("thinking about it")
        await on_tool({"phase": "start", "id": "t1", "name": "read_file", "input": '{"path": "x.py"}'})
        await on_text("all done.")
        # The wire signals ride the RESULT, not callbacks.
        return _TappedResult(
            reply="the tapped reply",
            usage={"used": 7, "size": 70},
            plan=[{"content": "do the thing", "status": "in_progress"}],
            stop_reason="end_turn",
        )

    def _no_fallback(*a, **k):
        raise AssertionError("must not fall back to worktree.dispatch_coder when the seam is present")

    monkeypatch.setattr(worktree, "dispatch_coder", _no_fallback)

    out = await coder_seam.dispatch_coder_tapped(
        _FakeCoder(manage_git=True),
        "/wt/cand",
        "do it",
        fid="bd-tap",
        gen=3,
        tier="smart",
        _dispatch_tapped=_fake_seam,
    )
    assert out == "the tapped reply"
    # board policy applied on the scoped copy handed to the seam
    assert seen["delegate"].workdir == "/wt/cand"
    assert seen["delegate"].manage_git is False  # the BOARD owns git
    assert seen["prompt"] == "do it"
    # every forwarded signal recorded on the gen
    (g,) = coder_seam.progress_snapshot("bd-tap")["gens"]
    assert g["gen"] == 3 and g["tier"] == "smart"
    assert "thinking about it" in g["thought_tail"]
    assert g["current_tool"]["name"] == "read_file"
    assert g["current_tool"]["kind"] == "read"
    assert g["usage"] == {"used": 7, "size": 70}
    assert g["plan"][0]["content"] == "do the thing"
    assert g["answer_tail"].endswith("all done.")
    assert g["stop_reason"] == "end_turn"
    assert g["done"] is True  # the gen closed on the success path


async def test_dispatch_coder_tapped_sanitizes_env_on_the_seam_path(monkeypatch):
    """The env allowlist (#142) is applied on the tapped path too, not just the
    fallback: the scoped delegate the seam receives carries the sanitized overlay."""
    coder_seam._progress.clear()
    monkeypatch.setenv("AGENT_NAME", "host-agent")
    monkeypatch.setenv("A2A_TOKEN", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    seen = {}

    async def _fake_seam(delegate, prompt, *, on_tool=None, on_thought=None, on_text=None, timeout=None):
        seen["env"] = dict(delegate.env)
        return _TappedResult(reply="ok")

    await coder_seam.dispatch_coder_tapped(
        _FakeCoder(),
        "/wt",
        "do it",
        fid="bd-env",
        gen=1,
        env_passthrough=["A2A_TOKEN"],
        _dispatch_tapped=_fake_seam,
    )
    assert seen["env"]["A2A_TOKEN"] == "secret"  # whitelisted → present
    assert "AGENT_NAME" not in seen["env"]  # host identity stripped


async def test_dispatch_coder_tapped_normalizes_a_seam_failure_to_worktree_error(monkeypatch):
    """A real dispatch failure BELOW the seam is normalised to WorktreeError and
    propagates — it does NOT silently degrade to an untapped retry (the fallback is
    reserved for a seam that is ABSENT, not one that raised). The gen still closes."""
    coder_seam._progress.clear()

    async def _boom_seam(delegate, prompt, *, timeout=None, **_cbs):
        raise RuntimeError("acp session died")

    def _no_fallback(*a, **k):
        raise AssertionError("a seam failure must not fall back to an untapped dispatch")

    monkeypatch.setattr(worktree, "dispatch_coder", _no_fallback)

    try:
        await coder_seam.dispatch_coder_tapped(
            _FakeCoder(), "/wt", "x", fid="bd-err", gen=1, _dispatch_tapped=_boom_seam
        )
        raised = False
    except worktree.WorktreeError as exc:
        raised = True
        assert "acp session died" in str(exc)
    assert raised
    assert coder_seam.progress_snapshot("bd-err")["gens"][0]["done"] is True


async def test_dispatch_coder_tapped_maps_a_seam_timeout_to_coder_timeout(monkeypatch):
    """A configured timeout hard-bounds the seam via asyncio.wait_for exactly as the
    untapped path does — a fired deadline surfaces as CoderTimeout, not a raw error."""
    import asyncio as real_asyncio

    coder_seam._progress.clear()

    async def _hang_seam(delegate, prompt, *, timeout=None, **_cbs):
        await real_asyncio.sleep(10)
        return "never"

    async def _boom_wait_for(coro, timeout):
        coro.close()
        raise real_asyncio.TimeoutError()

    monkeypatch.setattr("project_board.coder_seam.asyncio.wait_for", _boom_wait_for)

    try:
        await coder_seam.dispatch_coder_tapped(
            _FakeCoder(), "/wt", "x", fid="bd-to", gen=1, timeout=0.01, _dispatch_tapped=_hang_seam
        )
        raised = False
    except worktree.CoderTimeout:
        raised = True
    assert raised


async def test_untapped_only_when_neither_the_public_seam_nor_the_internals_are_reachable(monkeypatch):
    """Rung 3, and ONLY rung 3: in this host-free env the public C1 seam is absent AND
    `plugins.coding_agent` cannot be imported, so the legacy tap can't run either — the
    dispatch degrades to the untapped worktree.dispatch_coder and still records the gen.
    A host that has the internals takes rung 2 instead (the test below)."""
    coder_seam._progress.clear()
    assert coder_seam._import_dispatch_tapped() is None  # genuinely absent here (no host)
    seen = {}

    async def _fallback(coder, wt, prompt, *, timeout=None, env_passthrough=()):
        seen["args"] = (wt, prompt)
        return "fallback reply"

    monkeypatch.setattr(worktree, "dispatch_coder", _fallback)
    out = await coder_seam.dispatch_coder_tapped(_FakeCoder(), "/wt/x", "do it", fid="bd-fb", gen=4, tier="fast")
    assert out == "fallback reply"
    assert seen["args"] == ("/wt/x", "do it")
    (g,) = coder_seam.progress_snapshot("bd-fb")["gens"]
    assert g["gen"] == 4 and g["tier"] == "fast" and g["done"] is True


# ── rung 2: a pre-C1 host still gets a LIVE monitor (the regression Josh hit) ──────


class _FakeAcpClient:
    """Enough of the pooled AcpClient for the legacy tap: it streams a tool event and a
    thought through the callbacks it is handed, then returns a reply."""

    def __init__(self):
        self.last_usage = {"used": 12, "size": 100}
        self.last_plan = [{"content": "do the thing", "status": "in_progress"}]
        self.last_stop_reason = "end_turn"
        self._permission = None
        self.killed = False

    def kill_now(self):
        self.killed = True

    async def prompt(self, text, *, tool_callback=None, thought_callback=None, text_callback=None, timeout=None):
        await tool_callback({"phase": "start", "id": "t1", "name": "edit_file", "input": '{"path": "store.py"}'})
        await thought_callback("weighing the options")
        await text_callback("here is what I changed")
        await tool_callback({"phase": "end", "id": "t1", "name": "edit_file", "status": "completed"})
        return "the coder reply"


def _install_pre_c1_host(monkeypatch, client):
    """Put a `plugins.coding_agent` (WITHOUT dispatch_tapped) + `plugins.delegates`
    into sys.modules — a host that predates C1 but has the internals the legacy tap
    uses. Exactly the shape of every currently-released protoAgent."""
    import sys
    import types as _t

    torn = []
    pkg = _t.ModuleType("plugins")
    pkg.__path__ = []
    ca = _t.ModuleType("plugins.coding_agent")
    ca.__path__ = []
    ca._client_for = lambda spec: client
    ca._drop_client = lambda spec: None
    ca._make_permission = lambda spec: "policy"
    acp = _t.ModuleType("plugins.coding_agent.acp_client")
    acp.AcpError = type("AcpError", (Exception,), {})
    dele = _t.ModuleType("plugins.delegates")
    dele.__path__ = []
    ad = _t.ModuleType("plugins.delegates.adapters")
    ad.DelegateError = type("DelegateError", (Exception,), {})

    class _Adapter:
        @staticmethod
        def _spec(scoped):
            return {"workdir": getattr(scoped, "workdir", None)}

        async def forget_session(self, scoped):
            return None

        async def teardown(self, scoped):
            torn.append(scoped)
            return True

    ad.ADAPTERS = {"acp": _Adapter()}
    for name, mod in (
        ("plugins", pkg),
        ("plugins.coding_agent", ca),
        ("plugins.coding_agent.acp_client", acp),
        ("plugins.delegates", dele),
        ("plugins.delegates.adapters", ad),
    ):
        monkeypatch.setitem(sys.modules, name, mod)
    return torn


async def test_a_pre_c1_host_taps_the_live_stream_through_the_legacy_path(monkeypatch):
    """RED-IS-REACHABLE — this is the regression that shipped: F7 moved the tap onto
    `coding_agent.dispatch_tapped`, which landed in protoAgent AFTER this plugin's
    supported floor. On every released host the seam is absent, F7's first cut went
    straight to the UNTAPPED dispatch, and the drawer recorded a gen with ZERO tools,
    no thoughts and no plan — the coder monitor silently stopped working. Rung 2 keeps
    it alive: the same host must produce a real stream."""
    coder_seam._progress.clear()
    coder_seam._LEGACY_TAP_WARNED = False
    client = _FakeAcpClient()
    torn = _install_pre_c1_host(monkeypatch, client)
    assert coder_seam._import_dispatch_tapped() is None  # the public seam really is absent

    async def _never(*a, **kw):
        raise AssertionError("must not degrade to the untapped dispatch on a host with internals")

    monkeypatch.setattr(worktree, "dispatch_coder", _never)

    out = await coder_seam.dispatch_coder_tapped(_FakeCoder(), "/wt/x", "do it", fid="bd-l1", gen=2, tier="smart")

    assert out == "the coder reply"
    (g,) = coder_seam.progress_snapshot("bd-l1")["gens"]
    assert g["gen"] == 2 and g["tier"] == "smart" and g["done"] is True
    assert g["recent_tools"], "the monitor recorded NO tools — this is the broken state"
    assert g["recent_tools"][0]["name"] == "edit_file"
    assert "weighing the options" in g["thought_tail"]
    assert "here is what I changed" in g["answer_tail"]
    assert g["usage"] == {"used": 12, "size": 100}
    assert g["plan"] and g["plan"][0]["content"] == "do the thing"
    assert g["stop_reason"] == "end_turn"
    assert torn, "the worktree-scoped subprocess must still be torn down"


async def test_the_legacy_tap_warns_once_so_the_degrade_is_never_silent(monkeypatch, caplog):
    """The first cut degraded with no log line at all, which is why it took a human
    noticing an empty drawer to find it."""
    coder_seam._progress.clear()
    coder_seam._LEGACY_TAP_WARNED = False
    _install_pre_c1_host(monkeypatch, _FakeAcpClient())
    with caplog.at_level("WARNING"):
        await coder_seam.dispatch_coder_tapped(_FakeCoder(), "/wt/x", "a", fid="bd-w1", gen=1)
        await coder_seam.dispatch_coder_tapped(_FakeCoder(), "/wt/x", "b", fid="bd-w2", gen=1)
    hits = [r for r in caplog.records if "predates coding_agent.dispatch_tapped" in r.message]
    assert len(hits) == 1, "warn once per process, not once per dispatch"


async def test_the_seam_is_called_with_c1s_exact_keyword_names(monkeypatch):
    """The signature contract, pinned. F7 shipped a call using the PRIVATE client's
    kwarg names (`tool_callback` / `thought_callback` / `text_callback` plus
    usage/plan/stop_reason callbacks the seam never had). No host carried C1 yet, so no
    test could catch it — every fake accepted whatever it was handed. The day a release
    carried the seam, every dispatch died on `dispatch_tapped() got an unexpected
    keyword argument 'tool_callback'` and the board blocked cards terminally.

    So assert the wire names directly: exactly `on_tool`, `on_thought`, `on_text`,
    `timeout`, and nothing else. If C1 renames a parameter, this fails HERE."""
    coder_seam._progress.clear()
    got = {}

    async def _recording_seam(delegate, prompt, /, **kwargs):
        got.update(kwargs)
        return _TappedResult(reply="ok")

    await coder_seam.dispatch_coder_tapped(
        _FakeCoder(), "/wt", "do it", fid="bd-sig", gen=1, timeout=None, _dispatch_tapped=_recording_seam
    )
    assert set(got) == {"on_tool", "on_thought", "on_text", "timeout"}
    assert callable(got["on_tool"]) and callable(got["on_thought"]) and callable(got["on_text"])


async def test_a_seam_returning_a_non_result_is_refused_not_passed_up(monkeypatch):
    """A reply is written into a PR body, so an unexpected return SHAPE must fail loudly
    rather than travel up the board's reply path. A bare string still works (the other
    known shape); an object that is neither is a WorktreeError."""
    coder_seam._progress.clear()

    async def _bare_string_seam(delegate, prompt, *, on_tool=None, on_thought=None, on_text=None, timeout=None):
        return "just the reply"

    assert (
        await coder_seam.dispatch_coder_tapped(
            _FakeCoder(), "/wt", "x", fid="bd-bare", gen=1, _dispatch_tapped=_bare_string_seam
        )
        == "just the reply"
    )

    async def _junk_seam(delegate, prompt, *, on_tool=None, on_thought=None, on_text=None, timeout=None):
        return {"reply": "in a dict, not a result"}

    with pytest.raises(worktree.WorktreeError, match="expected a TappedResult"):
        await coder_seam.dispatch_coder_tapped(
            _FakeCoder(), "/wt", "x", fid="bd-junk", gen=1, _dispatch_tapped=_junk_seam
        )
    assert coder_seam.progress_snapshot("bd-junk")["gens"][0]["done"] is True  # gen still closed
