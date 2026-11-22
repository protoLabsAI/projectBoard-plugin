"""Manual worktree ownership suppresses loop work until explicitly released (#754)."""

import asyncio
import logging
from unittest.mock import AsyncMock, Mock

import pytest

from project_board import worktree
from project_board.loop import BoardLoop
from project_board.loop.drive import _is_livelock_skip_reason
from project_board.store import LABEL_WORKTREE_HOLD
from tests.test_loop import FEATURE, FakeLoopStore, _ClaimStore, _drive_with, _ready


@pytest.mark.parametrize("projection", [{"labels": [LABEL_WORKTREE_HOLD]}, {"worktree_hold": True}])
async def test_pending_integration_resumes_after_release(monkeypatch, projection):
    feature = {**FEATURE, "board_state": "in_review", "pr_url": "https://example/pr/7", **projection}
    store = FakeLoopStore()
    store.get_feature = lambda fid: dict(feature)
    prepare = AsyncMock(return_value={"head_sha": "head", "base_sha": "base", "conflict_paths": []})
    pending = AsyncMock(return_value=("/wt/feat-bd-1", "feat/bd-1"))
    dispatch = AsyncMock()
    monkeypatch.setattr(worktree, "pending_integration", pending)
    monkeypatch.setattr(worktree, "prepare_integration", prepare)
    loop, _ = await _drive_with(monkeypatch, feature=feature, store=store, dispatch=dispatch, open_pr=AsyncMock())
    pending.assert_not_awaited()
    prepare.assert_not_awaited()
    dispatch.assert_not_awaited()
    assert not store.calls

    # Stop at the dispatch seam after proving the real keep-worktree path integrates.
    class Dispatched(BaseException):
        pass

    dispatch.side_effect = Dispatched
    monkeypatch.setattr(worktree, "preserved_recovery_progress", AsyncMock(return_value=False))
    monkeypatch.setattr(worktree, "recovery_context", AsyncMock(return_value={}))
    monkeypatch.setattr(worktree, "recovery_round_start", AsyncMock(return_value="head"))
    monkeypatch.setattr(worktree, "recovery_edit_state", AsyncMock(return_value={}))
    monkeypatch.setattr(worktree, "tree_exists", lambda path: True)
    store.list_features = lambda state=None: [dict(feature)] if state == feature["board_state"] else []
    store.ready_queue = lambda **kw: [dict(feature)] if feature["board_state"] == "ready" else []
    store.requeue = lambda fid: feature.update(board_state="ready")

    def claim(fid, **kw):
        feature["board_state"] = "in_progress"
        return dict(feature)

    store.claim = claim
    loop.auto_rebase = True
    monkeypatch.setattr(loop, "_maybe_reconcile", loop._reconcile_prs)
    for name in ("_maybe_sweep", "_maybe_preflight", "_host_disk_tick", "_page_agent_panel_verdict"):
        monkeypatch.setattr(loop, name, AsyncMock())
    monkeypatch.setattr(loop, "_reconcile_external_review", AsyncMock(return_value=False))
    monkeypatch.setattr(loop, "_budget_get", AsyncMock(return_value=0))
    monkeypatch.setattr(loop, "_budget_set", AsyncMock())
    monkeypatch.setattr(worktree, "pr_review_state", AsyncMock(return_value={"state": "OPEN"}))
    monkeypatch.setattr(worktree, "pr_merge_state", AsyncMock(return_value="DIRTY"))
    monkeypatch.setattr(worktree, "rebase_onto_base", AsyncMock(return_value=("conflict", "a.py")))
    assert not await loop._tick()
    prepare.assert_not_awaited()
    dispatch.assert_not_awaited()
    feature.update(labels=[], worktree_hold=False)
    assert await loop._tick()
    with pytest.raises(Dispatched):
        await asyncio.gather(*loop._drives)
    prepare.assert_awaited_once_with("/wt/feat-bd-1", "feat/bd-1", "main")
    dispatch.assert_awaited_once()


