"""The P2 board seam (ADR 0064): dispatch a feature's build through the `coder`
plugin's execution-grounded ``solve()`` ladder instead of a single
``delegate_to(acp)`` shot — greedy → best-of-k → tree-search → fusion, gated on
the feature's acceptance tests actually PASSING in a real worktree, never an LLM
judge.

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

**Rung 4 — fusion (ADR 0064 P3).** Fusion (e.g. ``protolabs/fusion``) is a strong
*generator* but, per the ADR, it **can't tool-call** — unlike the ``acp`` coder
(a real edit/verify session in the worktree), it can only return a plain chat
completion. So its candidate generation is a DIFFERENT shape from the ACP rungs:
``_fusion_prompt`` hands it the task + the CURRENT content of the feature's
declared ``files_to_modify`` (read from the base repo — fusion has no tool access
to look these up itself) and asks for the complete, final content of every file it
creates or changes; ``_parse_fusion_files`` extracts ``{path: content}`` from the
reply; ``_WorktreeSolveAdapter.generate_fusion`` writes those files into a fresh
worktree (the same throwaway-per-candidate discipline as the ACP rungs) and hands
the path to the SAME ``verify()`` — real acceptance tests, same oracle, no separate
judge. Wholesale file replacement (not a unified diff) is deliberate: an LLM
completion reliably reproduces a full file; a hand-rolled patch with drifted
context lines is a common failure mode `git apply` doesn't forgive. Only reached
when a ``fusion_delegate`` is configured (an ``openai``-type Delegate, already
resolved by the caller) — absent that, ``solve()`` gets ``fusion_generate=None``
and stops at tree-search exactly as before (honest degrade, unchanged).

**Fusion + large files — honest-degrade, not silent truncation.** Whole-file
replacement only works when a real completion can (a) see the WHOLE current file
and (b) reproduce the WHOLE new one — and the tighter constraint is usually the
OUTPUT side: a delegate's own ``max_tokens`` (often ~1024 by default, ~4K chars)
can truncate the response well before a merely-medium file's size, and
``OpenAiAdapter.dispatch`` doesn't surface ``finish_reason`` to tell a caller that
happened. So this module never attempts a full-file rewrite it can't stand behind:
``fusion_viable_for_files`` gates on the feature's ACTUAL on-disk file sizes
(per-file and combined) BEFORE fusion is ever dispatched — callers (``loop.py``,
``api.py``) check it and treat "not viable" exactly like "no fusion_delegate
configured" (``fusion_delegate=None`` for that dispatch). As a defensive backstop
(in case a caller skips the gate — direct ``test_rung`` callers, say)
``generate_fusion`` ALSO refuses to write a candidate file back over a
significantly larger original — a shrunk "complete" rewrite is far more likely a
truncated one than an honest tiny file than a real edit, so that file is left
unwritten (verify() then judges the candidate on what's actually there) rather
than risking data loss.

**``test_rung`` (operator-only diagnostic).** Verifying a specific rung — fusion
especially, only otherwise reached after three cheaper rungs fail — shouldn't
require contriving a task hard enough to fail its way there. ``test_rung`` runs
ONE named rung once against a feature's real acceptance tests in a throwaway
worktree that's ALWAYS reaped, win or lose — never promoted, no PR. Exposed via
api.py's ``test-rung`` route with no ``@tool`` wrapper, so it's operator-only,
not something the board's own lead agent can reach for itself."""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Callable

from . import config, worktree

log = logging.getLogger("protoagent.plugins.project_board")


def resolve_delegate(name: str, expect_type: str):
    """Look up a live delegate by name from the delegates registry. Returns the
    Delegate or None (not configured / wrong type / plugin disabled). Shared by
    ``loop.py`` (coder/reviewer/fusion resolution in the real dispatch path) and
    ``api.py`` (the operator-only test-rung route) — one lookup, not two copies."""
    try:
        from plugins.delegates.registry import DelegateRegistry
        from plugins.delegates.store import merged_delegates

        d = DelegateRegistry(merged_delegates()).get(name)
    except Exception:  # noqa: BLE001 — delegates plugin may be disabled
        return None
    if d is None or d.type != expect_type:
        return None
    return d


# ``### path/to/file.py`` header, then a fenced block (any/no language hint) holding
# that file's COMPLETE new content. Deliberately simple/strict: a fusion completion
# that doesn't follow the format parses to no files, which just fails verify() like
# any other empty candidate — never a silent partial/mangled write.
_FUSION_FILE_RE = re.compile(r"^###\s+(\S.+?)\s*$\n```[^\n]*\n(.*?)```", re.MULTILINE | re.DOTALL)

