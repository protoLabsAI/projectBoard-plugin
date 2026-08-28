"""API tests — the two-router split, the view-path mount, the webhook Done edge,
and the /ci escalate-vs-bounce branch.

The artifact-plugin lesson, applied here: assert the **actual registered path**.
The board view's #1 regression was the iframe loading a path the router didn't
serve — so these tests mount the routers exactly as ``__init__.register`` does
(``build_router`` at ``/plugins/project_board``, ``build_data_router`` at
``/api/plugins/project_board``) and check the served paths against the manifest.

The store is faked (``api.get_store`` patched) — no ``br``, no DB.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import logging
import sys
import types
from collections import OrderedDict
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import project_board as pb
from project_board import api, coder_seam
from project_board.store import BoardError

ROOT = Path(__file__).resolve().parent.parent


class FakeStore:
    """Records calls; returns minimal feature dicts. ``escalate``/``record_merge``
    returns are configurable so the /ci and /webhook branches can be steered."""

    def __init__(self, *, escalate_to="smart", merged=None):
        self.calls = []
        self._escalate_to = escalate_to
        self._merged = merged

    def _rec(self, name, *a, **k):
        self.calls.append((name, a, k))
        return {"id": a[0] if a else "bd-x", "op": name}

    def list_features(self, state=None, include_archived=False):
        self.calls.append(("list_features", (), {"state": state, "include_archived": include_archived}))
        return [{"id": "bd-1", "title": "T", "board_state": "ready", "priority": 2}]

    def get_feature(self, fid):
        self.calls.append(("get_feature", (fid,), {}))
        return None if fid == "missing" else {"id": fid, "board_state": "ready"}

    def create_epic(self, *a):
        return self._rec("create_epic", *a)

    def create_milestone(self, *a):
        return self._rec("create_milestone", *a)

    def create_feature(self, **k):
        self.calls.append(("create_feature", (), k))
        # Echo `project` back as the real store's projection does (#90) — "" when the
        # body named none (the store then stamps its own default_project).
        return {
            "id": "bd-new",
            "board_state": "backlog",
            "title": k.get("title", ""),
            "project": k.get("project", ""),
        }

    def create_from_plan(self, plan, mark_ready=False):
        self.calls.append(("create_from_plan", (), {"plan": plan, "mark_ready": mark_ready}))
        ids = [f"bd-{i}" for i in range(len(plan))]
        return {
            "items": [{"index": i, "created": True, "id": fid} for i, fid in enumerate(ids)],
            "created_ids": ids,
            "summary": {"requested": len(plan), "created": len(plan), "failed": 0, "ready": 0, "warnings": 0},
        }

    def add_dependency(self, fid, dep):
        return self._rec("add_dependency", fid, dep)

    def mark_ready(self, fid):
        if fid == "bad":
            raise BoardError("Ready gate: missing spec")
        return self._rec("mark_ready", fid)

    def flag_blocked(self, fid, reason):
        return self._rec("flag_blocked", fid, reason)

    def clear_blocked(self, fid):
        return self._rec("clear_blocked", fid)

    def cancel_feature(self, fid, reason=""):
        return self._rec("cancel_feature", fid, reason)

    def mark_done(self, fid, *, reason=""):
        return self._rec("mark_done", fid, reason=reason)

    def delete_feature(self, fid, reason=""):
        return self._rec("delete_feature", fid, reason)

    def bounce_ci_fail(self, fid, reason):
        return self._rec("bounce_ci_fail", fid, reason)

    def record_review_bounce(self, fid, findings=""):
        return self._rec("record_review_bounce", fid, findings)

    def record_delivery(self, fid, text="", ref=""):
        # Echo the real store's in_progress → in_review transition (#217) so the
        # route test can assert the projection is returned verbatim, not just the call.
        self.calls.append(("record_delivery", (fid,), {"text": text, "ref": ref}))
        return {"id": fid, "issue_type": "task", "board_state": "in_review", "deliverable": text, "external_ref": ref}

    def record_verification(self, fid, approved=True, feedback=""):
        # approve → done (closed); reject → requeued to ready — the real store's edges (#217).
        self.calls.append(("record_verification", (fid,), {"approved": approved, "feedback": feedback}))
        return {"id": fid, "issue_type": "task", "board_state": "done" if approved else "ready"}

    def escalate(self, fid, reason):
        self.calls.append(("escalate", (fid, reason), {}))
        return self._escalate_to

    def requeue(self, fid):
        return self._rec("requeue", fid)

    def block_from_review(self, fid, reason):
        return self._rec("block_from_review", fid, reason)

    def update_feature(self, fid, **kwargs):
        self.calls.append(("update_feature", (fid,), kwargs))
        return {"id": fid, "board_state": "backlog", **kwargs}

    def _comment(self, fid, text):
        self.calls.append(("_comment", (fid, text), {}))

    def record_merge(self, *, pr_url):
        self.calls.append(("record_merge", (), {"pr_url": pr_url}))
        return self._merged


def _client(monkeypatch, store, *, cfg=None):
    """Mount both routers as register() does, with ``get_store`` → ``store``."""
    cfg = cfg or {}
    monkeypatch.setattr(api, "get_store", lambda **_kw: store)
    app = FastAPI()
    app.include_router(api.build_router(cfg), prefix="/plugins/project_board")
    app.include_router(api.build_data_router(cfg), prefix="/api/plugins/project_board")
    return TestClient(app)


def _stub_reap(monkeypatch):
    """No-op the terminal-edge worktree reap (it shells out to git) and return the list
    it records ``(repo, root, fid)`` into, so a test can assert it fired with the router's
    configured repo/worktrees_root and the feature id."""
    reaped = []

    async def _reap(repo, root, fid):
        reaped.append((repo, root, fid))

    monkeypatch.setattr("project_board.worktree.reap_feature_worktree", _reap)
    return reaped


# ── the route split + the view-path mount (the regression guard) ────────────────


def test_board_view_is_served_on_the_declared_public_path(monkeypatch):
    c = _client(monkeypatch, FakeStore())
    # The PAGE is public (an iframe src can't carry a bearer)…
    r = c.get("/plugins/project_board/board")
    assert r.status_code == 200 and "<!doctype html>" in r.text.lower()
    # …and it is NOT under /api (where the kit's base-derivation would break).
    assert c.get("/api/plugins/project_board/board").status_code == 404


def test_projects_config_page_is_public_but_its_data_routes_are_not(monkeypatch):
    c = _client(monkeypatch, FakeStore())
    page = c.get("/plugins/project_board/config/projects")
    assert page.status_code == 200 and "Boarded projects" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert c.get("/plugins/project_board/projects").status_code == 404
    assert c.put("/plugins/project_board/projects/demo", json={"repo": "/repo"}).status_code == 404


def test_projects_data_route_reads_live_registry_not_router_config(monkeypatch):
    import project_board.project_registry as registry

    monkeypatch.setattr(
        registry,
        "project_registry_snapshot",
        lambda: {"projects": [{"name": "live"}], "default_project": "live", "onboarding": {}},
    )
    c = _client(monkeypatch, FakeStore(), cfg={"projects": {"stale": {"repo": "/old"}}})
    response = c.get("/api/plugins/project_board/projects")
    assert response.json()["projects"] == [{"name": "live"}]
    assert response.headers["cache-control"] == "no-store"


def test_projects_data_route_surfaces_live_config_read_failure(monkeypatch):
    import project_board.project_registry as registry

    monkeypatch.setattr(
        registry,
        "project_registry_snapshot",
        lambda: (_ for _ in ()).throw(registry.ProjectRegistryError("live config unavailable")),
    )
    response = _client(monkeypatch, FakeStore()).get("/api/plugins/project_board/projects")

    assert response.status_code == 503
    assert response.json()["detail"] == "live config unavailable"


def test_existing_data_router_constructs_the_first_store_with_live_project_routing(monkeypatch):
    live_cfg = types.SimpleNamespace(
        plugin_config={
            "project_board": {
                "projects": {"new": {"repo": "/new", "base_branch": "develop"}},
                "default_project": "new",
            }
        }
    )
    fake_sdk = types.ModuleType("graph.sdk")
    fake_sdk.config = lambda: live_cfg
    monkeypatch.setitem(sys.modules, "graph.sdk", fake_sdk)
    seen = []
    board = FakeStore()
    board.reconfigure_projects = lambda projects, default: seen.append(("reconfigure", projects, default))

    def get_store(**kwargs):
        seen.append(("construct", kwargs))
        return board

    monkeypatch.setattr(api, "get_store", get_store)
    app = FastAPI()
    app.include_router(
        api.build_data_router({"projects": {"old": {"repo": "/old"}}, "default_project": "old"}),
        prefix="/api/plugins/project_board",
    )

    response = TestClient(app).get("/api/plugins/project_board/features")

    assert response.status_code == 200
    constructed = next(event[1] for event in seen if event[0] == "construct")
    assert constructed["projects"] == {"new": {"name": "new", "repo": "/new", "base_branch": "develop"}}
    assert constructed["default_project"] == "new"
    assert ("reconfigure", constructed["projects"], "new") in seen


def test_project_put_uses_shared_registry_mutation(monkeypatch):
    import project_board.project_registry as registry

    seen = {}

    async def save(name, repo, **kwargs):
        seen.update(name=name, repo=repo, **kwargs)
        return {"project": name, "entry": {"repo": repo}, "created": True, "default_project": name}

    monkeypatch.setattr(registry, "upsert_project", save)
    c = _client(monkeypatch, FakeStore())
    response = c.put(
        "/api/plugins/project_board/projects/demo",
        json={"repo": "/dev/demo", "base_branch": "trunk", "default_action": "set"},
    )
    assert response.status_code == 200 and response.json()["ok"] is True
    assert seen == {
        "name": "demo",
        "repo": "/dev/demo",
        "base_branch": "trunk",
        "local_gate_cmd": "",
        "repo_conventions": "",
        "make_default": True,
        "clear_default": False,
        "replace_optional": True,
    }


def test_project_put_rejects_unknown_fields_and_non_booleanish_default_actions(monkeypatch):
    c = _client(monkeypatch, FakeStore())
    assert (
        c.put("/api/plugins/project_board/projects/demo", json={"repo": "/dev/demo", "surprise": True}).status_code
        == 422
    )
    assert (
        c.put(
            "/api/plugins/project_board/projects/demo",
            json={"repo": "/dev/demo", "default_action": "false"},
        ).status_code
        == 422
    )


def test_project_delete_refuses_an_active_card_before_config_mutation(monkeypatch):
    import project_board.project_registry as registry

    store = FakeStore()
    store.list_features = lambda state=None, include_archived=False: [
        {"id": "bd-live", "project": "demo", "board_state": "in_progress"},
        {"id": "bd-old", "project": "demo", "board_state": "done"},
    ]

    async def guarded_delete(name, *, assert_unused):
        await assert_unused(name, "")
        raise AssertionError("config mutation must not run after the active-card check refuses")

    monkeypatch.setattr(registry, "delete_project", guarded_delete)
    c = _client(monkeypatch, store)
    response = c.delete("/api/plugins/project_board/projects/demo")
    assert response.status_code == 409
    assert "bd-live" in response.json()["detail"] and "bd-old" not in response.json()["detail"]


def test_project_delete_refuses_an_unlabelled_active_card_using_the_effective_default(monkeypatch):
    import project_board.project_registry as registry

    store = FakeStore()
    store.list_features = lambda state=None, include_archived=False: [
        {"id": "bd-legacy", "project": "", "board_state": "ready"},
    ]

    async def guarded_delete(name, *, assert_unused):
        await assert_unused(name, "demo")
        raise AssertionError("config mutation must not run after the active-card check refuses")

    monkeypatch.setattr(registry, "delete_project", guarded_delete)
    response = _client(monkeypatch, store).delete("/api/plugins/project_board/projects/demo")

    assert response.status_code == 409
    assert "bd-legacy" in response.json()["detail"]


def test_manifest_view_path_matches_the_served_route():
    import yaml

    m = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    path = m["views"][0]["path"]
    assert path == "/plugins/project_board/board"  # public, not /api/plugins/…
    assert path.split("/plugins/")[0] == ""  # base derives to "" on the host


def test_data_routes_live_on_the_gated_prefix(monkeypatch):
    c = _client(monkeypatch, FakeStore())
    # CRUD is under /api (operator-bearer gated by the host)…
    assert c.get("/api/plugins/project_board/features").json() == {
        "features": [{"id": "bd-1", "title": "T", "board_state": "ready", "priority": 2}]
    }
    # …and NOT on the public prefix (that would skip the bearer gate).
    assert c.get("/plugins/project_board/features").status_code == 404


def test_unusable_board_reads_surface_as_json_400_not_500(monkeypatch):
    """An unusable board (no repo bound, no .beads, br missing) raises BoardError
    on ANY read — that must reach the view as JSON 400 carrying the actionable
    message, not escape as a text/plain 500 the page can only show as a
    JSON-parse error."""

    class BrokenStore(FakeStore):
        def list_features(self, state=None, include_archived=False):
            raise BoardError("repo '.' has no beads workspace — set project_board.repo")

        def get_feature(self, fid):
            raise BoardError("repo '.' has no beads workspace — set project_board.repo")

    c = _client(monkeypatch, BrokenStore())
    for path in ("/api/plugins/project_board/features", "/api/plugins/project_board/features/bd-1"):
        r = c.get(path)
        assert r.status_code == 400, path
        assert "beads workspace" in r.json()["detail"], path


def test_status_reports_unbound_for_the_shipped_default(monkeypatch):
    """First-run tell for the view: the shipped default (repo ".", no db_path, no
    projects map) only works when the process cwd IS the target repo — on a GUI/
    desktop member every read raises BoardError. /status is a pure config read so
    it answers even then, letting the view render setup guidance instead of a raw
    error. It must live on the GATED prefix like every other data route."""
    c = _client(monkeypatch, FakeStore())

    r = c.get("/api/plugins/project_board/status")

    assert r.status_code == 200
    assert r.json()["bound"] is False
    assert c.get("/plugins/project_board/status").status_code == 404  # never public


def test_status_reports_bound_once_a_repo_or_db_is_configured(monkeypatch):
    """Any of the three binding paths — an explicit repo, a db_path pin, or a
    `projects:` map — flips bound; the view then shows real errors, not setup."""
    for cfg in (
        {"repo": "/work/checkout"},
        {"db_path": "/tmp/board.db"},
        {"projects": {"web": {"repo": "/work/web"}}},
    ):
        c = _client(monkeypatch, FakeStore(), cfg=cfg)
        assert c.get("/api/plugins/project_board/status").json()["bound"] is True, cfg


# ── CRUD + the Ready gate surfacing as 400 ──────────────────────────────────────


def test_create_feature_splats_the_body(monkeypatch):
    store = FakeStore()
    c = _client(monkeypatch, store)
    r = c.post("/api/plugins/project_board/features", json={"title": "Add X", "spec": "do X"})
    assert r.status_code == 200 and r.json()["id"] == "bd-new"
    call = next(c for c in store.calls if c[0] == "create_feature")
    assert call[2] == {"title": "Add X", "spec": "do X"}


def test_unknown_feature_is_404(monkeypatch):
    c = _client(monkeypatch, FakeStore())
    assert c.get("/api/plugins/project_board/features/missing").status_code == 404


def test_features_route_defaults_to_live_and_forwards_include_archived(monkeypatch):
    """GET /features is the LIVE board by default (archived excluded in the store,
    #115); ?include_archived=true opts the caller into the full history."""
    store = FakeStore()
    c = _client(monkeypatch, store)
    assert c.get("/api/plugins/project_board/features").status_code == 200
    assert ("list_features", (), {"state": None, "include_archived": False}) in store.calls
    assert c.get("/api/plugins/project_board/features?include_archived=true").status_code == 200
    assert ("list_features", (), {"state": None, "include_archived": True}) in store.calls


# ── batch create from a structured decomposition (#92): POST /features/batch ────


def test_batch_route_forwards_plan_and_mark_ready(monkeypatch):
    store = FakeStore()
    c = _client(monkeypatch, store)
    plan = [{"title": "A", "spec": "sa"}, {"title": "B", "spec": "sb"}]
    r = c.post("/api/plugins/project_board/features/batch", json={"plan": plan, "mark_ready": True})
    assert r.status_code == 200
    body = r.json()
    assert body["created_ids"] == ["bd-0", "bd-1"]
    assert body["summary"]["requested"] == 2
    call = next(c for c in store.calls if c[0] == "create_from_plan")
    assert call[2] == {"plan": plan, "mark_ready": True}


def test_batch_route_defaults_empty_plan_and_is_operator_gated(monkeypatch):
    store = FakeStore()
    c = _client(monkeypatch, store)
    # no body → empty plan, mark_ready False (a valid request, not a 422)
    r = c.post("/api/plugins/project_board/features/batch")
    assert r.status_code == 200
    call = next(c for c in store.calls if c[0] == "create_from_plan")
    assert call[2] == {"plan": [], "mark_ready": False}
    # NOT served on the public prefix (that would skip the operator bearer gate)
    assert c.post("/plugins/project_board/features/batch", json={"plan": []}).status_code == 404


def test_batch_route_surfaces_a_boarderror_as_400(monkeypatch):
    class BrokenStore(FakeStore):
        def create_from_plan(self, plan, mark_ready=False):
            raise BoardError("plan must be a list of feature sections")

    c = _client(monkeypatch, BrokenStore())
    r = c.post("/api/plugins/project_board/features/batch", json={"plan": "not a list"})
    assert r.status_code == 400 and "plan must be a list" in r.json()["detail"]


# ── live coder-monitoring snapshot (#84): GET /features/{fid}/progress ───────────


def test_progress_404s_on_an_unknown_feature(monkeypatch):
    c = _client(monkeypatch, FakeStore())
    assert c.get("/api/plugins/project_board/features/missing/progress").status_code == 404


def test_progress_is_empty_but_valid_when_no_live_run(monkeypatch):
    coder_seam._progress.clear()
    c = _client(monkeypatch, FakeStore())  # bd-1 is a known feature with no live run
    r = c.get("/api/plugins/project_board/features/bd-1/progress")
    assert r.status_code == 200
    assert r.json() == {"gens": []}


def test_progress_returns_the_per_gen_snapshot_contract(monkeypatch):
    """The endpoint contract: {"gens": [{gen, tier, elapsed_s, current_tool,
    recent_tools, thought_tail, usage}]} — fed straight from the in-memory buffer."""
    coder_seam._progress.clear()
    coder_seam.progress_begin("bd-1", 1, "fast")
    coder_seam.progress_tool(
        "bd-1", 1, {"phase": "start", "id": "t1", "name": "edit_file", "input": '{"path": "a.py"}'}
    )
    coder_seam.progress_thought("bd-1", 1, "planning the change")
    coder_seam.progress_usage("bd-1", 1, {"used": 12, "size": 100})
    c = _client(monkeypatch, FakeStore())
    r = c.get("/api/plugins/project_board/features/bd-1/progress")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"gens"} and len(body["gens"]) == 1
    g = body["gens"][0]
    assert {"gen", "tier", "elapsed_s", "current_tool", "recent_tools", "thought_tail", "usage"} <= set(g)
    assert g["gen"] == 1 and g["tier"] == "fast"
    assert g["current_tool"]["name"] == "edit_file" and g["current_tool"]["locations"] == ["a.py"]
    assert g["thought_tail"] == "planning the change"
    assert g["usage"] == {"used": 12, "size": 100}


