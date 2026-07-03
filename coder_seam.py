"""The P2 board seam (ADR 0064): dispatch a feature's build through the `coder`
plugin's execution-grounded ``solve()`` ladder instead of a single
``delegate_to(acp)`` shot — greedy → best-of-k → tree-search, gated on the
feature's acceptance tests actually PASSING in a real worktree, never an LLM judge.

**Composes** `plugins.coder.solve` (a separate, git-URL-installed plugin — imported
lazily/best-effort so this repo carries no hard dependency on it and no import-time
coupling) with THIS repo's own worktree primitives. The coder plugin never sees a
board worktree; it only supplies the deterministic ladder (`solve()`, `Budget`,
`Verdict`). Each candidate the ladder tries gets its OWN throwaway worktree — the
"independent-parallel acp attempts" `coder`'s own generator module already flags as
"the P2 path" (`plugins/coder/generate.py`) — and the winning (test-passing)
candidate is PROMOTED to the feature's canonical worktree/branch so the rest of the
drive (fixups, the pre-PR local gate, `open_pr`, the CI bounce, tier escalation) is
UNCHANGED; every other candidate is reaped.

**Honest degrade** (ADR 0064's no-LLM-judge rule, applied at the board layer): the
dispatch decision (``should_use_solve``) requires ALL THREE — the `coder` plugin
importable (the host has it enabled), the feature's acceptance criteria present
(the Ready gate's oracle), and a configured, runnable acceptance-test command (the
actual executable verifier — `solve()` cannot run prose). Missing any of the three
⇒ the caller falls back to today's single ``delegate_to(acp)`` shot; never a silent
best-of-k/judge substitute.

**Deferred** (see the ADR + the PR that lands this): compiling EARS acceptance
criteria into a generated test file. The simplest-correct path used here instead:
the coder is already prompted (``loop._build_prompt``) to write tests satisfying
the acceptance criteria as part of its definition of done; this module's ``verify``
just RUNS whatever tests exist in a candidate's worktree via the configured command
and gates on its exit code — real execution, no fabricated grounding.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from . import worktree

log = logging.getLogger("protoagent.plugins.project_board")


class SolveExhausted(worktree.WorktreeError):
    """``coder.solve()`` spent its whole generation budget against this feature's
    acceptance tests and no candidate passed. A CAPABILITY failure for the CURRENT
    model tier — real diffs existed, they just failed the tests, so this is NOT
    "no diff" — but the loop treats it exactly like ``NoChangesError``/
    ``CoderTimeout``: escalate a configured tier ladder, or block. Never opens a PR
    on an unverified best-partial (ADR 0064's honest-degrade contract)."""


def _import_solve():
    """Best-effort import of the `coder` plugin's solve library. Returns the module,
    or ``None`` — `coder` is a separate, git-URL-installed plugin (ADR 0064), not a
    dependency of this one, and it ships DISABLED by default, so absent/disabled is
    the expected common case (not an error worth logging)."""
    try:
        import importlib

        return importlib.import_module("plugins.coder.solve")
    except Exception:  # noqa: BLE001 — coder plugin absent/disabled → honest degrade
        return None


def should_use_solve(feature: dict, *, test_cmd: str, _solve_mod=None) -> bool:
    """The P2 dispatch decision (ADR 0064): use `coder.solve()` only when ALL of —
    the `coder` plugin is importable, the feature carries acceptance criteria (the
    Ready gate's oracle), and a runnable acceptance-test command is configured (the
    actual verifier `solve()` gates on — prose acceptance criteria alone isn't
    executable). Missing any ⇒ False, the honest degrade to a single delegate_to(acp)
    shot. ``_solve_mod`` is a test-injection seam; production callers never pass it —
    the real best-effort import happens here."""
    mod = _solve_mod if _solve_mod is not None else _import_solve()
    if mod is None:
        return False
    if not str(feature.get("acceptance_criteria") or "").strip():
        return False
    if not str(test_cmd or "").strip():
        return False
    return True


def _augment_prompt(task: str, feedback: Optional[str]) -> str:
    """Fold the ladder's failing-test feedback into the next candidate's prompt.
    Every candidate gets a FRESH worktree off base (see ``_WorktreeSolveAdapter`` —
    the same "fresh-both" discipline ``worktree.dispatch_coder`` already documents
    for re-dispatches), so the coder is told explicitly there is no prior diff to
    build on in THIS worktree — only the failure to fix."""
    if not feedback:
        return task
    return (
        f"{task}\n\n"
        "## Your previous attempt FAILED the acceptance tests — fix exactly this\n"
        "This is a fresh worktree (no prior diff here); re-implement with the failure "
        f"below in mind:\n{feedback.strip()}\n"
    )


class _WorktreeSolveAdapter:
    """Adapts `coder.solve()`'s ``generate``/``verify`` contract onto board
    worktrees. `solve()` treats a candidate as an opaque string; here that string is
    a candidate's WORKTREE PATH, not code text — the coder edits files, it doesn't
    return a source string. Each ``generate()`` call creates a fresh throwaway
    worktree, dispatches the ACP coder into it, and hands the path back; ``verify()``
    then runs the acceptance-test command in that same worktree and reports real
    pass/fail. Tracks every candidate worktree it creates so the caller can promote
    the winner and reap the losers."""

    def __init__(
        self,
        *,
        repo: str,
        base: str,
        root: str,
        fid: str,
        coder,
        dispatch_timeout: Optional[float],
        test_cmd: str,
        test_timeout: float,
        verdict_cls,
    ):
        self.repo = repo
        self.base = base
        self.root = root
        self.fid = fid
        self.coder = coder
        self.dispatch_timeout = dispatch_timeout
        self.test_cmd = test_cmd
        self.test_timeout = test_timeout
        self.verdict_cls = verdict_cls  # `plugins.coder.solve.Verdict` — passed in, never imported here
        self.candidates: list[tuple[str, str]] = []  # (worktree_path, branch)
        # `git worktree add` against the SAME repo must not run concurrently (best-
        # of-k dispatches `generate()` via asyncio.gather) — serialize just that
        # step; the slow part (the coder dispatch) still runs in parallel.
        self._wt_lock = asyncio.Lock()
        self._n = 0

    async def generate(self, task: str, *, feedback: Optional[str] = None) -> str:
        self._n += 1
        cid = f"{self.fid}.g{self._n}"
        async with self._wt_lock:
            wt, branch = await worktree.create_worktree(self.repo, self.base, cid, self.root)
        self.candidates.append((wt, branch))
        await worktree.dispatch_coder(self.coder, wt, _augment_prompt(task, feedback), timeout=self.dispatch_timeout)
        return wt

    async def verify(self, candidate_wt: str):
        Verdict = self.verdict_cls
        try:
            proc = await asyncio.create_subprocess_shell(
                self.test_cmd,
                cwd=candidate_wt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            return Verdict(passed=False, total=1, failed=1, output=f"could not launch acceptance tests: {exc}")
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.test_timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            # Unlike the pre-PR local gate (which fails OPEN on a timeout — a broken
            # gate must never block otherwise-good work), THIS is the ladder's own
            # search oracle: a candidate we couldn't confirm passed must never be
            # silently treated as passing, or we'd be faking grounding.
            return Verdict(
                passed=False, total=1, failed=1, output=f"acceptance tests timed out after {self.test_timeout:.0f}s"
            )
        text = (out or b"").decode("utf-8", "replace").strip()
        ok = proc.returncode == 0
        return Verdict(
            passed=ok,
            total=1,
            failed=0 if ok else 1,
            failing=[] if ok else [f"{self.test_cmd!r} (exit {proc.returncode})"],
            output=text[-4000:],
        )


RecordGens = Callable[[int], None]


async def dispatch(
    *,
    task: str,
    coder,
    repo: str,
    base: str,
    root: str,
    fid: str,
    dispatch_timeout: Optional[float],
    test_cmd: str,
    test_timeout: float,
    budget: int,
    k: int,
    tree_depth: int,
    record_gens: Optional[RecordGens] = None,
    _solve=None,
    _budget_cls=None,
    _verdict_cls=None,
) -> tuple[str, str, str]:
    """Run the execution-grounded ladder for one feature build.

    Returns ``(worktree, branch, result_text)`` on a passing candidate — the SAME
    3-tuple shape ``_dispatch_max_mode`` returns, so the caller's downstream drive
    (fixups, local gate, ``open_pr``) is unchanged. Raises :class:`SolveExhausted`
    (a capability failure) when the budget is spent with no passing candidate, after
    reaping every candidate worktree it created.

    ``record_gens`` (if given) is called with ``result.gens_spent`` exactly once,
    win or lose — the cost accounting (ADR 0064) must survive a failed search too.
    ``_solve``/``_budget_cls``/``_verdict_cls`` are test-injection seams for
    ``solve()``/``Budget``/``Verdict``; production callers never pass them (the real
    import happens here, deferred so this module carries no hard dependency on the
    `coder` plugin)."""
    if _solve is not None:
        solve, Budget, Verdict = _solve, _budget_cls, _verdict_cls
    else:
        from plugins.coder.solve import Budget, Verdict, solve

    adapter = _WorktreeSolveAdapter(
        repo=repo,
        base=base,
        root=root,
        fid=fid,
        coder=coder,
        dispatch_timeout=dispatch_timeout,
        test_cmd=test_cmd,
        test_timeout=test_timeout,
        verdict_cls=Verdict,
    )
    result = await solve(
        task,
        generate=adapter.generate,
        verify=adapter.verify,
        budget=Budget(budget),
        k=k,
        tree_depth=tree_depth,
    )
    if record_gens is not None:
        record_gens(result.gens_spent)

    if not result.passed or not result.solution:
        for wt, branch in adapter.candidates:
            await worktree.remove_worktree(repo, wt, branch)
        detail = result.verdict.feedback() if result.verdict else ""
        log.info(
            "[project_board] %s coder.solve exhausted (rung=%s, gens=%d/%d) — no candidate passed",
            fid,
            result.rung,
            result.gens_spent,
            budget,
        )
        raise SolveExhausted(
            f"coder.solve exhausted after {result.gens_spent} generation(s) (rung={result.rung}): "
            f"{detail or result.note}"
        )

    win_wt = result.solution
    win_branch = next(b for wt, b in adapter.candidates if wt == win_wt)
    canon_wt, canon_branch = await worktree.promote_worktree(repo, win_wt, win_branch, fid, root)
    for wt, branch in adapter.candidates:
        if wt != win_wt:
            await worktree.remove_worktree(repo, wt, branch)
    log.info(
        "[project_board] %s coder.solve verified by acceptance tests (rung=%s, gens=%d/%d)",
        fid,
        result.rung,
        result.gens_spent,
        budget,
    )
    result_text = f"[coder.solve rung={result.rung} gens={result.gens_spent}] {result.note}"
    return canon_wt, canon_branch, result_text
