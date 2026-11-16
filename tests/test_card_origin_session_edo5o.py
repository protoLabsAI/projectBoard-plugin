"""A board card records the chat it was created from (ADR 0131 P2b-3).

`board_create_feature` / `board_create_task` resolve the originating chat from the host
session resolver (`tools.session.origin_session_for`, core P2b-1) applied to the tool
turn's injected state, and persist it on the card. The block-page pass-through (the other
half of P2b-3) is covered in test_block_pages_735.py (operator page) and
test_panel_verdict_wake.py (agent page).
"""

from __future__ import annotations

import json
import sys
import types

import pytest

import project_board as pb
from project_board import store as store_mod
from project_board.store import BeadsBoard


def _tool(name, cfg=None):
    return {t.name: t for t in pb._board_tools(cfg or {})}[name]


class _RecordingStore:
    """A store double whose ``create_feature`` records the kwargs the tool handed it — so a
    test can assert the resolved ``origin_session`` reached the store. ``list_features`` is
    empty so the dedup gate never fires."""

    def __init__(self):
        self.created: list[dict] = []

    def list_features(self, state=None):
        return []

    def create_feature(self, title, **kw):
        f = {"id": "bd-new1", "title": title, "board_state": "backlog", **kw}
        self.created.append(f)
        return f


def _install_resolver(monkeypatch, fn):
    """Register a fake host ``tools.session`` whose ``origin_session_for`` is ``fn`` — the
    P2b-1 resolver seam the create tools feature-detect (register the parent package too so
    ``from tools.session import …`` resolves with no real host present)."""
    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = []  # a package, so the submodule import resolves
    session_mod = types.ModuleType("tools.session")
    session_mod.origin_session_for = fn
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.session", session_mod)


# ── r1 / AC1: the create tools bind a card to the chat that caused it ────────────────


@pytest.mark.parametrize("verb", ["board_create_feature", "board_create_task"])
def test_create_tool_records_the_resolved_origin_session(monkeypatch, verb):
    """The resolver maps this turn's state session (`job:X`) to the originating chat
    (`chat-a`); the tool passes that onto the card. FAILS ON MAIN — main threads no origin."""
    store = _RecordingStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: store)
    _install_resolver(monkeypatch, lambda state: "chat-a" if (state or {}).get("session_id") == "job:X" else "")

    out = _tool(verb).invoke({"title": "Add a thing", "spec": "s", "state": {"session_id": "job:X"}})

    assert json.loads(out)["id"] == "bd-new1"
    assert store.created[0]["origin_session"] == "chat-a"


# ── r3 / AC3: an older host without the resolver still creates, with an empty origin ──


@pytest.mark.parametrize("verb", ["board_create_feature", "board_create_task"])
def test_create_tool_without_resolver_records_an_empty_origin(monkeypatch, verb):
    """An older host whose ``tools.session`` predates the resolver: the import of
    ``origin_session_for`` fails, the origin is "", and the card still creates."""
    store = _RecordingStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: store)
    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = []
    session_mod = types.ModuleType("tools.session")  # NO origin_session_for — the older host
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.session", session_mod)

    out = _tool(verb).invoke({"title": "Add a thing", "spec": "s", "state": {"session_id": "job:X"}})

    assert json.loads(out)["id"] == "bd-new1"
    assert store.created[0]["origin_session"] == ""


@pytest.mark.parametrize("verb", ["board_create_feature", "board_create_task"])
def test_create_tool_with_no_session_module_records_an_empty_origin(monkeypatch, verb):
    """The same graceful degradation when the host has no ``tools.session`` module at all."""
    store = _RecordingStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: store)
    monkeypatch.delitem(sys.modules, "tools.session", raising=False)
    monkeypatch.delitem(sys.modules, "tools", raising=False)

    out = _tool(verb).invoke({"title": "Add a thing", "spec": "s", "state": {"session_id": "job:X"}})

    assert json.loads(out)["id"] == "bd-new1"
    assert store.created[0]["origin_session"] == ""


def test_resolver_that_raises_never_fails_the_create(monkeypatch):
    """Resolving the origin is best-effort provenance — a resolver that throws yields "",
    never a failed create."""
    store = _RecordingStore()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: store)

    def _boom(_state):
        raise RuntimeError("resolver exploded")

    _install_resolver(monkeypatch, _boom)

    out = _tool("board_create_feature").invoke({"title": "Add a thing", "spec": "s", "state": {"session_id": "job:X"}})

    assert json.loads(out)["id"] == "bd-new1"
    assert store.created[0]["origin_session"] == ""


# ── store persistence + projection: the origin rides the bead `notes` field ──────────


