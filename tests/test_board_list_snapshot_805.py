"""board_list answers from the shared read snapshot, not a fresh store listing (#805).

On the live host a ``board_list`` tool call took 3 to 7 s: every call ran its own
``list_features`` (one ``br list`` over the whole history plus a ``br show`` per 100 live
rows), and each of those ``br`` commands queued for the process-wide ``br`` lock behind
the drive loop and the sweep. The tool now reads the snapshot ``GET /features`` and
``/cards`` already serve from (read_cache).

These tests run a REAL ``br`` store seeded like the #808 fixture (many archived cards, a
live board in every state) and count the ``br`` processes each tool call starts.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import project_board as pb
from project_board import api, read_cache
from project_board import store as store_mod

BASE = "/api/plugins/project_board"
STALE_LINE = "\n\nThe board store is busy: this listing is from "
PR = "https://github.com/example/repo/pull/"

# The live board: one card per state the listing treats differently, across two projects
# and one project name the board does not know (`project_unresolved` + `project_hint`).
LIVE = [
    dict(id="snap-00100", status="open", labels=["project:alpha"]),  # backlog
    dict(id="snap-00101", status="open", labels=["ready", "project:alpha"], priority=1),
    dict(id="snap-00102", status="open", labels=["ready", "project:beta"]),
    dict(id="snap-00103", status="in_progress", labels=["project:beta"]),
    dict(
        id="snap-00104",
        status="in_progress",
        labels=["in-review", "review-clean", "project:alpha"],
        external_ref=PR + "104",
    ),  # awaiting-merge (auto_merge off)
    dict(
        id="snap-00105",
        status="in_progress",
        labels=["in-review", "review-pending", "project:beta"],
        external_ref=PR + "105",
    ),  # review in progress
    dict(id="snap-00106", status="open", labels=["blocked", "blocked-class:manual", "project:alpha"]),
    dict(id="snap-00107", status="open", labels=["project:gone"]),
    dict(id="snap-00108", status="deferred", labels=[]),
    dict(id="snap-00109", status="closed", labels=["cancelled", "project:alpha"]),
]
ARCHIVED, DONE = 40, 4


@pytest.fixture(name="board")
def seeded_board(tmp_path):
    """A real ``br`` workspace: ARCHIVED archived cards, DONE merged ones and LIVE."""
    if not shutil.which("br"):
        pytest.skip("real br required")
    path = tmp_path.resolve()  # br refuses a JSONL it reaches through a symlinked tmp dir
    (path / ".beads").mkdir()
    db = str(path / ".beads" / "beads.db")
    subprocess.run(["br", "init", "--prefix", "snap", "--db", db], cwd=path, check=True, capture_output=True)
    rows = []
    for i in range(ARCHIVED + DONE):
        rows.append(
            dict(
                id=f"snap-{i:05d}",
                status="closed",
                closed_at="2026-11-21T00:00:00Z",
                external_ref=f"{PR}{i + 1}",
                labels=(["archived"] if i < ARCHIVED else []) + ["project:alpha" if i % 2 else "project:beta"],
            )
        )
    rows += [dict(r) for r in LIVE]
    for r in rows:
        r.setdefault("priority", 2)
        if r["status"] == "closed":
            r.setdefault("closed_at", "2026-11-21T00:00:00Z")
        r.update(
            title=f"Card {r['id']}",
            description="spec " * 200,
            issue_type="feature",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-11-21T00:00:00Z",
        )
    (path / ".beads" / "issues.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    subprocess.run(["br", "sync", "--import-only", "--db", db], cwd=path, check=True, capture_output=True)
    return {
        "db_path": db,
        "repo": str(path),
        "projects": {"alpha": {"repo": str(path)}, "beta": {"repo": str(path)}},
        "default_project": "alpha",
        "auto_merge": False,
        "review_gate": True,
    }


@pytest.fixture
def br_calls(monkeypatch):
    """Every real ``br`` process the store starts, as ``(subcommand, …)`` tuples. Set
    ``br_calls.list_delay`` to make each ``br list`` take that long (a busy store)."""
    real = store_mod._run_br_process

    class _Calls(list):
        list_delay = 0.0

    calls = _Calls()

    def _counting(cmd, *, cwd, args):
        calls.append(tuple(args))
        if calls.list_delay and args and args[0] == "list":
            time.sleep(calls.list_delay)
        return real(cmd, cwd=cwd, args=args)

    monkeypatch.setattr(store_mod, "_run_br_process", _counting)
    return calls


def _tools(cfg):
    return {t.name: t for t in pb._board_tools(cfg)}


def _by_id(reply):
    return {r["id"]: r for r in json.loads(reply)}


def _store(cfg):
    """The board the tools resolve for the default project (same cached object)."""
    return store_mod.get_store(**pb._board_store_kwargs(cfg))


def _settle(cfg):
    """Wait until no listing is running for this board (a tool write starts one)."""
    store = _store(cfg)
    caches = [c for c in (read_cache.published(cfg["db_path"]), read_cache._own.get(store)) if c is not None]
    deadline = time.monotonic() + 30
    while any(c._runs for c in caches):
        assert time.monotonic() < deadline, "the board listing never finished"
        time.sleep(0.01)


# ── r2: an unchanged board costs no `br` ────────────────────────────────────────────


def test_second_call_on_an_unchanged_board_runs_no_br(board, br_calls):
    board_list = _tools(board)["board_list"]
    first = board_list.invoke({})
    assert [c[0] for c in br_calls][:1] == ["list"], "the first call must list the real store"
    assert len(json.loads(first)) == DONE + len(LIVE)

    br_calls.clear()
    assert board_list.invoke({}) == first
    # Every filter is answered from the same listing; only the archived view is its own.
    assert set(_by_id(board_list.invoke({"project": "beta"}))) == {
        "snap-00040",
        "snap-00042",
        "snap-00102",
        "snap-00103",
        "snap-00105",
    }
    assert set(_by_id(board_list.invoke({"state": "ready"}))) == {"snap-00101", "snap-00102"}
    assert set(_by_id(board_list.invoke({"state": "in_review", "project": "alpha"}))) == {"snap-00104"}
    assert br_calls == []

    assert len(json.loads(board_list.invoke({"include_archived": True}))) == ARCHIVED + DONE + len(LIVE)
    assert br_calls, "the archived view is a second listing, read once"
    br_calls.clear()
    assert len(json.loads(board_list.invoke({"include_archived": True, "project": "alpha"}))) == 27
    assert br_calls == []


# ── r4: the reply is the one the direct listing gave ────────────────────────────────


class _Direct:
    """The store as board_list read it before this change: not a ``BeadsBoard``, so the
    tool lists it directly, with ``state`` narrowed inside the store."""

    def __init__(self, real):
        self._real = real
        self.listings = 0

    def list_features(self, state=None, include_archived=False):
        self.listings += 1
        return self._real.list_features(state=state, include_archived=include_archived)


QUERIES = [
    {},
    {"project": "alpha"},
    {"project": "beta"},
    {"project": "gone"},
    {"project": "nobody"},
    {"include_archived": True},
    {"include_archived": True, "project": "beta"},
    {"include_archived": True, "state": "done"},
    *({"state": s} for s in ("backlog", "ready", "in_progress", "in_review", "done", "blocked", "cancelled")),
    {"state": "ready", "project": "beta"},
    {"state": "in_review", "project": "alpha"},
    {"state": "in_review", "project": '"alpha"'},
]


def test_output_matches_the_direct_listing(board, br_calls, monkeypatch):
    direct = _Direct(_store(board))
    with monkeypatch.context() as m:
        m.setattr("project_board.store.get_store", lambda **_kw: direct)
        previous = _tools(board)["board_list"]
    board_list = _tools(board)["board_list"]

    for query in QUERIES:
        expected = previous.invoke(query)
        assert board_list.invoke(query) == expected, query
    assert direct.listings == len(QUERIES)

    # The fields the listing exists for are really in what was compared.
    rows = _by_id(board_list.invoke({}))
    assert rows["snap-00104"]["next_action"] == "awaiting-merge (auto_merge off)"
    assert rows["snap-00104"]["awaiting_merge"] is True and "merge #104" in rows["snap-00104"]["next_action_hint"]
    assert rows["snap-00105"]["next_action"] == "review in progress"
    assert rows["snap-00106"]["blocked"] is True and rows["snap-00106"]["blocked_class"] == "manual"
    assert rows["snap-00107"]["project_unresolved"] is True and "snap-00107" in rows["snap-00107"]["project_hint"]
    assert "next_action" not in rows["snap-00100"]
    assert list(rows)[0] == "snap-00106"  # blocked first: the store's order is kept


def test_annotations_never_reach_the_cached_rows(board, br_calls):
    """next_action is stamped on a per-call copy: the routes read the same cached rows."""
    board_list = _tools(board)["board_list"]
    board_list.invoke({})
    store = _store(board)
    cached = read_cache.own_cache(store, board["db_path"]).snapshot().rows
    assert cached and not any("next_action" in f or "awaiting_merge" in f for f in cached)
    # A config change the hints read shows on the next call, with no listing.
    br_calls.clear()
    board["auto_merge"] = True
    assert _by_id(_tools(board)["board_list"].invoke({}))["snap-00104"]["next_action"] != (
        "awaiting-merge (auto_merge off)"
    )
    assert br_calls == []


@pytest.mark.parametrize("shared", [False, True])
def test_dependency_filters_match_direct_listing(board, monkeypatch, shared):
    """Cancelled dependency expansion belongs to live_cards, not list_features.
    Keep the latter's rows AND next_action hints for each state/archive filter."""
    store = _store(board)
    store._run("update", "snap-00000", "--add-label", "cancelled")
    for fid in ("snap-00100", "snap-00106"):
        for dep in ("snap-00000", "snap-00109"):
            store._run("dep", "add", fid, dep)
    direct = _Direct(store)
    with monkeypatch.context() as m:
        m.setattr("project_board.store.get_store", lambda **_kw: direct)
        previous = _tools(board)["board_list"]
    if shared:
        assert _client(board).get(BASE + "/features").status_code == 200
    board_list = _tools(board)["board_list"]
    for archived in (False, True):
        for state in ("", "backlog", "blocked", "cancelled"):
            for project in ("", "alpha", "beta"):
                query = dict(state=state, include_archived=archived, project=project)
                assert board_list.invoke(query) == previous.invoke(query), query
        cancelled = _by_id(board_list.invoke(dict(state="cancelled", include_archived=archived)))
        assert ("snap-00000" in cancelled) is archived
        # The store itself, with no tool in between: ``list_features(state=…)`` is the full
        # listing narrowed by ``board_state`` and nothing else. It appends no dependency
        # row from another state, so narrowing the snapshot afterwards loses and gains none.
        full = store.list_features(include_archived=archived)
        for state in ("backlog", "ready", "in_progress", "in_review", "done", "blocked", "cancelled"):
            assert store.list_features(state=state, include_archived=archived) == [
                f for f in full if f["board_state"] == state
            ], (state, archived)
    assert store.get_feature("snap-00100")["depends_on"]


