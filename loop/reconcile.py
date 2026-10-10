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

from .. import external_review, merge_state_hold, review_coverage_hold
from ._common import *  # noqa: F401,F403 — share the loop kernel namespace

_loop = sys.modules[__package__]  # the loop package, for monkeypatch-visible seams

# How often the working-state refresher checks whether a read is due (an in-memory compare;
# the reads themselves are bounded by work_snapshot.MIN_INTERVAL_S / MAX_AGE_S).
_SNAPSHOT_POLL_S = 1.0

# How much of a red gate's output an operator salvage hands back — in its record, and in
# the body of a forced draft (#427). The tail: that is where a test runner's summary is.
_SALVAGE_GATE_TAIL_CHARS = 3000
# The PR comment a forced salvage posts the gate output in, updated in place on a repeat.
_SALVAGE_GATE_MARKER = "<!-- project-board:salvage-gate -->"


async def _within(coro, timeout: float, *, on_abandon=None):
    """Await ``coro`` for at most ``timeout`` seconds, then give up on it — HARD (#462).

    ``asyncio.wait_for`` waits for the cancelled call to actually finish, so a call that
    ignores or swallows its cancel (a hung model stream inside a host client whose own
    ``request_timeout`` did not apply, protoAgent#3699) held the review gate for 80 minutes.
    Here a timeout cancels the call and returns at once; its eventual outcome is retrieved
    and dropped. Raises ``asyncio.TimeoutError``."""
    task = asyncio.ensure_future(coro)
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        task.cancel()
        raise
    if task in done:
        return task.result()
    task.cancel()
    task.add_done_callback(lambda t: t.cancelled() or t.exception())  # retrieve it: no "never retrieved" noise
    if on_abandon is not None and not task.done():
        on_abandon(task)  # the caller tracks the call that is still running (#471 review)
    raise asyncio.TimeoutError


# The prefix of an unrunnable-review reason that is a TIMEOUT of the board's own cap (#462).
# A timeout is not the review failing — a local model can simply be slow — so it does not
# spend the `review-run` budget that blocks the card; see `_review_gate_run`.
_REVIEW_TIMED_OUT = "review call timed out"


def _gate_failure_block(gate_out: str) -> str:
    """The red gate's output for a PR body or comment — fenced with more backticks than any
    run in the output itself, so a test log that prints a code fence cannot break out of it."""
    tail = gate_out[-_SALVAGE_GATE_TAIL_CHARS:]
    fence = "`" * max(3, max((len(run) for run in re.findall(r"`+", tail)), default=0) + 1)
    return (
        "## ⚠ The pre-PR gate FAILED on this tree\n\n"
        "Published by an operator override. Fix what the gate reports before marking it ready:\n\n"
        f"{fence}\n{tail}\n{fence}"
    )


