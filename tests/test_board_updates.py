"""board_update_feature + Ready-gate messaging + input hygiene (#79).

Three behaviours that together fix the 'unrepairable bead' trap:
  1. ``BeadsBoard.update_feature`` / the ``board_update_feature`` tool partially update a
     feature's fields, so a bead that misses a Ready-gate field can be repaired in place
     and then pass ``board_mark_ready`` — no cancel-and-recreate.
  2. the Ready gate's rejection names the missing fields AND points at the update tool.
  3. ``board_create_feature`` peels one symmetric layer of literal wrapping double quotes
     off its string args before storage.

Store-level flows use the ``make_board`` fixture (a fake ``br``); the tool-level flows
patch ``project_board.store.get_store`` exactly as test_board_create_feature_dedup does.
"""

from __future__ import annotations

import json

import pytest

import project_board as pb
from project_board.store import BoardError


class _StatefulBr:
    """A fake ``_run`` backed by one mutable feature dict: an ``update`` carrying
    ``--description`` / ``--acceptance-criteria`` / ``--design`` / ``--notes`` /
    ``--add-label`` writes through to the dict, so a store method's effect is observable
    via ``get_feature`` (which each test points at the same dict). Every call is recorded
    so the emitted ``br`` args can be asserted."""

    _FIELD_FLAGS = {
        "--description": "spec",
        "--acceptance-criteria": "acceptance_criteria",
        "--design": "design",
    }

    def __init__(self, state):
        self.state = state
        self.calls = []

    def __call__(self, *args, want_json=False):
        self.calls.append(args)
        if args and args[0] == "update":
            i = 2
            while i < len(args):
                flag = args[i]
                if flag in self._FIELD_FLAGS and i + 1 < len(args):
                    self.state[self._FIELD_FLAGS[flag]] = args[i + 1]
                    i += 2
                elif flag == "--notes" and i + 1 < len(args):
                    self.state["files_to_modify"] = [p.strip() for p in args[i + 1].splitlines() if p.strip()]
                    i += 2
                elif flag == "--add-label" and i + 1 < len(args):
                    self.state.setdefault("labels", []).append(args[i + 1])
                    i += 2
                else:
                    i += 1
        return [] if want_json else ""

    def cmds(self, name):
        return [a for a in self.calls if a and a[0] == name]


class _RecordingStore:
    """Records the kwargs board_create_feature hands ``create_feature`` (``list_features``
    stays empty, so the dedup guard never fires), letting the stored, post-hygiene values
    be asserted."""

    def __init__(self):
        self.created = None

    def list_features(self, state=None):
        return []

    def create_feature(self, title, **kw):
        self.created = {"title": title, **kw}
        return {"id": "bd-1", "title": title, "board_state": "backlog"}


def _get_tool(name, cfg=None):
    tools = {t.name: t for t in pb._board_tools(cfg or {})}
    return tools[name]


# ── input hygiene: strip one symmetric layer of literal wrapping double quotes ──────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('"Fix the thing"', "Fix the thing"),
        ('""double""', '"double"'),
        ("no quotes", "no quotes"),
        ('"lopsided', '"lopsided'),
        ('lopsided"', 'lopsided"'),
        ('""', ""),
        ('"', '"'),
        ("", ""),
        ('a"b"c', 'a"b"c'),
    ],
)
def test_strip_wrapping_quotes_peels_exactly_one_symmetric_layer(raw, expected):
    assert pb._strip_wrapping_quotes(raw) == expected


def test_board_create_feature_strips_wrapping_quotes_before_storage(monkeypatch):
    fake = _RecordingStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    create = _get_tool("board_create_feature")

    args = {
        "title": '"Add search bar"',
        "spec": '"lets a user search"',
        "acceptance_criteria": '"WHEN a query is typed THE SYSTEM SHALL filter"',
        "files_to_modify": '"src/search.py"',
        "design": '"a debounced input"',
    }
    out = json.loads(create.invoke(args))

    # the tool echoes the STORED (de-quoted) title, and every field lands unwrapped.
    assert out["title"] == "Add search bar"
    assert fake.created["title"] == "Add search bar"
    assert fake.created["spec"] == "lets a user search"
    assert fake.created["acceptance_criteria"] == "WHEN a query is typed THE SYSTEM SHALL filter"
    assert fake.created["design"] == "a debounced input"
    # de-quoted THEN split, so the wrapping quotes never reach the stored path.
    assert fake.created["files_to_modify"] == ["src/search.py"]


