"""#856 — the REST merge-hold routes take the same inputs as the tools.

`DELETE /features/{fid}/merge-hold` accepts `force` and an npm `token` and passes them to
`store.release_merge_hold`, so an operator can release (or force-release) a conditional
hold (#676) without an agent; an unmet condition without force is a 400 naming it.
`POST /features/{fid}/merge-hold` accepts `until`, and a re-hold without `until` keeps the
stored condition (store-level, so `board_hold_merge` behaves the same).

Exercised through the real `build_data_router` with a stateful fake `br` (labels, notes,
comments round-trip) and the gate checks stubbed, so no network is touched.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from project_board import api, gates, store
from tests.test_merge_hold_until_676 import SPEC, _bead, _FakeBr, _tool, _wire

BASE = "/api/plugins/project_board/features/bd-1/merge-hold"
NPM_SPEC = "npm:@x/private@>=1.0.0"


@pytest.fixture(autouse=True)
def _fresh_gate_cache():
    gates.reset_cache()
    yield
    gates.reset_cache()


@pytest.fixture
def pr_open(monkeypatch):
    calls = {"n": 0}

    def _eval_pr(spec):
        calls["n"] += 1
        return {"met": False, "detail": f"{spec.describe()} (open)"}

    monkeypatch.setattr(gates, "eval_pr", _eval_pr)
    return calls


def _board(make_board):
    return make_board(_FakeBr([_bead(status="in_progress", labels=["in-review"], notes="a.py\nsource-issue: o/r#9")]))


def _client(board, monkeypatch, cfg=None):
    monkeypatch.setattr(api, "get_store", lambda **_kw: board)
    app = FastAPI()
    app.include_router(api.build_data_router(cfg or {}), prefix="/api/plugins/project_board")
    return TestClient(app)


def _delete(c, body=None, **params):
    return c.request("DELETE", BASE, json=body, params=params or None)


# ── r1: an unmet condition without force is refused, the hold stays ────────────────────


def test_delete_without_force_on_unmet_condition_is_refused(make_board, monkeypatch, pr_open):
    board = _board(make_board)
    c = _client(board, monkeypatch)
    assert c.post(BASE, json={"reason": "after o/r#31", "until": SPEC}).status_code == 200
    n_comments = len(board._run.comments())

    r = _delete(c)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert SPEC in detail and "unmet" in detail and "force=true" in detail
    assert store.LABEL_MERGE_HOLD in board._run.labels()
    assert f"merge-hold-until: {SPEC}" in board._run.notes()
    assert len(board._run.comments()) == n_comments
    assert pr_open["n"] == 1  # checked live


# ── r2: force=true releases and audits the override, as the tool does ──────────────────


@pytest.mark.parametrize("how", ["body", "query", "body-string"])
def test_delete_with_force_releases_and_audits_like_the_tool(make_board, monkeypatch, pr_open, how):
    board = _board(make_board)
    c = _client(board, monkeypatch)
    assert c.post(BASE, json={"reason": "after o/r#31", "until": SPEC}).status_code == 200

    if how == "body":
        r = _delete(c, {"force": True})
    elif how == "query":
        r = _delete(c, force="true")
    else:
        r = _delete(c, {"force": "true"})
    assert r.status_code == 200, r.text
    assert r.json()["merge_hold"] is False
    assert store.LABEL_MERGE_HOLD not in board._run.labels()
    assert "merge-hold-until" not in board._run.notes()
    route_note = board._run.comments()[-1]
    assert "override" in route_note and SPEC in route_note

    # The tool's forced release on an identical card writes the same audit comment.
    twin = _board(make_board)
    twin.set_merge_hold("bd-1", "after o/r#31", until=SPEC)
    _wire(twin, monkeypatch)
    gates.reset_cache()
    json.loads(_tool("board_release_merge_hold").invoke({"feature_id": "bd-1", "force": True}))
    assert twin._run.comments()[-1] == route_note


def test_delete_force_false_string_is_not_force(make_board, monkeypatch, pr_open):
    board = _board(make_board)
    c = _client(board, monkeypatch)
    c.post(BASE, json={"reason": "after o/r#31", "until": SPEC})
    assert _delete(c, {"force": "false"}).status_code == 400
    assert store.LABEL_MERGE_HOLD in board._run.labels()


# ── r3: a supplied npm token reaches the store's release check ──────────────────────────


def _npm_stub(monkeypatch, seen):
    def _eval_npm(spec, *, token="", resolve_card=None):
        seen.append(token)
        ok = token == "s3cret"
        return {"met": ok, "detail": f"{spec.describe()} ({'published' if ok else '404 without a token'})"}

    monkeypatch.setattr(gates, "eval_npm", _eval_npm)


def test_delete_passes_the_supplied_npm_token_to_the_release_check(make_board, monkeypatch):
    seen: list[str] = []
    _npm_stub(monkeypatch, seen)
    monkeypatch.delenv("PROJECT_BOARD_NPM_TOKEN", raising=False)
    monkeypatch.delenv("NPM_TOKEN", raising=False)
    board = _board(make_board)
    c = _client(board, monkeypatch)
    assert c.post(BASE, json={"reason": "after publish", "until": NPM_SPEC}).status_code == 200

    r = _delete(c)  # no token → the private package reads unmet → refused
    assert r.status_code == 400 and NPM_SPEC in r.json()["detail"]
    assert seen[-1] == ""
    assert store.LABEL_MERGE_HOLD in board._run.labels()

    r = _delete(c, {"token": "s3cret"})
    assert r.status_code == 200, r.text
    assert seen[-1] == "s3cret"
    assert store.LABEL_MERGE_HOLD not in board._run.labels()
    assert board._run.comments()[-1] == f"merge-hold released (until met: {NPM_SPEC})"


def test_delete_without_token_falls_back_to_the_configured_npm_token(make_board, monkeypatch):
    seen: list[str] = []
    _npm_stub(monkeypatch, seen)
    board = _board(make_board)
    c = _client(board, monkeypatch, cfg={"npm_token": "s3cret"})
    c.post(BASE, json={"reason": "after publish", "until": NPM_SPEC})
    r = _delete(c)
    assert r.status_code == 200, r.text
    assert seen[-1] == "s3cret"


# ── r4: POST stores `until`; a later reason-only POST keeps it ──────────────────────────


def test_post_with_until_stores_the_condition_like_the_tool(make_board, monkeypatch):
    board = _board(make_board)
    c = _client(board, monkeypatch)
    r = c.post(BASE, json={"reason": "after o/r#31", "until": SPEC})
    assert r.status_code == 200, r.text
    via_route = board.get_feature("bd-1")

    twin = _board(make_board)
    _wire(twin, monkeypatch)
    out = _tool("board_hold_merge").invoke({"feature_id": "bd-1", "reason": "after o/r#31", "until": SPEC})
    assert not out.startswith("Error:"), out
    via_tool = twin.get_feature("bd-1")

    for key in ("merge_hold", "merge_hold_until_specs", "merge_hold_reason"):
        assert via_route[key] == via_tool[key]
    assert via_route["merge_hold_until_specs"] == [SPEC]
    assert board._run.notes() == twin._run.notes()
    assert board._run.comments() == twin._run.comments()


def test_reason_only_repost_keeps_the_condition(make_board, monkeypatch):
    board = _board(make_board)
    c = _client(board, monkeypatch)
    c.post(BASE, json={"reason": "after o/r#31", "until": SPEC})
    n = len(board._run.comments())

    # A retry (same reason, no until) is a no-op that keeps the condition.
    r = c.post(BASE, json={"reason": "after o/r#31"})
    assert r.status_code == 200
    assert len(board._run.comments()) == n
    assert board.get_feature("bd-1")["merge_hold_until_specs"] == [SPEC]

    # A reason edit (no until) updates the reason only and keeps the condition.
    r = c.post(BASE, json={"reason": "after o/r#31 and QA"})
    assert r.status_code == 200
    f = board.get_feature("bd-1")
    assert f["merge_hold_until_specs"] == [SPEC] and f["merge_hold_reason"] == "after o/r#31 and QA"
    assert board._run.comments()[-1] == "merge-hold updated: reason 'after o/r#31' → 'after o/r#31 and QA'"
    assert len(board._run.comments()) == n + 1


def test_tool_reason_only_rehold_keeps_the_condition_too(make_board):
    board = _board(make_board)
    board.set_merge_hold("bd-1", "old", until=SPEC)
    board.set_merge_hold("bd-1", "new")
    f = board.get_feature("bd-1")
    assert f["merge_hold_until_specs"] == [SPEC] and f["merge_hold_reason"] == "new"


def test_post_with_bad_until_is_a_400_and_changes_nothing(make_board, monkeypatch):
    board = _board(make_board)
    c = _client(board, monkeypatch)
    r = c.post(BASE, json={"reason": "x", "until": "nonsense:abc"})
    assert r.status_code == 400 and "until" in r.json()["detail"]
    assert store.LABEL_MERGE_HOLD not in board._run.labels()
    assert board._run.comments() == []


def test_forced_npm_recheck_with_a_new_token_skips_the_throttled_verdict(monkeypatch):
    seen: list[str] = []
    _npm_stub(monkeypatch, seen)
    first = gates.evaluate([NPM_SPEC], token="", force=True, now=100.0)
    assert first[0]["met"] is False
    # Same token inside the force throttle → the cached verdict, no new check.
    assert gates.evaluate([NPM_SPEC], token="", force=True, now=101.0)[0]["met"] is False
    assert len(seen) == 1
    # A different token inside the throttle → re-checked with it.
    assert gates.evaluate([NPM_SPEC], token="s3cret", force=True, now=102.0)[0]["met"] is True
    assert seen == ["", "s3cret"]
    # An unforced (background) read still honours the cache.
    assert gates.evaluate([NPM_SPEC], token="other", now=103.0)[0]["met"] is True
    assert len(seen) == 2