@pytest.mark.parametrize("shared", [False, True])
def test_default_repo_removed_after_warming_cache_is_refused(board, tmp_path, br_calls, shared):
    # Keep the database outside the repo: removing cwd must not change the cache token.
    repo = tmp_path / "repo"
    repo.mkdir()
    board.update(repo=str(repo), projects={}, default_project="")
    board_list = _tools(board)["board_list"]
    if shared:
        assert _client(board).get(BASE + "/features").status_code == 200
    first = board_list.invoke({})
    assert not first.startswith("Error:")
    moved = tmp_path / "moved"
    repo.rename(moved)
    br_calls.clear()
    reply = board_list.invoke({})
    assert reply == f"Error: {_store(board).missing_repo_error()}"
    assert str(repo) in reply and "restore the folder" in reply
    assert br_calls == []
    moved.rename(repo)
    assert board_list.invoke({}) == first
    assert br_calls == []


def test_relative_db_uses_the_selected_projects_cache(board, tmp_path, br_calls):
    other = tmp_path / "other"
    other.mkdir()
    other_cfg = seeded_board.__wrapped__(other)
    _store(other_cfg)._run("update", "snap-00100", "--title", "Other database")
    board["db_path"] = ".beads/beads.db"
    board["projects"]["other"] = {"repo": str(other)}
    _store(other_cfg)._run("update", "snap-00100", "--add-label", "project:other", "--remove-label", "project:alpha")
    assert _client(board).get(BASE + "/features").status_code == 200
    board_list = _tools(board)["board_list"]
    reply = board_list.invoke({"project": "other"})
    assert _by_id(reply)["snap-00100"]["title"] == "Other database"
    br_calls.clear()
    assert board_list.invoke({"project": "other"}) == reply
    assert br_calls == []
    _store(other_cfg)._run("update", "snap-00100", "--title", "Other database changed")
    assert _by_id(board_list.invoke({"project": "other"}))["snap-00100"]["title"] == "Other database changed"


