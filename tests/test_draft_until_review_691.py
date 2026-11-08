"""#691: with the review gate on, the board's first PR opens as a DRAFT and is marked ready
only when the gate is clean.

The fleet QA panel skips drafts and reviews on `ready_for_review`. Before this, the board
opened a normal PR and ran its own gate ~3 min later, so the panel reviewed every head the
gate was about to bounce: 71% of board re-pushes followed the board gate's own FAIL, costing
~17% of the panel's time and 45% of its superseded rounds (audit 2026-11-08).
"""

from __future__ import annotations

import shutil

import pytest

from project_board import store as store_mod
from project_board.store import BeadsBoard
from project_board import worktree
from project_board.loop import BoardLoop

from test_loop import (  # noqa: F401 — the two autouse fixtures keep the gate from shelling a real `gh`
    FEATURE,
    _EscalatingStore,
    _GateStore,
    _gate_loop,
    _inject_fake_findings,
    _no_real_pr_head_sha,
    _no_real_review_status_seam,
)

PR = "https://github.com/o/r/pull/9"
_BLOCKER = '[{"file": "a.py", "line": 3, "severity": "blocker", "claim": "drops data", "evidence": "x", "verdict": "confirmed"}]'


class _DraftStore(_GateStore):
    def record_board_draft(self, fid, on):
        self.calls.append(("record_board_draft", fid, on))
        if on and store_mod.LABEL_BOARD_DRAFT not in self.labels:
            self.labels.append(store_mod.LABEL_BOARD_DRAFT)
        if not on and store_mod.LABEL_BOARD_DRAFT in self.labels:
            self.labels.remove(store_mod.LABEL_BOARD_DRAFT)
        return {"id": fid}


def _ready_recorder(monkeypatch, ok=True):
    calls = []

    async def _ready(pr_url, branch, *, cwd=".", adopted=True):
        assert adopted is False  # the gate's release, not a coder-draft adoption
        calls.append(pr_url)
        return ok

    monkeypatch.setattr(worktree, "_promote_adopted_draft", _ready)
    return calls


def test_the_knob_defaults_on_and_can_be_turned_off():
    assert BoardLoop({}).draft_until_review is True
    assert BoardLoop({"draft_until_review": "false"}).draft_until_review is False


async def test_a_clean_gate_marks_the_boards_own_draft_ready(monkeypatch):
    _inject_fake_findings(monkeypatch)
    store = _DraftStore()
    store.labels = [store_mod.LABEL_BOARD_DRAFT]
    ready = _ready_recorder(monkeypatch)
    loop = _gate_loop(monkeypatch, "clean.\n```json\n[]\n```")
    await loop._review_gate(store, "bd-1", PR, "/repo")
    assert store.review_states[-1][0] == "review-clean"
    assert ready == [PR]
    assert store_mod.LABEL_BOARD_DRAFT not in store.labels


async def test_an_operator_draft_is_never_undrafted(monkeypatch):
    """#207: a draft the operator set by hand is a hold; only `board-draft` cards are released."""
    _inject_fake_findings(monkeypatch)
    store = _DraftStore()
    ready = _ready_recorder(monkeypatch)
    loop = _gate_loop(monkeypatch, "clean.\n```json\n[]\n```")
    await loop._review_gate(store, "bd-1", PR, "/repo")
    assert store.review_states[-1][0] == "review-clean"
    assert ready == []


async def test_blocking_findings_keep_the_pr_a_draft(monkeypatch):
    _inject_fake_findings(monkeypatch)
    store = _DraftStore()
    store.labels = [store_mod.LABEL_BOARD_DRAFT]
    ready = _ready_recorder(monkeypatch)
    loop = _gate_loop(monkeypatch, f"```json\n{_BLOCKER}\n```")
    await loop._review_gate(store, "bd-1", PR, "/repo")
    assert ("requeue", "bd-1") in store.calls
    assert ready == []
    assert store_mod.LABEL_BOARD_DRAFT in store.labels  # the fix round stays off the panel's radar


async def test_a_failed_ready_keeps_the_label_for_the_next_clean_verdict(monkeypatch):
    _inject_fake_findings(monkeypatch)
    store = _DraftStore()
    store.labels = [store_mod.LABEL_BOARD_DRAFT]
    ready = _ready_recorder(monkeypatch, ok=False)
    loop = _gate_loop(monkeypatch, "clean.\n```json\n[]\n```")
    await loop._review_gate(store, "bd-1", PR, "/repo")
    assert ready == [PR]
    assert store_mod.LABEL_BOARD_DRAFT in store.labels


