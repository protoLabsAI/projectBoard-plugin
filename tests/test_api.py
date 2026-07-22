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
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

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

    def list_features(self, state=None):
        self.calls.append(("list_features", (), {"state": state}))
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
        return {"id": "bd-new", "board_state": "backlog", "title": k.get("title", "")}

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

    def delete_feature(self, fid, reason=""):
        return self._rec("delete_feature", fid, reason)

    def bounce_ci_fail(self, fid, reason):
        return self._rec("bounce_ci_fail", fid, reason)

    def escalate(self, fid, reason):
        self.calls.append(("escalate", (fid, reason), {}))
        return self._escalate_to

    def requeue(self, fid):
        return self._rec("requeue", fid)

    def block_from_review(self, fid, reason):
        return self._rec("block_from_review", fid, reason)

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


# ── the route split + the view-path mount (the regression guard) ────────────────


def test_board_view_is_served_on_the_declared_public_path(monkeypatch):
    c = _client(monkeypatch, FakeStore())
    # The PAGE is public (an iframe src can't carry a bearer)…
    r = c.get("/plugins/project_board/board")
    assert r.status_code == 200 and "<!doctype html>" in r.text.lower()
    # …and it is NOT under /api (where the kit's base-derivation would break).
    assert c.get("/api/plugins/project_board/board").status_code == 404


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
        def list_features(self, state=None):
            raise BoardError("repo '.' has no beads workspace — set project_board.repo")

        def get_feature(self, fid):
            raise BoardError("repo '.' has no beads workspace — set project_board.repo")

    c = _client(monkeypatch, BrokenStore())
    for path in ("/api/plugins/project_board/features", "/api/plugins/project_board/features/bd-1"):
        r = c.get(path)
        assert r.status_code == 400, path
        assert "beads workspace" in r.json()["detail"], path


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


# ── live coder-monitoring snapshot (#84): GET /features/{fid}/progress ───────────


def test_progress_404s_on_an_unknown_feature(monkeypatch):
    c = _client(monkeypatch, FakeStore())
    assert c.get("/api/plugins/project_board/features/missing/progress").status_code == 404


def test_progress_is_empty_but_valid_when_no_live_run(monkeypatch):
    coder_seam._progress_reset()
    c = _client(monkeypatch, FakeStore())  # bd-1 is a known feature with no live run
    r = c.get("/api/plugins/project_board/features/bd-1/progress")
    assert r.status_code == 200
    assert r.json() == {"gens": []}


def test_progress_returns_the_per_gen_snapshot_contract(monkeypatch):
    """The endpoint contract: {"gens": [{gen, tier, elapsed_s, current_tool,
    recent_tools, thought_tail, usage}]} — fed straight from the in-memory buffer."""
    coder_seam._progress_reset()
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


def test_ready_gate_rejection_surfaces_as_400(monkeypatch):
    c = _client(monkeypatch, FakeStore())
    r = c.post("/api/plugins/project_board/features/bad/ready")
    assert r.status_code == 400 and "Ready gate" in r.json()["detail"]


def test_cancel_route_calls_cancel_feature_with_reason(monkeypatch):
    """POST /features/{fid}/cancel — the second terminal edge (#47). Carries the
    optional reason through; works with no body too."""
    store = FakeStore()
    c = _client(monkeypatch, store)
    r = c.post("/api/plugins/project_board/features/bd-7/cancel", json={"reason": "duplicate"})
    assert r.status_code == 200
    assert ("cancel_feature", ("bd-7", "duplicate"), {}) in store.calls
    # No body → cancels with an empty reason (still a valid request, not a 422).
    r2 = c.post("/api/plugins/project_board/features/bd-8/cancel")
    assert r2.status_code == 200
    assert ("cancel_feature", ("bd-8", ""), {}) in store.calls


