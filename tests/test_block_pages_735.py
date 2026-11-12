"""Block pages stay short and follow the card's lifecycle, including retry paths."""

from copy import deepcopy
from types import SimpleNamespace
import sys

import pytest

from project_board import block_pages, loop as loop_mod
from project_board.loop import BoardLoop
from project_board.store import BeadsBoard, BoardError


@pytest.fixture
def inbox(monkeypatch, tmp_path):
    rows = []

    class Inbox:
        def __init__(self, path, **kwargs):
            pass

        def add(self, text, *, priority="next", source="", dedup_key=""):
            if any(r["dedup_key"] == dedup_key and not r["delivered_at"] for r in rows):
                return None
            row = dict(
                id=len(rows) + 1, text=text, source=source, priority=priority, dedup_key=dedup_key, delivered_at=None
            )
            rows.append(row)
            return row.copy()

        def list(self, *, priority_floor, include_recovery_claimed, limit):
            assert include_recovery_claimed
            return [r.copy() for r in rows if not r["delivered_at"]][:limit]

        def mark_delivered(self, ids):
            for row in rows:
                if row["id"] in ids:
                    row["delivered_at"] = "now"

    monkeypatch.setitem(sys.modules, "inbox", SimpleNamespace(InboxStore=Inbox))
    monkeypatch.setattr(loop_mod, "_inbox_db_path", lambda: tmp_path / "agent.db")
    return rows


class Board(BeadsBoard):
    """Run real transition methods over a minimal beads command seam."""

    def __init__(self):
        self.repo, self.db, self.actor = ".", None, "operator"
        self.bead = dict(
            id="bd-test",
            title="Fix the build",
            status="in_progress",
            labels=["blocked", "blocked-class:terminal"],
            issue_type="feature",
        )
        self.reason = "DO NOT UNBLOCK _finding_identity file.py:123 " * 40
        self.fail_close = False

    def get_feature(self, fid):
        return dict(
            deepcopy(self.bead),
            board_state=self.board_state(self.bead),
            blocked="blocked" in self.bead["labels"],
            blocked_reason=self.reason,
            blocked_class="terminal",
        )

    def _run(self, *args, **kwargs):
        if args[0] == "close":
            if self.fail_close:
                raise BoardError("close failed")
            self.bead["status"] = "closed"
        elif args[0] == "update":
            for i, arg in enumerate(args):
                if arg == "--remove-label":
                    self.bead["labels"] = [v for v in self.bead["labels"] if v != args[i + 1]]
                elif arg == "--add-label" and args[i + 1] not in self.bead["labels"]:
                    self.bead["labels"].append(args[i + 1])
                elif arg == "--status":
                    self.bead["status"] = args[i + 1]

    def _open_blockers(self, fid):
        return []

    def _find_by_external_ref(self, ref):
        return self.get_feature("bd-test")


def notify(board):
    BoardLoop({"coder": "proto"})._notify_operator(
        "bd-test", board.reason, incident="terminal|same failure|0", store=board
    )


def test_short_page_and_duplicate_across_restart(inbox):
    board = Board()
    board.bead["title"] *= 20
    notify(board)
    notify(board)  # fresh loop instance, same outstanding block
    assert len(inbox) == 1
    text = inbox[0]["text"]
    assert len(text) <= 200
    assert "bd-test" in text and "Fix the build" in text and "open the card" in text
    assert "/plugins/project_board/board?card=bd-test" in text
    assert "_finding_identity" not in text
    assert board.get_feature("bd-test")["blocked_reason"] == board.reason


@pytest.mark.parametrize("edge", ["clear_blocked", "mark_done", "cancel_feature", "record_merge"])
def test_each_exit_retires_outstanding_pages(inbox, edge):
    board = Board()
    notify(board)
    if edge == "record_merge":
        board.record_merge(pr_url="https://github.com/org/repo/pull/1")
    else:
        getattr(board, edge)("bd-test")
    assert all(row["delivered_at"] for row in inbox)


def test_same_reason_reblock_creates_one_new_page(inbox):
    board = Board()
    notify(board)
    board.clear_blocked("bd-test")
    board.flag_blocked("bd-test", board.reason, category="terminal", automatic=True)
    notify(board)
    notify(board)
    assert len(inbox) == 2
    assert inbox[0]["delivered_at"] and not inbox[1]["delivered_at"]