# ── reload-stability (#178): the monitor buffer survives a plugin/graph reload ──


def _reimport_coder_seam():
    """Import a FRESH coder_seam module instance — exactly what reload_plugins/a
    graph reload produce: a new module object whose body re-runs, while the old
    instance (and anything still holding it, like the live dispatch loop's own
    import-time ``from . import coder_seam``) keeps its reference. Callers must
    restore via ``_restore_coder_seam`` so the rest of the suite keeps the
    original instance."""
    sys.modules.pop("project_board.coder_seam", None)
    return importlib.import_module("project_board.coder_seam")


def _restore_coder_seam():
    sys.modules["project_board.coder_seam"] = coder_seam
    pb.coder_seam = coder_seam


def test_progress_survives_a_plugin_reload(monkeypatch, caplog):
    """#178 r1: the gen that was streaming before a reload is still served by the
    freshly-mounted route — never ``{"gens": []}`` for a live run — and the OLD
    instance's still-running loop keeps feeding the SAME buffer the new instance
    reads (adoption shares the dict, it doesn't copy a snapshot)."""
    coder_seam._progress.clear()
    coder_seam.progress_begin("bd-1", 1, "smart")  # the pre-reload live gen
    coder_seam.progress_thought("bd-1", 1, "mid-stream")
    try:
        with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
            fresh = _reimport_coder_seam()
        # The re-mounted route resolves the FRESH module (`from . import coder_seam`)…
        c = _client(monkeypatch, FakeStore())
        r = c.get("/api/plugins/project_board/features/bd-1/progress")
        assert r.status_code == 200
        gens = r.json()["gens"]
        assert [g["gen"] for g in gens] == [1]  # NOT {"gens": []} — the reload bug
        assert gens[0]["tier"] == "smart"
        assert gens[0]["thought_tail"] == "mid-stream"
        # …while the OLD instance (the still-running dispatch loop) writes into the
        # SAME dict the new instance serves — a later delta stays visible:
        coder_seam.progress_thought("bd-1", 1, " and still going")
        assert fresh.progress_snapshot("bd-1")["gens"][0]["thought_tail"].endswith("and still going")
        # #178 r3: the adoption logged how much was carried over, for the splunk pass.
        assert "1 feature(s) carried over (1 with a live gen)" in caplog.text
    finally:
        _restore_coder_seam()
        coder_seam._progress.clear()