async def test_ready_hold_is_quiet_and_not_a_livelock(monkeypatch, caplog):
    card = {**_ready("bd-held", []), "labels": [LABEL_WORKTREE_HOLD]}
    store = _ClaimStore([card])
    store.get_feature = lambda fid: card
    loop = BoardLoop({"coder": "proto", "max_pending_reviews": 0})
    monkeypatch.setattr(loop, "_store", lambda: store)
    block = Mock()
    page = Mock()
    monkeypatch.setattr(loop, "_flag_blocked", block)
    monkeypatch.setattr(loop, "_notify_operator", page)
    with caplog.at_level(logging.INFO):
        for _ in range(8):
            await loop._spawn_ready()
    assert not store.claimed
    assert not _is_livelock_skip_reason("worktree-hold")
    block.assert_not_called()
    page.assert_not_called()
    assert len([r for r in caplog.records if "worktree held for manual work" in r.message]) == 1
    card["labels"] = []
    drive = AsyncMock()
    monkeypatch.setattr(loop, "_drive", drive)
    await loop._spawn_ready()
    await asyncio.gather(*loop._drives)
    assert store.claimed == ["bd-held"]
    drive.assert_awaited_once()
    card["labels"] = [LABEL_WORKTREE_HOLD]
    with caplog.at_level(logging.INFO):
        assert loop._worktree_held(card)
    assert len([r for r in caplog.records if "worktree held for manual work" in r.message]) == 2


async def test_reconcile_base_refresh_only_runs_git_for_unheld_card(monkeypatch):
    cards = [
        {**FEATURE, "id": fid, "labels": labels}
        for fid, labels in [("bd-held", [LABEL_WORKTREE_HOLD]), ("bd-free", [])]
    ]
    store = Mock()
    store.get_feature.side_effect = lambda fid: next(f for f in cards if f["id"] == fid)
    loop = BoardLoop({"coder": "proto"})
    runner = AsyncMock(return_value=(0, "", ""))
    monkeypatch.setattr(worktree, "_git", runner)
    monkeypatch.setattr(worktree, "pr_merge_state", AsyncMock(return_value="BEHIND"))

    async def rebase(repo, branch, base, **kw):
        await worktree._git(f"/wt/{branch}", "merge", base)
        return "clean", ""

    monkeypatch.setattr(worktree, "rebase_onto_base", rebase)
    for card in cards:
        await loop._maybe_rebase(store, card, "pr", "/repo")
    assert runner.await_count == 1
    assert "bd-free" in runner.call_args.args[0]
    assert all("bd-held" not in call.args[0] for call in runner.call_args_list)
    store.flag_blocked.assert_not_called()


@pytest.mark.parametrize("entry", ["orphan", "resume", "verified", "salvage", "sweep", "pr", "verify", "blocked"])
async def test_recovery_entries_leave_held_work_alone(monkeypatch, entry):
    card = {**FEATURE, "labels": [LABEL_WORKTREE_HOLD], "board_state": "blocked", "pr_url": "pr"}
    store = Mock()
    store.get_feature.return_value = card
    store.list_features.return_value = [card]
    loop = BoardLoop({"coder": "proto", "local_gate_cmd": "test"})
    monkeypatch.setattr(loop, "_store", lambda: store)
    git = AsyncMock(side_effect=AssertionError("held tree touched"))
    monkeypatch.setattr(worktree, "_git", git)
    page = Mock()
    monkeypatch.setattr(loop, "_notify_operator", page)
    monkeypatch.setattr(worktree, "list_feature_worktrees", lambda *args: [card["id"]])
    calls = {
        "orphan": lambda: loop._reconcile_orphan(card["id"]),
        "resume": lambda: loop._resume_stranded_work(store, card["id"]),
        "verified": lambda: loop._salvage_verified_candidate(store, card["id"]),
        "salvage": lambda: loop._salvage(card["id"], force=True, tree="", rec={}),
        "sweep": lambda: loop._sweep_worktrees(store, "/repo"),
        "pr": lambda: loop._reconcile_pr(store, card),
        "verify": lambda: loop._verify_merged_state(store, card, "pr", "/repo"),
        "blocked": lambda: loop._recover_blocked(store),
    }
    await calls[entry]()
    git.assert_not_awaited()
    store.flag_blocked.assert_not_called()
    store.requeue.assert_not_called()
    page.assert_not_called()