# ── r3: read-after-write ────────────────────────────────────────────────────────────


def test_list_right_after_a_tool_write_shows_the_write(board, br_calls):
    tools = _tools(board)
    assert _by_id(tools["board_list"].invoke({}))["snap-00100"]["title"] == "Card snap-00100"

    reply = tools["board_update_feature"].invoke({"feature_id": "snap-00100", "title": "Renamed by the agent"})
    assert not reply.startswith("Error:"), reply
    assert _by_id(tools["board_list"].invoke({}))["snap-00100"]["title"] == "Renamed by the agent"
    assert _by_id(tools["board_list"].invoke({"state": "backlog", "project": "alpha"}))["snap-00100"]["title"] == (
        "Renamed by the agent"
    )

    # …and once that listing is in, the board is unchanged again: no `br`.
    _settle(board)
    br_calls.clear()
    assert _by_id(tools["board_list"].invoke({}))["snap-00100"]["title"] == "Renamed by the agent"
    assert br_calls == []


def test_a_tool_write_is_never_answered_from_the_previous_listing(board, br_calls, monkeypatch):
    """The store is slow and the stale wait is over at once, so a reader that MAY be given
    the previous listing is given it. After the agent's own write it may not be: the call
    waits for the listing taken after the write."""
    monkeypatch.setattr(read_cache, "STALE_WAIT_S", 0.01)
    tools = _tools(board)
    tools["board_list"].invoke({})
    br_calls.list_delay = 0.4

    for title, write in (
        ("Second title", {"feature_id": "snap-00100", "title": "Second title"}),
        ("Third title", {"feature_id": "snap-00100", "title": "Third title"}),
    ):
        assert not tools["board_update_feature"].invoke(write).startswith("Error:")
        reply = tools["board_list"].invoke({})
        assert STALE_LINE not in reply
        assert _by_id(reply)["snap-00100"]["title"] == title

    # A comment is not a projection write (no board revision bump), but a tool that wrote
    # one is still never shown the listing from before it.
    reply = tools["board_block_feature"].invoke(
        {"feature_id": "snap-00103", "reason": "waiting on a decision", "owner": "agent"}
    )
    assert not reply.startswith("Error:"), reply
    row = _by_id(tools["board_list"].invoke({}))["snap-00103"]
    assert row["blocked"] is True and "waiting on a decision" in row["blocked_reason"]