def test_progress_buffer_is_fresh_on_a_clean_boot(caplog):
    """#178 r2: no previous instance (the stable slot is empty — a real fresh boot)
    → an empty buffer and no adoption warning, exactly as before the fix."""
    slot = coder_seam._progress_slot_name()
    saved_holder = sys.modules.pop(slot, None)
    try:
        with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
            fresh = _reimport_coder_seam()
        assert not fresh._progress  # empty…
        assert fresh._progress is not coder_seam._progress  # …and genuinely new
        assert fresh.progress_snapshot("bd-1") == {"gens": []}
        assert "carried over" not in caplog.text
    finally:
        _restore_coder_seam()
        if saved_holder is not None:
            sys.modules[slot] = saved_holder
        else:
            sys.modules.pop(slot, None)


def test_ensure_progress_attached_reports_and_repairs(monkeypatch):
    """#178 r4: the register()-time hook — idempotent on the common path (already
    attached at import; same shared object, reports the carried count) and, if the
    slot was somehow replaced after import, re-adopts it rather than keeping two
    live dicts (the very split this fix removes)."""
    coder_seam._progress.clear()
    coder_seam.progress_begin("bd-1", 1, "fast")
    before = coder_seam._progress
    assert coder_seam.ensure_progress_attached() == 1
    assert coder_seam._progress is before
    slot = coder_seam._progress_slot_name()
    saved_holder = sys.modules[slot]
    replacement = types.ModuleType(slot)
    replacement.progress = OrderedDict()
    sys.modules[slot] = replacement
    try:
        coder_seam.ensure_progress_attached()
        assert coder_seam._progress is replacement.progress
    finally:
        sys.modules[slot] = saved_holder
        coder_seam.ensure_progress_attached()  # re-adopt the original for the rest of the suite
        coder_seam._progress.clear()


def test_register_runs_the_reload_adoption_hook(monkeypatch):
    """register() (every plugin (re)mount) goes through the adoption hook, so a
    reload adopts at mount time — not lazily at the first progress poll after it."""
    called = []
    monkeypatch.setattr(coder_seam, "ensure_progress_attached", lambda: called.append(True) or 0)

    class _Registry:
        config = {"coder": "proto"}

        def register_tool(self, t):
            pass

        def register_router(self, router, prefix):
            pass

        def register_surface(self, start, stop=None, name=None, reload=None):
            pass

        def register_subagent(self, config):
            pass

        def register_skill_dir(self, path):
            pass

    pb.register(_Registry())
    assert called


def test_ready_gate_rejection_surfaces_as_400(monkeypatch):
    c = _client(monkeypatch, FakeStore())
    r = c.post("/api/plugins/project_board/features/bad/ready")
    assert r.status_code == 400 and "Ready gate" in r.json()["detail"]


def test_cancel_route_calls_cancel_feature_with_reason(monkeypatch):
    """POST /features/{fid}/cancel — the second terminal edge (#47). Carries the
    optional reason through; works with no body too."""
    _stub_reap(monkeypatch)  # the terminal edge now reaps; keep it hermetic (no git)
    store = FakeStore()
    c = _client(monkeypatch, store)
    r = c.post("/api/plugins/project_board/features/bd-7/cancel", json={"reason": "duplicate"})
    assert r.status_code == 200
    assert ("cancel_feature", ("bd-7", "duplicate"), {}) in store.calls
    # No body → cancels with an empty reason (still a valid request, not a 422).
    r2 = c.post("/api/plugins/project_board/features/bd-8/cancel")
    assert r2.status_code == 200
    assert ("cancel_feature", ("bd-8", ""), {}) in store.calls


def test_done_route_calls_mark_done_with_reason(monkeypatch):
    """POST /features/{fid}/done — the manual Done edge (#228). Carries the optional
    reason through as a keyword (mark_done's signature) and works with no body too."""
    _stub_reap(monkeypatch)  # the terminal edge reaps; keep it hermetic (no git)
    store = FakeStore()
    c = _client(monkeypatch, store)
    r = c.post("/api/plugins/project_board/features/bd-7/done", json={"reason": "shipped off-board"})
    assert r.status_code == 200
    assert ("mark_done", ("bd-7",), {"reason": "shipped off-board"}) in store.calls
    # No body → an empty reason (still a valid request, not a 422).
    r2 = c.post("/api/plugins/project_board/features/bd-8/done")
    assert r2.status_code == 200
    assert ("mark_done", ("bd-8",), {"reason": ""}) in store.calls


def test_done_route_reaps_the_worktree_at_the_terminal_edge(monkeypatch):
    """#109: a hand-done feature is terminal (nothing left to build), so the route reaps
    its worktree right after mark_done() succeeds — same pattern as cancel/merge."""
    reaped = _stub_reap(monkeypatch)
    store = FakeStore()
    c = _client(monkeypatch, store, cfg={"repo": "/repo", "worktrees_root": ".wt"})
    r = c.post("/api/plugins/project_board/features/bd-7/done", json={"reason": "done"})
    assert r.status_code == 200
    assert ("mark_done", ("bd-7",), {"reason": "done"}) in store.calls  # mark_done runs first…
    assert reaped == [("/repo", ".wt", "bd-7")]  # …then the reap fires


