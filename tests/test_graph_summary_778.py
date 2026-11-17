"""Project graph advice in ready replies (#778)."""

import copy

import pytest

from project_board import _ready_dry_run
from project_board.store import _graph_summary
from test_edge_advisory_778 import _Br, _card, _wire


def test_chain_and_independent_card(make_board, monkeypatch):
    board = make_board(_Br())
    cards = [
        _card("A", ["a.py (new)"]),
        _card("B", ["b.py (new)"], deps=["A"]),
        _card("C", ["b.py"], deps=["B"]),
        _card("D", ["d.py (new)"]),
    ]
    _wire(monkeypatch, board, cards)
    before = copy.deepcopy(cards)
    graph = _ready_dry_run(board, "C")["graph"]
    assert graph == {
        "startable_now": ["A", "D"],
        "longest_chain": ["A", "B", "C"],
        "no_shared_file_edges": [{"feature_id": "B", "depends_on": "A"}],
    }
    assert cards == before


def test_terminal_missing_other_project_and_empty_files():
    cards = [
        _card("done", ["done.py"], state="done"),
        _card("cancelled", [], state="cancelled"),
        _card("A", ["a.py"], deps=["done", "cancelled"]),
        _card("B", ["./a.py (new)"], deps=["A"]),
        _card("C", [], deps=["A"]),
        _card("D", ["d.py"], deps=["missing"]),
        _card("other", ["x.py"], project="elsewhere", deps=["A"]),
    ]
    graph = _graph_summary("", cards)
    assert graph["startable_now"] == ["A"]
    assert graph["no_shared_file_edges"] == []
    assert graph["longest_chain"] == ["A", "B"]


@pytest.mark.parametrize(
    "cards",
    [
        [_card("A", [], deps=["missing"])],
        [_card("A", []), _card("B", [], deps=["A"])],
        [_card("A", []), _card("B", [])],
        [_card("A", [], deps=["missing"]), _card("B", [], deps=["A"], project="other")],
    ],
)
def test_small_projects_omit_graph(make_board, monkeypatch, cards):
    board = make_board(_Br())
    _wire(monkeypatch, board, cards)
    assert "graph" not in _ready_dry_run(board, "A")


def test_cycle_is_safe():
    cards = [_card("A", ["a.py"], deps=["B"]), _card("B", ["b.py"], deps=["A"])]
    graph = _graph_summary("", cards)
    assert graph["startable_now"] == []
    assert set(graph["longest_chain"]) == {"A", "B"}
    assert len(graph["longest_chain"]) == 2
    assert len(graph["no_shared_file_edges"]) == 2


def test_deep_chain_and_branch_choose_longest():
    cards = [_card(str(i).zfill(4), [], deps=[str(i - 1).zfill(4)] if i else []) for i in range(1100)]
    cards.append(_card("branch", [], deps=["0000", "1099"]))
    assert len(_graph_summary("", cards)["longest_chain"]) == 1101


def test_fresh_card_overlays_listing_and_archived_blockers_are_read(make_board, monkeypatch):
    board = make_board(_Br())
    cards = [_card("A", [], state="done"), _card("B", [], deps=["A"])]
    fresh = _card("C", [], deps=["B"])
    calls = []

    def listing(**kwargs):
        calls.append(kwargs)
        return cards

    monkeypatch.setattr(board, "list_features", listing)
    graph = board.graph_summary("", current=fresh)
    assert graph["startable_now"] == ["B"]
    assert graph["longest_chain"] == ["B", "C"]
    assert calls == [{"include_archived": True}]


@pytest.mark.parametrize("missing", [False, True])
def test_graph_failure_preserves_ready_advice(make_board, monkeypatch, missing):
    board = make_board(_Br())
    monkeypatch.setattr(board, "ready_check", lambda _: {"ok": True, "refusals": [], "note": "kept"})
    monkeypatch.setattr(board, "get_feature", lambda _: None if missing else _card("A", []))

    def fail(*args, **kwargs):
        raise RuntimeError("read failed")

    monkeypatch.setattr(board, "graph_summary", fail)
    assert _ready_dry_run(board, "A") == {"ok": True, "note": "kept"}


@pytest.mark.parametrize("with_graph", [False, True])
def test_mark_ready_reply(make_board, monkeypatch, with_graph):
    import json

    from project_board import _board_tools

    board = make_board(_Br())
    cards = [_card("A", [], state="ready")]
    if with_graph:
        cards += [_card("B", [], deps=["A"]), _card("C", [], deps=["B"])]
    _wire(monkeypatch, board, cards)
    monkeypatch.setattr(board, "mark_ready", lambda _: cards[0])
    monkeypatch.setattr("project_board.store.get_store", lambda **kwargs: board)
    tool = next(t for t in _board_tools({}) if t.name == "board_mark_ready")
    reply = json.loads(tool.invoke({"feature_id": "A"}))
    assert reply["id"] == "A"
    assert reply["state"] == "ready"
    if with_graph:
        assert reply["graph"]["longest_chain"] == ["A", "B", "C"]
    else:
        assert reply == {"id": "A", "state": "ready"}
