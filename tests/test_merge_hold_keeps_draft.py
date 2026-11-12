"""An operator merge hold also holds the board's draft hand-off."""

from unittest.mock import AsyncMock

import pytest

from project_board import store, worktree
from project_board.loop import BoardLoop
from test_draft_until_review_691 import _DraftStore, _ready_recorder
from test_loop import (  # noqa: F401
    _gate_loop,
    _inject_fake_findings,
    _no_real_pr_head_sha,
    _no_real_review_status_seam,
)
from test_merge_hold_setter_532 import PR, _FakeBr, _bead, _client

HEAD = "abcdef123456" + "0" * 28


@pytest.mark.parametrize("held", [True, False])
async def test_clean_gate_respects_merge_hold(monkeypatch, held):
    _inject_fake_findings(monkeypatch)
    board = _DraftStore()
    board.labels = [store.LABEL_BOARD_DRAFT] + ([store.LABEL_MERGE_HOLD] if held else [])
    ready = _ready_recorder(monkeypatch)
    loop = _gate_loop(monkeypatch, "clean.\n```json\n[]\n```")
    await loop._review_gate(board, "bd-1", PR, "/repo")
    assert board.review_states[-1][0] == "review-clean"
    assert ready == ([] if held else [PR])
    assert (store.LABEL_BOARD_DRAFT in board.labels) is held


@pytest.fixture
def draft_board(make_board):
    return make_board(
        _FakeBr(
            [
                _bead(
                    "bd-1",
                    status="in_progress",
                    labels=[
                        "in-review",
                        "review-clean",
                        f"review-clean-sha:{HEAD[: store.SHORT_SHA_LEN]}",
                        store.LABEL_BOARD_DRAFT,
                        store.LABEL_MERGE_HOLD,
                    ],
                )
            ]
        )
    )


async def test_hold_logs_once_per_head(monkeypatch, draft_board, caplog):
    ready = _ready_recorder(monkeypatch)
    head = AsyncMock(return_value=HEAD)
    monkeypatch.setattr(worktree, "pr_head_sha", head)
    loop = BoardLoop({})
    with caplog.at_level("INFO"):
        for _ in range(2):
            await loop._release_board_draft(draft_board, "bd-1", PR, "/repo")
        head.return_value = "f" * 40
        await loop._release_board_draft(draft_board, "bd-1", PR, "/repo")
    assert caplog.text.count("draft kept: merge-hold") == 2
    assert ready == []


@pytest.mark.parametrize("auto_merge", [True, False])
async def test_delete_hold_releases_on_reconcile_then_merges(monkeypatch, draft_board, auto_merge):
    loop = BoardLoop({"auto_merge": auto_merge, "review_gate": True, "auto_rebase": False, "ci_poll": False})
    for name in ("_reconcile_external_review", "_reconcile_review_threads", "_reconcile_trusted_qa_pass"):
        monkeypatch.setattr(loop, name, AsyncMock(return_value=False))
    monkeypatch.setattr(worktree, "pr_head_sha", AsyncMock(return_value=HEAD))
    draft = True
    events = []

    async def ready(*args, **kwargs):
        nonlocal draft
        events.append("ready")
        draft = False
        return True

    async def info(*args, **kwargs):
        return {"isDraft": draft, "mergeStateStatus": "CLEAN"}

    async def merge(*args, **kwargs):
        assert not draft
        assert kwargs["expected_head"] == HEAD
        events.append("merge")
        return True, ""

    monkeypatch.setattr(worktree, "_promote_adopted_draft", ready)
    monkeypatch.setattr(worktree, "pr_merge_info", info)
    monkeypatch.setattr(worktree, "merge_pr", merge)
    monkeypatch.setattr(worktree, "reap_feature_worktree", AsyncMock())
    monkeypatch.setattr(worktree, "delete_remote_branch", AsyncMock(return_value=True))
    feature = dict(draft_board.get_feature("bd-1"), pr_url=PR)
    await loop._reconcile_pr(draft_board, feature, known_state="OPEN")
    assert events == []
    response = _client(draft_board, monkeypatch).delete("/api/plugins/project_board/features/bd-1/merge-hold")
    assert response.status_code == 200
    # Deliberately pass the old held snapshot: the release must read fresh state.
    await loop._reconcile_pr(draft_board, feature, known_state="OPEN")
    assert events == (["ready", "merge"] if auto_merge else ["ready"])
    assert store.LABEL_BOARD_DRAFT not in draft_board.get_feature("bd-1")["labels"]


