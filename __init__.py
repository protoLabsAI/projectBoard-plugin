"""project_board — lean 6-state coding board + ACP spawn loop.

Composition over construction: the board is a **projection over beads** (`br`) — no
separate store of its own (store.py shells the `br` CLI; beads owns the `.beads/*.db`
+ JSONL) — plus an HTTP API and the orchestration loop, which dispatches through the
already-built ``delegates`` plugin (ADR 0024/0025) — it does NOT reimplement the
spawn primitive.

Reach used (no core edits): ``register_router`` (the board API), ``register_surface``
(the background puller), ``register_tool`` (board ops the agent can drive headlessly).

Ships DISABLED. Enable with ``plugins: { enabled: [delegates, project_board] }`` and
declare ``proto`` (acp) + ``quinn`` (a2a) delegates. The Ready gate + single Done
edge live in store.py; the teardown + error paths live in loop.py/worktree.py.
"""

from __future__ import annotations

import json
import logging

from langchain_core.tools import tool

log = logging.getLogger("protoagent.plugins.project_board")


def register(registry) -> None:
    # Resolve `project:` against the host's ADR 0095 registry BEFORE anything reads
    # cfg — every consumer below (router, loop, tools) takes cfg from here, so this
    # is the single place the two layers meet. Deliberately OUTSIDE the try/except
    # blocks below: an unresolvable project name must abort registration rather than
    # let the board start building in cwd (see projects.py). The host records the
    # message on the plugin entry and skips us; boot is unaffected.
    from .projects import resolve_project_cfg

    cfg = resolve_project_cfg(registry.config or {})

    # Board HTTP API + console view, mounted as ONE router under the UNGATED prefix
    # /plugins/project_board (matching the sibling agent-browser/doom plugins, whose
    # browser-loaded views are also ungated). UNGATED because two of these routes are
    # browser navigations that can't attach a bearer: GET /board is loaded as an
    # iframe src, and POST /webhook/pr is hit by GitHub (which can't send a bearer and
    # whose public URL must not change). The board view's GET /board route is folded
    # into this same router so the declared view path (/plugins/project_board/board)
    # is genuinely served. We must NOT register a second router at the same prefix:
    # the host dedupes routers by (plugin_id, prefix), so a second one would be
    # silently dropped → the view 404s.
    # …and the operator CRUD/transition routes ride a SECOND router at the gated
    # /api/plugins/project_board prefix (rule 2) — distinct prefixes, so the host's
    # (plugin_id, prefix) de-dupe keeps both.
    try:
        from .api import build_data_router, build_router

        registry.register_router(build_router(cfg), prefix="/plugins/project_board")
        registry.register_router(build_data_router(cfg), prefix="/api/plugins/project_board")
    except Exception:  # noqa: BLE001 — API is best-effort
        log.exception("[project_board] mounting board API + view failed")

    # Background orchestration loop (off unless project_board.loop_enabled).
    try:
        from .loop import BoardLoop

        loop = BoardLoop(cfg)
        registry.register_surface(loop.start, stop=loop.stop, name="project-board-loop")
    except Exception:  # noqa: BLE001 — loop is best-effort; the API still serves
        log.exception("[project_board] registering loop surface failed")

    # A few board tools so the agent (or A2A) can drive the board headlessly.
    for t in _board_tools(cfg):
        registry.register_tool(t)

    # Planning layer (D9): the decompose/antagonist subagents + the orchestration
    # skill that turns an idea into the docs tree + the board (per-epic human gate).
    try:
        from .subagents import ANTAGONIST_CONFIG, DECOMPOSE_CONFIG

        registry.register_subagent(DECOMPOSE_CONFIG)
        registry.register_subagent(ANTAGONIST_CONFIG)
        registry.register_skill_dir("skills")
    except Exception:  # noqa: BLE001 — planning layer is best-effort
        log.exception("[project_board] registering planning subagents/skill failed")

    log.info(
        "[project_board] registered board API + loop + tools + planning (coder=%s reviewer=%s)",
        cfg.get("coder", "proto"),
        cfg.get("reviewer", "quinn"),
    )


