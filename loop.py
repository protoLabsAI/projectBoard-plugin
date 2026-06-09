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
              done                in_progress (bounce)     blocked (flag + reason)

STUBBED for review: CI status + merge arrive via webhook/poll (api.py), not here;
``open_pr`` is sketched (worktree.py). The loop's claim/dispatch/teardown/error
paths below are the thing to review before hardening. Concurrency is capped at 1
for the first cut (token + merge-integration cost); the cap is where parallelism
lands later.
"""

from __future__ import annotations

import asyncio
import logging

from . import worktree
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
        self._store_kw = dict(db=self.cfg.get("db_path") or None,
                              repo=self.cfg.get("repo", "."),
                              base_branch=self.cfg.get("base_branch", "main"))
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # The in-flight build's worktree, so shutdown can reap it (a cancel mid-drive
        # would otherwise orphan the worktree; the coder subprocess is reaped by
        # dispatch_coder's finally). (repo, worktree_path, branch) or None.
        self._active: tuple[str, str, str] | None = None

    def _store(self):
        return get_store(**self._store_kw)

    # ── lifecycle (register_surface start/stop) ───────────────────────────────
    def start(self):
        if not self.enabled:
            log.info("[project_board] loop disabled (project_board.loop_enabled=false) — board API still serves")
            return None
        self._task = asyncio.create_task(self._run(), name="project-board-loop")
        log.info("[project_board] loop started (coder=%s reviewer=%s every %ss)",
                 self.coder_name, self.reviewer_name, self.interval)
        return self._task

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Reap an interrupted build's worktree (a drive cancelled mid-flight leaves
        # self._active set; a completed/blocked drive clears it). Best-effort.
        act, self._active = self._active, None
        if act:
            repo, wt, branch = act
            try:
                await worktree.remove_worktree(repo, wt, branch or "")
                log.info("[project_board] reaped in-flight worktree on shutdown: %s", wt)
            except Exception:  # noqa: BLE001 — teardown must not raise out of shutdown
                log.warning("[project_board] worktree reap on shutdown failed: %s", wt, exc_info=True)

    # ── the puller ────────────────────────────────────────────────────────────
    async def _run(self):
        while not self._stop.is_set():
            try:
                worked = await self._tick()
            except Exception:  # noqa: BLE001 — a bad tick must never kill the loop
                log.exception("[project_board] loop tick failed")
                worked = False
            # If we did work, loop again immediately (drain Ready); else sleep.
            if not worked:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                except asyncio.TimeoutError:
                    pass

    async def _tick(self) -> bool:
        """Claim and drive at most one Ready feature. Returns True if it claimed
        one (so the runner can drain), False if Ready was empty."""
        feature = self._store().claim_next_ready(assignee=self.coder_name)
        if feature is None:
            return False
        await self._drive(feature)
        return True

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
                self._active = (repo, wt, branch)   # track for shutdown reaping
                try:
                    result = await worktree.dispatch_coder(coder, wt, prompt)  # reaps subprocess
                    pr_url = await worktree.open_pr(wt, branch, base=base, title=title,
                                                    body=(result or "")[:4000])
                except (worktree.NoChangesError, worktree.WorktreeError) as exc:
                    # Capability failure (coder errored / no diff) → escalate when a
                    # ladder exists; an infra failure (push/gh) is not escalable → block.
                    capability = isinstance(exc, worktree.NoChangesError) or \
                        str(exc).startswith("coder dispatch failed")
                    if self.escalation_on and capability:
                        nxt = store.escalate(fid, str(exc)[:200])
                        if nxt:
                            log.info("[project_board] %s escalating %s→%s: %s", fid, tier, nxt, exc)
                            tier = nxt
                            continue
                    log.warning("[project_board] %s blocked: %s", fid, exc)
                    store.flag_blocked(fid, str(exc))
                    if wt:
                        await worktree.remove_worktree(repo, wt, branch or "")
                    self._active = None
                    return
                # Built + PR opened. The fleet PR-review pipeline reviews it on open;
                # only dispatch an explicit review when configured to (review_dispatch).
                log.info("[project_board] %s coder done (%d chars) → %s", fid, len(result or ""), pr_url)
                store.open_review(fid, pr_url=pr_url)
                if self.review_dispatch:
                    await self._request_review(fid, pr_url)
                # Keep the worktree (a CI-fail bounce re-dispatches); reaping happens
                # on a terminal block above, and the coder subprocess is already reaped.
                self._active = None   # built OK — not an interrupted build to reap
                return
        except BoardError as exc:
            log.warning("[project_board] %s blocked (board): %s", fid, exc)
            store.flag_blocked(fid, str(exc))
            self._active = None
        except Exception as exc:  # noqa: BLE001 — unexpected; block, don't crash the loop
            log.exception("[project_board] %s unexpected failure", fid)
            store.flag_blocked(fid, f"unexpected: {type(exc).__name__}: {exc}")
            if wt:
                await worktree.remove_worktree(repo, wt, branch or "")
            self._active = None

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
        files_block = "\n".join(f"- {f}" for f in files) if files else \
            "(none listed — create the files the task requires)"
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
