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
    cfg = registry.config or {}

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


def _open_duplicate(features: list, title: str) -> dict | None:
    """The first OPEN board feature whose title matches ``title`` (normalized), or
    None. A board's own agent (onboarding, or its own reasoning about a dispatched
    task) can call board_create_feature more than once for the same piece of work
    within a single turn — this catches it at the tool boundary, same as
    portfolio_dispatch's dedup guards the PM's re-dispatch one tier up."""
    want = _norm_title(title)
    if not want:
        return None
    for f in features or []:
        if str(f.get("board_state", "")).lower() in _TERMINAL_STATES:
            continue
        if _norm_title(f.get("title", "")) == want:
            return f
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
        `owner/repo#N`, stored normalized as `owner/repo#N` — so the feature's PR
        gets a `Fixes #N` line and the issue auto-closes on merge.

        DEDUP: refuses to create when a feature with the same title is already OPEN
        on this board (backlog/ready/in_progress/in_review/blocked) — calling this
        twice for the same task (e.g. reconsidering mid-turn) stacks a duplicate the
        loop then churns on. Pass `force=true` to create a second copy anyway. A
        store read failure never blocks creation (better a possible dup than a
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
            if not force:
                try:
                    existing = store.list_features()
                except BoardError:
                    existing = []  # can't check → don't block creation on a read failure
                dup = _open_duplicate(existing, title)
                if dup is not None:
                    return (
                        f"Skipped — a feature titled {title!r} is already open on this board "
                        f"({dup.get('id', '?')}, {dup.get('board_state', 'open')}). It's likely "
                        "the same work; re-check the board before creating again, or pass "
                        "force=true to create a second copy anyway."
                    )
            deps = _split_list(depends_on)
            files = _split_list(files_to_modify)
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
        again — no need to cancel and recreate the bead. `difficulty` (small|medium|large)
        re-seeds the model tier. `depends_on` (comma-separated feature ids) ADDS blocking
        edges, and `foundation=True` restores the foundation flag — the repairs for
        dependencies/foundation dropped by a create-time failure (False = leave as-is;
        this tool never removes the flag). `source_issue` (a full GitHub issue URL or
        `owner/repo#N`, stored normalized) sets/replaces the originating issue the
        feature's PR will reference as `Fixes #N`. Inputs are
        stripped of any literal wrapping double quotes before storage (same hygiene as
        board_create_feature)."""
        try:
            store = get_store(**store_kw)
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
    def board_mark_ready(feature_id: str) -> str:
        """Promote a feature backlog → ready. Fails if it lacks a spec +
        acceptance_criteria (the Ready gate). Only `ready` features are pulled."""
        try:
            f = get_store(**store_kw).mark_ready(feature_id)
            return json.dumps({"id": f["id"], "state": f["board_state"]})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_list(state: str = "") -> str:
        """List board features, optionally filtered by board `state` (backlog/ready/
        in_progress/in_review/done/blocked). Priority order."""
        feats = get_store(**store_kw).list_features(state=state or None)
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
        stop repeating known failures. Read-only."""
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

    return [board_create_epic, board_create_feature, board_update_feature, board_mark_ready, board_list, board_retro]
