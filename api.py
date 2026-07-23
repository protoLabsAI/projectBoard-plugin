"""Board HTTP API + console view (D5).

TWO routers (plugin-view rule 2): ``build_router`` carries the public-of-necessity
surface on ``/plugins/project_board`` — GET ``/board`` (an iframe src can't carry a
bearer), POST ``/webhook/pr`` (GitHub signs with HMAC, and its public URL must stay
stable), and POST ``/features/{fid}/ci`` (a CI-infra edge). ``build_data_router``
carries the operator CRUD/transition routes on ``/api/plugins/project_board``, where
they inherit the host's operator bearer gate. The whole flow — create project →
features → Ready gate → (loop dispatches) → in_review → merge webhook → done — is
drivable here, headlessly.

The ``/webhook/pr`` endpoint is the SINGLE external Done edge: a merged-PR event
sets ``done`` and nothing else does (invariant #2). The raw body is HMAC-verified
against ``X-Hub-Signature-256`` whenever a ``webhook_secret`` is configured.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os

from fastapi import Request  # module-level so the webhook's stringized annotation resolves

from .store import BoardError, escalation_enabled, get_store

log = logging.getLogger("protoagent.plugins.project_board")


def build_router(cfg: dict):
    from fastapi import APIRouter, Body, HTTPException
    from fastapi.responses import HTMLResponse

    from .board_view import BOARD_PAGE

    router = APIRouter()

    # ── console view (ADR 0026) — the Kanban/list page the left-rail icon iframes.
    # Served by THIS router (not a second one) so the declared view path
    # /plugins/project_board/board is genuinely mounted; the host dedupes
    # routers by (plugin_id, prefix), so a second router here would be dropped.
    @router.get("/board", response_class=HTMLResponse)
    async def _board():
        return HTMLResponse(BOARD_PAGE)

    store_kw = dict(
        db=(cfg or {}).get("db_path") or None,
        repo=(cfg or {}).get("repo", "."),
        base_branch=(cfg or {}).get("base_branch", "main"),
    )
    escalate_on = escalation_enabled(cfg)
    worktrees_root = (cfg or {}).get("worktrees_root", ".worktrees")
    # GitHub webhook secret (HMAC-SHA256). From config or env; blank ⇒ verification
    # disabled (dev only) — a warning fires per unsigned request.
    webhook_secret = str(
        (cfg or {}).get("webhook_secret") or os.environ.get("PROJECT_BOARD_WEBHOOK_SECRET", "")
    ).strip()

    def store():
        return get_store(**store_kw)

    def _guard(fn):
        try:
            return fn()
        except BoardError as e:
            raise HTTPException(400, str(e))

    # The operator CRUD/transition routes moved to build_data_router — gated under
    # /api/plugins/project_board (plugin-view rule 2). What stays here is the
    # PUBLIC-of-necessity surface: the /board page (an iframe page-load can't
    # carry a bearer) and the CI-infra edges — /webhook/pr (GitHub signs with
    # HMAC, not the operator bearer) and /features/{fid}/ci (posted by CI
    # runners; a CI-infra edge with bounded semantics).

    @router.post("/features/{fid}/ci")
    async def _ci(fid: str, body: dict = Body(...)):
        """CI result for the feature's PR. ``passed: true`` is a no-op (merge sets
        done, via the webhook). ``passed: false``:
          - with an escalation ladder → record + climb a tier and **requeue** to
            ready (the puller re-dispatches at the higher tier, pushing to the same
            PR); when the ladder is exhausted → Blocked.
          - with a single coder → bounce to in_progress for the operator (no auto-
            requeue, so a persistently-failing coder can't loop forever)."""
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

        return _guard(_handle)

    # ── the ONE Done edge: merge webhook ──────────────────────────────────────
    @router.post("/webhook/pr")
    async def _webhook_pr(request: Request):
        """GitHub PR webhook — the SINGLE Done edge. On a ``closed`` event with
        ``merged: true`` it sets the matching feature ``done`` (nothing else does)
        and reaps its worktree. The raw body is HMAC-verified against
        ``X-Hub-Signature-256`` when a secret is configured."""
        raw = await request.body()
        sig = request.headers.get("X-Hub-Signature-256", "")
        if webhook_secret:
            expected = "sha256=" + hmac.new(webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, sig):
                raise HTTPException(401, "invalid webhook signature")
        else:
            log.warning(
                "[project_board] webhook signature NOT verified — set "
                "project_board.webhook_secret (or PROJECT_BOARD_WEBHOOK_SECRET)"
            )
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(400, "invalid JSON body")

        action = body.get("action")
        pr = body.get("pull_request") or {}
        if action != "closed" or not pr.get("merged"):
            return {"ok": True, "ignored": f"action={action} merged={pr.get('merged')}"}
        pr_url = pr.get("html_url") or ""
        f = store().record_merge(pr_url=pr_url)
        if f is None:
            return {"ok": True, "ignored": f"no feature for PR {pr_url}"}
        # Reap the feature's worktree now that it's merged → done (stop accumulation).
        try:
            from . import worktree

            await worktree.reap_feature_worktree(store_kw["repo"], worktrees_root, f["id"])
        except Exception:  # noqa: BLE001 — reaping is best-effort; done is already set
            log.warning("[project_board] worktree reap for %s failed", f["id"], exc_info=True)
        log.info("[project_board] merge webhook → done: %s (%s)", f["id"], pr_url)
        return {"ok": True, "feature": f}

    return router


def build_data_router(cfg: dict):
    """The operator CRUD/transition routes — mounted under
    ``/api/plugins/project_board`` so they inherit the operator bearer gate
    (plugin-view rule 2). Previously these lived under the public ``/plugins/``
    prefix: on a token-gated deployment anyone who could reach the port could
    create/transition features without the bearer."""
    from fastapi import APIRouter, Body, HTTPException

    router = APIRouter()
    store_kw = dict(
        db=(cfg or {}).get("db_path") or None,
        repo=(cfg or {}).get("repo", "."),
        base_branch=(cfg or {}).get("base_branch", "main"),
    )

    def store():
        return get_store(**store_kw)

    def _guard(fn):
        try:
            return fn()
        except BoardError as e:
            raise HTTPException(400, str(e))

    # ── hierarchy (epic → milestone → feature) ────────────────────────────────
    @router.post("/epics")
    async def _create_epic(body: dict = Body(...)):
        return _guard(lambda: store().create_epic(body.get("title", ""), body.get("description", "")))

    @router.post("/milestones")
    async def _create_milestone(body: dict = Body(...)):
        return _guard(
            lambda: store().create_milestone(
                body.get("title", ""), body.get("epic_id", ""), body.get("description", "")
            )
        )

    # ── features ──────────────────────────────────────────────────────────────
    @router.get("/features")
    async def _features(state: str | None = None):
        # _guard, like every other store-touching route: an unusable board (no repo
        # bound, no .beads, br missing) must reach the view as JSON 400 with the
        # actionable BoardError message — an escaped BoardError is a text/plain 500
        # the view can only render as a JSON-parse error.
        return _guard(lambda: {"features": store().list_features(state=state)})

    @router.get("/features/{fid}")
    async def _feature(fid: str):
        f = _guard(lambda: store().get_feature(fid))
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
        f = _guard(lambda: store().get_feature(fid))
        if f is None:
            raise HTTPException(404, f"unknown feature {fid!r}")
        from . import coder_seam

        return coder_seam.progress_snapshot(fid)

    @router.post("/features")
    async def _create_feature(body: dict = Body(...)):
        return _guard(lambda: store().create_feature(**body))

    @router.post("/features/batch")
    async def _create_from_plan(body: dict = Body(default={})):
        """Batch-create a whole decomposition (#92). Body: ``{"plan": [{title, spec,
        acceptance_criteria, files, difficulty, depends_on, foundation}, …],
        "mark_ready": false}``. All-or-report: a malformed item fails itself with a
        named reason, the rest proceed; inter-item ``depends_on`` (by 0-based index or
        title) resolves after every create; ``mark_ready`` promotes only clean items."""
        return _guard(
            lambda: store().create_from_plan(
                (body or {}).get("plan") or [], mark_ready=bool((body or {}).get("mark_ready", False))
            )
        )

    @router.post("/features/{fid}/dep")
    async def _dep(fid: str, body: dict = Body(...)):
        """Add a `blocks` edge: `fid` waits for `depends_on` to be merged→done.
        (Foundation gating is just a blocks-edge on the foundation feature.)"""
        return _guard(
            lambda: (store().add_dependency(fid, str(body.get("depends_on", ""))), store().get_feature(fid))[1]
        )

    # ── transitions ───────────────────────────────────────────────────────────
    @router.post("/features/{fid}/ready")
    async def _ready(fid: str):
        """The Ready gate (invariant #1) — 400 if spec/acceptance_criteria missing."""
        return _guard(lambda: store().mark_ready(fid))

    @router.post("/features/{fid}/block")
    async def _block(fid: str, body: dict = Body(...)):
        return _guard(lambda: store().flag_blocked(fid, str(body.get("reason", ""))))

    @router.post("/features/{fid}/unblock")
    async def _unblock(fid: str):
        return _guard(lambda: store().clear_blocked(fid))

    @router.post("/features/{fid}/cancel")
    async def _cancel(fid: str, body: dict = Body(default={})):
        """Cancel a feature created in error — the second terminal edge (#47). Closes
        the bead with an audit reason and tags it `cancelled` (a distinct state, not
        `done`), so a bad decomposition/duplicate leaves the board cleanly instead of
        being deleted out-of-band (which desyncs the board ↔ JSONL)."""
        return _guard(lambda: store().cancel_feature(fid, str((body or {}).get("reason", ""))))

    @router.delete("/features/{fid}")
    async def _delete(fid: str, body: dict = Body(default={})):
        """Hard-delete a feature created in error — a `br` tombstone (the harder sibling
        of POST …/cancel). Goes through the board so board ↔ JSONL stay consistent;
        refuses (400) if the feature has dependents (deleting would orphan them). Prefer
        cancel to keep a visible, reopenable audit lane; use delete to leave no trace."""
        return _guard(lambda: store().delete_feature(fid, str((body or {}).get("reason", ""))))

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

        f = _guard(lambda: store().get_feature(fid))
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

        coder_name = str(body.get("coder") or (cfg or {}).get("coder", "proto"))
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
