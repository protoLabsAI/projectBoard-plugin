"""A shipped PR wins over every blocked reason, including stale loop holds (#653)."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from project_board import worktree
from project_board.loop import BoardLoop
from project_board.store import BeadsBoard


PR = "https://github.com/protoLabsAI/protoAgent/pull/4581"
FID = "bd-jq56.6"


class BlockedStore(BeadsBoard):
    """In-memory br seam; exercise the real record_merge and close/rollback edge."""

    def __init__(self, block_class):
        self.feature = {
            "id": FID,
            "pr_url": PR,
            "board_state": "blocked",
            "blocked": True,
            "blocked_reason": f"{block_class}: automated attempts exhausted",
            "labels": [
                "blocked",
                f"blocked-class:{block_class}",
                "in-review",
                "review-pending",
                "changes-requested",
                "skip-review-gate",
                "budget:merged-verify:5",
            ],
        }
        self.comments = []
        self.commands = []
        self.cleared = []

    def list_features(self, state=None):
        return [deepcopy(self.feature)] if state == self.feature["board_state"] else []

    def get_feature(self, fid):
        assert fid == FID
        return deepcopy(self.feature)

    def _find_by_external_ref(self, ref):
        assert ref == PR
        row = deepcopy(self.feature)
        row.pop("blocked_reason")  # br list may not carry the comment thread
        return row

    def _open_blockers(self, fid):
        return []

    def _run(self, *args, **kwargs):
        self.commands.append(args)
        if args[0] == "comments":
            self.comments.append(args[-1])
        elif args[0] == "update":
            for op, label in zip(args[2::2], args[3::2]):
                assert op == "--remove-label"
                self.feature["labels"].remove(label)
        elif args[0] == "close":
            self.feature.update(board_state="done", blocked=False, blocked_reason="")
        else:
            raise AssertionError(args)

    def clear_budgets(self, fid, kinds=None):
        self.cleared.append(fid)
        return self.get_feature(fid)


@pytest.mark.parametrize("block_class", ["attempt-exhausted", "terminal", "review-budget", "too-wide", "manual"])
@pytest.mark.parametrize(
    "hold", ["", "a drive holds it", "its review gate is running", "review is no longer running", "busy repo"]
)
@pytest.mark.parametrize("state", ["MERGED", "OPEN", "CLOSED"])
async def test_blocked_pr_reconciles_only_merged(monkeypatch, block_class, hold, state):
    store = BlockedStore(block_class)
    before = deepcopy(store.feature)
    monkeypatch.setattr("project_board.loop.get_store", lambda **kwargs: store)
    loop = BoardLoop({"auto_rebase": True, "ci_poll": True, "review_gate": True, "auto_merge": True})
    monkeypatch.setattr(loop, "_repo_for", lambda feature: "/repos/protoAgent")
    monkeypatch.setattr(loop, "_card_held", lambda fid: hold if hold != "busy repo" else "")
    read = AsyncMock(return_value=state)
    reap = AsyncMock()
    monkeypatch.setattr(worktree, "pr_state", read)
    monkeypatch.setattr(worktree, "reap_feature_worktree", reap)
    forbidden = AsyncMock(side_effect=AssertionError("blocked cards must not run review or OPEN gates"))
    monkeypatch.setattr(worktree, "pr_review_state", forbidden)
    for name in (
        "_reconcile_external_review",
        "_maybe_rebase",
        "_verify_merged_state",
        "_reconcile_ci",
        "_reconcile_review_threads",
        "_maybe_auto_merge",
    ):
        monkeypatch.setattr(loop, name, forbidden)
    lock = loop._repo_lock("/repos/protoAgent")
    if hold == "busy repo":
        await lock.acquire()
        loop._card_slots = asyncio.Semaphore(0)
    try:
        await asyncio.wait_for(loop._reconcile_prs(), timeout=2)
    finally:
        if lock.locked():
            lock.release()
    read.assert_awaited_once_with(PR, cwd="/repos/protoAgent")
    forbidden.assert_not_awaited()
    if state == "MERGED":
        assert store.feature["board_state"] == "done"
        assert not store.feature["blocked"]
        assert "blocked" not in store.feature["labels"]
        assert not any(label.startswith("blocked-class:") for label in store.feature["labels"])
        assert len(store.comments) == 1
        assert PR in store.comments[0]
        assert before["blocked_reason"] in store.comments[0]
        assert store.cleared == [FID]
        reap.assert_awaited_once_with("/repos/protoAgent", loop.root, FID)
        # Repeated reconciles and webhook delivery are idempotent.
        await loop._reconcile_prs()
        store.record_merge(pr_url=PR)
        assert len(store.comments) == 1
    else:
        assert store.feature == before
        assert store.commands == []
        assert store.cleared == []
        reap.assert_not_awaited()