def test_done_route_surfaces_an_invalid_state_as_400(monkeypatch):
    """mark_done rejects a not-in-flight feature with a BoardError; the route must
    surface it as a JSON 400 (the shared _guard), not a 500."""

    class RejectingStore(FakeStore):
        def mark_done(self, fid, *, reason=""):
            raise BoardError("mark_done accepts in_progress/in_review/blocked, got 'backlog'")

    _stub_reap(monkeypatch)
    c = _client(monkeypatch, RejectingStore())
    r = c.post("/api/plugins/project_board/features/bd-9/done", json={"reason": "x"})
    assert r.status_code == 400 and "mark_done accepts" in r.json()["detail"]


def test_delete_route_calls_delete_feature(monkeypatch):
    """DELETE /features/{fid} — the hard-delete sibling of cancel (#47). Carries an
    optional reason; works with no body too."""
    _stub_reap(monkeypatch)  # the terminal edge now reaps; keep it hermetic (no git)
    store = FakeStore()
    c = _client(monkeypatch, store)
    r = c.request("DELETE", "/api/plugins/project_board/features/bd-7", json={"reason": "mistake"})
    assert r.status_code == 200
    assert ("delete_feature", ("bd-7", "mistake"), {}) in store.calls
    r2 = c.delete("/api/plugins/project_board/features/bd-8")
    assert r2.status_code == 200
    assert ("delete_feature", ("bd-8", ""), {}) in store.calls


def test_cancel_route_reaps_the_worktree_at_the_terminal_edge(monkeypatch):
    """#109: cancel is terminal (nothing left to build), so it reaps the feature's
    worktree right after cancel_feature() succeeds — same pattern as the merge webhook
    — instead of leaking it until the health sweep. The reap gets the router's configured
    repo + worktrees_root and the feature id."""
    reaped = _stub_reap(monkeypatch)
    store = FakeStore()
    c = _client(monkeypatch, store, cfg={"repo": "/repo", "worktrees_root": ".wt"})
    r = c.post("/api/plugins/project_board/features/bd-7/cancel", json={"reason": "dup"})
    assert r.status_code == 200
    assert ("cancel_feature", ("bd-7", "dup"), {}) in store.calls  # cancel runs first…
    assert reaped == [("/repo", ".wt", "bd-7")]  # …then the reap fires


def test_delete_route_reaps_the_worktree_at_the_terminal_edge(monkeypatch):
    """#109: delete is the same terminal class as cancel/merge — a deleted feature leaves
    nothing to build — so its worktree is reaped on the way out too."""
    reaped = _stub_reap(monkeypatch)
    store = FakeStore()
    c = _client(monkeypatch, store, cfg={"repo": "/repo", "worktrees_root": ".wt"})
    r = c.request("DELETE", "/api/plugins/project_board/features/bd-7", json={"reason": "oops"})
    assert r.status_code == 200
    assert ("delete_feature", ("bd-7", "oops"), {}) in store.calls
    assert reaped == [("/repo", ".wt", "bd-7")]


def test_terminal_edge_reap_failure_does_not_fail_the_response(monkeypatch):
    """The reap is best-effort — the bead is already cancelled/deleted, so a git or
    worktree blow-up must not turn a successful terminal transition into a 500 (#109).
    The health sweep is the backstop for whatever the edge reap missed."""

    async def _boom(*_a, **_k):
        raise RuntimeError("git worktree remove exploded")

    monkeypatch.setattr("project_board.worktree.reap_feature_worktree", _boom)
    store = FakeStore()
    c = _client(monkeypatch, store)
    r = c.post("/api/plugins/project_board/features/bd-7/cancel", json={"reason": "dup"})
    assert r.status_code == 200
    assert ("cancel_feature", ("bd-7", "dup"), {}) in store.calls
    r2 = c.request("DELETE", "/api/plugins/project_board/features/bd-8", json={"reason": "oops"})
    assert r2.status_code == 200
    assert ("delete_feature", ("bd-8", "oops"), {}) in store.calls


# ── the single Done edge: the merge webhook ─────────────────────────────────────


def _signed(secret, raw):
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


_EXTERNAL_SECRET = "test-external-secret"


def _external_cfg(cfg=None):
    return {**(cfg or {}), "webhook_secret": _EXTERNAL_SECRET}


def _external_post(client, path, body, *, secret=_EXTERNAL_SECRET):
    raw = json.dumps(body, separators=(",", ":")).encode()
    return client.post(path, content=raw, headers={"X-Hub-Signature-256": _signed(secret, raw)})


def _merge_body(url="https://example/pr/1"):
    return json.dumps({"action": "closed", "pull_request": {"merged": True, "html_url": url}}).encode()


def test_webhook_rejects_a_bad_signature(monkeypatch):
    c = _client(monkeypatch, FakeStore(), cfg={"webhook_secret": "s3cret"})
    raw = _merge_body()
    r = c.post(
        "/plugins/project_board/webhook/pr",
        content=raw,
        headers={"X-Hub-Signature-256": "sha256=deadbeef"},
    )
    assert r.status_code == 401


def test_webhook_accepts_a_valid_signature_and_sets_done(monkeypatch):
    # Reaping the worktree shells out to git — stub it (best-effort path anyway).
    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr("project_board.worktree.remove_worktree", _noop)
    store = FakeStore(merged={"id": "bd-9", "board_state": "done"})
    c = _client(monkeypatch, store, cfg={"webhook_secret": "s3cret"})
    raw = _merge_body()
    r = c.post(
        "/plugins/project_board/webhook/pr",
        content=raw,
        headers={"X-Hub-Signature-256": _signed("s3cret", raw)},
    )
    assert r.status_code == 200 and r.json()["feature"]["id"] == "bd-9"
    assert ("record_merge", (), {"pr_url": "https://example/pr/1"}) in store.calls


def test_webhook_ignores_a_non_merge_event(monkeypatch):
    store = FakeStore()
    c = _client(monkeypatch, store, cfg={"webhook_secret": "s3cret"})
    raw = json.dumps({"action": "opened", "pull_request": {"merged": False}}).encode()
    r = c.post(
        "/plugins/project_board/webhook/pr",
        content=raw,
        headers={"X-Hub-Signature-256": _signed("s3cret", raw)},
    )
    assert r.status_code == 200 and "ignored" in r.json()
    assert not any(call[0] == "record_merge" for call in store.calls)


def test_webhook_without_a_secret_fails_closed(monkeypatch):
    store = FakeStore(merged=None)
    c = _client(monkeypatch, store, cfg={"webhook_secret": ""})
    r = c.post("/plugins/project_board/webhook/pr", content=_merge_body())
    assert r.status_code == 503
    assert not store.calls


def test_public_ci_and_review_reject_unsigned_or_bad_signatures_before_store_access(monkeypatch):
    store = FakeStore()
    c = _client(monkeypatch, store, cfg=_external_cfg())
    cases = (
        ("/plugins/project_board/features/bd-1/ci", {"passed": False}),
        ("/plugins/project_board/features/bd-1/review", {"findings": "block it"}),
    )
    for path, body in cases:
        assert c.post(path, json=body).status_code == 401
        assert _external_post(c, path, body, secret="wrong-secret").status_code == 401
    assert not store.calls


def test_public_ci_and_review_fail_closed_without_a_secret(monkeypatch):
    store = FakeStore()
    c = _client(monkeypatch, store, cfg={"webhook_secret": ""})
    assert c.post("/plugins/project_board/features/bd-1/ci", json={"passed": False}).status_code == 503
    assert c.post("/plugins/project_board/features/bd-1/review", json={"findings": "x"}).status_code == 503
    assert not store.calls


def test_public_mutation_authenticates_before_json_parsing(monkeypatch):
    store = FakeStore()
    c = _client(monkeypatch, store, cfg=_external_cfg())
    raw = b"{not-json"
    r = c.post(
        "/plugins/project_board/features/bd-1/ci",
        content=raw,
        headers={"X-Hub-Signature-256": _signed(_EXTERNAL_SECRET, raw)},
    )
    assert r.status_code == 400 and r.json()["detail"] == "invalid JSON body"
    assert not store.calls


def test_external_secret_env_fallback_is_used(monkeypatch):
    monkeypatch.setenv("PROJECT_BOARD_WEBHOOK_SECRET", "env-secret")
    store = FakeStore()
    c = _client(monkeypatch, store, cfg={})
    r = _external_post(c, "/plugins/project_board/features/bd-1/ci", {"passed": True}, secret="env-secret")
    assert r.status_code == 200 and r.json()["ok"] is True


# ── /ci: escalate when a ladder exists, else bounce ─────────────────────────────

ESCALATION_CFG = {"coders": {"fast": "proto", "smart": "proto-smart"}}


def test_ci_pass_is_a_noop(monkeypatch):
    c = _client(monkeypatch, FakeStore(), cfg=_external_cfg(ESCALATION_CFG))
    r = _external_post(c, "/plugins/project_board/features/bd-1/ci", {"passed": True})
    assert r.json()["ok"] is True


def test_ci_fail_with_a_ladder_escalates_and_requeues(monkeypatch):
    store = FakeStore(escalate_to="smart")
    c = _client(monkeypatch, store, cfg=_external_cfg(ESCALATION_CFG))
    r = _external_post(c, "/plugins/project_board/features/bd-1/ci", {"passed": False, "reason": "boom"})
    body = r.json()
    assert body["requeued"] is True and body["escalated"] is True and body["next_tier"] == "smart"
    assert any(call[0] == "requeue" for call in store.calls)