@pytest.mark.parametrize("head", ["", "f" * 40, None])
async def test_release_requires_readable_current_head(monkeypatch, draft_board, head):
    draft_board.release_merge_hold("bd-1")
    ready = _ready_recorder(monkeypatch)
    monkeypatch.setattr(worktree, "pr_head_sha", AsyncMock(return_value=head))
    await BoardLoop({})._release_board_draft(draft_board, "bd-1", PR, "/repo", require_clean_head=True)
    assert ready == []
    assert store.LABEL_BOARD_DRAFT in draft_board.get_feature("bd-1")["labels"]


async def test_failed_promotion_retries_and_missing_card_is_safe(monkeypatch, draft_board):
    draft_board.release_merge_hold("bd-1")
    monkeypatch.setattr(worktree, "pr_head_sha", AsyncMock(return_value=HEAD))
    promote = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(worktree, "_promote_adopted_draft", promote)
    loop = BoardLoop({})
    await loop._release_board_draft(draft_board, "bd-1", PR, "/repo", require_clean_head=True)
    assert store.LABEL_BOARD_DRAFT in draft_board.get_feature("bd-1")["labels"]
    await loop._release_board_draft(draft_board, "bd-1", PR, "/repo", require_clean_head=True)
    assert store.LABEL_BOARD_DRAFT not in draft_board.get_feature("bd-1")["labels"]
    monkeypatch.setattr(draft_board, "get_feature", lambda fid: None)
    await loop._release_board_draft(draft_board, "bd-1", PR, "/repo", require_clean_head=True)
    assert promote.await_count == 2


@pytest.mark.parametrize("change", ["pin", "clean", "pending", "rejected"])
async def test_poll_requires_unambiguous_pinned_clean_verdict(monkeypatch, draft_board, change):
    draft_board.release_merge_hold("bd-1")
    labels = draft_board._run.beads["bd-1"]["labels"]
    if change == "pin":
        labels[:] = [x for x in labels if not x.startswith("review-clean-sha:")]
    elif change == "clean":
        labels.remove("review-clean")
    else:
        labels.append("review-pending" if change == "pending" else "changes-requested")
    ready = _ready_recorder(monkeypatch)
    monkeypatch.setattr(worktree, "pr_head_sha", AsyncMock(return_value=HEAD))
    await BoardLoop({})._release_board_draft(draft_board, "bd-1", PR, "/repo", require_clean_head=True)
    assert ready == []


@pytest.mark.parametrize("change", ["hold", "requeue", "deleted", "read_error"])
async def test_state_changes_and_read_failures_keep_draft(monkeypatch, draft_board, change):
    draft_board.release_merge_hold("bd-1")
    ready = _ready_recorder(monkeypatch)

    async def head(*args, **kwargs):
        if change == "hold":
            draft_board.set_merge_hold("bd-1", "test first")
        elif change == "requeue":
            draft_board._run.beads["bd-1"]["labels"].remove("in-review")
        elif change == "deleted":
            monkeypatch.setattr(draft_board, "get_feature", lambda fid: None)
        else:
            raise RuntimeError("GitHub unavailable")
        return HEAD

    monkeypatch.setattr(worktree, "pr_head_sha", head)
    await BoardLoop({})._release_board_draft(draft_board, "bd-1", PR, "/repo", require_clean_head=True)
    assert ready == []
