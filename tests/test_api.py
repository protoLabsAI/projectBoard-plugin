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

from project_board import api
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


def test_ready_gate_rejection_surfaces_as_400(monkeypatch):
    c = _client(monkeypatch, FakeStore())
    r = c.post("/api/plugins/project_board/features/bad/ready")
    assert r.status_code == 400 and "Ready gate" in r.json()["detail"]


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