def test_ci_fail_at_the_top_of_the_ladder_blocks(monkeypatch):
    store = FakeStore(escalate_to=None)  # ladder exhausted
    c = _client(monkeypatch, store, cfg=_external_cfg(ESCALATION_CFG))
    r = _external_post(c, "/plugins/project_board/features/bd-1/ci", {"passed": False})
    body = r.json()
    assert body["exhausted"] is True and body["requeued"] is False
    assert any(call[0] == "block_from_review" for call in store.calls)


def test_ci_fail_with_a_single_coder_bounces_to_in_progress(monkeypatch):
    store = FakeStore()
    c = _client(monkeypatch, store, cfg=_external_cfg())  # no coders map → no escalation
    r = _external_post(c, "/plugins/project_board/features/bd-1/ci", {"passed": False, "reason": "x"})
    body = r.json()
    assert body["escalated"] is False and body["requeued"] is False
    assert any(call[0] == "bounce_ci_fail" for call in store.calls)


# ── /features/{fid}/review — the adverse-review bounce (bd-171) ─────────────────
# The review sibling of /ci fail: record a distinct review-bounce comment, feed the
# findings into the loop's re-dispatch prompt (the _ci_feedback bridge), and requeue
# onto the SAME open PR. escalate=false keeps the tier; escalate=true climbs.


