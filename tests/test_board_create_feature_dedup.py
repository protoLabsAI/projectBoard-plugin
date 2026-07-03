"""board_create_feature dedup — the same class of bug portfolio_dispatch's #25
dedup fixed one tier up, but here: an agent's OWN reasoning (onboarding, or its
own read of a dispatched task) calling this tool twice in one turn for the same
work, live-caught in dogfooding (two board_create_feature calls, ~9s apart, same
title, no force) with no guard to stop it."""

from __future__ import annotations

import json

import project_board as pb


def test_norm_title_collapses_whitespace_and_case():
    assert pb._norm_title("  Fix   the Thing  ") == "fix the thing"
    assert pb._norm_title("Fix the Thing") == pb._norm_title("fix   the    thing")


def test_open_duplicate_finds_same_title_open_feature():
    features = [{"id": "bd-1", "title": "Add X", "board_state": "backlog"}]
    dup = pb._open_duplicate(features, "add   x")
    assert dup is not None and dup["id"] == "bd-1"


def test_open_duplicate_ignores_terminal_states():
    features = [
        {"id": "bd-1", "title": "Add X", "board_state": "done"},
        {"id": "bd-2", "title": "Add X", "board_state": "cancelled"},
    ]
    assert pb._open_duplicate(features, "Add X") is None


def test_open_duplicate_none_when_no_match():
    features = [{"id": "bd-1", "title": "Add X", "board_state": "backlog"}]
    assert pb._open_duplicate(features, "Add Y") is None


class _FakeStore:
    """Records create_feature calls; list_features returns whatever's been created
    so far — enough to exercise board_create_feature's dedup decision without a
    real BeadsBoard/br CLI (store.py's own projection is tested in test_store.py)."""

    def __init__(self):
        self.created: list[dict] = []
        self._next = 1

    def list_features(self, state=None):
        return list(self.created)

    def create_feature(self, title, **kw):
        fid = f"bd-{self._next}"
        self._next += 1
        f = {"id": fid, "title": title, "board_state": "backlog", **kw}
        self.created.append(f)
        return f


def _get_tool(name: str, cfg: dict | None = None):
    tools = {t.name: t for t in pb._board_tools(cfg or {})}
    return tools[name]


def test_board_create_feature_refuses_a_same_title_open_dup(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    create = _get_tool("board_create_feature")

    first = json.loads(create.invoke({"title": "Rollup surfacing", "spec": "s1"}))
    assert first["id"] == "bd-1"

    second = create.invoke({"title": "Rollup surfacing", "spec": "s2 — reconsidered"})
    assert "Skipped" in second
    assert "bd-1" in second
    assert len(fake.created) == 1  # the dup was refused, not created


def test_board_create_feature_force_true_creates_a_second_copy(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    create = _get_tool("board_create_feature")

    create.invoke({"title": "Rollup surfacing", "spec": "s1"})
    second = create.invoke({"title": "Rollup surfacing", "spec": "s2", "force": True})
    assert json.loads(second)["id"] == "bd-2"
    assert len(fake.created) == 2


def test_board_create_feature_allows_recreating_after_the_first_is_done(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    create = _get_tool("board_create_feature")

    create.invoke({"title": "Rollup surfacing", "spec": "s1"})
    fake.created[0]["board_state"] = "done"  # the first shipped — this is legit new work

    second = create.invoke({"title": "Rollup surfacing", "spec": "s2"})
    assert json.loads(second)["id"] == "bd-2"
