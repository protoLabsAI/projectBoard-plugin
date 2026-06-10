"""project_board — lean 6-state coding board + ACP spawn loop.

Composition over construction: the board is a SQLite data model + an HTTP API +
the orchestration loop, and the loop dispatches through the already-built
``delegates`` plugin (ADR 0024/0025) — it does NOT reimplement the spawn primitive.

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

    # Board HTTP API + console view, mounted as ONE router under the GATED prefix
    # /api/plugins/project_board (inherits the operator bearer gate; it serves
    # board/user data + the iframe page). The board view's GET /board route is
    # folded into this same router so the declared view path
    # (/api/plugins/project_board/board) is genuinely served. We must NOT register a
    # second router at the same prefix: the host dedupes routers by
    # (plugin_id, prefix), so a second one would be silently dropped → the view 404s.
    try:
        from .api import build_router
        registry.register_router(build_router(cfg), prefix="/api/plugins/project_board")
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

    log.info("[project_board] registered board API + loop + tools + planning (coder=%s reviewer=%s)",
             cfg.get("coder", "proto"), cfg.get("reviewer", "quinn"))


def _board_tools(cfg: dict):
    from .store import BoardError, get_store

    store_kw = dict(db=cfg.get("db_path") or None, repo=cfg.get("repo", "."),
                    base_branch=cfg.get("base_branch", "main"))

    @tool
    def board_create_epic(title: str, description: str = "") -> str:
        """Create a top-level epic (a container for milestones/features)."""
        try:
            e = get_store(**store_kw).create_epic(title, description)
            return json.dumps({"id": e["id"], "title": e["title"]})
        except BoardError as exc:
            return f"Error: {exc}"

    @tool
    def board_create_feature(title: str, spec: str = "", acceptance_criteria: str = "",
                             files_to_modify: str = "", design: str = "", parent: str = "",
                             priority: int = 2, difficulty: str = "", depends_on: str = "") -> str:
        """Create a board feature (a bead; starts in `backlog`). To pass the Ready
        gate a feature needs a self-sufficient `spec`, testable `acceptance_criteria`,
        AND `files_to_modify` (comma-separated paths to create/modify — vague tasks
        make a coding agent produce nothing). `parent` is the epic/milestone id;
        `difficulty` (small|medium|large) seeds the model tier; `depends_on` is a
        comma-separated list of blocking feature ids."""
        try:
            deps = [d.strip() for d in depends_on.split(",") if d.strip()]
            files = [p.strip() for p in files_to_modify.replace("\n", ",").split(",") if p.strip()]
            f = get_store(**store_kw).create_feature(
                title, spec=spec, acceptance_criteria=acceptance_criteria, design=design,
                files_to_modify=files, parent=parent, priority=priority,
                difficulty=difficulty, depends_on=deps)
            return json.dumps({"id": f["id"], "state": f["board_state"], "title": f["title"]})
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
        return json.dumps([
            {"id": f["id"], "title": f["title"], "state": f["board_state"],
             "blocked": f["blocked"], "dag_blocked": f.get("dag_blocked", False),
             "pr_url": f["pr_url"], "priority": f["priority"], "difficulty": f["difficulty"]}
            for f in feats
        ])

    return [board_create_epic, board_create_feature, board_mark_ready, board_list]
