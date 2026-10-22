"""Host-free ledger contracts, including real beads persistence across store restart."""

import json
import shutil

import pytest

import project_board as pb
from project_board import store, work_locations as ledger


def entry(**changes):
    return {"id": "clone", "path": "/external/clone", "purpose": "preserve experiments", **changes}


@pytest.fixture
def board(make_board):
    bead = {"id": "bd-1", "title": "Work", "status": "open", "labels": [], "comments": []}
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if args[0] == "show":
            return [bead] if args[1] == "bd-1" else []
        assert args[:3] == ("comments", "add", "bd-1")
        bead["comments"].append({"text": args[3]})

    return make_board(run), bead, calls


def test_retry_update_provenance_and_distinct_paths(board):
    b, bead, calls = board
    b.actor = "alice"
    state = entry(repo="owner/repo", branch="experiment", sha="a" * 40, session_id="session-1")
    first = b.record_work_location("bd-1", state)
    assert first["entries"][0]["pushed"] is None
    assert first["entries"][0]["by"] == "alice"
    assert first["entries"][0]["session_id"] == "session-1"
    assert b.record_work_location("bd-1", state) == first
    assert len(bead["comments"]) == 1
    b.actor = "bob"
    updated = b.record_work_location("bd-1", {**state, "pushed": False, "session_id": "session-2"})
    assert [r["by"] for r in updated["history"]] == ["alice", "bob"]
    b.record_work_location("bd-1", {**state, "id": "copy", "path": "/other/clone"})
    b.record_work_location("bd-1", entry(id="evidence", path="relative evidence/$(never execute)"))
    projected = b.get_feature("bd-1")["work_locations"]
    assert len(projected["entries"]) == 3
    assert projected["entries"][2]["repo"] == ""
    assert projected["entries"][2]["sha"] == ""
    assert b.list_work_locations("bd-1") == projected
    assert all(c[0] in {"show", "comments"} for c in calls)


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"path": "/x", "purpose": "x"},
        entry(id=""),
        entry(path=""),
        entry(purpose=""),
        entry(pushed="unknown"),
        entry(pushed=1),
        entry(sha="not-sha"),
        entry(path="a\nb"),
        entry(path="a" * 4097),
        entry(purpose="a" * 2049),
        entry(by="spoof"),
        entry(version=2),
        entry(credentials="secret"),
        entry(session_id=123),
        entry(remote_ref=[]),
        None,
        [],
    ],
)
def test_invalid_records_do_not_read_or_write(board, bad):
    b, _, calls = board
    with pytest.raises(store.BoardError):
        b.record_work_location("bd-1", bad)
    assert calls == []


def test_unknown_and_missing_card_ids(board):
    b, bead, calls = board
    for fid in ("", "--help", None, "a\nb"):
        with pytest.raises(store.BoardError):
            b.record_work_location(fid, entry())
    assert calls == []
    with pytest.raises(store.BoardError, match="unknown feature"):
        b.record_work_location("bd-unknown", entry())
    assert bead["comments"] == []
    assert calls == [("show", "bd-unknown")]


def test_conflicts_corruption_and_write_failure(board, monkeypatch):
    b, bead, _ = board
    b.record_work_location("bd-1", entry())
    for changed in (entry(id="new-id"), entry(path="/new-path")):
        with pytest.raises(store.BoardError, match="already"):
            b.record_work_location("bd-1", changed)
    bead["comments"].append({"text": ledger.PREFIX + '{"version":999}'})
    assert b.list_work_locations("bd-1")["errors"]
    with pytest.raises(store.BoardError, match="malformed"):
        b.record_work_location("bd-1", entry(pushed=True))
    bead["comments"].pop()
    original = b._run

    def fail(*args, **kwargs):
        if args[0] == "comments":
            raise store.BoardError("write failed: retry")
        return original(*args, **kwargs)

    monkeypatch.setattr(b, "_run", fail)
    with pytest.raises(store.BoardError, match="write failed"):
        b.record_work_location("bd-1", entry(pushed=True))
    assert len(bead["comments"]) == 1


def test_limits(board, monkeypatch):
    b, _, _ = board
    b.record_work_location("bd-1", entry())
    monkeypatch.setattr(ledger, "MAX_ENTRIES", 1)
    with pytest.raises(store.BoardError, match="entry limit"):
        b.record_work_location("bd-1", entry(id="other", path="/other"))
    monkeypatch.setattr(ledger, "MAX_REVISIONS", 1)
    assert len(b.record_work_location("bd-1", entry())["history"]) == 1
    with pytest.raises(store.BoardError, match="history limit"):
        b.record_work_location("bd-1", entry(pushed=True))


def test_tools_and_feature_projection(board, monkeypatch):
    b, _, calls = board
    monkeypatch.setattr(store, "get_store", lambda **kw: b)
    tools = {t.name: t for t in pb._board_tools({})}
    write = tools["board_record_work_location"]
    read = tools["board_list_work_locations"]
    assert write.invoke({"feature_id": "bd-1", "entry": {}}).startswith("Error:")
    assert read.invoke({"feature_id": ""}).startswith("Error:")
    assert calls == []
    assert "unknown feature" in write.invoke({"feature_id": "missing", "entry": entry()})
    assert "unknown feature" in read.invoke({"feature_id": "missing"})
    result = json.loads(write.invoke({"feature_id": "bd-1", "entry": entry()}))
    assert json.loads(read.invoke({"feature_id": "bd-1"})) == result
    feature = json.loads(tools["board_get_feature"].invoke({"feature_id": "bd-1"}))
    assert feature["work_locations"] == result


@pytest.mark.br_shape
@pytest.mark.skipif(
    shutil.which(store.BR) is None, reason="real beads CLI required (enforced by integration tier in CI)"
)
def test_real_store_roundtrip_across_restart(tmp_path):
    b = store.BeadsBoard(repo=str(tmp_path), actor="original")
    fid = b.create_feature("External work")["id"]
    state = entry(repo="untrusted/repo", branch="work", sha="b" * 40, remote_ref="refs/heads/work", session_id="s1")
    b.record_work_location(fid, state)
    restarted = store.BeadsBoard(repo=str(tmp_path), actor="original")
    assert len(restarted.record_work_location(fid, state)["history"]) == 1
    restarted.actor = "reviewer"
    restarted.record_work_location(fid, {**state, "pushed": True})
    restarted.record_work_location(fid, entry(id="evidence", path="/not/a/git/repo"))
    restarted.record_work_location(fid, {**state, "id": "copy", "path": "/distinct/clone"})
    final = store.BeadsBoard(repo=str(tmp_path)).get_feature(fid)["work_locations"]
    assert len(final["entries"]) == 3
    assert len(final["history"]) == 4
    assert final["history"][0]["by"] == "original"
    assert final["entries"][0]["by"] == "reviewer"
    assert final["entries"][0]["pushed"] is True
    assert final["entries"][1]["pushed"] is None
    assert restarted.repo == str(tmp_path)
    assert restarted.projects == {}
    assert not final["errors"]


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        '{"version": true}',
        '{"version": 1}',
        "[" * 1100 + "]" * 1100,
    ],
)
def test_malformed_persisted_records_are_visible_errors(payload):
    result = ledger.project([ledger.PREFIX + payload])
    assert result["entries"] == []
    assert result["history"] == []
    assert len(result["errors"]) == 1


def test_encoded_size_is_bounded_before_store_access(board):
    b, _, calls = board
    with pytest.raises(store.BoardError, match="too large"):
        b.record_work_location("bd-1", entry(path="😀" * 4000))
    assert calls == []