def test_a_loop_write_on_a_busy_store_is_answered_at_once_and_says_it_is_stale(board, br_calls, monkeypatch):
    """A write the agent did not make (the loop, the sweep): the call does not queue behind
    the busy store. It answers from the previous listing, says so, and the next call after
    the refresh lands is current."""
    monkeypatch.setattr(read_cache, "STALE_WAIT_S", 0.05)
    board_list = _tools(board)["board_list"]
    fresh = board_list.invoke({})
    store = _store(board)
    store._run("update", "snap-00100", "--title", "Renamed by the loop")  # not inside a tool call
    br_calls.list_delay = 0.6

    started = time.monotonic()
    reply = board_list.invoke({})
    assert time.monotonic() - started < 0.5, "the call waited for the slow listing"
    body, _, note = reply.partition("\n\n")
    assert body == fresh
    assert note.startswith("The board store is busy: this listing is from ") and "Call board_list again" in note

    _settle(board)
    reply = board_list.invoke({})
    assert STALE_LINE not in reply
    assert _by_id(reply)["snap-00100"]["title"] == "Renamed by the loop"


def test_a_write_by_another_process_is_seen(board, br_calls):
    """A hand-run `br` moves the store's file stamp, so the next call lists again."""
    board_list = _tools(board)["board_list"]
    board_list.invoke({})
    subprocess.run(
        ["br", "update", "snap-00100", "--title", "Renamed by hand", "--db", board["db_path"]],
        cwd=board["repo"],
        check=True,
        capture_output=True,
    )
    assert _by_id(board_list.invoke({}))["snap-00100"]["title"] == "Renamed by hand"