def test_review_bounce_requeues_and_records_a_distinct_comment(monkeypatch):
    from project_board import loop as loop_mod

    loop_mod._PENDING_FEEDBACK.clear()
    store = FakeStore()
    c = _client(monkeypatch, store, cfg=_external_cfg())  # no ladder → default keeps the same tier
    r = _external_post(
        c,
        "/plugins/project_board/features/bd-1/review",
        {"findings": "auth check missing a null guard"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["requeued"] is True and body["escalated"] is False
    names = [call[0] for call in store.calls]
    assert "record_review_bounce" in names  # a distinct review-bounce comment on the bead
    assert "requeue" in names  # same open PR reused (store.requeue keeps external_ref)
    assert "escalate" not in names  # default keeps the same tier


def test_review_findings_reach_the_loop_feedback_bridge(monkeypatch):
    """AC: the findings text crosses into the loop's re-dispatch path (the same
    _ci_feedback lever the in-loop review gate writes), via queue_review_feedback."""
    from project_board import loop as loop_mod

    loop_mod._PENDING_FEEDBACK.clear()
    c = _client(monkeypatch, FakeStore(), cfg=_external_cfg())
    _external_post(c, "/plugins/project_board/features/bd-1/review", {"findings": "missing a null guard"})
    assert "missing a null guard" in loop_mod._PENDING_FEEDBACK.get("bd-1", "")


def test_review_escalate_true_climbs_the_ladder(monkeypatch):
    store = FakeStore(escalate_to="smart")
    c = _client(monkeypatch, store, cfg=_external_cfg(ESCALATION_CFG))
    r = _external_post(c, "/plugins/project_board/features/bd-1/review", {"findings": "x", "escalate": True})
    body = r.json()
    assert body["escalated"] is True and body["next_tier"] == "smart" and body["requeued"] is True
    assert any(call[0] == "escalate" for call in store.calls)
    assert any(call[0] == "requeue" for call in store.calls)


def test_review_escalate_false_keeps_the_same_tier(monkeypatch):
    store = FakeStore(escalate_to="smart")
    c = _client(monkeypatch, store, cfg=_external_cfg(ESCALATION_CFG))  # a ladder exists…
    r = _external_post(c, "/plugins/project_board/features/bd-1/review", {"findings": "x", "escalate": False})
    body = r.json()
    assert body["escalated"] is False and body["requeued"] is True
    assert not any(call[0] == "escalate" for call in store.calls)  # …but the default doesn't climb it
    assert any(call[0] == "requeue" for call in store.calls)


def test_review_escalate_exhausted_blocks(monkeypatch):
    store = FakeStore(escalate_to=None)  # ladder already at the top
    c = _client(monkeypatch, store, cfg=_external_cfg(ESCALATION_CFG))
    r = _external_post(c, "/plugins/project_board/features/bd-1/review", {"findings": "x", "escalate": True})
    body = r.json()
    assert body["exhausted"] is True and body["requeued"] is False
    assert any(call[0] == "block_from_review" for call in store.calls)


def test_review_is_public_hmac_authenticated_not_operator_gated(monkeypatch):
    c = _client(monkeypatch, FakeStore(), cfg=_external_cfg())
    # served on the public prefix behind its own HMAC boundary…
    assert _external_post(c, "/plugins/project_board/features/bd-1/review", {"findings": "x"}).status_code == 200
    # …and NOT on the gated /api prefix.
    assert c.post("/api/plugins/project_board/features/bd-1/review", json={"findings": "x"}).status_code == 404


def test_review_from_a_non_in_review_state_surfaces_as_400(monkeypatch):
    class WrongState(FakeStore):
        def record_review_bounce(self, fid, findings=""):
            raise BoardError("review bounce expects in_review, got 'in_progress'")

    c = _client(monkeypatch, WrongState(), cfg=_external_cfg())
    r = _external_post(c, "/plugins/project_board/features/bd-1/review", {"findings": "x"})
    assert r.status_code == 400 and "in_review" in r.json()["detail"]


# ── /features/{fid}/deliver + /verify — the task-type review lane (#217 S2) ──────
# The coder-PR-free siblings of open_review → record_merge: deliver moves a task
# in_progress → in_review; verify is the task Done edge (approve closes, reject
# requeues to ready). Both live on build_data_router → the operator bearer gate.


def test_deliver_route_records_the_deliverable_and_returns_in_review(monkeypatch):
    """POST /features/{fid}/deliver forwards text+ref as keywords to record_delivery
    and returns the in_review projection verbatim (#217)."""
    store = FakeStore()
    c = _client(monkeypatch, store)
    r = c.post(
        "/api/plugins/project_board/features/bd-1/deliver",
        json={"text": "shipped the doc", "ref": "https://docs/x"},
    )
    assert r.status_code == 200
    assert ("record_delivery", ("bd-1",), {"text": "shipped the doc", "ref": "https://docs/x"}) in store.calls
    assert r.json()["board_state"] == "in_review"


def test_deliver_route_defaults_missing_body_fields_to_empty(monkeypatch):
    """No body (and a body missing text/ref) → empty strings, not a 422 — the same
    optional-body shape as /done."""
    store = FakeStore()
    c = _client(monkeypatch, store)
    r = c.post("/api/plugins/project_board/features/bd-2/deliver")
    assert r.status_code == 200
    assert ("record_delivery", ("bd-2",), {"text": "", "ref": ""}) in store.calls
    # A partial body (only ref) still defaults the missing field.
    r2 = c.post("/api/plugins/project_board/features/bd-3/deliver", json={"ref": "artifact://y"})
    assert r2.status_code == 200
    assert ("record_delivery", ("bd-3",), {"text": "", "ref": "artifact://y"}) in store.calls


def test_deliver_route_surfaces_an_invalid_state_or_type_as_400(monkeypatch):
    """record_delivery 400s a non-in_progress or non-task feature; the route surfaces
    the BoardError as a JSON 400 via the shared _guard, not a 500."""

    class WrongState(FakeStore):
        def record_delivery(self, fid, text="", ref=""):
            raise BoardError("record_delivery expects in_progress, got 'ready'")

    c = _client(monkeypatch, WrongState())
    r = c.post("/api/plugins/project_board/features/bd-1/deliver", json={"text": "x"})
    assert r.status_code == 400 and "in_progress" in r.json()["detail"]


def test_verify_route_approve_closes_the_task_to_done(monkeypatch):
    """POST /features/{fid}/verify with {approved: true} forwards approved+feedback and
    returns the done (closed) projection (#217)."""
    store = FakeStore()
    c = _client(monkeypatch, store)
    r = c.post("/api/plugins/project_board/features/bd-1/verify", json={"approved": True})
    assert r.status_code == 200
    assert ("record_verification", ("bd-1",), {"approved": True, "feedback": ""}) in store.calls
    assert r.json()["board_state"] == "done"


def test_verify_route_defaults_approved_true_when_body_omits_it(monkeypatch):
    """approved defaults to true — an empty body (or one omitting approved) is an
    approval, not a 422."""
    store = FakeStore()
    c = _client(monkeypatch, store)
    r = c.post("/api/plugins/project_board/features/bd-2/verify")
    assert r.status_code == 200
    assert ("record_verification", ("bd-2",), {"approved": True, "feedback": ""}) in store.calls
    assert r.json()["board_state"] == "done"


def test_verify_route_reject_requeues_to_ready_with_feedback(monkeypatch):
    """{approved: false, feedback: "..."} forwards approved=False + the feedback and
    returns the requeued (ready) projection."""
    store = FakeStore()
    c = _client(monkeypatch, store)
    r = c.post(
        "/api/plugins/project_board/features/bd-1/verify",
        json={"approved": False, "feedback": "missing the summary table"},
    )
    assert r.status_code == 200
    assert (
        "record_verification",
        ("bd-1",),
        {"approved": False, "feedback": "missing the summary table"},
    ) in store.calls
    assert r.json()["board_state"] == "ready"


def test_verify_route_surfaces_an_invalid_state_or_type_as_400(monkeypatch):
    """record_verification 400s a non-in_review or non-task feature; the route surfaces
    the BoardError as a JSON 400 via the shared _guard."""

    class WrongState(FakeStore):
        def record_verification(self, fid, approved=True, feedback=""):
            raise BoardError("record_verification expects in_review, got 'in_progress'")

    c = _client(monkeypatch, WrongState())
    r = c.post("/api/plugins/project_board/features/bd-1/verify", json={"approved": True})
    assert r.status_code == 400 and "in_review" in r.json()["detail"]


def test_deliver_and_verify_are_operator_gated_not_public(monkeypatch):
    """Both live on build_data_router → the operator bearer prefix (#217 r6): served
    under /api/plugins/project_board, NOT on the public build_router prefix (the
    inverse of /review, which is a public review-infra edge)."""
    store = FakeStore()
    c = _client(monkeypatch, store)
    assert c.post("/api/plugins/project_board/features/bd-1/deliver", json={}).status_code == 200
    assert c.post("/api/plugins/project_board/features/bd-1/verify", json={}).status_code == 200
    # …and absent from the public prefix.
    assert c.post("/plugins/project_board/features/bd-1/deliver", json={}).status_code == 404
    assert c.post("/plugins/project_board/features/bd-1/verify", json={}).status_code == 404


# ── /features/{fid}/test-rung — operator-only diagnostic (ADR 0064) ─────────────
# No @tool wrapper anywhere in coder_seam.py/api.py exposes this to the board's
# own lead agent — these tests only exercise the HTTP route directly, mirroring
# how an operator (console/curl) would reach it.


def _feature_with_ac(fid="bd-7", files=None):
    return {
        "id": fid,
        "title": "T",
        "spec": "do the thing",
        "acceptance_criteria": "WHEN x THE SYSTEM SHALL y",
        "files_to_modify": files or ["a.py"],
        "board_state": "ready",
    }


def test_test_rung_rejects_an_unknown_rung_name(monkeypatch):
    store = FakeStore()
    c = _client(monkeypatch, store)
    r = c.post("/api/plugins/project_board/features/bd-7/test-rung", json={"rung": "nonsense"})
    assert r.status_code == 400
    assert "rung must be one of" in r.json()["detail"]


def test_test_rung_404s_on_an_unknown_feature(monkeypatch):
    store = FakeStore()
    c = _client(monkeypatch, store)
    r = c.post("/api/plugins/project_board/features/missing/test-rung", json={"rung": "greedy"})
    assert r.status_code == 404


def test_test_rung_400s_without_acceptance_criteria(monkeypatch):
    store = FakeStore()  # get_feature returns {"id": fid, "board_state": "ready"} — no AC
    c = _client(monkeypatch, store)
    r = c.post("/api/plugins/project_board/features/bd-7/test-rung", json={"rung": "greedy"})
    assert r.status_code == 400
    assert "acceptance_criteria" in r.json()["detail"]


def test_test_rung_400s_when_coder_plugin_unavailable(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(store, "get_feature", lambda fid: _feature_with_ac(fid))
    monkeypatch.setattr(coder_seam, "_import_solve", lambda: None)
    c = _client(monkeypatch, store)
    r = c.post("/api/plugins/project_board/features/bd-7/test-rung", json={"rung": "greedy"})
    assert r.status_code == 400
    assert "coder` plugin" in r.json()["detail"]


def test_test_rung_400s_without_a_test_command(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(store, "get_feature", lambda fid: _feature_with_ac(fid))
    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())
    c = _client(monkeypatch, store, cfg={})  # no coder_solve_test_cmd, no local_gate_cmd
    r = c.post("/api/plugins/project_board/features/bd-7/test-rung", json={"rung": "greedy"})
    assert r.status_code == 400
    assert "test_cmd" in r.json()["detail"] or "gate_cmd" in r.json()["detail"]


def test_test_rung_400s_when_the_coder_delegate_is_missing(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(store, "get_feature", lambda fid: _feature_with_ac(fid))
    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())
    monkeypatch.setattr(coder_seam, "resolve_delegate", lambda name, t: None)
    # An EXPLICIT coder that the roster can't resolve (v0.42.0 dropped the implicit
    # `proto` default this test used to lean on).
    c = _client(monkeypatch, store, cfg={"coder_solve_test_cmd": "pytest -q", "coder": "proto"})
    r = c.post("/api/plugins/project_board/features/bd-7/test-rung", json={"rung": "greedy"})
    assert r.status_code == 400
    assert "acp delegate 'proto'" in r.json()["detail"]


def test_test_rung_400s_when_no_coder_is_configured_at_all(monkeypatch):
    """v0.42.0: there is NO default coder name. An unset `project_board.coder` with no
    `coder` in the body is a named 400 — not a roster probe for a phantom 'proto'."""
    store = FakeStore()
    monkeypatch.setattr(store, "get_feature", lambda fid: _feature_with_ac(fid))
    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())
    probed = []
    monkeypatch.setattr(coder_seam, "resolve_delegate", lambda name, t: probed.append(name))
    c = _client(monkeypatch, store, cfg={"coder_solve_test_cmd": "pytest -q"})
    r = c.post("/api/plugins/project_board/features/bd-7/test-rung", json={"rung": "greedy"})
    assert r.status_code == 400
    assert "no coder configured" in r.json()["detail"]
    assert probed == []  # never asked the roster about "proto"


def test_test_rung_fusion_400s_without_a_configured_fusion_delegate(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(store, "get_feature", lambda fid: _feature_with_ac(fid))
    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())
    monkeypatch.setattr(coder_seam, "resolve_delegate", lambda name, t: object())
    c = _client(monkeypatch, store, cfg={"coder_solve_test_cmd": "pytest -q", "coder": "proto"})  # no fusion delegate
    r = c.post("/api/plugins/project_board/features/bd-7/test-rung", json={"rung": "fusion"})
    assert r.status_code == 400
    assert "coder_solve_fusion_delegate" in r.json()["detail"]


def test_test_rung_fusion_400s_when_files_are_oversized(monkeypatch, tmp_path):
    """Same gate `_drive` applies before a real dispatch: fusion can't tool-call
    and returns whole-file replacements, so an oversized declared file must be
    refused here too, before ever reaching coder_seam.test_rung."""
    (tmp_path / "big.py").write_text("x" * 1000)
    store = FakeStore()
    monkeypatch.setattr(store, "get_feature", lambda fid: _feature_with_ac(fid, files=["big.py"]))
    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())
    monkeypatch.setattr(coder_seam, "resolve_delegate", lambda name, t: object())

    async def _boom(**kwargs):
        raise AssertionError("coder_seam.test_rung must not be reached when fusion isn't viable")

    monkeypatch.setattr(coder_seam, "test_rung", _boom)
    c = _client(
        monkeypatch,
        store,
        cfg={
            "coder_solve_test_cmd": "pytest -q",
            "coder_solve_fusion_delegate": "fusion-model",
            "coder_solve_fusion_max_file_chars": 10,
            "repo": str(tmp_path),
            "coder": "proto",
        },
    )
    r = c.post("/api/plugins/project_board/features/bd-7/test-rung", json={"rung": "fusion"})
    assert r.status_code == 400
    assert "not viable" in r.json()["detail"]
    assert "big.py" in r.json()["detail"]


def test_test_rung_happy_path_calls_coder_seam_test_rung_and_returns_its_result(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(store, "get_feature", lambda fid: _feature_with_ac(fid))
    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())

    resolved = {}

    def _resolve(name, expect_type):
        resolved[expect_type] = name
        return object()

    monkeypatch.setattr(coder_seam, "resolve_delegate", _resolve)

    seen_kwargs = {}

    async def _fake_test_rung(**kwargs):
        seen_kwargs.update(kwargs)
        return {
            "rung": "greedy",
            "passed": True,
            "gens_spent": 1,
            "candidates_tried": 1,
            "note": "ok",
            "verdict_output": "",
        }

    monkeypatch.setattr(coder_seam, "test_rung", _fake_test_rung)

    c = _client(
        monkeypatch,
        store,
        cfg={"coder_solve_test_cmd": "pytest -q", "coder": "proto", "repo": "/repo", "base_branch": "main"},
    )
    r = c.post("/api/plugins/project_board/features/bd-7/test-rung", json={"rung": "greedy"})
    assert r.status_code == 200
    assert r.json() == {
        "rung": "greedy",
        "passed": True,
        "gens_spent": 1,
        "candidates_tried": 1,
        "note": "ok",
        "verdict_output": "",
    }
    assert resolved == {"acp": "proto"}
    assert seen_kwargs["rung"] == "greedy"
    assert seen_kwargs["repo"] == "/repo"
    assert "WHEN x THE SYSTEM SHALL y" in seen_kwargs["task"]
    assert seen_kwargs["files_to_modify"] == ["a.py"]


def test_test_rung_surfaces_a_solve_failure_as_400_not_500(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(store, "get_feature", lambda fid: _feature_with_ac(fid))
    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())
    monkeypatch.setattr(coder_seam, "resolve_delegate", lambda name, t: object())

    async def _boom(**kwargs):
        raise RuntimeError("worktree op failed")

    monkeypatch.setattr(coder_seam, "test_rung", _boom)
    c = _client(monkeypatch, store, cfg={"coder_solve_test_cmd": "pytest -q", "coder": "proto"})
    r = c.post("/api/plugins/project_board/features/bd-7/test-rung", json={"rung": "greedy"})
    assert r.status_code == 400
    assert "test-rung failed" in r.json()["detail"]


# ── PATCH /features/{fid} — in-place spec edit (#148) ──────────────────────────


class InProgressStore(FakeStore):
    """FakeStore variant whose get_feature always returns in_progress state."""

    def get_feature(self, fid):
        self.calls.append(("get_feature", (fid,), {}))
        return None if fid == "missing" else {"id": fid, "board_state": "in_progress"}


def test_patch_feature_updates_spec_and_returns_updated_feature(monkeypatch):
    """PATCH /features/{fid} delegates to update_feature with only non-null fields
    and returns the updated feature dict."""
    store = FakeStore()
    c = _client(monkeypatch, store)
    r = c.patch("/api/plugins/project_board/features/bd-5", json={"spec": "new spec text"})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "bd-5"
    assert body["spec"] == "new spec text"
    call = next(x for x in store.calls if x[0] == "update_feature")
    assert call[1] == ("bd-5",) and call[2] == {"spec": "new spec text"}


def test_patch_feature_records_audit_comment_naming_changed_fields(monkeypatch):
    """The route appends a comment on the bead naming every field that changed."""
    store = FakeStore()
    c = _client(monkeypatch, store)
    c.patch(
        "/api/plugins/project_board/features/bd-5",
        json={"spec": "s", "acceptance_criteria": "ac"},
    )
    comment_calls = [x for x in store.calls if x[0] == "_comment"]
    assert len(comment_calls) == 1
    text = comment_calls[0][1][1]
    assert "spec updated:" in text
    assert "acceptance_criteria" in text and "spec" in text


def test_patch_feature_in_progress_without_force_is_400(monkeypatch):
    """Editing an in_progress feature without force=true is refused with 400."""
    store = InProgressStore()
    c = _client(monkeypatch, store)
    r = c.patch("/api/plugins/project_board/features/bd-5", json={"spec": "new"})
    assert r.status_code == 400
    assert "in_progress" in r.json()["detail"]
    assert not any(x[0] == "update_feature" for x in store.calls)


def test_patch_feature_in_progress_with_force_succeeds(monkeypatch):
    """force=true bypasses the in_progress guard and applies the update."""
    store = InProgressStore()
    c = _client(monkeypatch, store)
    r = c.patch("/api/plugins/project_board/features/bd-5", json={"spec": "new", "force": True})
    assert r.status_code == 200
    assert any(x[0] == "update_feature" for x in store.calls)


def test_patch_feature_only_writes_non_null_fields(monkeypatch):
    """Null fields in the body are not forwarded to update_feature."""
    store = FakeStore()
    c = _client(monkeypatch, store)
    r = c.patch(
        "/api/plugins/project_board/features/bd-5",
        json={"spec": "s", "acceptance_criteria": None, "title": None},
    )
    assert r.status_code == 200
    call = next(x for x in store.calls if x[0] == "update_feature")
    assert call[2] == {"spec": "s"}


def test_patch_feature_404_on_unknown_feature(monkeypatch):
    c = _client(monkeypatch, FakeStore())
    r = c.patch("/api/plugins/project_board/features/missing", json={"spec": "x"})
    assert r.status_code == 404


def test_patch_feature_is_operator_gated(monkeypatch):
    """PATCH is under /api (operator-bearer gated) — not on the public prefix."""
    c = _client(monkeypatch, FakeStore())
    assert c.patch("/api/plugins/project_board/features/bd-5", json={"spec": "x"}).status_code == 200
    assert c.patch("/plugins/project_board/features/bd-5", json={"spec": "x"}).status_code == 404


def test_patch_feature_accepts_all_r1_fields(monkeypatch):
    """All fields named in r1 — including priority — are forwarded to update_feature."""
    store = FakeStore()
    c = _client(monkeypatch, store)
    payload = {
        "title": "new title",
        "spec": "new spec",
        "acceptance_criteria": "new ac",
        "design": "new design",
        "files_to_modify": ["a.py", "b.py"],
        "difficulty": "hard",
        "priority": 1,
        "source_issue": "https://github.com/org/repo/issues/99",
    }
    r = c.patch("/api/plugins/project_board/features/bd-5", json=payload)
    assert r.status_code == 200
    call = next(x for x in store.calls if x[0] == "update_feature")
    assert call[1] == ("bd-5",)
    for field in payload:
        assert field in call[2], f"field {field!r} missing from update_feature kwargs"
    assert call[2]["priority"] == 1


# ── #90 slice 3: the `project` param through the data router ─────────────────────


class _ProjectStore(FakeStore):
    """A board holding features across two projects — exercises the ?project filter and
    the detail-includes-project echo without a real ``br``."""

    _FEATS = [
        {"id": "bd-a", "title": "A", "board_state": "ready", "priority": 2, "project": "board-plugin"},
        {"id": "bd-b", "title": "B", "board_state": "ready", "priority": 2, "project": "protoagent"},
        {"id": "bd-c", "title": "C", "board_state": "backlog", "priority": 2, "project": "board-plugin"},
    ]

    def list_features(self, state=None, include_archived=False):
        self.calls.append(("list_features", (), {"state": state, "include_archived": include_archived}))
        return [dict(f) for f in self._FEATS]

    def get_feature(self, fid):
        self.calls.append(("get_feature", (fid,), {}))
        return next((dict(f) for f in self._FEATS if f["id"] == fid), None)


def test_create_feature_accepts_project_in_the_body(monkeypatch):
    """r4: POST /features carries ``project`` through to store.create_feature, and the
    created feature echoes it back in the response."""
    store = FakeStore()
    c = _client(monkeypatch, store)
    r = c.post("/api/plugins/project_board/features", json={"title": "X", "spec": "s", "project": "board-plugin"})
    assert r.status_code == 200 and r.json()["project"] == "board-plugin"
    call = next(c for c in store.calls if c[0] == "create_feature")
    assert call[2] == {"title": "X", "spec": "s", "project": "board-plugin"}


def test_create_feature_without_project_lets_the_store_default_it(monkeypatch):
    """r8 (API): a create with no ``project`` in the body forwards no project key, so the
    store stamps its own ``default_project`` (the store-side default is pinned in
    test_store.py) rather than the route forcing a value."""
    store = FakeStore()
    c = _client(monkeypatch, store)
    r = c.post("/api/plugins/project_board/features", json={"title": "X", "spec": "s"})
    assert r.status_code == 200
    call = next(c for c in store.calls if c[0] == "create_feature")
    assert "project" not in call[2]  # not forced → create_feature applies its default_project


def test_features_route_filters_by_project(monkeypatch):
    """r5: GET /features?project=<name> returns only the features stamped for that
    project; absent, every project is listed; an unknown name is an empty listing."""
    c = _client(monkeypatch, _ProjectStore())
    filtered = c.get("/api/plugins/project_board/features?project=board-plugin")
    assert filtered.status_code == 200
    assert [f["id"] for f in filtered.json()["features"]] == ["bd-a", "bd-c"]
    every = c.get("/api/plugins/project_board/features")
    assert {f["id"] for f in every.json()["features"]} == {"bd-a", "bd-b", "bd-c"}
    nope = c.get("/api/plugins/project_board/features?project=nope")
    assert nope.status_code == 200 and nope.json()["features"] == []


def test_feature_detail_includes_project(monkeypatch):
    """r3 (API): GET /features/{fid} carries the feature's ``project`` field."""
    c = _client(monkeypatch, _ProjectStore())
    r = c.get("/api/plugins/project_board/features/bd-b")
    assert r.status_code == 200 and r.json()["project"] == "protoagent"


# ── #90 slice 3: the tool layer ─────────────────────────────────────────────────


class _ProjectToolStore:
    """A minimal store for the tool-level project flow: ``create_feature`` records the
    ``project`` it's handed and remembers the feature; ``list_features`` returns them (so
    the create-time dedup read and board_list both see the growing board)."""

    def __init__(self):
        self.feats = []

    def list_features(self, state=None, include_archived=False):
        return [dict(f) for f in self.feats]

    def create_feature(self, title, **kw):
        f = {
            "id": f"bd-{len(self.feats) + 1}",
            "title": title,
            "board_state": "backlog",
            "blocked": False,
            "pr_url": "",
            "priority": 2,
            "difficulty": "",
            "project": kw.get("project", ""),
        }
        self.feats.append(f)
        return f


def test_tools_create_in_two_projects_then_list_filters(monkeypatch):
    """r1/r2/r7: two features created via board_create_feature into two projects; the
    ``project`` rides create → list, and board_list(project=…) keeps only that project's
    rows while an unfiltered list shows the whole board."""
    fake = _ProjectToolStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    cfg = {
        "projects": {"board-plugin": {"repo": "/plugin"}, "protoagent": {"repo": "/proto"}},
        "default_project": "board-plugin",
    }
    tools = {t.name: t for t in pb._board_tools(cfg)}
    create, lst = tools["board_create_feature"], tools["board_list"]

    create.invoke(
        {"title": "A", "spec": "s", "acceptance_criteria": "a", "files_to_modify": "a.py", "project": "board-plugin"}
    )
    create.invoke(
        {"title": "B", "spec": "s", "acceptance_criteria": "a", "files_to_modify": "b.py", "project": "protoagent"}
    )

    only_plugin = json.loads(lst.invoke({"project": "board-plugin"}))
    assert [(r["title"], r["project"]) for r in only_plugin] == [("A", "board-plugin")]
    only_proto = json.loads(lst.invoke({"project": "protoagent"}))
    assert [(r["title"], r["project"]) for r in only_proto] == [("B", "protoagent")]
    everything = json.loads(lst.invoke({}))
    assert {r["title"] for r in everything} == {"A", "B"}


def test_tools_resolve_store_kw_to_the_named_project_repo(monkeypatch):
    """r6: a project-scoped tool op resolves get_store to THAT project's repo/base_branch
    (from the board's projects map), not the instance default — while the shared projects
    map + default_project ride along so the store keeps its own per-feature resolution."""
    seen = []
    fake = _ProjectToolStore()

    def _get_store(**kw):
        seen.append(kw)
        return fake

    monkeypatch.setattr("project_board.store.get_store", _get_store)
    cfg = {
        "repo": "/server",
        "projects": {
            "board-plugin": {"repo": "/plugin", "base_branch": "main"},
            "protoagent": {"repo": "/proto", "base_branch": "develop"},
        },
        "default_project": "board-plugin",
    }
    tools = {t.name: t for t in pb._board_tools(cfg)}

    tools["board_create_feature"].invoke(
        {"title": "A", "spec": "s", "acceptance_criteria": "a", "files_to_modify": "a.py", "project": "protoagent"}
    )
    assert seen[-1]["repo"] == "/proto" and seen[-1]["base_branch"] == "develop"
    assert seen[-1]["default_project"] == "board-plugin"
    assert set(seen[-1]["projects"]) == {"board-plugin", "protoagent"}

    seen.clear()
    tools["board_list"].invoke({"project": "protoagent"})
    assert seen[-1]["repo"] == "/proto" and seen[-1]["base_branch"] == "develop"


def test_tool_absent_project_stamps_the_board_default_end_to_end(monkeypatch):
    """r8: board_create_feature with no ``project`` stamps the board's ``default_project``
    — the store fills in the default the tool forwards as empty, proven on the real ``br``
    label the create emits."""
    from project_board import store as store_mod

    monkeypatch.setattr(store_mod.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    calls = []

    def run_impl(*args, want_json=False):
        calls.append(args)
        if args and args[0] == "create":
            return "bd-1"
        if args and args[0] == "show":
            return [{"id": "bd-1", "status": "open", "labels": ["project:board-plugin"]}]
        return [] if want_json else ""

    board = store_mod.BeadsBoard(
        db=None, repo="/repo", projects={"board-plugin": {"repo": "/repo"}}, default_project="board-plugin"
    )
    monkeypatch.setattr(board, "_run", run_impl)
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)

    cfg = {"projects": {"board-plugin": {"repo": "/repo"}}, "default_project": "board-plugin"}
    create = {t.name: t for t in pb._board_tools(cfg)}["board_create_feature"]
    out = json.loads(create.invoke({"title": "T", "spec": "s", "acceptance_criteria": "a", "files_to_modify": "x.py"}))

    assert out["id"] == "bd-1"
    # the create stamped the board default project though the tool call named none
    assert any(c and c[0] == "update" and "--add-label" in c and "project:board-plugin" in c for c in calls)


def test_tool_get_feature_includes_project(monkeypatch):
    """r3 (tool): board_get_feature surfaces the feature's ``project`` in its JSON."""

    class _S:
        def get_feature(self, fid):
            return {
                "id": fid,
                "title": "T",
                "spec": "s",
                "acceptance_criteria": "a",
                "design": "",
                "board_state": "ready",
                "labels": ["project:board-plugin"],
                "pr_url": "",
                "difficulty": "",
                "files_to_modify": [],
                "foundation": False,
                "priority": 2,
                "source_issue": "",
                "depends_on": [],
                "open_depends_on": [],
                "project": "board-plugin",
            }

    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: _S())
    out = json.loads({t.name: t for t in pb._board_tools({})}["board_get_feature"].invoke({"feature_id": "bd-1"}))
    assert out["project"] == "board-plugin"


