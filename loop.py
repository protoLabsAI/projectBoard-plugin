"""The orchestration loop — the deterministic state machine around the spine.

A background surface (ADR 0018 ``register_surface``) that pulls ``ready`` features
and drives each: worktree → coder → PR → review. It is the ONLY thing that moves a
feature forward through the build states; ``done`` it never sets — that is the
merge webhook (``api.record_merge``), the single external edge (invariant #2).

    ready ──claim──▶ in_progress
       worktree add  →  delegate_to(coder) in worktree  →  push + gh pr create
       │                       [finally: reap coder subprocess]
       └──▶ in_review  ──delegate_to(reviewer)──▶  (CI + review on the PR)
                 │
   merge webhook ▼                 CI fail ▼                 any failure ▼
   /merge poll                in_progress (bounce)     blocked (flag + reason)
              done

CI status arrives out-of-band via the board API (``api.py``). ``done`` is set by
the merge webhook (``api.record_merge``) — or, when no public webhook URL is
reachable, by the loop's **PR reconcile** (``merge_poll``), which asks ``gh`` for
each ``in_review`` PR's state and drives the terminal edges: merged → done (the same
idempotent edge), closed-unmerged → blocked. Up to ``max_concurrent`` features build
concurrently, each in its own worktree.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from . import worktree
from .failures import classify
from .store import BoardError, escalation_enabled, get_store

log = logging.getLogger("protoagent.plugins.project_board")

# Deterministic test-coverage gate (path-based — no LLM, no diff). A code change must
# ship a test; checking the changed-file LIST is instant and immune to the truncation
# that made the old LLM-eyeballs-the-diff verifier false-reject tests it couldn't see.
_TEST_PATH_RE = re.compile(r"(^|/)tests?/|(^|/)(test_[^/]+|conftest)\.py$|(^|/)[^/]+_test\.py$|\.(test|spec)\.[jt]sx?$")
_CODE_EXTS = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go", ".rs")


def _is_test_path(p: str) -> bool:
    return bool(_TEST_PATH_RE.search(p))


def _is_code_path(p: str) -> bool:
    return p.endswith(_CODE_EXTS)


# Error/summary lines worth keeping from a failing CI log — the ones that name the
# ACTUAL failure (pytest's "FAILED … - AssertionError: golden field map …", ruff's
# "F841"/"would reformat", a conflict, version drift) so the attempt comment the retro
# mines is CLASSIFIABLE, not a generic "checks red".
_CI_SIGNAL_RE = re.compile(
    r"FAILED|Error|assert|\bF\d{3}\b|reformat|no column|out of sync|conflict|drift|lint-imports", re.I
)


def _ci_failure_reason(summary: str, max_chars: int = 500) -> str:
    """Distill a CI summary into a compact but classifiable failure reason for the
    attempt comment (the loop-retro mines these to bucket recurring failures).

    The useful signal is NOT the ``Failing checks:`` header — it's the failing check
    NAMES plus the tail of the failing log, where pytest/ruff print the real error.
    Falls back to the header / ``checks red`` when there's nothing better."""
    if not summary:
        return "checks red"
    checks = [ln[2:].strip() for ln in summary.splitlines() if ln.startswith("- ")]
    head = "; ".join(checks) if checks else summary.splitlines()[0].strip()
    detail = ""
    if "Failing log" in summary:
        log = summary.split("Failing log", 1)[1]
        errs = [ln.strip() for ln in log.splitlines() if ln.strip() and _CI_SIGNAL_RE.search(ln)]
        if errs:
            detail = " · ".join(errs[-4:])
        else:
            tail = [ln.strip() for ln in log.splitlines() if ln.strip()]
            detail = tail[-1] if tail else ""
    reason = f"{head} — {detail}" if detail else head
    return reason[:max_chars]


_MAX_MODE_JUDGE_SYS = (
    "You are a strict code reviewer choosing the best of several diffs for the same "
    "task. Pick the one that most completely and correctly satisfies the acceptance "
    "criteria. Answer with ONLY the candidate number."
)


