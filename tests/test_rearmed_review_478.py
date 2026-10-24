"""Re-armed reviews run promptly or expose the scheduling obstacle (#478)."""

import asyncio
from unittest.mock import AsyncMock

import pytest

import project_board.loop as loop_mod
from project_board import worktree
from project_board.loop import BoardLoop
from project_board.store import BeadsBoard, annotate_next_action

FID = "bd-478"
PR = "https://github.com/o/r/pull/478"
HEAD_A = "a" * 40
HEAD_B = "b" * 40


class MemoryBoard(BeadsBoard):
    """Keep beads in memory while using the real projection and unblock operation."""

    def __init__(self):
        self.repo = "/repo"
        self.base_branch = "main"
        self.bead = {
            "id": FID,
            "status": "in_progress",
            "external_ref": PR,
            "labels": ["in-review", "review-clean", f"review-clean-sha:{HEAD_A[:12]}"],
            "comments": [],
        }

    def _require(self, fid):
        return self.get_feature(fid)

    def get_feature(self, fid):
        return self._project(self.bead)

    def list_features(self, state=None):
        feature = self.get_feature(FID)
        return [feature] if state is None or feature["board_state"] == state else []

    def _run(self, *args, **kwargs):
        assert args[:2] == ("update", FID)
        for i, arg in enumerate(args):
            if arg == "--remove-label":
                self.bead["labels"].remove(args[i + 1])

    def set_review_substate(self, fid, label, note="", head_sha=""):
        self.bead["labels"] = [
            x
            for x in self.bead["labels"]
            if x not in ("review-clean", "review-pending", "changes-requested")
            and not x.startswith("review-clean-sha:")
        ]
        if label:
            self.bead["labels"].append(label)
        if label == "review-clean" and head_sha:
            self.bead["labels"].append(f"review-clean-sha:{head_sha[:12]}")


@pytest.fixture
def setup_review(monkeypatch):
    board = MemoryBoard()
    loop = BoardLoop({"review_gate": True, "auto_merge": True, "merge_poll": False})
    monkeypatch.setattr(loop, "_store", lambda: board)
    monkeypatch.setattr(loop_mod, "live_loop", lambda: loop)
    monkeypatch.setattr(worktree, "pr_review_state", AsyncMock(return_value={"state": "OPEN"}))
    monkeypatch.setattr(worktree, "pr_head_sha", AsyncMock(return_value=HEAD_B))
    monkeypatch.setattr(worktree, "pr_merge_info", AsyncMock(return_value={"mergeStateStatus": "CLEAN"}))
    monkeypatch.setattr(worktree, "merge_pr", AsyncMock(return_value=False))
    runs = []

    async def review(store, fid, pr_url, repo):
        runs.append(await worktree.pr_head_sha(pr_url, cwd=repo))
        # Leave pending: the real merge blockers must keep this head from merging.

    monkeypatch.setattr(loop, "_review_gate_run", review)
    return board, loop, runs


def action(board, loop):
    return annotate_next_action([board.get_feature(FID)], loop.cfg)[0]["next_action"]


@pytest.mark.parametrize("after_unblock", [False, True])
async def test_external_push_runs_new_head_within_two_passes(setup_review, after_unblock):
    board, loop, runs = setup_review
    if after_unblock:
        board.bead["labels"].append("blocked")
        board.bead["comments"].append({"text": "blocked: old review failure"})
        assert board.get_feature(FID)["blocked_reason"] == "old review failure"
        board.clear_blocked(FID)
        assert board.get_feature(FID)["blocked_reason"] == ""
    await loop._reconcile_prs()
    assert "review-pending" in board.bead["labels"]
    await loop._reconcile_prs()
    assert runs == [HEAD_B]


@pytest.mark.parametrize("at_resume", [False, True])
@pytest.mark.parametrize("finished_task", [False, True])
async def test_stale_marker_is_visible_and_next_pass_retries(setup_review, at_resume, finished_task):
    board, loop, runs = setup_review
    board.set_review_substate(FID, "review-pending")
    loop._review_inflight.add(FID)
    if finished_task:
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        loop._review_tasks[FID] = task
    if at_resume:
        await loop._reconcile_pr(board, board.get_feature(FID))
    else:
        await loop._reconcile_prs()
    assert not runs
    assert FID not in loop._review_inflight
    assert action(board, loop).startswith("review not running: stale in-flight marker")
    await loop._reconcile_prs()
    assert runs == [HEAD_B]
    assert action(board, loop) == "review in progress"
    assert not loop._review_tasks


async def test_zombie_is_visible_until_it_finishes(setup_review):
    board, loop, runs = setup_review
    board.set_review_substate(FID, "review-pending")
    release = asyncio.Event()
    zombie = asyncio.create_task(release.wait())
    loop._review_zombies[FID] = zombie
    try:
        for _ in range(2):
            await loop._reconcile_prs()
            assert not runs
            assert action(board, loop) == "review not running: abandoned review call is still running"
        release.set()
        await zombie
        await loop._reconcile_prs()
        assert runs == [HEAD_B]
        assert action(board, loop) == "review in progress"
        assert FID not in loop._review_zombies
    finally:
        release.set()
        await zombie


async def test_live_owner_prevents_duplicate_review(setup_review, monkeypatch):
    board, loop, runs = setup_review
    board.set_review_substate(FID, "review-pending")
    entered, release = asyncio.Event(), asyncio.Event()

    async def review(*args):
        runs.append(HEAD_B)
        entered.set()
        await release.wait()

    monkeypatch.setattr(loop, "_review_gate_run", review)
    task = asyncio.create_task(loop._review_gate(board, FID, PR, "/repo"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await loop._review_gate(board, FID, PR, "/repo")
        await loop._reconcile_prs()
        assert runs == [HEAD_B]
        assert action(board, loop) == "review in progress"
    finally:
        release.set()
        await task
    assert FID not in loop._review_inflight
    assert FID not in loop._review_tasks


def test_unblocked_reason_is_empty_but_history_survives():
    board = MemoryBoard()
    board.bead["comments"] = [{"text": "blocked: first failure"}, {"text": "blocked: latest failure"}]
    board.bead["labels"].append("blocked")
    assert board.get_feature(FID)["blocked_reason"] == "latest failure"
    assert board.clear_blocked(FID)["blocked_reason"] == ""
    assert len(board.bead["comments"]) == 2
    board.bead["labels"].append("blocked")
    board.bead["comments"].append({"text": "blocked: new failure"})
    assert board.get_feature(FID)["blocked_reason"] == "new failure"


async def test_scheduling_reason_is_card_specific_and_only_for_pending_review(setup_review):
    board, loop, _ = setup_review
    board.set_review_substate(FID, "review-pending")
    loop._review_inflight.add(FID)
    await loop._review_gate(board, FID, PR, "/repo")
    sibling = {**board.get_feature(FID), "id": "bd-other"}
    rows = annotate_next_action([board.get_feature(FID), sibling], loop.cfg)
    assert rows[0]["next_action"].startswith("review not running:")
    assert rows[1]["next_action"] == "review in progress"
    board.set_review_substate(FID, "review-clean", head_sha=HEAD_B)
    assert not action(board, loop).startswith("review not running:")
