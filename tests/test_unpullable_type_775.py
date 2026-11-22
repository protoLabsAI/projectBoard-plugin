"""Refuse unclaimable cards at authoring edges; explain legacy ready cards (#775)."""

import json

import pytest

from project_board.loop import BoardLoop
from project_board.store import BoardError
from test_api import _client
from test_store import Br


class Beads(Br):
    """The store-test br seam, with writes persisted for end-to-end projections."""

    def __init__(self):
        super().__init__()
        self.rows = {}
        self.returns = {
            "create": self.create,
            "update": self.update,
            "show": self.show,
            "ready": lambda _: [r.copy() for r in self.rows.values() if "ready" in r["labels"]],
        }

    def create(self, args):
        fid = f"bd-{len(self.rows) + 1}"
        self.rows[fid] = {
            "id": fid,
            "title": args[1],
            "issue_type": args[args.index("--type") + 1],
            "status": "open",
            "labels": [],
            "description": args[args.index("--description") + 1] if "--description" in args else "",
        }
        return fid

    def update(self, args):
        row = self.rows[args[1]]
        for i, arg in enumerate(args[2:], 2):
            if arg.startswith("--") and "=" in arg:
                key, value = arg[2:].split("=", 1)
                row[key.replace("-", "_")] = value
            elif arg == "--add-label":
                row["labels"].append(args[i + 1])
        return ""

    def show(self, args):
        return [self.rows[fid].copy() for fid in args[1:]]


def authored(board, issue_type="feature"):
    return board.create_feature(
        "Repair",
        issue_type=issue_type,
        spec="Repair the parser",
        acceptance_criteria="The parser accepts the input",
        files_to_modify=["parser.py (new)"],
        chain_hot_files=False,
    )


def legacy(board, beads, issue_type="bug", ready=False):
    card = authored(board)
    beads.rows[card["id"]]["issue_type"] = issue_type
    if ready:
        beads.rows[card["id"]]["labels"].append("ready")
    return card["id"]


def message(issue_type):
    return f"issue_type {issue_type!r} is never claimed by the board; use one of: feature, task"


@pytest.mark.parametrize("issue_type", ["bug", "chore", "epic", "milestone"])
def test_bug_ready_reproduction(make_board, issue_type):
    # Before #775 this create + mark_ready reached ready, but ready_queue was empty.
    beads = Beads()
    board = make_board(beads)
    with pytest.raises(BoardError) as exc:
        card = authored(board, issue_type)
        board.mark_ready(card["id"])
    assert str(exc.value) == message(issue_type)
    assert beads.cmds("create") == []
    assert beads.rows == {}


@pytest.mark.parametrize("issue_type", ["feature", "task"])
def test_pullable_creation_and_ready(make_board, issue_type):
    board = make_board(Beads())
    card = authored(board, issue_type)
    board.mark_ready(card["id"])
    assert [f["id"] for f in board.ready_queue()] == [card["id"]]


def test_structural_creation(make_board):
    board = make_board(Beads())
    epic = board.create_epic("Epic")
    milestone = board.create_milestone("Milestone", epic["id"])
    assert epic["issue_type"] == "epic"
    assert milestone["issue_type"] == "milestone"


@pytest.mark.parametrize("issue_type", ["bug", "chore"])
@pytest.mark.parametrize("prepare", ["mark_ready", "_prepare_ready"])
def test_legacy_backlog_refused(make_board, issue_type, prepare):
    beads = Beads()
    board = make_board(beads)
    fid = legacy(board, beads, issue_type)
    before = len(beads.cmds("update"))
    with pytest.raises(BoardError) as exc:
        getattr(board, prepare)(fid)
    assert str(exc.value) == message(issue_type)
    assert board.get_feature(fid)["board_state"] == "backlog"
    assert len(beads.cmds("update")) == before


def test_api_returns_400(make_board, monkeypatch):
    beads = Beads()
    board = make_board(beads)
    with _client(monkeypatch, board) as client:
        response = client.post("/api/plugins/project_board/features", json={"title": "Bug", "issue_type": "bug"})
        assert response.status_code == 400
        assert response.json()["detail"] == message("bug")
        assert beads.cmds("create") == []
        fid = legacy(board, beads)
        response = client.post(f"/api/plugins/project_board/features/{fid}/ready")
        assert response.status_code == 400
        assert response.json()["detail"] == message("bug")
        assert board.get_feature(fid)["board_state"] == "backlog"


async def test_legacy_ready_skip_and_order(make_board, monkeypatch, caplog):
    beads = Beads()
    board = make_board(beads)
    bug = legacy(board, beads, ready=True)
    legacy(board, beads, "epic", ready=True)
    task = legacy(board, beads, "task", ready=True)
    legacy(board, beads, "milestone", ready=True)
    feature = legacy(board, beads, "feature", ready=True)
    monkeypatch.setattr("project_board.loop.get_store", lambda **_: board)
    loop = BoardLoop({"max_concurrent": 2})
    # Reserve the valid cards so the real scan runs without dispatching work.
    loop._inflight_files = {task: set(), feature: set()}
    with caplog.at_level("INFO", logger="protoagent.plugins.project_board"):
        assert await loop._spawn_ready() is False
    payload = json.loads(next(m.split("claim_decision", 1)[1] for m in caplog.messages if "claim_decision" in m))
    assert payload["skipped"] == [
        {"fid": bug, "reason": "unpullable-type", "issue_type": "bug"},
        {"fid": task, "reason": "reserved"},
        {"fid": feature, "reason": "reserved"},
    ]
    before = len(beads.calls)
    assert [f["id"] for f in board.ready_queue()] == [task, feature]
    assert beads.calls[before:] == [("ready", "--label", "ready", "--limit", "0")]
    beads.rows[bug]["labels"] = []
    board.ready_queue()
    assert board.ready_queue_unpullable == []
    # A failed scan must not leave stale diagnostic rows behind.
    beads.rows[bug]["labels"] = ["ready"]
    board.ready_queue()

    def fail(_):
        raise BoardError("read failed")

    beads.returns["ready"] = fail
    with pytest.raises(BoardError, match="read failed"):
        board.ready_queue()
    assert board.ready_queue_unpullable == []


async def test_only_unpullable_ready_still_emits_decision(make_board, monkeypatch):
    beads = Beads()
    board = make_board(beads)
    fid = legacy(board, beads, "chore", ready=True)
    # Rows without an id cannot identify a skipped card.
    beads.returns["ready"] = lambda _: [beads.rows[fid], {"issue_type": "bug"}]
    monkeypatch.setattr("project_board.loop.get_store", lambda **_: board)
    loop = BoardLoop({"max_concurrent": 2})
    assert await loop._spawn_ready() is False
    assert loop._last_claim_decision["skipped"] == [{"fid": fid, "reason": "unpullable-type", "issue_type": "chore"}]
    beads.returns["ready"] = []
    assert await loop._spawn_ready() is False
    assert loop._last_claim_decision["skipped"] == []