# Defaults for `fusion_viable_for_files` — deliberately conservative. The binding
# constraint is usually the OUTPUT side (a delegate's own `max_tokens`, often
# ~1024 ⇒ ~4K chars, silently truncating a reply the adapter doesn't even expose
# `finish_reason` for), not this repo's own read logic — these caps exist so
# fusion is refused for a feature's files BEFORE a doomed rewrite is attempted,
# not tuned to "how much can Python read." Configurable per-board — see loop.py's
# `coder_solve_fusion_max_file_chars` / `_max_total_chars`.
FUSION_MAX_FILE_CHARS_DEFAULT = 8_000
FUSION_MAX_TOTAL_CHARS_DEFAULT = 16_000

# Defense-in-depth for `generate_fusion`'s write guard: a returned file under this
# fraction of the ORIGINAL file's size is treated as a likely-truncated rewrite,
# not a legitimately smaller edit, and is refused. Only applies above a minimum
# original size (`_SHRINK_GUARD_MIN_ORIGINAL_CHARS`) — a real small edit to a
# small file (e.g. a 40-char file trimmed to 10) shouldn't trip a "big shrink"
# heuristic meant to catch multi-KB truncation.
_SHRINK_GUARD_RATIO = 0.5
_SHRINK_GUARD_MIN_ORIGINAL_CHARS = 500


def fusion_viable_for_files(
    repo: str,
    files_to_modify: list[str],
    *,
    max_file_chars: int = FUSION_MAX_FILE_CHARS_DEFAULT,
    max_total_chars: int = FUSION_MAX_TOTAL_CHARS_DEFAULT,
) -> tuple[bool, str]:
    """Gate fusion on the feature's files BEFORE ever dispatching to it — whole-file
    replacement only works when a real completion can see the whole current file
    and reproduce the whole new one. Checks actual on-disk size (``os.path.getsize``,
    never reads the file into memory just to measure it). Returns ``(True, "")`` when
    every file is small enough (or doesn't exist yet — nothing to be too large),
    else ``(False, reason)``. Callers treat ``False`` exactly like "no
    fusion_delegate configured": honest degrade, not a silent truncated attempt."""
    import os

    total = 0
    for rel in files_to_modify:
        p = Path(repo) / rel
        try:
            size = os.path.getsize(p)
        except OSError:
            continue  # doesn't exist yet — nothing to be too large
        if size > max_file_chars:
            return False, f"{rel} is {size} chars, over the {max_file_chars}-char per-file cap for a full rewrite"
        total += size
    if total > max_total_chars:
        return False, f"files_to_modify total {total} chars, over the {max_total_chars}-char combined cap"
    return True, ""


def _parse_fusion_files(reply: str) -> dict[str, str]:
    """Extract ``{relative path: full file content}`` from a fusion completion. No
    match ⇒ empty dict — the caller writes nothing, and the untouched worktree just
    fails ``verify()`` like any other candidate that didn't address the task."""
    return {path.strip(): content for path, content in _FUSION_FILE_RE.findall(reply or "")}


def _fusion_prompt(
    task: str,
    *,
    feedback: str | None,
    repo: str,
    files_to_modify: list[str],
    max_file_chars: int = FUSION_MAX_FILE_CHARS_DEFAULT,
) -> str:
    """Build fusion's prompt. Fusion can't read the repo itself (no tool-calling), so
    this hands it the CURRENT content of every file the feature declares — read from
    the base repo, best-effort (a listed-but-not-yet-created file is noted as new).

    Callers are expected to have already checked ``fusion_viable_for_files`` (the
    real gate) so a genuinely oversized file should never reach here — this
    truncation is a DEFENSIVE backstop only (e.g. a direct ``test_rung`` call that
    skipped the gate), and unlike the gate it's never silent: a truncated file is
    marked as such so fusion knows not to claim a full-file replacement for it."""
    file_blocks = []
    for rel in files_to_modify:
        p = Path(repo) / rel
        try:
            raw = p.read_text(errors="replace")
        except OSError:
            file_blocks.append(f"### {rel} (does not exist yet — you are creating it)")
            continue
        if len(raw) > max_file_chars:
            text = raw[:max_file_chars]
            file_blocks.append(
                f"### {rel} (current content — TRUNCATED at {max_file_chars} chars, "
                f"real file is {len(raw)} chars — do NOT return this as a complete "
                "replacement; skip this file instead)\n```\n"
                f"{text}\n```"
            )
        else:
            file_blocks.append(f"### {rel} (current content)\n```\n{raw}\n```")
    files_section = (
        "\n\n".join(file_blocks) if file_blocks else "(no existing files listed — create what the task needs)"
    )
    parts = [
        "Implement the task below. You have NO tool access — you cannot read or run "
        "anything else, so work only from what's given here.",
        "",
        "## Task",
        task.strip(),
        "",
        "## Current file contents",
        files_section,
        "",
        "## Your reply format — REQUIRED, exactly this shape per file",
        "For every file you create or change, return its COMPLETE, FINAL content "
        "(never a partial diff or `...` elisions) as:",
        "### relative/path/to/file.py",
        "```",
        "<the file's entire new content>",
        "```",
        "Only include files you're actually creating or changing. No prose outside the file blocks.",
    ]
    if feedback:
        parts += [
            "",
            "## Your previous attempt FAILED the acceptance tests — fix exactly this",
            feedback.strip(),
        ]
    return "\n".join(parts)


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