# Board states where a feature is DONE and can't be a live duplicate — a new
# creation of the same title is legitimately re-doing closed work. Everything else
# (backlog/ready/in_progress/in_review/blocked) is "open" and a same-title create
# would stack (mirrors portfolio-plugin's _TERMINAL_LANES/#25 dedup precedent — this
# is the same class of bug one layer down: an agent's OWN reasoning calling
# board_create_feature twice for one task, not just a PM re-dispatching).
_TERMINAL_STATES = {"done", "cancelled"}


def _norm_title(t: str) -> str:
    """Normalize a feature title for duplicate comparison: trimmed, lowercased,
    internal whitespace collapsed. Exact-after-normalize only — no fuzzy match (a
    false positive silently drops a real task, worse than an occasional missed
    near-dup)."""
    return " ".join(str(t or "").strip().lower().split())


def _open_duplicate(features: list, title: str, depends_on: list | None = None) -> dict | None:
    """The first board feature this new card would DUPLICATE, or None. Two patterns,
    checked in order:

    1. TITLE — the first OPEN feature whose title matches ``title`` (normalized). A
       board's own agent (onboarding, or its own reasoning about a dispatched task) can
       call board_create_feature more than once for the same piece of work within a
       single turn; this catches it at the tool boundary, same as portfolio_dispatch's
       dedup guards the PM's re-dispatch one tier up.

    2. DEPENDS_ON — a card whose ``depends_on`` names a feature that is ``in_review``
       with an open ``pr_url``: the "work based on work that hasn't landed" pattern.
       Title matching can't see it (the new card has a legitimately different title), so
       it slips past pattern 1 and stacks a second card subordinate to in-flight work.
       The matched blocker is returned tagged ``_dup_reason="depends_on_in_review"`` so
       the caller can steer the author to board_requeue_feature (a fix round belongs on
       the in-flight feature's OWN PR, not a new card).

    ``features`` is the live board projection; ``depends_on`` is the new card's declared
    blocker ids."""
    features = features or []
    want = _norm_title(title)
    if want:
        for f in features:
            if str(f.get("board_state", "")).lower() in _TERMINAL_STATES:
                continue
            if _norm_title(f.get("title", "")) == want:
                return f
    by_id = {str(f.get("id", "")): f for f in features}
    for dep in depends_on or []:
        blocker = by_id.get(str(dep).strip())
        if blocker is None:
            continue
        if str(blocker.get("board_state", "")).lower() == "in_review" and str(blocker.get("pr_url", "")).strip():
            return {**blocker, "_dup_reason": "depends_on_in_review"}
    return None


def _strip_wrapping_quotes(s: str) -> str:
    """Peel ONE symmetric layer of literal wrapping double quotes off a string arg.

    Agents (and shells) sometimes hand us a value that arrives already wrapped in a
    literal pair of double quotes — an outer pair that is part of the value, not a
    delimiter — which, stored verbatim, then renders quoted in every downstream view
    and can even defeat the title dedup. Strip exactly one balanced outer layer (BOTH
    ends must be a double-quote char); inner or lopsided quotes are left untouched, and
    a non-string passes straight through."""
    if isinstance(s, str) and len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


def _split_list(raw: str) -> list[str]:
    """Comma- or newline-separated string → clean list (the shared normalization for
    files_to_modify and depends_on across create/update — round-4 DRY finding on #88)."""
    return [x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()]


def _feature_reply(f: dict) -> str:
    """Serialize a store feature for a tool return — carrying the success-with-warning
    trio through the boundary when set (QA panel on #88: stripping it hides the repair
    contract from the agent)."""
    out = {"id": f["id"], "state": f["board_state"], "title": f["title"]}
    if f.get("enrichment_failed"):
        out["enrichment_failed"] = True
        out["missing_fields"] = f.get("missing_fields", [])
        out["warning"] = f.get("warning", "")
    return json.dumps(out)