async def request_salvage(fid: str, *, force: bool = False, tree: str = "") -> dict:
    """The seam the salvage route and ``board_salvage_feature`` call (#427): the RUNNING
    loop's :meth:`ReconcileMixin.salvage`, reached through the process-stable registry
    exactly as ``request_dispatch`` reaches ``dispatch_now``. A salvage runs the loop's own
    gate and publish steps under its own claim, so with no loop surface live in this
    process there is nothing to run it: ``loop-not-running``. (A DISABLED loop is
    registered, and salvages — publishing needs the loop's config, not its ticking.)"""
    loop = _loop.live_loop()
    if loop is None:
        return {
            "feature_id": fid,
            "outcome": "loop-not-running",
            "detail": "no board loop surface is running in this process — nothing to run the salvage with",
            "worktree": "",
            "pr_url": "",
            "draft": False,
            "gate_output": "",
        }
    return await loop.salvage(fid, force=force, tree=tree)


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
                if await self._still_orphaned_then(store, fid, store.requeue):
                    log.info("[project_board] %s self task reset to ready (no live drive — re-dispatch)", fid)
            elif self._resolve_task_delegate(assignee) is not None:
                if await self._still_orphaned_then(store, fid, store.requeue):
                    log.info("[project_board] %s task reset to ready (sister-agent drive died — re-dispatch)", fid)
            return
        pr_url = await worktree.pr_url_for_branch(
            worktree.branch_name(fid, feature.get("title") or ""), cwd=self._repo_for(feature)
        )
        if fid in self._inflight_files:
            return  # reserved during the gh read — an operator salvage owns the card now (#427)
        if pr_url:
            if await self._still_orphaned_then(store, fid, lambda f: store.open_review(f, pr_url=pr_url)):
                log.info("[project_board] %s already had a PR → in_review (%s)", fid, pr_url)
        elif await self._salvage_verified_candidate(store, fid):
            pass  # resumed + PR opened → in_review (logged inside)
        elif await self._still_orphaned_then(store, fid, store.requeue):
            log.info("[project_board] %s reset to ready (no PR — rebuild fresh)", fid)

    async def _still_orphaned_then(self, store, fid: str, move) -> bool:
        """Apply ``move(fid)`` only if ``fid`` is STILL an orphan: in_progress with no drive
        behind it. The re-read happens under the claim lock (#402). The decision above was
        made on a read taken before a ``gh`` round-trip, and an operator's attach can move
        the card to in_review in that window. Requeueing on the stale read undid the attach,
        and the next claim put a coder back on the PR's branch. The attach and the claim scan
        write under this same lock. Only positive evidence that the card moved skips the
        move, never a read that says nothing. A card an operator salvage reserved in that
        window is not an orphan either (#427). Returns whether the move ran."""
        async with self._claim_guard():
            fresh = await asyncio.to_thread(store.get_feature, fid) or {}
            state = fresh.get("board_state")
            held = _loop.live_drive(fid) is not None or fid in self._inflight_files
            if (state and state != "in_progress") or held:
                log.info(
                    "[project_board] %s is no longer an orphan (now %s) — leaving it alone",
                    fid,
                    fresh.get("board_state") or "gone",
                )
                return False
            await asyncio.to_thread(move, fid)
            return True

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
            # env changed) is a doubt, not a ship — so this never forces.
            wt, branch = await worktree.promote_worktree(repo, wt, branch, fid, self.root, title=title_raw)
            if await self._gate_tree(f, wt) is not None:
                log.info("[project_board] %s salvage: gate fails on the candidate now — rebuild fresh", fid)
                await asyncio.to_thread(self._clear_verified, store, fid)
                return False
            pr_url = await self._open_tree_pr(f, wt, branch)
            await asyncio.to_thread(store.open_review, fid, pr_url=pr_url)
            await asyncio.to_thread(self._clear_verified, store, fid)
            log.info("[project_board] %s salvaged the verified candidate → %s (no re-solve)", fid, pr_url)
            return True
        except Exception:  # noqa: BLE001 — ANY doubt/error → today's rebuild-fresh path
            log.warning("[project_board] %s salvage attempt failed — rebuild fresh", fid, exc_info=True)
            return False

    # ── publishing a tree whose coder is gone (#91 crash salvage, #427 operator salvage) ──
    # The tail of a drive, for a tree whose coder is gone — shared by the crash salvage of
    # a verified candidate and the operator's salvage, in two halves so a caller can stop
    # between them: a red gate must be able to refuse before the tree is touched further.
    async def _gate_tree(self, f: dict, wt: str) -> str | None:
        """Fixups, then the pre-PR gate, in ``wt``: ``None`` when green (or when no gate is
        configured, or it could not run — the drive's own fail-open), else its output."""
        await self._run_fixups(wt, f)
        return await self._run_local_gate(wt, f)

    async def _open_tree_pr(self, f: dict, wt: str, branch: str, *, gate_out: str | None = None, note: str = "") -> str:
        """``open_pr`` for a tree whose coder is gone. ``note`` is appended to the body: how
        this tree came to be published. ``gate_out`` — a RED gate an operator overrode —
        opens it as a DRAFT whose body carries that output, so nobody takes it for a green
        PR and the auto-merge edge (which never merges a draft) leaves it for a human."""
        fid = f.get("id") or ""
        body = _pr_body("", f) + (f"\n\n{note}" if note else "")
        if gate_out is not None:
            body += "\n\n" + _gate_failure_block(gate_out)
        body = await self._with_source_issue_ref(f, wt, body)
        as_draft = {"draft": True} if gate_out is not None else {}
        return await worktree.open_pr(
            wt,
            branch,
            base=self._base_branch_for(f),
            title=f"feat: {f.get('title') or fid}",
            body=body,
            promote_draft=not f.get("pr_url") and not as_draft,
            **as_draft,
        )

    async def salvage(self, fid: str, *, force: bool = False, tree: str = "") -> dict:
        """Publish a stranded card's worktree WITHOUT a coder (#427): commit what it holds,
        run the pre-PR gate, push, open the PR — or push onto the card's existing one — and
        put the card in review. bd-ezs7's finished 170 lines could only be published by
        dispatching a coder, the one thing that was down that day.

        An OPERATOR OVERRIDE: it skips the drive's pre-PR goal, requirement-ledger and
        source-issue checks. CI and the review gate still apply — with ``review_gate`` on, the
        gate runs here, inline, exactly as a drive runs it.

        Refuses, publishing nothing, when a live drive (or another salvage) owns the card;
        when the card is not stranded (only ``in_progress`` with no drive, or ``blocked`` — a
        ``ready`` card could be claimed by the loop mid-publish); when none of its trees has
        changes vs base; or when several do and ``tree`` (a path, or a ``feat-…`` directory
        name) does not pick one. A directory under the card's tree names that is not a
        worktree the repo registered — a leftover with no ``.git``, a husk, a separate clone —
        is never touched (``worktree.own_worktree``). A RED gate publishes nothing (``gate-red``, with
        the output's tail) unless ``force``: then the PR is a DRAFT carrying that output — a
        new one is opened as a draft, an existing one converted, with the output posted on
        it. ``draft`` in the record is read back from GitHub, never assumed. A cancel that
        lands mid-publish stops it before the PR, or closes the PR it just opened (#211).

        While it runs, the card is reserved in ``_inflight_files`` — the claim a drive holds,
        released by identity so it can never drop another holder's — and its trees are held
        (``worktree.hold_trees``): no claim, sweep, auto-unblock or cancel reap gets in.
        Returns ``{outcome, detail, feature_id, worktree, branch, pr_url, draft,
        gate_output}`` — ``outcome`` one of ``published`` / ``gate-red`` / ``refused`` /
        ``cancelled`` / ``not-found`` / ``error``."""
        rec = {
            "feature_id": fid,
            "outcome": "refused",
            "detail": "",
            "worktree": "",
            "branch": "",
            "pr_url": "",
            "draft": False,
            "gate_output": "",
        }
        if fid in self._inflight_files or _loop.live_drive(fid) is not None:
            return {**rec, "detail": f"a live drive or another salvage owns {fid} — let it finish, or stop it first"}
        mine: set = set()  # this salvage's reservation, told apart from any other by identity
        self._inflight_files[fid] = mine  # reserved before the first await: no other edge gets in
        try:
            return await self._salvage(fid, force=force, tree=tree, rec=rec)
        except Exception as exc:  # noqa: BLE001 — a record for the route and the tool, never a raise
            # A board read that fails or stalls (#431 bounds a `br` call, and says so by
            # raising) lands here before anything was published: the steps after the push
            # report their own failures in the record.
            log.warning("[project_board] %s salvage failed", fid, exc_info=True)
            return {**rec, "outcome": "error", "detail": f"the salvage failed: {type(exc).__name__}: {exc}"}
        finally:
            if self._inflight_files.get(fid) is mine:
                self._inflight_files.pop(fid, None)

    async def _salvage(self, fid: str, *, force: bool, tree: str, rec: dict) -> dict:
        store = self._store()
        f = await asyncio.to_thread(store.get_feature, fid)
        if not f:
            return {**rec, "outcome": "not-found", "detail": f"no feature {fid}"}
        if f.get("issue_type") == LABEL_TASK:
            return {**rec, "detail": f"{fid} is a task — it ships a deliverable, not a worktree; deliver it instead"}
        state = str(f.get("board_state") or "")
        if state not in ("in_progress", "blocked"):
            hint = " — block it first, or the loop may claim it mid-publish" if state == "ready" else ""
            return {
                **rec,
                "detail": f"only a stranded card can be salvaged (in_progress with no drive, or blocked); "
                f"{fid} is {state}{hint}",
            }
        repo, base, title = self._repo_for(f), self._base_branch_for(f), f.get("title") or ""
        trees, strays = [], []
        for path, branch in worktree.feature_worktrees(repo, self.root, fid):
            if not await worktree.own_worktree(repo, path):
                strays.append(os.path.basename(path))  # not a checkout: git would answer for the main one
                continue
            held = [await worktree.unpublished_work(path, branch=branch, base=base)]
            ahead = await worktree.commits_ahead(path, base)
            held.append(f"{ahead} commit(s) ahead of {base}" if ahead else "")
            if any(held):
                trees.append((path, branch, "; ".join(h for h in held if h)))
        if tree:
            trees = [t for t in trees if tree in (t[0], os.path.basename(t[0]))]
        if not trees:
            named = f" named {tree!r}" if tree else ""
            stray = f" ({', '.join(strays)}: not a worktree of {repo}, left untouched)" if strays else ""
            return {**rec, "detail": f"no worktree of {fid}{named} has changes vs {base}{stray} — nothing to publish"}
        if len(trees) > 1:
            listing = "; ".join(f"{os.path.basename(p)}: {held}" for p, _b, held in trees)
            return {**rec, "detail": f"{len(trees)} worktrees of {fid} have changes — pick one with `tree`: {listing}"}
        path, branch, held = trees[0]
        canon = os.path.join(repo, self.root, worktree.worktree_dir(fid, title))
        rec = {**rec, "worktree": path}
        async with worktree.hold_trees(path, canon):
            # The gate runs where the tree IS, before anything moves: a red gate refuses with
            # the tree where the dead coder left it — bar the formatter's fixups, which run
            # first and may already have rewritten files.
            gate_out = await self._gate_tree(f, path)
            if gate_out is not None:
                rec = {**rec, "gate_output": gate_out[-_SALVAGE_GATE_TAIL_CHARS:]}
                if not force:
                    return {
                        **rec,
                        "outcome": "gate-red",
                        "detail": "the pre-PR gate failed on this tree, so nothing was published — fix it, "
                        "or pass force=true to open it as a draft carrying the gate output",
                    }
            if await asyncio.to_thread(self._cancelled, store, fid):
                return {
                    **rec,
                    "outcome": "cancelled",
                    "detail": f"{fid} was cancelled during the salvage — nothing published",
                }
            try:
                wt, branch = await worktree.promote_worktree(repo, path, branch, fid, self.root, title=title)
                rec = {**rec, "worktree": wt, "branch": branch}
                note = (
                    f"Salvaged from `{os.path.basename(path)}` by the operator — no coder was dispatched "
                    f"(#427). It held: {held}"
                )
                pr_url = await self._open_tree_pr(f, wt, branch, gate_out=gate_out, note=note)
            except worktree.NoChangesError as exc:
                return {**rec, "detail": f"nothing to publish: {exc}"}
            except worktree.WorktreeError as exc:
                if str(exc).startswith("gh pr create failed"):  # the push already landed
                    return {
                        **rec,
                        "outcome": "error",
                        "detail": f"pushed {rec['branch']}, but no PR could be opened for it ({exc}) — open one by "
                        f"hand: gh pr create --head {rec['branch']} --base {base}",
                    }
                return {**rec, "outcome": "error", "detail": str(exc)}
            rec = {**rec, "pr_url": pr_url}
            if await asyncio.to_thread(self._cancelled, store, fid):
                ok, why = await worktree.close_pr(pr_url, comment=cancel_pr_comment(fid), cwd=repo)
                closed = "closed it" if ok else f"could NOT close it ({why[:160]}) — close it by hand"
                return {**rec, "outcome": "cancelled", "detail": f"{fid} was cancelled while {pr_url} opened; {closed}"}
            draft_note = ""
            if gate_out is not None:
                rec["draft"] = (await worktree.pr_merge_info(pr_url, cwd=repo)).get("isDraft") is True
                existing = bool(str(f.get("pr_url") or "").strip())
                if existing or not rec["draft"]:
                    # The body of an existing PR never changes, and a PR that is not a draft
                    # must at least say why it should not merge: the output goes on it.
                    posted = await worktree.post_or_update_pr_comment(
                        pr_url, _gate_failure_block(gate_out), marker=_SALVAGE_GATE_MARKER, cwd=repo
                    )
                    draft_note = (
                        "; the gate output is posted on the PR" if posted else "; posting the gate output FAILED"
                    )
                draft_note = (
                    " — as a DRAFT (the pre-PR gate failed)"
                    if rec["draft"]
                    else " — NOT a draft: GitHub refused the conversion"
                ) + draft_note
        try:
            # Under the claim lock, so the ready-queue scan cannot claim the card in the
            # moment between clearing its block and moving it on. A card blocked out of
            # in_review (its PR already open) goes straight back there; one parked
            # in_progress enters review through open_review, as a drive's does.
            async with self._claim_guard():
                cur = await asyncio.to_thread(store.get_feature, fid) or {}
                if cur.get("blocked"):
                    cur = await asyncio.to_thread(store.clear_blocked, fid) or {}
                if cur.get("board_state") == "ready":  # blocked after a requeue: take it back
                    cur = await asyncio.to_thread(store.claim, fid, assignee=self.coder_name) or {}
                if cur.get("board_state") == "in_progress":
                    await asyncio.to_thread(store.open_review, fid, pr_url=pr_url)
                elif cur.get("board_state") != "in_review":
                    raise BoardError(f"{fid} is {cur.get('board_state') or 'unreadable'}, not stranded any more")
        except Exception as exc:  # noqa: BLE001 — the PR is open; say so rather than raise
            return {
                **rec,
                "outcome": "error",
                "detail": f"published {pr_url}, but could not put {fid} in review: {exc}",
            }
        done = f"salvaged by operator: {os.path.basename(path)} → {pr_url}, no coder dispatched{draft_note}"
        try:
            await asyncio.to_thread(store.comment, fid, done)
        except Exception:  # noqa: BLE001 — the trail is best-effort; the PR is the record
            log.warning("[project_board] %s salvage comment failed", fid, exc_info=True)
        log.info("[project_board] %s %s", fid, done)
        if self.review_gate:
            # Inline, as a drive runs it — the reconcile's resume edge only ever runs with
            # merge_poll on, so leaving it `review-pending` could leave it unreviewed.
            await self._review_gate(store, fid, pr_url, repo)
        return {**rec, "outcome": "published", "detail": done}

    async def _recover(self):
        """On boot, reconcile every ``in_progress`` feature the previous run left
        mid-drive (a drive doesn't survive a restart). ``in_review`` features are NOT
        touched — they have a PR and the webhook/poll resolves them. Also releases the
        previous run's orphaned preflight holds (#186) — see
        ``_recover_preflight_holds``."""
        store = self._store()
        for f in await asyncio.to_thread(store.list_features, state="in_progress"):
            if f["id"] in self._inflight_files:
                continue  # reserved — an operator salvage got here before recovery did (#427)
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

    async def _list_for_pass(self, store, state: str, pass_name: str) -> list[dict]:
        """The ``state`` rows one pass of the reconcile/sweep acts on — or none, when the
        read itself failed (#404). A pass drives several independent edges off separate
        reads (the PR reconcile scans in_review AND blocked), and one read that stalled
        must cost only the cards it would have returned: the pass carries on with the
        rest, and the skipped cards are read again on its next turn. Only a BoardError
        (a `br` call that failed) is absorbed. A STALL is not: the store did not answer,
        and the next read would stall too, so a ``BoardTimeout`` goes up to the tick, which
        skips the rest of that tick. Anything else is a bug and is left to the tick's phase
        guard, traceback intact."""
        try:
            return await asyncio.to_thread(store.list_features, state=state)
        except store_mod.BoardTimeout:
            raise  # a STALLED store: the tick stops here, rather than stall on the next read too
        except BoardError as exc:
            log.warning("[project_board] %s: could not read the %s cards, skipped this pass: %s", pass_name, state, exc)
            return []

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
        (#115) — the board's growth valve; archival only, nothing is ever deleted. Last,
        it publishes the agent's working-state snapshot, which names any card stranded
        outside the ready lane with every dependency closed (#406) — surfaced, never moved.
        Best-effort; a per-item failure never stops the sweep or the loop. A stalled store
        ends the sweep, and the tick with it (#404)."""
        store = self._store()
        for f in await self._list_for_pass(store, "in_progress", "health sweep"):
            fid = f["id"]
            if fid in self._inflight_files:
                continue  # a live drive owns it
            try:
                log.info("[project_board] sweep: %s in_progress with no live drive", fid)
                await self._reconcile_orphan(fid)
            except store_mod.BoardTimeout:
                raise  # a stalled store, not one card's failure: stop, don't try the next card on it
            except Exception:  # noqa: BLE001
                log.warning("[project_board] sweep reconcile for %s failed", fid, exc_info=True)
        # #90: reap orphaned worktrees across EVERY project's checkout, not just the
        # instance default — a multi-repo board holds feat-<id> worktrees under each
        # project's repo, and a worktree resolved in repo A must be reaped in repo A.
        for repo in self._all_repos():
            await self._sweep_worktrees(store, repo)
        # (a2) bring each board-owned base checkout up to origin/<base> where that is safe,
        # and report the ones that can't be (#452). Its own task: network-bound, so it must
        # never hold the tick (#462). Never touches the board store.
        self._start_base_refresh()
        # (a3) cards whose project no longer resolves (#454), for /status.
        await self._publish_orphaned_cards(store)
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
        except store_mod.BoardTimeout:
            raise
        except Exception:  # noqa: BLE001
            log.warning("[project_board] sweep archive pass failed", exc_info=True)
        # (d) the host's <working_state> snapshot, LAST, so it carries this sweep's own
        # reconciles and unblocks. Published first, as it used to be, it showed a card the
        # sweep had just moved in the state it had left, for a whole interval (#401). The
        # refresher would pick those writes up within seconds anyway; publishing here makes
        # the sweep's result current the moment it ends. Its stall is the sweep's stall.
        await self._publish_work_snapshot(stall_ends_tick=True)

    # ── the host's <working_state> snapshot (ADR 0079 Observe, #401) ──────────
    def _take_work_snapshot(self, store) -> None:
        """Read the open cards and publish them for the host's working-state block.

        The revision is read BEFORE the board, so a write that lands mid-read leaves the
        snapshot marked stale (and re-read soon) rather than claiming a state its rows may
        predate. The read is ``live_cards``, the light one: open statuses only, no
        whole-board ``br show``. Rows are annotated as board_list's are, so a card's hint is
        the board's own next action. A blocked card, ranked first, gets its block reason and
        what moves it, which the posture hints leave blank. A card newly stranded outside
        the ready lane is logged (``_report_stranded``)."""
        revision = work_snapshot.board_revision()
        features = store_mod.annotate_next_action(store.live_cards(), self.cfg)
        # The claim-stall signal's "is there ready work?" (#462), from the read this already makes.
        self._ready_count = sum(1 for f in features if f.get("board_state") == "ready" and not f.get("blocked"))
        for f in features:
            if f.get("blocked") and not f.get("next_action_hint"):
                cls = str(f.get("blocked_class") or "").strip()
                reason = str(f.get("blocked_reason") or "").strip() or "no reason recorded"
                who = (
                    f"retries on its own ({cls})"
                    if cls in _SELF_HEALING_BLOCKS
                    else f"held until {PREFLIGHT_HOLD_STEP} ({cls})"
                    if cls == PREFLIGHT_HOLD_CLASS
                    else f"needs a human ({cls or 'unclassified'})"
                )
                f["next_action_hint"] = f"{who}: {reason}"
        work_snapshot.publish(features, revision=revision)
        self._report_stranded(features)

    async def _publish_work_snapshot(self, *, stall_ends_tick: bool = False) -> bool:
        """``_take_work_snapshot``, off the event loop and best-effort. Returns whether it
        published. A failed read backs the refresher off (see ``_work_snapshot_due``), and
        the provider keeps marking the old snapshot stale meanwhile.

        A ``BoardError`` (a `br` call that failed, or stalled and was stopped) is an
        understood, transient outcome, logged as ONE line like ``_tick_phase`` logs it;
        anything else is a bug and keeps its traceback. The exception is a stall inside a
        tick (``stall_ends_tick``, the sweep's publish): it goes up to ``_tick_phase``, which
        ends the tick on it (#404). Swallowed there, the tick went on to the preflight and the
        claim scan, and each stalled again on the same wedged store.

        One read at a time: the refresher and the sweep can both get here, and a second read
        while one is in flight is skipped. The one in flight publishes, and a write since it
        began leaves that snapshot STALE for the refresher to re-read."""
        if self._snapshot_reading:
            return False
        self._snapshot_reading = True
        self._snapshot_attempted_at = time.monotonic()
        try:
            await asyncio.to_thread(self._take_work_snapshot, self._store())
        except BoardError as exc:
            self._snapshot_failures += 1
            if stall_ends_tick and isinstance(exc, store_mod.BoardTimeout):
                raise
            log.warning("[project_board] work snapshot refresh failed (retrying with backoff): %s", exc)
            return False
        except Exception:  # noqa: BLE001 — never let a snapshot refresh stop the loop
            self._snapshot_failures += 1
            log.warning("[project_board] work snapshot refresh failed (retrying with backoff)", exc_info=True)
            return False
        finally:
            self._snapshot_reading = False
        self._snapshot_failures = 0
        return True

    def _work_snapshot_due(self) -> bool:
        """Whether the refresher should read now. In memory, no I/O.

        - Never inside ``MIN_INTERVAL_S`` of the last attempt. A burst of writes (a claim,
          its labels, a drive's budget stamps) costs ONE read, not one each.
        - After failures, wait out an exponential backoff (``MIN_INTERVAL_S`` doubling, up to
          ``MAX_AGE_S``). A store that is failing is not hammered.
        - Otherwise read when the board changed since the snapshot, when there is none yet,
          or when it is older than ``MAX_AGE_S``. The last case is what bounds a writer this
          process can't see."""
        since = time.monotonic() - self._snapshot_attempted_at
        wait = work_snapshot.MIN_INTERVAL_S
        if self._snapshot_failures:
            wait = min(work_snapshot.MIN_INTERVAL_S * 2**self._snapshot_failures, work_snapshot.MAX_AGE_S)
        if since < wait:
            return False
        taken = work_snapshot.taken_at()
        return work_snapshot.needs_refresh() or taken is None or time.time() - taken >= work_snapshot.MAX_AGE_S

    async def _keep_work_snapshot_current(self) -> None:
        """The snapshot's refresher, a task of its own (started with the loop). It is NOT part
        of the claim tick. A loop paused at its setup gate (a missing coder, say) runs no
        ticks, and a snapshot refreshed only by ticks stayed STALE for as long as the pause
        lasted. It polls an in-memory check every ``_SNAPSHOT_POLL_S`` and reads only when
        ``_work_snapshot_due`` says so."""
        while not self._stop.is_set() and not self._shutting_down:
            if self._work_snapshot_due():
                await self._publish_work_snapshot()
            # Off the tick on purpose (#462): a tick stuck in one phase can't report its own stall.
            self._check_claim_stall()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_SNAPSHOT_POLL_S)
            except asyncio.TimeoutError:
                pass

    def _report_stranded(self, feats: list[dict]) -> None:
        """Log each card that has just become stranded outside the ready lane, with every
        dependency closed (#406). ``store.stranded_posture`` found them, and they read so
        on every listing and in the agent's working state. This line is the loop's own
        record of WHEN it first saw each one: once per card, not once per snapshot read, and
        again only if the card leaves that state and comes back. Nothing is changed on the
        card. A backlog card is promoted by the PM, and a block is lifted by whoever set it."""
        stranded = {
            f["id"]: f
            for f in feats
            if f.get("id")
            and f.get("next_action") in (store_mod.NEXT_ACTION_DEPS_CLEARED, store_mod.NEXT_ACTION_BLOCKED_DEPS_CLEARED)
        }
        seen = getattr(self, "_stranded_seen", set())
        for fid in sorted(set(stranded) - seen):
            f = stranded[fid]
            log.info("[project_board] %s stranded (%s): %s", fid, f["board_state"], f.get("next_action_hint"))
        self._stranded_seen = set(stranded)

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
        except store_mod.BoardTimeout:
            raise  # a stalled store: the tick stops (#404)
        except Exception:  # noqa: BLE001
            log.warning("[project_board] blocked sweep: could not list blocked features", exc_info=True)
            return
        for f in blocked:
            fid = f["id"]
            if fid in self._inflight_files:
                continue  # reserved — an operator salvage is publishing it (#427); leave it be
            try:
                cls = str(f.get("blocked_class") or "").strip()
                # The reason rides a COMMENT. `br list` carries none, but since #416 the
                # listing copies the thread across for blocked rows, so it is normally
                # here already. If it is still empty, the one card being escalated is
                # re-read through get_feature (`br show`): escalating "no reason recorded"
                # tells the operator nothing and sends them digging, which is the thing
                # this alert exists to prevent. Escalation path only, once per card, never
                # a per-row probe across the whole blocked lane.
                reason = str(f.get("blocked_reason") or "").strip()
                spent = await self._budget_get(store, fid, "unblock-retry", f)
                # Never for a card blocked before it was ever ready (#406): the self-heal
                # REQUEUES, and for a card that never passed the Ready gate that promotes it
                # straight past it. Such a block was set by hand (the loop blocks only ready
                # and in-flight cards) — a hand block written before hand blocks were always
                # terminal still carries the class its wording guessed, `transient` for
                # "waiting on the network team". It goes to a human instead.
                by_hand = store_mod.blocked_before_ready(f)
                if cls in _SELF_HEALING_BLOCKS and spent < _UNBLOCK_RETRY_MAX and not by_hand:
                    # Re-read under the claim lock before moving the card (#402). The list is
                    # from the start of the pass, and an attach (or an operator unblock) may
                    # have moved the card since. Requeueing it then undid that move. A salvage
                    # that reserved it since (#427) owns it the same way.
                    async with self._claim_guard():
                        fresh = await asyncio.to_thread(store.get_feature, fid) or {}
                        if fid in self._inflight_files or (
                            fresh.get("board_state")
                            and (not fresh.get("blocked") or str(fresh.get("blocked_class") or "").strip() != cls)
                        ):
                            log.info(
                                "[project_board] blocked sweep: %s changed since the pass began (now %s) — left alone",
                                fid,
                                fresh.get("board_state") or "gone",
                            )
                            continue
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
                    f"{cls} block set before the card was ever ready, so never auto-cleared"
                    if by_hand and cls in _SELF_HEALING_BLOCKS
                    else f"{cls or 'unclassified'} block"
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
                # #406: a card blocked in backlog whose every dependency has since closed.
                # Its block may have been nothing BUT that wait, and nothing clears a block
                # for you, so this is the moment whoever set it needs to hear about it.
                stranded = store_mod.stranded_posture(f)["next_action_hint"]
                # A preflight hold (#3585) DOES clear itself — once its project's gate runs.
                # The operator still hears about it (the environment needs them), but not as
                # a card that is stuck for good.
                head = (
                    f"Board card {fid} is held by its project's gate preflight ({PREFLIGHT_HOLD_STEP})"
                    if cls == PREFLIGHT_HOLD_CLASS
                    else f"Board card {fid} is blocked and will not clear itself ({why})"
                )
                self._notify_operator(
                    fid,
                    f"{head}: "
                    f"{reason or 'no reason recorded'}"
                    + (f" — {title}" if title else "")
                    + (f". Note: {stranded}" if stranded else ""),
                    # The recovery CYCLE is part of the incident's identity (#346 r7): a
                    # card that auto-healed, rebuilt and failed the SAME way again is a new
                    # failed cycle and IS news — the self-heal did not work. Keying on
                    # class+reason alone suppressed exactly that for the whole window.
                    # `spent` is the unblock-retry budget the self-heal already tracks, so
                    # this costs no new state: it increments on every auto-unblock and is
                    # therefore different on each side of a recovery. Dependencies closing
                    # under a block is news in the same way (#406), so it is part of the
                    # identity too — ONE more alert, when the last one closes.
                    incident=f"{cls}|{reason}|{spent}" + ("|deps-closed" if stranded else ""),
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
            drive = _loop.live_drive(fid)
            if (
                fid in self._inflight_files
                or drive is not None
                or fid in self._card_tasks
                or fid in self._review_inflight
            ):
                # Held by the loop (#461): a drive — stalled or not — a salvage, or this card's
                # reconcile/merge gate/review. Never an orphan. A drive still running for a card
                # that has since closed is the one exception, and it is retired, not reaped.
                await self._retire_dead_drive(store, fid, drive)
                continue
            try:
                f = await asyncio.to_thread(store.get_feature, fid)
                if f is None and not worktree.wt_id_is_exact(repo, self.root, wtid):
                    # The id came out of a SLUGGED dir name, and the store has no such card.
                    # That is a parse we cannot trust, not proof of an orphan (#461: every
                    # `ds-` board's live trees were reaped this way). Keep it; say so once.
                    unknown = getattr(self, "_sweep_unknown_trees", None)
                    if unknown is None:
                        unknown = self._sweep_unknown_trees = set()
                    if (repo, wtid) not in unknown:
                        unknown.add((repo, wtid))
                        log.warning(
                            "[project_board] sweep: kept worktree feat-%s… under %s — its card id could not be "
                            "resolved from the directory name, so it is not treated as orphaned (#461)",
                            wtid,
                            repo,
                        )
                    continue
                if f is None or f["board_state"] in ("done", "cancelled"):
                    # The last word before a reap (#461): a process working in the tree — a
                    # coder whose drive the registry lost, a gate, an operator's shell — means
                    # it is in use, whatever the board says. Kept; the next sweep looks again.
                    trees = [p for p, _b in worktree.feature_worktrees(repo, self.root, wtid)]
                    busy = await worktree.processes_in_trees(trees)
                    if busy:
                        n = self._busy_keeps.get(wtid, 0) + 1
                        self._busy_keeps[wtid] = n
                        (log.warning if n <= _REAP_WARN_CAP else log.debug)(
                            "[project_board] sweep: kept worktree feat-%s — process(es) %s still working in %s (%d)",
                            wtid,
                            ", ".join(str(pid) for pids in busy.values() for pid in pids),
                            ", ".join(busy),
                            n,
                        )
                        continue
                    self._busy_keeps.pop(wtid, None)
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

    async def _retire_dead_drive(self, store, fid: str, drive) -> None:
        """Cancel a drive still running for a card that is done, cancelled or gone (#461).
        Its tree is not reaped here: the drive's own cancel edge saves and removes it, and a
        leftover is reaped by a later sweep once no drive holds it. Best-effort."""
        if drive is None or drive.done():
            return
        try:
            f = await asyncio.to_thread(store.get_feature, fid)
        except store_mod.BoardTimeout:
            raise
        except Exception:  # noqa: BLE001 — unreadable: leave the drive be
            return
        if f is not None and f.get("board_state") not in ("done", "cancelled"):
            return
        log.warning(
            "[project_board] sweep: %s is %s but its drive is still running — cancelling the drive; its "
            "worktree is reaped once it has stopped",
            fid,
            (f or {}).get("board_state") or "gone",
        )
        drive.cancel()

    # ── the PR reconcile (terminal-edge fallback to the webhook) ───────────────
    async def _maybe_reconcile(self):
        """Run the PR reconcile at most once per ``merge_poll_interval`` (and only when
        enabled) — cheap, but no reason to hammer ``gh`` every busy tick.

        A store stall inside a detached card reconcile (#462) ends the NEXT tick here, as a
        stall in the inline reconcile used to end its own (#404): the rest of that tick would
        only queue more calls on the wedged store."""
        stall, self._card_stall = self._card_stall, None
        if stall is not None:
            raise stall
        if not self.merge_poll:
            return
        now = time.monotonic()
        if now - self._last_poll < self.merge_poll_interval:
            return
        self._last_poll = now
        # The tick only STARTS the per-card work (#462): each card's reconcile runs as its
        # own tracked task, so a 600 s gate or a hung review can't hold up the claim scan.
        await self._reconcile_prs(detach=True)

    async def _reconcile_prs(self, *, detach: bool = False):
        """Reconcile each ``in_review`` feature against its PR's real state — the
        fallback to the webhook and the active half of the terminal edges (for
        deployments GitHub can't post a webhook to, where a feature would otherwise
        sit in_review forever): ``MERGED`` → done (+reap); ``CLOSED`` unmerged →
        Blocked for triage (+reap; the work was rejected, don't silently re-dispatch);
        ``OPEN`` → leave it in review.

        Each card's reconcile runs as its OWN tracked task (#462), at most
        ``reconcile_concurrency`` at once, and a card whose previous pass is still running
        is skipped. ``detach`` (the tick) returns once they are started: before, one card's
        600 s merged-state gate or a hung review call held the whole tick, and the board
        claimed nothing for four hours with 32 cards ready. Without it (a direct call) the
        pass is awaited to the end, and a stalled store still raises out of it."""
        store = self._store()
        # #196: blocked cards can carry a PR too (review-verify blocks, closed-PR triage,
        # manual flags) — a merged PR is ground truth for them exactly as for in_review,
        # and scanning only in_review left merged-but-blocked cards stuck forever. They
        # take ONLY the MERGED edge below: CLOSED would rewrite their blocked reason, and
        # the OPEN-branch gates (rebase/CI/review) must not run against held work.
        in_review = await self._list_for_pass(store, "in_review", "PR reconcile")
        blocked = await self._list_for_pass(store, "blocked", "PR reconcile")
        started = [t for t in (self._start_card_work(store, f) for f in [*in_review, *blocked] if f.get("pr_url")) if t]
        if detach or not started:
            return
        for outcome in await asyncio.gather(*started, return_exceptions=True):
            if isinstance(outcome, store_mod.BoardTimeout):
                raise outcome

    def _start_card_work(self, store, f: dict):
        """Start ``f``'s reconcile as a tracked task, or None while its last one still runs."""
        fid = f["id"]
        tasks = self._card_tasks
        running = tasks.get(fid)
        if running is not None and not running.done():
            return None
        task = asyncio.create_task(self._card_work(store, f), name=f"pb-card-{fid}")
        tasks[fid] = task

        def _done(t, fid=fid):
            if tasks.get(fid) is t:
                tasks.pop(fid, None)
            if not t.cancelled():
                t.exception()  # a detached card's stall is logged in _card_work; mark it seen

        task.add_done_callback(_done)
        return task

    def _repo_lock(self, repo: str) -> asyncio.Lock:
        """The lock that keeps ONE repo's card reconciles serial (#471 review). Two PRs of one
        repo reconciled at once could both auto-merge on a merged-state stamp the first merge
        had just made stale (#131): the stamp is checked against base, then gh round trips,
        then the merge — and the sibling's merge moves base in between. Serial within a repo
        is what the old one-pass loop guaranteed; concurrency is across repos only."""
        lock = self._repo_locks.get(repo)
        if lock is None:
            lock = self._repo_locks[repo] = asyncio.Lock()
        return lock

    def _card_held(self, fid: str) -> str:
        """What else in the loop is working ``fid`` right now, or "" (#471 review)."""
        if _loop.live_drive(fid) is not None or fid in self._inflight_files:
            return "a drive holds it"
        if fid in self._review_inflight:
            return "its review gate is running"
        return ""

    async def _card_work(self, store, f: dict):
        """One card's reconcile: inside a ``reconcile_concurrency`` slot AND its repo's lock
        (``_repo_lock``), so one repo's cards run one at a time and different repos in
        parallel. A card that has to QUEUE first settles a merged/closed PR cheaply (it must
        not wait behind a hung review to be marked done) and, once it gets its turn, is
        re-read: the snapshot it was started from may be minutes old, and in that time the
        card may have been requeued and claimed, blocked, or re-pointed at another PR. A
        stall is recorded for the next tick to end on (#404) and re-raised for an awaiting
        caller."""
        fid = f["id"]
        repo = self._repo_for(f)
        if self._card_slots is None:
            self._card_slots = asyncio.Semaphore(self.reconcile_concurrency)
        lock = self._repo_lock(repo)
        try:
            if self._card_slots.locked() or lock.locked():
                try:
                    state = await worktree.pr_state(f["pr_url"], cwd=repo)
                except Exception:  # noqa: BLE001 — unread: take the ordinary path
                    state = ""
                if state in ("MERGED", "CLOSED"):
                    if not self._card_held(fid):
                        await self._reconcile_pr(store, f, known_state=state)
                    return
                async with self._card_slots, lock:
                    fresh = await asyncio.to_thread(store.get_feature, fid)
                    if (
                        not fresh
                        or fresh.get("board_state") not in ("in_review", "blocked")
                        or fresh.get("pr_url") != f.get("pr_url")
                    ):
                        log.info(
                            "[project_board] %s moved while its reconcile queued (now %s) — skipped this pass",
                            fid,
                            (fresh or {}).get("board_state") or "gone",
                        )
                        return
                    held = self._card_held(fid)
                    if held:
                        log.info("[project_board] %s reconcile skipped this pass — %s", fid, held)
                        return
                    await self._reconcile_pr(store, fresh)
                return
            async with self._card_slots, lock:
                held = self._card_held(fid)
                if held:
                    log.info("[project_board] %s reconcile skipped this pass — %s", fid, held)
                    return
                await self._reconcile_pr(store, f)
        except store_mod.BoardTimeout as exc:
            log.warning(
                "[project_board] reconcile for %s stalled on the board store — the next tick ends on it: %s", fid, exc
            )
            self._card_stall = exc
            raise

    async def _reconcile_pr(self, store, f: dict, *, known_state: str = ""):
        """The per-card body of ``_reconcile_prs`` (#462), unchanged: a one-card loop, so
        its ``continue`` still means "nothing further for this card this pass"."""
        for f in (f,):  # noqa: B020 — see the docstring
            fid = f["id"]
            pr_url = f.get("pr_url")
            if not pr_url:
                continue
            # #90: reconcile each PR against ITS project's checkout, not the board default.
            repo = self._repo_for(f)
            try:
                # ONE read for the PR's state AND the external panel's verdict (#473): the
                # state rides the same `gh pr view` as the head, reviews and checks. Unreadable
                # → the plain state read, and no external verdict this pass (fail open).
                view = None if known_state else await worktree.pr_review_state(pr_url, cwd=repo)
                state = known_state or str((view or {}).get("state") or "") or await worktree.pr_state(pr_url, cwd=repo)
                if f.get("board_state") == "blocked" and state != "MERGED":
                    continue
                if state in ("MERGED", "CLOSED"):
                    merge_state_hold.clear_hold(fid)  # #495: a finished PR holds nothing
                    review_coverage_hold.clear_hold(fid)
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
                    # FIRST (#473): has the repo's own QA panel FAILED this PR at its current
                    # head? Then the PR cannot merge as it stands, whatever the board's own gate
                    # said. Bounce it into a fix round (or hold it for a human), and skip every
                    # edge below: re-verifying the merged state, rebasing, bouncing on CI or
                    # merging a PR the panel has rejected spends budget on nothing.
                    if await self._reconcile_external_review(store, f, pr_url, repo, view=view):
                        continue
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
                        continue  # blocked on a red merged-state gate (or the card left review) → nothing further
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
                    # The INBOUND half of the review gate (#323/#512): a trusted EXTERNAL-panel
                    # PASS for the PR's CURRENT head repairs a stale changes-requested or an
                    # absent local review verdict to review-clean, so the ordinary merge gates
                    # can proceed — unless the board's OWN gate FAILED that head, in which case
                    # the strictest verdict wins and the card is held for an operator (#512).
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
                        await self._maybe_auto_merge(store, fid, pr_url, repo, view=view)
            except store_mod.BoardTimeout:
                raise  # a stalled store, not this PR's failure: stop the pass (#404)
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
        # LAST, and only for a PR that is otherwise mergeable right now: is the repo
        # mid-release (release_freeze.py)? A merge during a release restarts its checks,
        # so hold — never block, never spend a merge attempt — and re-ask next poll.
        freeze = await self._release_freeze_evidence(feature, pr_url, repo)
        if freeze:
            return [f"{RELEASE_FREEZE_BLOCKER} ({freeze})"]
        return []

    async def _release_freeze_evidence(self, feature: dict, pr_url: str, repo: str) -> str:
        """The freeze evidence for this card's repo ("" = not frozen / check disabled)."""
        patterns = release_freeze.parse_config(self._release_freeze_cfg_for(feature))
        if patterns is None:
            return ""
        slug, _num = worktree._parse_pr(pr_url)
        return await release_freeze.check(slug, repo, patterns, cwd=repo, base=self._base_branch_for(feature))

    def _note_freeze_hold(self, store, fid: str, pr_url: str, why: list[str]) -> None:
        """Record (or clear) the card's release-freeze hold — the process state the
        listing reads for `held: release freeze (…)` — logging and commenting ONCE per
        hold, not once per poll. Best-effort: bookkeeping never breaks the reconcile."""
        held = next((w for w in why if w.startswith(RELEASE_FREEZE_BLOCKER)), "")
        if not held:
            prior = release_freeze.clear_hold(fid)
            if prior:
                log.info(
                    "[project_board] %s release freeze lifted (was: %s) — merge edge open: %s",
                    fid,
                    prior["evidence"],
                    pr_url,
                )
            return
        evidence = held[len(RELEASE_FREEZE_BLOCKER) :].strip().removeprefix("(").removesuffix(")")
        if release_freeze.set_hold(fid, evidence, repo=worktree._parse_pr(pr_url)[0]):
            log.info("[project_board] %s auto-merge HELD — release freeze (%s): %s", fid, evidence, pr_url)
            try:
                store.comment(
                    fid,
                    f"auto-merge held: release freeze ({evidence}). The repo is preparing a release; a merge "
                    f"now would restart its checks. The loop re-checks every merge poll and merges when it "
                    f"lifts: {pr_url}",
                )
            except Exception:  # noqa: BLE001 — bookkeeping must not break the reconcile
                log.warning("[project_board] %s freeze-hold comment failed", fid, exc_info=True)

    def _note_merge_state_hold(self, fid: str, pr_url: str, why: list[str], view: dict | None) -> None:
        """Record (or clear) why GitHub does not read this PR as CLEAN (#495) — the process
        state ``annotate_next_action`` reads to say ``held: PR not clean on GitHub
        (UNSTABLE) — QA panel: in progress`` instead of ``auto-merge pending``. Built from
        reads the pass already made: the ``mergeStateStatus`` in ``why`` and the head's
        checks in the pass's ``pr_review_state`` payload. No GitHub call of its own.

        UNSTABLE / BLOCKED set the hold; an UNKNOWN or unreadable status keeps whatever was
        there (GitHub computing, a gh blip); any other outcome clears it. Logged once per
        distinct blocker — BLOCKED on pending required checks is every fresh PR's normal
        state, so the bead is not commented and the operator is not paged for it."""
        prefix = "github mergeStateStatus="
        mss = next((w[len(prefix) :] for w in why if w.startswith(prefix)), None)
        if mss in ("UNKNOWN", "unavailable"):
            return
        if mss not in merge_state_hold.HELD_STATUSES:
            merge_state_hold.clear_hold(fid)
            return
        checks = merge_state_hold.outstanding_checks(view)
        if merge_state_hold.set_hold(fid, mss, checks, pr_url):
            log.info(
                "[project_board] %s auto-merge held: PR is %s on GitHub (%s): %s",
                fid,
                mss,
                "; ".join(checks) or "no pending or failing check in the pass's read",
                pr_url,
            )

    async def _maybe_auto_merge(self, store, fid: str, pr_url: str, repo: str, *, view: dict | None = None) -> bool:
        """Merge an in_review PR once every gate the loop owns is green and current
        (see ``_auto_merge_blockers``). Returns True if it merged. The board flips to
        done on the next reconcile pass (the existing MERGED edge — one Done path,
        idempotent, webhook-compatible). A refusal is retried next pass up to
        ``auto_merge_max`` times, then recorded on the bead and left for a human —
        never a block: the work is good, only the merge didn't land. ``view`` is the
        pass's ``pr_review_state`` read, used only to name the checks holding a PR that
        GitHub does not read as CLEAN (#495)."""
        feature = await asyncio.to_thread(store.get_feature, fid)
        if feature is None:  # card deleted between the reconcile snapshot and this re-read
            log.debug("[project_board] %s vanished before auto-merge — nothing to merge", fid)
            return False
        self._refresh_coverage_hold(fid, feature, view)
        why = await self._auto_merge_blockers(store, feature, pr_url, repo)
        self._note_merge_state_hold(fid, pr_url, why, view)
        if why:
            # Store-only bookkeeping (a bead comment) — off the event loop (#258).
            await asyncio.to_thread(self._note_draft_hold, store, fid, pr_url, why)
            await asyncio.to_thread(self._note_freeze_hold, store, fid, pr_url, why)
            log.debug("[project_board] %s not auto-merging: %s", fid, "; ".join(why))
            return False
        await asyncio.to_thread(self._note_freeze_hold, store, fid, pr_url, why)
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
        # Re-read right before the merge (#471 review): the gh round trips above take seconds,
        # and a requeue, a hold or a claimed fix round in that time makes this card no longer
        # the one the gates were read for.
        fresh = await asyncio.to_thread(store.get_feature, fid)
        if (
            not fresh
            or fresh.get("board_state") != "in_review"
            or _loop.live_drive(fid) is not None
            or fid in self._inflight_files
        ):
            log.info("[project_board] %s not merging: the card moved during the merge checks", fid)
            return False
        # Defence in depth (#473): the pass's external-review step ran minutes ago, and the
        # panel may have FAILED this head since. Ask again, pinned to the head about to
        # merge, and hold (no merge attempt spent) on a FAIL there. Unreadable → merge as
        # before: GitHub's own required-review rules still stand behind it.
        ext_cfg = self._external_review_cfg_for(feature)
        require_complete = ext_cfg is not None and self._require_complete_review_for(feature)
        if not require_complete:
            review_coverage_hold.clear_hold(fid)
        if ext_cfg is not None:
            view = await worktree.pr_review_state(pr_url, cwd=repo)
            ext = external_review.evaluate(view, ext_cfg) if view else None
            if ext is None and require_complete:
                # `require_complete_review` asks for PROOF the head was fully reviewed; an
                # unreadable panel is not that. Hold this pass (no attempt spent), re-ask next.
                log.info(
                    "[project_board] %s not merging: require_complete_review is on and the panel's "
                    "verdict is unreadable this pass: %s",
                    fid,
                    pr_url,
                )
                return False
            if ext is not None and ext.failed and (not merge_head or ext.head == merge_head):
                log.info(
                    "[project_board] %s not merging: the external review FAILED head %s (%s): %s",
                    fid,
                    ext.head[:12],
                    external_review.fail_summary(ext),
                    pr_url,
                )
                return False
            if ext is not None and merge_head and ext.head != merge_head:
                log.info(
                    "[project_board] %s not merging: the head moved to %s under the merge: %s",
                    fid,
                    ext.head[:12],
                    pr_url,
                )
                return False
            if require_complete and ext is not None:
                if ext.incomplete:
                    await self._hold_incomplete_review(store, feature, pr_url, repo, ext)
                    return False
                if review_coverage_hold.clear_hold(fid):
                    log.info(
                        "[project_board] %s the panel's pass at %s is complete — the merge edge is open: %s",
                        fid,
                        ext.head[:12],
                        pr_url,
                    )
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
            # Reap the worktree BEFORE the remote branch goes. Deleting it drops our
            # `origin/<branch>` tracking ref too, and that ref is how the reap knows the
            # tree's commits are published — without it a squash-merged card's commits
            # look like nobody's and get saved to a `stranded/` branch for nothing (#405).
            await worktree.reap_feature_worktree(repo, self.root, fid)
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

    def _refresh_coverage_hold(self, fid: str, feature: dict, view: dict | None) -> None:
        """Drop a stale incomplete-review hold from the pass's own read, no GitHub call: the
        project turned ``require_complete_review`` off, the head moved, or the panel's word
        at the head is no longer an incomplete pass. Only the merge edge's final check SETS
        the hold (and summons), so a card held for another reason reads that reason."""
        hold = review_coverage_hold.hold_for(fid)
        if not hold:
            return
        ext_cfg = self._external_review_cfg_for(feature)
        if ext_cfg is None or not self._require_complete_review_for(feature):
            review_coverage_hold.clear_hold(fid)
            return
        ext = external_review.evaluate(view, ext_cfg) if view else None
        if ext is None:
            return  # nothing read this pass — keep what the last read said
        if ext.head != hold["head"] or not ext.incomplete:
            review_coverage_hold.clear_hold(fid)

    async def _hold_incomplete_review(self, store, feature: dict, pr_url: str, repo: str, ext) -> None:
        """Hold the merge on an incomplete panel pass at the head (``require_complete_review``)
        and ask the panel, ONCE per head, to review it again.

        The hold is process state (``review_coverage_hold``) that ``annotate_next_action``
        projects as ``awaiting complete review (panel pass was incomplete)``; the card stays
        in_review and no merge attempt is spent. The summon is a PR comment
        ``@<handle> review — <reason>`` posted through ``worktree.post_or_update_pr_comment``
        under a per-head marker. Two layers keep it to one per head: the hold remembers the
        head it summoned for (a 30-second tick never re-posts), and the marker lets a
        restarted process find the earlier comment (an unchanged body is a no-op, never a
        PATCH). A failed post is retried on the next merge poll."""
        fid = feature["id"]
        head = ext.head
        signals = list(ext.incomplete_signals)
        if review_coverage_hold.set_hold(fid, head, signals, pr_url):
            log.info(
                "[project_board] %s auto-merge held: the panel's pass at %s was incomplete (%s) — "
                "require_complete_review is on: %s",
                fid,
                head[:12],
                "; ".join(signals),
                pr_url,
            )
            try:
                await asyncio.to_thread(
                    store.comment,
                    fid,
                    f"auto-merge held: {review_coverage_hold.NEXT_ACTION} at {head[:12]} — "
                    f"{'; '.join(signals)}. require_complete_review is on for this project, so the loop "
                    f"merges only after a complete pass at the head: {pr_url}",
                )
            except Exception:  # noqa: BLE001 — bookkeeping must not break the reconcile
                log.warning("[project_board] %s incomplete-review hold comment failed", fid, exc_info=True)
        handle = self._review_summon_handle_for(feature)
        if not handle or review_coverage_hold.summoned(fid, head):
            return
        posted = await worktree.post_or_update_pr_comment(
            pr_url,
            review_coverage_hold.summon_body(handle, head),
            marker=review_coverage_hold.summon_marker(head),
            cwd=repo,
        )
        if posted:
            review_coverage_hold.mark_summoned(fid, head)
            log.info("[project_board] %s requested a re-review (@%s) of head %s: %s", fid, handle, head[:12], pr_url)
        else:
            log.warning(
                "[project_board] %s could not post the @%s re-review request for %s — retrying next poll: %s",
                fid,
                handle,
                head[:12],
                pr_url,
            )

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
        ``merged_verify_max`` (0 = unlimited), which counts only the runs that reached
        no verdict (a gate that timed out, was killed or could not launch) and red ones:
        a real green verdict RESETS the count (#490), because base moving under a card
        that keeps passing is a busy repo, not a failure. Once spent, re-verification stops and
        the stale stamp stays visible to the adjudicator rather than the loop burning
        a gate run every poll forever — with ``auto_merge`` on that hold is the merge
        edge's, so exhaustion logs a WARNING naming the remedy. A merge conflict is the
        DIRTY/rebase edge's job and an infra error retries next poll — neither burns
        budget nor stamps. So does INFRA in the tree itself: a failed ``setup_cmd``
        install skips the gate, and a gate whose output shows a broken dependency tree
        (``broken_dependency_tree``) is no verdict. Both retry next poll, up to
        ``_MERGED_VERIFY_INFRA_MAX`` in a row, then surface one INFRA warning and count
        as a no-verdict run. Before a red verdict blocks, the PR and card are re-read
        (``_merged_verify_red_is_moot``): a card that merged, closed or left review
        while the gate ran is reported, not blocked. Returns True when it BLOCKED the
        card or found it gone from review (the caller skips the rest of this pass)."""
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
        if self._gate_known_slow(feature, self._local_gate_cmd_for(feature)):
            # The gate timed out last time (#483): its "pass" was fail-open and verified
            # nothing, yet each re-run held this repo's lock for the full timeout, one card
            # after another, on every base move (protoAgent PRs stalled for hours). Record
            # the same outcome a timed-out run would have (the stamp, so the merge edge is
            # not held on it) without running it again, and spend no budget: nothing ran.
            short = base_sha[:_MERGED_VERIFIED_SHA_LEN]
            try:
                await asyncio.to_thread(store.record_merged_verified, fid, short)
            except BoardError:
                return False
            log.info(
                "[project_board] %s merged-state gate skipped — this project's local gate timed out at %ss "
                "(verifies nothing); stamped %s@%s as a timed-out run would: %s",
                fid,
                self.local_gate_timeout,
                base,
                short,
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
            # The merged tree is fresh too: its gate needs the same deps a coder's tree gets.
            # A FAILED install leaves a tree no gate can judge — running one anyway is how a
            # half-built node_modules became "the RESULT is broken" on a green PR — so the
            # gate is skipped and the run is INFRA (below), never a verdict.
            setup_failure = await self._prepare_tree(detail, feature)
            failure = None if setup_failure else await self._run_local_gate(detail, feature)
        finally:
            await worktree.remove_worktree(repo, detail)
        # #490: did the gate reach a verdict, or degrade to "pass" (timeout, killed,
        # unlaunchable)? Only the latter counts toward merged_verify_max.
        no_verdict = self.__dict__.get("_gate_no_verdict", set())
        judged = detail not in no_verdict
        no_verdict.discard(detail)
        gate_infra = self.__dict__.setdefault("_gate_infra", {}).pop(detail, "")
        infra = f"dependency install failed: {setup_failure}" if setup_failure else gate_infra
        short = base_sha[:_MERGED_VERIFIED_SHA_LEN]
        streaks = self.__dict__.setdefault("_merged_verify_infra", {})
        if infra:
            # INFRA — the dependency tree, not the code. Retry next poll with nothing
            # stamped and nothing spent, up to _MERGED_VERIFY_INFRA_MAX in a row; the last
            # says so ONCE, labelled INFRA, and is recorded as a no-verdict run (stamped
            # like a timed-out gate, one unit spent) so a broken install can't reinstall
            # every poll forever. Never a block, never "the RESULT is broken".
            streak = streaks.get(fid, 0) + 1
            if streak < _MERGED_VERIFY_INFRA_MAX:
                streaks[fid] = streak
                log.warning(
                    "[project_board] %s merged-state verify hit INFRA (%d/%d) — %s; not a verdict on the PR, "
                    "nothing stamped or spent, next poll retries: %s",
                    fid,
                    streak,
                    _MERGED_VERIFY_INFRA_MAX,
                    infra,
                    pr_url,
                )
                return False
            streaks.pop(fid, None)
            note = (
                f"INFRA: the merged-state verify (branch + {base}@{short}) could not get a working dependency "
                f"tree {streak} times in a row — latest: {infra}. This is NOT a verdict on the PR: its code was "
                "never judged, and CI still gates. Check the project's setup_cmd / local_gate_cmd install in a "
                "fresh worktree. Recorded as a no-verdict run; the board retries when base moves again."
            )
            log.warning("[project_board] %s %s (%s)", fid, note, pr_url)
            try:
                await asyncio.to_thread(store.comment, fid, note)
            except Exception:  # noqa: BLE001 — the audit trail is best-effort
                log.debug("[project_board] %s could not comment the INFRA note", fid, exc_info=True)
            failure, judged = None, False
        else:
            streaks.pop(fid, None)
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
            if not judged:
                # The gate ran to no verdict (fail-open "pass"): nothing was verified, so
                # this is the run the cap exists for — a gate burned every poll forever.
                await self._budget_set(store, fid, "merged-verify", n + 1)
                log.info(
                    "[project_board] %s merged-state gate reached no verdict against %s@%s (counted %d/%s)",
                    fid,
                    base,
                    short,
                    n + 1,
                    self.merged_verify_max or "unlimited",
                )
                return False
            # A real green verdict (#490). Being moved by siblings' merges is not this
            # card's failure, and on a busy repo it is the normal state: a steady run of
            # green re-verifies must never park the card. Reset instead of spending (only
            # when something was spent, so a clean card pays no store write per poll).
            if n:
                await self._budget_reset(store, fid, "merged-verify")
            log.info(
                "[project_board] %s merged-state gate green — verdict re-verified against %s@%s",
                fid,
                base,
                short,
            )
            return False
        if await self._merged_verify_red_is_moot(store, fid, pr_url, repo, base, short, failure):
            return True  # the card left review while the gate ran — nothing further this pass
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

    async def _merged_verify_red_is_moot(
        self, store, fid: str, pr_url: str, repo: str, base: str, short: str, failure: str
    ) -> bool:
        """A red merged-state gate is about to block — is the card still the one it judged?

        The gate takes minutes, and ``_reconcile_pr`` read the PR as OPEN and the card as
        in_review BEFORE it ran. Re-read both now. The block is for an OPEN PR on an
        in_review card: it stops a merge that would land a broken result. Anything else
        means the world moved under the gate, and a terminal block (which pages a human)
        would be wrong:

        - **PR merged / card done.** The merge happened while the gate ran (designSystem
          ds-xof / ds-h5s, docs-only cards blocked after merging). Blocking cannot un-merge
          the PR, and it rewrites a finished card into a "needs triage" one. The red is
          still worth reporting, because if it is real, ``base`` is broken now. So it is
          reported, as a WARNING and a comment on the card that carry the output, and the
          card is left done. Repairing ``base`` is base CI's job (or a fix card / revert,
          a human's call), not this card's.
        - **PR closed.** The CLOSED edge triages it next poll with its own reason.
        - **Card moved out of in_review** (requeued into a fix round, already blocked,
          cancelled). The block would clobber a state someone else just set.

        An unreadable PR state or card keeps the original behaviour (block): the verdict
        is real and nothing says the card moved. Returns True when the block is moot."""
        try:
            card = await asyncio.to_thread(store.get_feature, fid) or {}
        except Exception:  # noqa: BLE001 — unreadable card: nothing says it moved
            card = {}
        board_state = str(card.get("board_state") or "")
        try:
            state = await worktree.pr_state(pr_url, cwd=repo)
        except Exception:  # noqa: BLE001 — pr_state is documented never to raise; belt and braces
            state = ""
        if state == "MERGED" or board_state == "done":
            what = "the PR merged" if state == "MERGED" else "the card is done"
            note = (
                f"merged-state gate FAILED (branch + {base}@{short}), but {what} while the gate ran, so "
                f"this finished card is NOT blocked: blocking cannot un-merge it. If the failure is real, "
                f"{base} itself is now broken. Check {base}'s CI and open a fix card or revert: {pr_url}\n{failure}"
            )
            log.warning("[project_board] %s %s", fid, note.split("\n", 1)[0])
            try:
                await asyncio.to_thread(store.comment, fid, note)
            except Exception:  # noqa: BLE001 — the audit trail is best-effort
                log.debug("[project_board] %s could not comment the post-merge red", fid, exc_info=True)
            return True
        if state == "CLOSED":
            log.info(
                "[project_board] %s merged-state gate red, but the PR closed while it ran — the closed edge "
                "triages it, not this one: %s",
                fid,
                pr_url,
            )
            return True
        if board_state and board_state != "in_review":
            log.info(
                "[project_board] %s merged-state gate red, but the card moved to %s while it ran — not "
                "blocking over that state: %s",
                fid,
                board_state,
                pr_url,
            )
            return True
        return False

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
        if status == "passing":
            await self._settle_ci_rerun(store, fid, pr_url, repo, feature)
        if status != "failing":
            return
        # #487: a red rollup may be a flake. Rerun its failed Actions jobs once per head
        # before spending a fix round; the next pass sees the rerun's verdict.
        if await self._rerun_ci_once(store, fid, pr_url, repo, feature, summary):
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

    async def _rerun_ci_once(self, store, fid: str, pr_url: str, repo: str, feature: dict | None, summary: str) -> bool:
        """Rerun a red PR's failed GitHub Actions jobs before a coder fix round (#487).

        Returns True when a rerun was started — or when the red run is still running, so
        GitHub can't rerun it yet (wait a pass, unstamped) — the caller then spends nothing
        and requeues nothing, and a later pass reads the rerun's verdict (green → ``_settle_ci_rerun``
        logs the flake; red again at the same head → the bounce below runs as it always has).

        At most ``ci_rerun_max`` reruns per PR head, counted on the bead's
        ``ci-rerun:<sha>:<n>`` label so a restart can't rerun a head again; a new push is a
        new head and gets a fresh allowance. Returns False — bounce exactly as before — when
        reruns are off (``ci_rerun_max: 0``), this head's allowance is spent, the head can't
        be read to prove otherwise, or nothing was rerun (no Actions run behind the red
        checks, e.g. only a non-Actions required status failed, or ``gh`` refused)."""
        cap = int(getattr(self, "ci_rerun_max", 0) or 0)
        if cap <= 0:
            return False
        stamped, used = store_mod.ci_rerun_from_labels((feature or {}).get("labels"))
        head = ""
        if stamped:
            # Only a stamp needs the head up front: without one this is the head's first
            # rerun whatever it is, and the head is read after the rerun to stamp it.
            head = await worktree.pr_head_sha(pr_url, cwd=repo)
            if not head:
                return False  # can't prove this head still has an allowance → bounce as before
            if head[: store_mod.SHORT_SHA_LEN] != stamped:
                used = 0  # a new push since the last rerun: a fresh allowance
            elif used >= cap:
                return False  # this head was already rerun and is red again → a real failure
        busy: list[str] = []
        run_ids = await worktree.rerun_failed_ci(pr_url, cwd=repo, busy=busy)
        if not run_ids and busy:
            # A job failed while the rest of its run is still going: GitHub won't rerun a
            # run until it finishes. That is "not yet", not "nothing to rerun" — wait a pass
            # (no stamp, no fix round spent) and rerun once the run completes.
            waiting = self.__dict__.setdefault("_ci_rerun_waiting", {})
            if waiting.get(fid) != tuple(busy):
                waiting[fid] = tuple(busy)
                log.info(
                    "[project_board] %s CI red but run(s) %s still running — waiting for them to finish before a rerun (%s)",
                    fid,
                    ", ".join(busy),
                    pr_url,
                )
            return True
        self.__dict__.setdefault("_ci_rerun_waiting", {}).pop(fid, None)
        if not run_ids:
            return False
        if not head:
            head = await worktree.pr_head_sha(pr_url, cwd=repo)
        short = head[: store_mod.SHORT_SHA_LEN] if head else "an unreadable head"
        self.__dict__.setdefault("_ci_rerun_checks", {})[fid] = _ci_failed_check_names(summary)
        if head:
            try:
                await asyncio.to_thread(store.record_ci_rerun, fid, head, used + 1)
            except Exception as exc:  # noqa: BLE001 — the rerun is already running; a failed
                # stamp only means a later red at this head may be rerun once more.
                log.warning("[project_board] %s: could not stamp the CI rerun at %s: %s", fid, short, exc)
        log.info(
            "[project_board] %s CI red at %s — rerunning failed jobs once before a fix round: %s (%s)",
            fid,
            short,
            ", ".join(run_ids),
            pr_url,
        )
        return True

    async def _settle_ci_rerun(self, store, fid: str, pr_url: str, repo: str, feature: dict | None) -> None:
        """A green rollup on a card with a ``ci-rerun:`` stamp (#487): the rerun passed with
        no fix round. Log ONE flake line naming the checks that failed the first time (when
        the head is still the one that was rerun; a green new head is just a fixed PR), and
        clear the stamp. Best-effort: an unreadable head leaves the stamp for the next poll."""
        checks_by_fid = self.__dict__.setdefault("_ci_rerun_checks", {})
        stamped, _used = store_mod.ci_rerun_from_labels((feature or {}).get("labels"))
        if not stamped:
            checks_by_fid.pop(fid, None)
            return
        head = await worktree.pr_head_sha(pr_url, cwd=repo)
        if not head:
            return
        checks = checks_by_fid.pop(fid, "") or "the failed checks (names not kept across a restart)"
        if head[: store_mod.SHORT_SHA_LEN] == stamped:
            log.info(
                "[project_board] %s CI flake: the rerun at %s passed with no fix round spent — failed first: %s (%s)",
                fid,
                stamped,
                checks,
                pr_url,
            )
        try:
            await asyncio.to_thread(store.record_ci_rerun, fid, "")
        except Exception as exc:  # noqa: BLE001 — a stale stamp only costs a rerun allowance
            log.warning("[project_board] %s: could not clear the CI rerun stamp: %s", fid, exc)

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
        """Ingest a trusted EXTERNAL-PANEL PASS for the PR's CURRENT head and repair a stale
        ``changes-requested`` or ABSENT local review verdict to ``review-clean`` (#323) — the
        inbound counterpart of the gate's head-pinned publish.

        #512: the PASS signal is the configured external review panel's marker review NAMING
        the live head with a non-blocking verdict — read via ``worktree.pr_review_state`` +
        ``external_review.evaluate``, exactly as the panel-FAIL edge reads it. It is NEVER a
        commit status: #354 first wrote the board's own gate verdict under ``QA panel``, the
        very name the panel's App check uses, and #323 read that back as the panel's verdict —
        so on careercoach#17 the board trusted its OWN earlier FAIL and held a head the panel
        had PASSed for hours. The board's own status is a record of ITS verdict, never evidence
        of the panel's. With no external panel configured there is nothing to adopt (returns
        False); the board never manufactures a PASS from its own signals.

        Strictest verdict wins (#512), checked in two layers once a current-head external PASS is
        in hand — the PRIMARY veto is the card's OWN local verdict, NOT a status read back:

          1. ``changes-requested`` set AND the card's ``reviewed-head:<sha>`` stamp (prefix,
             ``_REVIEWED_HEAD_SHA_LEN``) EQUALS the live head ⇒ the board's own gate FAILED this
             EXACT head. HOLD (``_note_gate_override_hold`` once per (card, head)) and do not adopt.
             A status read cannot override this — it stands even if the gate's ``failure`` status
             never reached GitHub (the round-3 edge: a verdict that never posted still lives here).
          2. ``changes-requested`` set but the stamp names a DIFFERENT head ⇒ the rejection is stale
             (an external push moved the head out from under it). Fall through to adoption.
          3. ``changes-requested`` set with NO (or an ambiguous) stamp ⇒ fail closed, do not adopt —
             #328's missing-stamp doctrine: identity we cannot prove stale stays blocking.
          4. No local verdict (ABSENT — a pre-upgrade card, an operator unblock, the inert-gate
             exit) ⇒ fall through to adoption. A leftover ``pending`` status plays no part (round 2).

        The SECONDARY veto, belt-and-braces AFTER the local check, is the board's OWN
        ``board/review-gate`` status at the live head, read TRI-STATE
        (``worktree.read_review_status_result`` on the board's OWN context, never ``QA panel``): a
        COMPLETED non-success (``failure`` / ``error``) — a real, head-pinned verdict the gate
        actually REACHED — HOLDS (``_note_gate_override_hold``), recording the internal-vs-external
        disagreement and that an operator unblock/override is required; an UNREADABLE read (a gh
        error / malformed / ambiguous response the plain ``read_review_status`` could not tell apart
        from absence) FAILS CLOSED this pass — we cannot prove the gate did not FAIL this head, so a
        transient read failure is never mistaken for "no gate verdict" and promotes a head the gate
        may have FAILED (careercoach#17, round 1); a ``pending`` or PROVEN-ABSENT status does NOT
        veto — a genuinely-running gate is already excluded ABOVE (its card is ``review-pending``,
        which this method skips, plus the live-drive / in-flight guards) BEFORE the status is read,
        so a ``pending`` reaching it is the INERT-gate exit's leftover (it posts ``pending`` then
        clears the substate without a terminal verdict), the very ABSENT-verdict case this method
        repairs; holding on it would strand, for good, a card the external PASS can clean (round 2).

        It invents no verdict — it ADOPTS a verified one, and only ever RELAXES a blocking state
        to clean (never manufactures a blocking one). Fails CLOSED, leaving the card exactly as
        it was, on everything that is not a provable current-head external PASS: no config (r4),
        an unreadable panel view, a panel FAIL (r2/r6), a PASS judged against a head that is no
        longer live (the TOCTOU guard — a push between ``pr_head_sha`` and the panel read leaves
        ``verdict.head != head``, r3), or no marker PASS at the head at all. The verdict is
        written PINNED to the head it was proven for (#323): a later push leaves the pin naming a
        dead head, the merge gate declines, and the card goes back for review — nothing to race,
        nothing to undo. NEVER races the internal gate (r4/r5): it skips a ``review-pending``
        card (the gate owns that live verdict) and a ``review-clean`` card (already promoted →
        idempotent no-op), and — the same liveness guard the stranded-fix recovery (#340) uses —
        any card with a live drive, a claimed worktree, or an in-flight gate. Returns True only
        when it repaired the substate; the caller then refreshes its snapshot so the downstream
        #340 / merge edges read the cleaned labels this same pass."""
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
        # The PASS signal is the EXTERNAL panel's marker review (#512), never a commit status the
        # board wrote. No panel configured for this card's project ⇒ nothing to adopt.
        ext_cfg = self._external_review_cfg_for(feature)
        if ext_cfg is None:
            return False
        view = await worktree.pr_review_state(pr_url, cwd=repo)
        verdict = external_review.evaluate(view, ext_cfg) if view else None
        if verdict is None or verdict.head != head:
            # Unreadable panel view, or one judged against a head that is no longer live (a push
            # landed between the head read and the panel read) → fail closed. Never adopt a PASS
            # that could be for a head that is gone.
            return False
        if verdict.failed:
            # The panel FAILED this head — authoritative the OTHER way (``_reconcile_external_review``
            # owns the bounce). It must never promote or clear a blocking state (r2).
            log.info(
                "[project_board] %s external panel FAILED the current head %s — not promoting: %s",
                fid,
                head[:12],
                pr_url,
            )
            return False
        if not verdict.review_verdict or verdict.review_verdict in external_review.BLOCKING_VERDICTS:
            # ``failed`` is False but NO configured reviewer's marker review names this head with a
            # non-blocking (PASS) verdict — a green CI rollup or an un-reviewed head is not a PASS.
            return False
        # A trusted, current-head external PASS. Strictest verdict wins (#512), PRIMARY veto: the
        # card's OWN local verdict, read from its labels — NOT a commit status read back (a status
        # read can be unreadable, or the gate's FAIL may never have reached GitHub at all). A live
        # ``changes-requested`` whose ``reviewed-head:<sha>`` stamp NAMES the live head is the
        # board's own gate FAILING this EXACT head; it vetoes the external PASS and no status read
        # can override it (round 3). The stamp is the same #328 identity the stale-review re-arm
        # compares, read SHORT (``_REVIEWED_HEAD_SHA_LEN``).
        if stale_rejection:
            stamps = [l[len(LABEL_REVIEWED_HEAD_PREFIX) :] for l in labels if l.startswith(LABEL_REVIEWED_HEAD_PREFIX)]
            if len(stamps) != 1 or not stamps[0]:
                # changes-requested with NO (or an ambiguous) reviewed-head stamp → fail closed:
                # #328's missing-stamp doctrine. We cannot prove the rejection is stale, so the
                # blocking verdict stands and the external PASS is not adopted. Not a recorded
                # disagreement (no operator hold) — the rejection is simply still in force.
                log.info(
                    "[project_board] %s changes-requested with no reviewed-head stamp — not adopting the external "
                    "PASS (fail closed, #328 missing-stamp doctrine): %s",
                    fid,
                    pr_url,
                )
                return False
            if head[:_REVIEWED_HEAD_SHA_LEN] == stamps[0]:
                # The board's own gate FAILED this EXACT head (the live head carries the rejected
                # verdict's stamp). Strictest verdict wins — HOLD, once per (card, head). This stands
                # even if the gate's ``failure`` status never posted, and even if the secondary
                # status read below would find nothing (round 3).
                short = head[: store_mod.SHORT_SHA_LEN]
                why = (
                    f"internal review gate rejected (changes-requested) head {short} while the external panel "
                    f"PASSed — strictest verdict wins; an operator unblock/override is required to merge: {pr_url}"
                )
                await asyncio.to_thread(self._note_gate_override_hold, store, fid, head, why)
                return False
            # The stamp names a DIFFERENT head — the rejection is for a head a push has since
            # replaced. The local verdict does not veto THIS head; fall through to the secondary
            # gate-status check and then adoption.
        # Strictest verdict wins (#512), SECONDARY veto (belt-and-braces, AFTER the local check):
        # the board's OWN gate status at this head can VETO the PASS, but can never BE it — read
        # only the board's own context, and read it TRI-STATE so a PROVEN-ABSENT gate status (the
        # gate never ran this head — adoption may proceed) is told apart from an UNREADABLE one. The
        # veto must FAIL CLOSED on an unreadable read: ``read_review_status`` returns None for BOTH
        # absence and a gh error / malformed / ambiguous response, and treating a transient read
        # failure as "no gate verdict" would promote a head the gate may have FAILED (careercoach#17).
        outcome, gate = await worktree.read_review_status_result(
            repo_slug, head, context=worktree.GATE_STATUS_CONTEXT, cwd=repo
        )
        if outcome == worktree.STATUS_READ_UNREADABLE:
            # The gate status could not be read cleanly — we cannot PROVE the gate did not FAIL
            # this head, so we do not adopt. Not a recorded disagreement (no operator hold): a
            # transient read failure the next poll simply retries.
            log.info(
                "[project_board] %s gate status unreadable at head %s — not adopting the external PASS this "
                "pass (fail closed, retries next poll): %s",
                fid,
                head[:12],
                pr_url,
            )
            return False
        # Only a COMPLETED gate verdict vetoes — ``failure`` / ``error``, a real head-pinned
        # judgement the gate actually reached. A ``pending`` status does NOT veto here: a gate that
        # is genuinely mid-run is already excluded ABOVE (its card is ``review-pending``, which this
        # method skips, and its fid is in ``_review_inflight`` / has a live drive), so a ``pending``
        # reaching this read is the INERT-gate exit's leftover — it posts ``pending`` then clears the
        # review substate without landing a terminal status. That is the ABSENT-verdict case this
        # method's contract repairs; vetoing on it would strand the card the external PASS can clean,
        # for good (the #512 review finding). The gate holds a card only when it has a real FAILING
        # verdict for the head, never merely because it once began looking.
        if outcome == worktree.STATUS_READ_PRESENT and gate.get("state") in ("failure", "error"):
            gate_state = gate.get("state")
            short = head[: store_mod.SHORT_SHA_LEN]
            why = (
                f"internal review gate FAILED ({gate_state}) at head {short} while the external panel "
                f"PASSed — strictest verdict wins; an operator unblock/override is required to merge: {pr_url}"
            )
            await asyncio.to_thread(self._note_gate_override_hold, store, fid, head, why)
            return False
        # No internal-gate veto. The verdict is written PINNED to the head it was proven for
        # (#323): `set_review_substate` stamps `review-clean-sha:<head>` and `merge_posture`
        # refuses to merge unless that pin equals the live head. A push at any point leaves the
        # pin naming a head that no longer exists, the merge gate declines, and the card goes
        # back for review — there is nothing to lose a race to, and nothing to undo.
        note = (
            f"review reconciled to clean (#323): the external QA panel PASSed PR head {head[:12]} "
            f"({verdict.review_verdict}) — "
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
            "[project_board] %s adopted a trusted current-head external QA PASS (%s) → review-clean, pinned to %s: %s",
            fid,
            verdict.review_verdict,
            head[:12],
            pr_url,
        )
        return True

    def _note_gate_override_hold(self, store, fid: str, head: str, why: str) -> None:
        """Say ONCE per (card, head) that the board's own review gate REJECTED a head the external
        panel PASSed, so an operator override is needed to merge (#512) — on the bead and to the
        operator. Fires from EITHER veto in ``_reconcile_trusted_qa_pass``: the PRIMARY local-verdict
        veto (a live ``changes-requested`` whose ``reviewed-head`` stamp names this exact head) or
        the SECONDARY gate-status veto (a COMPLETED ``failure`` / ``error`` status). Never fires on a
        merely ``pending`` gate status (that is the inert-gate leftover, not a verdict — see
        ``_reconcile_trusted_qa_pass``). A SEPARATE ledger from ``_note_external_hold`` (that one is
        CLEARED whenever the panel passes, which is exactly when THIS hold fires), so the two never
        cross-silence each other. Store-only; runs off the event loop."""
        held = getattr(self, "_gate_override_held", None)
        if held is None:
            held = self._gate_override_held = {}
        if held.get(fid) == head:
            return
        held[fid] = head
        log.warning("[project_board] %s held: %s", fid, why)
        try:
            store.comment(fid, f"auto-merge held: {why}")
        except Exception:  # noqa: BLE001 — bookkeeping must not break the reconcile
            log.warning("[project_board] %s gate-override hold comment failed", fid, exc_info=True)
        self._notify_operator(fid, f"Board card {fid} is held: {why}", incident=f"gate-override|{head}")

    # ── the external QA panel's FAIL at the current head (#473) ───────────────
    def _external_review_cfg_for(self, feature: dict):
        """This feature's project's ``external_review`` setting, else the flat top-level key,
        parsed (``external_review.parse_config``): a Config, or None when the check is off."""
        pc = self._project_cfg(feature)
        raw = pc.get("external_review") if "external_review" in pc else self.cfg.get("external_review")
        return external_review.parse_config(raw)

    def _note_external_hold(self, store, fid: str, head: str, why: str) -> None:
        """Say ONCE per (card, head) why a card the panel failed is held rather than bounced
        — on the bead and to the operator. Store-only; runs off the event loop."""
        held = getattr(self, "_ext_review_held", None)
        if held is None:
            held = self._ext_review_held = {}
        if held.get(fid) == head:
            return
        held[fid] = head
        log.warning("[project_board] %s held: %s", fid, why)
        try:
            store.comment(fid, f"auto-merge held: {why}")
        except Exception:  # noqa: BLE001 — bookkeeping must not break the reconcile
            log.warning("[project_board] %s external-review hold comment failed", fid, exc_info=True)
        self._notify_operator(fid, f"Board card {fid} is held: {why}", incident=f"external-review|{head}")

    async def _reconcile_external_review(
        self, store, feature: dict, pr_url: str, repo: str, *, view: dict | None = None
    ) -> bool:
        """Act on an EXTERNAL QA panel's FAIL at the PR's CURRENT head (#473). Returns True
        when the panel has failed this head, whatever was done about it, so the caller
        skips the rest of the pass: no rebase, no merged-state gate (and no merged-verify
        budget spent), no CI bounce and no merge for a PR the panel has rejected.

        One read (``worktree.pr_review_state``) gives the head, the reviews and the head
        commit's checks; ``external_review.evaluate`` judges it. A FAIL is:

        * the latest review by a configured reviewer (``protoreview[bot]``) whose hidden
          marker (``<!-- protoagent-qa-review head=<sha> verdict=FAIL … -->``) names the
          current head with a blocking verdict (FAIL / BLOCK / REJECT), not dismissed; or
        * the configured check run (``QA panel``, an App check) failing at the head, unless
          a non-blocking marked review at the head was submitted after it (a configured
          status likewise; none by default — ``Review at head`` is also red on every head
          the panel has not reviewed YET, #477 review).

        Then, in order:

        * no FAIL review at this head (only a red check or status) → HOLD. There are no
          findings to hand a coder, and a bounce without them re-runs the same code. The
          operator is told once.
        * the head already carries this card's ``ext-review-bounced:<sha>`` stamp → HOLD.
          The fix round came back without a new head. Once per head: only a new push
          re-arms the bounce.
        * the loop is still working the card (a live drive, a claimed build, a running
          review gate — ``requeue_refusal``) → hold for this pass, never bounce under it.
        * ``review_fix_max`` external fix rounds are spent (``budget:ext-review-fix``,
          counted apart from the in-process gate's own, which a clean verdict resets) →
          Blocked for a human, the findings on the bead, as the gate's exhaustion does.
        * otherwise → a fix round on the SAME PR: the findings (blocker/major, confirmed or
          verified, with file:line, claim and evidence) are recorded as a review-bounce
          comment with the head stamped (``record_review_bounce``), queued to lead the
          next prompt (``queue_review_feedback``) with the PR diff beside them, one
          ext-review-fix unit is spent, and the card is requeued.

        Fails OPEN on anything unreadable (no config, a ``gh`` error, no head): an
        unreadable panel is not a FAIL, and the pass goes on as it did before #473."""
        fid = feature["id"]
        cfg = self._external_review_cfg_for(feature)
        if cfg is None:
            return False
        if view is None:  # not handed the pass's read (a queued card, a direct call): read it
            view = await worktree.pr_review_state(pr_url, cwd=repo)
        verdict = external_review.evaluate(view, cfg) if view else None
        if verdict is None or not verdict.failed:
            getattr(self, "_ext_review_held", {}).pop(fid, None)
            return False
        head = verdict.head
        short = head[: store_mod.SHORT_SHA_LEN]
        signals = external_review.fail_summary(verdict)
        if not verdict.has_findings_review:
            await asyncio.to_thread(
                self._note_external_hold,
                store,
                fid,
                head,
                f"the external review FAILED at head {short} ({signals}) but no findings review from "
                f"{', '.join(cfg.reviewers)} names this head, so there is nothing to hand a fix round — the "
                f"merge edge and the merged-state gate are skipped until the panel passes or posts its findings: "
                f"{pr_url}",
            )
            return True
        stamp = f"{store_mod.LABEL_EXTERNAL_REVIEW_BOUNCED_PREFIX}{short}"
        if stamp in (feature.get("labels") or []):
            await asyncio.to_thread(
                self._note_external_hold,
                store,
                fid,
                head,
                f"the external review still FAILS at head {short} ({signals}), and a fix round was already "
                f"bounced for this head without a new push — needs a human (push a fix, or close the PR): "
                f"{pr_url}",
            )
            return True
        if (
            _loop.live_drive(fid) is not None
            or fid in self._inflight_files
            or fid in self._review_inflight
            or _loop.requeue_refusal(fid, own_task=asyncio.current_task())
        ):
            log.info(
                "[project_board] %s external review FAILED at %s, but the loop is still working the card — "
                "bounce deferred to the next poll: %s",
                fid,
                short,
                pr_url,
            )
            return True
        rendered = external_review.render_findings(verdict, pr_url)
        n = await self._budget_get(store, fid, "ext-review-fix", feature)
        # The diff the panel failed rides beside the findings (the gate's carry-the-lesson
        # lever). Read BEFORE the claim lock: a `gh` round-trip must not hold the claim scan.
        prior_diff = ""
        if n < self.review_fix_max:
            try:
                prior_diff = await worktree.pr_diff(pr_url, cwd=repo)
            except Exception:  # noqa: BLE001 — the diff is a convenience; the branch is resumed regardless
                prior_diff = ""
        async with self._claim_guard():
            # Re-read under the claim lock (#402): an attach, a salvage or an operator
            # requeue may have moved the card since the pass read it.
            fresh = await asyncio.to_thread(store.get_feature, fid) or {}
            if fresh.get("board_state") != "in_review" or stamp in (fresh.get("labels") or []):
                return True
            if (
                _loop.live_drive(fid) is not None
                or fid in self._inflight_files
                or _loop.requeue_refusal(fid, own_task=asyncio.current_task())
            ):
                return True
            await asyncio.to_thread(lambda: store.record_review_bounce(fid, rendered, head=head))
            if n >= self.review_fix_max:
                await asyncio.to_thread(
                    store.flag_blocked,
                    fid,
                    f"external review FAILED at head {short} ({signals}) after {n} review fix round(s) — "
                    f"needs human review: {pr_url}",
                    # Stated, never guessed from the words: a `TIMED_OUT` check in the reason
                    # must not read as a transient block the sweep would auto-requeue.
                    category="terminal",
                )
                self._ci_feedback.pop(fid, None)
                self._ci_prior_diff.pop(fid, None)
                await self._budget_reset(store, fid, "ext-review-fix")
                log.warning(
                    "[project_board] %s blocked (external review FAIL at %s, %d fix round(s) spent): %s",
                    fid,
                    short,
                    n,
                    pr_url,
                )
                return True
            await self._budget_set(store, fid, "ext-review-fix", n + 1)
            _loop.queue_review_feedback(fid, rendered)
            if prior_diff:
                self._ci_prior_diff[fid] = prior_diff
            else:
                self._ci_prior_diff.pop(fid, None)
            await asyncio.to_thread(store.requeue, fid)
        getattr(self, "_ext_review_held", {}).pop(fid, None)
        log.info(
            "[project_board] %s external review FAILED at %s (%s) → fix round %d/%d on the same PR: %s",
            fid,
            short,
            signals,
            n + 1,
            self.review_fix_max,
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
        so it never actually published. The status is posted under the board's OWN
        ``board/review-gate`` context (#512 — NOT the external panel's ``QA panel`` App check, so
        the #323 adoption can never read this verdict back as the panel's PASS), one of
        success/failure/pending, a concise <=140-char ``description``, and the PR link as the
        stable ``target_url``.

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
                repo_slug,
                head_sha,
                state=state,
                description=description,
                target_url=pr_url,
                context=worktree.GATE_STATUS_CONTEXT,
                cwd=repo,
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
        zombie = self._review_zombies.get(fid)
        if zombie is not None and not zombie.done():
            # The last review call timed out and was abandoned, but it has not returned yet
            # (#471 review). Starting a second one would stack model calls on a hung stream.
            log.warning("[project_board] %s review gate not re-run — its abandoned review call is still running", fid)
            return
        self._review_zombies.pop(fid, None)
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
            if reason.startswith(_REVIEW_TIMED_OUT):
                # Our own cap fired (#462). That is not an unrunnable review, and counting it
                # toward review_run_max would block cards on a slow local model that used to
                # simply take long. Leave review-pending; the next poll retries once the
                # abandoned call has returned (see `_review_gate`).
                log.warning("[project_board] %s review gate timed out — will retry on the next poll: %s", fid, reason)
                return
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
            # judge, so it must not pretend to. Record and leave in review. The
            # ``pending`` status posted above is left on the head (we never fake a
            # terminal verdict the gate did not reach); it is an ABANDONED gate, not a
            # running one, so the trusted-QA adoption treats it as the repairable
            # ABSENT-verdict case and the external panel can still clean the card —
            # a ``pending`` gate status does NOT veto that adoption (see
            # ``_reconcile_trusted_qa_pass``; #512 review finding).
            await asyncio.to_thread(
                store.set_review_substate, fid, None, note="review gate: host lacks graph.review.findings — gate inert"
            )
            log.warning("[project_board] %s review gate inert (no findings parser on this host)", fid)
            return
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
        if blocking and not full_diff.endswith(worktree.DIFF_TRUNCATED_MARKER):
            blocking, ungrounded = partition_by_grounding(blocking, full_diff)
            for f in ungrounded:
                # ADR 0077's own word for a quote that cannot be grounded. Set BEFORE
                # `_review_prior` is serialized below so the demotion rides into the next
                # round's delta review — otherwise the re-review is handed back a
                # `confirmed` verdict for a finding this round refused to block on, and
                # re-confirms it.
                f.verdict = "uncertain"
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
                # Rendered by hand rather than through `_render_findings`: that stamps
                # `_REVIEW_FINDINGS_TITLE`, which the recovery path scans for (see
                # `_latest_review_findings`) to rebuild a card's BLOCKING findings. A
                # demotion comment carrying that title would be read back as a blocking
                # verdict — the opposite of what it records.
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
        # Remember this round's findings — the next run (a bounce re-review) passes
        # them back as the recipe's prior_findings input, making it a DELTA review
        # (drop fixed, carry still-open) instead of a from-scratch re-litigation.
        # Serialized HERE, after the grounding partition (#381) and before either verdict
        # branch, so a demoted finding rides into the next round as `uncertain` rather than
        # as the `confirmed` this round declined to act on — which the re-review would
        # simply re-confirm.
        try:
            self._review_prior[fid] = json.dumps([f.to_dict() for f in findings]) if findings else ""
        except Exception:  # noqa: BLE001 — memory is an optimization, never a gate failure
            self._review_prior.pop(fid, None)
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
        self._ci_prior_diff[fid] = worktree.truncate_diff(full_diff, worktree.PR_DIFF_MAX_CHARS)
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
                result = await _within(
                    runner(self.review_workflow, inputs),
                    self.review_gate_timeout,
                    on_abandon=lambda t: self._review_zombies.__setitem__(fid, t),
                )
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
            except asyncio.TimeoutError:
                # A hard cap of our own (#462): a hung model stream ignored the host client's
                # request_timeout (protoAgent#3699) and held this gate for 80 minutes.
                log.warning(
                    "[project_board] %s review workflow %r did not finish in %ss — abandoned",
                    fid,
                    self.review_workflow,
                    self.review_gate_timeout,
                )
                no_run_reason = (
                    f"{_REVIEW_TIMED_OUT}: workflow {self.review_workflow!r} did not finish within "
                    f"review_gate_timeout_s={self.review_gate_timeout:g}s"
                )
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
            output = await _within(
                ADAPTERS["a2a"].dispatch(reviewer, msg),
                self.review_gate_timeout,
                on_abandon=lambda t: self._review_zombies.__setitem__(fid, t),
            )
            if output is None:
                return None, f"reviewer {self.reviewer_name!r} returned no output"
            return output, None
        except asyncio.TimeoutError:
            log.warning(
                "[project_board] %s reviewer %r did not answer in %ss",
                fid,
                self.reviewer_name,
                self.review_gate_timeout,
            )
            return (
                None,
                f"{_REVIEW_TIMED_OUT}: reviewer {self.reviewer_name!r} did not answer within "
                f"review_gate_timeout_s={self.review_gate_timeout:g}s",
            )
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

    def _note_gate_speed(self, feature: dict | None, cmd: str, *, timed_out: bool) -> None:
        """Remember whether this project's gate finishes inside ``local_gate_timeout_s``
        (#483). A timeout is recorded against the exact command and timeout; a gate run
        that FINISHES (any exit) clears it."""
        project = self._project_name(feature) if feature is not None else ""
        before = dict(self._slow_gates)
        if timed_out:
            self._slow_gates[project] = {"cmd": cmd, "timeout_s": float(self.local_gate_timeout)}
        else:
            self._slow_gates.pop(project, None)
        if self._slow_gates != before:
            health.publish_slow_gates(self._slow_gates)

    def _gate_known_slow(self, feature: dict, cmd: str) -> bool:
        """This project's gate, as configured NOW, timed out last time it ran (#483)."""
        slow = self._slow_gates.get(self._project_name(feature))
        return bool(slow) and slow["cmd"] == cmd and slow["timeout_s"] == float(self.local_gate_timeout)

    async def _run_local_gate(self, wt: str, feature: dict | None = None) -> str | None:
        """Run the pre-PR local gate (``local_gate_cmd``) in the worktree.

        Returns ``None`` when the gate passes (exit 0), when no gate is configured,
        or when the gate itself couldn't run on a HEALTHY tree (timeout / unlaunchable
        command / signal kill) or ran over a BROKEN DEPENDENCY TREE (output matching
        ``broken_dependency_tree`` — recorded in ``_gate_infra`` too) — a broken or flaky
        gate must never block otherwise-good work, so those
        degrade to "pass" (CI is still the real gate). Returns the captured output (tail,
        truncated to ``local_gate_output_chars``) on a CLEAN non-zero exit, so the
        caller can hand it to the coder to fix. Resolves the gate command from the
        feature's project when given (#90).

        Raises ``worktree.WorktreeMissing`` when the tree itself is gone — before the
        gate could launch, or by the time it ended (#461). That is not a gate that could
        not run; there is nothing left to publish, and "treating as pass" opened a PR
        from a deleted directory."""
        cmd = self._local_gate_cmd_for(feature) if feature is not None else self.local_gate_cmd
        if not cmd:
            return None
        # #490: every "treating as pass" below is a run that judged nothing; say so to the
        # caller that counts verdicts (the merged-state re-verify) without changing the
        # return contract every other caller relies on.
        no_verdict = self.__dict__.setdefault("_gate_no_verdict", set())
        no_verdict.discard(wt)
        self.__dict__.setdefault("_gate_infra", {}).pop(wt, None)

        def _gone(during: str) -> None:
            if not os.path.isdir(wt):
                raise worktree.WorktreeMissing(wt, during)

        try:
            proc = await worktree.spawn_shell(
                cmd,
                cwd=wt,
                env=self._child_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as exc:  # noqa: BLE001 — a gate that can't run must not block…
            _gone("the pre-PR gate could not launch in it")  # …unless there is no tree to gate
            log.info("[project_board] pre-PR gate failed to run (treating as pass — CI still gates): %s", exc)
            no_verdict.add(wt)
            return None
        try:
            try:
                # Kills the whole gate tree on a timeout or cancel (#423) — killing only
                # the shell orphaned `pnpm install` behind every timed-out gate.
                out, _ = await worktree.communicate_or_kill(proc, timeout=self.local_gate_timeout)
            except asyncio.TimeoutError:
                _gone("removed while the pre-PR gate ran")
                log.warning("[project_board] pre-PR gate timed out (%ss) — treating as pass", self.local_gate_timeout)
                self._note_gate_speed(feature, cmd, timed_out=True)
                no_verdict.add(wt)
                return None
            self._note_gate_speed(feature, cmd, timed_out=False)
            if proc.returncode == 0:
                return None
            # A red or killed gate over a tree that has since vanished judged nothing.
            _gone("removed while the pre-PR gate ran")
            sig = killed_by_signal(proc.returncode)
            if sig is not None:
                # Killed by a signal — an operator `kill`, the OOM killer, a wrapper whose
                # child died on one — NOT the repo failing its own gate. (A member shutdown
                # used to land here too, its SIGTERM reaching the gate through the shared
                # process group; since #423 the gate has its own group and a shutdown
                # cancel kills the tree before any exit code is read.) Same posture as the
                # timeout above: the gate couldn't run to
                # a verdict, so it must not produce one. Seen 2026-08-20: a restart
                # landed mid merged-state gate, pytest died at 13% with rc=-15, and the
                # feature was flag_blocked "gate FAILED on the merged state" against a
                # PR whose CI was fully green. Seen AGAIN 2026-09-01 (#386) through a
                # wrapper that flattened the signal to 1 — which is why this now reads
                # 128+N too; see `killed_by_signal`.
                log.warning(
                    "[project_board] pre-PR gate killed by signal %d (shutdown / external kill) — "
                    "no verdict, treating as pass (CI still gates)",
                    sig,
                )
                no_verdict.add(wt)
                return None
            text = (out or b"").decode("utf-8", "replace").strip()
            broken = broken_dependency_tree(text)
            if broken:
                # The dependency tree under the gate is broken (a half-built node_modules,
                # pnpm's reinstall prompt, an ERR_PNPM_* failure): the gate never reached
                # the code, so this red is the toolchain's, not the repo's. Same posture as
                # a kill: no verdict, CI still gates. Recorded separately so the merged-state
                # re-verify can retry it as INFRA rather than stamp or block on it.
                log.warning(
                    "[project_board] pre-PR gate hit a broken dependency tree (INFRA, not a verdict on the "
                    "code) — treating as pass, CI still gates: %s",
                    broken,
                )
                no_verdict.add(wt)
                self.__dict__.setdefault("_gate_infra", {})[wt] = f"broken dependency tree: {broken}"
                return None
            if len(text) > self.local_gate_output_chars:
                text = "…(truncated)…\n" + text[-self.local_gate_output_chars :]
            return text or f"gate command exited {proc.returncode} with no output"
        except worktree.WorktreeMissing:
            raise
        except Exception as exc:  # noqa: BLE001 — a gate that can't run must not block
            log.info("[project_board] pre-PR gate failed to run (treating as pass — CI still gates): %s", exc)
            no_verdict.add(wt)
            return None