def _augment_prompt(task: str, feedback: str | None) -> str:
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
        dispatch_timeout: float | None,
        test_cmd: str,
        test_timeout: float,
        verdict_cls,
        fusion_delegate=None,
        files_to_modify: list[str] | None = None,
        fusion_max_file_chars: int = FUSION_MAX_FILE_CHARS_DEFAULT,
        env_passthrough: Iterable[str] = (),
        _fusion_dispatch=None,
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
        # The gate's env_passthrough whitelist (#86), threaded from the loop so the
        # acceptance-test (verify) subprocess strips the SAME host identity/credential
        # block the gate/preflight/format subprocesses already do — the host's
        # PROTOAGENT_*/A2A_*/AGENT_NAME must never leak into a candidate's tests.
        self.env_passthrough = tuple(env_passthrough)
        self.fusion_delegate = fusion_delegate  # a resolved `openai`-type Delegate, or None
        self.files_to_modify = files_to_modify or []
        self.fusion_max_file_chars = fusion_max_file_chars
        # Test-injection seam (mirrors `_solve`/`_budget_cls`/`_verdict_cls` on
        # `dispatch()`): production never passes this — the real lazy
        # `ADAPTERS["openai"].dispatch` import happens in `generate_fusion` below.
        self._fusion_dispatch = _fusion_dispatch
        self.candidates: list[tuple[str, str]] = []  # (worktree_path, branch)
        # `git worktree add` against the SAME repo must not run concurrently (best-
        # of-k dispatches `generate()` via asyncio.gather) — serialize just that
        # step; the slow part (the coder dispatch) still runs in parallel.
        self._wt_lock = asyncio.Lock()
        self._n = 0
        # worktree_path -> the coder's own final reply (its clean PR summary, per
        # `loop._build_prompt`'s "your FINAL message becomes the PR description"
        # contract) — captured so `dispatch()` can use the WINNING candidate's real
        # summary as the PR body instead of an internal rung/gens diagnostic string.
        # Also what `_verify_goal`'s NO_TEST_NEEDED escape hatch reads. Fusion has no
        # such reply (a plain completion, not a summary) — absent for fusion wins.
        self._replies: dict[str, str] = {}

    async def _new_candidate_worktree(self) -> tuple[str, str]:
        self._n += 1
        cid = f"{self.fid}.g{self._n}"
        async with self._wt_lock:
            wt, branch = await worktree.create_worktree(self.repo, self.base, cid, self.root)
        self.candidates.append((wt, branch))
        return wt, branch

    async def generate(self, task: str, *, feedback: str | None = None) -> str:
        wt, _branch = await self._new_candidate_worktree()
        reply = await worktree.dispatch_coder(
            self.coder, wt, _augment_prompt(task, feedback), timeout=self.dispatch_timeout
        )
        if (reply or "").strip():
            self._replies[wt] = reply
        return wt

    async def generate_fusion(self, task: str, *, feedback: str | None = None) -> str:
        """Rung 4 (ADR 0064 P3): fusion can't tool-call, so instead of dispatching an
        ACP session into the worktree, get a plain completion and write its files into
        one. Same candidate bookkeeping (``candidates``/``_wt_lock``) as ``generate``,
        so promote/reap treats a fusion winner identically to an ACP one."""
        if self._fusion_dispatch is not None:
            openai_dispatch = self._fusion_dispatch
        else:
            from plugins.delegates.adapters import ADAPTERS

            openai_dispatch = ADAPTERS["openai"].dispatch

        prompt = _fusion_prompt(
            task,
            feedback=feedback,
            repo=self.repo,
            files_to_modify=self.files_to_modify,
            max_file_chars=self.fusion_max_file_chars,
        )
        reply = await openai_dispatch(self.fusion_delegate, prompt, timeout=self.dispatch_timeout)
        files = _parse_fusion_files(reply)
        wt, _branch = await self._new_candidate_worktree()
        wt_root = Path(wt).resolve()
        written = 0
        for rel, content in files.items():
            # `rel` comes from a model completion — an absolute path or a `../` climb
            # would otherwise write outside the worktree (Path.__truediv__ with an
            # absolute right-hand side even discards the left side entirely). Resolve
            # and require containment; skip (don't crash the candidate) on a miss.
            dest = (wt_root / rel).resolve()
            if wt_root not in dest.parents and dest != wt_root:
                log.warning(
                    "[project_board] %s fusion tried to write outside its worktree: %r — skipped", self.fid, rel
                )
                continue
            # Fusion has no tool access — it can only ever act on the files we showed
            # it. A path outside the feature's declared set means it hallucinated a
            # file (or the parser mis-split the reply); writing it would silently
            # touch unrelated code with no test coverage backing the change.
            if self.files_to_modify and rel not in self.files_to_modify:
                log.warning("[project_board] %s fusion tried to write an undeclared path: %r — skipped", self.fid, rel)
                continue
            # Fusion returns whole-file replacements with no diff to sanity-check.
            # A reply that's drastically smaller than the file it claims to replace
            # is far more likely a truncated completion (see FUSION_MAX_FILE_CHARS_DEFAULT
            # and the delegate's own max_tokens ceiling) than an intentional big
            # deletion — refuse it rather than risk silent data loss.
            if dest.exists():
                try:
                    original_size = dest.stat().st_size
                except OSError:
                    original_size = 0
                if (
                    original_size > _SHRINK_GUARD_MIN_ORIGINAL_CHARS
                    and len(content) < original_size * _SHRINK_GUARD_RATIO
                ):
                    log.warning(
                        "[project_board] %s fusion's rewrite of %r (%d chars) is suspiciously smaller than "
                        "the original (%d chars) — refusing, likely truncated",
                        self.fid,
                        rel,
                        len(content),
                        original_size,
                    )
                    continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
            written += 1
        if not written:
            log.warning(
                "[project_board] %s fusion reply parsed to 0 writable files — candidate is unchanged base", self.fid
            )
        return wt

    async def verify(self, candidate_wt: str):
        Verdict = self.verdict_cls
        try:
            proc = await asyncio.create_subprocess_shell(
                self.test_cmd,
                cwd=candidate_wt,
                # #86: strip the host identity/credential block from the acceptance-test
                # env — with NO env= the child inherits os.environ verbatim (the host's
                # PROTOAGENT_*/A2A_*/AGENT_NAME), which burned 15 solve gens on an
                # unwinnable test. `sanitized_env` mirrors the loop's own gate spawns.
                env=config.sanitized_env(self.env_passthrough),
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


def _record_gens_best_effort(record_gens: RecordGens, fid: str, n: int) -> None:
    """Call ``record_gens(n)`` and swallow any exception it raises.

    ``store.record_gens_spent`` documents itself as fire-and-forget ("a br hiccup
    here must never fail the build the way a missing PR would") — a transient `br`
    failure (lock contention, a flaky CLI invocation, a race with a concurrent
    label write) must never propagate out of ``dispatch()``. Left unguarded, it
    would surface as an unrelated ``BoardError`` past every capability-failure
    handler in the caller's loop, discarding an already-verified (or already-
    reaped) candidate purely because of a bookkeeping label write."""
    try:
        record_gens(n)
    except Exception:  # noqa: BLE001 — fire-and-forget cost accounting, never fails the build
        log.warning("[project_board] %s record_gens(%d) failed (ignored — fire-and-forget)", fid, n, exc_info=True)


async def dispatch(
    *,
    task: str,
    coder,
    repo: str,
    base: str,
    root: str,
    fid: str,
    dispatch_timeout: float | None,
    test_cmd: str,
    test_timeout: float,
    budget: int,
    k: int,
    tree_depth: int,
    record_gens: RecordGens | None = None,
    fusion_delegate=None,
    fusion_k: int = 2,
    files_to_modify: list[str] | None = None,
    fusion_max_file_chars: int = FUSION_MAX_FILE_CHARS_DEFAULT,
    env_passthrough: Iterable[str] = (),
    _solve=None,
    _budget_cls=None,
    _verdict_cls=None,
    _fusion_dispatch=None,
) -> tuple[str, str, str]:
    """Run the execution-grounded ladder for one feature build.

    Returns ``(worktree, branch, result_text)`` on a passing candidate — the SAME
    3-tuple shape ``_dispatch_max_mode`` returns, so the caller's downstream drive
    (fixups, local gate, ``open_pr``) is unchanged. ``result_text`` is the WINNING
    candidate's own reply (its clean PR summary) when the ladder reached it via an
    ACP rung; only a fusion win (no natural-language reply, just file content) falls
    back to an internal rung/gens diagnostic string. Raises :class:`SolveExhausted`
    (a capability failure) when the budget is spent with no passing candidate, after
    reaping every candidate worktree it created.

    ``record_gens`` (if given) is called with ``result.gens_spent`` exactly once,
    win or lose — the cost accounting (ADR 0064) must survive a failed search too.
    ``_solve``/``_budget_cls``/``_verdict_cls`` are test-injection seams for
    ``solve()``/``Budget``/``Verdict``; production callers never pass them (the real
    import happens here, deferred so this module carries no hard dependency on the
    `coder` plugin).

    ``fusion_delegate`` (a resolved ``openai``-type Delegate, or ``None``) gates rung
    4 (ADR 0064 P3) — the caller resolves it (mirroring how ``coder`` itself is
    resolved), so this module never does delegate lookup. ``None`` (unconfigured) ⇒
    ``solve()`` gets ``fusion_generate=None`` and stops at tree-search, unchanged from
    before this rung existed. ``files_to_modify`` feeds fusion's prompt (it can't read
    the repo itself, unlike the ACP rungs) — the same list the feature's Ready gate
    already required.

    ``env_passthrough`` (#86) is the loop's env whitelist, threaded through to the
    adapter so the acceptance-test (verify) subprocess strips the same host
    identity/credential block (``PROTOAGENT_*``/``A2A_*``/``AGENT_NAME``) the gate and
    preflight already strip — with no ``env=`` the verify child would inherit the host's
    whole environment and could pass/fail on the HOST's identity, not the candidate's.

    **``solve()`` itself can raise.** The ladder (`coder`'s own ``solve.py``) has no
    try/except around ``generate``/``verify`` — it assumes a candidate attempt never
    errors, only that it might fail its tests. A REAL dispatch can still raise
    (``CoderTimeout`` on one best-of-k candidate, a worktree op erroring) and that
    propagates straight out of ``solve()`` uncaught. Every worktree ``generate()``
    already created for THIS run is tracked in ``adapter.candidates`` (appended right
    after ``create_worktree`` returns, before the dispatch that might fail) but would
    otherwise leak forever: it's untracked in the loop's ``_inflight`` map until this
    function returns, and invisible to the health sweep (a `.gN` candidate id isn't a
    real board feature, so the sweep's own ``get_feature`` lookup raises and the sweep
    skips it rather than reaping). So any exception here reaps every candidate seen so
    far, surfaces the attempted cost, and re-raises the ORIGINAL exception unchanged —
    the loop's existing capability-failure handling (retry/escalate/block) still
    applies to whatever it actually was."""
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
        fusion_delegate=fusion_delegate,
        files_to_modify=files_to_modify,
        fusion_max_file_chars=fusion_max_file_chars,
        env_passthrough=env_passthrough,
        _fusion_dispatch=_fusion_dispatch,
    )
    try:
        result = await solve(
            task,
            generate=adapter.generate,
            verify=adapter.verify,
            budget=Budget(budget),
            k=k,
            tree_depth=tree_depth,
            fusion_generate=adapter.generate_fusion if fusion_delegate is not None else None,
            fusion_k=fusion_k,
        )
    except Exception as exc:
        for wt, branch in adapter.candidates:
            await worktree.remove_worktree(repo, wt, branch)
        if record_gens is not None and adapter._n:
            # `solve()` never got to return a `gens_spent` count — the attempted
            # generation count is the honest stand-in (a failed dispatch still spent
            # the gen; ADR 0064's cost accounting doesn't get to look the other way).
            # Best-effort per store.record_gens_spent's own contract ("a br hiccup
            # here must never fail the build"): the worktrees above are ALREADY
            # reaped and the original exception below is what the loop must see —
            # a transient `br` failure recording the spend must never mask it.
            _record_gens_best_effort(record_gens, fid, adapter._n)
        log.warning(
            "[project_board] %s coder.solve raised mid-ladder (%d candidate(s) reaped): %s",
            fid,
            len(adapter.candidates),
            exc,
        )
        raise
    if record_gens is not None:
        # Same fire-and-forget contract as above: a bookkeeping failure here must
        # never discard a candidate that ALREADY exists on disk (test-verified or
        # not) — the promote/reap logic below still has to run either way.
        _record_gens_best_effort(record_gens, fid, result.gens_spent)

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
    # The winning candidate's own reply (its clean PR summary, per the "your FINAL
    # message becomes the PR description" contract every coder dispatch is given) is
    # the real result — `loop.py` uses this verbatim as the PR body, and `_verify_goal`
    # reads it for the NO_TEST_NEEDED escape hatch. Only fusion (a plain completion,
    # no such reply) or an unexpectedly-empty one falls back to the diagnostic string.
    result_text = (
        adapter._replies.get(win_wt) or f"[coder.solve rung={result.rung} gens={result.gens_spent}] {result.note}"
    )
    return canon_wt, canon_branch, result_text


