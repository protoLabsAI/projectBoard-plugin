"""Unblocking a review-gate block re-arms the gate, or an operator override supersedes
the board's own failure status (#512, second half).

When the in-loop review gate exhausts its fix budget it BLOCKS the card and leaves a
``failure`` status under ``worktree.GATE_STATUS_CONTEXT`` (``board/review-gate``) on the PR
head. ``store.clear_blocked`` alone lifts the flag but touches neither the review substate
nor that status, so the card lands back in_review and can never promote (careercoach#17).
``loop.unblock_side_effects`` is the shared fix: on an in_review PR it re-arms the gate on
the live head (default) or records an explicit operator acceptance (override), and the
route + tool both thread ``override_review`` through to it.

These stub ``worktree._gh_sync`` (the sync gh seam) and use a fake store, mirroring the
patterns in ``tests/test_api.py`` / ``tests/test_board_tools.py``.
"""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import project_board as pb
from project_board import api, worktree
import project_board.loop as loop_mod
from project_board.store import LABEL_IN_REVIEW, LABEL_REVIEW_CLEAN, LABEL_REVIEW_PENDING

HEAD = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"  # a full 40-char live PR head
PR_URL = "https://github.com/acme/widget/pull/7"
PRIOR = "review findings persist after 2 fix attempt(s) — needs human review: " + PR_URL


class FakeStore:
    """Records the review-substate / budget / comment writes the helper makes."""

    def __init__(self):
        self.substate = []
        self.budgets_cleared = []
        self.comments = []

    def set_review_substate(self, fid, label, note="", head_sha=""):
        self.substate.append({"fid": fid, "label": label, "note": note, "head_sha": head_sha})
        if note:  # the real store records a non-empty note as a bead comment
            self.comments.append((fid, note))
        return {"id": fid}

    def clear_budgets(self, fid, kinds=None):
        self.budgets_cleared.append((fid, kinds))
        return {"id": fid}

    def comment(self, fid, text):
        self.comments.append((fid, text))


def _gh_stub(records, *, head=HEAD, head_rc=0, raise_on_view=False):
    """A fake ``worktree._gh_sync``: records every call, answers a ``pr view`` with ``head``
    (or ``head_rc``/an exception) and an ``api … statuses`` POST with success."""

    def _run(*args, cwd=".", timeout=30.0):
        records.append({"args": args, "cwd": cwd, "timeout": timeout})
        if args[:2] == ("pr", "view"):
            if raise_on_view:
                raise worktree.WorktreeError("gh pr view timed out")
            return (head_rc, (head + "\n") if head_rc == 0 else "", "" if head_rc == 0 else "no such PR")
        return (0, "", "")

    return _run


def _review_blocked(fid="bd-1", pr_url=PR_URL):
    """A card the review gate blocked: still carries the in-review label + pr_url, and the
    projection reports ``board_state: blocked`` plus the last ``blocked_reason``."""
    return {
        "id": fid,
        "board_state": "blocked",
        "labels": [LABEL_IN_REVIEW, "blocked"],
        "pr_url": pr_url,
        "blocked_reason": PRIOR,
        "project": "",
    }


def _api_posts(calls):
    return [c for c in calls if c["args"][:1] == ("api",)]


# ── (a) default: re-arm the gate on the live head ────────────────────────────────────


