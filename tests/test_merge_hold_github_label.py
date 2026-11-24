"""The reconcile pass mirrors a bead's merge-hold onto the card's GitHub PR (bd-t266a).

The board's own merge edge honours the bead `merge-hold`, but pr-reviewer arms GitHub
auto-merge and only refuses when the GITHUB PR carries `merge-hold` — so four held cards
merged. The reconcile now labels the held PR, disarms an armed auto-merge, and removes only
the label it added once the hold is released. GitHub is a fake `gh` here; the real store
verbs run against a fake `br`.
"""

import json
from unittest.mock import AsyncMock

import pytest

from project_board import store, worktree
from project_board.loop import BoardLoop
from test_merge_hold_setter_532 import PR, _FakeBr, _bead

# Captured at import, before conftest's autouse stub replaces it for each test.
_REAL_HOLD_FACTS = worktree.pr_hold_facts


class _FakeGh:
    """A stateful fake `gh` for one PR: `pr view --json labels,autoMergeRequest` reads the
    state, `pr edit --add-label/--remove-label` and `pr merge --disable-auto` change it."""

    def __init__(self, labels=(), auto_merge=False):
        self.labels = list(labels)
        self.auto_merge = auto_merge
        self.calls = []
        self.fail = set()  # verbs ("add-label", "remove-label", "disable-auto") to refuse

    async def __call__(self, *args, cwd, timeout=60):
        self.calls.append(args)
        if args[:2] == ("pr", "view"):
            auto = {"mergeMethod": "SQUASH"} if self.auto_merge else None
            return 0, json.dumps({"labels": [{"name": n} for n in self.labels], "autoMergeRequest": auto}), ""
        if args[:2] == ("label", "create"):
            return 0, "", ""
        if args[:2] == ("pr", "edit"):
            flag, label = args[3], args[4]
            if flag.lstrip("-") in self.fail:
                return 1, "", "HTTP 403: Resource not accessible by integration"
            if flag == "--add-label" and label not in self.labels:
                self.labels.append(label)
            if flag == "--remove-label" and label in self.labels:
                self.labels.remove(label)
            return 0, "", ""
        if args[:2] == ("pr", "merge") and "--disable-auto" in args:
            if "disable-auto" in self.fail:
                return 1, "", "HTTP 403"
            self.auto_merge = False
            return 0, "", ""
        return 1, "", f"unexpected gh {args}"

    def writes(self):
        return [c for c in self.calls if c[:2] in (("pr", "edit"), ("pr", "merge"))]


@pytest.fixture
def gh(monkeypatch):
    fake = _FakeGh()
    monkeypatch.setattr(worktree, "_gh", fake)
    monkeypatch.setattr(worktree, "pr_hold_facts", _REAL_HOLD_FACTS)
    return fake


def _board(make_board, labels):
    br = _FakeBr([_bead("bd-1", status="in_progress", labels=["in-review", *labels])])
    return make_board(br), br


def _loop(monkeypatch):
    loop = BoardLoop({"auto_merge": False, "review_gate": False, "auto_rebase": False, "ci_poll": False})
    for name in ("_reconcile_external_review", "_reconcile_review_threads", "_page_agent_panel_verdict"):
        monkeypatch.setattr(loop, name, AsyncMock(return_value=False))
    monkeypatch.setattr(loop, "_release_board_draft", AsyncMock())
    return loop


async def _pass(loop, board):
    await loop._reconcile_pr(board, dict(board.get_feature("bd-1"), pr_url=PR), known_state="OPEN")


async def test_held_card_labels_pr_once_and_second_pass_writes_nothing(monkeypatch, make_board, gh):
    board, _br = _board(make_board, [store.LABEL_MERGE_HOLD])
    loop = _loop(monkeypatch)
    await _pass(loop, board)
    assert gh.writes() == [("pr", "edit", PR, "--add-label", "merge-hold")]
    assert ("label", "create", "merge-hold") == gh.calls[1][:3]  # created in the repo first
    assert store.LABEL_GH_MERGE_HOLD in board.get_feature("bd-1")["labels"]
    # r3: already labelled, not armed → a read only, no gh write.
    before = len(gh.writes())
    await _pass(loop, board)
    assert len(gh.writes()) == before


async def test_armed_auto_merge_is_disabled_once(monkeypatch, make_board, gh):
    gh.labels, gh.auto_merge = ["merge-hold"], True  # the label is there; GitHub armed anyway
    board, _br = _board(make_board, [store.LABEL_MERGE_HOLD, store.LABEL_GH_MERGE_HOLD])
    loop = _loop(monkeypatch)
    await _pass(loop, board)
    await _pass(loop, board)
    assert gh.writes() == [("pr", "merge", PR, "--disable-auto")]
    assert gh.auto_merge is False