async def test_rung(
    *,
    rung: str,
    task: str,
    coder,
    repo: str,
    base: str,
    root: str,
    fid: str,
    dispatch_timeout: float | None,
    test_cmd: str,
    test_timeout: float,
    budget: int = 10,
    k: int = 3,
    tree_depth: int = 2,
    fusion_delegate=None,
    fusion_k: int = 2,
    files_to_modify: list[str] | None = None,
    fusion_max_file_chars: int = FUSION_MAX_FILE_CHARS_DEFAULT,
    env_passthrough: Iterable[str] = (),
    _solve=None,
    _budget_cls=None,
    _verdict_cls=None,
    _fusion_dispatch=None,
) -> dict:
    """Operator-only diagnostic (ADR 0064): run exactly ONE named rung of
    ``coder.solve()`` against a feature's REAL acceptance tests, in a throwaway
    worktree that is ALWAYS reaped afterward — never promoted, no PR opened, no
    board state touched. For verifying a rung actually works (especially fusion,
    only otherwise reached after three cheaper rungs fail) without contriving a
    task hard enough to fail its way there.

    Deliberately separate from ``dispatch()``: that function's contract (promote
    the winner, raise ``SolveExhausted`` on exhaustion) is shaped for the board's
    real per-feature build — mixing test semantics into it would risk the real
    dispatch path. This is exposed to operators only via api.py's ``test-rung``
    route, which carries NO ``@tool`` wrapper — the board's own lead agent has no
    way to call this itself (see api.py's docstring for the same boundary the
    plugin already draws around ``/features/{id}/cancel`` etc.)."""
    if _solve is not None:
        solve, Budget, Verdict = _solve, _budget_cls, _verdict_cls
    else:
        from plugins.coder.solve import Budget, Verdict, solve

    adapter = _WorktreeSolveAdapter(
        repo=repo,
        base=base,
        root=root,
        fid=f"{fid}.test",
        coder=coder,
        dispatch_timeout=dispatch_timeout,
        test_cmd=test_cmd,
        test_timeout=test_timeout,
        verdict_cls=Verdict,
        fusion_delegate=fusion_delegate,
        files_to_modify=files_to_modify,
        fusion_max_file_chars=fusion_max_file_chars,
        env_passthrough=env_passthrough,
        _fusion_dispatch=_fusion_dispatch,
    )
    try:
        result = await solve(
            task,
            generate=adapter.generate,
            verify=adapter.verify,
            budget=Budget(budget),
            k=k,
            tree_depth=tree_depth,
            fusion_generate=adapter.generate_fusion if fusion_delegate is not None else None,
            fusion_k=fusion_k,
            force_rung=rung,
        )
    finally:
        # ALWAYS reap — pass or fail, this is a diagnostic run, never a real build.
        for wt, branch in adapter.candidates:
            await worktree.remove_worktree(repo, wt, branch)
    return {
        "rung": result.rung,
        "passed": result.passed,
        "gens_spent": result.gens_spent,
        "candidates_tried": result.candidates_tried,
        "note": result.note,
        "verdict_output": result.verdict.output if result.verdict else "",
    }