# ── one cache with the routes ───────────────────────────────────────────────────────


def _client(cfg) -> TestClient:
    app = FastAPI()
    app.include_router(api.build_data_router(cfg), prefix=BASE)
    return TestClient(app)


def test_the_tool_and_the_routes_read_one_listing(board, br_calls):
    client = _client(board)
    tools = _tools(board)
    listed = client.get(BASE + "/features")
    assert listed.status_code == 200 and br_calls
    assert read_cache.published(board["db_path"]) is not None

    br_calls.clear()
    rows = _by_id(tools["board_list"].invoke({}))
    assert br_calls == [], "the tool listed the store again instead of reading the routes' snapshot"
    assert set(rows) == {f["id"] for f in listed.json()["features"]}

    # A tool write is in the routes' next read as a fresh snapshot, and the tool's.
    assert (
        not tools["board_update_feature"]
        .invoke({"feature_id": "snap-00101", "title": "Tool wrote this"})
        .startswith("Error:")
    )
    after = client.get(BASE + "/features")
    assert after.headers["x-board-snapshot"] == "fresh"
    assert {f["id"]: f["title"] for f in after.json()["features"]}["snap-00101"] == "Tool wrote this"
    _settle(board)
    br_calls.clear()
    assert _by_id(tools["board_list"].invoke({}))["snap-00101"]["title"] == "Tool wrote this"
    assert br_calls == []

    # An API write is in the tool's next read.
    assert client.patch(BASE + "/features/snap-00102", json={"title": "API wrote this"}).status_code == 200
    assert _by_id(tools["board_list"].invoke({}))["snap-00102"]["title"] == "API wrote this"


def test_a_project_whose_folder_is_gone_is_still_refused_from_a_warm_cache(board, br_calls, tmp_path):
    """The routes' listing runs in the board's repo, so it succeeds; board_list for a
    project whose own folder is missing must keep naming that folder (#803)."""
    gone = tmp_path.resolve() / "gone"
    board["projects"]["gone"] = {"repo": str(gone)}
    client = _client(board)
    assert client.get(BASE + "/features").status_code == 200
    board_list = _tools(board)["board_list"]
    assert not board_list.invoke({"project": "alpha"}).startswith("Error:")

    reply = board_list.invoke({"project": "gone"})
    assert reply.startswith("Error:") and str(gone) in reply
    assert "remove the project or restore the folder" in reply