# ── the Ready gate names the missing fields AND points at the repair tool ───────────


def test_ready_gate_message_names_missing_fields_and_suggests_update_tool(make_board, monkeypatch):
    b = make_board(lambda *a, **k: None)
    feature = {
        "id": "bd-1",
        "board_state": "backlog",
        "spec": "",
        "acceptance_criteria": "",
        "files_to_modify": [],
    }
    monkeypatch.setattr(b, "get_feature", lambda fid: feature)

    with pytest.raises(BoardError) as exc:
        b.mark_ready("bd-1")

    msg = str(exc.value)
    assert "spec" in msg
    assert "acceptance_criteria" in msg
    assert "files_to_modify" in msg
    assert "board_update_feature" in msg


# ── update_feature: the store-level partial write ───────────────────────────────────


def test_update_feature_writes_only_the_passed_fields(make_board, monkeypatch):
    state = {"id": "bd-1", "board_state": "backlog", "labels": []}
    br = _StatefulBr(state)
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: state)

    b.update_feature("bd-1", acceptance_criteria="AC", files_to_modify=["x.py", "y.py"])

    (call,) = br.cmds("update")
    assert call == ("update", "bd-1", "--acceptance-criteria", "AC", "--notes", "x.py\ny.py")


def test_update_feature_replaces_a_stale_difficulty_label(make_board, monkeypatch):
    state = {"id": "bd-1", "board_state": "backlog", "labels": ["diff:small", "ready"]}
    br = _StatefulBr(state)
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: state)

    b.update_feature("bd-1", difficulty="Large")

    (call,) = br.cmds("update")
    assert call == ("update", "bd-1", "--remove-label", "diff:small", "--add-label", "diff:large")


def test_update_feature_with_nothing_to_change_makes_no_update_call(make_board, monkeypatch):
    state = {"id": "bd-1", "board_state": "backlog", "labels": []}
    br = _StatefulBr(state)
    b = make_board(br)
    monkeypatch.setattr(b, "get_feature", lambda fid: state)

    b.update_feature("bd-1")

    assert br.cmds("update") == []


# ── the whole flow: repair a rejected bead via the tools, then it goes ready ─────────


def test_update_tool_fills_a_missing_field_so_mark_ready_then_passes(make_board, monkeypatch):
    state = {
        "id": "bd-1",
        "title": "Repairable feature",
        "board_state": "backlog",
        "spec": "do the thing",
        "acceptance_criteria": "",
        "files_to_modify": ["a.py"],
        "labels": [],
    }
    br = _StatefulBr(state)
    board = make_board(br)
    monkeypatch.setattr(board, "get_feature", lambda fid: state)
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: board)

    update = _get_tool("board_update_feature")
    mark_ready = _get_tool("board_mark_ready")

    # underspecced → the Ready gate refuses (the tool returns an "Error: …" string that
    # names the missing field and points at the repair tool).
    rejected = mark_ready.invoke({"feature_id": "bd-1"})
    assert "acceptance_criteria" in rejected
    assert "board_update_feature" in rejected

    # fill the missing field in place with the update tool …
    update.invoke({"feature_id": "bd-1", "acceptance_criteria": "WHEN x THE SYSTEM SHALL y"})
    assert state["acceptance_criteria"] == "WHEN x THE SYSTEM SHALL y"

    # … and now the SAME feature clears the gate (JSON echo, not an Error string).
    out = json.loads(mark_ready.invoke({"feature_id": "bd-1"}))
    assert out["id"] == "bd-1"
    assert ("update", "bd-1", "--add-label", "ready", "--remove-label", "designing") in br.calls