async def test_an_inert_gate_does_not_hold_the_pr_out_of_external_review(monkeypatch):
    """No findings parser on the host: the gate cannot judge, so it hands the PR to the panel."""
    import sys

    monkeypatch.delitem(sys.modules, "graph.review.findings", raising=False)
    monkeypatch.delitem(sys.modules, "graph.review", raising=False)
    store = _DraftStore()
    store.labels = [store_mod.LABEL_BOARD_DRAFT]
    ready = _ready_recorder(monkeypatch)
    loop = _gate_loop(monkeypatch, "anything")
    monkeypatch.setattr(loop, "_parse_findings", lambda output: None)
    await loop._review_gate(store, "bd-1", PR, "/repo")
    assert ready == [PR]


async def test_note_board_draft_records_only_a_real_draft(monkeypatch):
    for is_draft, want in ((True, [store_mod.LABEL_BOARD_DRAFT]), (False, [])):
        store = _DraftStore()

        async def _info(pr_url, *, cwd=".", _d=is_draft):
            return {"isDraft": _d}

        monkeypatch.setattr(worktree, "pr_merge_info", _info)
        await BoardLoop({"review_gate": True})._note_board_draft(store, "bd-1", PR, "/repo")
        assert store.labels == want


async def _first_open_kwargs(monkeypatch, cfg, *, pr_url=None):
    """Drive one build with the worktree tier mocked up to `open_pr`, record the kwargs the
    drive passes, then stop the drive there (NoChangesError)."""
    store = _EscalatingStore(tiers=["smart"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    seen: list[dict] = []

    async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
        return "done"

    async def _create(repo, base, fid, root, title="", **_kw):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _head(path):
        return "a" * 40

    async def _unchanged(path, start_sha):
        return None  # the fix round moved the head

    async def _open_pr(wt, branch, *, base, title, body, promote_draft=True, **kw):
        seen.append({"promote_draft": promote_draft, **kw})
        raise worktree.NoChangesError("stop here")

    async def _none(*_a, **_k):
        return None

    async def _discard(repo, wt, branch="", *, base="", why=""):
        return (True, None)

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "checkout_head_sha", _head)
    monkeypatch.setattr(worktree, "fix_round_unchanged", _unchanged)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", _open_pr)
    monkeypatch.setattr(worktree, "discard_worktree", _discard)
    monkeypatch.setattr(worktree, "remove_worktree", _none)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _none)
    loop = BoardLoop({"coders": {"fast": "proto-fast", "smart": "proto-smart"}, **cfg})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())
    feature = {**FEATURE}
    if pr_url:
        feature["pr_url"] = pr_url
    await loop._drive(feature)
    return seen


@pytest.mark.parametrize(
    "cfg,pr_url,want",
    [
        ({"review_gate": True}, None, {"promote_draft": False, "draft": True}),
        ({"review_gate": True, "draft_until_review": False}, None, {"promote_draft": True}),
        ({}, None, {"promote_draft": True}),
        ({"review_gate": True}, PR, {"promote_draft": False}),  # a re-dispatch leaves the PR alone
    ],
)
async def test_the_first_pr_opens_as_a_draft_only_with_the_gate_on(monkeypatch, cfg, pr_url, want):
    seen = await _first_open_kwargs(monkeypatch, cfg, pr_url=pr_url)
    assert seen and seen[0] == want


@pytest.mark.skipif(shutil.which(store_mod.BR) is None, reason="real `br` not on PATH (CI installs it)")
@pytest.mark.br_shape
def test_record_board_draft_sets_and_clears_through_real_br(tmp_path):
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    fid = board.create_feature("feat: a thing", spec="s")["id"]

    def has():
        return store_mod.LABEL_BOARD_DRAFT in board.get_feature(fid)["labels"]

    board.record_board_draft(fid, True)
    assert has()
    board.record_board_draft(fid, True)  # idempotent: one label, no failed write
    assert board.get_feature(fid)["labels"].count(store_mod.LABEL_BOARD_DRAFT) == 1
    board.record_board_draft(fid, False)
    assert not has()
    board.record_board_draft(fid, False)  # nothing to clear: a no-op
    assert not has()
