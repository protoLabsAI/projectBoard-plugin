"""`board_remove_project` (#803, 2/2): remove a retired project from chat.

Before this tool the only way to drop a board project was `DELETE /projects/{name}` (or
the Settings editor that calls it), so an agent that found a project's repo folder gone
could only tell the operator to go and click. The tool runs the SAME active-card guard as
the route (`project_registry.unused_project_guard`) and tells the agent what is left on
the host side, so it can pass that on.

Every test runs against a real temp config document (`_host_apply.HostConfig`, the double
that applies a patch with the host's own rules) and real git checkouts.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
from pathlib import Path

from _host_apply import HostConfig, wire_host
from fastapi import FastAPI
from fastapi.testclient import TestClient

import project_board as pb
from project_board import api
from project_board import project_registry as registry
from project_board.project_registry import build_remove_tool, unused_project_guard, upsert_project
from project_board.store import BoardError


def _checkout(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    return path


def _host(tmp_path: Path, **doc) -> HostConfig:
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    base = {"onboarding": {"enabled": True, "root": str(root)}, "project_board": {"projects": {}}}
    base.update(doc)
    return HostConfig(base)


def _board(host: HostConfig) -> dict:
    return host.doc["project_board"]["projects"]


def _cards(*cards):
    """A store listing: records that it was asked for archived cards too."""
    calls = []

    def list_features(state=None, include_archived=False):
        calls.append({"include_archived": include_archived, "thread": threading.get_ident()})
        return list(cards)

    list_features.calls = calls
    return list_features


async def _remove(list_features, name: str) -> str:
    return await build_remove_tool({}, list_features).ainvoke({"name": name})


# ── removes an unused project ────────────────────────────────────────────────────────
async def test_removes_an_unused_project_and_names_the_new_default(monkeypatch, tmp_path):
    host = _host(tmp_path)
    wire_host(monkeypatch, host)
    old = _checkout(tmp_path / "workspace" / "old")
    keep = _checkout(tmp_path / "workspace" / "keep")
    await upsert_project("old", str(old), make_default=True)
    await upsert_project("keep", str(keep))
    assert host.doc["project_board"]["default_project"] == "old"

    # Finished and cancelled cards keep their label as history; they do not hold the project.
    listing = _cards(
        {"id": "bd-done", "project": "old", "board_state": "done"},
        {"id": "bd-cut", "project": "old", "board_state": "cancelled"},
        {"id": "bd-other", "project": "keep", "board_state": "in_progress"},
    )
    out = await _remove(listing, "old")

    assert out.startswith("Removed board project 'old'")
    assert set(_board(host)) == {"keep"}
    assert host.doc["project_board"]["default_project"] == "keep"
    assert "The default project is now 'keep'." in out
    assert listing.calls and listing.calls[0]["include_archived"] is True


async def test_a_removed_non_default_project_does_not_announce_a_default_change(monkeypatch, tmp_path):
    host = _host(tmp_path)
    wire_host(monkeypatch, host)
    await upsert_project("main-app", str(_checkout(tmp_path / "workspace" / "main-app")), make_default=True)
    await upsert_project("old", str(_checkout(tmp_path / "workspace" / "old")))

    out = await _remove(_cards(), "old")

    assert out.startswith("Removed board project 'old'")
    assert "default project" not in out
    assert host.doc["project_board"]["default_project"] == "main-app"


async def test_removes_a_project_whose_repo_folder_is_gone(monkeypatch, tmp_path):
    """The case the issue is about: the folder was deleted, the board entry lingers."""
    gone = tmp_path / "workspace" / "gone"
    host = _host(tmp_path, project_board={"projects": {"gone": {"repo": str(gone)}, "live": {"repo": "/live"}}})
    wire_host(monkeypatch, host)
    assert not gone.exists()

    out = await _remove(_cards(), "gone")

    assert out.startswith("Removed board project 'gone'") and str(gone) in out
    assert set(_board(host)) == {"live"}


# ── refuses while active cards use it ────────────────────────────────────────────────
async def test_refuses_while_an_active_card_uses_the_project_and_removes_nothing(monkeypatch, tmp_path):
    host = _host(tmp_path)
    wire_host(monkeypatch, host)
    await upsert_project("web", str(_checkout(tmp_path / "workspace" / "web")))
    before_board, before_host = dict(_board(host)), list(host.doc.get("projects") or [])
    patches = len(host.patches)

    out = await _remove(
        _cards(
            {"id": "bd-live", "project": "web", "board_state": "in_progress"},
            {"id": "bd-old", "project": "web", "board_state": "done"},
        ),
        "web",
    )

    assert out.startswith("Error:") and "bd-live" in out and "bd-old" not in out
    assert "Nothing was removed" in out
    assert _board(host) == before_board
    assert list(host.doc.get("projects") or []) == before_host
    assert len(host.patches) == patches  # no config write was attempted


async def test_refuses_an_unstamped_active_card_when_the_project_is_the_default(monkeypatch, tmp_path):
    host = _host(tmp_path)
    wire_host(monkeypatch, host)
    await upsert_project("web", str(_checkout(tmp_path / "workspace" / "web")))  # sole project = default

    out = await _remove(_cards({"id": "bd-legacy", "project": "", "board_state": "ready"}), "web")

    assert out.startswith("Error:") and "bd-legacy" in out
    assert "web" in _board(host)


async def test_an_unreadable_board_refuses_rather_than_removing(monkeypatch, tmp_path):
    host = _host(tmp_path)
    wire_host(monkeypatch, host)
    await upsert_project("web", str(_checkout(tmp_path / "workspace" / "web")))

    def broken(**_kw):
        raise BoardError("br timed out")

    out = await _remove(broken, "web")

    assert out.startswith("Error:") and "cannot prove" in out and "br timed out" in out
    assert "web" in _board(host)


async def test_an_unknown_name_is_a_plain_error_listing_the_projects(monkeypatch, tmp_path):
    host = _host(tmp_path)
    wire_host(monkeypatch, host)
    await upsert_project("web", str(_checkout(tmp_path / "workspace" / "web")))
    listing = _cards()

    out = await _remove(listing, "nope")

    assert out.startswith("Error: unknown project 'nope'") and "web" in out
    assert "web" in _board(host)
    assert listing.calls == []  # nothing to check, nothing read


async def test_with_no_host_config_it_answers_an_error_instead_of_raising():
    out = await _remove(_cards(), "web")
    assert isinstance(out, str) and out.startswith("Error:")


# ── what is left on the host side ────────────────────────────────────────────────────
async def test_says_nothing_is_left_when_the_board_removed_the_entry_it_added(monkeypatch, tmp_path):
    host = _host(tmp_path)
    wire_host(monkeypatch, host)
    repo = _checkout(tmp_path / "workspace" / "web")
    await upsert_project("web", str(repo))
    assert [e["name"] for e in host.doc["projects"]] == ["web"]  # the board added it

    out = await _remove(_cards(), "web")

    assert host.doc["projects"] == []
    assert "No host work-folder entry is left" in out
    assert "the board also removed the managed project 'web'" in out
    assert "still left" not in out


async def test_names_the_operators_managed_project_that_is_left_behind(monkeypatch, tmp_path):
    """onboard_project (the operator) registered the folder first; the board never owned
    that entry, so it stays, and the reply must say so with the path."""
    repo = _checkout(tmp_path / "workspace" / "protoContent")
    onboarded = {"name": "protoContent", "path": str(repo), "default_branch": "main", "write": True}
    host = _host(tmp_path, projects=[dict(onboarded)])
    wire_host(monkeypatch, host)
    await upsert_project("pc", str(repo))

    out = await _remove(_cards(), "pc")

    assert "pc" not in _board(host)
    assert host.doc["projects"] == [onboarded]
    assert "still left" in out
    assert "managed project 'protoContent'" in out and str(repo) in out
    assert "No host work-folder entry is left" not in out


async def test_names_a_leftover_filesystem_projects_entry(monkeypatch, tmp_path):
    """An explicit fence entry the operator wrote also still reaches the folder."""
    gone = tmp_path / "workspace" / "gone"
    fence = [{"name": "scratch", "path": str(gone), "write": True}]
    host = _host(
        tmp_path,
        filesystem={"projects": [dict(f) for f in fence], "enabled": True},
        project_board={"projects": {"gone": {"repo": str(gone)}}},
    )
    wire_host(monkeypatch, host)

    out = await _remove(_cards(), "gone")

    assert _board(host) == {}
    assert host.doc["filesystem"]["projects"] == fence
    assert "still left" in out and "filesystem.projects entry 'scratch'" in out and str(gone) in out


async def test_says_why_when_a_sibling_project_keeps_the_managed_entry(monkeypatch, tmp_path):
    host = _host(tmp_path)
    wire_host(monkeypatch, host)
    repo = _checkout(tmp_path / "workspace" / "web")
    await upsert_project("web", str(repo))
    await upsert_project("web-two", str(repo))  # a second board project on the same checkout

    out = await _remove(_cards(), "web")

    assert [e["name"] for e in host.doc["projects"]] == ["web"]
    assert "still left" in out and "'web-two' still uses that checkout" in out


# ── one guard, off the event loop ────────────────────────────────────────────────────
async def test_the_guard_reads_the_store_off_the_event_loop():
    """The store can shell out to `br`; on the loop thread that stalls every other route
    and the tick. The listing must run in a worker thread while the loop keeps turning."""
    loop_thread = threading.get_ident()
    seen: dict = {}
    ticks = 0

    def slow_listing(**_kw):
        seen["thread"] = threading.get_ident()
        threading.Event().wait(0.15)
        return []

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ticker())
    try:
        await unused_project_guard(slow_listing)("web", "")
    finally:
        task.cancel()
    assert seen["thread"] != loop_thread
    assert ticks >= 3, "the event loop stalled while the store was read"


def test_the_registered_tool_uses_the_board_store_and_the_shared_guard(monkeypatch, tmp_path):
    """Through the real registration (`_board_tools`): the tool reads the board's own
    store, and its check is built by the shared guard function."""
    host = _host(tmp_path, project_board={"projects": {"web": {"repo": str(tmp_path / "web")}}})
    wire_host(monkeypatch, host)

    class Store:
        def list_features(self, state=None, include_archived=False):
            return [{"id": "bd-9", "project": "web", "board_state": "backlog"}]

    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: Store())
    built = []
    real = registry.unused_project_guard
    monkeypatch.setattr(registry, "unused_project_guard", lambda *a, **k: built.append("tool") or real(*a, **k))
    tool = next(t for t in pb._board_tools({}) if t.name == "board_remove_project")
    assert set(tool.args) == {"name"}

    out = asyncio.run(tool.ainvoke({"name": "web"}))

    assert built == ["tool"]
    assert out.startswith("Error:") and "bd-9" in out
    assert "web" in _board(host)


def test_the_delete_route_builds_its_check_from_the_same_guard(monkeypatch, tmp_path):
    host = _host(tmp_path, project_board={"projects": {"web": {"repo": str(tmp_path / "web")}}})
    wire_host(monkeypatch, host)

    class Store:
        def list_features(self, state=None, include_archived=False):
            return [{"id": "bd-9", "project": "web", "board_state": "in_review"}]

    monkeypatch.setattr(api, "get_store", lambda **_kw: Store())
    built = []
    real = registry.unused_project_guard
    monkeypatch.setattr(registry, "unused_project_guard", lambda *a, **k: built.append("route") or real(*a, **k))
    app = FastAPI()
    app.include_router(api.build_data_router({}), prefix="/api/plugins/project_board")

    response = TestClient(app).delete("/api/plugins/project_board/projects/web")

    assert built == ["route"]
    assert response.status_code == 409 and "bd-9" in response.json()["detail"]
    assert "web" in _board(host)
    # and the route no longer carries its own copy of the check
    assert "still referenced by active board card" not in Path(api.__file__).read_text()


def test_the_delete_route_reports_what_is_left_on_the_host(monkeypatch, tmp_path):
    repo = _checkout(tmp_path / "workspace" / "web")
    onboarded = {"name": "web", "path": str(repo), "write": True}
    host = _host(tmp_path, projects=[dict(onboarded)], project_board={"projects": {"web": {"repo": str(repo)}}})
    wire_host(monkeypatch, host)

    class Store:
        def list_features(self, state=None, include_archived=False):
            return []

    monkeypatch.setattr(api, "get_store", lambda **_kw: Store())
    app = FastAPI()
    app.include_router(api.build_data_router({}), prefix="/api/plugins/project_board")

    body = TestClient(app).delete("/api/plugins/project_board/projects/web").json()

    assert body["ok"] is True and body["deleted"] is True and body["repo"] == str(repo)
    assert body["host_entries_left"] == [{"kind": "managed project", "name": "web", "path": str(repo)}]