# ── #211: cancel closes the open PR + stops the in-flight drive ────────────────────


def test_cancel_route_closes_the_open_pr_and_reports_it(monkeypatch):
    """A cancel that lands while the card has an open PR (CI/review bounce) closes it
    with a comment pointing at the card — read BEFORE the cancel (the store edge
    clears nothing, but the order matters for a future store that does)."""
    from project_board import worktree

    _stub_reap(monkeypatch)
    closed = []
    monkeypatch.setattr(
        worktree,
        "close_pr_sync",
        lambda url, *, comment, cwd=".", timeout=60: closed.append((url, comment, cwd)) or (True, ""),
    )
    store = FakeStore()
    monkeypatch.setattr(
        store, "get_feature", lambda fid: {"id": fid, "board_state": "in_review", "pr_url": "https://x/pr/9"}
    )
    c = _client(monkeypatch, store, cfg={"repo": "/repo"})
    r = c.post("/api/plugins/project_board/features/bd-7/cancel", json={"reason": "scope cut"})
    assert r.status_code == 200
    assert r.json()["cancel"] == {"pr_closed": True, "pr_detail": "", "drive_cancelled": False}
    assert closed == [("https://x/pr/9", "cancelled by operator — see card bd-7", "/repo")]
    assert ("cancel_feature", ("bd-7", "scope cut"), {}) in store.calls


