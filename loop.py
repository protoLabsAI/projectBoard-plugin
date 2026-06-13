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
reachable, by the loop's **merge poll** (``merge_poll``), which asks ``gh`` whether
each ``in_review`` PR has merged and runs the same idempotent Done edge. Up to
``max_concurrent`` features build concurrently, each in its own worktree.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import worktree
from .failures import classify
from .store import BoardError, escalation_enabled, get_store

log = logging.getLogger("protoagent.plugins.project_board")


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
        # Stuck-drive watchdog: hard cap on a single coder dispatch (the only
        # otherwise-unbounded await in a drive — git/gh calls already self-time-out).
        # 0 disables it. A timeout reaps the coder subprocess and is a capability
        # failure (escalate-or-block), not a transient retry.
        self.coder_timeout = float(self.cfg.get("coder_timeout_s", 1800))
        # Merge poll: a fallback to the /webhook/pr Done edge for deployments with no
        # public webhook URL. On by default (cheap; only probes `in_review` PRs).
        self.merge_poll = bool(self.cfg.get("merge_poll", True))
        self.merge_poll_interval = float(self.cfg.get("merge_poll_interval_s", 60))
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
        self._last_poll = 0.0  # monotonic ts of the last merge poll

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
    async def _recover(self):
        """Reconcile features the previous run left mid-drive. A drive doesn't survive
        a restart, so every ``in_progress`` feature is orphaned (claimed, no PR yet):
        if its PR actually got opened (a crash between ``open_pr`` and ``open_review``)
        adopt it → ``in_review``; otherwise reset it to ``ready`` for a clean rebuild
        (any stale worktree is cleaned when the puller re-claims it). ``in_review``
        features are NOT touched — they have a PR and the webhook/poll resolves them."""
        store = self._store()
        repo = self._store_kw["repo"]
        for f in store.list_features(state="in_progress"):
            fid = f["id"]
            try:
                pr_url = await worktree.pr_url_for_branch(f"feat/{fid}", cwd=repo)
                if pr_url:
                    store.open_review(fid, pr_url=pr_url)
                    log.info("[project_board] recovery: %s already had a PR → in_review (%s)", fid, pr_url)
                else:
                    store.requeue(fid)
                    log.info("[project_board] recovery: %s reset to ready (no PR — rebuild fresh)", fid)
            except Exception:  # noqa: BLE001 — recovery is best-effort, per feature
                log.warning("[project_board] recovery for %s failed", fid, exc_info=True)

    # ── the puller ────────────────────────────────────────────────────────────
    async def _run(self):
        try:
            await self._recover()
        except Exception:  # noqa: BLE001 — recovery must never stop the loop from starting
            log.exception("[project_board] crash recovery failed")
        while not self._stop.is_set():
            spawned = False
            try:
                await self._maybe_poll_merges()
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
        """Claim Ready features up to the concurrency cap and spawn a drive task for
        each. Returns True if it started at least one (so the runner stays hot)."""
        spawned = False
        while len(self._drives) < self.max_concurrent:
            feature = self._store().claim_next_ready(assignee=self.coder_name)
            if feature is None:
                break
            task = asyncio.create_task(self._drive(feature), name=f"pb-drive-{feature['id']}")
            self._drives.add(task)
            task.add_done_callback(self._drives.discard)
            spawned = True
        return spawned

    # ── the merge poll (Done-edge fallback to the webhook) ─────────────────────
    async def _maybe_poll_merges(self):
        """Run the merge poll at most once per ``merge_poll_interval`` (and only when
        enabled) — cheap, but no reason to hammer ``gh`` every busy tick."""
        if not self.merge_poll:
            return
        now = time.monotonic()
        if now - self._last_poll < self.merge_poll_interval:
            return
        self._last_poll = now
        await self._poll_merges()

    async def _poll_merges(self):
        """Ask ``gh`` whether each ``in_review`` PR has merged and run the idempotent
        Done edge for any that have — the fallback for deployments GitHub can't post
        a webhook to (otherwise a merged feature would sit in_review forever)."""
        store = self._store()
        repo = self._store_kw["repo"]
        for f in store.list_features(state="in_review"):
            pr_url = f.get("pr_url")
            if not pr_url:
                continue
            try:
                if not await worktree.pr_is_merged(pr_url, cwd=repo):
                    continue
                done = store.record_merge(pr_url=pr_url)
            except Exception:  # noqa: BLE001 — a poll error must never kill the loop
                log.warning("[project_board] merge poll for %s failed", f["id"], exc_info=True)
                continue
            if done:
                await worktree.reap_feature_worktree(repo, self.root, f["id"])
                log.info("[project_board] merge poll → done: %s (%s)", f["id"], pr_url)

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
        prompt = self._build_prompt(feature)
        tier = store.current_tier(fid) if self.escalation_on else ""
        retries = 0  # transient-failure retries at the current tier (reset on a climb)
        wt = branch = None
        try:
            while True:
                coder_name = self.coders.get(tier, self.coder_name) if self.escalation_on else self.coder_name
                coder = self._resolve_delegate(coder_name, "acp")
                if coder is None:
                    store.flag_blocked(fid, f"coder delegate {coder_name!r} not configured/enabled")
                    return
                # Fresh worktree per attempt (a failed attempt may leave partial work).
                wt, branch = await worktree.create_worktree(repo, base, fid, self.root)
                self._inflight[fid] = (repo, wt, branch)  # track for shutdown reaping
                try:
                    result = await worktree.dispatch_coder(
                        coder, wt, prompt, timeout=self.coder_timeout or None
                    )  # reaps subprocess; CoderTimeout if it overruns
                    pr_url = await worktree.open_pr(wt, branch, base=base, title=title, body=(result or "")[:4000])
                except (worktree.NoChangesError, worktree.WorktreeError) as exc:
                    policy = classify(str(exc))
                    # A capability failure = the coder didn't deliver (no diff / dispatch
                    # error / timed out). Those are NOT transient-retried (re-running the
                    # same coder won't help) — they escalate a tier or block. Only true
                    # infra failures (push/fetch/gh network/rate-limit) get the backoff.
                    capability = isinstance(exc, (worktree.NoChangesError, worktree.CoderTimeout)) or str(
                        exc
                    ).startswith("coder dispatch failed")
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
        return (
            f"You are implementing ONE feature in this repository. Your working "
            f"directory is an isolated git worktree — **make all the edits here, now**. "
            f"Do not ask questions or just describe a plan; if something is ambiguous, "
            f"make the most reasonable choice and write the code.\n\n"
            f"# {feature['title']}\n\n"
            f"## Task\n{feature.get('spec', '')}\n\n"
            f"## Files to create / modify\n{files_block}\n"
            f"{design_block}\n"
            f"## Acceptance criteria (definition of done)\n{feature.get('acceptance_criteria', '')}\n\n"
            f"## Rules\n"
            f"- Make the edits directly in the working tree NOW — actually write the files.\n"
            f"- Touch only the files this task needs; mirror the surrounding code's style.\n"
            f"- You cannot run shell commands (edit-only); tests run in CI on the PR.\n"
            f"- You are done when the listed files exist and satisfy every acceptance criterion."
        )