def test_unblock_rearms_the_review_gate_on_the_live_head(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(loop_mod, "get_store", lambda *a, **k: store)
    calls = []
    monkeypatch.setattr(worktree, "_gh_sync", _gh_stub(calls))

    out = loop_mod.unblock_side_effects("bd-1", _review_blocked(), cwd="/repo")

    assert out["review"] == "re-armed"
    assert out["head"] == HEAD
    assert out["status_posted"] is True
    # review-pending set, pinning NO head (only a clean verdict pins one)
    assert store.substate and store.substate[0]["label"] == LABEL_REVIEW_PENDING
    assert store.substate[0]["head_sha"] == ""
    # the review-fix budget label reset so the next reconcile gets a fresh budget
    assert ("bd-1", ["review-fix"]) in store.budgets_cleared
    # exactly one status POSTed: pending, under board/review-gate, at the live head, in cwd
    (post,) = _api_posts(calls)
    assert post["args"][:3] == ("api", "--method", "POST")
    assert f"/repos/acme/widget/statuses/{HEAD}" in post["args"]
    assert "state=pending" in post["args"]
    assert f"context={worktree.GATE_STATUS_CONTEXT}" in post["args"]
    assert "target_url=" + PR_URL in post["args"]
    assert post["cwd"] == "/repo"


# ── (b) override: accept the findings, pin review-clean to the live head ──────────────


def test_unblock_override_posts_success_and_pins_review_clean(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(loop_mod, "get_store", lambda *a, **k: store)
    calls = []
    monkeypatch.setattr(worktree, "_gh_sync", _gh_stub(calls))

    out = loop_mod.unblock_side_effects("bd-2", _review_blocked("bd-2"), override_review=True, cwd="/repo")

    assert out["review"] == "overridden"
    assert out["head"] == HEAD
    assert out["status_posted"] is True
    # review-clean, PINNED to the exact live head
    assert store.substate and store.substate[0]["label"] == LABEL_REVIEW_CLEAN
    assert store.substate[0]["head_sha"] == HEAD
    # the audit comment names the override, the head and the prior block reason
    note = store.substate[0]["note"]
    assert "override" in note.lower()
    assert HEAD[:12] in note
    assert "review findings persist" in note
    assert ("bd-2", note) in store.comments
    # one success status under board/review-gate; override does NOT reset the fix budget
    (post,) = _api_posts(calls)
    assert "state=success" in post["args"]
    assert f"context={worktree.GATE_STATUS_CONTEXT}" in post["args"]
    assert f"/repos/acme/widget/statuses/{HEAD}" in post["args"]
    assert store.budgets_cleared == []


# ── (c) a card that was not in_review / had no pr_url → no gh calls ───────────────────


def test_unblock_of_a_non_review_card_touches_no_gh_or_store(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(loop_mod, "get_store", lambda *a, **k: store)
    calls = []
    monkeypatch.setattr(worktree, "_gh_sync", _gh_stub(calls))

    # has a pr_url but is NOT in review (no in-review label, board_state != in_review)
    not_in_review = {"id": "bd-3", "board_state": "blocked", "labels": ["blocked"], "pr_url": PR_URL}
    # in review but has NO pr_url
    no_pr = {"id": "bd-4", "board_state": "blocked", "labels": [LABEL_IN_REVIEW, "blocked"], "pr_url": ""}

    assert loop_mod.unblock_side_effects("bd-3", not_in_review, cwd="/repo") == {"review": "n/a"}
    assert loop_mod.unblock_side_effects("bd-4", no_pr, override_review=True, cwd="/repo") == {"review": "n/a"}
    assert loop_mod.unblock_side_effects("bd-5", {}, cwd="/repo") == {"review": "n/a"}

    assert calls == []  # gh never shelled
    assert store.substate == [] and store.budgets_cleared == [] and store.comments == []


# ── (d) an unreadable live head → the unblock succeeds, but no status is posted ───────


def test_unblock_with_unreadable_head_posts_no_status(monkeypatch):
    monkeypatch.setattr(loop_mod, "get_store", lambda *a, **k: FakeStore())

    for stub in (_gh_stub([], head_rc=1), _gh_stub([], raise_on_view=True)):
        store = FakeStore()
        monkeypatch.setattr(loop_mod, "get_store", lambda *a, **k: store)
        calls = []

        def _record(*args, cwd=".", timeout=30.0):
            calls.append({"args": args, "cwd": cwd})
            return stub(*args, cwd=cwd, timeout=timeout)

        monkeypatch.setattr(worktree, "_gh_sync", _record)

        out = loop_mod.unblock_side_effects("bd-6", _review_blocked("bd-6"), cwd="/repo")

        assert out == {"review": "head-unknown"}
        assert _api_posts(calls) == []  # no status posted against an unknown head
        assert store.substate == [] and store.budgets_cleared == [] and store.comments == []


# ── (e) the route AND the tool both thread override_review through to the helper ──────


def _capture_helper():
    captured = []

    def _fake(fid, feature_before, *, override_review=False, cwd="."):
        captured.append({"fid": fid, "override_review": override_review, "cwd": cwd})
        return {"review": "overridden" if override_review else "re-armed", "head": "h"}

    return captured, _fake


class _RouteStore:
    def get_feature(self, fid):
        return _review_blocked(fid)

    def clear_blocked(self, fid):
        return {"id": fid, "board_state": "in_review"}


def test_route_passes_override_review_through(monkeypatch):
    captured, fake = _capture_helper()
    monkeypatch.setattr(loop_mod, "unblock_side_effects", fake)
    monkeypatch.setattr(api, "get_store", lambda **_kw: _RouteStore())
    app = FastAPI()
    app.include_router(api.build_data_router({}), prefix="/api/plugins/project_board")
    c = TestClient(app)

    r = c.post("/api/plugins/project_board/features/bd-1/unblock", json={"override_review": True})
    assert r.status_code == 200
    assert r.json()["review"] == {"review": "overridden", "head": "h"}
    assert captured[-1]["override_review"] is True

    # default (omitted body) and an explicit false both re-arm
    assert c.post("/api/plugins/project_board/features/bd-1/unblock").status_code == 200
    assert captured[-1]["override_review"] is False
    c.post("/api/plugins/project_board/features/bd-1/unblock", json={"override_review": False})
    assert captured[-1]["override_review"] is False


def test_tool_passes_override_review_through(monkeypatch):
    captured, fake = _capture_helper()
    monkeypatch.setattr(loop_mod, "unblock_side_effects", fake)
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: _RouteStore())
    tool = {t.name: t for t in pb._board_tools({})}["board_unblock_feature"]

    out = json.loads(tool.invoke({"feature_id": "bd-1", "override_review": True}))
    assert out["review"] == {"review": "overridden", "head": "h"}
    assert captured[-1]["override_review"] is True

    json.loads(tool.invoke({"feature_id": "bd-1"}))  # default
    assert captured[-1]["override_review"] is False


# ── (f) the board NEVER writes a status under the external panel's `QA panel` ─────────


def test_unblock_never_posts_under_qa_panel(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(loop_mod, "get_store", lambda *a, **k: store)
    calls = []
    monkeypatch.setattr(worktree, "_gh_sync", _gh_stub(calls))

    loop_mod.unblock_side_effects("bd-7", _review_blocked("bd-7"), cwd="/r")
    loop_mod.unblock_side_effects("bd-7", _review_blocked("bd-7"), override_review=True, cwd="/r")

    flat = [a for c in calls for a in c["args"]]
    assert worktree.REVIEW_STATUS_CONTEXT == "QA panel"  # the legacy context we must never write
    assert not any(f"context={worktree.REVIEW_STATUS_CONTEXT}" == a for a in flat)
    assert not any("QA panel" in str(a) for a in flat)
    posts = _api_posts(calls)
    assert posts and all(f"context={worktree.GATE_STATUS_CONTEXT}" in p["args"] for p in posts)
