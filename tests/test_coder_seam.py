"""Tests for the ADR 0064 P2 board seam (coder_seam.py).

Covers the dispatch DECISION (``should_use_solve``) — the honest-degrade gate that
must fire false the instant any one of coder/acceptance/test-cmd is missing — and
``dispatch()``'s own orchestration (worktree-per-candidate, promote the winner, reap
the losers, surface gens-spent, raise ``SolveExhausted`` on a spent budget) with the
`coder` plugin's ``solve``/``Budget``/``Verdict`` injected as fakes, so none of this
needs the (separate, git-URL-installed) `coder` plugin to be present."""

from __future__ import annotations

from dataclasses import dataclass, field

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

    async def _dispatch(coder, wt, prompt, *, timeout=None):
        return f"reply from {wt}"

    async def _remove(repo, wt, branch=""):
        removed.append(wt)

    async def _promote(repo, src_wt, src_branch, fid, root=".worktrees"):
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

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth):
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
    assert "best-of-k" in result and "gens=2" in result
    assert gens == [2]  # cost surfaced exactly once


async def test_dispatch_records_gens_even_on_a_single_greedy_win(monkeypatch):
    _stub_worktree(monkeypatch)

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth):
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


async def test_dispatch_promotes_the_winner_even_if_record_gens_raises(monkeypatch):
    """`store.record_gens_spent` documents itself as fire-and-forget ("a br hiccup
    here must never fail the build the way a missing PR would") — a `BoardError`
    (lock contention, a flaky `br` call) out of `record_gens` must never discard an
    already-verified winning candidate or leak it un-promoted."""
    created, removed, promoted = _stub_worktree(monkeypatch)

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth):
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

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth):
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

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth):
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

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth):
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

    async def _dispatch(coder, wt, prompt, *, timeout=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise _Boom("candidate coder timed out")
        return "ok"

    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth):
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

    async def _dispatch(coder, wt, prompt, *, timeout=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise _Boom("candidate coder timed out")
        return "ok"

    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth):
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

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth):
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

    async def _dispatch(coder, wt, prompt, *, timeout=None):
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