async def test_unlabelled_and_armed_gets_both_writes(monkeypatch, make_board, gh):
    gh.auto_merge = True
    board, _br = _board(make_board, [store.LABEL_MERGE_HOLD])
    await _pass(_loop(monkeypatch), board)
    assert gh.writes() == [
        ("pr", "edit", PR, "--add-label", "merge-hold"),
        ("pr", "merge", PR, "--disable-auto"),
    ]


async def test_release_removes_only_the_label_the_board_added(monkeypatch, make_board, gh):
    board, _br = _board(make_board, [store.LABEL_MERGE_HOLD])
    loop = _loop(monkeypatch)
    await _pass(loop, board)
    assert gh.labels == ["merge-hold"]
    board.release_merge_hold("bd-1")
    await _pass(loop, board)
    assert gh.writes()[-1] == ("pr", "edit", PR, "--remove-label", "merge-hold")
    assert gh.labels == []
    assert store.LABEL_GH_MERGE_HOLD not in board.get_feature("bd-1")["labels"]
    # And it is settled: the next pass makes no gh call at all.
    calls = len(gh.calls)
    await _pass(loop, board)
    assert len(gh.calls) == calls


async def test_human_label_on_never_held_card_is_left_alone(monkeypatch, make_board, gh):
    gh.labels = ["merge-hold"]
    board, _br = _board(make_board, [])
    loop = _loop(monkeypatch)
    await _pass(loop, board)
    await _pass(loop, board)
    assert gh.calls == []  # not held, not marked: no read, no write
    assert gh.labels == ["merge-hold"]


async def test_human_label_on_held_card_is_not_claimed_by_the_board(monkeypatch, make_board, gh):
    gh.labels = ["merge-hold"]  # a human labelled the PR before the board held the card
    board, _br = _board(make_board, [store.LABEL_MERGE_HOLD])
    loop = _loop(monkeypatch)
    await _pass(loop, board)
    assert gh.writes() == []
    assert store.LABEL_GH_MERGE_HOLD not in board.get_feature("bd-1")["labels"]
    board.release_merge_hold("bd-1")
    await _pass(loop, board)
    assert gh.labels == ["merge-hold"]


async def test_failed_label_write_keeps_hold_warns_and_comments_once(monkeypatch, make_board, gh, caplog):
    gh.fail.add("add-label")
    board, br = _board(make_board, [store.LABEL_MERGE_HOLD])
    loop = _loop(monkeypatch)
    with caplog.at_level("INFO"):
        await _pass(loop, board)
        await _pass(loop, board)
    labels = board.get_feature("bd-1")["labels"]
    assert store.LABEL_MERGE_HOLD in labels
    assert store.LABEL_GH_MERGE_HOLD not in labels
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "merge-hold label" in r.getMessage()]
    assert len(warnings) == 1
    notes = [c for c in br.comments_for("bd-1") if c.startswith("merge-hold mirror:")]
    assert len(notes) == 1 and "403" in notes[0]
    # GitHub recovers: the next pass labels the PR.
    gh.fail.clear()
    await _pass(loop, board)
    assert gh.labels == ["merge-hold"]
    assert store.LABEL_GH_MERGE_HOLD in board.get_feature("bd-1")["labels"]


async def test_failed_remove_keeps_marker_and_retries(monkeypatch, make_board, gh):
    gh.labels = ["merge-hold"]
    gh.fail.add("remove-label")
    board, br = _board(make_board, [store.LABEL_GH_MERGE_HOLD])  # released, board's label
    loop = _loop(monkeypatch)
    await _pass(loop, board)
    assert store.LABEL_GH_MERGE_HOLD in board.get_feature("bd-1")["labels"]
    assert len([c for c in br.comments_for("bd-1") if c.startswith("merge-hold mirror:")]) == 1
    gh.fail.clear()
    await _pass(loop, board)
    assert gh.labels == []
    assert store.LABEL_GH_MERGE_HOLD not in board.get_feature("bd-1")["labels"]


async def test_unreadable_pr_writes_nothing(monkeypatch, make_board, gh):
    async def broken(*args, cwd, timeout=60):
        gh.calls.append(args)
        return 1, "", "HTTP 502"

    monkeypatch.setattr(worktree, "_gh", broken)
    board, _br = _board(make_board, [store.LABEL_MERGE_HOLD])
    await _pass(_loop(monkeypatch), board)
    assert all(c[:2] == ("pr", "view") for c in gh.calls)
    assert store.LABEL_MERGE_HOLD in board.get_feature("bd-1")["labels"]


async def test_pr_hold_facts_parses_auto_merge(gh):
    assert await worktree.pr_hold_facts(PR) == {"labels": [], "auto_merge": False}
    gh.labels, gh.auto_merge = ["merge-hold", "reviewer-of-record:board"], True
    assert await worktree.pr_hold_facts(PR) == {
        "labels": ["merge-hold", "reviewer-of-record:board"],
        "auto_merge": True,
    }