def test_delete_route_calls_delete_feature(monkeypatch):
    """DELETE /features/{fid} — the hard-delete sibling of cancel (#47). Carries an
    optional reason; works with no body too."""
    store = FakeStore()
    c = _client(monkeypatch, store)
    r = c.request("DELETE", "/api/plugins/project_board/features/bd-7", json={"reason": "mistake"})
    assert r.status_code == 200
    assert ("delete_feature", ("bd-7", "mistake"), {}) in store.calls
    r2 = c.delete("/api/plugins/project_board/features/bd-8")
    assert r2.status_code == 200
    assert ("delete_feature", ("bd-8", ""), {}) in store.calls


# ── the single Done edge: the merge webhook ─────────────────────────────────────


def _signed(secret, raw):
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


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


def test_webhook_without_a_secret_processes_unsigned(monkeypatch):
    store = FakeStore(merged=None)  # no feature matches → ignored, but no 401
    c = _client(monkeypatch, store, cfg={"webhook_secret": ""})
    r = c.post("/plugins/project_board/webhook/pr", content=_merge_body())
    assert r.status_code == 200  # dev mode: signature not verified


# ── /ci: escalate when a ladder exists, else bounce ─────────────────────────────

ESCALATION_CFG = {"coders": {"fast": "proto", "smart": "proto-smart"}}


def test_ci_pass_is_a_noop(monkeypatch):
    c = _client(monkeypatch, FakeStore(), cfg=ESCALATION_CFG)
    r = c.post("/plugins/project_board/features/bd-1/ci", json={"passed": True})
    assert r.json()["ok"] is True


def test_ci_fail_with_a_ladder_escalates_and_requeues(monkeypatch):
    store = FakeStore(escalate_to="smart")
    c = _client(monkeypatch, store, cfg=ESCALATION_CFG)
    r = c.post("/plugins/project_board/features/bd-1/ci", json={"passed": False, "reason": "boom"})
    body = r.json()
    assert body["requeued"] is True and body["escalated"] is True and body["next_tier"] == "smart"
    assert any(call[0] == "requeue" for call in store.calls)


def test_ci_fail_at_the_top_of_the_ladder_blocks(monkeypatch):
    store = FakeStore(escalate_to=None)  # ladder exhausted
    c = _client(monkeypatch, store, cfg=ESCALATION_CFG)
    r = c.post("/plugins/project_board/features/bd-1/ci", json={"passed": False})
    body = r.json()
    assert body["exhausted"] is True and body["requeued"] is False
    assert any(call[0] == "block_from_review" for call in store.calls)


def test_ci_fail_with_a_single_coder_bounces_to_in_progress(monkeypatch):
    store = FakeStore()
    c = _client(monkeypatch, store, cfg={})  # no coders map → no escalation
    r = c.post("/plugins/project_board/features/bd-1/ci", json={"passed": False, "reason": "x"})
    body = r.json()
    assert body["escalated"] is False and body["requeued"] is False
    assert any(call[0] == "bounce_ci_fail" for call in store.calls)


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
    c = _client(monkeypatch, store, cfg={"coder_solve_test_cmd": "pytest -q"})
    r = c.post("/api/plugins/project_board/features/bd-7/test-rung", json={"rung": "greedy"})
    assert r.status_code == 400
    assert "acp delegate" in r.json()["detail"]


def test_test_rung_fusion_400s_without_a_configured_fusion_delegate(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(store, "get_feature", lambda fid: _feature_with_ac(fid))
    monkeypatch.setattr(coder_seam, "_import_solve", lambda: object())
    monkeypatch.setattr(coder_seam, "resolve_delegate", lambda name, t: object())
    c = _client(monkeypatch, store, cfg={"coder_solve_test_cmd": "pytest -q"})  # no coder_solve_fusion_delegate
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
    c = _client(monkeypatch, store, cfg={"coder_solve_test_cmd": "pytest -q"})
    r = c.post("/api/plugins/project_board/features/bd-7/test-rung", json={"rung": "greedy"})
    assert r.status_code == 400
    assert "test-rung failed" in r.json()["detail"]
