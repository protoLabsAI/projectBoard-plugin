"""Tests for the ADR 0064 P2 board seam (coder_seam.py).

Covers the dispatch DECISION (``should_use_solve``) — the honest-degrade gate that
must fire false the instant any one of coder/acceptance/test-cmd is missing — and
``dispatch()``'s own orchestration (worktree-per-candidate, promote the winner, reap
the losers, surface gens-spent, raise ``SolveExhausted`` on a spent budget) with the
`coder` plugin's ``solve``/``Budget``/``Verdict`` injected as fakes, so none of this
needs the (separate, git-URL-installed) `coder` plugin to be present."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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

    async def _dispatch(coder, wt, prompt, *, timeout=None):
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

    async def _dispatch(coder, wt, prompt, *, timeout=None):
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

    async def _dispatch(coder, wt, prompt, *, timeout=None):
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

    async def _dispatch(coder, wt, prompt, *, timeout=None):
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

    async def _dispatch(coder, wt, prompt, *, timeout=None):
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


# ── resolve_delegate: shared by loop.py and api.py's test-rung route ─────────────


def test_resolve_delegate_returns_none_when_delegates_plugin_absent():
    """`plugins.delegates` is genuinely absent in this standalone test env — the
    honest-degrade case in production too when the plugin's disabled."""
    assert coder_seam.resolve_delegate("anything", "acp") is None
