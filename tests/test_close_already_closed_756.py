"""A stale merge snapshot must tolerate only a confirmed already-closed refusal."""

from unittest.mock import Mock

import pytest

from project_board.store import BeadsBoard, BoardError
from test_blocked_merged_653 import BlockedStore, FID
from test_integration import _ready_feature, requires_br


PR = "https://github.com/protoLabsAI/projectBoard-plugin/pull/756"


@pytest.fixture
def board(tmp_path):
    return BeadsBoard(repo=str(tmp_path), actor="test")


@requires_br
@pytest.mark.parametrize("blocked", [False, True])
def test_merge_with_stale_open_row_after_real_close(board, tmp_path, monkeypatch, blocked):
    fid = _ready_feature(board, tmp_path)["id"]
    board.claim(fid)
    board.open_review(fid, pr_url=PR)
    if blocked:
        board.flag_blocked(fid, "waiting on review", category="terminal")
    stale = board._find_by_external_ref(PR)
    assert stale["board_state"] != "done"
    board._run("close", fid, "-r", f"merged: {PR}")
    monkeypatch.setattr(board, "_find_by_external_ref", lambda ref: stale)
    comment = Mock()
    monkeypatch.setattr(board, "comment", comment)

    # Real br 0.2.16 stderr before the fix (generated id from the failing run):
    # Warning: Skipped test_merge_with_stale_open_row0-z0n: already closed
    # Error: Nothing to do: all 1 issue(s) skipped
    # Hint: All specified issues were already closed or not found.
    merged = board.record_merge(pr_url=PR)

    assert merged["id"] == fid
    assert merged["board_state"] == "done"
    assert merged["bead_status"] == "closed"
    assert "in-review" not in merged["labels"]
    assert "blocked" not in merged["labels"]
    comment.assert_not_called()


@pytest.mark.parametrize(
    "error_text, reread",
    [
        ("database is locked", "closed"),
        ("already closed", "open"),
        ("already closed", "missing"),
        ("already closed", "failed"),
    ],
)
def test_merge_preserves_other_close_errors(monkeypatch, error_text, reread):
    board = BlockedStore("terminal")
    stale = board._find_by_external_ref(board.feature["pr_url"])
    monkeypatch.setattr(board, "_find_by_external_ref", lambda ref: stale)
    error = BoardError(error_text)
    real_run = board._run
    real_get = board.get_feature

    def read(fid):
        if reread == "failed":
            raise BoardError("read failed")
        if reread == "missing":
            return None
        return dict(real_get(fid), bead_status=reread)

    def run(*args, **kwargs):
        if args[0] == "close":
            monkeypatch.setattr(board, "get_feature", read)
            raise error
        return real_run(*args, **kwargs)

    monkeypatch.setattr(board, "_run", run)
    with pytest.raises(BoardError) as caught:
        board.record_merge(pr_url=board.feature["pr_url"])
    assert caught.value is error
    assert board.commands == []  # No stale-label cleanup after an unconfirmed close.
    assert board.comments == []


def test_already_closed_refusal_is_case_insensitive(monkeypatch):
    board = BlockedStore("terminal")
    stale = board._find_by_external_ref(board.feature["pr_url"])
    monkeypatch.setattr(board, "_find_by_external_ref", lambda ref: stale)
    real_run = board._run

    def run(*args, **kwargs):
        if args[0] == "close":
            board.feature.update(board_state="done", bead_status="closed")
            raise BoardError("ALREADY CLOSED")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(board, "_run", run)
    merged = board.record_merge(pr_url=board.feature["pr_url"])
    assert merged["id"] == FID
    assert merged["board_state"] == "done"
    assert "blocked" not in merged["labels"]
    assert board.comments == []