class _Br:
    """A recording ``_run``: `create` returns an id, `show` a bare open bead, everything
    else records + returns inert — enough to exercise the notes writes and the projection."""

    def __init__(self, created="bd-1"):
        self.calls: list[tuple] = []
        self.created = created

    def __call__(self, *args, want_json=False, with_has_more=False):
        self.calls.append(args)
        head = args[0] if args else ""
        if head == "create":
            val = self.created
        elif head == "show":
            val = [{"id": self.created, "status": "open", "title": "T", "labels": []}]
        else:
            val = [] if want_json else ""
        return (val, None) if with_has_more else val

    def cmds(self, name):
        return [a for a in self.calls if a and a[0] == name]


def test_create_feature_stores_the_origin_session_in_notes(make_board):
    """files_to_modify + the origin land in the SAME `--notes=` payload: paths first, the
    `origin-session:` metadata line last — never a label (a session id can carry chars the
    label validator refuses)."""
    br = _Br()
    b = make_board(br)
    b.create_feature("T", spec="s", files_to_modify=["a.py"], origin_session="chat-a")
    (update,) = br.cmds("update")
    assert "--notes=a.py\norigin-session: chat-a" in update
    assert "--add-label" not in update  # never a label


def test_create_feature_with_only_an_origin_still_writes_the_notes_line(make_board):
    """The origin alone triggers the notes write (no files/source/waits needed)."""
    br = _Br()
    b = make_board(br)
    b.create_feature("T", spec="s", origin_session="chat-a")
    (update,) = br.cmds("update")
    assert "--notes=origin-session: chat-a" in update


def test_create_feature_without_an_origin_writes_no_origin_line(make_board):
    br = _Br()
    b = make_board(br)
    b.create_feature("T", spec="s", files_to_modify=["a.py"])
    for c in br.cmds("update"):
        assert not any("origin-session:" in str(tok) for tok in c)


def test_project_surfaces_the_origin_and_keeps_it_out_of_the_file_list(make_board):
    b = make_board(_Br())
    bead = {
        "id": "x",
        "status": "open",
        "labels": ["ready"],
        "notes": "a.py\nsource-issue: acme/widgets#8\norigin-session: chat-a",
    }
    f = b._project(bead)
    assert f["origin_session"] == "chat-a"
    assert f["files_to_modify"] == ["a.py"]  # the metadata line never leaks into the file list
    assert f["source_issue"] == "acme/widgets#8"  # the neighbouring metadata still projects
    # absent → ""
    assert b._project({"id": "y", "status": "open", "labels": []})["origin_session"] == ""


def test_update_feature_files_edit_preserves_the_origin_line(make_board, monkeypatch):
    """`--notes` replaces the whole field, so a files-only update must carry the origin
    forward (the same contract that preserves the source-issue line)."""
    br = _Br()
    b = make_board(br)
    monkeypatch.setattr(
        b, "_require", lambda fid: {"id": fid, "files_to_modify": ["a.py"], "origin_session": "chat-a", "labels": []}
    )
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    b.update_feature("bd-1", files_to_modify=["x.py", "y.py"])
    (call,) = br.cmds("update")
    assert call == ("update", "bd-1", "--notes=x.py\ny.py\norigin-session: chat-a")


def test_set_requirements_preserves_the_origin_line(make_board, monkeypatch):
    br = _Br()
    b = make_board(br)
    monkeypatch.setattr(
        b, "_require", lambda fid: {"id": fid, "files_to_modify": ["a.py"], "origin_session": "chat-a", "labels": []}
    )
    monkeypatch.setattr(b, "get_feature", lambda fid: {"id": fid, "labels": []})
    b.set_requirements("bd-1", [{"id": "r1", "text": "do x", "status": "open"}])
    (call,) = br.cmds("update")
    notes = next(t for t in call if str(t).startswith("--notes="))
    assert "origin-session: chat-a" in notes and "a.py" in notes


def test_refuse_structured_paths_rejects_a_path_that_is_really_the_origin_line(make_board):
    """A files_to_modify entry starting with the origin prefix would round-trip as metadata,
    not a path — refused by name, like the other reserved prefixes."""
    b = make_board(_Br())
    with pytest.raises(store_mod.BoardError, match="origin-session:"):
        b.create_feature("T", spec="s", files_to_modify=["origin-session: chat-a"])


def test_render_notes_round_trips_the_origin_through_split_and_reader():
    notes = store_mod._render_notes(["a.py"], "acme/widgets#8", (), ("npm:pkg@>=1",), "chat-a")
    files, src, _reqs = store_mod._split_notes(notes)
    assert files == ["a.py"]  # neither metadata line is mistaken for a path
    assert src == "acme/widgets#8"
    assert store_mod._origin_from_notes(notes) == "chat-a"
    assert store_mod._origin_from_notes("a.py\nsource-issue: acme/widgets#8") == ""


def test_isinstance_board_exposes_project_helper():
    """Guard the test's reliance on BeadsBoard._project existing (projection entrypoint)."""
    assert hasattr(BeadsBoard, "_project")
