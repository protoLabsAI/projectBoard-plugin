"""Reconcile (CI / rebase / merged-state / review / auto-merge) edge of the board loop (extracted from loop.py, #268).

Behavior-preserving move: these methods were lifted verbatim from ``BoardLoop``
and run as a mixin on the assembled ``BoardLoop`` in :mod:`.core`. Cross-edge
``self.<method>()`` calls resolve through the MRO, unchanged. The shared loop
kernel (constants, helpers, process-stable state) is re-exported from
:mod:`._common`; rebindable seams are read through the live package (``_loop``)
so tests that monkeypatch ``project_board.loop.<name>`` still take effect.
"""

from __future__ import annotations

import sys

from ._common import *  # noqa: F401,F403 — share the loop kernel namespace

_loop = sys.modules[__package__]  # the loop package, for monkeypatch-visible seams


class ReconcileMixin:
    # ── merged-verify exhaustion sentinel ↔ operator reset (ADR 0326, #326) ───────
    def _arm_merged_verify_exhaustion(self, store, fid: str) -> bool:
        """Persist the ONE-TIME exhaustion sentinel `budget:merged-verify:<max+1>` — the
        fact ``store.merge_posture`` reads to hold an ``auto_merge`` card whose merged-
        state re-verify budget is spent while base keeps moving — but as a COMPARE-AND-SET
        under the reset lock, NOT a blind write. Arms ONLY if the in-process count is
        still exactly at the cap; a concurrent operator reset
        (``_invalidate_merged_verify_budget``) PINS the count to 0 and clears the label
        under the same lock, so if it already landed we read 0 (≠ cap) and skip — the
        reset's fresh window stands instead of being silently re-held. If we arm first,
        the reset that follows wipes both halves (pinned cache + re-cleared label). Runs
        in a worker thread (via ``asyncio.to_thread``) so the plain lock is never held on
        the event loop. Best-effort on the label, like ``_budget_set``. Returns True iff
        it armed."""
        with self._mv_reset_lock:
            if self._merged_verify_attempts.get(fid) != self.merged_verify_max:
                return False  # a reset (pin 0) or a tier climb moved it off the cap — don't re-arm
            value = self.merged_verify_max + 1
            self._merged_verify_attempts[fid] = value
            try:
                store.record_budget(fid, "merged-verify", value)
            except Exception:  # noqa: BLE001 — bookkeeping must never break the edge
                log.warning("[project_board] %s merged-verify exhaustion sentinel (%d) not persisted", fid, value)
            return True

    def _invalidate_merged_verify_budget(self, fid: str, store) -> None:
        """Operator reset of the LIVE loop's merged-verify budget (ADR 0326, #326). PINS
        the in-process count to 0 — NOT a pop: the #259 ``_budget_reset`` mid-flow rule is
        that a popped key lets the very next ``_budget_get(..., feature)`` rehydrate the
        exhausted count from a poll's stale label snapshot and re-hold the card, so 0 must
        be AUTHORITATIVE until a real re-verify spends it again. Re-clears the durable
        label under the SAME lock the exhaustion sentinel arms under: an in-flight
        reconcile that already read the at-cap count and won the race to arm the sentinel
        (cache + label = max+1) is then fully undone — the pin wipes its cache write and
        this clear wipes the label it persisted, so the board projection can't keep
        reading a stale hold. Best-effort on the label (the tool's store reset already
        dropped it); this backstops only the racing re-arm. Runs on the reset verb's
        worker thread."""
        with self._mv_reset_lock:
            self._merged_verify_attempts[fid] = 0
            try:
                store.clear_budgets(fid, ["merged-verify"])
            except Exception:  # noqa: BLE001 — bookkeeping must never break the reset
                log.warning("[project_board] %s merged-verify label re-clear failed on reset", fid)

    def _warn_if_review_gate_unrunnable(self) -> None:
        """Boot-time preflight for the review gate (#180): review_gate=True with
        neither a workflow runner (``STATE.workflow_run`` — absent when the
        workflows plugin is disabled) nor a resolvable reviewer means EVERY
        review will fail closed. Say so once, loudly, at loop start — instead of
        letting the operator correlate per-feature gate warnings with a plugin
        toggle by reading the server log. Advisory only: the per-run gate still
        fails closed on its own; a runner appearing later just works."""
        if not self.review_gate:
            return
        runner = None
        try:
            from runtime.state import STATE

            runner = getattr(STATE, "workflow_run", None)
        except Exception:  # noqa: BLE001 — non-protoAgent host (tests)
            runner = None
        if runner is not None or self._resolve_delegate(self.reviewer_name, "a2a") is not None:
            return
        log.warning(
            "[project_board] review_gate is on but no review runner available "
            "(workflows plugin disabled? reviewer_name not set?) — every review will fail closed"
        )

    # ── crash recovery (runs once, before the puller claims new work) ──────────
    async def _reconcile_orphan(self, fid: str):
        """A claimed feature with no live drive: if its PR actually got opened (a crash
        between ``open_pr`` and ``open_review``) adopt it → ``in_review``; else, if a
        VERIFIED candidate was recorded at coder_seam's verify boundary and still checks
        out on disk (a crash between verify and ``open_pr``), salvage it — resume at
        promote → fixups → gate → open_pr instead of re-solving (#91); otherwise reset
        it to ``ready`` for a clean rebuild (a stale worktree is cleaned when the
        puller re-claims it). Shared by boot recovery and the health sweep."""
        store = self._store()
        feature = await asyncio.to_thread(store.get_feature, fid) or {}
        # #217/#304: a task bead has no PR/worktree, so the PR-adopt / verified-candidate
        # salvage below never apply. A task parked on a HUMAN assignee is NOT orphaned —
        # it is intentionally in_progress awaiting async delivery (API/chat), the same
        # "leave it, an out-of-band edge resolves it" posture an in_review PR gets — so
        # leave it be. A task on a DISPATCHABLE target whose drive died mid-flight IS
        # orphaned: requeue it for a clean re-dispatch. Dispatchable means either
        #
        #   - a SISTER-AGENT assignee (ACP coder OR A2A agent), or
        #   - this board's OWN agent (#311 — the `self`/`agent` aliases, or the configured
        #     coder name), which `_dispatch_self` drives through HOST.invoke.
        #
        # The self case was missing, and it stranded every self-assigned task PERMANENTLY:
        # `_is_self_assignee` is only consulted in `_dispatch_task`, which only ever sees
        # `ready` candidates, so a self task that reached in_progress without a live drive
        # could never get back to `ready` — making the whole #311 self-dispatch path
        # structurally unreachable for it. The sweep logged "in_progress with no live
        # drive" against it forever instead.
        #
        # Note this also covers the TRULY-UNASSIGNED park: `claim_task` resolves a task
        # with no target to the store actor, whose default name ("agent") IS a self alias,
        # so an unassigned card arrives here reading self-assigned and now self-dispatches
        # on the next tick rather than parking forever. That is the honest reading of the
        # assignee-as-dispatch-target invariant `requeue` already protects (it deliberately
        # keeps a task's assignee, because clearing it stranded a live self-assigned audit
        # task once). A task that must wait on a PERSON has to name that person.
        if feature.get("issue_type") == LABEL_TASK:
            assignee = str(feature.get("assignee") or "").strip()
            if self._is_self_assignee(assignee):
                await asyncio.to_thread(store.requeue, fid)
                log.info("[project_board] %s self task reset to ready (no live drive — re-dispatch)", fid)
            elif self._resolve_task_delegate(assignee) is not None:
                await asyncio.to_thread(store.requeue, fid)
                log.info("[project_board] %s task reset to ready (sister-agent drive died — re-dispatch)", fid)
            return
        pr_url = await worktree.pr_url_for_branch(
            worktree.branch_name(fid, feature.get("title") or ""), cwd=self._repo_for(feature)
        )
        if pr_url:
            await asyncio.to_thread(store.open_review, fid, pr_url=pr_url)
            log.info("[project_board] %s already had a PR → in_review (%s)", fid, pr_url)
        elif await self._salvage_verified_candidate(store, fid):
            pass  # resumed + PR opened → in_review (logged inside)
        else:
            await asyncio.to_thread(store.requeue, fid)
            log.info("[project_board] %s reset to ready (no PR — rebuild fresh)", fid)

    @staticmethod
    def _clear_verified(store, fid: str) -> None:
        """Best-effort drop of the salvage record — bookkeeping only, never raises."""
        try:
            store.clear_verified_candidate(fid)
        except Exception:  # noqa: BLE001 — a failed clear must not fail recovery
            log.warning("[project_board] %s clear_verified_candidate failed (ignored)", fid, exc_info=True)

    async def _salvage_verified_candidate(self, store, fid: str) -> bool:
        """Crash salvage (#91): resume a build whose candidate already PASSED its
        acceptance tests but crashed before ``open_pr``.

        ``coder_seam.dispatch`` records the verified candidate at its verify boundary
        (a ``verified:<sha>`` label + a bead comment with {branch, sha, worktree}). If
        that record still checks out EXACTLY — the canonical worktree dir exists, it
        has the recorded branch checked out at the recorded sha, and the pre-PR gate
        passes on it NOW — resume the tail of the drive (promote → fixups → gate →
        open_pr → in_review) instead of throwing a verified build away to re-solve.
        ANY doubt (no record, worktree gone, branch/sha drift, gate red now, any error
        anywhere) → False, and the caller falls through to today's rebuild-fresh
        unchanged — a wrong salvage ships unverified code; a skipped one only costs a
        rebuild."""
        try:
            f = await asyncio.to_thread(store.get_feature, fid) or {}
            sha = str(f.get("verified_sha") or "").strip()
            if not sha:
                return False
            repo = self._repo_for(f)
            base = self._base_branch_for(f)
            title_raw = f.get("title") or ""
            branch = worktree.branch_name(fid, title_raw)
            wt = os.path.join(repo, self.root, worktree.worktree_dir(fid, title_raw))
            if not os.path.isdir(wt):
                log.info("[project_board] %s salvage: verified worktree gone — rebuild fresh", fid)
                await asyncio.to_thread(self._clear_verified, store, fid)
                return False
            rc, head, _err = await worktree._git(wt, "rev-parse", "HEAD")
            if rc != 0 or head.strip() != sha:
                log.info(
                    "[project_board] %s salvage: sha drift (%s ≠ recorded %s) — rebuild fresh",
                    fid,
                    head.strip()[:12],
                    sha[:12],
                )
                await asyncio.to_thread(self._clear_verified, store, fid)
                return False
            rc, cur, _err = await worktree._git(wt, "rev-parse", "--abbrev-ref", "HEAD")
            if rc != 0 or cur.strip() != branch:
                log.info("[project_board] %s salvage: branch drift (%s ≠ %s) — rebuild fresh", fid, cur.strip(), branch)
                await asyncio.to_thread(self._clear_verified, store, fid)
                return False
            # Resume the drive's tail in its normal order: promote (a no-op — the
            # record is written post-promote, so the candidate already holds the
            # canonical name) → fixups → gate → open_pr. The gate re-runs NOW: a
            # candidate that verified before the crash but fails today (base moved,
            # env changed) is a doubt, not a ship.
            wt, branch = await worktree.promote_worktree(repo, wt, branch, fid, self.root, title=title_raw)
            await self._run_fixups(wt, f)
            if await self._run_local_gate(wt, f) is not None:
                log.info("[project_board] %s salvage: gate fails on the candidate now — rebuild fresh", fid)
                await asyncio.to_thread(self._clear_verified, store, fid)
                return False
            title = f"feat: {f.get('title') or fid}"
            body = await self._with_source_issue_ref(f, wt, _pr_body("", f))
            pr_url = await worktree.open_pr(
                wt, branch, base=base, title=title, body=body, promote_draft=not f.get("pr_url")
            )
            await asyncio.to_thread(store.open_review, fid, pr_url=pr_url)
            await asyncio.to_thread(self._clear_verified, store, fid)
            log.info("[project_board] %s salvaged the verified candidate → %s (no re-solve)", fid, pr_url)
            return True
        except Exception:  # noqa: BLE001 — ANY doubt/error → today's rebuild-fresh path
            log.warning("[project_board] %s salvage attempt failed — rebuild fresh", fid, exc_info=True)
            return False

    async def _recover(self):
        """On boot, reconcile every ``in_progress`` feature the previous run left
        mid-drive (a drive doesn't survive a restart). ``in_review`` features are NOT
        touched — they have a PR and the webhook/poll resolves them. Also releases the
        previous run's orphaned preflight holds (#186) — see
        ``_recover_preflight_holds``."""
        store = self._store()
        for f in await asyncio.to_thread(store.list_features, state="in_progress"):
            try:
                await self._reconcile_orphan(f["id"])
            except Exception:  # noqa: BLE001 — recovery is best-effort, per feature
                log.warning("[project_board] recovery for %s failed", f["id"], exc_info=True)
        # Store-only helper — run the whole scan+release off the event loop (#258).
        await asyncio.to_thread(self._recover_preflight_holds, store)

    def _recover_preflight_holds(self, store) -> None:
        """Release the PREVIOUS run's preflight holds on boot (#186). `_preflight_held`
        is in-memory and dies with the process, so a restart orphans every card the old
        loop flag_blocked'd for a failed preflight: the cards are blocked (not ready),
        which makes them invisible to `_ready_projects`, and the fresh `_preflight_state`
        is empty — nothing would ever re-smoke their project or clear them. A restart is
        also the moment the environment most plausibly changed, so simply unblock them:
        a still-broken gate re-holds them one tick later (`_maybe_preflight` +
        `_hold_ready_for_preflight` — fail-closed is preserved), a fixed one lets them
        build. Cards blocked for any OTHER reason are never touched."""
        try:
            blocked = store.raw_features_with_comments(states=("blocked",))
        except Exception:  # noqa: BLE001 — a failed scan must not stop the loop from booting
            log.warning("[project_board] boot preflight release: blocked-card scan failed", exc_info=True)
            return
        for feat in blocked:
            fid = feat.get("id")
            if not fid or not _last_block_reason(feat).startswith(PREFLIGHT_BLOCK_PREFIX):
                continue
            try:
                store.clear_blocked(fid)
                log.info(
                    "[project_board] boot: released orphaned preflight hold on %s (re-checked on the first tick)",
                    fid,
                )
            except Exception:  # noqa: BLE001 — best-effort, per feature
                log.warning("[project_board] boot preflight release: clear_blocked failed for %s", fid, exc_info=True)

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
        ``feat-<id>`` worktrees whose feature is gone or already terminal —
        ``done``/``cancelled`` (a missed reap); (c) label terminal features past the
        archive window ``archived``
        (#115) — the board's growth valve; archival only, nothing is ever deleted.
        Best-effort; a per-item failure never stops the sweep or the loop."""
        store = self._store()
        # Publish the board's live projection for the host's <working_state> block (ADR
        # 0079 Observe). Done on the SWEEP cadence, not per tick: it is one extra `br`
        # call per sweep, and the provider that reads it must never touch the store
        # itself — it runs inline on every agent turn. Best-effort, like the rest of the
        # sweep.
        try:
            await asyncio.to_thread(lambda: work_snapshot.publish(store.list_features()))
        except Exception:  # noqa: BLE001 — never let a snapshot refresh stop the sweep
            log.warning("[project_board] work snapshot refresh failed (ignored)", exc_info=True)
        for f in await asyncio.to_thread(store.list_features, state="in_progress"):
            fid = f["id"]
            if fid in self._inflight_files:
                continue  # a live drive owns it
            try:
                log.info("[project_board] sweep: %s in_progress with no live drive", fid)
                await self._reconcile_orphan(fid)
            except Exception:  # noqa: BLE001
                log.warning("[project_board] sweep reconcile for %s failed", fid, exc_info=True)
        # #90: reap orphaned worktrees across EVERY project's checkout, not just the
        # instance default — a multi-repo board holds feat-<id> worktrees under each
        # project's repo, and a worktree resolved in repo A must be reaped in repo A.
        for repo in self._all_repos():
            await self._sweep_worktrees(store, repo)
        # (b2) the blocked lane: self-heal what can be, and TELL THE OPERATOR about what
        # cannot — a blocked card used to leave the queue with only a log line, so
        # dependents sat `ready` waiting on a blocker that would never arrive.
        await self._recover_blocked(store)
        # (c) the archive pass (#115): age done/cancelled features out of the live
        # view so the Done column doesn't bury recent work — a label write only. Runs
        # ONCE per sweep (project-independent — the board db is shared), after the
        # per-repo worktree reap above.
        try:
            archived = await asyncio.to_thread(store.archive_stale, self.archive_after_days)
            if archived:
                log.info(
                    "[project_board] sweep: archived %d terminal feature(s): %s", len(archived), ", ".join(archived)
                )
        except Exception:  # noqa: BLE001
            log.warning("[project_board] sweep archive pass failed", exc_info=True)

    async def _recover_blocked(self, store) -> None:
        """The blocked lane's self-heal + escalation pass.

        Before this, every block was terminal in practice: the card left the queue, the
        only record was a WARNING in a log nobody reads, and the board said nothing. A
        transient coder timeout and a bad credential died in exactly the same silent way,
        and dependent cards sat `ready` forever waiting on a blocker that would never
        arrive. That is the failure mode this pass exists to end.

        Two outcomes, never zero:

        * the block classifies as self-healing (`rate-limit` / `transient` /
          `merge-conflict`) and the card has retries left → clear it, requeue it, spend
          one `unblock-retry`; the next tick re-dispatches it.
        * anything else — `auth`, `terminal`, an unclassified block, or a card that has
          spent its retries → the OPERATOR is told, once, naming the card and the actual
          reason. It stays blocked; a human decides.

        Best-effort per card, exactly like the rest of the sweep: one card that fails to
        recover must never stop the pass or the loop."""
        try:
            blocked = await asyncio.to_thread(store.list_features, state="blocked")
        except Exception:  # noqa: BLE001
            log.warning("[project_board] blocked sweep: could not list blocked features", exc_info=True)
            return
        for f in blocked:
            fid = f["id"]
            try:
                cls = str(f.get("blocked_class") or "").strip()
                # The reason rides a COMMENT, and `br list` carries none — a list row
                # always projects "". Escalating "no reason recorded" tells the operator
                # nothing and makes them go digging, which is the thing this alert exists
                # to prevent, so the one card being escalated is re-read through
                # get_feature (`br show`). Only on the escalation path: rare, once per
                # card, never a per-row probe across the whole blocked lane.
                reason = str(f.get("blocked_reason") or "").strip()
                spent = await self._budget_get(store, fid, "unblock-retry", f)
                if cls in _SELF_HEALING_BLOCKS and spent < _UNBLOCK_RETRY_MAX:
                    await self._budget_set(store, fid, "unblock-retry", spent + 1)
                    await asyncio.to_thread(store.clear_blocked, fid)
                    await asyncio.to_thread(store.requeue, fid)
                    log.info(
                        "[project_board] blocked sweep: %s auto-unblocked (%s, retry %d/%d): %s",
                        fid,
                        cls,
                        spent + 1,
                        _UNBLOCK_RETRY_MAX,
                        reason[:120],
                    )
                    continue
                why = (
                    f"{cls or 'unclassified'} block"
                    if spent < _UNBLOCK_RETRY_MAX
                    else f"{cls} block, {spent} auto-retr{'y' if spent == 1 else 'ies'} spent"
                )
                if not reason:
                    try:
                        full = await asyncio.to_thread(store.get_feature, fid)
                        reason = str((full or {}).get("blocked_reason") or "").strip()
                    except Exception:  # noqa: BLE001 — the alert matters more than its detail
                        pass
                title = str(f.get("title") or "").strip()
                self._notify_operator(
                    fid,
                    f"Board card {fid} is blocked and will not clear itself ({why}): "
                    f"{reason or 'no reason recorded'}" + (f" — {title}" if title else ""),
                    # The recovery CYCLE is part of the incident's identity (#346 r7): a
                    # card that auto-healed, rebuilt and failed the SAME way again is a new
                    # failed cycle and IS news — the self-heal did not work. Keying on
                    # class+reason alone suppressed exactly that for the whole window.
                    # `spent` is the unblock-retry budget the self-heal already tracks, so
                    # this costs no new state: it increments on every auto-unblock and is
                    # therefore different on each side of a recovery.
                    incident=f"{cls}|{reason}|{spent}",
                )
            except Exception:  # noqa: BLE001
                log.warning("[project_board] blocked sweep for %s failed", fid, exc_info=True)

    async def _sweep_worktrees(self, store, repo: str) -> None:
        """Reap orphaned ``feat-<id>`` worktrees under one project's checkout (#90) —
        the per-repo half of the health sweep, factored out so it runs once per project
        repo. Best-effort; a per-item failure never stops the sweep."""
        for wtid in worktree.list_feature_worktrees(repo, self.root):
            # A `.gN`/`.cN` candidate worktree is not a feature id (bd-1cp.g1) — its
            # board state lives on the PARENT feature, so resolve through that (#91):
            # skip while the parent's drive is live, reap when the parent is gone or
            # terminal (done/cancelled — the terminal-edge reap's crash backstop, #109).
            # The old raw-id `get_feature` lookup failed every sweep and just warned
            # forever without ever reaping the candidate.
            fid = worktree.parent_feature_id(wtid)
            if fid in self._inflight_files:
                continue  # a live drive owns this worktree (or its candidates)
            try:
                f = await asyncio.to_thread(store.get_feature, fid)
                if f is None or f["board_state"] in ("done", "cancelled"):
                    reaped = await worktree.reap_feature_worktree(repo, self.root, wtid)
                    if reaped:
                        self._reap_failures.pop(wtid, None)
                        log.info("[project_board] sweep: reaped orphaned worktree feat-%s", wtid)
                    else:
                        n = self._reap_failures.get(wtid, 0) + 1
                        self._reap_failures[wtid] = n
                        if n <= _REAP_WARN_CAP:
                            log.warning(
                                "[project_board] sweep: could not reap orphaned worktree feat-%s (attempt %d)",
                                wtid,
                                n,
                            )
                        else:
                            log.debug(
                                "[project_board] sweep: could not reap orphaned worktree feat-%s (attempt %d)",
                                wtid,
                                n,
                            )
            except Exception:  # noqa: BLE001
                log.warning("[project_board] sweep reap for %s failed", wtid, exc_info=True)

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
        # #196: blocked cards can carry a PR too (review-verify blocks, closed-PR triage,
        # manual flags) — a merged PR is ground truth for them exactly as for in_review,
        # and scanning only in_review left merged-but-blocked cards stuck forever. They
        # take ONLY the MERGED edge below: CLOSED would rewrite their blocked reason, and
        # the OPEN-branch gates (rebase/CI/review) must not run against held work.
        in_review = await asyncio.to_thread(store.list_features, state="in_review")
        blocked = await asyncio.to_thread(store.list_features, state="blocked")
        for f in [*in_review, *blocked]:
            fid = f["id"]
            pr_url = f.get("pr_url")
            if not pr_url:
                continue
            # #90: reconcile each PR against ITS project's checkout, not the board default.
            repo = self._repo_for(f)
            try:
                state = await worktree.pr_state(pr_url, cwd=repo)
                if f.get("board_state") == "blocked" and state != "MERGED":
                    continue
                if state == "MERGED":
                    if await asyncio.to_thread(store.record_merge, pr_url=pr_url):
                        await worktree.reap_feature_worktree(repo, self.root, fid)
                        self._ci_feedback.pop(fid, None)
                        self._review_prior.pop(fid, None)
                        # Merge edge: EVERY fix budget resets — cache and the bead's
                        # `budget:` labels together (#259) — so a reopened/requeued
                        # card starts with full budgets, exactly as pre-persistence.
                        await self._budget_reset(store, fid)
                        # A merge with the gate still unhappy is a human override —
                        # reality wins, but it must be visible, not silent.
                        if self.review_gate and LABEL_CHANGES_REQUESTED in (f.get("labels") or []):
                            log.warning(
                                "[project_board] %s merged with review changes-requested still set "
                                "(human override): %s",
                                fid,
                                pr_url,
                            )
                        log.info("[project_board] reconcile → done: %s (%s)", fid, pr_url)
                elif state == "CLOSED":
                    await asyncio.to_thread(
                        store.flag_blocked, fid, f"PR closed without merging — needs triage: {pr_url}"
                    )
                    await worktree.reap_feature_worktree(repo, self.root, fid)
                    self._ci_feedback.pop(fid, None)
                    self._review_prior.pop(fid, None)
                    # Closed-unmerged is the other terminal edge: same full reset,
                    # so a post-triage requeue starts with fresh budgets (#259).
                    await self._budget_reset(store, fid)
                    log.info("[project_board] reconcile → blocked (PR closed): %s (%s)", fid, pr_url)
                elif state == "OPEN":
                    # Keep a stale/conflicting PR mergeable BEFORE the CI reconcile: a
                    # sibling merge re-stales the others off the shared base, and a rebase
                    # force-pushes + re-runs CI — so checking CI on the stale head first
                    # would just be thrown away.
                    if self.auto_rebase and await self._maybe_rebase(store, f, pr_url, repo):
                        continue
                    # The VERDICT half of the rebase edge (#131): a sibling merge
                    # moved base under this still-CLEAN PR (no conflict, so the
                    # rebase above left it alone) — the state that will actually
                    # LAND was never gated. Re-run the gate on the merged state
                    # (no push) and stamp the sha; only a red gate blocks.
                    if self.auto_rebase and await self._verify_merged_state(store, f, pr_url, repo):
                        continue  # blocked on a red merged-state gate → nothing further this pass
                    if self.ci_poll:
                        await self._reconcile_ci(store, fid, pr_url, repo, feature=f)
                    # The re-arm half of the review gate (#328): a direct/human push to
                    # the branch of an in_review PR sitting in `changes-requested` moved
                    # the head out from under a verdict the gate — which re-runs only on
                    # `review-pending` — will never revisit. Left alone, the stale
                    # rejection pins a dead head forever (or, labels cleared by hand, an
                    # un-reviewed head merges). Re-arm the gate for the new head ONLY on a
                    # demonstrable reviewed-head↔live-head mismatch; the resume edge below
                    # then runs the fresh review. Fail-closed cases leave `changes-requested`
                    # in place, so the merge edge still can't touch an un-reviewed head. The
                    # board_state re-read guards against the CI reconcile having just
                    # requeued the feature out of in_review this same pass.
                    if (
                        self.review_gate
                        and LABEL_CHANGES_REQUESTED in (f.get("labels") or [])
                        and (await asyncio.to_thread(store.get_feature, fid) or {}).get("board_state") == "in_review"
                        and await self._rearm_review_for_new_head(store, f, pr_url, repo)
                    ):
                        f = await asyncio.to_thread(store.get_feature, fid) or f
                    # The INBOUND half of the review gate (#323): a trusted, promoted QA
                    # PASS for the PR's CURRENT head repairs a stale changes-requested or an
                    # absent local review verdict to review-clean, so the ordinary merge
                    # gates can proceed — the counterpart to #347's head-pinned publish.
                    # Runs AFTER #328 (a genuine head move takes the fresh-internal-review
                    # path, never this trust path — #328 flipped it to review-pending, which
                    # this edge then skips) and BEFORE #340 (a proven current-head PASS
                    # supersedes resuming an internal fix round; on a promotion the refreshed
                    # snapshot drops changes-requested so #340 short-circuits this pass). Fails
                    # closed on anything unproven, so the un-promoted card still can't merge.
                    # The board_state re-read guards against #328 / the CI reconcile having
                    # just moved the card out of in_review this same pass.
                    if (
                        self.review_gate
                        and (await asyncio.to_thread(store.get_feature, fid) or {}).get("board_state") == "in_review"
                        and await self._reconcile_trusted_qa_pass(store, f, pr_url, repo)
                    ):
                        f = await asyncio.to_thread(store.get_feature, fid) or f
                    # The RECOVERY half of the review gate (#340): a shutdown/restart can
                    # abort a fix round mid-transition and leave the card in_review +
                    # changes-requested with the review gate's requeue never landed — no
                    # live drive survives, the gate re-runs only on review-pending, and
                    # auto-merge needs review-clean, so the card sits in_review forever
                    # while merged-state verify churns. DISTINCT from the #328 re-arm above:
                    # the trigger is a dead drive, not a moved head — #328 ran FIRST, so a
                    # head that actually moved is already re-armed off changes-requested by
                    # here (a genuine external push takes that path, never this one). Requeue
                    # to ready to resume the SAME PR's fix round; the re-read guards against
                    # #328 / the CI reconcile having just moved the card this same pass.
                    if (
                        self.review_gate
                        and LABEL_CHANGES_REQUESTED in (f.get("labels") or [])
                        and (await asyncio.to_thread(store.get_feature, fid) or {}).get("board_state") == "in_review"
                        and await self._requeue_stranded_review_fix(store, f, pr_url, repo)
                    ):
                        continue  # requeued to ready — the next dispatch resumes the fix; nothing else this pass
                    # The merge-edge half of the review gate (M5): an in_review PR still
                    # marked review-pending had its gate interrupted (host restart, dead
                    # workflow run) — finish it here so the gate can't silently lapse into
                    # advisory. Skip when the CI reconcile just requeued the feature. A
                    # gate that is merely still RUNNING (the drive's call, minutes long)
                    # is not interrupted — _review_gate's in-flight guard makes this a
                    # no-op for it (#205), so this edge never double-reviews a head.
                    if (
                        self.review_gate
                        and LABEL_REVIEW_PENDING in (f.get("labels") or [])
                        and (await asyncio.to_thread(store.get_feature, fid) or {}).get("board_state") == "in_review"
                    ):
                        await self._review_gate(store, fid, pr_url, repo)
                    # The MERGE edge — last, so it sees this pass's rebase / verify /
                    # CI / review outcomes, and re-reads the feature rather than
                    # trusting the snapshot those gates may have changed.
                    if self.auto_merge:
                        await self._maybe_auto_merge(store, fid, pr_url, repo)
            except Exception:  # noqa: BLE001 — a reconcile error must never kill the loop
                log.warning("[project_board] reconcile for %s failed", fid, exc_info=True)

    async def _auto_merge_blockers(self, store, feature: dict, pr_url: str, repo: str) -> list[str]:
        """Why this in_review PR must NOT be auto-merged right now — empty means every
        gate the loop owns is green AND current. Each reason is a short, greppable
        phrase; the caller logs them at debug so a parked PR is explainable, not
        mysterious. Order: the cheap board reads first, GitHub last."""
        fid = feature["id"]
        labels = set(feature.get("labels") or [])
        # The board-side half is shared with the PM-facing `next_action` (#208,
        # store.merge_posture) — one decoding of the review sub-state labels. No head is
        # passed here: this runs on every posture evaluation and must stay a pure label
        # decode with no GitHub read. The head-pin check (#323) belongs on the merge edge
        # itself, immediately before the merge, where one read is worth it — see
        # `_maybe_auto_merge`.
        why: list[str] = list(
            _loop.merge_posture(feature, auto_merge=self.auto_merge, review_gate=self.review_gate)["blockers"]
        )
        if await self._budget_get(store, fid, "auto-merge", feature) >= self.auto_merge_max:
            why.append("merge attempts exhausted")
        if why:
            return why
        # Verdict currency (#131): the merged-state gate must have run against the
        # base that will actually land. Stale = unverified, so hold (never block).
        # But ONLY when there is a local gate to have verified the merged state WITH:
        # `_verify_merged_state` returns before stamping when `local_gate_cmd` is
        # blank (the default), so demanding the stamp regardless made auto_merge
        # unreachable on every board without a local gate — `merged-verified stamp
        # (none)` forever, at debug level, while review-clean + CI-green cards sat
        # in_review (#209). Without a local gate CI + GitHub's CLEAN are the gates,
        # as the verify edge's own docstring says.
        if self.auto_rebase and self._local_gate_cmd_for(feature):
            base = self._base_branch_for(feature)
            head = await worktree.origin_head_sha(repo, base)
            if not head:
                return ["base sha unavailable"]
            stamped = next(
                (
                    lb[len(LABEL_MERGED_VERIFIED_PREFIX) :]
                    for lb in labels
                    if lb.startswith(LABEL_MERGED_VERIFIED_PREFIX)
                ),
                "",
            )
            if not stamped or not head.startswith(stamped):
                return [f"merged-verified stamp {stamped or '(none)'} ≠ {base}@{head[:_MERGED_VERIFIED_SHA_LEN]}"]
        info = await worktree.pr_merge_info(pr_url, cwd=repo)
        mss = info.get("mergeStateStatus") or ""
        if info.get("isDraft") is True:
            # #207: GitHub reports CLEAN for a draft whose checks pass, so the status
            # alone never says "draft" — and `gh pr merge` refuses a draft, which used
            # to burn an auto_merge_max attempt per poll and park the card on "merge
            # attempts exhausted" with no hint. A named blocker instead; the fix is
            # one `gh pr ready` (open_pr already does it for an adopted coder draft).
            return [
                f"draft (PR is a draft — run `gh pr ready {pr_url}`; the loop never spends a merge attempt on a draft)"
            ]
        if mss != "CLEAN":
            # BLOCKED = required checks not satisfied; UNSTABLE = a non-required check
            # failing; BEHIND/DIRTY = the rebase edge's job; UNKNOWN = GitHub still
            # computing; "" = gh failed. None of them is a merge.
            return [f"github mergeStateStatus={mss or 'unavailable'}"]
        return []

    async def _maybe_auto_merge(self, store, fid: str, pr_url: str, repo: str) -> bool:
        """Merge an in_review PR once every gate the loop owns is green and current
        (see ``_auto_merge_blockers``). Returns True if it merged. The board flips to
        done on the next reconcile pass (the existing MERGED edge — one Done path,
        idempotent, webhook-compatible). A refusal is retried next pass up to
        ``auto_merge_max`` times, then recorded on the bead and left for a human —
        never a block: the work is good, only the merge didn't land."""
        feature = await asyncio.to_thread(store.get_feature, fid)
        if feature is None:  # card deleted between the reconcile snapshot and this re-read
            log.debug("[project_board] %s vanished before auto-merge — nothing to merge", fid)
            return False
        why = await self._auto_merge_blockers(store, feature, pr_url, repo)
        if why:
            # Store-only bookkeeping (a bead comment) — off the event loop (#258).
            await asyncio.to_thread(self._note_draft_hold, store, fid, pr_url, why)
            log.debug("[project_board] %s not auto-merging: %s", fid, "; ".join(why))
            return False
        self._draft_noted.discard(fid)
        # LAST gate before the merge (#323): a clean verdict is only a verdict about the
        # code it READ, so it is written pinned to that head and must still match the live
        # one. Two layers guard the merge. (1) The stale-pin check below is a belt: a push
        # landing before it leaves the pin stale on every later pass too, so the check can
        # only ever close the gate, never open it — the pin's WRITE never needed to win a
        # race (earlier cuts that re-read-and-undid the write were correctly rejected). (2)
        # But a push can still land in the tiny window AFTER this check and BEFORE the merge
        # call; the check alone can't cover that, so the verified head is carried into
        # `merge_pr` as `expected_head` and GitHub refuses the merge atomically
        # (`--match-head-commit`) if the head moved. Belt and suspenders — the merge itself,
        # not just the board state, is constrained to the reviewed head.
        merge_head = ""  # the verified reviewed head to pin the merge to (empty = grandfathered/no gate)
        if self.review_gate:
            pinned = next(
                (
                    str(x)[len(store_mod.LABEL_REVIEW_CLEAN_SHA_PREFIX) :]
                    for x in (feature.get("labels") or [])
                    if str(x).startswith(store_mod.LABEL_REVIEW_CLEAN_SHA_PREFIX)
                ),
                "",
            )
            if pinned:  # unpinned verdicts are grandfathered — see merge_posture
                live = await worktree.pr_head_sha(pr_url, cwd=repo)
                # The pin is stored SHORT (beads' 50-char label cap), so compare the live
                # head's matching prefix — not the full sha, which could never equal it.
                if not live or str(live)[: store_mod.SHORT_SHA_LEN] != pinned:
                    log.info(
                        "[project_board] %s not merging: the review-clean verdict is for %s but the head is %s "
                        "— the push that moved it is unreviewed; re-arming review: %s",
                        fid,
                        pinned,
                        (live or "unreadable")[: store_mod.SHORT_SHA_LEN],
                        pr_url,
                    )
                    await asyncio.to_thread(store.set_review_substate, fid, LABEL_REVIEW_PENDING)
                    return False
                # The read above matched, but a push can still land in the window before the
                # merge call — so carry the verified head into the merge and let GitHub refuse
                # atomically (``--match-head-commit``) if the head moved. This closes the
                # residual TOCTOU: without it, a commit pushed after this comparison would be
                # the head ``gh pr merge`` lands, merging code the gate never reviewed.
                merge_head = live
        ok, detail = await worktree.merge_pr(pr_url, method=self.merge_method, cwd=repo, expected_head=merge_head)
        if not ok:
            # gh's exit code is not the verdict — the merge may have landed and a
            # later step failed, or a concurrent merge (webhook, human) beat us. The
            # PR's real state is.
            ok = (await worktree.pr_state(pr_url, cwd=repo)) == "MERGED"
        if ok:
            await self._budget_reset(store, fid, "auto-merge")
            log.info(
                "[project_board] %s auto-merged (%s, all gates green + current): %s", fid, self.merge_method, pr_url
            )
            # Remote-branch cleanup, best-effort; the worktree (which still holds the
            # local branch) is reaped when the reconcile reads MERGED.
            branch = worktree.branch_name(fid, (feature or {}).get("title") or "")
            if not await worktree.delete_remote_branch(repo, branch):
                log.info("[project_board] %s remote branch %s not deleted (already gone or protected)", fid, branch)
            return True
        n = await self._budget_get(store, fid, "auto-merge", feature) + 1
        await self._budget_set(store, fid, "auto-merge", n)
        if n >= self.auto_merge_max:
            try:
                await asyncio.to_thread(
                    store.comment,
                    fid,
                    f"auto-merge gave up after {n} attempt(s) — every gate is green but GitHub refused the "
                    f"merge; needs a human: {pr_url}\n{detail}",
                )
            except Exception:  # noqa: BLE001 — bookkeeping must not break the reconcile
                log.warning("[project_board] %s auto-merge give-up comment failed", fid, exc_info=True)
            log.warning("[project_board] %s auto-merge gave up after %d attempt(s): %s", fid, n, detail)
        else:
            log.warning(
                "[project_board] %s auto-merge refused (attempt %d/%d): %s", fid, n, self.auto_merge_max, detail
            )
        return False

    def _note_draft_hold(self, store, fid: str, pr_url: str, why: list[str]) -> None:
        """ONE bead comment the first time the auto-merge edge holds on a draft (#207):
        `open_pr`'s `gh pr ready` can fail (a fork PR, no write on base) or the operator
        may have drafted the PR — either way the hold was only a DEBUG line, invisible
        on the card. Mirrors the give-up comment; a comment failure never breaks the
        reconcile. The mark clears when the PR is seen non-draft, so a later re-draft
        is noted again (once)."""
        if not any(w.startswith("draft") for w in why):
            self._draft_noted.discard(fid)
            return
        if fid in self._draft_noted:
            return
        self._draft_noted.add(fid)
        try:
            store.comment(
                fid,
                f"auto-merge is holding: the PR is a draft — run `gh pr ready {pr_url}` (or leave it drafted "
                f"as a hold); the loop never spends a merge attempt on a draft",
            )
        except Exception:  # noqa: BLE001 — bookkeeping must not break the reconcile
            log.warning("[project_board] %s draft-hold comment failed", fid, exc_info=True)
        log.info("[project_board] %s auto-merge holding on a draft PR: %s", fid, pr_url)

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
        base = self._base_branch_for(feature)
        branch = worktree.branch_name(fid, feature.get("title") or "")
        outcome, detail = await worktree.rebase_onto_base(repo, branch, base, root=self.root)
        if outcome == "clean":
            log.info("[project_board] %s auto-rebased onto %s (was %s) — force-pushed", fid, base, mss)
            return True
        if outcome == "error":
            log.warning(
                "[project_board] %s auto-rebase hit infra trouble (%s) — next poll retries: %s", fid, mss, detail
            )
            return False  # transient — don't burn the coder budget on an infra blip
        # outcome == "conflict": a real merge conflict only the coder can resolve.
        n = await self._budget_get(store, fid, "rebase", feature)
        if n >= self.rebase_fix_max:
            await asyncio.to_thread(
                store.flag_blocked,
                fid,
                f"rebase conflict with {base} after {n} attempt(s) — needs a manual rebase: {pr_url}",
            )
            await worktree.reap_feature_worktree(repo, self.root, fid)
            log.warning("[project_board] %s blocked (rebase conflict, %d attempt(s)): %s", fid, n, detail)
            return True
        await self._budget_set(store, fid, "rebase", n + 1)
        self._ci_prior_diff.pop(fid, None)
        self._ci_feedback[fid] = (
            f"Your branch now CONFLICTS with `{base}` — a sibling change merged into the same "
            f"file(s): {detail}. Re-apply your change onto the latest `{base}` and resolve the "
            "conflict, keeping BOTH sides' intent. Then stop."
        )
        await asyncio.to_thread(store.requeue, fid)
        log.info(
            "[project_board] %s rebase conflict — re-dispatch %d/%d to resolve (%s): %s",
            fid,
            n + 1,
            self.rebase_fix_max,
            mss,
            detail,
        )
        return True

    async def _verify_merged_state(self, store, feature: dict, pr_url: str, repo: str) -> bool:
        """Re-verify an ``in_review`` PR's VERDICT after its base moved (#131).

        The rebase above only acts on BEHIND/DIRTY — but without strict base-freshness
        a PR whose base advanced still reads CLEAN, merges clean, and nobody ever ran
        the gate on the state that will actually land (five straight PRs, verified by
        hand). So when current ``origin/<base>`` ≠ the ``merged-verified:<sha>`` stamp
        on the bead (a missing stamp counts as moved — the first poll verifies and
        stamps), build the merged state (branch tip + that base commit) in a throwaway
        worktree, run ``local_gate_cmd`` there, and stamp the SHORT base sha the verdict
        was verified against — the ONE field an adjudicator checks for verdict currency
        (short because ``merged-verified:`` + a full 40-char sha = 56 chars blew beads'
        50-char label cap, so #132's stamp never actually landed until #135). The stamp
        is best-effort bookkeeping: a ``BoardError`` writing it is caught and logged so
        the required CI/merge reconciliation is never skipped, and a write that didn't
        land never spends the re-verify budget. Same principle as the completion gate
        (#113): verify the property, don't trust the report.

        NON-BLOCKING by default: a moved base is unverified, not broken. A green gate
        (or one that can't run — the ``_run_local_gate`` fail-open contract; CI is
        still the real gate) just refreshes the stamp and the card stays in review;
        only a CLEAN gate FAILURE on the merged state blocks. Bounded by
        ``merged_verify_max`` (0 = unlimited): once spent, re-verification stops and
        the stale stamp stays visible to the adjudicator rather than the loop burning
        a gate run every poll forever — with ``auto_merge`` on that hold is the merge
        edge's, so exhaustion logs a WARNING naming the remedy. A merge conflict is the
        DIRTY/rebase edge's job and an infra error retries next poll — neither burns
        budget nor stamps. Returns True only when it BLOCKED the card (the caller
        skips the rest of this pass)."""
        fid = feature["id"]
        if not self._local_gate_cmd_for(feature):
            return False  # no gate → nothing to verify the merged state WITH
        base = self._base_branch_for(feature)
        base_sha = await worktree.origin_head_sha(repo, base)
        if not base_sha:
            return False  # transient git/infra hiccup — next poll retries
        stamped = next(
            (
                l[len(LABEL_MERGED_VERIFIED_PREFIX) :]
                for l in feature.get("labels") or []
                if l.startswith(LABEL_MERGED_VERIFIED_PREFIX)
            ),
            "",
        )
        if stamped == base_sha[:_MERGED_VERIFIED_SHA_LEN]:
            return False  # the verdict is current — base hasn't moved since it was stamped
        n = await self._budget_get(store, fid, "merged-verify", feature)
        if self.merged_verify_max and n >= self.merged_verify_max:
            if n == self.merged_verify_max:  # arm the exhaustion sentinel once, then stay quiet
                # The ONE-TIME sentinel: bump the persisted budget to `max+1`. Beyond
                # logging once, this is the loop SUPPLYING the exhaustion fact to the
                # board projection (ADR 0326): `budget:merged-verify:<max+1>` is a value
                # a gate-run spend can never reach (the `n >= max` guard returns before
                # the gate runs), so `budget > merged_verify_max` uniquely means "base
                # moved while exhausted" — store.merge_posture reads it back and an
                # auto_merge card reads `auto-merge held: merged-verify budget exhausted`
                # instead of the `auto-merge pending` lie. NOT a gate-run spend (the gate
                # never ran this pass) — the budget accounting for actual verifications is
                # untouched below. The write is a COMPARE-AND-SET under the reset lock
                # (in a worker thread): an operator budget reset landing between the read
                # above and this write pins the count to 0 under the same lock, so the CAS
                # reads 0 (≠ cap) and SKIPS — the reset's fresh window is never silently
                # re-held (`armed` is False and the next poll re-verifies).
                armed = await asyncio.to_thread(self._arm_merged_verify_exhaustion, store, fid)
                if armed and self.auto_merge:
                    # The loop IS the adjudicator here, and a stale stamp is a hard hold
                    # on the merge edge — say so, and say what unsticks it.
                    log.warning(
                        "[project_board] %s merged-verify budget (%d) spent with auto_merge on — the card "
                        "will NOT auto-merge until base stops moving or merged_verify_max is raised "
                        "(0 = unlimited); label it merge-hold to hand it to a human: %s",
                        fid,
                        self.merged_verify_max,
                        pr_url,
                    )
                # Only claim a "stale stamp" when one actually exists. With the budget
                # exhausted and NO stamp ever written (e.g. rebase_fix_max=0, or the
                # pre-#135 world where every write failed), the adjudicator sees an
                # UNVERIFIED merged state — reporting a stale verdict that isn't there
                # is the same lie #132 was built to prevent.
                if armed and stamped:
                    log.info(
                        "[project_board] %s base moved again but the merged-verify budget (%d) is spent — "
                        "leaving the stale merged-verified stamp for the adjudicator: %s",
                        fid,
                        self.merged_verify_max,
                        pr_url,
                    )
                elif armed:
                    log.info(
                        "[project_board] %s base moved but the merged-verify budget (%d) is spent and no "
                        "merged-verified stamp was ever written — the merged state stays unverified: %s",
                        fid,
                        self.merged_verify_max,
                        pr_url,
                    )
            return False
        branch = worktree.branch_name(fid, feature.get("title") or "")
        outcome, detail = await worktree.merged_state_worktree(repo, branch, base_sha, root=self.root)
        if outcome == "error":
            log.warning("[project_board] %s merged-state verify hit infra trouble — next poll retries: %s", fid, detail)
            return False
        if outcome == "conflict":
            # A real conflict is the DIRTY/rebase edge's job (pr_merge_state reads
            # DIRTY once GitHub recomputes) — not a verdict, not a reason to block.
            log.info(
                "[project_board] %s merged-state verify: merge conflicts (%s) — leaving to the rebase edge", fid, detail
            )
            return False
        try:
            failure = await self._run_local_gate(detail, feature)
        finally:
            await worktree.remove_worktree(repo, detail)
        short = base_sha[:_MERGED_VERIFIED_SHA_LEN]
        if failure is None:
            # Green: the verdict still holds on the merged state. Stamp the SHORT sha,
            # then — and only then — spend a budget unit. The stamp is optional
            # bookkeeping: a BoardError writing it must NOT abort the reconcile pass
            # (the merge/CI edges below still have to run) nor burn the re-verify budget
            # on a write that didn't land — the next poll simply re-verifies (#135).
            try:
                await asyncio.to_thread(store.record_merged_verified, fid, short)
            except BoardError:
                log.warning(
                    "[project_board] %s merged-state gate green but stamping the verified sha failed — "
                    "reconcile continues, next poll re-verifies: %s",
                    fid,
                    pr_url,
                    exc_info=True,
                )
                return False
            await self._budget_set(store, fid, "merged-verify", n + 1)
            log.info(
                "[project_board] %s merged-state gate green — verdict re-verified against %s@%s",
                fid,
                base,
                short,
            )
            return False
        await self._budget_set(store, fid, "merged-verify", n + 1)
        await asyncio.to_thread(
            store.flag_blocked,
            fid,
            f"gate FAILED on the merged state (branch + {base}@{short}) — the PR merges clean "
            f"but the RESULT is broken; needs triage: {pr_url}\n{failure}",
        )
        await worktree.reap_feature_worktree(repo, self.root, fid)
        log.warning("[project_board] %s blocked (merged-state gate failed against %s@%s)", fid, base, short)
        return True

    async def _reconcile_ci(self, store, fid: str, pr_url: str, repo: str, feature: dict | None = None):
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

        Two guards keep this from bouncing a PR it shouldn't (bd-1zp):
        - **Merged/closed guard** — ``_reconcile_prs`` read the PR state at the top of
          the poll, but the rebase/`gh` round-trips since then leave a window in which
          the PR could have merged or closed. Re-read the state right here and bail on
          anything that is no longer ``OPEN`` — a CI fix must NEVER dispatch against a
          PR that has already left review.
        - **Advisory filter** — ``pr_ci_status`` only reports ``failing`` when a
          *blocking* check (a required check or a GitHub Actions run) is red; a red
          third-party advisory status (CodeRabbit, a coverage bot) reads ``passing`` and
          never triggers a bounce.

        Passing/pending/no-checks left in review (the merge edge resolves it)."""
        if await worktree.pr_state(pr_url, cwd=repo) != "OPEN":
            return  # merged/closed since the poll started -> never dispatch a CI fix
        status, summary = await worktree.pr_ci_status(pr_url, cwd=repo)
        if status != "failing":
            return
        # Carry the lesson: the CI error + the diff that failed it (best-effort).
        self._ci_feedback[fid] = summary
        self._ci_prior_diff[fid] = await worktree.pr_diff(pr_url, cwd=repo)

        async def _block(reason: str):
            await asyncio.to_thread(store.flag_blocked, fid, reason)
            self._ci_feedback.pop(fid, None)
            self._ci_prior_diff.pop(fid, None)
            await self._budget_reset(store, fid, "ci-fix")

        # Same-tier CI-fix budget FIRST (both ladder and single-coder): a red check
        # is usually a fixable nit the current tier can correct once it sees the
        # error — don't burn a stronger model on a one-line lint fix. The CI error +
        # prior diff are already injected above, so the re-dispatch improves on the
        # last try rather than repeating it.
        attempts = await self._budget_get(store, fid, "ci-fix", feature)
        if attempts < self.ci_fix_max:
            await self._budget_set(store, fid, "ci-fix", attempts + 1)
            await asyncio.to_thread(store.requeue, fid)
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
            nxt = await asyncio.to_thread(store.escalate, fid, f"CI failed: {_ci_failure_reason(summary)}")
            if not nxt:
                await _block(
                    f"CI failing at the top model tier after {attempts} same-tier fix(es) — needs triage: {pr_url}"
                )
                await worktree.reap_feature_worktree(repo, self.root, fid)
                log.warning("[project_board] reconcile → blocked (CI fails at top tier): %s", fid)
                return
            await self._budget_reset(store, fid, "ci-fix")  # fresh same-tier budget at the new rung
            await asyncio.to_thread(store.requeue, fid)
            log.info("[project_board] reconcile → escalate to %s + re-dispatch (CI failed): %s", nxt, fid)
            return

        await _block(f"CI still failing after {attempts} fix attempt(s) — needs triage: {pr_url}")
        await worktree.reap_feature_worktree(repo, self.root, fid)
        log.warning("[project_board] reconcile → blocked (CI fails, %d attempt(s) exhausted): %s", attempts, fid)

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

    # ── stale-review re-arm on an external head push (#328) ───────────────────
    async def _rearm_review_for_new_head(self, store, feature: dict, pr_url: str, repo: str) -> bool:
        """Re-arm the review gate when a direct/human push moved the PR head out from
        under an active ``changes-requested`` verdict (#328).

        The gate normally re-runs only on ``review-pending``, so a push to a board PR
        sitting in ``changes-requested`` leaves the rejection pinned to a dead head —
        blocking the card forever, or (labels cleared by hand) merging an un-reviewed
        head. This compares the LIVE PR head against the ``reviewed-head:<sha>`` the
        verdict was stamped for and, ONLY on a demonstrable mismatch, invalidates the
        stale disposition by swapping ``changes-requested`` → ``review-pending`` so the
        established gate runs one fresh normal review for the new head.

        Recorded SHA identity, never a timestamp or the label's presence: an UNCHANGED
        rejected head stays rejected (return False, no re-arm). FAIL CLOSED — leave the
        blocking ``changes-requested`` in place so the card cannot auto-merge — whenever
        identity is unreadable, absent, or ambiguous: not a ``changes-requested`` card,
        no live head (a gh hiccup), no stamp, an empty stamp, or MORE THAN ONE stamp.
        Never touches the review-fix / review-run budgets (the re-armed gate spends them
        exactly as any review does) and never erases the findings history (it lives in
        the bead comments the gate wrote). Returns True only when it re-armed — the
        caller refreshes its snapshot so the review-pending resume edge picks the gate
        up this same pass; the in-flight guard in ``_review_gate`` keeps concurrent
        reconcile ticks from starting a second review for the new head."""
        fid = feature["id"]
        labels = feature.get("labels") or []
        if LABEL_CHANGES_REQUESTED not in labels:
            return False  # only a blocking verdict can go stale
        stamps = [l[len(LABEL_REVIEWED_HEAD_PREFIX) :] for l in labels if l.startswith(LABEL_REVIEWED_HEAD_PREFIX)]
        if len(stamps) != 1 or not stamps[0]:
            # Absent or ambiguous verdict identity → fail closed: the rejection stands,
            # the card can't merge. Cannot re-arm what we can't prove is stale.
            return False
        stamped = stamps[0]
        head = await worktree.pr_head_sha(pr_url, cwd=repo)
        if not head:
            return False  # unreadable live head → fail closed; the next poll retries
        short = head[:_REVIEWED_HEAD_SHA_LEN]
        if short == stamped:
            return False  # head unchanged since the verdict — exactly-once holds, still rejected
        await asyncio.to_thread(
            store.set_review_substate,
            fid,
            LABEL_REVIEW_PENDING,
            note=(
                f"review re-armed (#328): the PR head moved to {short} (the changes-requested "
                f"verdict was for {stamped}) — an external push invalidated that verdict; running "
                "a fresh review for the new head"
            ),
        )
        log.info(
            "[project_board] %s external push moved head %s→%s under changes-requested — re-armed the review gate: %s",
            fid,
            stamped,
            short,
            pr_url,
        )
        return True

    # ── recover a shutdown-stranded review fix round (#340) ───────────────────
    async def _requeue_stranded_review_fix(self, store, feature: dict, pr_url: str, repo: str) -> bool:
        """Requeue an in_review ``changes-requested`` card whose fix round/drive no longer
        exists — the shutdown/restart sibling of the #328 re-arm (#340).

        The review gate marks ``changes-requested`` and ``requeue``s a card for a same-PR
        fix round. If a shutdown/restart aborts that fix drive mid-transition (or the
        gate's own ``set_review_substate`` → ``requeue`` sequence), the requeue never
        lands and the card is stranded ``in_review`` + ``changes-requested``: no live
        drive survives, ``_review_gate`` re-runs only on ``review-pending``, and auto-merge
        requires ``review-clean`` — so the card sits in review forever while merged-state
        verification churns. This restores the ESTABLISHED same-PR fix-round lifecycle by
        requeuing to ``ready`` (the PR, the recorded findings, and the review-fix budget
        all preserved), so the next dispatch resumes the existing branch and leads with the
        findings — it invents no new review outcome.

        The authoritative trigger is LIVENESS, not head identity: a ``changes-requested``
        in_review card with NO surviving drive/fix round is stranded. That is DISTINCT from
        #328, which fires on a demonstrable reviewed-head↔live-head mismatch (an external
        push) and re-ARMS a fresh review; the reconcile runs #328 first, so a head that
        actually moved is already off ``changes-requested`` before this is reached (a genuine
        external-push card takes that path, never this one).

        NEVER requeues a genuinely live drive: a review gate mid-transition INSIDE a running
        drive is still ``changes-requested`` for the instant between its ``set_review_substate``
        and its own ``requeue`` — the liveness guard (a registered drive task #211, a claimed
        worktree, or an in-flight gate) keeps this from racing it, so nothing is requeued or
        duplicated. NEVER spends a review-fix budget merely to restore liveness: the requeue
        carries the budget through untouched, so the resumed round has exactly the bounces it
        had before the crash and the recovery is idempotent across repeated sweeps/restarts
        (once requeued, the card is ``ready`` and the in_review-only reconcile never sees it
        again). Returns True only when it requeued."""
        fid = feature["id"]
        if LABEL_CHANGES_REQUESTED not in (feature.get("labels") or []):
            return False  # only a blocking verdict can strand a fix round
        # A live drive/fix round is not stranded — leave it, and never duplicate it. The
        # three signals together span the whole window a fix round can be alive in this
        # process: a registered drive TASK (process-stable across a reload, #211), a claimed
        # worktree (``_inflight_files``), and a review gate still mid-transition
        # (``_review_inflight`` — the instant a running gate has set changes-requested but
        # not yet requeued). On a restart all three are empty, which is exactly the stranded
        # case this recovery exists for.
        if _loop.live_drive(fid) is not None or fid in self._inflight_files or fid in self._review_inflight:
            return False
        # Restore the fix-round prompt levers the aborted process dropped (best-effort),
        # then requeue onto the SAME PR — requeue preserves external_ref, so the fix-round
        # resume edge (open PR ⇒ resume the branch) continues the existing work. The
        # review-fix budget is deliberately untouched (r5).
        await self._reinject_review_feedback(store, fid, pr_url, repo)
        await asyncio.to_thread(store.requeue, fid)
        log.info(
            "[project_board] %s review fix round stranded by shutdown (in_review + changes-requested, "
            "no live drive) — requeued to ready to resume the fix on the same PR: %s",
            fid,
            pr_url,
        )
        return True

    async def _reinject_review_feedback(self, store, fid: str, pr_url: str, repo: str) -> None:
        """Best-effort restore of a review fix round's prompt levers after a restart dropped
        the in-memory copies (#340): the LATEST recorded findings block (the bead comment the
        gate wrote alongside ``changes-requested``) back into ``_ci_feedback``, and the live
        PR diff back into ``_ci_prior_diff`` — so the resumed dispatch leads with exactly the
        findings and diff the pre-crash bounce carried, instead of re-opening the same PR
        blind to what it must fix. A live in-memory copy is never clobbered, and any read
        failure just leaves the levers empty (the fix round still resumes the branch, only
        without the lead-in)."""
        if self._ci_feedback.get(fid):
            return  # a surviving in-memory copy already leads the next dispatch
        findings = await asyncio.to_thread(self._last_review_findings, store, fid)
        if not findings:
            return
        self._ci_feedback[fid] = (
            "An adversarial code review of your PR REQUESTED CHANGES. Fix every finding "
            "below in the existing branch (the PR updates on push) — do not rewrite "
            "unrelated code.\n\n" + findings
        )
        try:
            self._ci_prior_diff[fid] = await worktree.pr_diff(pr_url, cwd=repo)
        except Exception:  # noqa: BLE001 — the diff is a convenience; the branch is resumed regardless
            self._ci_prior_diff.pop(fid, None)

    @staticmethod
    def _last_review_findings(store, fid: str) -> str:
        """The LATEST recorded review-findings block for ``fid`` — the bead comment the
        review gate wrote alongside ``changes-requested`` (``set_review_substate``'s ``note``,
        a ``_REVIEW_FINDINGS_TITLE`` block). Scanned newest-first so a re-review's findings
        win over an earlier round's. Returns "" when none is recorded or the comment history
        can't be read (a store without ``feature_comments``, a ``br`` hiccup) — never raises."""
        try:
            comments = store.feature_comments(fid)
        except Exception:  # noqa: BLE001 — a comment read must never break the recovery
            return ""
        for text in reversed(comments or []):
            if _REVIEW_FINDINGS_TITLE in (text or ""):
                return str(text).strip()
        return ""

    async def _stamp_reviewed_head(self, store, fid: str, sha: str) -> None:
        """Best-effort stamp of the PR head the review verdict was rendered against
        (#328) — the ``reviewed-head:<sha>`` label the reconcile compares against the
        live head to spot an external push that stales a ``changes-requested`` verdict.
        ``sha=""`` clears it (a clean verdict pins no head). Fire-and-forget like the
        merged-verified stamp: a ``br`` hiccup must never fail the gate that landed the
        verdict — the next poll re-reads, and a MISSING stamp fails the reconcile CLOSED
        (the rejection stands) rather than re-arming on unproven identity."""
        try:
            await asyncio.to_thread(store.record_reviewed_head, fid, sha)
        except Exception:  # noqa: BLE001 — bookkeeping must never break the gate
            log.warning(
                "[project_board] %s reviewed-head stamp (%s) not persisted", fid, sha or "(clear)", exc_info=True
            )

    # ── inbound trusted-QA reconcile on the current head (#323) ───────────────
    async def _reconcile_trusted_qa_pass(self, store, feature: dict, pr_url: str, repo: str) -> bool:
        """Ingest a trusted, promoted QA PASS for the PR's CURRENT head and repair a stale
        ``changes-requested`` or ABSENT local review verdict to ``review-clean`` (#323) —
        the inbound counterpart of #347's head-pinned publish.

        #354 makes the board's own gate verdict a reliable, head-pinned ``QA panel`` commit
        STATUS (the PAT-compatible successor to #347's check run). This reads that SAME status
        back (``worktree.read_review_status`` — the identity plumbing, not a second parsed
        signal): when a PROMOTED PASS whose head-scoped status equals the LIVE PR head exists —
        never promoting from ambiguous/untrusted data — the local review substate is repaired to
        ``review-clean`` so the ordinary merged-state / CI / auto-merge gates decide the
        rest. It invents no verdict — it ADOPTS a verified one, and only ever RELAXES a
        blocking state to clean (never manufactures a blocking one).

        Fails CLOSED, leaving the card exactly as it was (no promotion, so the merge edge
        still can't touch it), on everything that is not a provable current-head PASS: a
        FAIL never promotes or clears a blocking state (r2); a PASS for another head,
        unreadable / malformed / ambiguous marker data, or no promotion evidence changes
        nothing (r3); and — the TOCTOU guard — a live head that MOVED between the check read
        and the promotion write (a PR push landing mid-reconcile) is not trusted either, so
        a PASS proven for the old head can never mark a newly pushed, unreviewed head clean
        (r3). That guard is BOTH a pre-write early-out AND a post-write confirmation: a push
        that races the review-clean write itself is detected right after it lands and the
        write is UNDONE (reverted to the prior blocking / absent substate) before the merge
        edge can act on it. NEVER races the internal gate (r4/r5): it skips a ``review-pending``
        card (the gate owns that live verdict) and a ``review-clean`` card (already promoted
        → idempotent no-op), and — the same liveness guard the stranded-fix recovery (#340)
        uses — any card with a live drive, a claimed worktree, or an in-flight gate. Returns
        True only when it repaired the substate; the caller then refreshes its snapshot so
        the downstream #340 / merge edges read the cleaned labels this same pass."""
        fid = feature["id"]
        if not self.review_gate:
            return False  # no gate ⇒ no review substate to repair
        labels = set(feature.get("labels") or [])
        # review-pending → the internal gate owns the live verdict (r4/r5); review-clean →
        # already promoted, a repeated poll is a no-op (idempotent). Only a stale rejection
        # (changes-requested) or an ABSENT verdict (pre-upgrade card, operator unblock, inert
        # gate — merge_posture's "no review-clean verdict") is repairable.
        if LABEL_REVIEW_PENDING in labels or LABEL_REVIEW_CLEAN in labels:
            return False
        stale_rejection = LABEL_CHANGES_REQUESTED in labels
        # A live fix round / in-flight gate is about to land its OWN verdict — adopting an
        # external PASS under it would overwrite that in-flight result (r4). The three signals
        # span the whole window a fix round is alive in this process (mirrors #340).
        if _loop.live_drive(fid) is not None or fid in self._inflight_files or fid in self._review_inflight:
            return False
        head = await worktree.pr_head_sha(pr_url, cwd=repo)
        if not head:
            return False  # unreadable live head → fail closed; the next poll retries
        _number, repo_slug = _parse_pr_url(pr_url)
        if not repo_slug:
            return False  # no repo identity → fail closed
        verdict = await worktree.read_review_status(repo_slug, head, cwd=repo)
        if verdict is None:
            # Unreadable / absent / malformed / ambiguous / another-head status → fail closed,
            # leaving the card unpromoted (#354 r5). Never promotes from ambiguous/untrusted data.
            return False
        if not verdict.get("passed"):
            # A trusted, current-head FAIL is authoritative the OTHER way: it must never
            # promote or clear a blocking state (r2). Leave changes-requested / absence as is.
            log.info(
                "[project_board] %s trusted QA verdict for current head %s is %s — not promoting (fail closed): %s",
                fid,
                head[:12],
                verdict.get("state"),
                pr_url,
            )
            return False
        # A trusted, PROMOTED, current-head PASS. The verdict is written PINNED to the head
        # it was proven for (#323): `set_review_substate` stamps `review-clean-sha:<head>`
        # and `merge_posture` refuses to merge unless that pin equals the live head.
        #
        # That is what makes this write safe with no race window. The first cuts tried to
        # guard it — re-read the head before the write, then re-read again after and undo —
        # and review correctly rejected both: a push can land after ANY check, so
        # check-then-act cannot be made safe here. It can, however, be made IRRELEVANT. A
        # push at any point leaves the pin naming a head that no longer exists, the merge
        # gate declines, and the card goes back for review. There is nothing to lose a race
        # to, and nothing to undo.
        note = (
            f"review reconciled to clean (#323): a trusted QA PASS ({verdict.get('state')}) is promoted for "
            f"PR head {head[:12]} — "
            + ("the stale changes-requested verdict" if stale_rejection else "no local review verdict was recorded")
            + " has been repaired to review-clean, PINNED to that head; the ordinary merge gates decide the rest"
        )
        await asyncio.to_thread(store.set_review_substate, fid, LABEL_REVIEW_CLEAN, note=note, head_sha=head)
        # The REVIEWED-HEAD stamp is a different thing from the clean verdict's pin, and a
        # clean verdict still clears it: #328 judges a later changes-requested against that
        # stamp, and an absent one fails that reconcile closed. The pin (#323) is what says
        # WHICH head this PASS is good for. Also reset the fix budget the adopted PASS makes
        # moot.
        await self._stamp_reviewed_head(store, fid, "")
        await self._budget_reset(store, fid, "review-fix")
        log.info(
            "[project_board] %s reconciled a trusted current-head QA PASS (%s) → review-clean, pinned to that head: %s",
            fid,
            head[:12],
            pr_url,
        )
        return True

    async def _publish_gate_verdict(
        self,
        fid: str,
        pr_url: str,
        repo: str,
        head_sha: str,
        *,
        state: str,
        description: str,
        comment: str = "",
    ) -> None:
        """Publish the in-loop review-gate verdict where the PR is reviewed — a PAT-compatible
        COMMIT STATUS (#354), replacing #347's check run. ``POST /repos/{slug}/statuses/{sha}``
        succeeds under the board's user/PAT ``gh`` token; #347's ``POST /check-runs`` needs a
        GitHub App installation token and 403s here ("You must authenticate via a GitHub App"),
        so it never actually published. The status is a ``QA panel`` context (the same historic
        name), one of success/failure/pending, a concise <=140-char ``description``, and the PR
        link as the stable ``target_url``.

        Pinned to ``head_sha``, the IMMUTABLE head the gate actually reviewed (#328,
        ``reviewed_head`` read BEFORE the panel): an unknown head (gh couldn't read it, so
        ``head_sha`` is empty) posts NOTHING — a verdict never lands against a head the gate did
        not see. That missing-head skip is logged DISTINCTLY from a publication permission/API
        refusal (#354 r7): the refusal surfaces from ``worktree.post_review_status`` itself.

        For a BLOCKING verdict, ``comment`` carries the full actionable findings — posted/updated
        as ONE board-authored PR comment (``worktree.post_or_update_pr_comment``, idempotent per
        PR) so the human sees the rationale GitHub-side, not just the terse status line. The bead
        comment stays the durable audit record throughout; a status/comment failure is best-effort
        and never breaks the landed verdict."""
        if not head_sha:
            # Unreadable/no head SHA — a MISSING-HEAD skip (#354 r7), distinct from a permission
            # refusal: there is nothing to pin to, so neither status nor comment is published.
            log.info("[project_board] %s review verdict not published — reviewed head unknown (#328 fail-closed)", fid)
            return
        _number, repo_slug = _parse_pr_url(pr_url)
        if not repo_slug:
            log.info("[project_board] %s review verdict not published — no repo slug from %s", fid, pr_url)
            return
        try:
            ok = await worktree.post_review_status(
                repo_slug, head_sha, state=state, description=description, target_url=pr_url, cwd=repo
            )
        except Exception as exc:  # noqa: BLE001 — a status post must never break the landed verdict
            log.warning("[project_board] %s review status post raised (verdict still on the bead): %s", fid, exc)
            ok = False
        if not ok:
            log.warning(
                "[project_board] %s review status not posted (gh permission/API refusal) — verdict rides the bead "
                "comment",
                fid,
            )
        # Blocking verdicts also carry the full findings to the PR as one idempotent comment, so
        # the human sees the actionable rationale beside the non-success status (#354 r2).
        if comment:
            try:
                posted = await worktree.post_or_update_pr_comment(pr_url, comment, cwd=repo)
            except Exception as exc:  # noqa: BLE001 — the PR comment must never break the verdict
                log.warning(
                    "[project_board] %s review PR-comment post raised (findings still on the bead): %s", fid, exc
                )
                posted = False
            if not posted:
                log.warning(
                    "[project_board] %s review findings not posted to the PR (gh failure) — findings ride the bead", fid
                )

    # ── blocking review gate (plan M5) ────────────────────────────────────────
    async def _review_gate(self, store, fid: str, pr_url: str, repo: str) -> None:
        """Run the adversarial review workflow on the just-opened PR and act on the
        findings — the review sibling of the CI bounce:

        - **clean** (no blocker/major surviving the verify pass) → clear the review
          sub-state; the feature stays in_review for the merge edge.
        - **blocking findings** → store them on the bead (comment), inject them into
          the retry prompt via ``_ci_feedback`` (+ the PR diff via ``_ci_prior_diff``
          — the same carry-the-lesson levers), label ``changes-requested``, and
          requeue — bounded by ``review_fix_max``.
        - **budget exhausted** → ``flag_blocked`` for human review. NEVER a silent
          merge, and never a silent pass: a gate that can't run (no workflow runner,
          no parser, no reviewer) leaves the feature in_review with a warning — the
          same posture as CI being unreachable.

        Sequencing (ADR 0064): this is deliberately a single call-site-agnostic
        method — when the board face of execution-grounded selection lands, moving
        the gate after test-passing candidate selection is a one-line move.

        Re-entrancy (#205): at most ONE gate per feature runs at a time in this
        process. A second call while the first is still running (the reconcile's
        resume edge seeing the pending label the running gate just set) is a no-op
        — the running gate will land its own verdict. The resume edge keeps its
        job for gates that actually died (host restart: the set is empty on boot).
        """
        if fid in self._review_inflight:
            log.debug("[project_board] %s review gate already running — not re-armed", fid)
            return
        self._review_inflight.add(fid)
        try:
            await self._review_gate_run(store, fid, pr_url, repo)
        finally:
            self._review_inflight.discard(fid)

    async def _review_gate_run(self, store, fid: str, pr_url: str, repo: str) -> None:
        """The gate body — see ``_review_gate`` (the re-entrancy guard) for the contract."""
        await asyncio.to_thread(store.set_review_substate, fid, LABEL_REVIEW_PENDING)
        # The head THIS verdict is for (#328) — read BEFORE the panel so a push that lands
        # DURING the review can never stamp the verdict as current for a head the review
        # never saw (which would merge an un-reviewed head); if the head moved mid-review,
        # the stamp stays at the reviewed head and the next reconcile re-arms. "" when gh
        # can't be read → the verdict lands UNSTAMPED and the reconcile fails closed on it.
        reviewed_head = await worktree.pr_head_sha(pr_url, cwd=repo)
        # Show a live ``QA panel`` PENDING status on the reviewed head while the gate runs
        # (#354) — a fail-closed yellow the merge edge won't cross, resolved to success/failure
        # below. No PR comment for pending (only the terminal blocking verdict carries findings).
        await self._publish_gate_verdict(
            fid, pr_url, repo, reviewed_head, state="pending", description="Review gate running…"
        )
        output, why = await self._run_review_workflow(fid, pr_url)
        if output is None:
            # Could not review — ``why`` names the actual cause (#180: no runner +
            # no reviewer, failed panel steps, a dead call — a failed finder step is
            # not a review; judging from it is how an unreviewed PR gets promoted,
            # ADR 0078 D3). Leave review-pending so the PR reconcile retries next
            # poll — but bounded: a persistently unrunnable gate escalates to the
            # operator instead of re-burning the workflow every poll forever.
            reason = why or "review produced no output"
            n = await self._budget_get(store, fid, "review-run") + 1
            await self._budget_set(store, fid, "review-run", n)
            if n >= self.review_run_max:
                # Deliberately KEEP review-pending through the block (#181): blocked
                # features aren't reconciled, so the label is inert while blocked —
                # but the moment the operator unblocks, the feature is back in_review
                # with review-pending set and the next poll re-arms the gate. Clearing
                # it here left an unblocked feature indistinguishable from a clean
                # review, so its PR could merge un-reviewed.
                await asyncio.to_thread(
                    store.flag_blocked,
                    fid,
                    f"review gate could not complete after {n} attempt(s) — {reason} — "
                    f"needs operator attention: {pr_url}",
                )
                await self._budget_reset(store, fid, "review-run")
                log.warning("[project_board] %s blocked (review gate unrunnable %d times: %s)", fid, n, reason)
                return
            log.warning(
                "[project_board] %s review gate could not run (%d/%d): %s — will retry on the next poll",
                fid,
                n,
                self.review_run_max,
                reason,
            )
            return
        await self._budget_reset(store, fid, "review-run")
        findings = self._parse_findings(output)
        if findings is None:
            # Host predates the findings convention (ADR 0077) — the gate can't
            # judge, so it must not pretend to. Record and leave in review.
            await asyncio.to_thread(
                store.set_review_substate, fid, None, note="review gate: host lacks graph.review.findings — gate inert"
            )
            log.warning("[project_board] %s review gate inert (no findings parser on this host)", fid)
            return
        # Remember this round's findings — the next run (a bounce re-review) passes
        # them back as the recipe's prior_findings input, making it a DELTA review
        # (drop fixed, carry still-open) instead of a from-scratch re-litigation.
        try:
            self._review_prior[fid] = json.dumps([f.to_dict() for f in findings]) if findings else ""
        except Exception:  # noqa: BLE001 — memory is an optimization, never a gate failure
            self._review_prior.pop(fid, None)
        blocking = [f for f in findings if f.verdict != "refuted" and f.severity in ("blocker", "major")]
        # #381: enforce the grounding ADR 0077 already promises. A blocking finding must
        # quote the diff VERBATIM; one whose quote the diff demonstrably does not contain
        # cannot be fixed by editing code that already says the right thing, so it bounces
        # the card twice and terminal-blocks a green branch. Fetch the WHOLE diff for this
        # (the prompt-sized default would make a later hunk look absent) and decline to
        # judge a truncated one. Reused below as the bounce's prior-diff, re-cut to the
        # prompt budget, so the extra `gh pr diff` costs nothing.
        full_diff = ""
        if blocking:
            full_diff = await worktree.pr_diff(pr_url, cwd=repo, max_chars=_GROUNDING_DIFF_MAX_CHARS)
        if blocking and worktree.DIFF_TRUNCATED_MARKER not in full_diff:
            blocking, ungrounded = partition_by_grounding(blocking, full_diff)
            for f in ungrounded:
                # Loud, and on the bead below — the finding is demoted, never dropped: a
                # silent downgrade would hide a real defect behind a quoting slip.
                log.warning(
                    "[project_board] %s review finding NOT blocking — its evidence is absent "
                    "from the PR diff (%s:%s %s): %s",
                    fid,
                    f.file,
                    f.line,
                    f.severity,
                    f.claim,
                )
            if ungrounded:
                try:
                    await asyncio.to_thread(
                        store.comment,
                        fid,
                        f"review gate: {len(ungrounded)} finding(s) demoted to non-blocking — evidence "
                        f"absent from the PR diff (ADR 0077 requires a verbatim quote):\n"
                        + "\n".join(f"- {f.file}:{f.line} [{f.severity}] {f.claim}" for f in ungrounded),
                    )
                except Exception:  # noqa: BLE001 — bookkeeping must not fail the gate
                    log.warning("[project_board] %s ungrounded-finding comment failed", fid, exc_info=True)
        if not blocking:
            await asyncio.to_thread(
                store.set_review_substate,
                fid,
                LABEL_REVIEW_CLEAN,
                note=f"review gate: clean — {len(findings)} finding(s), none blocking (blocker/major)",
                # Pin the verdict to the head it actually READ (#323). The merge gate
                # requires this to equal the live head, so a push after the review — at any
                # point, including while this write lands — leaves the pin stale and the
                # merge declines instead of shipping code nothing reviewed.
                head_sha=reviewed_head,
            )
            # A clean verdict pins no head: clear the reviewed-head stamp so a later
            # changes-requested (an external fleet review, a re-block) can't be judged
            # stale against a dead head — an absent stamp fails the reconcile CLOSED (#328).
            await self._stamp_reviewed_head(store, fid, "")
            await self._budget_reset(store, fid, "review-fix")
            # r1: the clean verdict is a passing gate ON THE HEAD IT REVIEWED (#354). The
            # stamp is cleared (a clean verdict pins no head for the reconcile), but the status
            # must land against the exact reviewed head — the full sha, not "". No PR comment on
            # a clean verdict (only the success status); the bead carries the audit note.
            await self._publish_gate_verdict(
                fid,
                pr_url,
                repo,
                reviewed_head,
                state="success",
                description=f"Review gate clean — {len(findings)} finding(s), none blocking",
            )
            log.info("[project_board] %s review gate clean (%d non-blocking finding(s))", fid, len(findings))
            return

        rendered = self._render_findings(blocking)
        n = await self._budget_get(store, fid, "review-fix")
        if n >= self.review_fix_max:
            await asyncio.to_thread(store.set_review_substate, fid, None, note=rendered)
            # Blocked for a human, changes-requested dropped: clear the head stamp too so
            # an operator unblock can't leave a dead-head marker for the reconcile (#328).
            await self._stamp_reviewed_head(store, fid, "")
            await asyncio.to_thread(
                store.flag_blocked,
                fid,
                f"review findings persist after {n} fix attempt(s) — needs human review: {pr_url}",
            )
            self._ci_feedback.pop(fid, None)
            self._ci_prior_diff.pop(fid, None)
            await self._budget_reset(store, fid, "review-fix")
            # r2: a blocking verdict is a NON-success status carrying the surviving findings to
            # the PR as a comment (#354). The exhausted round is terminal (a human owns it now),
            # so `failure`, and the findings comment names the persistence + the PR reference.
            await self._publish_gate_verdict(
                fid,
                pr_url,
                repo,
                reviewed_head,
                state="failure",
                description=f"Review gate: {len(blocking)} finding(s) persist after {n} fix attempt(s) — needs human review",
                comment=f"{rendered}\n\nThese findings persist after {n} fix attempt(s) — needs human review: {pr_url}",
            )
            log.warning("[project_board] %s blocked (review findings, %d bounce(s) exhausted)", fid, n)
            return
        await self._budget_set(store, fid, "review-fix", n + 1)
        # Carry the lesson exactly like the CI bounce: findings as the rejection
        # feedback + the reviewed diff so the coder fixes THIS attempt, not a fresh one.
        self._ci_prior_diff[fid] = worktree.truncate_diff(full_diff, _PRIOR_DIFF_MAX_CHARS)
        self._ci_feedback[fid] = (
            "An adversarial code review of your PR REQUESTED CHANGES. Fix every finding "
            "below in the existing branch (the PR updates on push) — do not rewrite "
            "unrelated code.\n\n" + rendered
        )
        await asyncio.to_thread(store.set_review_substate, fid, LABEL_CHANGES_REQUESTED, note=rendered)
        # Pin the verdict to the head it was rendered against (#328) so a later external
        # push to this branch reads as a demonstrable head move and re-arms the gate — an
        # unchanged head keeps matching this stamp and stays rejected. Empty (unreadable
        # head) writes no stamp → the reconcile fails closed on it, never re-arming blind.
        await self._stamp_reviewed_head(store, fid, reviewed_head[:_REVIEWED_HEAD_SHA_LEN] if reviewed_head else "")
        # r2: publish the blocking verdict against the reviewed head as a NON-success status
        # (#354) with the surviving findings posted to the PR as a comment — a fix round is
        # active, the coder is re-driving. The full reviewed head, not the truncated stamp.
        await self._publish_gate_verdict(
            fid,
            pr_url,
            repo,
            reviewed_head,
            state="failure",
            description=f"Review gate: {len(blocking)} blocking finding(s) — a fix round is in progress",
            comment=f"{rendered}\n\nThe coder is re-driving a fix for these findings.\n\n{pr_url}",
        )
        await asyncio.to_thread(store.requeue, fid)
        log.info(
            "[project_board] %s review gate bounce %d/%d (%d blocking finding(s))",
            fid,
            n + 1,
            self.review_fix_max,
            len(blocking),
        )

    async def _run_review_workflow(self, fid: str, pr_url: str) -> tuple[str | None, str | None]:
        """Produce the raw review output for a PR: the host's workflow runner
        (``runtime.state.STATE.workflow_run`` — published by the workflows plugin,
        no plugin import needed) running ``review_workflow``, else the configured
        a2a reviewer told to emit the findings convention.

        Returns ``(output, None)`` on success, ``(None, reason)`` when the review
        could not happen. The reason names the ACTUAL cause — runner missing,
        failed panel steps, a dead call — so the gate's retry warning and eventual
        block reason tell the operator what to fix instead of making them
        correlate a generic three-hypothesis message with the server log (#180:
        the live incident was simply the workflows plugin being disabled)."""
        number, repo_slug = _parse_pr_url(pr_url)
        runner = None
        try:
            from runtime.state import STATE

            runner = getattr(STATE, "workflow_run", None)
        except Exception:  # noqa: BLE001 — non-protoAgent host (tests) → try the reviewer
            runner = None
        # Why the workflow path yielded nothing (None = there was no runner at all) —
        # composed into the reason when the reviewer fallback can't save the run.
        no_run_reason: str | None = None
        if runner is not None and number:
            try:
                inputs: dict = {"pr": number, "repo": repo_slug}
                prior = self._review_prior.get(fid)
                if prior:
                    inputs["prior_findings"] = prior
                result = await runner(self.review_workflow, inputs)
                failed = list((result or {}).get("failed") or [])
                if failed:
                    # A partial panel is NOT a review (ADR 0078 D3): a starved/errored
                    # finder means unreviewed angles, and a verdict synthesized from
                    # the survivors reads as clean coverage it never had.
                    log.warning(
                        "[project_board] %s review workflow %r had failed step(s) %s — fail closed, not a review",
                        fid,
                        self.review_workflow,
                        failed,
                    )
                    return None, (
                        f"workflow {self.review_workflow!r} ran but had failed step(s): "
                        f"{', '.join(str(s) for s in failed)}"
                    )
                output = str((result or {}).get("output") or "")
                if output:
                    return output, None
                return None, f"workflow {self.review_workflow!r} ran but produced no output"
            except Exception as exc:  # noqa: BLE001 — a dead workflow ≠ a dead loop
                log.warning("[project_board] %s review workflow %r failed: %s", fid, self.review_workflow, exc)
                no_run_reason = f"workflow {self.review_workflow!r} call failed: {exc}"
                # fall through to the reviewer alternative
        elif runner is not None:
            no_run_reason = f"workflow runner present but no PR number parses from {pr_url!r}"
        reviewer = self._resolve_delegate(self.reviewer_name, "a2a")
        if reviewer is None:
            if no_run_reason is not None:
                return None, f"{no_run_reason}; no reviewer fallback configured"
            return None, "no workflow runner available and no reviewer configured"
        from plugins.delegates.adapters import ADAPTERS

        try:
            msg = (
                f"Adversarially review this pull request: {pr_url}\n\n"
                "Read the diff, verify each suspicion against the code, and report ONLY "
                "evidence-backed findings as a fenced ```json array of objects "
                '{"file", "line", "severity" (blocker|major|minor|nit), "category", '
                '"claim", "evidence", "verdict" (confirmed|refuted|uncertain)}. '
                "No findings → an empty array []."
            )
            output = await ADAPTERS["a2a"].dispatch(reviewer, msg)
            if output is None:
                return None, f"reviewer {self.reviewer_name!r} returned no output"
            return output, None
        except Exception as exc:  # noqa: BLE001
            log.warning("[project_board] %s reviewer fallback failed: %s", fid, exc)
            return None, f"reviewer {self.reviewer_name!r} call failed: {exc}"

    @staticmethod
    def _parse_findings(output: str):
        """The findings convention parser (ADR 0077), imported from the HOST lazily —
        the contract both this gate and the craft skill consume. None = the host
        doesn't ship it (gate goes inert rather than guessing at prose)."""
        try:
            from graph.review.findings import parse_findings
        except ImportError:
            return None
        return parse_findings(output or "")

    @staticmethod
    def _render_findings(findings) -> str:
        try:
            from graph.review.findings import render_findings_markdown

            return render_findings_markdown(findings, title=_REVIEW_FINDINGS_TITLE)
        except ImportError:  # unreachable when _parse_findings succeeded; belt+braces
            return "\n".join(f"- {f.file}:{f.line} [{f.severity}] {f.claim}" for f in findings)

    async def _run_local_gate(self, wt: str, feature: dict | None = None) -> str | None:
        """Run the pre-PR local gate (``local_gate_cmd``) in the worktree.

        Returns ``None`` when the gate passes (exit 0), when no gate is configured,
        or when the gate itself couldn't run (timeout / unlaunchable command) — a
        broken or flaky gate must never block otherwise-good work, so those degrade
        to "pass" (CI is still the real gate). Returns the captured output (tail,
        truncated to ``local_gate_output_chars``) on a CLEAN non-zero exit, so the
        caller can hand it to the coder to fix. Resolves the gate command from the
        feature's project when given (#90)."""
        cmd = self._local_gate_cmd_for(feature) if feature is not None else self.local_gate_cmd
        if not cmd:
            return None
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=wt,
                env=self._child_env(),
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
            if proc.returncode is not None and proc.returncode < 0:
                # Killed by a signal — the member shutting down (SIGTERM reaches the
                # child), an operator `kill`, the OOM killer — NOT the repo failing its
                # own gate. Same posture as the timeout above: the gate couldn't run to
                # a verdict, so it must not produce one. Seen 2026-08-20: a restart
                # landed mid merged-state gate, pytest died at 13% with rc=-15, and the
                # feature was flag_blocked "gate FAILED on the merged state" against a
                # PR whose CI was fully green.
                log.warning(
                    "[project_board] pre-PR gate killed by signal %d (shutdown / external kill) — "
                    "no verdict, treating as pass (CI still gates)",
                    -proc.returncode,
                )
                return None
            text = (out or b"").decode("utf-8", "replace").strip()
            if len(text) > self.local_gate_output_chars:
                text = "…(truncated)…\n" + text[-self.local_gate_output_chars :]
            return text or f"gate command exited {proc.returncode} with no output"
        except Exception as exc:  # noqa: BLE001 — a gate that can't run must not block
            log.info("[project_board] pre-PR gate failed to run (treating as pass — CI still gates): %s", exc)
            return None