def test_failed_close_preserves_page(inbox):
    board = Board()
    notify(board)
    board.fail_close = True
    with pytest.raises(BoardError):
        board.mark_done("bd-test")
    assert not inbox[0]["delivered_at"]
    assert board.get_feature("bd-test")["board_state"] == "blocked"


@pytest.mark.parametrize("state", ["ready", "done", "cancelled"])
def test_sweep_retires_legacy_pages_but_preserves_other_alerts(inbox, state):
    api = block_pages.inbox_store()
    for key, text, source in [
        ("blocked:bd-test:old1", "Board card bd-test is blocked: wall of text", "project_board"),
        ("blocked:bd-test:old2", "Another block", "project_board"),
        ("blocked:bd-test:work", "Work location /repo: unpushed", "project_board"),
        ("blocked:bd-test:hold", "Board card bd-test is held: review override", "project_board"),
        ("blocked:bd-test2:other", "Other card", "project_board"),
        ("blocked:bd-test:foreign", "Other source", "foreign"),
    ]:
        api.add(text, source=source, dedup_key=key)
    board = SimpleNamespace(get_feature=lambda fid: dict(id=fid, board_state=state if fid == "bd-test" else "blocked"))
    block_pages.sweep(board)
    assert [bool(r["delivered_at"]) for r in inbox] == [True, True, False, False, False, False]


@pytest.mark.parametrize("missing", [True, False])
def test_uncertain_read_preserves_page(inbox, missing):
    notify(Board())

    def read(fid):
        if missing:
            return None
        raise BoardError("unavailable")

    block_pages.sweep(SimpleNamespace(get_feature=read))
    assert not inbox[0]["delivered_at"]


def test_cleanup_failure_retried_by_sweep(inbox, monkeypatch):
    board = Board()
    notify(board)
    cls = sys.modules["inbox"].InboxStore
    original = cls.mark_delivered
    monkeypatch.setattr(cls, "mark_delivered", lambda *a: (_ for _ in ()).throw(OSError("busy")))
    board.clear_blocked("bd-test")
    assert not inbox[0]["delivered_at"]
    monkeypatch.setattr(cls, "mark_delivered", original)
    block_pages.sweep(board)
    assert inbox[0]["delivered_at"]


def test_transition_between_read_and_insert_retires_page(inbox, monkeypatch):
    board = Board()
    cls = sys.modules["inbox"].InboxStore
    original = cls.add

    def add(self, *args, **kwargs):
        board.clear_blocked("bd-test")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(cls, "add", add)
    notify(board)
    assert inbox[0]["delivered_at"]


def test_link_opens_requested_card():
    from pathlib import Path

    script = (Path(__file__).parents[1] / "view/board.js").read_text()
    assert 'new URLSearchParams(window.location.search).get("card")' in script
    assert 'if (fid) openCard(fid, "card")' in script


def test_stale_transition_result_does_not_retire_reblock(inbox):
    board = Board()
    notify(board)

    @block_pages.after_transition
    def delayed_result(store):
        # Another caller already unblocked, re-blocked and paged before this
        # transition returned its older result to the cleanup hook.
        store.clear_blocked("bd-test")
        store.flag_blocked("bd-test", store.reason, category="terminal", automatic=True)
        notify(store)
        return dict(id="bd-test", board_state="ready")

    delayed_result(board)
    assert len(inbox) == 2
    assert inbox[0]["delivered_at"] and not inbox[1]["delivered_at"]


@pytest.mark.parametrize("edge", ["requeue", "record_verification"])
def test_task_exit_cleans_legacy_page(inbox, edge):
    board = Board()
    notify(board)
    board.bead.update(issue_type="task", labels=["in-review"])
    getattr(board, edge)("bd-test")
    assert inbox[0]["delivered_at"]


def test_review_hold_and_work_location_copy_and_keys_are_preserved(inbox):
    board = Board()
    loop = BoardLoop({"coder": "proto"})
    for incident, text in [
        ("gate-override|abc", "Board card bd-test is held: review override"),
        ("work-location:abc", "Work location /repo: unpushed"),
    ]:
        loop._notify_operator("bd-test", text, incident=incident, store=board)
        assert inbox[-1]["text"] == text
        assert inbox[-1]["dedup_key"].startswith("blocked:bd-test:")
    board.clear_blocked("bd-test")
    assert all(not r["delivered_at"] for r in inbox)
