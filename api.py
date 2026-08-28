"""Board HTTP API + console view (D5).

TWO routers (plugin-view rule 2): ``build_router`` carries the public-of-necessity
surface on ``/plugins/project_board`` — GET ``/board`` (an iframe src can't carry a
bearer) plus the HMAC-authenticated external mutation ingress: POST ``/webhook/pr``,
``/features/{fid}/ci``, and ``/features/{fid}/review``. ``build_data_router``
carries the operator CRUD/transition routes on ``/api/plugins/project_board``, where
they inherit the host's operator bearer gate. The whole flow — create project →
features → Ready gate → (loop dispatches) → in_review → merge webhook → done — is
drivable here, headlessly.

The ``/webhook/pr`` endpoint is the SINGLE external Done edge: a merged-PR event
sets ``done`` and nothing else does (invariant #2). Every public mutation verifies
its raw body against ``X-Hub-Signature-256`` and fails closed when no
``webhook_secret`` is configured.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from typing import Literal

from fastapi import Request  # module-level so the webhook's stringized annotation resolves
from pydantic import BaseModel, ConfigDict, Field

from . import setup_check
from .projects import default_project as resolve_default_project
from .projects import resolve_projects, store_db_path
from .store import BoardError, annotate_next_action, escalation_enabled, get_store

log = logging.getLogger("protoagent.plugins.project_board")


class ProjectUpsertBody(BaseModel):
    """Strict operator API shape for one editor-owned project entry."""

    model_config = ConfigDict(extra="forbid", strict=True)

    repo: str = Field(min_length=1, max_length=4096)
    base_branch: str = Field(default="main", min_length=1, max_length=255)
    local_gate_cmd: str = Field(default="", max_length=8192)
    repo_conventions: str = Field(default="", max_length=32768)
    default_action: Literal["keep", "set", "clear"] = "keep"


def _store_kw(cfg: dict) -> dict:
    """The store constructor kwargs shared by both routers. Carries the board's
    `projects:` map + `default_project` (#90) so the API's store resolves feature
    defaults and per-feature repos (the Ready gate's `_repo_for`) exactly as the loop
    does — a create with no `project` stamps the board default, and a labeled feature's
    gate validates against its own repo."""
    cfg = cfg or {}
    return dict(
        # D3 (#260): the db is resolved at the config seam, the same way as the tool
        # store_kw and the coder-monitor persist factory — a blank/absent db_path rides
        # the ONE instance-default store (store.default_db_path), an explicit path
        # stays the operator pin verbatim. Resolving HERE re-homes nothing: get_store
        # itself resolves a blank db to the same instance default, so this hands it the
        # exact path the pre-seam `db=cfg.get("db_path") or None` call already landed
        # on — same cache key, same board (pinned in test_projects). Cards a pre-D3
        # install left in a repo's `.beads/` are surfaced by the setup preflight's
        # migration advisory (setup_check.legacy_store_repos), never silently dropped.
        db=store_db_path(cfg),
        repo=cfg.get("repo", "."),
        base_branch=cfg.get("base_branch", "main"),
        max_files_by_difficulty=cfg.get("max_files_by_difficulty"),
        projects=resolve_projects(cfg),
        default_project=resolve_default_project(cfg),
    )


def repo_for_feature(feature: dict | None, store_kw: dict) -> str:
    """The repo root ``feature`` builds in — the route/tool sibling of the loop's
    per-feature ``_repo_for`` (loop.py), resolving in the SAME order so the terminal
    edges act in the project's checkout (#262). A feature carrying an explicit
    ``project:<name>`` label resolves STRICTLY to that project's ``repo``; an
    unlabeled feature keeps the store-stamped repo, then the default project's,
    then the instance default (back-compat). Shared by the merge webhook and the
    cancel/done/delete edges here and by ``board_cancel_feature`` (__init__.py) —
    without it every terminal edge reaped (and closed PRs) under the board-default
    repo, stranding a non-default project's worktree until the health sweep."""
    feature = feature or {}
    projects = store_kw.get("projects") or {}
    default = str(store_kw.get("default_project") or "").strip()
    name = str(feature.get("project") or "").strip()
    if name:
        repo = str((projects.get(name) or {}).get("repo") or "").strip()
        if repo:
            return repo
    entry = projects.get(name or default)
    if entry is None:
        entry = projects.get(default)
    return (
        str(feature.get("repo") or "").strip()
        or str((entry or {}).get("repo") or "").strip()
        or str(store_kw.get("repo") or ".")
    )


def build_router(cfg: dict):
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import HTMLResponse

    from .board_view import BOARD_PAGE
    from .projects_view import PROJECTS_PAGE

    router = APIRouter()

    # ── console view (ADR 0026) — the Kanban/list page the left-rail icon iframes.
    # Served by THIS router (not a second one) so the declared view path
    # /plugins/project_board/board is genuinely mounted; the host dedupes
    # routers by (plugin_id, prefix), so a second router here would be dropped.
    @router.get("/board", response_class=HTMLResponse)
    async def _board():
        return HTMLResponse(BOARD_PAGE)

    @router.get("/config/projects", response_class=HTMLResponse)
    async def _projects_config():
        """Public page chrome for the sandboxed Configure tab.

        It contains no config data; every read and mutation goes through the
        operator-bearer-gated ``/api/plugins/project_board/projects`` routes.
        """
        return HTMLResponse(PROJECTS_PAGE, headers={"Cache-Control": "no-store"})

    store_kw = _store_kw(cfg)
    escalate_on = escalation_enabled(cfg)
    worktrees_root = (cfg or {}).get("worktrees_root", ".worktrees")
    # Shared external-ingress secret (HMAC-SHA256). GitHub supplies the signature for
    # /webhook/pr; CI/review callers sign their exact JSON bytes the same way. Blank
    # fails CLOSED: these public routes mutate board state and cannot have a dev-mode
    # authentication bypass merely because the host is reachable only on localhost.
    webhook_secret = str(
        (cfg or {}).get("webhook_secret") or os.environ.get("PROJECT_BOARD_WEBHOOK_SECRET", "")
    ).strip()

    def store():
        return get_store(**store_kw)

    async def _guard(fn):
        # Off the event loop (#258): every store touch blocks in `_run` (subprocess
        # + contention sleeps) — on the loop thread that stalls the tick and every
        # other route for the duration.
        try:
            return await asyncio.to_thread(fn)
        except BoardError as e:
            raise HTTPException(400, str(e))

    def _verify_external(raw: bytes, signature: str) -> None:
        if not webhook_secret:
            raise HTTPException(
                503,
                "public board mutations are disabled — configure project_board.webhook_secret "
                "or PROJECT_BOARD_WEBHOOK_SECRET",
            )
        expected = "sha256=" + hmac.new(webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature or ""):
            raise HTTPException(401, "invalid webhook signature")

    async def _signed_json(request: Request) -> dict:
        """Authenticate exact raw bytes, then require a JSON object body."""
        raw = await request.body()
        _verify_external(raw, request.headers.get("X-Hub-Signature-256", ""))
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(400, "invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(400, "JSON body must be an object")
        return body

    # The operator CRUD/transition routes moved to build_data_router — gated under
    # /api/plugins/project_board (plugin-view rule 2). What stays here is the
    # PUBLIC-of-necessity surface: the /board page (an iframe page-load can't
    # carry a bearer) and the CI-infra edges — /webhook/pr (GitHub signs with
    # HMAC, not the operator bearer), /features/{fid}/ci, and /review. Every POST
    # below crosses the same fail-closed HMAC boundary before touching the store.

    @router.post("/features/{fid}/ci")
    async def _ci(fid: str, request: Request):
        """CI result for the feature's PR. ``passed: true`` is a no-op (merge sets
        done, via the webhook). ``passed: false``:
          - with an escalation ladder → record + climb a tier and **requeue** to
            ready (the puller re-dispatches at the higher tier, pushing to the same
            PR); when the ladder is exhausted → Blocked.
          - with a single coder → bounce to in_progress for the operator (no auto-
            requeue, so a persistently-failing coder can't loop forever)."""
        body = await _signed_json(request)
        if bool(body.get("passed")):
            return {"ok": True, "note": "CI green — done is set by the merge webhook, not CI"}
        reason = str(body.get("reason", ""))

        def _handle():
            s = store()
            if not escalate_on:
                return {"requeued": False, "escalated": False, "feature": s.bounce_ci_fail(fid, reason)}
            nxt = s.escalate(fid, f"ci-fail: {reason}" if reason else "ci-fail")
            if nxt is None:
                return {
                    "requeued": False,
                    "escalated": True,
                    "exhausted": True,
                    "feature": s.block_from_review(fid, f"ci-fail: {reason}"),
                }
            return {"requeued": True, "escalated": True, "next_tier": nxt, "feature": s.requeue(fid)}

        return await _guard(_handle)

    @router.post("/features/{fid}/review")
    async def _review(fid: str, request: Request):
        """Adverse code-review bounce for the feature's open PR — the review sibling
        of ``/ci`` fail. Records the ``findings`` as a DISTINCT review-bounce comment
        on the bead (≠ ci-fail), feeds them into the next dispatch prompt (the same
        ``_ci_feedback`` lever the in-loop review gate uses), and **requeues onto the
        SAME open PR** (``pr_url`` preserved via external_ref). Works from
        ``in_review``.

        Body: ``{findings: str, escalate: bool=false}``. ``escalate=true`` climbs the
        model ladder (like ``/ci``); the default keeps the same tier. With escalation
        enabled and the ladder already at the top, an escalated bounce → Blocked
        (never a silent re-loop)."""
        body = await _signed_json(request)
        findings = str(body.get("findings", ""))
        escalate = bool(body.get("escalate", False))

        def _handle():
            s = store()
            # Distinct review-bounce comment on the bead (enforces in_review), then hand
            # the findings to the loop so its next dispatch prompt LEADS with them — the
            # external sibling of the in-loop review gate's _ci_feedback write.
            s.record_review_bounce(fid, findings)
            from .loop import queue_review_feedback

            queue_review_feedback(fid, findings)
            if escalate and escalate_on:
                nxt = s.escalate(fid, f"review-fail: {findings}" if findings else "review-fail")
                if nxt is None:
                    return {
                        "requeued": False,
                        "escalated": True,
                        "exhausted": True,
                        "feature": s.block_from_review(fid, f"review-fail: {findings}"),
                    }
                return {"requeued": True, "escalated": True, "next_tier": nxt, "feature": s.requeue(fid)}
            # escalate=false (or no ladder configured): requeue at the SAME tier.
            return {"requeued": True, "escalated": False, "feature": s.requeue(fid)}

        return await _guard(_handle)

    # ── the ONE Done edge: merge webhook ──────────────────────────────────────
    @router.post("/webhook/pr")
    async def _webhook_pr(request: Request):
        """GitHub PR webhook — the SINGLE Done edge. On a ``closed`` event with
        ``merged: true`` it sets the matching feature ``done`` (nothing else does)
        and reaps its worktree. The raw body is HMAC-verified against
        ``X-Hub-Signature-256``; no configured secret fails closed."""
        raw = await request.body()
        _verify_external(raw, request.headers.get("X-Hub-Signature-256", ""))
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(400, "invalid JSON body")

        action = body.get("action")
        pr = body.get("pull_request") or {}
        if action != "closed" or not pr.get("merged"):
            return {"ok": True, "ignored": f"action={action} merged={pr.get('merged')}"}
        pr_url = pr.get("html_url") or ""
        # Off the event loop (#258): record_merge scans + closes via `br` (subprocess).
        f = await asyncio.to_thread(lambda: store().record_merge(pr_url=pr_url))
        if f is None:
            return {"ok": True, "ignored": f"no feature for PR {pr_url}"}
        # Reap the feature's worktree now that it's merged → done (stop accumulation).
        # #262: under ITS project's repo — a project-B feature's worktree lives in B's
        # checkout, not the board default's.
        try:
            from . import worktree

            await worktree.reap_feature_worktree(repo_for_feature(f, store_kw), worktrees_root, f["id"])
        except Exception:  # noqa: BLE001 — reaping is best-effort; done is already set
            log.warning("[project_board] worktree reap for %s failed", f["id"], exc_info=True)
        log.info("[project_board] merge webhook → done: %s (%s)", f["id"], pr_url)
        return {"ok": True, "feature": f}

    return router


def build_data_router(cfg: dict, *, gap_reporter=None):
    """The operator CRUD/transition routes — mounted under
    ``/api/plugins/project_board`` so they inherit the operator bearer gate
    (plugin-view rule 2). Previously these lived under the public ``/plugins/``
    prefix: on a token-gated deployment anyone who could reach the port could
    create/transition features without the bearer.

    ``gap_reporter`` (v0.42.0) is the ``setup_check.GapReporter`` register() shares
    with the loop: ``GET /status`` re-reports its fresh preflight through it, so a
    host warning clears when the operator fixes the gap even on a board whose loop
    is off (no tick to re-check) — the board page polls /status while open."""
    from fastapi import APIRouter, Body, HTTPException
    from fastapi.responses import JSONResponse

    router = APIRouter()
    store_kw = _store_kw(cfg)
    worktrees_root = (cfg or {}).get("worktrees_root", ".worktrees")

    def store():
        # Existing routers stay mounted across a host config reload, so their
        # construction-time `cfg` is stale. Project routing is explicitly live:
        # resolve that complete unit afresh before the first post-edit store is
        # constructed (and reconfigure an existing shared store). Boot-only
        # db/repo/base remain pinned in `store_kw` as promised by the manifest.
        current = dict(store_kw)
        try:
            from .project_registry import _live_section

            live = _live_section(required=True)
            candidate = dict(cfg or {})
            if "projects" in live:
                candidate["projects"] = live.get("projects")
            if "default_project" in live:
                candidate["default_project"] = live.get("default_project")
            current["projects"] = resolve_projects(candidate)
            current["default_project"] = resolve_default_project(candidate)
        except Exception:  # noqa: BLE001 — retain the last validated routing unit
            pass
        board = get_store(**current)
        reconfigure = getattr(board, "reconfigure_projects", None)
        if callable(reconfigure):
            reconfigure(current["projects"], current["default_project"])
        store_kw.update(projects=current["projects"], default_project=current["default_project"])
        return board

    async def _guard(fn):
        # Off the event loop (#258): every store touch blocks in `_run` (subprocess
        # + contention sleeps) — on the loop thread that stalls the tick and every
        # other route for the duration.
        try:
            return await asyncio.to_thread(fn)
        except BoardError as e:
            raise HTTPException(400, str(e))

    @router.get("/projects")
    async def _projects():
        """Live project config for the custom Configure tab (no router snapshot)."""
        from .project_registry import ProjectRegistryError, project_registry_snapshot

        try:
            snapshot = project_registry_snapshot()
        except ProjectRegistryError as exc:
            raise HTTPException(503, str(exc))
        return JSONResponse(snapshot, headers={"Cache-Control": "no-store"})

    @router.put("/projects/{name}")
    async def _put_project(name: str, body: ProjectUpsertBody):
        """Add/update one boarded repo through the same bounded seam as the agent tool."""
        from .project_registry import ProjectRegistryError, upsert_project

        try:
            result = await upsert_project(
                name,
                body.repo,
                base_branch=body.base_branch,
                local_gate_cmd=body.local_gate_cmd,
                repo_conventions=body.repo_conventions,
                make_default=body.default_action == "set",
                clear_default=body.default_action == "clear",
                replace_optional=True,
            )
        except ProjectRegistryError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, **result}

    @router.delete("/projects/{name}")
    async def _delete_project(name: str):
        """Delete a project only after proving no active board card references it."""
        from .project_registry import ProjectRegistryConflict, ProjectRegistryError, delete_project

        async def assert_unused(project: str, effective_default: str) -> None:
            try:
                features = await asyncio.to_thread(store().list_features, include_archived=True)
            except BoardError as exc:
                raise ProjectRegistryConflict(f"cannot prove project {project!r} is unused: {exc}") from exc
            terminal = {"done", "cancelled"}
            references = [
                str(f.get("id") or "")
                for f in features
                if (
                    str(f.get("project") or "") == project
                    or (not str(f.get("project") or "") and effective_default == project)
                )
                and str(f.get("board_state") or f.get("state") or "") not in terminal
            ]
            if references:
                sample = ", ".join(references[:5])
                more = f" (+{len(references) - 5} more)" if len(references) > 5 else ""
                raise ProjectRegistryConflict(
                    f"project {project!r} is still referenced by active board card(s): {sample}{more}"
                )

        try:
            result = await delete_project(name, assert_unused=assert_unused)
        except ProjectRegistryConflict as exc:
            raise HTTPException(409, str(exc))
        except ProjectRegistryError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, **result}

    @router.get("/status")
    async def _status():
        """Is this board BOUND yet? A pure config read — no `br` calls — so it answers
        even when the store can't, which is exactly when the view needs it. The shipped
        default (`repo: "."`, no db_path, no projects map) only works when the process
        cwd IS the target repo; on a GUI/desktop member the cwd is the app bundle
        (read-only), so a first-run board fails every read with a BoardError. The view
        asks here to tell "never bound" (render setup guidance) from "bound but broken"
        (render the real error).

        ``setup`` (v0.42.0) is the full setup preflight — ``br``/``gh`` on PATH, every
        configured coder resolvable in the delegate roster, the repo bound and
        present — each ``{ok, hint, …}`` plus ``ready`` and the ``loop_blockers`` the
        puller is paused on (see setup_check.py). A board can be *bound* and still
        unable to run; the view renders each failing check with its hint. Still no
        ``br`` board op: one cached ``br --version`` at most. Never raises."""
        # NB: read `projects` from the RAW cfg, not store_kw — resolve_projects
        # back-compat synthesizes an implicit project from the flat keys, so the
        # resolved map is non-empty even for the unbound shipped default.
        raw_projects = (cfg or {}).get("projects")
        explicit_projects = isinstance(raw_projects, dict) and bool(raw_projects)
        # Same for the db (D3, #260): store_kw's `db` ALWAYS carries a real path now
        # (a blank db_path resolves to the instance-default store at the seam), so the
        # resolved value can no longer tell an operator's pin from the shipped default.
        # Only an EXPLICIT db_path is a binding signal — the setup_check._is_bound rule.
        explicit_db = bool(str((cfg or {}).get("db_path") or "").strip())
        bound = explicit_db or explicit_projects or str(store_kw.get("repo") or ".") not in ("", ".")
        try:
            # Off the event loop (review on #212): the preflight may shell `br
            # --version` (once per path) and reads the delegates YAML; this route is
            # polled every 10 s by the board page.
            setup = await asyncio.to_thread(setup_check.setup_status, cfg or {})
            if gap_reporter is not None:
                gap_reporter.report(setup)  # edge-triggered; a steady state forwards nothing
        except Exception as exc:  # noqa: BLE001 — setup_status never raises by contract; belt and braces
            log.warning("[project_board] /status setup preflight errored", exc_info=True)
            setup = {"ready": False, "error": str(exc)}
        # #255: a board can be fully `ready` and still be picking up nothing, because
        # one project's gate preflight fail-closed and its ready cards got held (they
        # then drop out of the ready scan, so the board just looks idle). Report it here
        # — this route is what the board page polls — rather than only in the log.
        from . import health

        preflight = health.preflight_snapshot()
        return {
            "bound": bound,
            "repo": store_kw.get("repo") or ".",
            "db_path": explicit_db,
            "projects": sorted(raw_projects) if explicit_projects else [],
            "setup": setup,
            "preflight": preflight,
            "held_projects": sorted(preflight["held"]),
        }

    async def _reap_worktree(fid: str, feature: dict | None = None) -> None:
        """Reap the feature's worktree at a terminal edge (cancel/delete) — the same
        best-effort pattern as the merge webhook's Done-edge reap (``build_router``).
        ``feature`` is the edge's projection: its project label resolves the repo the
        worktree actually lives in (#262 — the loop's ``_repo_for`` order), so a
        project-B card reaps under B's checkout, not the board default's. The bead is
        already closed, so a reap failure must never raise into the response; the
        health sweep stays the crash backstop for anything missed."""
        try:
            from . import worktree

            await worktree.reap_feature_worktree(repo_for_feature(feature, store_kw), worktrees_root, fid)
        except Exception:  # noqa: BLE001 — reaping is best-effort; the edge is already closed
            log.warning("[project_board] worktree reap for %s failed", fid, exc_info=True)

    # ── hierarchy (epic → milestone → feature) ────────────────────────────────
    @router.post("/epics")
    async def _create_epic(body: dict = Body(...)):
        return await _guard(lambda: store().create_epic(body.get("title", ""), body.get("description", "")))

    @router.post("/milestones")
    async def _create_milestone(body: dict = Body(...)):
        return await _guard(
            lambda: store().create_milestone(
                body.get("title", ""), body.get("epic_id", ""), body.get("description", "")
            )
        )

    # ── features ──────────────────────────────────────────────────────────────
    @router.get("/features")
    async def _features(state: str | None = None, include_archived: bool = False, project: str | None = None):
        # _guard, like every other store-touching route: an unusable board (no repo
        # bound, no .beads, br missing) must reach the view as JSON 400 with the
        # actionable BoardError message — an escaped BoardError is a text/plain 500
        # the view can only render as a JSON-parse error.
        # Default = the LIVE board: `archived` features (terminal + past the archive
        # window, #115) are excluded unless ?include_archived=true — same contract
        # as the board_list tool; nothing is deleted, everything stays queryable.
        # ?project=<name> (#90) narrows the listing to the features stamped for that
        # project — the multi-repo board's per-repo view; absent, every project is listed.
        # Every row carries `next_action` / `awaiting_merge` / `next_action_hint`
        # (#208) — derived from labels + the board's auto_merge/review_gate config,
        # no per-row network — so the console can chip "awaiting merge".
        want = str(project or "").strip()

        def _list():
            feats = store().list_features(state=state, include_archived=include_archived)
            if want:
                feats = [f for f in feats if str(f.get("project") or "") == want]
            return {"features": annotate_next_action(feats, cfg or {})}

        return await _guard(_list)

    @router.get("/features/{fid}")
    async def _feature(fid: str):
        f = await _guard(lambda: store().get_feature(fid))
        if f is None:
            raise HTTPException(404, f"unknown feature {fid!r}")
        return f

    @router.get("/features/{fid}/progress")
    async def _progress(fid: str):
        """Live coder-monitoring snapshot (#84) for the board view's monitor drawer.

        Returns ``{"gens": [{gen, tier, elapsed_s, current_tool, recent_tools,
        thought_tail, usage, verify}]}`` — the per-gen in-memory ring buffer the loop/
        coder_seam dispatch taps fill. 404 on an unknown feature; an empty-but-valid
        ``{"gens": []}`` for a known feature with no live (or recent) run in this
        process's memory. Read-only + purely in-process — never touches the board."""
        f = await _guard(lambda: store().get_feature(fid))
        if f is None:
            raise HTTPException(404, f"unknown feature {fid!r}")
        from . import coder_seam

        return coder_seam.progress_snapshot(fid)

    @router.patch("/features/{fid}")
    async def _patch_feature(fid: str, body: dict = Body(default={})):
        """In-place spec edit — the REST complement of ``board_update_feature``.
        Accepts ``title``, ``spec``, ``acceptance_criteria``, ``design``,
        ``files_to_modify``, ``difficulty``, ``source_issue``; only non-null
        fields are written. Refuses edits to an ``in_progress`` feature unless
        ``force=true`` is passed (a live drive owns it)."""
        body = body or {}
        force = bool(body.get("force", False))

        s = store()
        f = await _guard(lambda: s.get_feature(fid))
        if f is None:
            raise HTTPException(404, f"unknown feature {fid!r}")

        if f.get("board_state") == "in_progress" and not force:
            raise HTTPException(
                400,
                f"feature {fid!r} is in_progress — a live drive owns it; pass force=true to edit anyway",
            )

        _PATCH_FIELDS = frozenset(
            {
                "title",
                "spec",
                "acceptance_criteria",
                "design",
                "files_to_modify",
                "difficulty",
                "priority",
                "source_issue",
            }
        )
        kwargs = {k: v for k, v in body.items() if k in _PATCH_FIELDS and v is not None}
        changed = sorted(kwargs)

        updated = await _guard(lambda: s.update_feature(fid, **kwargs))
        if changed:
            await asyncio.to_thread(s._comment, fid, f"spec updated: {', '.join(changed)}")

        return updated

    @router.post("/features")
    async def _create_feature(body: dict = Body(...)):
        """Create a feature — the body is splatted into ``store.create_feature``, so it
        accepts every create field, including ``project`` (#90): the entry in the board's
        `projects:` map the feature builds in, stamped as an immutable ``project:<name>``
        label. Absent, it falls back to the board's ``default_project``."""
        return await _guard(lambda: store().create_feature(**body))

    @router.post("/features/batch")
    async def _create_from_plan(body: dict = Body(default={})):
        """Batch-create a whole decomposition (#92). Body: ``{"plan": [{title, spec,
        acceptance_criteria, files, difficulty, depends_on, foundation, source_issue}, …],
        "mark_ready": false}``. All-or-report: a malformed item fails itself with a
        named reason, the rest proceed; inter-item ``depends_on`` (by 0-based index or
        title) resolves after every create; ``mark_ready`` promotes only clean items."""
        return await _guard(
            lambda: store().create_from_plan(
                (body or {}).get("plan") or [], mark_ready=bool((body or {}).get("mark_ready", False))
            )
        )

    @router.post("/features/{fid}/dep")
    async def _dep(fid: str, body: dict = Body(...)):
        """Add a `blocks` edge: `fid` waits for `depends_on` to be merged→done.
        (Foundation gating is just a blocks-edge on the foundation feature.)"""
        return await _guard(
            lambda: (store().add_dependency(fid, str(body.get("depends_on", ""))), store().get_feature(fid))[1]
        )

    @router.delete("/features/{fid}/dep")
    async def _dep_delete(fid: str, body: dict = Body(default={})):
        """Remove a `blocks` edge — inverse of POST …/dep. Body: ``{"depends_on": "<id>"}``."""
        return await _guard(
            lambda: (
                store().remove_dependency(fid, str((body or {}).get("depends_on", ""))),
                store().get_feature(fid),
            )[1]
        )

    # ── transitions ───────────────────────────────────────────────────────────
    @router.post("/features/{fid}/ready")
    async def _ready(fid: str):
        """The Ready gate (invariant #1) — 400 if spec/acceptance_criteria missing."""
        return await _guard(lambda: store().mark_ready(fid))

    @router.post("/features/{fid}/block")
    async def _block(fid: str, body: dict = Body(...)):
        return await _guard(lambda: store().flag_blocked(fid, str(body.get("reason", ""))))

    @router.post("/features/{fid}/unblock")
    async def _unblock(fid: str):
        return await _guard(lambda: store().clear_blocked(fid))

    @router.post("/features/{fid}/cancel")
    async def _cancel(fid: str, body: dict = Body(default={})):
        """Cancel a feature created in error — the second terminal edge (#47). Closes
        the bead with an audit reason and tags it `cancelled` (a distinct state, not
        `done`), so a bad decomposition/duplicate leaves the board cleanly instead of
        being deleted out-of-band (which desyncs the board ↔ JSONL). Reaps the
        feature's worktree once cancel succeeds — a terminal edge leaves nothing left to
        build — the same reap the merge webhook does at `done` (#109).

        #211: a cancel must not leave an open PR or a running coder. The card's open
        `pr_url` (a cancel during the CI/review bounce) is closed with a comment pointing
        at the card, and an in-flight drive is stopped (its own cancel path closes a PR
        it opened meanwhile + reaps). Both best-effort: a gh failure logs, never 400s."""
        pr_url = ""
        before = None
        try:
            before = await asyncio.to_thread(lambda: store().get_feature(fid))
            pr_url = str((before or {}).get("pr_url") or "").strip()
        except BoardError:
            pass  # the cancel below raises the named error for an unknown card
        f = await _guard(lambda: store().cancel_feature(fid, str((body or {}).get("reason", ""))))
        from .loop import cancel_side_effects

        # #262: close the PR and reap under the FEATURE's project repo (the pre-cancel
        # read carries the label; the cancel projection is the fallback), not the default.
        repo = repo_for_feature(before or f, store_kw)
        side = await asyncio.to_thread(cancel_side_effects, fid, pr_url, cwd=repo)
        await _reap_worktree(fid, before or f)
        return {**f, "cancel": side}

    @router.post("/features/{fid}/done")
    async def _done(fid: str, body: dict = Body(default={})):
        """Mark a feature `done` by hand — the MANUAL Done edge (#228), for work that
        shipped OUTSIDE the board's PR lifecycle (record_merge's pr_url→external_ref
        match never fires). Accepts only an in-flight card (in_progress/in_review/
        blocked); 400 on a backlog/ready/done/cancelled one. Body: ``{reason}`` — the
        audit trail for WHY it was hand-closed, recorded as a comment on the bead.
        Reaps the feature's worktree once done — same terminal-edge reap as
        cancel/merge (#109)."""
        f = await _guard(lambda: store().mark_done(fid, reason=str((body or {}).get("reason", ""))))
        await _reap_worktree(fid, f)
        return f

    # ── task-type review lane (#217): deliver → verify, the coder-PR-free siblings
    #    of open_review → record_merge. deliver moves in_progress → in_review;
    #    verify is the task Done edge (approve closes, reject requeues to ready).
    @router.post("/features/{fid}/deliver")
    async def _deliver(fid: str, body: dict = Body(default={})):
        """Record a task-type feature's DELIVERABLE (#217) — the task sibling of the
        coder's open_review edge, moving in_progress → in_review. Body:
        ``{text?, ref?}``: ``text`` rides a `deliverable:` comment (the projection's
        `deliverable` reads the latest back), ``ref`` (a doc URL / artifact path)
        lands on `external_ref` — the slot a coding feature's pr_url occupies.
        TASK-ONLY: ``record_delivery`` 400s a coding feature (entering review with no
        pr_url would strand the merge reconciler) or one not in_progress."""
        body = body or {}
        return await _guard(
            lambda: store().record_delivery(fid, text=str(body.get("text", "")), ref=str(body.get("ref", "")))
        )

    @router.post("/features/{fid}/verify")
    async def _verify(fid: str, body: dict = Body(default={})):
        """The task-type Done edge (#217) — ``record_merge``'s verify sibling. Body:
        ``{approved?: bool=true, feedback?}``. ``approved=true`` closes the task with
        a `verified: <actor>` reason; ``approved=false`` records the ``feedback`` as a
        comment (the re-dispatch prompt injects it, the adverse-review shape) and
        requeues the bead to ready. Expects in_review. TASK-ONLY: a coding feature is
        refused (it closes via ``record_merge``, the ONE Done edge for code)."""
        body = body or {}
        approved = bool(body.get("approved", True))
        return await _guard(
            lambda: store().record_verification(fid, approved=approved, feedback=str(body.get("feedback", "")))
        )

    @router.delete("/features/{fid}")
    async def _delete(fid: str, body: dict = Body(default={})):
        """Hard-delete a feature created in error — a `br` tombstone (the harder sibling
        of POST …/cancel). Goes through the board so board ↔ JSONL stay consistent;
        refuses (400) if the feature has dependents (deleting would orphan them). Prefer
        cancel to keep a visible, reopenable audit lane; use delete to leave no trace.
        Reaps the feature's worktree too — same terminal-edge class as cancel/merge, a
        deleted feature leaves nothing to build (#109)."""
        f = await _guard(lambda: store().delete_feature(fid, str((body or {}).get("reason", ""))))
        await _reap_worktree(fid, f)
        return f

    # ── coder.solve() rung diagnostic (ADR 0064) — OPERATOR ONLY, deliberately no
    #    @tool wrapper: same boundary this router already draws around cancel/
    #    block/delete — the board's own lead agent has no tool to reach this.
    @router.post("/features/{fid}/test-rung")
    async def _test_rung(fid: str, body: dict = Body(...)):
        """Run exactly ONE named rung of coder.solve() against this feature's REAL
        acceptance tests, in a throwaway worktree that's ALWAYS reaped — never
        promoted, no PR opened, no board state touched. For verifying a rung
        actually works (fusion especially — otherwise only reached after three
        cheaper rungs fail) without contriving a task that fails its way there.

        Body: ``{"rung": "greedy"|"best-of-k"|"tree-search"|"fusion", "coder": "<delegate
        name>"}`` (``coder`` optional, defaults to ``project_board.coder``)."""
        rung = str(body.get("rung", "")).strip()
        if rung not in ("greedy", "best-of-k", "tree-search", "fusion"):
            raise HTTPException(400, "rung must be one of: greedy, best-of-k, tree-search, fusion")

        f = await _guard(lambda: store().get_feature(fid))
        if f is None:
            raise HTTPException(404, f"unknown feature {fid!r}")
        if not str(f.get("acceptance_criteria") or "").strip():
            raise HTTPException(400, f"feature {fid!r} has no acceptance_criteria — nothing to verify a rung against")

        from . import coder_seam

        if coder_seam._import_solve() is None:
            raise HTTPException(400, "the `coder` plugin isn't installed/enabled on this host")

        test_cmd = (
            str((cfg or {}).get("coder_solve_test_cmd") or "").strip()
            or str((cfg or {}).get("local_gate_cmd") or "").strip()
        )
        if not test_cmd:
            raise HTTPException(400, "no coder_solve_test_cmd or local_gate_cmd configured — nothing to run tests with")

        # No default coder (v0.42.0): an unset `project_board.coder` is "" — say so
        # instead of probing the roster for a phantom name.
        coder_name = str(body.get("coder") or (cfg or {}).get("coder") or "").strip()
        if not coder_name:
            raise HTTPException(400, "no coder configured — pass `coder` in the body or set project_board.coder")
        coder = coder_seam.resolve_delegate(coder_name, "acp")
        if coder is None:
            raise HTTPException(400, f"acp delegate {coder_name!r} not found — check `delegates:`")

        fusion_max_file_chars = max(
            1, int((cfg or {}).get("coder_solve_fusion_max_file_chars", coder_seam.FUSION_MAX_FILE_CHARS_DEFAULT))
        )
        fusion_delegate = None
        if rung == "fusion":
            fusion_name = str((cfg or {}).get("coder_solve_fusion_delegate") or "").strip()
            if not fusion_name:
                raise HTTPException(400, "rung='fusion' requires project_board.coder_solve_fusion_delegate")
            fusion_delegate = coder_seam.resolve_delegate(fusion_name, "openai")
            if fusion_delegate is None:
                raise HTTPException(400, f"openai delegate {fusion_name!r} not found — check `delegates:`")
            # Same gate `_drive` applies before a real dispatch — fusion can't
            # tool-call and returns whole-file replacements, so this diagnostic
            # must refuse the same oversized files a real build would skip.
            viable, reason = coder_seam.fusion_viable_for_files(
                (cfg or {}).get("repo", "."),
                f.get("files_to_modify") or [],
                max_file_chars=fusion_max_file_chars,
                max_total_chars=max(
                    1,
                    int(
                        (cfg or {}).get("coder_solve_fusion_max_total_chars", coder_seam.FUSION_MAX_TOTAL_CHARS_DEFAULT)
                    ),
                ),
            )
            if not viable:
                raise HTTPException(400, f"rung='fusion' not viable for this feature's files: {reason}")

        task = (
            f"# {f.get('title', '')}\n\n"
            f"## Task\n{f.get('spec', '')}\n\n"
            f"## Files to create / modify\n"
            + ("\n".join(f"- {p}" for p in (f.get("files_to_modify") or [])) or "(none listed)")
            + f"\n\n## Acceptance criteria (definition of done)\n{f.get('acceptance_criteria', '')}\n"
        )

        try:
            result = await coder_seam.test_rung(
                rung=rung,
                task=task,
                coder=coder,
                repo=(cfg or {}).get("repo", "."),
                base=(cfg or {}).get("base_branch", "main"),
                root=(cfg or {}).get("worktrees_root", ".worktrees"),
                fid=fid,
                dispatch_timeout=float((cfg or {}).get("coder_timeout_s", 1800)) or None,
                test_cmd=test_cmd,
                test_timeout=float((cfg or {}).get("coder_solve_test_timeout_s", 300)),
                budget=max(1, int((cfg or {}).get("coder_solve_budget", 6))),
                k=max(1, int((cfg or {}).get("coder_solve_k", 3))),
                tree_depth=max(0, int((cfg or {}).get("coder_solve_tree_depth", 2))),
                fusion_delegate=fusion_delegate,
                fusion_k=max(1, int((cfg or {}).get("coder_solve_fusion_k", 2))),
                files_to_modify=f.get("files_to_modify") or [],
                fusion_max_file_chars=fusion_max_file_chars,
            )
        except Exception as exc:  # noqa: BLE001 — surface as a 400, not a raw 500
            raise HTTPException(400, f"test-rung failed: {exc}") from exc
        return result

    return router