def test_cancel_route_never_fails_on_a_pr_close_failure(monkeypatch):
    from project_board import worktree

    _stub_reap(monkeypatch)
    monkeypatch.setattr(worktree, "close_pr_sync", lambda *a, **k: (False, "gh: HTTP 404"))
    store = FakeStore()
    monkeypatch.setattr(
        store, "get_feature", lambda fid: {"id": fid, "board_state": "in_review", "pr_url": "https://x/pr/9"}
    )
    c = _client(monkeypatch, store)
    r = c.post("/api/plugins/project_board/features/bd-7/cancel")
    assert r.status_code == 200
    assert r.json()["cancel"]["pr_closed"] is False
    assert ("cancel_feature", ("bd-7", ""), {}) in store.calls  # the cancel itself landed


def test_cancel_route_without_a_pr_does_not_touch_gh(monkeypatch):
    from project_board import worktree

    _stub_reap(monkeypatch)
    monkeypatch.setattr(worktree, "close_pr_sync", lambda *a, **k: (_ for _ in ()).throw(AssertionError("gh called")))
    store = FakeStore()  # get_feature → no pr_url
    c = _client(monkeypatch, store)
    assert c.post("/api/plugins/project_board/features/bd-7/cancel").json()["cancel"]["pr_closed"] is False


# ── /status explains an idle board (#256) ─────────────────────────────────────────


def test_status_reports_the_projects_a_failed_preflight_is_holding(monkeypatch):
    """A board can be fully `ready` and still pick up nothing: one project's gate
    preflight fail-closed, its ready cards got held, and held cards drop out of the
    ready scan — so the only symptom is `claim_decision {"selected": []}` tick after
    tick, with the reason buried in the log. /status is what the board page polls."""
    from project_board import health

    health.publish_preflight({"web": "tsc: not found", "api": True}, {})
    c = _client(monkeypatch, FakeStore())

    body = c.get("/api/plugins/project_board/status").json()

    assert body["held_projects"] == ["web"]  # 'api' passed — not held
    assert "tsc: not found" in body["preflight"]["held"]["web"]


def test_status_separates_a_dirty_checkout_from_a_held_project(monkeypatch):
    """Dirty is NOT held — the verdict was downgraded to indeterminate and dispatch
    was allowed, so the operator needs to see 'your gate result meant nothing', not
    'your work is frozen'."""
    from project_board import health

    health.publish_preflight({"web": True}, {"web": "uncommitted changes to store.py"})
    c = _client(monkeypatch, FakeStore())

    body = c.get("/api/plugins/project_board/status").json()

    assert body["held_projects"] == []
    assert "store.py" in body["preflight"]["dirty"]["web"]


def test_status_reports_nothing_held_before_any_preflight_has_run(monkeypatch):
    """A board whose loop is off never publishes — that must read as 'nothing held',
    not as a missing key the view has to defend against."""
    from project_board import health

    health._health.pop("preflight", None)
    c = _client(monkeypatch, FakeStore())

    body = c.get("/api/plugins/project_board/status").json()

    assert body["held_projects"] == [] and body["preflight"] == {"held": {}, "dirty": {}}
