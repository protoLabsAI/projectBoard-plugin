"""Drive / dispatch edge of the board loop (extracted from loop.py, #268).

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


# ── ready-queue livelock bound (#356) ────────────────────────────────────────────
# A genuinely-ready card the puller skips every tick for the SAME unresolved reason is
# not transient — it is a livelock (a stuck dispatch target, a claim race that never
# clears). After this many CONSECUTIVE identical skips the card is flagged blocked, so
# the existing blocked sweep / operator escalation makes the otherwise-invisible
# ready-but-unclaimable state visible. Read from the `ready_skip_max` cfg key with this
# default; floored (see `_ready_skip_max`) so a single/transient claim race is NEVER
# terminal (r5). NOT a manifest setting — an internal liveness bound, so it never widens
# the operator config surface (the packaging gate stays truthful about live-vs-restart).
_READY_SKIP_MAX_DEFAULT = 5
# Skip reasons NOT bounded into a livelock block (their counter is still tracked so a
# later reason CHANGE resets cleanly, but reaching the threshold does not block):
#  - "hot-file": an in-flight build is actively progressing and (bounded by
#    coder_timeout) will free the file — a RESOLVING skip, transient by construction.
#  - "blocked" / "preflight-hold": the card already sits in the visible blocked/held
#    path — re-blocking is redundant and would clobber its real reason.
_NON_LIVELOCK_SKIP_REASONS = frozenset({"hot-file", "blocked", "preflight-hold"})


def _is_livelock_skip_reason(reason: str) -> bool:
    """A skip reason whose indefinite repetition IS a livelock — nothing is working to
    make the ready card claimable (a lost claim race, a stuck dispatch target). vs a
    RESOLVING skip (hot-file: an in-flight build will free the file) or one that is
    already visible/blocked. Any reason not explicitly exempted is treated as a livelock,
    so a future genuinely-stuck skip reason is bounded by default rather than retrying
    forever (#356 — the liveness invariant is generic, not claim-race-specific). A
    `state=…` skip means the card is no longer `ready`, so it is never a ready-queue
    livelock."""
    r = str(reason or "")
    return bool(r) and r not in _NON_LIVELOCK_SKIP_REASONS and not r.startswith("state=")


# ── on-demand queue dispatch diagnostic (board_dispatch, #390) ────────────────────
async def request_dispatch() -> dict:
    """Ask the RUNNING board loop to evaluate its ready queue immediately and return its
    structured decision record — the seam the ``board_dispatch`` tool calls.

    Reaches the live loop through the process-stable registry (``live_loop`` — the same
    handle the operator cancel / budget-reset verbs use, monkeypatch-visible via the
    ``_loop`` package). Delegates every gate to :meth:`BoardLoop.dispatch_now`, which uses
    the ORDINARY scheduling path and bypasses nothing (loop_enabled, project isolation,
    concurrency, review-WIP, hot-file). Answers ``loop-not-running`` when no loop surface
    is live in this process (never started, or already stopped) — there is nothing to ask;
    a DISABLED loop is registered and is answered by ``dispatch_now`` itself
    (``loop-disabled``)."""
    loop = _loop.live_loop()
    if loop is None:
        return {
            "dispatched": [],
            "outcome": "loop-not-running",
            "detail": (
                "no board loop surface is running in this process (it was never started, or has "
                "stopped) — there is nothing to ask to evaluate the queue"
            ),
            "skipped": [],
            "parked": [],
        }
    return await loop.dispatch_now()


class DriveMixin:
    # ── lifecycle (register_surface start/stop) ───────────────────────────────
    def start(self):
        # Publish this loop into the process-stable registry BEFORE the enabled gate so
        # the merged-verify budget reset verb can reach its in-process cache (ADR 0326).
        # A disabled loop registers too (harmless — its budget cache stays empty).
        _register_loop(self)
        if not self.enabled:
            log.info("[project_board] loop disabled (project_board.loop_enabled=false) — board API still serves")
            return None
        self._task = asyncio.create_task(self._run(), name="project-board-loop")
        # The RUNNING loop's config, in a process-stable slot: after a reload the
        # routers see the new config while this loop keeps its construction-time
        # `coders`/`repo`/…; /status compares the two and says "restart to apply".
        setup_check.publish_loop_snapshot(self.cfg)
        log.info(
            "[project_board] loop started (coder=%s reviewer=%s every %ss, max_concurrent=%d, "
            "merge_poll=%s, coder_timeout=%ss; live on config reload: %s, projects — everything else "
            "needs a restart)",
            self.coder_name or "<unset>",
            self.reviewer_name,
            self.interval,
            self.max_concurrent,
            self.merge_poll,
            self.coder_timeout,
            ", ".join(LIVE_KNOBS),
        )
        self._warn_if_review_gate_unrunnable()
        if self.coder_solve and self.coder_solve_k > 1:
            peak = self.max_concurrent * self.coder_solve_k
            cap_note = f", capped at {self.max_concurrent_sessions}" if self.max_concurrent_sessions > 0 else ""
            log.info(
                "[project_board] coder_solve_k=%d: peak concurrent ACP sessions = "
                "max_concurrent × coder_solve_k = %d × %d = %d%s "
                "(set max_concurrent_sessions to cap this)",
                self.coder_solve_k,
                self.max_concurrent,
                self.coder_solve_k,
                peak,
                cap_note,
            )
        return self._task

    def reload(self, new_config) -> dict:
        """Apply live knobs and project routing to the RUNNING loop.

        The host fires this on every config reload with the new ``LangGraphConfig``
        (ADR 0018 ``register_surface(reload=)``) — the running surface survives a
        settings save, so without this hook a ``max_concurrent`` edit in the console
        only took effect after a restart (the loop reads ``cfg`` once, at
        construction). ``new_config`` may be the host config object (``.plugin_config
        [section]``) or the plain section dict; a section with no such key leaves that
        knob alone (a host without the manifest's defaults merged in). Returns the
        ``{knob: (old, new)}`` diff and logs it, so the splunk line for "did my
        settings change land" is one grep. Never raises: a malformed value logs a
        warning and keeps the current knob — a typo in Settings must not stall the
        loop mid-drive."""
        section = _plugin_section(new_config)
        changed: dict[str, tuple] = {}

        # A project's fields form one routing policy: resolving a repo with the old
        # base/gate/coder map (or vice versa) would cross streams. Validate a complete
        # candidate first, then atomically replace both the loop's map and the shared
        # board's map. In-flight drives have already resolved their checkout; new
        # claims and every subsequent dispatch use the replacement.
        if "projects" in section or "default_project" in section:
            candidate = dict(self.cfg)
            if "projects" in section:
                candidate["projects"] = section.get("projects")
            if "default_project" in section:
                candidate["default_project"] = section.get("default_project")
            try:
                projects = resolve_projects(candidate)
                default = resolve_default_project(candidate)
            except (TypeError, ValueError) as exc:
                log.warning("[project_board] reload: project routing is malformed — keeping current map: %s", exc)
            else:
                old_projects = self._projects
                old_default = self._default_project
                if projects != old_projects or default != old_default:
                    self._projects = projects
                    self._default_project = default
                    if "projects" in section:
                        self.cfg["projects"] = section.get("projects")
                    if "default_project" in section:
                        self.cfg["default_project"] = section.get("default_project")
                    self._store_kw["projects"] = projects
                    self._store_kw["default_project"] = default
                    _loop.reconfigure_cached_store(
                        db=self._store_kw["db"],
                        repo=self._store_kw["repo"],
                        base_branch=self._store_kw["base_branch"],
                        projects=projects,
                        default_project=default,
                    )
                    # A repo that previously failed preflight gets a clean evaluation
                    # under its new routing. Retain `_preflight_held`: it records cards
                    # the loop itself blocked and is needed to release them on recovery.
                    self._preflight_state.clear()
                    self._last_preflight.clear()
                    self._preflight_dirty.clear()
                    self._preflight_failed_at.clear()
                    changed["projects"] = (tuple(old_projects), tuple(projects))
                    if old_default != default:
                        changed["default_project"] = (old_default, default)
        for key in LIVE_KNOBS:
            if key not in section:
                continue
            attr = "coder_name" if key == "coder" else key
            cur = getattr(self, attr)
            try:
                if key in LIVE_BOOL_KNOBS:
                    new = _knob_bool(section, key, cur)
                elif key in LIVE_STR_KNOBS:
                    new = str(section.get(key) or "").strip()
                else:
                    new = _knob_int(section, key, cur, floor=LIVE_KNOB_FLOORS[key])
            except (TypeError, ValueError):
                log.warning("[project_board] reload: %s=%r is malformed — keeping %r", key, section.get(key), cur)
                continue
            if new != cur:
                setattr(self, attr, new)
                # Write the knob back into the SHARED cfg dict — `self.cfg` is the very
                # dict register() handed the routers and the tools (one `dict(cfg)`,
                # never copied), so this is how a live edit reaches every read path that
                # takes its posture from cfg rather than from the loop: the preflight's
                # `_setup_status` (a `coder` edit clears the coder gap here, not only on
                # the router's /status) and store.annotate_next_action (#208: a board_list
                # / /features after an `auto_merge` save says "auto-merge pending", not
                # "awaiting-merge", without a restart). Tested in test_next_action.py.
                self.cfg[key] = new
                changed[key] = (cur, new)
        if changed:
            setup_check.publish_loop_snapshot(self.cfg)
            log.info(
                "[project_board] reload applied live: %s (in-flight drives: %d)",
                ", ".join(f"{k} {o}→{n}" for k, (o, n) in changed.items()),
                len(self._drives),
            )
        return changed

    async def stop(self):
        self._shutting_down = True
        self._stop.set()
        _unregister_loop(self)  # drop the process-stable handle (ADR 0326)
        if self._task:
            setup_check.publish_loop_snapshot(None)  # no running loop → nothing to be stale against
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
                ok = await worktree.remove_worktree(repo, wt, branch or "")
                if ok:
                    log.info("[project_board] reaped in-flight worktree on shutdown: %s", wt)
                else:
                    log.warning("[project_board] worktree reap on shutdown failed (directory remains): %s", wt)
            except Exception:  # noqa: BLE001 — teardown must not raise out of shutdown
                log.warning("[project_board] worktree reap on shutdown failed: %s", wt, exc_info=True)

    def _notify_operator(self, fid: str, text: str, *, incident: str = "") -> None:
        """Put ONE item in the operator's inbox for a card that has stopped moving.

        Dedup is the whole difficulty here, and it is deliberately NOT solved with state
        on our side. Earlier cuts tried a per-process memo, then a durable
        ``notified:blocked`` label, then a rollback when a recovery raced the write, then
        a recovery-generation counter to make the rollback safe — and review found a
        narrower race in each. Every one of them was trying to answer "is this the same
        incident?" by tracking OUR OWN writes across a distributed edge, which is the hard
        version of the question.

        The easy version: let the KEY answer it. ``dedup_key`` carries the incident's
        identity — the card, its failure class and reason, and the recovery cycle it is on
        — so the same block dedups by construction, a genuinely different block is a
        different key and alerts, and no bead label, memo, generation or rollback exists
        to go stale.

        The recovery cycle belongs in that identity: a card that auto-healed, rebuilt and
        failed the SAME way again is a NEW failed cycle and is news, because the self-heal
        did not work. Keying on class+reason alone silently suppressed that for the whole
        window — the behaviour this docstring's first draft argued was correct, and was
        not.

        The window is long (`_ALERT_DEDUP_S`) because a blocked card can sit for hours and
        the default 300s would re-alert on every restart — the failure #341 opened with.

        Feature-detected: the inbox is a host module this plugin must not hard-depend on.
        A host without it still gets the WARNING below, which is strictly louder than the
        silence a block used to leave."""
        key = f"blocked:{fid}"
        if incident.strip():
            key += ":" + hashlib.sha1(incident.encode("utf-8", "replace")).hexdigest()[:12]
        try:
            from inbox import InboxStore  # host module — absent on older hosts

            db = _loop._inbox_db_path()
            if db is None:
                raise RuntimeError("no resolvable inbox store for this instance")
            InboxStore(str(db), dedup_window_s=_ALERT_DEDUP_S).add(
                text, priority="now", source="project_board", dedup_key=key
            )
            log.warning("[project_board] %s blocked — operator notified: %s", fid, text[:160])
        except Exception:  # noqa: BLE001 — no inbox seam, or it refused; say so loudly anyway
            log.warning(
                "[project_board] %s blocked and NOT self-healing (no operator inbox reachable): %s",
                fid,
                text[:200],
                exc_info=True,
            )

    # ── setup preflight (v0.42.0) ─────────────────────────────────────────────
    def _ensure_br(self) -> dict:
        """Arm the `br` auto-fetch for THIS loop's config (br_fetch.ensure_br — once per
        process, returns at once). Tests swap this."""
        return br_fetch.ensure_br(self.cfg, which=self._which)

    def _delegate_resolver(self):
        """ONE roster read per preflight (``coder_seam.delegate_resolver``) — the
        dispatch path's ``_resolve_delegate`` re-parses the YAML per name, which is
        fine for one lookup and not for every name every tick. Tests swap this."""
        return coder_seam.delegate_resolver("acp")

    def _setup_status(self) -> dict:
        """The board's setup preflight for THIS loop's config — `br`/`gh` via the
        injectable PATH probe, the coder names against one roster read. Compared to
        its OWN config it is never stale; the `/status` route owns the stale view."""
        return setup_check.setup_status(
            self.cfg,
            which=self._which,
            delegates=self._delegate_resolver(),
            loop_snapshot=setup_check.snapshot_of(self.cfg),
        )

    async def _setup_gate(self) -> bool:
        """Hold the puller until the setup preflight has no loop blockers (no `br`,
        an unresolvable coder, an unbound repo — each of which turned EVERY tick into
        a traceback before v0.42.0). Re-checks every `loop_interval_s`, so installing
        `br` or declaring the delegate recovers WITHOUT a restart; logs ONE warning
        when it pauses and one info line when it resumes. Every check is also
        forwarded to the host's setup-gap seam (edge-triggered, so a steady state is
        silent). Returns False only when the loop was stopped while paused."""
        paused = False
        while not self._stop.is_set() and not self._shutting_down:
            try:
                # Off the event loop: the preflight may shell `br --version` (once per
                # path) and reads the delegates YAML — neither belongs on the loop.
                # ensure_br first: a no-op once br resolves / the fetch is in flight or
                # spent; it arms the fetch when `br_autofetch` was flipped on live.
                await asyncio.to_thread(self._ensure_br)
                status = await asyncio.to_thread(self._setup_status)
                self._gap_reporter.report(status)
                blockers = setup_check.loop_blockers(status)
            except Exception:  # noqa: BLE001 — a broken probe must fail OPEN, never wedge the loop
                log.warning("[project_board] setup preflight errored — proceeding", exc_info=True)
                blockers = []
            if not blockers:
                if paused:
                    log.info("[project_board] loop resumed — setup gaps cleared")
                return True
            if not paused:
                log.warning("[project_board] loop paused: %s", setup_check.blocker_summary(status))
                paused = True
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
        return False

    # ── the puller ────────────────────────────────────────────────────────────
    async def _run(self):
        # Setup gate FIRST: with no `br` the recovery below can't even build a store,
        # and with no coder the first dispatch blocks every card. Pause (one warning,
        # cheap re-check each interval) instead of entering the tick loop.
        if not await self._setup_gate():
            return
        try:
            await self._recover()
        except Exception:  # noqa: BLE001 — recovery must never stop the loop from starting
            log.exception("[project_board] crash recovery failed")
        log.info("[project_board] recovery done — entering tick loop")
        while not self._stop.is_set() and not self._shutting_down:
            # Re-check at every tick boundary: a gap that opens mid-run (br removed,
            # the delegate deleted) pauses the ticks again; a passing preflight is a
            # `which` + a roster read and changes nothing about the tick itself.
            if not await self._setup_gate():
                return
            spawned = False
            try:
                await self._maybe_reconcile()
                await self._maybe_sweep()
                await self._maybe_preflight()  # fail-closed: hold work if the gate can't run
                # Under the claim lock so an on-demand board_dispatch (#390) evaluating the
                # SAME queue can never interleave with this tick into over-claiming past
                # max_concurrent or double-dispatching a card.
                async with self._claim_guard():
                    spawned = await self._spawn_ready()
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

    async def _spawn_ready(self) -> bool:
        """Claim Ready features up to the concurrency cap and spawn a drive for each,
        with two back-pressure gates: pause when too many PRs already await review
        (``max_pending_reviews``), and skip a candidate whose ``files_to_modify``
        overlap an in-flight build (the hot-file guard — two parallel coders editing
        the same file are a guaranteed merge conflict). Returns True if it started at
        least one drive (so the runner stays hot). Async so every store read/claim
        rides ``asyncio.to_thread`` (#258) — the scan must not stall the event loop;
        the drives themselves are still spawned on the loop (``create_task``).

        Every tick that reaches the claim scan emits ONE parseable ``claim_decision``
        ``log.info`` (#124): the fid(s) selected this tick and, for each higher-priority
        ``ready_queue`` candidate passed over, the structured reason it was skipped
        (hot-file overlap with which in-flight fid, ``claim()`` returned None, not
        ready/blocked). That is the evidence to tell a lost claim race from the hot-file
        guard from a ``ready_queue`` mis-ordering when a lower-priority card claims ahead
        of a higher one — the payload is JSON, so a future observer parses it without
        grepping log levels."""
        if len(self._drives) >= self.max_concurrent:
            self._last_claim_decision = {
                "gate": "max_concurrent",
                "selected": [],
                "skipped": [],
                "parked": [],
                "running": len(self._drives),
                "max_concurrent": self.max_concurrent,
            }
            return False
        # Fail-closed gate preflight, per-project (#90): a project whose gate can't run on
        # clean base HOLDS its own ready work (surfaced on the board) rather than dispatch
        # coders that can never pass it — but a broken gate in project A must NOT hold
        # project B, so this holds only the failed projects and the claim loop below skips
        # their candidates while continuing to dispatch every runnable project.
        if any(isinstance(v, str) for v in self._preflight_state.values()):
            await asyncio.to_thread(self._hold_ready_for_preflight)
        store = self._store()
        # Review-queue WIP limit — don't claim new work while the review queue is full.
        # (list_features is only read when the limit is enabled, exactly as before; the
        # count is captured so the board_dispatch diagnostic can report it, #390.)
        if self.max_pending_reviews:
            pending_reviews = len(await asyncio.to_thread(store.list_features, state="in_review"))
            if pending_reviews >= self.max_pending_reviews:
                self._last_claim_decision = {
                    "gate": "max_pending_reviews",
                    "selected": [],
                    "skipped": [],
                    "parked": [],
                    "pending_reviews": pending_reviews,
                    "max_pending_reviews": self.max_pending_reviews,
                }
                return False
        spawned = False
        # file → the in-flight (or claimed-this-tick) fid that owns it, so a hot-file
        # skip can NAME the build it collides with, not just report "some overlap".
        file_owner: dict[str, str] = {}
        for owner_fid, owner_files in self._inflight_files.items():
            for key in owner_files:
                file_owner.setdefault(key, owner_fid)
        busy = set(file_owner)
        selected: list[str] = []
        skipped: list[dict] = []  # {fid, reason, …} per passed-over candidate, priority order
        parked: list[str] = []  # #217: task beads claimed to in_progress without a slot (human-wait)
        ready = await asyncio.to_thread(store.ready_queue, relaxed=self.relaxed_gate)
        for candidate in ready:  # priority order, dep-unblocked
            if len(self._drives) >= self.max_concurrent:
                break  # remaining candidates are lower priority than what we already selected
            cid = candidate["id"]
            if candidate.get("board_state") != "ready" or candidate.get("blocked"):
                # a blocked-flagged feature can carry the `ready` label too
                reason = "blocked" if candidate.get("blocked") else f"state={candidate.get('board_state')}"
                skipped.append({"fid": cid, "reason": reason})
                continue
            # Per-project preflight hold (#90): this candidate's project can't run its
            # gate on clean base — skip it (it was flag_blocked above), but keep scanning
            # so a sibling in a HEALTHY project still gets claimed this tick.
            pname = self._project_name(candidate)
            if isinstance(self._preflight_state.get(pname), str):
                skipped.append({"fid": cid, "reason": "preflight-hold", "project": pname})
                continue
            # #217: task-type dispatch — a `task` bead ships a DELIVERABLE (a doc, a
            # decision, an artifact ref), not a diff, so it takes NO git worktree and
            # skips the hot-file guard below (with no worktree there is no file to
            # collide on). A sister-agent assignee — an ACP coder OR an A2A agent
            # (#304) — is driven via its native adapter (a real drive → counts toward
            # max_concurrent); a task assigned to the board's OWN agent (its coder name,
            # or the self/agent aliases) is driven first-party through HOST.invoke (#311,
            # also a real drive); a human/unassigned task is parked in_progress to await
            # async delivery (API/chat) and does NOT hold a slot.
            if candidate.get("issue_type") == LABEL_TASK:
                outcome = await self._dispatch_task(store, candidate)
                if outcome in ("acp", "a2a", "self"):
                    selected.append(cid)
                    spawned = True
                elif outcome == "parked":
                    parked.append(cid)
                else:  # "race" — lost the atomic claim, still ready for the next tick
                    skipped.append({"fid": cid, "reason": "claim-race"})
                continue
            # #197: key by (project, path) — bare paths false-collide across projects
            # (every repo has PROTO.md); an unstamped card ("" project) behaves as before.
            files = {(pname, p) for p in (candidate.get("files_to_modify") or [])}
            overlap = files & busy
            if overlap:
                # would edit a file an in-flight build owns → defer a tick
                owners = sorted({file_owner[k] for k in overlap})
                skipped.append(
                    {
                        "fid": cid,
                        "reason": "hot-file",
                        "overlaps": owners,
                        "files": sorted(path for _proj, path in overlap),
                    }
                )
                continue
            claimed = await asyncio.to_thread(store.claim, cid, assignee=self.coder_name)
            if claimed is None:
                skipped.append({"fid": cid, "reason": "claim-race"})  # raced / no longer ready
                continue
            self._inflight_files[claimed["id"]] = files
            for key in files:
                file_owner.setdefault(key, claimed["id"])
            task = asyncio.create_task(self._drive(claimed), name=f"pb-drive-{claimed['id']}")
            self._drives.add(task)
            _register_drive(claimed["id"], task)  # reachable by the cancel verbs (#211)
            task.add_done_callback(self._make_drive_done_cb(claimed["id"]))
            busy |= files
            selected.append(claimed["id"])
            spawned = True
        if selected or skipped or parked:
            log.info(
                "[project_board] claim_decision %s",
                json.dumps(
                    {"parked": parked, "selected": selected, "skipped": skipped},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
        # Record the claim scan for the on-demand dispatch diagnostic (board_dispatch, #390):
        # the same selected/skipped/parked the claim_decision line carries, read back by
        # dispatch_now so an empty board is distinguishable from a held/stalled one.
        self._last_claim_decision = {
            "gate": "scan",
            "selected": list(selected),
            "skipped": list(skipped),
            "parked": list(parked),
            "running": len(self._drives),
            "max_concurrent": self.max_concurrent,
        }
        # #356: bound ready-queue skip livelocks — an indefinitely repeating skip enters
        # the blocked/escalation path rather than retrying forever.
        await self._bound_ready_skips(store, selected, parked, skipped)
        return spawned

    def _ready_skip_max(self) -> int:
        """The consecutive-skip threshold before a livelocked ready card is flagged
        blocked (#356) — the `ready_skip_max` cfg key, floored at 2 so a single/transient
        claim race can NEVER be terminal (r5). A malformed value falls back to the
        default. Read live (not cached at construction) so an operator can retune it
        without a restart, like the other cfg-read knobs."""
        try:
            n = int(self.cfg.get("ready_skip_max", _READY_SKIP_MAX_DEFAULT))
        except (TypeError, ValueError):
            n = _READY_SKIP_MAX_DEFAULT
        return max(2, n)

    async def _bound_ready_skips(self, store, selected: list, parked: list, skipped: list) -> None:
        """Bound ready-queue skip livelocks (#356).

        Track CONSECUTIVE identical skip outcomes per ``(fid, reason)``; after
        ``_ready_skip_max`` repeats of the same UNRESOLVED reason
        (``_is_livelock_skip_reason`` — a lost claim race, a stuck dispatch target, or any
        future genuinely-stuck reason, but NOT a hot-file wait an in-flight build is
        actively resolving), flag the card blocked with actionable evidence so the
        existing blocked sweep / operator-notification machinery surfaces the
        otherwise-invisible ready-but-unclaimable state.

        The counter RESETS on any sign of progress, so a single/transient claim race never
        blocks prematurely: a successful claim (drive started) or park (task reached
        in_progress), a CHANGED skip reason (a fresh window), or the card LEAVING the ready
        queue entirely (claimed elsewhere, become dependency-blocked, or closed — it is no
        longer a candidate this tick, so its counter is dropped). Best-effort: a bookkeeping
        failure never kills the tick."""
        counters = getattr(self, "_ready_skips", None)
        if counters is None:
            counters = self._ready_skips = {}
        threshold = self._ready_skip_max()
        # A card claimed (drive started) or parked (task reached in_progress) made
        # progress — its skip window is over.
        seen: set[str] = set(selected) | set(parked)
        for fid in list(seen):
            counters.pop(fid, None)
        for item in skipped:
            fid = item.get("fid")
            if not fid:
                continue
            reason = str(item.get("reason") or "")
            seen.add(fid)
            prev = counters.get(fid)
            # Same reason as last tick → advance the streak; a changed reason (or a first
            # sighting) opens a fresh window — a reason change is itself progress (r5).
            count = prev[1] + 1 if (prev and prev[0] == reason) else 1
            counters[fid] = (reason, count)
            if count >= threshold and _is_livelock_skip_reason(reason):
                counters.pop(fid, None)  # now blocked → leaves the ready lane; stop counting it
                evidence = (
                    f"ready-queue livelock: skipped {count} consecutive ticks as {reason!r} and never "
                    "claimed — a ready card the puller cannot dispatch (a stuck dispatch target, or a "
                    "claim race that will not clear). Flagged blocked for operator triage (#356)."
                )
                log.warning(
                    "[project_board] %s ready-queue livelock — flagging blocked after %d consecutive %r skips",
                    fid,
                    count,
                    reason,
                )
                try:
                    # category=terminal → the blocked sweep escalates to the operator
                    # (not a self-healing class), making the stall visible (r6).
                    await asyncio.to_thread(store.flag_blocked, fid, evidence, category="terminal")
                except Exception:  # noqa: BLE001 — bookkeeping must never kill the tick
                    log.warning("[project_board] %s ready-queue livelock flag_blocked failed", fid, exc_info=True)
        # Any card tracked from a prior tick but NOT seen this tick left the ready queue
        # (claimed elsewhere, dependency-blocked, or closed) — progress → drop its counter.
        for fid in list(counters):
            if fid not in seen:
                counters.pop(fid, None)

    # ── on-demand queue dispatch (board_dispatch, #390) ───────────────────────────
    def _claim_guard(self):
        """The ``asyncio.Lock`` serializing the ready-queue claim scan. The periodic tick
        and the on-demand ``dispatch_now`` both hold it around ``_spawn_ready``, so an
        interactive dispatch can never race the tick into over-claiming past
        ``max_concurrent`` or double-dispatching a card (r3/r4). Lazily created — no
        BoardLoop ``__init__`` change — so it binds to whichever running event loop first
        drives a scan (the one the surface and the tools share)."""
        lock = getattr(self, "_claim_lock", None)
        if lock is None:
            lock = self._claim_lock = asyncio.Lock()
        return lock

    def _dispatch_disabled_record(self) -> dict:
        """The decision record for a DISABLED loop — no scan, no claim (r4). A disabled
        loop is still registered (``start`` publishes it before the enabled gate), so the
        diagnostic can report the disabled state rather than a silent no-op."""
        return {
            "dispatched": [],
            "outcome": "loop-disabled",
            "detail": (
                "the board loop is disabled (project_board.loop_enabled=false) — no card is "
                "dispatched until it is enabled in Settings ▸ Project Board"
            ),
            "running": len(self._drives),
            "max_concurrent": self.max_concurrent,
            "skipped": [],
            "parked": [],
        }

    def _dispatch_error_record(self, stage: str, exc: Exception, dispatched=None) -> dict:
        """The decision record for an UNEXPECTED crash in an on-demand dispatch stage (the
        fail-closed preflight or the claim scan raised — not a gate verdict, which never
        raises). A crashed diagnostic is a result, never an exception into the agent loop.
        If the claim scan had already started drive(s) before it raised, their feature ids
        are carried in ``dispatched`` so a partial-then-crashed pass is reported rather than
        silently lost (the claim is real; the caller must still see it)."""
        dispatched = sorted(dispatched or [])
        detail = f"the {stage} errored: {type(exc).__name__}: {exc}"
        if dispatched:
            detail = f"dispatched {', '.join(dispatched)} before " + detail
        return {
            "dispatched": dispatched,
            "outcome": "error",
            "detail": detail,
            "running": len(self._drives),
            "max_concurrent": self.max_concurrent,
            "skipped": [],
            "parked": [],
        }

    def _drive_fids(self, tasks) -> list:
        """Feature ids for the given drive tasks, recovered from their ``pb-*-<fid>`` task
        names (``pb-drive-`` coding drives, ``pb-task-`` sister-agent tasks, ``pb-self-``
        first-party tasks). ``self._drives`` holds asyncio Tasks, not ids, so a task-set
        diff has to be mapped back through the stable drive-name prefixes to report which
        cards an on-demand scan had already dispatched when it raised mid-pass."""
        prefixes = ("pb-drive-", "pb-task-", "pb-self-")
        fids = []
        for t in tasks:
            name = t.get_name() if hasattr(t, "get_name") else ""
            for prefix in prefixes:
                if name.startswith(prefix):
                    fids.append(name[len(prefix):])
                    break
        return sorted(fids)

    async def dispatch_now(self) -> dict:
        """Evaluate the ready queue right now instead of waiting for the next interval —
        the observable entry point behind the ``board_dispatch`` tool (#390).

        This is NOT a second scheduler: it runs the very ``_spawn_ready`` a periodic tick
        runs, under the same claim lock, and bypasses nothing — ``loop_enabled``, the
        per-project preflight hold, ``max_concurrent``, the review-WIP limit and the
        hot-file guard all apply exactly as in a tick. In particular it runs the SAME
        fail-closed ``_maybe_preflight`` a tick runs immediately before its claim scan
        (``_run``): without it an interactive kick could claim work past a newly required
        or freshly failing per-project gate that the next tick would have held. So a
        repeated or concurrent call can never double-claim or over-dispatch an in-flight
        feature: a card the tick already claimed is gone from the ready queue, and a full
        slot / WIP ceiling / preflight hold holds the scan (r3/r4).

        Returns a decision record — ``{dispatched, outcome, detail, running,
        max_concurrent, skipped, parked}`` — whose ``outcome`` tells the principal cases
        apart: ``dispatched`` (with the claimed feature id[s] in ``dispatched``),
        ``empty-queue``, ``at-capacity``, ``review-wip-limit``, ``all-candidates-held``
        (every ready card was blocked/held/hot-file-deferred or lost a claim race — see
        ``skipped``), ``parked`` (a task claimed to in_progress awaiting async delivery),
        ``loop-disabled``, or ``error`` (a dispatch stage — the preflight or the claim scan
        — raised unexpectedly; it is captured as a record, never re-raised into the agent
        loop, and any drive already started before the crash is still named in
        ``dispatched``)."""
        if not self.enabled:
            return self._dispatch_disabled_record()
        # Fail-closed preflight FIRST, exactly as the periodic tick does before its claim
        # scan (`_run`): a newly-required or freshly-failing per-project gate must hold this
        # on-demand dispatch too, or an interactive kick could claim work the next tick
        # would have held. Run it OUTSIDE the claim lock — same as the tick — since it
        # smokes a gate subprocess and must not serialize the claim scan.
        try:
            await self._maybe_preflight()
        except Exception as exc:  # noqa: BLE001 — a preflight crash is a diagnostic result, not a crash
            log.warning("[project_board] board_dispatch: preflight evaluation failed", exc_info=True)
            return self._dispatch_error_record("preflight evaluation", exc)
        async with self._claim_guard():
            drives_before = set(getattr(self, "_drives", ()) or ())  # drive-task snapshot
            try:
                await self._spawn_ready()
            except Exception as exc:  # noqa: BLE001 — a failed scan is a diagnostic result, not a crash
                log.warning("[project_board] board_dispatch: ready-queue evaluation failed", exc_info=True)
                # `_spawn_ready` writes `_last_claim_decision` only on a clean pass, so a
                # mid-scan crash loses it — recover the feature ids of any drive it DID
                # start from the drive-task set diff, so a partial-then-crashed pass still
                # reports what it dispatched rather than swallowing it (r1/r3).
                started = self._drive_fids(set(getattr(self, "_drives", ()) or ()) - drives_before)
                return self._dispatch_error_record("ready-queue evaluation", exc, dispatched=started)
            decision = dict(getattr(self, "_last_claim_decision", None) or {})
        return self._dispatch_decision_record(decision)

    def _dispatch_decision_record(self, decision: dict) -> dict:
        """Turn a ``_spawn_ready`` scan decision into the human-readable dispatch record.
        Pure over ``decision`` (+ the current knobs) so the outcome mapping is testable on
        its own and the tick's claim path stays untouched."""
        gate = decision.get("gate")
        selected = list(decision.get("selected") or [])
        skipped = list(decision.get("skipped") or [])
        parked = list(decision.get("parked") or [])
        running = decision.get("running", len(self._drives))
        if gate == "max_concurrent":
            outcome = "at-capacity"
            detail = (
                f"concurrency limit reached: {running}/{self.max_concurrent} drives already in flight — "
                "no new card is claimed until a slot frees (max_concurrent)"
            )
        elif gate == "max_pending_reviews":
            pending = decision.get("pending_reviews")
            outcome = "review-wip-limit"
            detail = (
                f"review WIP limit reached: {pending}/{self.max_pending_reviews} PRs already await review — "
                "the loop pauses new claims until the review queue drains (max_pending_reviews)"
            )
        elif selected:
            outcome = "dispatched"
            detail = f"dispatched {', '.join(selected)} — the loop claimed it and a drive is now running"
        elif not skipped and not parked:
            outcome = "empty-queue"
            detail = "the ready queue is empty — no card is ready to dispatch"
        elif parked and not skipped:
            outcome = "parked"
            detail = (
                f"claimed {', '.join(parked)} to in_progress awaiting async delivery — no coding drive was "
                "started and no concurrency slot is held"
            )
        else:
            reasons = sorted({str(s.get("reason") or "") for s in skipped if s.get("reason")})
            outcome = "all-candidates-held"
            detail = (
                "every ready candidate was passed over ("
                + (", ".join(reasons) or "no runnable candidate")
                + ") — blocked/held cards, a per-project preflight hold, a hot-file wait, or a lost claim "
                "race; nothing could be dispatched this pass"
            )
        return {
            "dispatched": selected,
            "outcome": outcome,
            "detail": detail,
            "running": running,
            "max_concurrent": self.max_concurrent,
            "skipped": skipped,
            "parked": parked,
        }

    def _make_drive_done_cb(self, fid: str):
        """A drive task's done-callback: drop it from the running set and release the
        files it held (so a deferred file-conflicting candidate can be claimed next)."""

        def _cb(task: asyncio.Task):
            self._drives.discard(task)
            self._inflight_files.pop(fid, None)
            _unregister_drive(fid, task)
            self._cancel_done.discard(fid)  # a later drive of the same card gets its own cancel edge
            # #258: drop any write-back futures a failed dispatch left un-awaited —
            # the executor still runs them (fire-and-forget); only the parking is freed.
            self._bg_records.pop(fid, None)

        return _cb

    # ── task-type dispatch (#217) ─────────────────────────────────────────────
    async def _dispatch_task(self, store, candidate: dict) -> str:
        """Claim + dispatch a ``task`` bead, branching on its assignee. Returns:

        - ``"acp"`` / ``"a2a"`` — the assignee resolves to a sister-agent delegate of
          that type (ACP coder or A2A agent, #304) → claimed and a ``_drive_task`` was
          spawned (into ``self._drives``, so it counts toward ``max_concurrent`` exactly
          like a coding drive). The caller treats both alike: a drive was started.
        - ``"self"`` — the assignee is the board's OWN agent (#311) → a first-party
          ``_drive_self_task`` was spawned through ``HOST.invoke`` (also into
          ``self._drives`` → counts toward ``max_concurrent``). Caller: a drive started.
        - ``"parked"`` — a human/unassigned task, or a name that resolves to NEITHER
          type (or a self task with no host seam / one already in flight) → claimed to
          ``in_progress`` and left there to await async delivery (API/chat). No drive,
          no slot held.
        - ``"race"`` — the atomic claim was lost (someone else took it); still ready.

        A task assigned to THIS board's OWN agent (its coder name, or the reserved
        ``self``/``agent`` aliases) is NOT a sister-agent dispatch — it is first-party
        work the host does itself through ``HOST.invoke`` (#311). That is detected BEFORE
        delegate resolution (the board's own name always means "I do it") and delegated to
        ``_dispatch_self``, which can also return ``"self"`` (a first-party drive spawned).

        The assignee is read from the CANDIDATE (the pre-claim projection) and passed
        back to ``claim`` so the claim preserves it — a task's assignee is its dispatch
        target, not the coder identity a coding claim stamps."""
        cid = candidate["id"]
        assignee = str(candidate.get("assignee") or "").strip()
        if self._is_self_assignee(assignee):
            return await self._dispatch_self(store, candidate, assignee)
        delegate = self._resolve_task_delegate(assignee)
        # #356: claim_task, NOT claim — a task's assignee is its DISPATCH TARGET, so the
        # transition to in_progress must PRESERVE it (`br --claim` would reassign to the
        # actor and refuse the already-assigned bead, the livelock this fixes).
        claimed = await asyncio.to_thread(store.claim_task, cid, assignee=assignee)
        if claimed is None:
            return "race"
        if delegate is None:
            # Human assignee, none, or a name that resolves to NEITHER an ACP coder nor
            # an A2A sister agent: nothing to dispatch. The task is now in_progress; its
            # deliverable arrives out-of-band (record_delivery via the API/chat). The
            # loop moves on and this parked card holds no slot.
            log.info(
                "[project_board] %s task parked in_progress — awaiting %s delivery (assignee=%s)",
                cid,
                "human" if assignee else "unassigned",
                assignee or "<none>",
            )
            return "parked"
        # A sister-agent assignee (ACP coder OR A2A agent, #304) → drive it via its
        # native adapter. No worktree, so it owns no files (empty set: present so the
        # done-callback's pop and the sweep's live-drive check both see it, but colliding
        # with nothing in the hot-file guard).
        self._inflight_files[cid] = set()
        task = asyncio.create_task(self._drive_task(claimed, delegate), name=f"pb-task-{cid}")
        self._drives.add(task)
        _register_drive(cid, task)  # reachable by the cancel verbs (#211)
        task.add_done_callback(self._make_drive_done_cb(cid))
        return str(getattr(delegate, "type", "") or "acp")

    def _is_self_assignee(self, assignee: str) -> bool:
        """True when a task's assignee names THIS board's own agent — the reserved
        ``self``/``agent`` aliases, or the board's configured ``coder`` name (#311). Such
        a task is dispatched through ``HOST.invoke`` (first-party), NOT a sister-agent
        delegate. Case-insensitive; an empty assignee (human/unassigned) is never self,
        and the aliases resolve even when no ``coder`` name is configured."""
        a = assignee.strip().lower()
        if not a:
            return False
        if a in self._SELF_ASSIGNEE_ALIASES:
            return True
        return bool(self.coder_name) and a == self.coder_name.strip().lower()

    async def _dispatch_self(self, store, candidate: dict, assignee: str) -> str:
        """Dispatch a task assigned to the board's OWN agent through ``HOST.invoke`` (#311)
        — the first-party sibling of ``_dispatch_task``'s sister-agent path. The board is
        the assignee: rather than shell out to a delegate, it drives its own ``invoke``
        seam with the task prompt + a stable per-card session id and records the reply via
        the SAME ``record_delivery`` lifecycle (→ in_review for independent board_verify).
        Returns like ``_dispatch_task``:

        - ``"self"`` — a first-party drive was spawned (into ``self._drives`` → counts
          toward ``max_concurrent``, exactly like an ACP/A2A task drive).
        - ``"parked"`` — the host predates the ``invoke`` seam (r2), OR a self-dispatch is
          already in flight for this board (r3): claimed to ``in_progress`` and left, no
          drive, no slot. Preserves the existing park behaviour when self work can't run.
        - ``"race"`` — the atomic claim was lost.

        Only ONE self-dispatch runs per board at a time (``self._self_inflight``): a second
        self-assigned task parks rather than invoking the host recursively/concurrently."""
        cid = candidate["id"]
        invoke = coder_seam.resolve_self_invoke()
        # r2: a host with no HOST.invoke seam (older host / host-free env) parks with the
        # existing log line. r3: a self-dispatch already in flight → park the second
        # rather than recurse. Both claim to in_progress and hold no slot, exactly like a
        # human/unassigned park.
        # r3b: `_self_inflight` is cleared by the drive's done-callback, which fires as soon
        # as the drive is CANCELLED — while an uncancellable worker thread may still be
        # executing on the host. `host_invoke_busy()` is owned by that thread, so it stays
        # true until the call really returns and the second task parks instead of racing it.
        if invoke is None or self._self_inflight or coder_seam.host_invoke_busy():
            # #356: claim_task preserves the self dispatch target (`self`/`agent`/the
            # board's own name) — `--claim` would rewrite it to the actor and refuse it.
            claimed = await asyncio.to_thread(store.claim_task, cid, assignee=assignee)
            if claimed is None:
                return "race"
            why = "no HOST.invoke seam" if invoke is None else "a self-dispatch is already in flight"
            log.info(
                "[project_board] %s self task parked in_progress — awaiting agent delivery (%s)",
                cid,
                why,
            )
            return "parked"
        # #356: claim_task preserves the self dispatch target on the transition to
        # in_progress (see the park branch above) — never `--claim`.
        claimed = await asyncio.to_thread(store.claim_task, cid, assignee=assignee)
        if claimed is None:
            return "race"
        # Raise the one-in-flight guard BEFORE the drive is spawned, and clear it from the
        # drive's done-callback (``_make_self_done_cb``) — which asyncio runs for EVERY
        # terminal state, including a cancel before the coroutine ever starts — so the
        # guard can never leak. A self-drive owns no worktree/files (empty set, like the
        # sister-agent task path) but IS a real drive counting toward max_concurrent.
        self._self_inflight = True
        self._inflight_files[cid] = set()
        session_id = f"board-self-{cid}"  # stable per card
        task = asyncio.create_task(self._drive_self_task(claimed, invoke, session_id), name=f"pb-self-{cid}")
        self._drives.add(task)
        _register_drive(cid, task)  # reachable by the cancel verbs (#211)
        task.add_done_callback(self._make_self_done_cb(cid))
        return "self"

    def _make_self_done_cb(self, fid: str):
        """Done-callback for a self-dispatch drive (#311): clear the one-in-flight guard —
        so the next self-assigned task can dispatch — on TOP of the standard drive cleanup.
        Runs for every terminal state asyncio can reach (success, failure, or a cancel
        even before the coroutine starts), so ``_self_inflight`` can never leak."""
        base = self._make_drive_done_cb(fid)

        def _cb(task: asyncio.Task):
            self._self_inflight = False
            base(task)

        return _cb

    async def _drive_self_task(self, feature: dict, invoke, session_id: str) -> None:
        """Drive a task assigned to the board's OWN agent through ``HOST.invoke`` (#311) —
        the first-party sibling of ``_drive_task``. No worktree, no git, no PR: build the
        same deliverable prompt, drive ``coder_seam.dispatch_self`` (bounded by
        ``coder_timeout_s``), then record the reply as the deliverable (``record_delivery``
        → in_review, where the board's independent ``board_verify`` closes it). A dispatch
        failure/timeout is classified and blocks the card EXACTLY like the sister-agent
        task path (r5) — a self task has no model ladder to climb (the assignee is the
        host itself), so the capability-escalation edge collapses to a terminal block."""
        store = self._store()
        fid = feature["id"]
        prompt = self._build_task_prompt(feature)
        try:
            reply = await coder_seam.dispatch_self(invoke, prompt, session_id, timeout=self.coder_timeout or None)
        except asyncio.CancelledError:
            raise  # operator cancel / shutdown owns the edge — never a block
        except (worktree.WorktreeError, worktree.CoderTimeout) as exc:
            if self._shutting_down:
                log.info("[project_board] %s self task dispatch aborted by shutdown — no block", fid)
                return
            policy = classify(str(exc))
            log.warning("[project_board] %s self task blocked (%s): %s", fid, policy.category, exc)
            await asyncio.to_thread(store.flag_blocked, fid, f"{policy.category}: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 — unexpected; block, don't crash the loop
            log.exception("[project_board] %s self task dispatch unexpected failure", fid)
            await asyncio.to_thread(store.flag_blocked, fid, f"unexpected: {type(exc).__name__}: {exc}")
            return
        await asyncio.to_thread(store.record_delivery, fid, text=reply or "")
        log.info("[project_board] %s self task delivered (%d chars) → in_review", fid, len(reply or ""))

    async def _drive_task(self, feature: dict, delegate) -> None:
        """Drive a ``task`` bead with a sister-agent assignee — an ACP coder OR an A2A
        agent (#217/#304) — the task sibling of ``_drive``. No worktree, no git, no PR:
        dispatch the spec + acceptance criteria to the assignee delegate over its native
        adapter (``coder_seam.dispatch_task`` picks acp/a2a by the delegate's type),
        then record the reply as the deliverable (``record_delivery`` → in_review, where
        a human or auto-verify closes it). A dispatch failure/timeout is classified like
        a coder failure and blocks the card for triage — a task has no model ladder to
        climb (the assignee is fixed), so the capability-escalation edge collapses to the
        coder path's terminal block."""
        store = self._store()
        fid = feature["id"]
        prompt = self._build_task_prompt(feature)
        try:
            reply = await coder_seam.dispatch_task(delegate, prompt, timeout=self.coder_timeout or None)
        except asyncio.CancelledError:
            raise  # operator cancel / shutdown owns the edge — never a block
        except (worktree.WorktreeError, worktree.CoderTimeout) as exc:
            if self._shutting_down:
                log.info("[project_board] %s task dispatch aborted by shutdown — no block", fid)
                return
            policy = classify(str(exc))
            log.warning("[project_board] %s task blocked (%s): %s", fid, policy.category, exc)
            await asyncio.to_thread(store.flag_blocked, fid, f"{policy.category}: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 — unexpected; block, don't crash the loop
            log.exception("[project_board] %s task dispatch unexpected failure", fid)
            await asyncio.to_thread(store.flag_blocked, fid, f"unexpected: {type(exc).__name__}: {exc}")
            return
        await asyncio.to_thread(store.record_delivery, fid, text=reply or "")
        log.info("[project_board] %s task delivered (%d chars) → in_review", fid, len(reply or ""))

    def _record_bg(self, fid: str, label: str, fn, *args, **kwargs) -> None:
        """Run one store write-back on a worker thread WITHOUT awaiting it (#258) —
        the offload for coder_seam's ``record_gens``/``record_verified`` callbacks,
        which are sync and invoked ON the event loop mid-dispatch (so the call site
        can't reach ``asyncio.to_thread`` itself). The future is parked per fid;
        ``_await_bg_records`` is the drive's barrier. Failures are logged and dropped
        — both call sites are documented fire-and-forget/best-effort."""

        def _call() -> None:
            try:
                fn(*args, **kwargs)
            except Exception:  # noqa: BLE001 — fire-and-forget: never raise into the executor
                log.warning("[project_board] %s background %s write failed (ignored)", fid, label, exc_info=True)

        self._bg_records.setdefault(fid, []).append(asyncio.get_running_loop().run_in_executor(None, _call))

    async def _await_bg_records(self, fid: str) -> None:
        """Barrier for ``_record_bg``'s in-flight write-backs. The drive awaits it as
        soon as dispatch returns, so gens/verified records have landed on the bead
        before the PR opens — the same ordering the synchronous callbacks gave, just
        off the event loop. Never raises (``_record_bg`` swallows per-write)."""
        for fut in self._bg_records.pop(fid, []):
            await fut

    async def _drive(self, feature: dict):
        """Drive one feature ready→in_review (or →blocked). `done` is set later by
        the merge webhook. With per-tier coders configured, a *capability* failure
        (coder errored / produced no diff) climbs the ladder; with a single coder
        it blocks at once — no redundant tier dance."""
        store = self._store()
        fid = feature["id"]
        # #90: resolve repo/base/coders from THIS feature's project, not the instance
        # default — so a multi-repo board builds each feature in its own checkout and
        # escalates through the ladder its project declares.
        repo = self._repo_for(feature)
        base = self._base_branch_for(feature)
        coders = self._coders_for(feature)
        title = f"feat: {feature['title']}"
        raw_title = feature.get("title") or ""  # #227: slugged onto the branch/dir tail
        tier = (await asyncio.to_thread(store.current_tier, fid)) if self.escalation_on else ""
        retries = 0  # transient-failure retries at the current tier (reset on a climb)
        # Which sibling of the current rung to use. Seeded from a per-process counter so
        # consecutive cards do not all open on the same provider — spread is the ordinary
        # case; the failover below is the exceptional one.
        sib = _next_rung_cursor()
        tried_here = 0  # siblings already exhausted at THIS rung (reset on a climb)
        wt = branch = None
        pr_url = None  # set once open_pr returns — the cancel paths below close it (#211)
        keep_wt = False  # reuse the worktree on a goal-fix retry (keep the impl; add tests)
        try:
            while True:
                # Rebuild the prompt each attempt so a re-dispatch (CI bounce,
                # goal-verify gap, or tier escalation) picks up the latest
                # _ci_feedback + _ci_prior_diff. Fetch this area's distilled lessons
                # from the KG (best-effort, async) and inject them — the flywheel READ.
                lessons = await self._fetch_kg_lessons(feature)
                prompt = self._build_prompt(feature, lessons=lessons)
                # Which PROVIDER at this rung. `siblings` are interchangeable delegates for
                # the same capability tier (#362); `sib` advances only on a quota failure
                # (below) or round-robin across dispatches, never on a capability failure —
                # that is what climbing a rung is for.
                siblings = coders.get(tier) if self.escalation_on else None
                if not siblings:
                    siblings = [self.coder_name]
                coder_name = siblings[sib % len(siblings)]
                coder = self._resolve_delegate(coder_name, "acp")
                if coder is None:
                    await asyncio.to_thread(
                        store.flag_blocked, fid, f"coder delegate {coder_name!r} not configured/enabled"
                    )
                    return
                try:
                    # How this attempt gets its worktree + coder result:
                    #  • keep_wt  → REUSE the kept worktree (impl intact), one re-dispatch.
                    #    A goal-fix/gate-fix retry must not throw the implementation away —
                    #    the coder only ADDS what the reviewer flagged (usually tests); a
                    #    fresh rebuild makes it re-implement and never reach the tests (the
                    #    bd-2fd/bd-3cj block).
                    #  • coder.solve (ADR 0064 P2, opt-in) → the execution-grounded
                    #    ladder over the feature's acceptance tests (coder_seam.py).
                    #    Same "from-scratch build only" rule as max-mode: a carried-
                    #    forward re-dispatch FIXES the existing diff with one coder.
                    #    Only preempts max-mode when max_mode_n<=1 (_use_coder_solve) —
                    #    a board already running Max-Mode keeps that behavior.
                    #  • max-mode → N parallel candidates, judge, promote the winner (#21).
                    #    ONLY for a from-scratch build: a carried-forward re-dispatch (a CI
                    #    bounce / goal-fix / gate-fix — all signalled by _ci_feedback) FIXES
                    #    the existing diff with one coder, so it must NOT re-fan-out N.
                    #  • otherwise → one fresh worktree, one dispatch.
                    if keep_wt and wt is not None:
                        keep_wt = False  # consume the reuse
                        self._inflight[fid] = (repo, wt, branch)
                        # Tap this re-dispatch into the live monitor (#84) — same gen 1,
                        # continuing the current build (no progress_new_run, so the drawer
                        # keeps the prior history rather than blanking on a keep-worktree fix).
                        # new_dispatch opens a fresh model-reached epoch WITHOUT clearing the
                        # buffer, so the prior dispatch's still-visible gens can't answer the
                        # pre-model check for this re-dispatch (#339 review): a re-run that
                        # dies below the seam reads as "no model work THIS dispatch" and
                        # blocks for infra triage instead of climbing the tier ladder.
                        result = await coder_seam.dispatch_coder_tapped(
                            coder,
                            wt,
                            prompt,
                            fid=fid,
                            gen=1,
                            tier=tier,
                            timeout=self.coder_timeout or None,
                            new_dispatch=True,
                        )
                    elif self._use_coder_solve(feature) and not self._ci_feedback.get(fid):
                        files_to_modify = feature.get("files_to_modify") or []
                        # #90: every solve() knob resolves from THIS feature's project.
                        solve = self._coder_solve_settings(feature)
                        fusion = (
                            self._resolve_delegate(solve["fusion_delegate"], "openai")
                            if solve["fusion_delegate"]
                            else None
                        )
                        if fusion is not None:
                            # Gate BEFORE dispatch: fusion can't tool-call and returns
                            # whole-file replacements, so an oversized file risks a
                            # silent truncated rewrite (coder_seam.fusion_viable_for_files).
                            # Not viable ⇒ this dispatch just skips the fusion rung — the
                            # ladder still runs greedy/best-of-k/tree-search unchanged.
                            viable, reason = coder_seam.fusion_viable_for_files(
                                repo,
                                files_to_modify,
                                max_file_chars=solve["fusion_max_file_chars"],
                                max_total_chars=solve["fusion_max_total_chars"],
                            )
                            if not viable:
                                log.info("[project_board] %s fusion rung skipped for this dispatch: %s", fid, reason)
                                fusion = None
                        coder_seam.progress_new_run(fid)  # fresh build → fresh monitor (#84)
                        wt, branch, result = await coder_seam.dispatch(
                            task=prompt,
                            coder=coder,
                            repo=repo,
                            base=base,
                            root=self.root,
                            fid=fid,
                            dispatch_timeout=self.coder_timeout or None,
                            test_cmd=solve["test_cmd"],
                            test_timeout=solve["test_timeout"],
                            budget=solve["budget"],
                            k=solve["k"],
                            tree_depth=solve["tree_depth"],
                            record_gens=lambda n: self._record_bg(fid, "record_gens", store.record_gens_spent, fid, n),
                            fusion_delegate=fusion,
                            fusion_k=solve["fusion_k"],
                            files_to_modify=files_to_modify,
                            fusion_max_file_chars=solve["fusion_max_file_chars"],
                            # #86: same host-env strip the gate/preflight/format spawns
                            # get — keep the whitelist consistent across every subprocess.
                            env_passthrough=self.env_passthrough,
                            tier=tier,  # #84: label each solve gen with the current tier
                            # #91: persist the verified candidate on the bead at the
                            # verify boundary, so a crash before open_pr is salvageable.
                            record_verified=lambda br_name, sha, wt_path: self._record_bg(
                                fid,
                                "record_verified",
                                store.record_verified_candidate,
                                fid,
                                branch=br_name,
                                sha=sha,
                                worktree=wt_path,
                            ),
                            commit_message=title,
                            title=raw_title,  # #227: canonical branch/dir slug tail
                            max_concurrent_sessions=solve["max_concurrent_sessions"],
                        )
                        self._inflight[fid] = (repo, wt, branch)
                    elif self.max_mode_n > 1 and not self._ci_feedback.get(fid):
                        coder_seam.progress_new_run(fid)  # fresh build → fresh monitor (#84)
                        wt, branch, result = await self._dispatch_max_mode(
                            feature, coder, prompt, repo, base, fid, tier
                        )
                        self._inflight[fid] = (repo, wt, branch)
                    else:
                        coder_seam.progress_new_run(fid)  # fresh build → fresh monitor (#84)
                        # A card with an open PR is on a FIX ROUND: resume its branch so
                        # the coder sees the change it is being asked to correct (#332 —
                        # three 30-minute rounds off a clean base, zero commits).
                        wt, branch = await worktree.create_worktree(
                            repo, base, fid, self.root, title=raw_title, resume=bool(feature.get("pr_url"))
                        )
                        self._inflight[fid] = (repo, wt, branch)  # track for shutdown reaping
                        result = await coder_seam.dispatch_coder_tapped(
                            coder, wt, prompt, fid=fid, gen=1, tier=tier, timeout=self.coder_timeout or None
                        )  # taps live monitor (#84); reaps subprocess; CoderTimeout if it overruns
                    # #258 barrier: any record_gens/record_verified write-backs the
                    # dispatch scheduled on worker threads land before the drive
                    # proceeds toward open_pr (the pre-offload ordering).
                    await self._await_bg_records(fid)
                    # Requirement-ledger write-back (#113): merge the reply's
                    # `## Requirements` dispositions into the ledger and persist it on
                    # the bead — the LOOP writes dispositions, never the coder, and the
                    # completion gate below reads the ledger, never the reply text.
                    # Silence leaves an item open. The bead write is best-effort (the
                    # gate still checks the in-hand merged ledger), but a re-dispatch
                    # after requeue re-projects from the bead, so a lost write only
                    # costs a repeat disposition, never a false "disposed".
                    ledger = list(feature.get("requirements") or [])
                    if ledger:
                        ledger = apply_requirement_dispositions(ledger, _parse_requirements_reply(result or ""))
                        feature["requirements"] = ledger
                        try:
                            await asyncio.to_thread(store.set_requirements, fid, ledger)
                        except Exception:  # noqa: BLE001 — bookkeeping must not fail the build
                            log.warning(
                                "[project_board] %s requirement write-back failed (gate still checks "
                                "the merged ledger)",
                                fid,
                                exc_info=True,
                            )
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
                            n = await self._budget_get(store, fid, "goal-fix", feature)
                            if n < self.goal_fix_max:
                                await self._budget_set(store, fid, "goal-fix", n + 1)
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
                    await self._run_fixups(wt, feature)
                    # Pre-PR local gate: run the repo's real checks in the worktree and, on
                    # failure, hand the coder the actual output to fix IN-WORKTREE before a PR
                    # (and a CI round-trip) ever opens. Same-tier, keep-worktree, bounded by
                    # local_gate_max; on exhaustion open the PR anyway (CI is the backstop).
                    gate_out = await self._run_local_gate(wt, feature)
                    if gate_out is not None:
                        n = await self._budget_get(store, fid, "gate-fix", feature)
                        if n < self.local_gate_max:
                            await self._budget_set(store, fid, "gate-fix", n + 1)
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
                    # Completion gate (#113, the planSpec mechanism): a feature may NOT
                    # reach in_review with OPEN requirement items — the ledger decides,
                    # not the coder's say-so. Same seam as the local gate (after the
                    # coder's fix rounds, before the PR opens). Open items bounce back
                    # same-tier/keep-worktree with the list, bounded by the goal-fix
                    # budget; exhaustion is a capability failure (escalate/block via
                    # the handler below), NEVER a PR with unaddressed requirements —
                    # unlike the local gate, this one does not fail open: a `declined`
                    # with a reason is always available, so "can't" has a valid exit.
                    if ledger and not _all_items_disposed(ledger):
                        open_items = [i for i in ledger if str(i.get("status", "")).lower() not in ("done", "declined")]
                        # #284: instrument the gate. An unresolved-disposition bounce is
                        # only diagnosable if we can tell a PARSE miss (the coder wrote a
                        # `## Requirements` section the loop failed to read) from genuine
                        # SILENCE. Record — at the gate, on the merged ledger the write-back
                        # above produced — the parsed dispositions, the still-open ids,
                        # len(result), whether the reply carried a `## Requirements` heading,
                        # and the first 200 chars after it.
                        diag = _requirement_gate_diagnostics(result or "", open_items)
                        log.info(
                            "[project_board] %s requirement gate diagnostics: %s",
                            fid,
                            _requirement_gate_diag_line(diag),
                        )
                        listing = "\n".join(f"- {i.get('id')}: {i.get('text')}" for i in open_items)
                        # #382: SILENCE (no `## Requirements` heading at all) is a protocol
                        # miss, not a capability failure — and the two must not share a
                        # remedy. The diagnostic above has always been able to tell them
                        # apart; nothing acted on it, so a coder that simply forgot the
                        # section got the full ladder: two `req-fix` re-dispatches, a tier
                        # escalation, two more, then a terminal block that reaps the
                        # worktree. bd-neiz burned seven ACP sessions and ~40 minutes that
                        # way and lost an implementation which had ALREADY passed its
                        # acceptance tests, because escalating the model cannot fix a
                        # missing markdown heading — the same "a timeout teaches nothing"
                        # shape as #143/#378: attempt N+1 gets a byte-identical instruction.
                        #
                        # So ask for the ledger ALONE first: implementation untouched on
                        # disk, same tier, `req-fix` unspent. On exhaustion this falls
                        # through to the ordinary bounce below — no new terminal edge.
                        if not diag["has_requirements_heading"]:
                            m = await self._budget_get(store, fid, "ledger-only", feature)
                            if m < _LEDGER_ONLY_MAX:
                                await self._budget_set(store, fid, "ledger-only", m + 1)
                                self._ci_prior_diff.pop(fid, None)  # impl is on disk; don't echo it back
                                self._ci_feedback[fid] = (
                                    "Your implementation is COMPLETE and staying as it is — do NOT edit "
                                    "code, tests, or docs this round. The ONLY thing missing is the "
                                    "disposition ledger, which your last reply left out entirely.\n\n"
                                    "Reply with TWO sections and nothing else:\n\n"
                                    "1. `## Requirements` — ONE line per item below, each exactly "
                                    "`- <id>: done` or `- <id>: declined — <concrete reason>`. Every "
                                    "item needs a line; silence is not a disposition. Mark an item "
                                    "`done` if the work already in the worktree satisfies it.\n"
                                    "2. `## Summary` — RE-EMIT your previous summary of this work, "
                                    "including any `NO_TEST_NEEDED: <reason>` line it carried, "
                                    "verbatim. The PR body is built from this section and the pre-PR "
                                    "gates re-read it, so dropping it loses work you already did.\n\n" + listing
                                )
                                try:
                                    await asyncio.to_thread(
                                        store.comment,
                                        fid,
                                        f"requirement ledger missing — ledger-only follow-up "
                                        f"{m + 1}/{_LEDGER_ONLY_MAX} (no tier escalation, req-fix "
                                        f"unspent) — diagnostics: {_requirement_gate_diag_line(diag)}",
                                    )
                                except Exception:  # noqa: BLE001 — bookkeeping must not fail the build
                                    log.warning(
                                        "[project_board] %s ledger-only follow-up comment failed",
                                        fid,
                                        exc_info=True,
                                    )
                                log.info(
                                    "[project_board] %s requirement ledger absent — ledger-only "
                                    "follow-up %d/%d (tier=%s, keep worktree, req-fix unspent)",
                                    fid,
                                    m + 1,
                                    _LEDGER_ONLY_MAX,
                                    tier or "default",
                                )
                                keep_wt = True
                                continue
                        n = await self._budget_get(store, fid, "req-fix", feature)
                        if n < self.goal_fix_max:
                            await self._budget_set(store, fid, "req-fix", n + 1)
                            self._ci_prior_diff.pop(fid, None)  # impl is on disk; don't echo it back
                            self._ci_feedback[fid] = (
                                "Your implementation is ALREADY in this worktree's files, but these "
                                "requirement items are still OPEN on the ledger — no disposition was "
                                "recorded for them. Address each one, then END your reply with a "
                                "`## Requirements` section marking EVERY item `done` or `declined — "
                                "<concrete reason>` (silence is not a disposition):\n\n" + listing
                            )
                            # #284: persist the SAME diagnostic payload on the bead so the
                            # NEXT occurrence is diagnosable from the card, not only the log
                            # — the log line is process-local, the comment survives a restart.
                            try:
                                await asyncio.to_thread(
                                    store.comment,
                                    fid,
                                    f"requirement gate bounce (re-dispatch {n + 1}/{self.goal_fix_max}) — "
                                    f"diagnostics: {_requirement_gate_diag_line(diag)}",
                                )
                            except Exception:  # noqa: BLE001 — bookkeeping must not fail the build
                                log.warning(
                                    "[project_board] %s requirement gate diagnostic comment failed",
                                    fid,
                                    exc_info=True,
                                )
                            log.info(
                                "[project_board] %s requirement gate: %d open item(s) — re-dispatch %d/%d "
                                "(tier=%s, keep worktree)",
                                fid,
                                len(open_items),
                                n + 1,
                                self.goal_fix_max,
                                tier or "default",
                            )
                            keep_wt = True
                            continue
                        raise worktree.WorktreeError(
                            f"requirements unresolved: {len(open_items)} item(s) still open after "
                            f"{n} fix round(s): " + ", ".join(str(i.get("id")) for i in open_items)
                        )
                    # Source-issue closed guard (#166): re-check the issue before
                    # opening a PR. A closed source issue means another PR already
                    # resolved the ticket — opening a duplicate wastes reviewer/CI
                    # cycles. Fail-open: any gh error lets the PR proceed normally.
                    # #253: but on a MULTI-SLICE board several features can share one
                    # source_issue — when a sibling slice merges first its `Fixes #N`
                    # closes the shared issue, and this guard must NOT then cancel the
                    # remaining siblings. Only cancel when the closure came from OUTSIDE
                    # the board's own feature set (no done sibling with a PR for it).
                    si_raw = str(feature.get("source_issue") or "").strip()
                    if (
                        si_raw
                        and not await _loop._source_issue_still_open(si_raw, wt)
                        and not await asyncio.to_thread(_issue_closed_by_board_sibling, store, feature)
                    ):
                        reason = f"source issue {si_raw} already closed — work superseded"
                        log.info("[project_board] %s skipping PR — %s", fid, reason)
                        try:
                            await asyncio.to_thread(store.cancel_feature, fid, reason)
                        except Exception:  # noqa: BLE001
                            log.warning(
                                "[project_board] %s cancel_feature failed — flagging blocked instead",
                                fid,
                                exc_info=True,
                            )
                            await asyncio.to_thread(store.flag_blocked, fid, reason)
                        await worktree.remove_worktree(repo, wt, branch or "")
                        self._inflight.pop(fid, None)
                        return
                    # Operator cancel (#211): re-read the card right before the PR edge.
                    # A card cancelled while the coder was finishing must NOT get a PR —
                    # the work is the operator's to salvage, not the loop's to publish
                    # (the old path opened it, then open_review refused the cancelled
                    # card and left an orphaned red PR nobody owned).
                    if await asyncio.to_thread(self._cancelled, store, fid):
                        await self._end_cancelled_drive(store, fid, repo, wt, branch)
                        return
                    body = await self._with_source_issue_ref(feature, wt, _pr_body(result, feature))
                    # #207: un-draft an adopted PR only on the card's FIRST adoption (no
                    # pr_url yet → the draft is the coder's). A re-dispatch of a card that
                    # already owns its PR (CI-fail bounce) leaves a draft alone: that is
                    # the operator's hold on the loop's own PR, not the coder's.
                    pr_url = await worktree.open_pr(
                        wt, branch, base=base, title=title, body=body, promote_draft=not feature.get("pr_url")
                    )
                    # …and again after: a cancel that lands during the push/create
                    # closes the PR it just opened rather than handing it to open_review.
                    if await asyncio.to_thread(self._cancelled, store, fid):
                        await self._end_cancelled_drive(store, fid, repo, wt, branch, pr_url=pr_url)
                        return
                except (worktree.NoChangesError, worktree.WorktreeError) as exc:
                    if self._shutting_down:
                        log.info("[project_board] %s dispatch aborted by shutdown — no escalation", fid)
                        self._inflight.pop(fid, None)
                        return
                    if await asyncio.to_thread(self._cancelled, store, fid):
                        # The cancel verb reaped the worktree under the coder (#175) and the
                        # dispatch/commit failed on it — that is the cancel, not a blockable
                        # failure on a closed card.
                        log.info("[project_board] %s dispatch ended by operator cancel: %s", fid, exc)
                        await self._end_cancelled_drive(store, fid, repo, wt, branch)
                        return
                    policy = classify(str(exc))
                    # Empty result (#198, retry policy #2991): a dispatch that COMPLETED
                    # with no worktree diff AND no tool-call activity is its own failure
                    # class — the coder connected but never executed. That is often a
                    # transient ACP hiccup, so the first occurrence at a tier retries
                    # ONCE on the SAME tier with the same prompt BEFORE any failure is
                    # counted (pre-escalation: the ladder is not consulted, no escalation
                    # attempt spent). Only when the retry ALSO comes back empty is the
                    # failure recorded — with the ACP stop-reason the monitor tap stashed
                    # (the WHY the retro and drawer show) — and the normal escalation
                    # ladder proceeds (single coder / ladder top → blocked, reason naming
                    # the class + evidence).
                    if isinstance(exc, worktree.NoChangesError):
                        had_tools, stop = self._empty_result_signals(fid)
                        if not had_tools:
                            n = await self._budget_get(store, fid, "empty-result", feature) + 1
                            await self._budget_set(store, fid, "empty-result", n)
                            evidence = (
                                f"empty coder reply — no diff, no tool calls (stop_reason={stop or 'none reported'})"
                            )
                            if n < self.empty_result_max:
                                # The same_tier_retry marker on the attempt comment is how
                                # the retro tells this retry from an escalation: the tier
                                # is unchanged and no failure is counted yet.
                                try:
                                    await asyncio.to_thread(
                                        store.record_attempt,
                                        fid,
                                        tier=tier or await asyncio.to_thread(store.current_tier, fid),
                                        outcome=f"empty_result: {evidence} — same_tier_retry (pre-escalation)",
                                    )
                                except Exception:  # noqa: BLE001 — attempt bookkeeping must never mask the verdict
                                    log.warning(
                                        "[project_board] %s empty_result attempt record failed", fid, exc_info=True
                                    )
                                log.info(
                                    "[project_board] %s Retrying on same tier (empty reply) %d/%d — "
                                    "no failure counted: %s",
                                    fid,
                                    n,
                                    self.empty_result_max,
                                    evidence,
                                )
                                continue
                            # The same-tier retry also returned empty — NOW it is a coder
                            # failure: record it, then let the normal ladder climb.
                            reason = f"empty_result: {evidence} — {n} occurrence(s), same-tier retry exhausted"
                            try:
                                await asyncio.to_thread(
                                    store.record_attempt,
                                    fid,
                                    tier=tier or await asyncio.to_thread(store.current_tier, fid),
                                    outcome=reason,
                                )
                            except Exception:  # noqa: BLE001 — attempt bookkeeping must never mask the verdict
                                log.warning("[project_board] %s empty_result attempt record failed", fid, exc_info=True)
                            await self._budget_reset(store, fid, "empty-result")  # a climb gets its own retry window
                            if self.escalation_on:
                                nxt = await asyncio.to_thread(store.escalate, fid, reason[:200])
                                if nxt:
                                    log.info(
                                        "[project_board] %s escalating %s→%s (empty-reply retry exhausted): %s",
                                        fid,
                                        tier,
                                        nxt,
                                        evidence,
                                    )
                                    tier = nxt
                                    retries = 0
                                    tried_here = 0  # a NEW rung has its own providers (#362)
                                    # Fresh per-tier budgets on the climb — mirrors the
                                    # shared capability-escalation path below.
                                    await self._budget_reset(
                                        store, fid, "goal-fix", "gate-fix", "req-fix", "ledger-only"
                                    )
                                    continue
                            log.warning("[project_board] %s blocked (%s)", fid, reason)
                            await asyncio.to_thread(store.flag_blocked, fid, reason)
                            if wt:
                                await worktree.remove_worktree(repo, wt, branch or "")
                            self._inflight.pop(fid, None)
                            return
                    # A capability failure = the coder didn't deliver (no diff / dispatch
                    # error / timed out). Those are NOT transient-retried (re-running the
                    # same coder won't help) — they escalate a tier or block. Only true
                    # infra failures (push/fetch/gh network/rate-limit) get the backoff.
                    # A dispatch failure is a capability failure ONLY when the classifier
                    # does not recognize it as transient. A rate limit / session limit /
                    # network blip inside "coder dispatch failed: …" is the PROVIDER
                    # refusing the call, not the coder failing the task — escalating on
                    # it burned the whole tier ladder in ten seconds (three attempts,
                    # three tiers, a block; 2026-08-28, bd-cwpv.12/.16) and left `tier:`
                    # labels that misrouted the card when it was requeued after the reset.
                    # #378: count timeouts durably, BEFORE the retry/escalate/block fork —
                    # bd-sxxf timed out, escalated a tier, timed out again and only then
                    # blocked, so a counter bumped at the block alone would have read 1.
                    if isinstance(exc, worktree.CoderTimeout):
                        timeouts = await self._budget_get(store, fid, "timeout") + 1
                        await self._budget_set(store, fid, "timeout", timeouts)
                    dispatch_failed = str(exc).startswith("coder dispatch failed") and not policy.retryable
                    capability = (
                        isinstance(exc, (worktree.NoChangesError, worktree.CoderTimeout, coder_seam.SolveExhausted))
                        or dispatch_failed
                        or str(exc).startswith("goal verification failed")
                        or str(exc).startswith("requirements unresolved")
                    )
                    # A capability failure that happened BEFORE the model could influence
                    # the result (a seam/adapter refusal, a missing delegate, a
                    # non-TappedResult reply, or a timeout before the first token) is NOT a
                    # model-capability ceiling — a stronger model can't clear it. The ladder
                    # used to escalate on it anyway, burning smart→reasoning→opus in seconds
                    # with no model work and leaving a `tier:opus` label that misrouted the
                    # card's next real build (bd-cwpv). Decide it from the classifier's seam
                    # signature AND the dispatch-lifecycle evidence (did any tool/thought/
                    # answer/token reach the ring buffer): a recognised seam failure with no
                    # model activity is pre-model. Fail-safe — an unreadable snapshot reads
                    # as "model not reached", so an ambiguous dispatch failure blocks for
                    # triage rather than climbing an expensive ladder (#339).
                    pre_model = capability and is_pre_model_dispatch_failure(
                        str(exc), model_reached=self._dispatch_reached_model(fid)
                    )
                    # 1. Transient infra → back off and retry the SAME tier (a re-dispatch
                    #    off the latest base also clears a merge conflict).
                    # 0.5 QUOTA failure with an untried sibling at this rung → switch
                    #     provider IMMEDIATELY (#362). Sleeping 60s to re-dispatch the same
                    #     exhausted provider, five times, then blocking the card, is what
                    #     this replaces — a rate limit says nothing about the model's
                    #     ability, only about its quota, so it must not spend the retry
                    #     budget or the capability ladder. Only when every sibling is
                    #     exhausted does the ordinary backoff below apply.
                    if should_rotate_provider(policy.category, siblings, tried_here):
                        tried_here += 1
                        sib += 1
                        log.info(
                            "[project_board] %s %s on %s — switching to %s at the same rung "
                            "(sibling %d/%d, no backoff, no tier climb): %s",
                            fid,
                            policy.category,
                            coder_name,
                            siblings[sib % len(siblings)],
                            tried_here + 1,
                            len(siblings),
                            str(exc)[:120],
                        )
                        continue
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
                    # 1.5 Pre-model dispatch/infra failure → block DIRECTLY for triage, no
                    #     tier climb. This is not an escalation: `store.escalate` is never
                    #     called, so no `tier:`/`attempt:` label is added and no ladder
                    #     budget is spent. It blocks under the `dispatch-infra` class the
                    #     blocked sweep never auto-heals, so the operator is notified with the
                    #     original infra evidence — and an operator unblock resets the tier
                    #     posture (store.clear_blocked) so the next genuine build starts at
                    #     its difficulty-selected tier (#339).
                    if pre_model:
                        reason = f"pre-model dispatch failure — infra triage, no tier climb: {exc}"
                        log.warning(
                            "[project_board] %s blocked (pre-model dispatch — no model work, tier untouched): %s",
                            fid,
                            exc,
                        )
                        await asyncio.to_thread(store.flag_blocked, fid, reason, category=PRE_MODEL_DISPATCH_CLASS)
                        if wt:
                            await worktree.remove_worktree(repo, wt, branch or "")
                        self._inflight.pop(fid, None)
                        return
                    # 2. Capability failure + a ladder → climb a model tier (fresh budget).
                    if self.escalation_on and capability:
                        nxt = await asyncio.to_thread(store.escalate, fid, str(exc)[:200])
                        if nxt:
                            log.info("[project_board] %s escalating %s→%s: %s", fid, tier, nxt, exc)
                            # Keep the worktree ONLY across the escalation classes whose repair
                            # rounds explicitly retained it (#282): a goal-verify / requirement-
                            # ledger gate exhausts its keep-worktree fix budget with the VERIFIED
                            # impl still on disk and _ci_feedback seeded to "your work is already
                            # in this worktree". Carry that worktree into the escalated dispatch so
                            # the stronger tier sees the work and the feedback stays truthful.
                            # Anything else is a from-scratch build: the seeded feedback would lie
                            # about a worktree that no longer exists, so clear it (+ the prior diff)
                            # to keep prompt and worktree consistent.
                            keep_wt_class = str(exc).startswith(("goal verification failed", "requirements unresolved"))
                            if keep_wt_class and wt is not None:
                                keep_wt = True  # reuse the verified worktree; _ci_feedback is truthful
                            elif isinstance(exc, worktree.CoderTimeout):
                                # A timeout carries NO diff and NO CI output, so the stronger tier
                                # would otherwise get a BYTE-IDENTICAL prompt — blind to the fact a
                                # prior attempt ran out of time and what it was doing when killed
                                # (#146). Seed the CI/review-bounce feedback lever with the ring
                                # buffer's timeout context so the escalated dispatch leads with it.
                                self._ci_feedback[fid] = self._timeout_escalation_context(fid)
                                self._ci_prior_diff.pop(fid, None)  # a timeout produced no diff to echo back
                            else:
                                # Fresh worktree ahead — drop any gate-fix feedback describing the
                                # OLD worktree's files so the new build isn't told its work is on
                                # disk when it isn't.
                                self._ci_feedback.pop(fid, None)
                                self._ci_prior_diff.pop(fid, None)
                            tier = nxt
                            retries = 0
                            tried_here = 0  # a NEW rung has its own providers (#362)
                            # Fresh goal-fix / local-gate / ledger budgets at the new tier —
                            # otherwise a tier that exhausted its retries hands the next
                            # (stronger) tier a spent budget, so it blocks on its first gap
                            # without a real shot.
                            await self._budget_reset(store, fid, "goal-fix", "gate-fix", "req-fix", "ledger-only")
                            continue
                    # 3. Terminal, or retries/ladder exhausted → Blocked.
                    log.warning("[project_board] %s blocked (%s): %s", fid, policy.category, exc)
                    await asyncio.to_thread(store.flag_blocked, fid, f"{policy.category}: {exc}")
                    # #378: a card that has now timed out repeatedly is not model-limited, it
                    # is too wide — and nothing in the block path says so, which is how one
                    # sat parked while an operator guessed. Ask the board's own agent to split
                    # it (once; `request_decomposition` no-ops on a repeat or on a task).
                    if isinstance(exc, worktree.CoderTimeout) and self.decompose_after_timeouts:
                        spent = await self._budget_get(store, fid, "timeout")
                        ask = getattr(store, "request_decomposition", None)  # older/stub store: skip
                        if spent >= self.decompose_after_timeouts and callable(ask):
                            asked = await asyncio.to_thread(ask, fid, timeouts=spent)
                            if asked:
                                log.warning(
                                    "[project_board] %s timed out %dx — filed %s to decompose it "
                                    "(a timeout is a SIZE signal, not a capability one)",
                                    fid,
                                    spent,
                                    asked.get("id", "?"),
                                )
                    if wt:
                        await worktree.remove_worktree(repo, wt, branch or "")
                    self._inflight.pop(fid, None)
                    return
                # Built + PR opened. The fleet PR-review pipeline reviews it on open;
                # only dispatch an explicit review when configured to (review_dispatch).
                log.info("[project_board] %s coder done (%d chars) → %s", fid, len(result or ""), pr_url)
                await asyncio.to_thread(store.open_review, fid, pr_url=pr_url)
                # Gate passed — reset the pre-PR budgets (goal-fix, local-gate, the
                # requirement ledger #113, and the empty-result count #198).
                await self._budget_reset(store, fid, "goal-fix", "gate-fix", "req-fix", "empty-result", "ledger-only")
                if self.review_gate:
                    # Blocking adversarial review (M5). May requeue the feature with
                    # findings injected — the next drive carries them in the prompt.
                    await self._review_gate(store, fid, pr_url, repo)
                elif self.review_dispatch:
                    await self._request_review(fid, pr_url)
                # Keep the worktree (a CI-fail bounce re-dispatches); reaping happens
                # on a terminal block above, and the coder subprocess is already reaped.
                self._inflight.pop(fid, None)  # built OK — not an interrupted build to reap
                return
        except asyncio.CancelledError:
            if self._shutting_down or self._stop.is_set():
                raise  # shutdown owns the reap (stop() sweeps _inflight)
            # An operator cancel stopped this drive mid-flight (#211): the coder
            # subprocess is already reaped by dispatch_coder's finally; close any PR
            # it opened, reap the worktree, leave the trail on the card — and end
            # quietly (the task completes, never a CancelledError in the loop log).
            log.info("[project_board] %s drive stopped by operator cancel", fid)
            await self._end_cancelled_drive(store, fid, repo, wt, branch, pr_url=pr_url)
        except BoardError as exc:
            if await asyncio.to_thread(self._cancelled, store, fid):
                # The card closed under us (open_review refused a cancelled card): a
                # cancel edge, not a block — and the PR it would have handed over is ours
                # to close.
                log.info("[project_board] %s cancelled under the drive: %s", fid, exc)
                await self._end_cancelled_drive(store, fid, repo, wt, branch, pr_url=pr_url)
                return
            log.warning("[project_board] %s blocked (board): %s", fid, exc)
            await asyncio.to_thread(store.flag_blocked, fid, str(exc))
            self._inflight.pop(fid, None)
        except Exception as exc:  # noqa: BLE001 — unexpected; block, don't crash the loop
            log.exception("[project_board] %s unexpected failure", fid)
            await asyncio.to_thread(store.flag_blocked, fid, f"unexpected: {type(exc).__name__}: {exc}")
            if wt:
                await worktree.remove_worktree(repo, wt, branch or "")
            self._inflight.pop(fid, None)

    # ── operator cancel during a drive (#211) ────────────────────────────────
    @staticmethod
    def _cancelled(store, fid: str) -> bool:
        """Is the card ``cancelled`` NOW? A fresh store read at the PR seam — fail OPEN
        (a read failure is not a cancel): the drive then proceeds exactly as before."""
        try:
            f = store.get_feature(fid)
        except Exception:  # noqa: BLE001 — BoardError, a fake store without the read
            return False
        return bool(f) and str(f.get("board_state") or "") == "cancelled"

    async def _end_cancelled_drive(self, store, fid: str, repo: str, wt, branch, *, pr_url: str | None = None) -> None:
        """Finish a drive whose card was cancelled by the operator: close the PR if one
        opened (with a comment pointing at the card), reap the worktree (#1 lifecycle
        rule), comment the trail on the card, release the slot. No flag_blocked, no
        escalation, nothing raised — a cancel is a terminal edge, not a failure.

        Runs ONCE per drive and to completion: a second cancel verb landing while this
        is awaiting gh/git cancels the drive task again, which would re-enter via the
        CancelledError handler — the ``_cancel_done`` mark makes that re-entry a no-op,
        and the body runs shielded so the repeat cancel can't abandon the reap or the
        trail halfway (a shutdown cancel is still honoured: ``stop()`` owns that reap)."""
        if fid in self._cancel_done:
            return
        self._cancel_done.add(fid)
        inner = asyncio.ensure_future(self._finish_cancelled_drive(store, fid, repo, wt, branch, pr_url=pr_url))
        while True:
            try:
                await asyncio.shield(inner)
                return
            except asyncio.CancelledError:
                if inner.done() or self._shutting_down or self._stop.is_set():
                    inner.cancel()
                    raise
                log.info("[project_board] %s repeat cancel during cancel cleanup — finishing it", fid)

    async def _finish_cancelled_drive(self, store, fid: str, repo: str, wt, branch, *, pr_url: str | None) -> None:
        if not pr_url and branch:
            # The cancel may have landed INSIDE `git push` / `gh pr create` (the task
            # cancel kills the child, but GitHub may already have the PR) — the drive
            # never got a URL back, so look the branch up before declaring "no PR".
            try:
                pr_url = await worktree.pr_url_for_branch(branch, cwd=repo) or None
            except Exception:  # noqa: BLE001 — a gh failure here is not a cancel failure
                log.debug("[project_board] %s cancel: pr lookup for %s failed", fid, branch, exc_info=True)
        note = f"cancelled by operator — drive ended without a PR (branch {branch or '?'})"
        if pr_url:
            ok, detail = await worktree.close_pr(pr_url, comment=cancel_pr_comment(fid), cwd=repo)
            if ok and detail in (worktree.PR_ALREADY_MERGED, worktree.PR_ALREADY_CLOSED):
                note = f"cancelled by operator — {pr_url} {detail}, nothing to close"
                log.info("[project_board] %s cancelled — %s %s", fid, pr_url, detail)
            elif ok:
                note = f"cancelled by operator — closed {pr_url}"
                log.info("[project_board] %s cancelled — closed %s", fid, pr_url)
            else:
                note = f"cancelled by operator — could not close {pr_url} ({detail[:200]}); close it by hand"
                log.warning("[project_board] %s cancelled — could not close %s: %s", fid, pr_url, detail[:300])
        if wt:
            await worktree.remove_worktree(repo, wt, branch or "")
            note += "; worktree reaped"
        try:
            await asyncio.to_thread(store.comment, fid, note)
        except Exception:  # noqa: BLE001 — the trail is best-effort
            log.debug("[project_board] %s cancel comment failed", fid, exc_info=True)
        self._inflight.pop(fid, None)
        log.info("[project_board] %s %s", fid, note)

    async def _with_source_issue_ref(self, feature: dict, wt: str, body: str) -> str:
        """Stamp the feature's source issue onto the PR body — ``Fixes #n`` when the
        issue is in the PR's own target repo, ``Refs <url>`` cross-repo. No source
        issue ⇒ body unchanged (and no ``gh`` round-trip). ``worktree.repo_slug`` fails
        open, so an unresolvable target repo degrades to a ``Refs`` link — never a
        wrong ``Fixes`` and never a blocked PR."""
        parsed = _source_issue(feature)
        if not parsed:
            return body
        issue_slug, n = parsed
        target_repo = await worktree.repo_slug(cwd=wt)
        return _inject_source_issue_line(body, issue_slug, n, target_repo)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _use_coder_solve(self, feature: dict) -> bool:
        """The P2 board-seam dispatch decision (ADR 0064) — see coder_seam.py.
        `coder_solve` is this repo's own opt-out valve (default on); the actual
        grounding gate (coder plugin importable + acceptance criteria + a runnable
        test command) lives in ``coder_seam.should_use_solve``.

        Max-Mode takes precedence when both are configured (`max_mode_n>1`): a
        board already relying on Max-Mode's judge-fallback (always ships a
        best-effort PR) must not have that silently swapped for solve()'s harder
        "block if nothing passes" behavior just because the separate `coder`
        plugin became importable. Enabling coder.solve on such a board is a
        deliberate config change (set `max_mode_n<=1`), never a side effect of
        installing `coder` for something else."""
        if not self.coder_solve:
            return False
        if self.max_mode_n > 1:
            return False
        return coder_seam.should_use_solve(feature, test_cmd=self._coder_solve_test_cmd_for(feature))

    def _resolve_delegate(self, name: str, expect_type: str):
        """Look up a live delegate by name from the delegates registry. Returns the
        Delegate or None (not configured / wrong type / plugin disabled). Thin
        wrapper — the real lookup is shared with api.py's test-rung route via
        ``coder_seam.resolve_delegate``."""
        return coder_seam.resolve_delegate(name, expect_type)

    def _resolve_task_delegate(self, assignee: str):
        """Resolve a task assignee to its dispatchable SISTER-AGENT delegate — ACP
        first, then A2A (#304). Review dispatch already reaches an ``a2a`` agent, but a
        task assignee was resolved as ``acp`` only, so a named A2A sister agent fell
        through to the human/unassigned park. Returns the Delegate (of either type) or
        None (empty/human assignee, or a name that resolves to NEITHER type)."""
        if not assignee:
            return None
        return self._resolve_delegate(assignee, "acp") or self._resolve_delegate(assignee, "a2a")

    async def _run_fixups(self, wt: str, feature: dict | None = None) -> None:
        """Run the repo's auto-fix command (``format_cmd``, e.g.
        ``ruff check --fix . && ruff format .``) in the worktree before opening the PR.
        The coder is edit-only — it can't run the linter/formatter, so trivial lint/format
        nits would otherwise fail CI and burn a bounce/escalation. Best-effort: no command
        configured, or any error/timeout, just proceeds (CI is still the real lint gate).
        Resolves ``format_cmd`` from the feature's project when given (#90)."""
        cmd = self._format_cmd_for(feature) if feature is not None else self.format_cmd
        if not cmd:
            return
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=wt,
                env=self._child_env(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=180)
        except Exception as exc:  # noqa: BLE001 — best-effort; CI still gates lint
            log.info("[project_board] fixups command failed (proceeding — CI still gates): %s", exc)

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
        ``NO_TEST_NEEDED: <reason>`` at line start inside its final ``## Summary`` section
        (#264 — structural via ``_no_test_marker``, not a substring scan: a mid-narration
        mention is not a declaration); we log the reason and pass, rather than burning
        retries on a test that doesn't apply. Returns a gap string (→ re-dispatch/
        escalate) or None. Fails OPEN on any error (never blocks a good PR on infra)."""
        ac = (feature.get("acceptance_criteria") or "").strip()
        if not ac:
            return None
        try:
            await worktree.stage_all(wt)
            _rc, names, _err = await worktree._git(wt, "diff", "--cached", "--name-only", f"origin/{base}")
        except Exception:  # noqa: BLE001 — best-effort
            return None
        changed = [n for n in (names or "").split() if n]
        if not changed:
            return None  # an empty diff is open_pr's NoChangesError job, not ours
        code = [n for n in changed if _is_code_path(n) and not _is_test_path(n)]
        if code and not any(_is_test_path(n) for n in changed):
            reason = _no_test_marker(coder_reply or "")
            if reason is not None:
                log.info(
                    "[project_board] %s no-test accepted (coder declared): %s",
                    feature.get("id"),
                    reason[:200],
                )
                return None
            head = ", ".join(code[:6]) + ("…" if len(code) > 6 else "")
            return (
                "no test was added/updated for the code change — add a test covering the new "
                f"behavior, or declare `NO_TEST_NEEDED: <reason>` on its own line in your final "
                f"`## Summary` section if a test genuinely doesn't apply (refactor/config/docs) "
                f"(code: {head})"
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
                await worktree.stage_all(wt)
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

    async def _candidate_diff_indices(self, base: str, worktrees: list[str]) -> list[int]:
        """Indices of candidates that produced a non-empty staged diff vs ``origin/<base>``.
        Cheap name-only check; best-effort (a candidate we can't stage/diff is skipped)."""
        out: list[int] = []
        for i, wt in enumerate(worktrees):
            try:
                await worktree.stage_all(wt)
                _rc, names, _err = await worktree._git(wt, "diff", "--cached", "--name-only", f"origin/{base}")
            except Exception:  # noqa: BLE001 — best-effort, like _judge_candidates
                names = ""
            if (names or "").strip():
                out.append(i)
        return out

    async def _select_candidate(self, feature: dict, base: str, worktrees: list[str]) -> int | None:
        """Pick the winning Max-Mode candidate — EXECUTION-GROUNDED (ADR 0064).

        When a pre-PR gate (``local_gate_cmd``) is configured, PREFER candidates whose
        gate actually PASSES: run the candidates, don't just judge their diffs. An LLM
        judge of code rewards plausible-looking diffs and can't catch subtle wrongness —
        only running the tests discriminates. The judge (``_judge_candidates``) then only
        breaks ties among the PASSING set (quality among the correct), or decides when no
        gate is configured / none pass. With no gate this is exactly the old behavior.

        Returns the winning index, or ``None`` when no candidate produced a diff."""
        # No oracle → judge exactly as before (it does its own emptiness handling and
        # returns None when every candidate is empty). Avoids a redundant diff pass.
        if not self._local_gate_cmd_for(feature):
            return await self._judge_candidates(feature, base, worktrees)

        nonempty = await self._candidate_diff_indices(base, worktrees)
        if not nonempty:
            return None
        if len(nonempty) == 1:
            return nonempty[0]

        fid = feature.get("id")
        gates = await asyncio.gather(*(self._run_local_gate(worktrees[i], feature) for i in nonempty))
        passing = [i for i, gap in zip(nonempty, gates) if gap is None]
        if not passing:
            log.info(
                "[project_board] %s execution-select: 0/%d candidates pass the gate — judging diffs", fid, len(nonempty)
            )
            return await self._judge_candidates(feature, base, worktrees)
        log.info(
            "[project_board] %s execution-select: %d/%d candidates pass the gate", fid, len(passing), len(nonempty)
        )
        if len(passing) == 1:
            return passing[0]
        # Tie-break among the PASSING (correct) candidates by quality, via the judge.
        j = await self._judge_candidates(feature, base, [worktrees[i] for i in passing])
        return passing[j] if j is not None else passing[0]

    async def _dispatch_max_mode(
        self, feature: dict, coder, prompt: str, repo: str, base: str, fid: str, tier: str = ""
    ) -> tuple[str, str, str]:
        """Max-Mode (#21): build the feature N ways in parallel and ship the best diff.

        Creates ``max_mode_n`` throwaway candidate worktrees off the same base (suffixed
        ``feat-<id>.c<k>`` so none collides with the canonical name), dispatches the coder
        into ALL of them concurrently — each keeps its own ``coder_timeout`` watchdog +
        ``finally`` subprocess teardown (``dispatch_coder``), and ``return_exceptions``
        means one candidate timing out / erroring leaves an empty tree the selector skips
        rather than sinking the batch. ``_select_candidate`` picks the winning index —
        EXECUTION-GROUNDED when a pre-PR gate is configured (prefer candidates whose tests
        pass; ADR 0064), else the best-of-N LLM judge; the winner is PROMOTED into the canonical
        ``feat-<id>`` worktree / ``feat/<id>`` branch (so the rest of the lifecycle is
        unchanged) and the losers are reaped. All-empty → ``NoChangesError``, which
        ``_drive`` escalates/blocks exactly like a single coder that produced nothing.

        Returns (canonical_wt, canonical_branch, winner_reply). The fan-out is bounded by
        ``max_concurrent`` × ``max_mode_n`` coders; size those to the host."""
        n = self.max_mode_n
        cand_ids = [f"{fid}.c{i}" for i in range(n)]
        # Create the N worktrees sequentially (git serializes worktree-list writes); the
        # slow part — the coder dispatch — is what we then fan out in parallel.
        cands: list[tuple[str, str]] = []
        for cid in cand_ids:
            cands.append(await worktree.create_worktree(repo, base, cid, self.root))
        log.info("[project_board] %s max-mode: dispatching %d parallel candidates", fid, n)
        # Tap each candidate into the live monitor as its own gen (#84) — the drawer
        # shows all N building in parallel; a tap that can't wire degrades per-candidate.
        results = await asyncio.gather(
            *(
                coder_seam.dispatch_coder_tapped(
                    coder, wt, prompt, fid=fid, gen=i + 1, tier=tier, timeout=self.coder_timeout or None
                )
                for i, (wt, _b) in enumerate(cands)
            ),
            return_exceptions=True,
        )
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                log.info("[project_board] %s max-mode candidate %d failed (skipped): %s", fid, i, r)
        idx = await self._select_candidate(feature, base, [wt for wt, _b in cands])
        if idx is None:
            for cid in cand_ids:
                await worktree.reap_feature_worktree(repo, self.root, cid)
            raise worktree.NoChangesError(f"max-mode: all {n} candidates produced no diff")
        log.info("[project_board] %s max-mode: candidate %d/%d wins → promoting", fid, idx, n)
        win_wt, win_branch = cands[idx]
        canon_wt, canon_branch = await worktree.promote_worktree(
            repo, win_wt, win_branch, fid, self.root, title=feature.get("title") or ""
        )
        # Reap the losers (the winner was moved out of its candidate name by promote).
        for i, cid in enumerate(cand_ids):
            if i != idx:
                await worktree.reap_feature_worktree(repo, self.root, cid)
        winner_reply = results[idx] if not isinstance(results[idx], Exception) else ""
        return canon_wt, canon_branch, winner_reply

    async def _fetch_kg_lessons(self, feature: dict) -> str:
        """Query the knowledge graph (via graph.sdk) for lessons relevant to THIS
        feature — the read half of the flywheel. Builds the query from the feature's
        title + files (the area it touches), pulls the top-``kg_lessons_k`` chunks from
        the ``kg_lessons_domain`` bucket, and returns them as a markdown bullet list for
        ``_build_prompt`` to inject. Best-effort: returns "" if disabled, no store, no
        SDK, or any error — a retrieval hiccup must never block a build."""
        if not self.kg_lessons:
            return ""
        query = " ".join(
            p
            for p in (
                feature.get("title", ""),
                " ".join(feature.get("files_to_modify") or []),
                feature.get("difficulty", ""),
            )
            if p
        ).strip()
        if not query:
            return ""
        try:
            from graph.sdk import knowledge_search

            hits = await knowledge_search(query, k=self.kg_lessons_k, domain=self.kg_lessons_domain or None)
        except Exception as exc:  # noqa: BLE001 — retrieval is best-effort; never block a build
            log.info("[project_board] kg-lessons fetch skipped (%s)", exc)
            return ""
        lines = []
        for h in hits or []:
            text = (h.get("preview") or h.get("content") or "").strip()
            if text:
                lines.append(f"- {text}")
        return "\n".join(lines)

    @staticmethod
    def _empty_result_signals(fid: str) -> tuple[bool, str]:
        """(had_tool_activity, stop_reason) for the dispatch that just failed with no
        diff, mined from the live-monitor ring buffer (#198). No tool events across
        any gen ⇒ the coder connected but never executed — the ``empty_result``
        class ``_drive`` retries once on the same tier before counting a failure
        (#2991). ``stop_reason`` is whatever
        signal the ACP adapter reported (``progress_stop_reason``), latest gen wins.
        Best-effort: an unreadable snapshot reports activity=True, so a monitor
        hiccup can never misclassify a real capability failure as empty."""
        try:
            gens = coder_seam.progress_snapshot(fid).get("gens") or []
        except Exception:  # noqa: BLE001 — a monitor read must never break the drive
            return True, ""
        if not gens:  # no record of the run at all (evicted) — can't call it empty
            return True, ""
        had_tools = any(g.get("recent_tools") or g.get("current_tool") for g in gens)
        stop = next((str(g["stop_reason"]) for g in reversed(gens) if g.get("stop_reason")), "")
        return had_tools, stop

    @staticmethod
    def _dispatch_reached_model(fid: str) -> bool:
        """Did the coder dispatch that just failed REACH the model — i.e. produce any
        first-token evidence (a tool call, a thought, streamed answer text, or token
        usage)? A dispatch that failed with NONE of these never got past the seam /
        adapter, so the model could not have influenced the result and a stronger model
        cannot clear it (it must block for infra triage, not climb the tier ladder).

        Delegates to ``coder_seam.dispatch_reached_model``, which scopes the check to
        the CURRENT dispatch's run epoch so a stale gen an earlier dispatch left in the
        feature's ring buffer can't misclassify a later pre-model failure as model-
        reachable (the review finding on the first cut). Fail-safe toward "no model
        work" so an ambiguous dispatch failure blocks rather than climbs (#339)."""
        return coder_seam.dispatch_reached_model(fid)

    def _timeout_escalation_context(self, fid: str) -> str:
        """Feedback for an escalated dispatch whose PRIOR attempt TIMED OUT (#146).

        A ``CoderTimeout`` climbs the tier ladder (``_drive``), but on its own the
        stronger model would get a BYTE-IDENTICAL prompt: zero signal that a prior
        attempt ran out of time, how long it ran, or what it was doing when killed.
        Mine the progress ring buffer (``coder_seam.progress_snapshot``) for the
        timed-out gen's elapsed time, the last tool in flight, and the thought tail,
        and lead the re-dispatch with them. Returned for injection into
        ``_ci_feedback`` so it rides the exact same prompt path a CI/review bounce
        uses — no new plumbing. Best-effort: a missing/empty snapshot still yields a
        usable "prior attempt timed out, produced no diff" note — a monitor read must
        never break escalation."""
        try:
            gens = coder_seam.progress_snapshot(fid).get("gens") or []
        except Exception:  # noqa: BLE001 — a monitor read must never break escalation
            gens = []
        gen = gens[-1] if gens else {}
        elapsed = gen.get("elapsed_s")
        ran_for = f"ran ~{elapsed}s and " if elapsed is not None else ""
        lines = [
            "A PREVIOUS attempt at this feature TIMED OUT and was killed — it "
            f"{ran_for}produced NO diff (nothing was committed). You are a stronger "
            "model taking over: be decisive, avoid open-ended exploration, and make "
            "the edits early rather than reading indefinitely.",
        ]
        cur = gen.get("current_tool") or {}
        if cur.get("name"):
            locs = ", ".join(cur.get("locations") or [])
            where = f" on {locs}" if locs else ""
            lines.append(
                f"- Last tool in flight when it was killed: {cur['name']} ({cur.get('status', 'running')}){where}."
            )
        else:
            recent = gen.get("recent_tools") or []
            if recent:
                last = recent[-1]
                lines.append(f"- Last observed tool: {last.get('name', 'tool')} ({last.get('status', '')}).")
        tail = (gen.get("thought_tail") or "").strip()
        if tail:
            lines.append(f"- Its last reasoning before the timeout (it never converged):\n{tail}")
        return "\n".join(lines)
