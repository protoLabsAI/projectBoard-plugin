"""#754: audited per-card worktree holds, independent of board state and merge holds."""

from copy import deepcopy

import pytest

from project_board import store


class FakeBr:
    def __init__(self, *, status="open", labels=()):
        self.bead = {"id": "bd-1", "title": "t", "status": status, "labels": list(labels), "comments": []}
        self.calls = []
        self.fail = None

    def __call__(self, *args, want_json=False, actor=""):
        self.calls.append((args, actor))
        if args[0] == self.fail:
            raise store.BoardError("write failed" if args[0] != "show" else "read failed")
        if args[0] == "show":
            if args[1] != self.bead["id"]:
                raise store.BoardNotFound("unknown feature")
            return [deepcopy(self.bead)]
        if args[0] == "comments":
            self.bead["comments"].append(args[3])
        elif args[0] == "update":
            if args[2] == "--add-label":
                self.bead["labels"].append(args[3])
            else:
                self.bead["labels"].remove(args[3])
        else:
            raise AssertionError(args)
        return ""

    @property
    def writes(self):
        return [(args, actor) for args, actor in self.calls if args[0] != "show"]


@pytest.mark.parametrize(
    "status, labels",
    [
        ("open", []),
        ("open", ["ready"]),
        ("open", ["blocked"]),
        ("in_progress", []),
        ("in_progress", ["in-review", "merge-hold"]),
    ],
)
def test_hold_round_trip_and_idempotency(make_board, status, labels):
    fake = FakeBr(status=status, labels=labels)
    board = make_board(fake)
    before = board.get_feature("bd-1")
    assert before["worktree_hold"] is False

    held = board.set_worktree_hold("bd-1", "squashing by hand", actor="agent")
    assert held["worktree_hold"] is True
    assert held["labels"] == labels + [store.LABEL_WORKTREE_HOLD]
    assert held["board_state"] == before["board_state"]
    assert board.get_feature("bd-1")["worktree_hold"] is True
    assert fake.writes == [
        (("comments", "add", "bd-1", "worktree-hold set: squashing by hand"), "agent"),
        (("update", "bd-1", "--add-label", "worktree-hold"), "agent"),
    ]
    assert board.set_worktree_hold("bd-1", "another reason") == held
    assert len(fake.writes) == 2
    assert fake.bead["comments"] == ["worktree-hold set: squashing by hand"]

    released = board.release_worktree_hold("bd-1", actor="operator")
    assert released["worktree_hold"] is False
    assert released["labels"] == labels
    assert released["board_state"] == before["board_state"]
    assert board.get_feature("bd-1")["worktree_hold"] is False
    assert fake.writes[2:] == [
        (("update", "bd-1", "--remove-label", "worktree-hold"), "operator"),
        (("comments", "add", "bd-1", "worktree-hold released"), "operator"),
    ]
    assert board.release_worktree_hold("bd-1") == released
    assert len(fake.writes) == 4
    assert fake.bead["comments"] == ["worktree-hold set: squashing by hand", "worktree-hold released"]


@pytest.mark.parametrize("reason", ["", "   "])
@pytest.mark.parametrize("labels", [[], ["worktree-hold"]])
def test_empty_reason_changes_nothing(make_board, reason, labels):
    fake = FakeBr(labels=labels)
    board = make_board(fake)
    before = deepcopy(fake.bead)
    with pytest.raises(store.BoardError, match="needs a reason"):
        board.set_worktree_hold("bd-1", reason)
    assert fake.bead == before
    assert fake.writes == []


@pytest.mark.parametrize("labels", [[], ["cancelled"], ["worktree-hold"], ["cancelled", "worktree-hold"]])
def test_terminal_card_changes_nothing(make_board, labels):
    fake = FakeBr(status="closed", labels=labels)
    board = make_board(fake)
    before = deepcopy(fake.bead)
    with pytest.raises(store.BoardError, match="terminal card"):
        board.set_worktree_hold("bd-1", "squashing by hand")
    assert fake.bead == before
    assert fake.writes == []


@pytest.mark.parametrize("method, args", [("set_worktree_hold", ("reason",)), ("release_worktree_hold", ())])
def test_unknown_id_changes_nothing(make_board, method, args):
    fake = FakeBr()
    board = make_board(fake)
    before = deepcopy(fake.bead)
    with pytest.raises(store.BoardError):
        getattr(board, method)("bd-missing", *args)
    assert fake.bead == before
    assert fake.writes == []


@pytest.mark.parametrize("actor, expected", [("store-agent", "store-agent"), ("", "operator")])
def test_default_actor(make_board, actor, expected):
    fake = FakeBr()
    board = make_board(fake)
    board.actor = actor
    board.set_worktree_hold("bd-1", "  manual rebase  ")
    board.release_worktree_hold("bd-1")
    assert all(who == expected for _, who in fake.writes)
    assert fake.bead["comments"][0] == "worktree-hold set: manual rebase"


def test_failed_audit_does_not_set_hold(make_board):
    fake = FakeBr()
    board = make_board(fake)
    fake.fail = "comments"
    with pytest.raises(store.BoardError, match="write failed"):
        board.set_worktree_hold("bd-1", "manual reset")
    assert board.get_feature("bd-1")["worktree_hold"] is False
    assert fake.bead["comments"] == []
    assert len(fake.writes) == 1


def test_failed_removal_does_not_record_release(make_board):
    fake = FakeBr(labels=["worktree-hold"])
    board = make_board(fake)
    fake.fail = "update"
    with pytest.raises(store.BoardError, match="write failed"):
        board.release_worktree_hold("bd-1")
    assert board.get_feature("bd-1")["worktree_hold"] is True
    assert fake.bead["comments"] == []


@pytest.mark.parametrize("labels", [["worktree-hold"], ["cancelled", "worktree-hold"]])
def test_terminal_card_can_be_released(make_board, labels):
    fake = FakeBr(status="closed", labels=labels)
    board = make_board(fake)
    assert board.release_worktree_hold("bd-1")["worktree_hold"] is False
    assert fake.bead["comments"] == ["worktree-hold released"]


def test_failed_label_add_preserves_reason_without_claiming_hold(make_board):
    fake = FakeBr(labels=["merge-hold"])
    board = make_board(fake)
    fake.fail = "update"
    with pytest.raises(store.BoardError, match="write failed"):
        board.set_worktree_hold("bd-1", "manual reset")
    assert fake.bead["labels"] == ["merge-hold"]
    assert fake.bead["comments"] == ["worktree-hold set: manual reset"]
    assert board.get_feature("bd-1")["worktree_hold"] is False


def test_failed_release_audit_propagates_after_removing_hold(make_board):
    fake = FakeBr(labels=["merge-hold", "worktree-hold"])
    board = make_board(fake)
    fake.fail = "comments"
    with pytest.raises(store.BoardError, match="write failed"):
        board.release_worktree_hold("bd-1")
    assert fake.bead["labels"] == ["merge-hold"]
    assert fake.bead["comments"] == []
    assert board.get_feature("bd-1")["worktree_hold"] is False


@pytest.mark.parametrize("method, args", [("set_worktree_hold", ("reason",)), ("release_worktree_hold", ())])
def test_failed_read_changes_nothing(make_board, method, args):
    fake = FakeBr(labels=["worktree-hold"])
    board = make_board(fake)
    before = deepcopy(fake.bead)
    fake.fail = "show"
    with pytest.raises(store.BoardError, match="read failed"):
        getattr(board, method)("bd-1", *args)
    assert fake.bead == before
    assert fake.writes == []