def _board_tools(cfg: dict):
    from .store import BoardError, get_store

    store_kw = dict(
        db=cfg.get("db_path") or None, repo=cfg.get("repo", "."), base_branch=cfg.get("base_branch", "main")
    )

    @tool
    def board_create_epic(title: str, description: str = "") -> str:
        """Create a top-level epic (a container for milestones/features)."""
        try:
            e = get_store(**store_kw).create_epic(title, description)
            return json.dumps({"id": e["id"], "title": e["title"]})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_create_feature(
        title: str,
        spec: str = "",
        acceptance_criteria: str = "",
        files_to_modify: str = "",
        design: str = "",
        parent: str = "",
        priority: int = 2,
        difficulty: str = "",
        depends_on: str = "",
        foundation: bool = False,
        force: bool = False,
        source_issue: str = "",
    ) -> str:
        """Create a board feature (a bead; starts in `backlog`). To pass the Ready
        gate a feature needs a self-sufficient `spec`, testable `acceptance_criteria`,
        AND `files_to_modify` (comma-separated paths to create/modify — vague tasks
        make a coding agent produce nothing). `parent` is the epic/milestone id;
        `difficulty` (small|medium|large) seeds the model tier; `depends_on` is a
        comma-separated list of blocking feature ids; set `foundation=True` for a
        feature others build on (dependents gate on its merge, never its review).
        `source_issue` names the ORIGINATING GitHub issue — a full issue URL or
        `owner/repo#N`, stored normalized as `owner/repo#N` (off-label, in the bead's
        notes metadata — a label can't carry `/`/`#`) — so the feature's PR gets a
        `Fixes #N` line and the issue auto-closes on merge.

        DEDUP: refuses to create when a feature with the same title is already OPEN
        on this board (backlog/ready/in_progress/in_review/blocked) — calling this
        twice for the same task (e.g. reconsidering mid-turn) stacks a duplicate the
        loop then churns on. It ALSO refuses when `depends_on` names a feature that is
        already `in_review` with an open PR: work built on in-flight work is a fix
        round and belongs on that feature's OWN PR — the warning points you at
        `board_requeue_feature(fid, findings=...)` instead. Pass `force=true` to create
        anyway. A store read failure never blocks creation (better a possible dup than a
        stuck board)."""
        try:
            store = get_store(**store_kw)
            # Input hygiene: an agent (or a shell one layer up) sometimes hands us a
            # value already wrapped in a literal pair of double quotes, which — stored
            # verbatim — renders quoted in every downstream view and even defeats the
            # title dedup below. Peel one symmetric outer layer off each string field
            # BEFORE anything is compared or stored.
            title = _strip_wrapping_quotes(title)
            spec = _strip_wrapping_quotes(spec)
            acceptance_criteria = _strip_wrapping_quotes(acceptance_criteria)
            files_to_modify = _strip_wrapping_quotes(files_to_modify)
            design = _strip_wrapping_quotes(design)
            parent = _strip_wrapping_quotes(parent)
            difficulty = _strip_wrapping_quotes(difficulty)
            depends_on = _strip_wrapping_quotes(depends_on)
            source_issue = _strip_wrapping_quotes(source_issue)
            deps = _split_list(depends_on)
            files = _split_list(files_to_modify)
            if not force:
                try:
                    existing = store.list_features()
                except BoardError:
                    existing = []  # can't check → don't block creation on a read failure
                dup = _open_duplicate(existing, title, deps)
                if dup is not None:
                    if dup.get("_dup_reason") == "depends_on_in_review":
                        # The new card builds on an in-flight feature (in_review, open PR).
                        # That's a fix round, and a fix round belongs on the in-flight
                        # feature's OWN PR — point the author at the requeue verb (#112),
                        # not a second card the loop would churn on.
                        return (
                            f"Skipped — this card's depends_on names {dup.get('id', '?')}, which is "
                            f"already in_review with an open PR ({dup.get('pr_url', '')}). Work built "
                            "on an in-flight feature belongs on THAT feature's PR, not a fresh card: "
                            f"call board_requeue_feature({dup.get('id', '?')!r}, findings=...) to carry "
                            "your changes onto the same branch. Pass force=true to stack a new card anyway."
                        )
                    return (
                        f"Skipped — a feature titled {title!r} is already open on this board "
                        f"({dup.get('id', '?')}, {dup.get('board_state', 'open')}). It's likely "
                        "the same work; re-check the board before creating again, or pass "
                        "force=true to create a second copy anyway."
                    )
            f = store.create_feature(
                title,
                spec=spec,
                acceptance_criteria=acceptance_criteria,
                design=design,
                files_to_modify=files,
                parent=parent,
                priority=priority,
                difficulty=difficulty,
                depends_on=deps,
                foundation=foundation,
                source_issue=source_issue,
            )
            return _feature_reply(f)
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_update_feature(
        feature_id: str,
        title: str = "",
        spec: str = "",
        acceptance_criteria: str = "",
        files_to_modify: str = "",
        design: str = "",
        difficulty: str = "",
        depends_on: str = "",
        foundation: bool = False,
        source_issue: str = "",
    ) -> str:
        """Partially update an existing feature — the REPAIR path for a bead the Ready
        gate rejects. Only the non-empty arguments are written; every other field is left
        as-is. Use it to fill a missing `spec`, `acceptance_criteria`, or `files_to_modify`
        (comma-separated paths) on a feature `board_mark_ready` refused, then mark it ready
        again — no need to cancel and recreate the bead. `title` renames the feature
        (empty = leave unchanged). `difficulty` (small|medium|large)
        re-seeds the model tier. `depends_on` (comma-separated feature ids) ADDS blocking
        edges, and `foundation=True` restores the foundation flag — the repairs for
        dependencies/foundation dropped by a create-time failure (False = leave as-is;
        this tool never removes the flag). `source_issue` (a full GitHub issue URL or
        `owner/repo#N`, stored normalized off-label in the bead's notes metadata)
        sets/replaces the originating issue the feature's PR will reference as
        `Fixes #N`. Inputs are
        stripped of any literal wrapping double quotes before storage (same hygiene as
        board_create_feature)."""
        try:
            store = get_store(**store_kw)
            title = _strip_wrapping_quotes(title)
            spec = _strip_wrapping_quotes(spec)
            acceptance_criteria = _strip_wrapping_quotes(acceptance_criteria)
            files_to_modify = _strip_wrapping_quotes(files_to_modify)
            design = _strip_wrapping_quotes(design)
            difficulty = _strip_wrapping_quotes(difficulty)
            depends_on = _strip_wrapping_quotes(depends_on)
            source_issue = _strip_wrapping_quotes(source_issue)
            files = _split_list(files_to_modify)
            deps = _split_list(depends_on)
            f = store.update_feature(
                feature_id,
                # strip BEFORE the truthiness check (the difficulty convention below) so a
                # whitespace-only title is a no-op (None), never a "set the title" signal.
                title=title.strip() or None,
                spec=spec or None,
                acceptance_criteria=acceptance_criteria or None,
                design=design or None,
                files_to_modify=files or None,
                # strip BEFORE the truthiness check so a whitespace-only difficulty is a
                # no-op (None), never a `"   "` that reaches the store as a "set it" signal.
                difficulty=difficulty.strip() or None,
                depends_on=deps or None,
                foundation=foundation or None,
                source_issue=source_issue.strip() or None,
            )
            return _feature_reply(f)
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_get_feature(feature_id: str) -> str:
        """Read a single feature's FULL detail as JSON — `title`, `spec`,
        `acceptance_criteria`, `design`, `state`, `labels`, `pr_url`, `difficulty`,
        `files_to_modify`, `foundation`, `priority`, `source_issue`, `requirements`
        (the tracked requirement ledger decomposed from the acceptance criteria at
        mark_ready — each item `{id, text, status, decline_reason?}`, status one of
        open/done/declined; the completion gate refuses a PR while any item is
        open), plus two dependency views: `depends_on` (EVERY blocking edge — the
        historical ledger, including already-merged blockers) and `open_depends_on`
        (only the edges whose blocker is still OPEN — the live, actionable "what's
        blocking me now" signal). The READ half of a read-modify-write: fetch the
        current criteria/spec, revise them, then `board_update_feature` — no
        operator round-trip. Returns `Error: unknown feature …` for an id that
        isn't on the board."""
        try:
            f = get_store(**store_kw).get_feature(feature_id)
        except BoardError as exc:
            return f"Error: {exc}"
        if f is None:
            return f"Error: unknown feature {feature_id!r}"
        return json.dumps(
            {
                "id": f["id"],
                "title": f["title"],
                "spec": f["spec"],
                "acceptance_criteria": f["acceptance_criteria"],
                "design": f["design"],
                "state": f["board_state"],
                "labels": f.get("labels", []),
                "pr_url": f["pr_url"],
                "difficulty": f["difficulty"],
                "files_to_modify": f.get("files_to_modify", []),
                "foundation": f.get("foundation", False),
                "priority": f.get("priority", 2),
                "source_issue": f.get("source_issue", ""),
                "requirements": f.get("requirements", []),
                "depends_on": f.get("depends_on", []),
                "open_depends_on": f.get("open_depends_on", []),
            }
        )

    @tool
    def board_mark_ready(feature_id: str) -> str:
        """Promote a feature backlog → ready. Fails if it lacks a spec +
        acceptance_criteria (the Ready gate). Only `ready` features are pulled."""
        try:
            f = get_store(**store_kw).mark_ready(feature_id)
            return json.dumps({"id": f["id"], "state": f["board_state"]})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_cancel_feature(feature_id: str, reason: str = "") -> str:
        """Cancel a feature created in error (bad decomposition, duplicate, scope cut) —
        the verb that RETIRES a bad card. Tags the bead `cancelled` and closes it with an
        auditable `reason` (`br close -r`), a second terminal edge that keeps the card (and
        its history) visible in a distinct `cancelled` state — NOT `done`, so the merge/CI
        reconcilers and loop-retro never mistake it for shipped work. The bead survives (a
        cancel, not a hard delete). `reason` is stripped of any literal wrapping double
        quotes before storage (same hygiene as board_create_feature)."""
        try:
            reason = _strip_wrapping_quotes(reason)
            f = get_store(**store_kw).cancel_feature(feature_id, reason)
            return json.dumps({"id": f["id"], "state": f["board_state"]})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_requeue_feature(feature_id: str, findings: str = "") -> str:
        """Put a feature back to `ready` for re-dispatch, keeping its open PR — the verb
        the fix-round doctrine needs to carry review findings to the SAME branch.

        WITH `findings` (an adverse review's requested changes): mirrors
        `POST /features/{fid}/review` — records a DISTINCT review-bounce comment on the
        bead (`record_review_bounce`), queues the findings so the loop's NEXT dispatch
        prompt LEADS with them (`queue_review_feedback`), THEN requeues onto the same
        open PR (`requeue`). This is the fix round: the coder re-dispatches knowing
        exactly what to fix, instead of blindly re-running the original prompt. Requires
        `in_review` (record_review_bounce enforces the state an adverse review lands
        from).

        WITHOUT `findings` (empty): a BARE re-dispatch — just `requeue`. Back to `ready`,
        open PR preserved, assignee cleared, NO new feedback; the coder re-runs against
        the ORIGINAL prompt (a plain retry, e.g. after a transient failure). The puller
        re-claims it and the loop re-dispatches (at the higher tier if it was just
        escalated; the open PR is pushed to, not reopened). `findings` is stripped of any
        literal wrapping double quotes before storage (same hygiene as
        board_create_feature)."""
        try:
            store = get_store(**store_kw)
            findings = _strip_wrapping_quotes(findings)
            if findings.strip():
                # Mirror the non-escalating POST /features/{fid}/review path: record the
                # distinct review-bounce comment, hand the findings to the loop so its
                # next dispatch prompt LEADS with them, THEN requeue onto the same PR.
                # Without this the findings would silently not travel and the coder would
                # burn a run re-producing the rejected output. Import queue_review_feedback
                # lazily (as the API route does) — the loop imports from here, so a
                # top-level import would be circular.
                store.record_review_bounce(feature_id, findings)
                from .loop import queue_review_feedback

                queue_review_feedback(feature_id, findings)
            f = store.requeue(feature_id)
            return json.dumps({"id": f["id"], "state": f["board_state"]})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_block_feature(feature_id: str, reason: str) -> str:
        """Flag a feature `blocked` with a `reason` — it stays ON the board (blocked is a
        flag, not a lane) with the reason visible, and is skipped by the puller until
        cleared. Complements the board_update_feature repair path: block when the feature
        is stuck on something external (a missing dep, an unanswered question) and you want
        the card parked-and-visible; update when the spec itself needs fixing. `reason` is
        stripped of any literal wrapping double quotes before storage (same hygiene as
        board_create_feature)."""
        try:
            reason = _strip_wrapping_quotes(reason)
            f = get_store(**store_kw).flag_blocked(feature_id, reason)
            return json.dumps({"id": f["id"], "state": f["board_state"]})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_unblock_feature(feature_id: str) -> str:
        """Clear the `blocked` flag so the feature can be re-dispatched — the inverse of
        board_block_feature. Removes the blocked label; the puller can claim it again once
        it's otherwise `ready`."""
        try:
            f = get_store(**store_kw).clear_blocked(feature_id)
            return json.dumps({"id": f["id"], "state": f["board_state"]})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_list(state: str = "", include_archived: bool = False) -> str:
        """List board features, optionally filtered by board `state` (backlog/ready/
        in_progress/in_review/done/blocked). Priority order. Terminal features
        (done/cancelled) past the archive window carry an `archived` label and are
        EXCLUDED from this default response (#115) — the live board, not all of
        history. Pass `include_archived=true` for the exhaustive view; nothing is
        ever deleted, so archived features stay fully queryable."""
        feats = get_store(**store_kw).list_features(state=state or None, include_archived=include_archived)
        return json.dumps(
            [
                {
                    "id": f["id"],
                    "title": f["title"],
                    "state": f["board_state"],
                    "blocked": f["blocked"],
                    "dag_blocked": f.get("dag_blocked", False),
                    "pr_url": f["pr_url"],
                    "priority": f["priority"],
                    "difficulty": f["difficulty"],
                }
                for f in feats
            ]
        )

    @tool
    def board_retro() -> str:
        """Retro the board: mine the attempt/outcome history of completed + blocked
        features into recurring failure CLASSES + flow stats (escalation / block /
        multi-attempt rates + the blocked features and why). The loop-retro skill
        reads this to distill durable grounding (PROTO.md gotchas) so the next runs
        stop repeating known failures. Read-only. Spans ALL history: the store's
        retro read explicitly opts into `archived` features (#115), so a
        retrospective never silently shrinks to the archive window."""
        from . import retro

        d = retro.summarize(get_store(**store_kw).raw_features_with_comments())
        return json.dumps(
            {
                "n_features": d["n_features"],
                "recurring_classes": d["recurring_classes"],
                "escalation_rate": d["escalation_rate"],
                "block_rate": d["block_rate"],
                "multi_attempt_rate": d["multi_attempt_rate"],
                "blocked_features": d["blocked_features"],
            },
            indent=2,
        )

    return [
        board_create_epic,
        board_create_feature,
        board_update_feature,
        board_get_feature,
        board_mark_ready,
        board_cancel_feature,
        board_requeue_feature,
        board_block_feature,
        board_unblock_feature,
        board_list,
        board_retro,
    ]