async def test_stale_snapshots_and_failed_reads(monkeypatch):
    loop = BoardLoop({"coder": "proto"})
    store = Mock()
    stale = {"id": "bd-1", "labels": []}
    store.get_feature.return_value = {**stale, "worktree_hold": True}
    assert await loop._worktree_hold_current(store, stale)
    store.get_feature.return_value = stale
    assert not await loop._worktree_hold_current(store, {**stale, "worktree_hold": True})
    store.get_feature.side_effect = RuntimeError("store unavailable")
    assert await loop._worktree_hold_current(store, stale)
    store.get_feature.side_effect = None
    store.get_feature.return_value = None
    assert await loop._worktree_hold_current(store, {**stale, "worktree_hold": True})


@pytest.mark.parametrize("entry", ["blocked", "review"])
@pytest.mark.parametrize("read_fails", [False, True])
async def test_review_and_escalation_preserve_known_holds(monkeypatch, entry, read_fails):
    from tests.test_loop import _BlockedStore, _blocked

    card = {**_blocked("bd-held", "auth", reason=""), "labels": [LABEL_WORKTREE_HOLD]}
    store = _BlockedStore([card])
    loop = BoardLoop({"coder": "proto"})
    if read_fails:
        # An earlier successful read observed the hold; a transient failure must
        # not erase that knowledge even on paths that allow missing card context.
        assert loop._worktree_held(card)
        store.get_feature = Mock(side_effect=RuntimeError("br hiccup"))
    else:
        store.get_feature = Mock(return_value=card)
    page = Mock()
    review = AsyncMock()
    monkeypatch.setattr(loop, "_notify_operator", page)
    monkeypatch.setattr(loop, "_run_review_workflow", review)
    if entry == "blocked":
        await loop._recover_blocked(store)
    else:
        await loop._review_gate(store, card["id"], "pr", "/repo")
    page.assert_not_called()
    review.assert_not_awaited()


async def test_missing_context_fallback_does_not_authorize_worktree_access():
    loop = BoardLoop({"coder": "proto"})
    store = Mock()
    store.get_feature.side_effect = RuntimeError("br hiccup")
    card = {"id": "bd-1", "labels": []}
    assert not await loop._worktree_hold_current(store, card, fail_closed=False)
    assert await loop._worktree_hold_current(store, card)
    assert await loop._worktree_hold_current(
        store, {**card, "labels": [LABEL_WORKTREE_HOLD]}, fail_closed=False
    )


def test_location_sweep_does_not_probe_held_card(monkeypatch):
    from project_board import work_location_sweep as sweep

    store = Mock()
    store.list_features.return_value = [
        {"id": "held", "labels": [LABEL_WORKTREE_HOLD]},
        {"id": "free", "labels": []},
    ]
    store.list_work_locations.return_value = {"entries": []}
    sweep.Sweep().check(store, Mock())
    store.list_work_locations.assert_called_once_with("free")


def test_preflight_does_not_block_manual_owner(monkeypatch):
    store = Mock()
    store.list_features.return_value = [{"id": "held", "labels": [LABEL_WORKTREE_HOLD]}]
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop, "_store", lambda: store)
    loop._hold_ready_for_preflight()
    store.flag_blocked.assert_not_called()


async def test_hold_acquired_while_preparing_round_stops_git_and_dispatch(monkeypatch):
    card = {**FEATURE, "pr_url": "pr"}
    store = FakeLoopStore()
    store.get_feature = lambda fid: dict(card)
    pending = AsyncMock()
    dispatch = AsyncMock()
    monkeypatch.setattr(worktree, "pending_integration", pending)

    async def lessons(feature):
        card["labels"] = [LABEL_WORKTREE_HOLD]
        return ""

    def seed(loop):
        monkeypatch.setattr(loop, "_fetch_kg_lessons", lessons)

    await _drive_with(monkeypatch, feature=card, store=store, seed=seed, dispatch=dispatch, open_pr=AsyncMock())
    pending.assert_not_awaited()
    dispatch.assert_not_awaited()
    assert "flag_blocked" not in store.names()