def test_concurrent_calls_on_a_cold_board_share_one_listing(board, br_calls):
    _client(board)  # the routes' cache is up, nothing listed yet
    board_list = _tools(board)["board_list"]
    br_calls.list_delay = 0.3
    replies: list[str] = []
    threads = [threading.Thread(target=lambda: replies.append(board_list.invoke({}))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(replies) == 4 and len(set(replies)) == 1
    assert [c[0] for c in br_calls].count("list") == 1


# ── a store that is not a `br` board is listed directly ─────────────────────────────


def test_a_store_without_a_change_token_is_listed_every_call(monkeypatch):
    class _Plain:
        def __init__(self):
            self.rows = []

        def list_features(self, state=None, include_archived=False):
            return [dict(r) for r in self.rows]

    plain = _Plain()
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: plain)
    board_list = _tools({})["board_list"]
    assert board_list.invoke({}) == "[]"
    plain.rows.append(
        dict(id="bd-1", title="New", board_state="backlog", blocked=False, pr_url="", priority=2, difficulty="")
    )
    assert [r["id"] for r in json.loads(board_list.invoke({}))] == ["bd-1"]


# ── the change stamp does not move on a `br` read ───────────────────────────────────
#
# `br` 0.1.x opens a write transaction to read: every list/show/ready moves the database's
# two SQLite header counters, the WAL header and both files' mtimes. On the file stamp
# alone the listing just taken was already out of date, so no call was ever answered from
# the cache (the 0.1.23 CI leg caught it; a newer `br` leaves the files alone).


def _read_like_br_01x(db: str) -> None:
    """What a `br` 0.1.x read leaves behind: the header counters moved, nothing else."""
    with open(db, "r+b") as fh:
        head = bytearray(fh.read(100))
        assert head.startswith(b"SQLite format 3\x00")
        for at in (24, 92):
            head[at : at + 4] = (int.from_bytes(head[at : at + 4], "big") + 1).to_bytes(4, "big")
        fh.seek(0)
        fh.write(head)
    with open(db + "-wal", "wb") as fh:
        fh.write(bytes(range(32)))  # a header with new salts, no frames


def test_a_read_that_touches_the_database_header_leaves_the_stamp_alone(board):
    db = board["db_path"]
    before_file, before = read_cache.store_rev(db), read_cache.listing_rev(db)
    _read_like_br_01x(db)
    assert read_cache.store_rev(db) != before_file, "the fake read must move the file stamp"
    assert read_cache.listing_rev(db) == before

    subprocess.run(
        ["br", "update", "snap-00100", "--title", "A real write", "--db", db],
        cwd=board["repo"],
        check=True,
        capture_output=True,
    )
    assert read_cache.listing_rev(db) != before


def test_a_real_br_read_leaves_the_stamp_alone(board):
    db = board["db_path"]
    before = read_cache.listing_rev(db)
    for args in (["list", "--json", "--limit", "0"], ["ready", "--json"], ["show", "snap-00100", "--json"]):
        subprocess.run(["br", *args, "--db", db], cwd=board["repo"], check=True, capture_output=True)
    assert read_cache.listing_rev(db) == before


def test_a_header_touching_read_between_calls_costs_no_listing(board, br_calls):
    """The whole path: the loop (or the listing itself, on 0.1.x) read the store between
    two board_list calls. The second is still answered from the snapshot."""
    board_list = _tools(board)["board_list"]
    first = board_list.invoke({})
    _read_like_br_01x(board["db_path"])
    br_calls.clear()
    assert board_list.invoke({}) == first
    assert br_calls == []


def test_frames_left_in_the_wal_move_the_stamp(board):
    db = board["db_path"]
    before = read_cache.listing_rev(db)
    with open(db + "-wal", "wb") as fh:
        fh.write(bytes(32) + b"a frame a writer has not checkpointed yet")
    assert read_cache.listing_rev(db) != before


def test_the_stamp_reads_the_content_once_per_file_change(board, monkeypatch):
    db = board["db_path"]
    reads = []
    real = read_cache._store_content
    monkeypatch.setattr(read_cache, "_store_content", lambda path: reads.append(path) or real(path))
    first = read_cache.listing_rev(db)
    assert read_cache.listing_rev(db) == first and len(reads) == 1
    _read_like_br_01x(db)
    assert read_cache.listing_rev(db) == first and len(reads) == 2
    assert read_cache.listing_rev(db) == first and len(reads) == 2


def test_the_stamp_falls_back_to_the_file_stamp(board, tmp_path, monkeypatch):
    # No database to read: the file stamp is all there is.
    missing = str(tmp_path / "nowhere" / "beads.db")
    assert read_cache.listing_rev(missing) == read_cache.store_rev(missing)
    # The files moved while the content was read (a write in flight): the content may be
    # half a write, so it is neither returned nor remembered.
    db = board["db_path"]
    stamps = iter(["during-1", "during-2"])
    monkeypatch.setattr(read_cache, "store_rev", lambda _db: next(stamps))
    assert read_cache.listing_rev(db, "at-start") == "at-start"
    assert db not in read_cache._listing_revs