class BoardLoop:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.coder_name = self.cfg.get("coder", "proto")
        self.reviewer_name = self.cfg.get("reviewer", "quinn")
        # Review dispatch is OPT-IN (default off). The fleet's PR-review pipeline
        # already reviews PRs the moment they're opened, so the loop doesn't need to
        # `delegate_to(reviewer)` — it just opens the PR and lets the pipeline + CI +
        # the merge webhook gate it. Turn this on only for repos NOT covered by a
        # PR-review pipeline (then a reachable `reviewer` a2a delegate is required).
        self.review_dispatch = bool(self.cfg.get("review_dispatch", False))
        # Goal-verification gate (OPT-IN, default off). When on, a DETERMINISTIC pre-PR
        # check (no LLM, no diff dump): a code change must ship a test — CI runs tests but
        # can't require their presence, so the gate does. A miss → re-dispatch/escalate
        # instead of opening a testless PR; correctness itself is CI's job. (Was an
        # LLM-eyeballs-the-diff check — it false-rejected tests it couldn't see past the
        # diff truncation, burning whole tier ladders on phantom gaps; see _verify_goal.)
        self.goal_verify = bool(self.cfg.get("goal_verify", False))
        # Max-Mode (MiMo Tier-2, OPT-IN, default 1 = off). When >1, a hard feature is
        # attempted with N parallel candidates and `_judge_candidates` picks the best
        # diff. Costs N× tokens, so gate it to hard work. The parallel-dispatch wiring
        # is tracked in #21; this ships the reusable best-of-N judge it composes.
        self.max_mode_n = max(1, int(self.cfg.get("max_mode_n", 1) or 1))
        self.interval = float(self.cfg.get("loop_interval_s", 30))
        self.root = self.cfg.get("worktrees_root", ".worktrees")
        self.enabled = bool(self.cfg.get("loop_enabled", False))
        # Escalation is OPT-IN: a `coders` map (tier → delegate name) with ≥2
        # distinct delegates. With a single ACP coder there's no ladder — one
        # dispatch, then Blocked on failure — so difficulty/tier stay irrelevant
        # and we never write redundant tier/attempt labels.
        self.coders = {str(k): str(v) for k, v in (self.cfg.get("coders") or {}).items()}
        self.escalation_on = escalation_enabled(self.cfg)
        # Concurrency: drive up to `max_concurrent` features at once, each in its own
        # worktree. 1 (the default) = serial — the safe default for token + merge-
        # integration cost; raise it on a repo that parallelizes cleanly.
        self.max_concurrent = max(1, int(self.cfg.get("max_concurrent", 1)))
        # Review-queue WIP limit: pause new claims when this many PRs already await
        # review, so the loop can't pile up PRs faster than they merge (flooding CI /
        # reviewers). 0 = unlimited.
        self.max_pending_reviews = int(self.cfg.get("max_pending_reviews", 5))
        # Dependency gate: "merge" (default) — a dependent waits for every blocker to
        # merge (done); "review" — a NON-foundation blocker releases its dependents at
        # in_review (more parallelism, at the risk of building on un-merged code).
        # Foundation blockers always gate on merge.
        self.relaxed_gate = str(self.cfg.get("dep_gate", "merge")).lower() == "review"
        # Stuck-drive watchdog: hard cap on a single coder dispatch (the only
        # otherwise-unbounded await in a drive — git/gh calls already self-time-out).
        # 0 disables it. A timeout reaps the coder subprocess and is a capability
        # failure (escalate-or-block), not a transient retry.
        self.coder_timeout = float(self.cfg.get("coder_timeout_s", 1800))
        # Merge poll: a fallback to the /webhook/pr Done edge for deployments with no
        # public webhook URL. On by default (cheap; only probes `in_review` PRs).
        self.merge_poll = bool(self.cfg.get("merge_poll", True))
        self.merge_poll_interval = float(self.cfg.get("merge_poll_interval_s", 60))
        # Health sweep: periodic self-heal (reclaim slots from dead drives, reap
        # orphaned worktrees). 0 disables it.
        self.sweep_interval = float(self.cfg.get("health_sweep_interval_s", 300))
        # CI-feedback edge (closed-loop verify): poll in_review PRs' check-runs and,
        # on a FAILING rollup, bounce the feature back to the coder with the failure
        # injected as feedback (vs the old open-loop: a red PR sat in_review forever).
        # Rides the merge-poll cadence. `ci_fix_max` caps re-dispatches before the
        # feature is blocked for human triage (a real bug, not a self-fixable nit).
        self.ci_poll = bool(self.cfg.get("ci_poll", self.merge_poll))
        self.ci_fix_max = max(0, int(self.cfg.get("ci_fix_max", 2)))
        # Auto-rebase a stale/conflicting in_review PR onto base. Parallel PRs branch
        # off the SAME base, and the hot-file guard serializes DISPATCH not the branch
        # BASE — so each merge re-stales the others (a sibling's change lands in the
        # same files). On BEHIND (stale, no conflict) a clean rebase + force-push fixes
        # it with NO coder; on DIRTY (a real conflict) the rebase aborts and the coder
        # is re-dispatched to re-resolve, bounded by rebase_fix_max. Rides the
        # merge-poll cadence; defaults to merge_poll's value.
        self.auto_rebase = bool(self.cfg.get("auto_rebase", self.merge_poll))
        self.rebase_fix_max = max(0, int(self.cfg.get("rebase_fix_max", 1)))
        # Pre-PR goal-verify gap: a rejected diff (e.g. missing tests) is fixable by
        # the SAME coder told what's missing — NOT a model-capability failure. So
        # carry the gap as feedback + re-dispatch the same tier, bounded by
        # `goal_fix_max`, BEFORE escalating/blocking (else a top-tier diff:large
        # feature blocks on attempt 1 with no chance to add the tests).
        self.goal_fix_max = max(0, int(self.cfg.get("goal_fix_max", 2)))
        # Auto-fix command run in the worktree BEFORE opening the PR (e.g.
        # "ruff check --fix . && ruff format ."). The coder is edit-only — it can't run
        # the repo's linter/formatter, so trivial lint/format nits would otherwise fail
        # CI and burn a whole bounce/escalation (bd-2fd: a full opus fix blocked on one
        # unused import). Best-effort; CI is still the real gate. Empty = off.
        self.format_cmd = str(self.cfg.get("format_cmd", "")).strip()
        # Pre-PR LOCAL GATE: the repo's real check command(s) run in the worktree
        # AFTER fixups and BEFORE open_pr (e.g. "ruff check . && uv run --no-sync pytest
        # tests/ -q"). The coder is edit-only — it can't run the suite — so a failure on
        # a knowable fact (a lint nit, a golden-map test, a wrong schema/column, version
        # drift) only surfaces in CI, then thrashes the bounce/escalation ladder. Running
        # it here hands the SAME coder the actual output to fix in-worktree, so the PR
        # opens already-green. Best-effort early filter: if it can't pass within
        # local_gate_max same-tier tries, the PR opens anyway (CI + the ci-fix budget
        # stay the backstop) — a flaky/misconfigured gate never blocks good work. Empty = off.
        self.local_gate_cmd = str(self.cfg.get("local_gate_cmd", "")).strip()
        self.local_gate_max = max(0, int(self.cfg.get("local_gate_max", 2)))
        self.local_gate_timeout = float(self.cfg.get("local_gate_timeout_s", 600))
        self.local_gate_output_chars = max(500, int(self.cfg.get("local_gate_output_chars", 4000)))
        self._store_kw = dict(
            db=self.cfg.get("db_path") or None,
            repo=self.cfg.get("repo", "."),
            base_branch=self.cfg.get("base_branch", "main"),
        )
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # The running drive tasks, and the worktrees they hold (fid → (repo, wt,
        # branch)) so shutdown can reap any a cancel mid-drive would orphan; the coder
        # subprocess itself is reaped by dispatch_coder's finally.
        self._drives: set[asyncio.Task] = set()
        self._inflight: dict[str, tuple[str, str, str]] = {}
        # files_to_modify of each in-flight feature, for the hot-file overlap guard
        # (don't run two parallel coders that edit the same file → sure conflict).
        self._inflight_files: dict[str, set[str]] = {}
        self._last_poll = 0.0  # monotonic ts of the last merge poll
        self._last_sweep = 0.0  # monotonic ts of the last health sweep
        # CI-feedback state (in-memory, per run): fid → last failing-CI summary (fed
        # into the re-dispatch prompt) and fid → count of CI-fix re-dispatches so far.
        self._ci_feedback: dict[str, str] = {}
        self._ci_prior_diff: dict[str, str] = {}
        self._ci_fix_attempts: dict[str, int] = {}
        # Pre-PR goal-verify gap re-dispatches so far (fid → count), same-tier.
        self._goal_fix_attempts: dict[str, int] = {}
        # Pre-PR local-gate failure re-dispatches so far (fid → count), same-tier.
        self._gate_fix_attempts: dict[str, int] = {}
        # Rebase-conflict re-dispatches so far (fid → count) when a sibling merge
        # leaves a PR with a real (non-clean) conflict against base.
        self._rebase_attempts: dict[str, int] = {}

    def _store(self):
        return get_store(**self._store_kw)

    # ── lifecycle (register_surface start/stop) ───────────────────────────────
    def start(self):
        if not self.enabled:
            log.info("[project_board] loop disabled (project_board.loop_enabled=false) — board API still serves")
            return None
        self._task = asyncio.create_task(self._run(), name="project-board-loop")
        log.info(
            "[project_board] loop started (coder=%s reviewer=%s every %ss, max_concurrent=%d, "
            "merge_poll=%s, coder_timeout=%ss)",
            self.coder_name,
            self.reviewer_name,
            self.interval,
            self.max_concurrent,
            self.merge_poll,
            self.coder_timeout,
        )
        return self._task

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Cancel any in-flight drives and await them out. A drive cancelled mid-flight
        # can't run its own cleanup, so its worktree stays in self._inflight — reaped
        # below. (A completed/blocked drive already popped itself.)
        drives, self._drives = list(self._drives), set()
        for t in drives:
            t.cancel()
        if drives:
            await asyncio.gather(*drives, return_exceptions=True)
        inflight, self._inflight = dict(self._inflight), {}
        for fid, (repo, wt, branch) in inflight.items():
            try:
                await worktree.remove_worktree(repo, wt, branch or "")
                log.info("[project_board] reaped in-flight worktree on shutdown: %s", wt)
            except Exception:  # noqa: BLE001 — teardown must not raise out of shutdown
                log.warning("[project_board] worktree reap on shutdown failed: %s", wt, exc_info=True)

    # ── crash recovery (runs once, before the puller claims new work) ──────────
    async def _reconcile_orphan(self, fid: str):
        """A claimed feature with no live drive: if its PR actually got opened (a crash
        between ``open_pr`` and ``open_review``) adopt it → ``in_review``; otherwise
        reset it to ``ready`` for a clean rebuild (a stale worktree is cleaned when the
        puller re-claims it). Shared by boot recovery and the health sweep."""
        store = self._store()
        pr_url = await worktree.pr_url_for_branch(f"feat/{fid}", cwd=self._store_kw["repo"])
        if pr_url:
            store.open_review(fid, pr_url=pr_url)
            log.info("[project_board] %s already had a PR → in_review (%s)", fid, pr_url)
        else:
            store.requeue(fid)
            log.info("[project_board] %s reset to ready (no PR — rebuild fresh)", fid)

    async def _recover(self):
        """On boot, reconcile every ``in_progress`` feature the previous run left
        mid-drive (a drive doesn't survive a restart). ``in_review`` features are NOT
        touched — they have a PR and the webhook/poll resolves them."""
        for f in self._store().list_features(state="in_progress"):
            try:
                await self._reconcile_orphan(f["id"])
            except Exception:  # noqa: BLE001 — recovery is best-effort, per feature
                log.warning("[project_board] recovery for %s failed", f["id"], exc_info=True)

    # ── periodic health sweep (self-heal during the run) ───────────────────────
    async def _maybe_sweep(self):
        """Run the health sweep at most once per ``health_sweep_interval`` (0 = off)."""
        if not self.sweep_interval:
            return
        now = time.monotonic()
        if now - self._last_sweep < self.sweep_interval:
            return
        self._last_sweep = now
        await self._sweep()

    async def _sweep(self):
        """Self-heal: (a) reset ``in_progress`` features that no live drive owns (a
        drive died without finishing) — same reconcile as boot recovery; (b) reap
        ``feat-<id>`` worktrees whose feature is gone or already ``done`` (a missed
        reap). Best-effort; a per-item failure never stops the sweep or the loop."""
        store = self._store()
        repo = self._store_kw["repo"]
        for f in store.list_features(state="in_progress"):
            fid = f["id"]
            if fid in self._inflight_files:
                continue  # a live drive owns it
            try:
                log.info("[project_board] sweep: %s in_progress with no live drive", fid)
                await self._reconcile_orphan(fid)
            except Exception:  # noqa: BLE001
                log.warning("[project_board] sweep reconcile for %s failed", fid, exc_info=True)
        for fid in worktree.list_feature_worktrees(repo, self.root):
            if fid in self._inflight_files:
                continue  # a live drive owns this worktree
            try:
                f = store.get_feature(fid)
                if f is None or f["board_state"] == "done":
                    await worktree.reap_feature_worktree(repo, self.root, fid)
                    log.info("[project_board] sweep: reaped orphaned worktree feat-%s", fid)
            except Exception:  # noqa: BLE001
                log.warning("[project_board] sweep reap for %s failed", fid, exc_info=True)

    # ── the puller ────────────────────────────────────────────────────────────
    async def _run(self):
        try:
            await self._recover()
        except Exception:  # noqa: BLE001 — recovery must never stop the loop from starting
            log.exception("[project_board] crash recovery failed")
        log.info("[project_board] recovery done — entering tick loop")
        while not self._stop.is_set():
            spawned = False
            try:
                await self._maybe_reconcile()
                await self._maybe_sweep()
                spawned = self._spawn_ready()
            except Exception:  # noqa: BLE001 — a bad tick must never kill the loop
                log.exception("[project_board] loop tick failed")
            # Idle (nothing started, nothing running) → sleep the full interval. Busy
            # → re-check soon so a freed concurrency slot refills and merges land
            # promptly (the poll itself stays rate-limited by merge_poll_interval).
            idle = not spawned and not self._drives
            timeout = self.interval if idle else min(self.interval, 3.0)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

    def _spawn_ready(self) -> bool:
        """Claim Ready features up to the concurrency cap and spawn a drive for each,
        with two back-pressure gates: pause when too many PRs already await review
        (``max_pending_reviews``), and skip a candidate whose ``files_to_modify``
        overlap an in-flight build (the hot-file guard — two parallel coders editing
        the same file are a guaranteed merge conflict). Returns True if it started at
        least one drive (so the runner stays hot)."""
        if len(self._drives) >= self.max_concurrent:
            return False
        store = self._store()
        # Review-queue WIP limit — don't claim new work while the review queue is full.
        if self.max_pending_reviews and len(store.list_features(state="in_review")) >= self.max_pending_reviews:
            return False
        spawned = False
        busy = set().union(*self._inflight_files.values()) if self._inflight_files else set()
        for candidate in store.ready_queue(relaxed=self.relaxed_gate):  # priority order, dep-unblocked
            if len(self._drives) >= self.max_concurrent:
                break
            if candidate.get("board_state") != "ready" or candidate.get("blocked"):
                continue  # a blocked-flagged feature can carry the `ready` label too
            files = set(candidate.get("files_to_modify") or [])
            if files & busy:
                continue  # would edit a file an in-flight build owns → defer a tick
            claimed = store.claim(candidate["id"], assignee=self.coder_name)
            if claimed is None:
                continue  # raced / no longer ready
            self._inflight_files[claimed["id"]] = files
            task = asyncio.create_task(self._drive(claimed), name=f"pb-drive-{claimed['id']}")
            self._drives.add(task)
            task.add_done_callback(self._make_drive_done_cb(claimed["id"]))
            busy |= files
            spawned = True
        return spawned

    def _make_drive_done_cb(self, fid: str):
        """A drive task's done-callback: drop it from the running set and release the
        files it held (so a deferred file-conflicting candidate can be claimed next)."""

        def _cb(task: asyncio.Task):
            self._drives.discard(task)
            self._inflight_files.pop(fid, None)

        return _cb

    # ── the PR reconcile (terminal-edge fallback to the webhook) ───────────────
    async def _maybe_reconcile(self):
        """Run the PR reconcile at most once per ``merge_poll_interval`` (and only when
        enabled) — cheap, but no reason to hammer ``gh`` every busy tick."""
        if not self.merge_poll:
            return
        now = time.monotonic()
        if now - self._last_poll < self.merge_poll_interval:
            return
        self._last_poll = now
        await self._reconcile_prs()

    async def _reconcile_prs(self):
        """Reconcile each ``in_review`` feature against its PR's real state — the
        fallback to the webhook and the active half of the terminal edges (for
        deployments GitHub can't post a webhook to, where a feature would otherwise
        sit in_review forever): ``MERGED`` → done (+reap); ``CLOSED`` unmerged →
        Blocked for triage (+reap; the work was rejected, don't silently re-dispatch);
        ``OPEN`` → leave it in review."""
        store = self._store()
        repo = self._store_kw["repo"]
        for f in store.list_features(state="in_review"):
            fid = f["id"]
            pr_url = f.get("pr_url")
            if not pr_url:
                continue
            try:
                state = await worktree.pr_state(pr_url, cwd=repo)
                if state == "MERGED":
                    if store.record_merge(pr_url=pr_url):
                        await worktree.reap_feature_worktree(repo, self.root, fid)
                        self._ci_feedback.pop(fid, None)
                        self._ci_fix_attempts.pop(fid, None)
                        self._rebase_attempts.pop(fid, None)
                        log.info("[project_board] reconcile → done: %s (%s)", fid, pr_url)
                elif state == "CLOSED":
                    store.flag_blocked(fid, f"PR closed without merging — needs triage: {pr_url}")
                    await worktree.reap_feature_worktree(repo, self.root, fid)
                    self._ci_feedback.pop(fid, None)
                    self._ci_fix_attempts.pop(fid, None)
                    self._rebase_attempts.pop(fid, None)
                    log.info("[project_board] reconcile → blocked (PR closed): %s (%s)", fid, pr_url)
                elif state == "OPEN":
                    # Keep a stale/conflicting PR mergeable BEFORE the CI reconcile: a
                    # sibling merge re-stales the others off the shared base, and a rebase
                    # force-pushes + re-runs CI — so checking CI on the stale head first
                    # would just be thrown away.
                    if self.auto_rebase and await self._maybe_rebase(store, f, pr_url, repo):
                        continue
                    if self.ci_poll:
                        await self._reconcile_ci(store, fid, pr_url, repo)
            except Exception:  # noqa: BLE001 — a reconcile error must never kill the loop
                log.warning("[project_board] reconcile for %s failed", fid, exc_info=True)

    async def _maybe_rebase(self, store, feature: dict, pr_url: str, repo: str) -> bool:
        """If a sibling merge left this in_review PR BEHIND/DIRTY vs base, refresh it.

        Returns True if it acted (rebased / re-dispatched / blocked) so the caller skips
        the CI reconcile this pass; False when there's nothing to do (CLEAN, a checks-only
        BLOCKED, an UNKNOWN still computing, or a transient gh/infra hiccup → next poll
        retries). BEHIND (stale, no conflict) → a clean rebase + force-push, NO coder.
        DIRTY (a real conflict) → the rebase aborts, so re-dispatch the coder to re-resolve
        off the fresh base, bounded by rebase_fix_max, then Blocked for a manual rebase."""
        fid = feature["id"]
        mss = await worktree.pr_merge_state(pr_url, cwd=repo)
        if mss not in ("BEHIND", "DIRTY"):
            return False  # CLEAN / BLOCKED(checks) / UNKNOWN(computing) / DRAFT → not ours
        base = feature.get("base_branch") or self._store_kw.get("base_branch") or "main"
        outcome, detail = await worktree.rebase_onto_base(repo, f"feat/{fid}", base, root=self.root)
        if outcome == "clean":
            log.info("[project_board] %s auto-rebased onto %s (was %s) — force-pushed", fid, base, mss)
            return True
        if outcome == "error":
            log.warning(
                "[project_board] %s auto-rebase hit infra trouble (%s) — next poll retries: %s", fid, mss, detail
            )
            return False  # transient — don't burn the coder budget on an infra blip
        # outcome == "conflict": a real merge conflict only the coder can resolve.
        n = self._rebase_attempts.get(fid, 0)
        if n >= self.rebase_fix_max:
            store.flag_blocked(
                fid, f"rebase conflict with {base} after {n} attempt(s) — needs a manual rebase: {pr_url}"
            )
            await worktree.reap_feature_worktree(repo, self.root, fid)
            log.warning("[project_board] %s blocked (rebase conflict, %d attempt(s)): %s", fid, n, detail)
            return True
        self._rebase_attempts[fid] = n + 1
        self._ci_prior_diff.pop(fid, None)
        self._ci_feedback[fid] = (
            f"Your branch now CONFLICTS with `{base}` — a sibling change merged into the same "
            f"file(s): {detail}. Re-apply your change onto the latest `{base}` and resolve the "
            "conflict, keeping BOTH sides' intent. Then stop."
        )
        store.requeue(fid)
        log.info(
            "[project_board] %s rebase conflict — re-dispatch %d/%d to resolve (%s): %s",
            fid,
            n + 1,
            self.rebase_fix_max,
            mss,
            detail,
        )
        return True

    async def _reconcile_ci(self, store, fid: str, pr_url: str, repo: str):
        """Closed-loop verify edge: an OPEN ``in_review`` PR whose checks FAILED is
        bounced back to the coder — and the re-dispatch *improves on the last try*
        rather than blindly repeating it (the missing OODA correction; before this a
        red PR sat in_review forever, then a same-model retry re-made the same mistake).

        Two improvement levers, both ProtoMaker-style:
        - **Carry the lesson forward** — inject the CI failure summary AND the prior
          attempt's diff into the next prompt (fresh-both keeps a fresh session, but
          the coder sees what it tried and why it failed).
        - **Same-tier fix, THEN escalate** — a red check is usually a fixable nit (a
          lint error, a golden-map update, a flaky assertion) the current tier can
          self-correct once it SEES the error, not a model-capability ceiling. So
          spend ``ci_fix_max`` same-tier retries first; only when those are exhausted
          does a configured `coders` ladder climb a tier (smart→reasoning→opus, the
          ladder is the bound → top tier fails → Blocked). Without a ladder the
          exhausted budget blocks directly. (Escalating on the FIRST failure burned
          the expensive tiers on one-line lint fixes — the goal-fix budget already
          learned this lesson; the CI path now mirrors it.)

        Passing/pending/no-checks → left in review (the merge edge resolves it)."""
        status, summary = await worktree.pr_ci_status(pr_url, cwd=repo)
        if status != "failing":
            return
        # Carry the lesson: the CI error + the diff that failed it (best-effort).
        self._ci_feedback[fid] = summary
        self._ci_prior_diff[fid] = await worktree.pr_diff(pr_url, cwd=repo)

        def _block(reason: str):
            store.flag_blocked(fid, reason)
            self._ci_feedback.pop(fid, None)
            self._ci_prior_diff.pop(fid, None)
            self._ci_fix_attempts.pop(fid, None)

        # Same-tier CI-fix budget FIRST (both ladder and single-coder): a red check
        # is usually a fixable nit the current tier can correct once it sees the
        # error — don't burn a stronger model on a one-line lint fix. The CI error +
        # prior diff are already injected above, so the re-dispatch improves on the
        # last try rather than repeating it.
        attempts = self._ci_fix_attempts.get(fid, 0)
        if attempts < self.ci_fix_max:
            self._ci_fix_attempts[fid] = attempts + 1
            store.requeue(fid)
            log.info(
                "[project_board] reconcile → same-tier CI-fix (attempt %d/%d): %s",
                attempts + 1,
                self.ci_fix_max,
                fid,
            )
            return

        # Same-tier budget exhausted. With a ladder, climb a model tier and reset the
        # per-tier budget so the new rung gets its own fix attempts; without one, block.
        if self.escalation_on:
            nxt = store.escalate(fid, f"CI failed: {_ci_failure_reason(summary)}")
            if not nxt:
                _block(f"CI failing at the top model tier after {attempts} same-tier fix(es) — needs triage: {pr_url}")
                await worktree.reap_feature_worktree(repo, self.root, fid)
                log.warning("[project_board] reconcile → blocked (CI fails at top tier): %s", fid)
                return
            self._ci_fix_attempts.pop(fid, None)  # fresh same-tier budget at the new rung
            store.requeue(fid)
            log.info("[project_board] reconcile → escalate to %s + re-dispatch (CI failed): %s", nxt, fid)
            return

        _block(f"CI still failing after {attempts} fix attempt(s) — needs triage: {pr_url}")
        await worktree.reap_feature_worktree(repo, self.root, fid)
        log.warning("[project_board] reconcile → blocked (CI fails, %d attempt(s) exhausted): %s", attempts, fid)

    async def _drive(self, feature: dict):
        """Drive one feature ready→in_review (or →blocked). `done` is set later by
        the merge webhook. With per-tier coders configured, a *capability* failure
        (coder errored / produced no diff) climbs the ladder; with a single coder
        it blocks at once — no redundant tier dance."""
        store = self._store()
        fid = feature["id"]
        repo = feature.get("repo") or "."
        base = feature.get("base_branch") or "main"
        title = f"feat: {feature['title']}"
        tier = store.current_tier(fid) if self.escalation_on else ""
        retries = 0  # transient-failure retries at the current tier (reset on a climb)
        wt = branch = None
        keep_wt = False  # reuse the worktree on a goal-fix retry (keep the impl; add tests)
        try:
            while True:
                # Rebuild the prompt each attempt so a re-dispatch (CI bounce,
                # goal-verify gap, or tier escalation) picks up the latest
                # _ci_feedback + _ci_prior_diff.
                prompt = self._build_prompt(feature)
                coder_name = self.coders.get(tier, self.coder_name) if self.escalation_on else self.coder_name
                coder = self._resolve_delegate(coder_name, "acp")
                if coder is None:
                    store.flag_blocked(fid, f"coder delegate {coder_name!r} not configured/enabled")
                    return
                # A goal-fix retry REUSES the worktree — the implementation is already
                # there, the coder just adds what the reviewer flagged (usually tests).
                # Rebuilding on a fresh worktree throws the impl away (the coder then
                # spends its budget re-implementing and never reaches the tests — the
                # bd-2fd/bd-3cj block). Otherwise: a fresh worktree per attempt.
                if keep_wt and wt is not None:
                    keep_wt = False  # consume the reuse
                else:
                    wt, branch = await worktree.create_worktree(repo, base, fid, self.root)
                self._inflight[fid] = (repo, wt, branch)  # track for shutdown reaping
                try:
                    result = await worktree.dispatch_coder(
                        coder, wt, prompt, timeout=self.coder_timeout or None
                    )  # reaps subprocess; CoderTimeout if it overruns
                    # Goal-verification gate: confirm the diff meets the acceptance
                    # criteria before opening a PR. A gap is a capability failure (the
                    # coder didn't deliver) → escalate/block, don't open the PR.
                    if self.goal_verify:
                        gap = await self._verify_goal(feature, wt, base, result or "")
                        if gap:
                            # A goal-verify gap (e.g. the coder skipped tests) is
                            # fixable by the SAME coder told what's missing — not a
                            # model-capability failure. Carry the gap (+ the rejected
                            # diff, stashed by _verify_goal) as feedback and re-dispatch
                            # the same tier, bounded by goal_fix_max, BEFORE escalating.
                            n = self._goal_fix_attempts.get(fid, 0)
                            if n < self.goal_fix_max:
                                self._goal_fix_attempts[fid] = n + 1
                                # KEEP the worktree (the impl is in its files); the coder
                                # only ADDS what the reviewer flagged. The diff is on disk,
                                # so don't also carry it as prompt text (redundant/confusing).
                                self._ci_prior_diff.pop(fid, None)
                                self._ci_feedback[fid] = (
                                    "Your implementation from the previous attempt is ALREADY in this "
                                    "worktree's files. A reviewer rejected it before it could open a PR "
                                    f"for: {gap}. ADD what's missing to the existing files (usually the "
                                    "tests) — do NOT rewrite or delete the working implementation. Then stop."
                                )
                                log.info(
                                    "[project_board] %s goal-verify gap — re-dispatch %d/%d (tier=%s, keep worktree): %s",
                                    fid,
                                    n + 1,
                                    self.goal_fix_max,
                                    tier or "default",
                                    gap,
                                )
                                keep_wt = True  # reuse the worktree (impl intact) on the retry
                                continue
                            raise worktree.WorktreeError(f"goal verification failed: {gap}")
                    # Auto-fix lint/format before the PR — the coder can't run the repo's
                    # formatter (edit-only), so this clears trivial nits that would fail CI.
                    await self._run_fixups(wt)
                    # Pre-PR local gate: run the repo's real checks in the worktree and, on
                    # failure, hand the coder the actual output to fix IN-WORKTREE before a PR
                    # (and a CI round-trip) ever opens. Same-tier, keep-worktree, bounded by
                    # local_gate_max; on exhaustion open the PR anyway (CI is the backstop).
                    gate_out = await self._run_local_gate(wt)
                    if gate_out is not None:
                        n = self._gate_fix_attempts.get(fid, 0)
                        if n < self.local_gate_max:
                            self._gate_fix_attempts[fid] = n + 1
                            self._ci_prior_diff.pop(fid, None)  # impl is on disk; don't echo it back
                            self._ci_feedback[fid] = (
                                "Your changes are ALREADY in this worktree's files, but the pre-PR "
                                "gate failed. FIX what it reports in the existing files, then stop — "
                                "the loop opens the PR. Do NOT rewrite working code. Gate output:\n\n" + gate_out
                            )
                            log.info(
                                "[project_board] %s pre-PR gate failed — re-dispatch %d/%d (tier=%s, keep worktree)",
                                fid,
                                n + 1,
                                self.local_gate_max,
                                tier or "default",
                            )
                            keep_wt = True
                            continue
                        log.warning(
                            "[project_board] %s pre-PR gate still failing after %d fix(es) — opening PR anyway (CI backstop)",
                            fid,
                            n,
                        )
                    pr_url = await worktree.open_pr(wt, branch, base=base, title=title, body=(result or "")[:4000])
                except (worktree.NoChangesError, worktree.WorktreeError) as exc:
                    policy = classify(str(exc))
                    # A capability failure = the coder didn't deliver (no diff / dispatch
                    # error / timed out). Those are NOT transient-retried (re-running the
                    # same coder won't help) — they escalate a tier or block. Only true
                    # infra failures (push/fetch/gh network/rate-limit) get the backoff.
                    capability = (
                        isinstance(exc, (worktree.NoChangesError, worktree.CoderTimeout))
                        or str(exc).startswith("coder dispatch failed")
                        or str(exc).startswith("goal verification failed")
                    )
                    # 1. Transient infra → back off and retry the SAME tier (a re-dispatch
                    #    off the latest base also clears a merge conflict).
                    if policy.retryable and not capability and retries < policy.max_attempts - 1:
                        retries += 1
                        log.info(
                            "[project_board] %s %s — retry %d/%d in %ss: %s",
                            fid,
                            policy.category,
                            retries + 1,
                            policy.max_attempts,
                            policy.base_delay_s,
                            exc,
                        )
                        await asyncio.sleep(policy.base_delay_s)
                        continue
                    # 2. Capability failure + a ladder → climb a model tier (fresh budget).
                    if self.escalation_on and capability:
                        nxt = store.escalate(fid, str(exc)[:200])
                        if nxt:
                            log.info("[project_board] %s escalating %s→%s: %s", fid, tier, nxt, exc)
                            tier = nxt
                            retries = 0
                            # Fresh goal-fix budget at the new tier — otherwise a tier that
                            # exhausted its goal-fix retries hands the next (stronger) tier a
                            # spent budget, so it blocks on its first gap without a real shot.
                            self._goal_fix_attempts.pop(fid, None)
                            self._gate_fix_attempts.pop(fid, None)  # fresh local-gate budget too
                            continue
                    # 3. Terminal, or retries/ladder exhausted → Blocked.
                    log.warning("[project_board] %s blocked (%s): %s", fid, policy.category, exc)
                    store.flag_blocked(fid, f"{policy.category}: {exc}")
                    if wt:
                        await worktree.remove_worktree(repo, wt, branch or "")
                    self._inflight.pop(fid, None)
                    return
                # Built + PR opened. The fleet PR-review pipeline reviews it on open;
                # only dispatch an explicit review when configured to (review_dispatch).
                log.info("[project_board] %s coder done (%d chars) → %s", fid, len(result or ""), pr_url)
                store.open_review(fid, pr_url=pr_url)
                self._goal_fix_attempts.pop(fid, None)  # gate passed — reset the goal-fix budget
                self._gate_fix_attempts.pop(fid, None)  # and the local-gate budget
                if self.review_dispatch:
                    await self._request_review(fid, pr_url)
                # Keep the worktree (a CI-fail bounce re-dispatches); reaping happens
                # on a terminal block above, and the coder subprocess is already reaped.
                self._inflight.pop(fid, None)  # built OK — not an interrupted build to reap
                return
        except BoardError as exc:
            log.warning("[project_board] %s blocked (board): %s", fid, exc)
            store.flag_blocked(fid, str(exc))
            self._inflight.pop(fid, None)
        except Exception as exc:  # noqa: BLE001 — unexpected; block, don't crash the loop
            log.exception("[project_board] %s unexpected failure", fid)
            store.flag_blocked(fid, f"unexpected: {type(exc).__name__}: {exc}")
            if wt:
                await worktree.remove_worktree(repo, wt, branch or "")
            self._inflight.pop(fid, None)

    async def _request_review(self, fid: str, pr_url: str):
        """Hand the PR to the reviewer (an a2a delegate, e.g. quinn). Best-effort:
        a review-dispatch failure doesn't block the feature — CI + the merge
        webhook are the gate; the reviewer is advisory signal."""
        reviewer = self._resolve_delegate(self.reviewer_name, "a2a")
        if reviewer is None:
            log.info("[project_board] no reviewer %r configured — skipping review dispatch", self.reviewer_name)
            return
        from plugins.delegates.adapters import ADAPTERS

        try:
            msg = f"Please review this PR for correctness and acceptance: {pr_url}"
            await ADAPTERS["a2a"].dispatch(reviewer, msg)
        except Exception as exc:  # noqa: BLE001 — fully best-effort: a review-dispatch
            # failure (DelegateError, httpx/connection, anything) must NEVER block a
            # feature whose PR already opened. CI + the merge webhook are the gate.
            log.warning("[project_board] review dispatch for %s failed: %s", fid, exc)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _resolve_delegate(self, name: str, expect_type: str):
        """Look up a live delegate by name from the delegates registry. Returns the
        Delegate or None (not configured / wrong type / plugin disabled)."""
        try:
            from plugins.delegates.registry import DelegateRegistry
            from plugins.delegates.store import merged_delegates

            d = DelegateRegistry(merged_delegates()).get(name)
        except Exception:  # noqa: BLE001 — delegates plugin may be disabled
            return None
        if d is None or d.type != expect_type:
            return None
        return d

    async def _run_fixups(self, wt: str) -> None:
        """Run the repo's auto-fix command (``format_cmd``, e.g.
        ``ruff check --fix . && ruff format .``) in the worktree before opening the PR.
        The coder is edit-only — it can't run the linter/formatter, so trivial lint/format
        nits would otherwise fail CI and burn a bounce/escalation. Best-effort: no command
        configured, or any error/timeout, just proceeds (CI is still the real lint gate)."""
        if not self.format_cmd:
            return
        try:
            proc = await asyncio.create_subprocess_shell(
                self.format_cmd,
                cwd=wt,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=180)
        except Exception as exc:  # noqa: BLE001 — best-effort; CI still gates lint
            log.info("[project_board] fixups command failed (proceeding — CI still gates): %s", exc)

    async def _run_local_gate(self, wt: str) -> str | None:
        """Run the pre-PR local gate (``local_gate_cmd``) in the worktree.

        Returns ``None`` when the gate passes (exit 0), when no gate is configured,
        or when the gate itself couldn't run (timeout / unlaunchable command) — a
        broken or flaky gate must never block otherwise-good work, so those degrade
        to "pass" (CI is still the real gate). Returns the captured output (tail,
        truncated to ``local_gate_output_chars``) on a CLEAN non-zero exit, so the
        caller can hand it to the coder to fix."""
        cmd = self.local_gate_cmd
        if not cmd:
            return None
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=wt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.local_gate_timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                log.warning("[project_board] pre-PR gate timed out (%ss) — treating as pass", self.local_gate_timeout)
                return None
            if proc.returncode == 0:
                return None
            text = (out or b"").decode("utf-8", "replace").strip()
            if len(text) > self.local_gate_output_chars:
                text = "…(truncated)…\n" + text[-self.local_gate_output_chars :]
            return text or f"gate command exited {proc.returncode} with no output"
        except Exception as exc:  # noqa: BLE001 — a gate that can't run must not block
            log.info("[project_board] pre-PR gate failed to run (treating as pass — CI still gates): %s", exc)
            return None

    async def _verify_goal(self, feature: dict, wt: str, base: str, coder_reply: str = "") -> str | None:
        """Pre-PR gate — DETERMINISTIC: no LLM, no diff dump. The one thing it adds over
        CI is requiring a test to EXIST for a code change (CI runs tests but can't require
        their presence). So it just checks the changed-file LIST for a test file — cheap,
        instant, and immune to the truncation that made the old "LLM eyeballs the diff"
        version false-reject tests it couldn't see (smart/reasoning/opus each "failed" on
        tests they'd actually written — tests sort LAST by path and fell off the cap, ~40
        min of cycles wasted). CORRECTNESS is CI's job — it runs the tests the coder wrote;
        a wrong diff fails CI and the CI-feedback edge bounces it back.

        ESCAPE HATCH: not every code change needs a test (a pure refactor, config/docs-as-
        code, a constant tweak). The coder — which saw the actual change — can declare
        ``NO_TEST_NEEDED: <reason>`` in its reply; we log the reason and pass, rather than
        burning retries on a test that doesn't apply. Returns a gap string (→ re-dispatch/
        escalate) or None. Fails OPEN on any error (never blocks a good PR on infra)."""
        ac = (feature.get("acceptance_criteria") or "").strip()
        if not ac:
            return None
        try:
            await worktree._git(wt, "add", "-A")
            _rc, names, _err = await worktree._git(wt, "diff", "--cached", "--name-only", f"origin/{base}")
        except Exception:  # noqa: BLE001 — best-effort
            return None
        changed = [n for n in (names or "").split() if n]
        if not changed:
            return None  # an empty diff is open_pr's NoChangesError job, not ours
        code = [n for n in changed if _is_code_path(n) and not _is_test_path(n)]
        if code and not any(_is_test_path(n) for n in changed):
            if "NO_TEST_NEEDED" in (coder_reply or ""):
                reason = (coder_reply.split("NO_TEST_NEEDED", 1)[1].lstrip(": ").splitlines() or [""])[0].strip()
                log.info(
                    "[project_board] %s no-test accepted (coder declared): %s",
                    feature.get("id"),
                    reason[:200] or "(no reason given)",
                )
                return None
            head = ", ".join(code[:6]) + ("…" if len(code) > 6 else "")
            return (
                "no test was added/updated for the code change — add a test covering the new "
                f"behavior, or declare `NO_TEST_NEEDED: <reason>` if a test genuinely doesn't "
                f"apply (refactor/config/docs) (code: {head})"
            )
        return None

    async def _judge_candidates(self, feature: dict, base: str, worktrees: list[str]) -> int | None:
        """Max-Mode best-of-N judge: given N candidate worktrees for the same feature,
        pick the index whose diff best satisfies the ``acceptance_criteria``. Returns
        the winning index, or ``None`` when there's no non-empty candidate.

        Reuses the goal-verify diff+``complete()`` seam. Best-effort: candidates with no
        diff are skipped; if the judge errors or is unparseable, falls back to the first
        non-empty candidate (never returns a worse-than-arbitrary answer). The N-parallel
        dispatch that produces ``worktrees`` is tracked in #21; this is the judge it calls."""
        ac = (feature.get("acceptance_criteria") or "").strip()
        diffs: list[str] = []
        for wt in worktrees:
            try:
                await worktree._git(wt, "add", "-A")
                _rc, d, _err = await worktree._git(wt, "diff", "--cached", f"origin/{base}")
            except Exception:  # noqa: BLE001 — judging is best-effort
                d = ""
            diffs.append((d or "").strip())

        nonempty = [i for i, d in enumerate(diffs) if d]
        if not nonempty:
            return None
        if len(nonempty) == 1:
            return nonempty[0]

        blocks = "\n\n".join(f"### Candidate {i}\n```diff\n{diffs[i][:4000]}\n```" for i in nonempty)
        prompt = (
            f"{len(nonempty)} coding agents each attempted the same task.\n\n"
            f"Acceptance criteria:\n{ac or '(none given)'}\n\n"
            f"{blocks}\n\n"
            "Which candidate BEST satisfies every acceptance criterion (most correct, "
            "complete, and clean)? Reply with ONLY the candidate number."
        )
        try:
            from graph.sdk import complete

            verdict = (await complete(prompt, system=_MAX_MODE_JUDGE_SYS) or "").strip()
        except Exception as exc:  # noqa: BLE001 — never fail the build on the judge
            log.warning(
                "[project_board] %s max-mode judge errored (using first candidate): %s",
                feature.get("id"),
                exc,
            )
            return nonempty[0]

        for tok in re.findall(r"\d+", verdict):
            idx = int(tok)
            if idx in nonempty:
                return idx
        return nonempty[0]  # judge unclear → first non-empty candidate

    def _build_prompt(self, feature: dict) -> str:
        """An imperative, fully-specified instruction (ProtoMaker discipline). A
        passive 'implement this feature' + a vague spec makes a coder produce
        nothing; naming the files + a direct 'make the edits now' makes it act."""
        files = feature.get("files_to_modify") or []
        files_block = (
            "\n".join(f"- {f}" for f in files) if files else "(none listed — create the files the task requires)"
        )
        design = feature.get("design", "")
        design_block = f"\n## Design / context\n{design}\n" if design.strip() else ""
        # CI-feedback re-dispatch (closed-loop verify): a prior attempt's PR failed
        # CI; lead with the failure so the coder FIXES it this pass (it can't run the
        # checks itself — edit-only). Also widen scope: the fix may touch tests/files
        # the original `files_to_modify` didn't list (the #1053 lesson).
        fid = feature.get("id", "")
        ci = self._ci_feedback.get(fid)
        prior = self._ci_prior_diff.get(fid)
        prior_block = (
            f"\n### The diff that failed (your previous attempt — fix it, don't restart from scratch)\n"
            f"```diff\n{prior}\n```\n"
            if prior
            else ""
        )
        ci_block = (
            "\n## ⚠ Your previous attempt was REJECTED — fix it this attempt\n"
            f"{ci}\n"
            f"{prior_block}"
            "Address the problem above. This may require editing files beyond the list "
            "below — e.g. ADD the missing tests, or update an e2e/unit test that assumed "
            "the old behavior.\n"
            if ci
            else ""
        )
        return (
            f"You are implementing ONE feature in this repository. Your working "
            f"directory is an isolated git worktree — **make all the edits here, now**. "
            f"Do not ask questions or just describe a plan; if something is ambiguous, "
            f"make the most reasonable choice and write the code.\n\n"
            f"# {feature['title']}\n\n"
            f"{ci_block}"
            f"## Task\n{feature.get('spec', '')}\n\n"
            f"## Files to create / modify\n{files_block}\n"
            f"{design_block}\n"
            f"## Acceptance criteria (definition of done)\n{feature.get('acceptance_criteria', '')}\n\n"
            f"## Rules\n"
            f"- Make the edits directly in the working tree NOW — actually write the files.\n"
            f"- Touch only the files this task needs; mirror the surrounding code's style.\n"
            f"- **Write automated tests** covering the new/changed behavior (a new or "
            f"updated test file, matching the repo's existing test conventions). This is "
            f"part of the definition of done, not optional — a code change with no test "
            f"is rejected before the PR opens. If a test GENUINELY doesn't apply (a pure "
            f"refactor, config/docs-as-code, or a change with no behavior to exercise), "
            f"write a single line `NO_TEST_NEEDED: <reason>` in your final message instead.\n"
            f"- You cannot run shell commands (edit-only); the tests you write run in CI "
            f"on the PR, so they must be correct and self-contained.\n"
            f"- You are done when the listed files exist, tests cover the change, and "
            f"every acceptance criterion is satisfied."
        )
