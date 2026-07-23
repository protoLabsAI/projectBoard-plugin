"""source_issue wiring through the agent tools (#97).

``board_create_feature`` / ``board_update_feature`` gain an optional ``source_issue``
param — the ORIGINATING GitHub issue (a full issue URL or ``owner/repo#N``) the
loop's PR opener stamps as ``Fixes #N``. The tools forward it (quote-stripped) to
the store, which normalizes and stores it on the bead; an invalid value surfaces as
the store's named error through the tool's ``Error: …`` boundary.

Tool-level flows patch ``project_board.store.get_store`` exactly as
test_board_create_feature_dedup does; the store's own normalize/store/project logic
is pinned in test_store.py.
"""

from __future__ import annotations

import json

import project_board as pb


class _RecordingStore:
    """Records the kwargs board_create_feature hands ``create_feature`` (``list_features``
    stays empty, so the dedup guard never fires)."""

    def __init__(self):
        self.created = None

    def list_features(self, state=None):
        return []

    def create_feature(self, title, **kw):
        self.created = {"title": title, **kw}
        return {"id": "bd-1", "title": title, "board_state": "backlog"}


class _UpdateRecordingStore:
    """Records the kwargs board_update_feature hands ``update_feature``."""

    def __init__(self):
        self.updated = None

    def update_feature(self, fid, **kw):
        self.updated = {"fid": fid, **kw}
        return {"id": fid, "title": "T", "board_state": "backlog"}


def _get_tool(name, cfg=None):
    tools = {t.name: t for t in pb._board_tools(cfg or {})}
    return tools[name]


# ── board_create_feature forwards source_issue to the store ─────────────────────────


def test_create_tool_forwards_source_issue(monkeypatch):
    fake = _RecordingStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    create = _get_tool("board_create_feature")

    out = json.loads(create.invoke({"title": "T", "spec": "s", "source_issue": "acme/widgets#8"}))

    assert out["id"] == "bd-1"
    assert fake.created["source_issue"] == "acme/widgets#8"


def test_create_tool_strips_wrapping_quotes_off_source_issue(monkeypatch):
    fake = _RecordingStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    create = _get_tool("board_create_feature")

    create.invoke({"title": "T", "spec": "s", "source_issue": '"https://github.com/acme/widgets/issues/8"'})

    assert fake.created["source_issue"] == "https://github.com/acme/widgets/issues/8"


def test_create_tool_defaults_source_issue_to_empty(monkeypatch):
    fake = _RecordingStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    create = _get_tool("board_create_feature")

    create.invoke({"title": "T", "spec": "s"})

    assert fake.created["source_issue"] == ""  # unset — the store adds no source label


# ── board_update_feature forwards source_issue to the store ─────────────────────────


def test_update_tool_forwards_source_issue(monkeypatch):
    fake = _UpdateRecordingStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    update = _get_tool("board_update_feature")

    update.invoke({"feature_id": "bd-1", "source_issue": "https://github.com/acme/widgets/issues/9"})

    assert fake.updated["source_issue"] == "https://github.com/acme/widgets/issues/9"


def test_update_tool_treats_absent_or_whitespace_source_issue_as_none(monkeypatch):
    fake = _UpdateRecordingStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    update = _get_tool("board_update_feature")

    update.invoke({"feature_id": "bd-1", "spec": "s"})
    assert fake.updated["source_issue"] is None  # absent → don't touch the field

    update.invoke({"feature_id": "bd-1", "source_issue": "   "})
    assert fake.updated["source_issue"] is None  # blank, never a "set it" signal


# ── the store's named rejection surfaces through the tool boundary ──────────────────


def _stateful_board(make_board, monkeypatch, labels=()):
    """A REAL BeadsBoard (fake `br`) so the store's normalize/reject path runs end to
    end under the tool — the invalid-format error must reach the agent by name."""
    state = {"id": "bd-1", "title": "T", "board_state": "backlog", "labels": list(labels)}
    calls = []

    def run_impl(*args, want_json=False):
        calls.append(args)
        if args and args[0] == "create":
            return "bd-1"
        if args and args[0] == "show":
            return [dict(state, status="open")]
        return [] if want_json else ""

    b = make_board(run_impl)
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: b)
    return b, calls


def test_create_tool_surfaces_the_named_invalid_source_issue_error(make_board, monkeypatch):
    _b, calls = _stateful_board(make_board, monkeypatch)
    create = _get_tool("board_create_feature")

    out = create.invoke({"title": "T", "spec": "s", "source_issue": "not-an-issue"})

    assert out.startswith("Error:") and "invalid source_issue" in out
    assert not any(c and c[0] == "create" for c in calls)  # rejected before minting a bead


def test_update_tool_surfaces_the_named_invalid_source_issue_error(make_board, monkeypatch):
    _b, calls = _stateful_board(make_board, monkeypatch)
    update = _get_tool("board_update_feature")

    out = update.invoke({"feature_id": "bd-1", "source_issue": "123"})  # bare number: no repo

    assert out.startswith("Error:") and "invalid source_issue" in out
    assert not any(c and c[0] == "update" for c in calls)  # nothing written


def test_create_tool_lands_the_normalized_source_label_end_to_end(make_board, monkeypatch):
    """Tool → store → bead: a full issue URL goes in, the single `source:owner/repo#N`
    label comes out on the `br update` — the value _source_issue() reads back."""
    _b, calls = _stateful_board(make_board, monkeypatch)
    create = _get_tool("board_create_feature")

    out = json.loads(create.invoke({"title": "T", "spec": "s", "source_issue": "https://github.com/o/r/issues/12"}))

    assert out["id"] == "bd-1"
    update = next(c for c in calls if c and c[0] == "update")
    assert "--add-label" in update and "source:o/r#12" in update
