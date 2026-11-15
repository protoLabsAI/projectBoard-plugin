"""A card that finishes tells the host: ``task.link.closed`` (#765).

Core's task auto-close and directive auto-fulfil both subscribe to this topic and
nothing published it, so a task linked to a card never resolved (task-59 sat in
"Doing now" for good). These tests pin both halves: the publish on each terminal
edge, and the backfill sweep for cards that went terminal without one.
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest

from project_board import link_events
from project_board.store import BeadsBoard, BoardError


@pytest.fixture
def bus(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(link_events, "_default_publish", lambda: lambda topic, data: events.append((topic, data)))
    return events


class Board(BeadsBoard):
    """Real transition methods over a minimal fake ``br`` seam (the #735 harness)."""

    def __init__(self, status="in_progress", labels=("in-review",)):
        self.repo, self.db, self.actor = ".", None, "operator"
        self.bead = dict(
            id="bd-test",
            title="Fix the build",
            status=status,
            labels=list(labels),
            issue_type="feature",
            pr_url="https://github.com/org/repo/pull/42",
        )
        self.fail_close = False

    def get_feature(self, fid):
        return dict(deepcopy(self.bead), board_state=self.board_state(self.bead), blocked=False)

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


def test_merge_publishes_link_closed_with_pr_number(bus):
    Board().record_merge(pr_url="https://github.com/org/repo/pull/42")
    assert bus == [("task.link.closed", {"ref": "bd-test", "reason": "merged (#42)"})]


def test_manual_done_publishes(bus):
    Board().mark_done("bd-test", reason="shipped by hand")
    assert bus == [("task.link.closed", {"ref": "bd-test", "reason": "merged (#42)"})]


def test_cancel_publishes_cancelled(bus):
    Board().cancel_feature("bd-test", "duplicate")
    assert bus == [("task.link.closed", {"ref": "bd-test", "reason": "cancelled"})]


def test_failed_close_publishes_nothing(bus):
    board = Board()
    board.fail_close = True
    with pytest.raises(BoardError):
        board.mark_done("bd-test")
    assert bus == []


def test_no_host_bus_never_breaks_the_transition(monkeypatch):
    def boom(topic, data):
        raise RuntimeError("bus down")

    monkeypatch.setattr(link_events, "_default_publish", lambda: boom)
    assert Board().mark_done("bd-test")["board_state"] == "done"


def test_reason_without_pr_is_done():
    assert link_events.reason_for({"id": "bd-x", "board_state": "done"}) == "done"


# ── backfill sweep ─────────────────────────────────────────────────────────────


@pytest.fixture
def host_tasks(monkeypatch):
    """A fake host task store plus the ``tasks.store.parse_links`` the sweep imports."""

    def parse_links(row):
        return json.loads(row.get("links") or "[]")

    monkeypatch.setitem(sys.modules, "tasks", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "tasks.store", SimpleNamespace(parse_links=parse_links))
    rows = [
        {"id": "task-59", "links": json.dumps([{"ref": "bd-done", "kind": "card", "state": "open"}])},
        {
            "id": "task-70",
            "links": json.dumps(
                [
                    {"ref": "bd-done", "kind": "card", "state": "open"},
                    {"ref": "bd-cancelled", "kind": "card", "state": "open"},
                    {"ref": "bd-live", "kind": "card", "state": "open"},
                    {"ref": "bd-resolved", "kind": "card", "state": "done"},
                    {"ref": "bd-gone", "kind": "card", "state": "open"},
                    {"ref": "bd-unreadable", "kind": "card", "state": "open"},
                    {"ref": "https://github.com/org/repo/pull/1", "kind": "pr", "state": "open"},
                ]
            ),
        },
    ]
    return SimpleNamespace(list=lambda include_closed: rows)


def _cards(fid):
    if fid == "bd-unreadable":
        raise BoardError("br show timed out")
    return {
        "bd-done": {"id": "bd-done", "board_state": "done", "pr_url": "https://github.com/o/r/pull/7"},
        "bd-cancelled": {"id": "bd-cancelled", "board_state": "cancelled"},
        "bd-live": {"id": "bd-live", "board_state": "in_review"},
        "bd-resolved": {"id": "bd-resolved", "board_state": "done"},
    }.get(fid)


def test_sweep_backfills_terminal_cards_once_each(host_tasks):
    events = []
    n = link_events.sweep(
        SimpleNamespace(get_feature=_cards),
        tasks_store=host_tasks,
        publish=lambda t, d: events.append((t, d)),
        force=True,
    )
    assert n == 2
    assert events == [
        ("task.link.closed", {"ref": "bd-done", "reason": "merged (#7)"}),
        ("task.link.closed", {"ref": "bd-cancelled", "reason": "cancelled"}),
    ]


def test_sweep_is_throttled(host_tasks):
    events = []
    store = SimpleNamespace(get_feature=_cards)
    kw = dict(tasks_store=host_tasks, publish=lambda t, d: events.append(d))
    assert link_events.sweep(store, now=1000.0, **kw) == 2
    assert link_events.sweep(store, now=1000.0 + link_events.SWEEP_INTERVAL_S - 1, **kw) == 0
    assert link_events.sweep(store, now=1000.0 + link_events.SWEEP_INTERVAL_S, **kw) == 2


def test_sweep_without_host_task_store_is_a_no_op():
    # conftest makes the default task store raise ImportError (a host without one).
    assert link_events.sweep(SimpleNamespace(get_feature=_cards), force=True) == 0
