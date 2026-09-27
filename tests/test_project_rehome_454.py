"""#454 — which binding a card uses, cards orphaned on a project that no longer resolves,
and re-homing a card.

Seen live on a board moved from the legacy single-repo binding (flat ``project_board.repo``)
to registered ``projects``:

* the Ready gate's refusal said the root was "set via project_board.repo" when it came from
  ``project_board.projects.<name>.repo`` — and the flat key pointed at ANOTHER clone of the
  same repo, so the agent could not tell which checkout, or which key, mattered;
* the old cards still carried project ``default``, which no longer resolved, and nothing said;
* ``board_update_feature`` had no ``project`` argument, so those cards could only be
  cancelled and recreated (new ids, lost history) although none had ever been dispatched.

The store writes (the re-home label, the Ready gate) run through the REAL ``br``; the
legacy-binding advisory reads REAL git remotes.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from project_board import health, setup_check
from project_board import store as store_mod
from project_board.projects import orphaned_cards, rehome_hint, repo_config_key, resolve_projects
from project_board.store import BeadsBoard, BoardError

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH (CI installs it and sets PB_REQUIRE_BR=1)",
)


def _projects(tmp_path: Path) -> dict:
    web, docs = tmp_path / "web", tmp_path / "docs"
    web.mkdir(exist_ok=True)
    docs.mkdir(exist_ok=True)
    (web / "app.py").write_text("x = 1\n")
    (docs / "guide.md").write_text("# g\n")
    return resolve_projects({"projects": {"web": {"repo": str(web)}, "docs": {"repo": str(docs)}}})


@pytest.fixture
def board(tmp_path):
    """A REAL board over a throwaway workspace, serving two projects; the flat repo is a
    THIRD directory — the legacy binding left behind."""
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    return BeadsBoard(repo=str(legacy), actor="test", projects=_projects(tmp_path), default_project="web")


# ── the config key a root came from ────────────────────────────────────────────────
def test_repo_config_key_names_the_project_entry_or_the_flat_key(tmp_path):
    projects = _projects(tmp_path)
    assert repo_config_key("web", projects) == "project_board.projects.web.repo"
    assert repo_config_key("default", projects) == "project_board.repo"  # not a project
    implicit = resolve_projects({"repo": "/flat"})
    assert repo_config_key("default", implicit) == "project_board.repo"  # synthesized from the flat key


@requires_br
def test_the_ready_gate_names_the_project_key_not_the_flat_one(board):
    f = board.create_feature(
        "Web card", spec="s", acceptance_criteria="a", files_to_modify=["missing.py"], project="web"
    )
    with pytest.raises(BoardError) as exc:
        board.mark_ready(f["id"])
    err = str(exc.value)
    assert "set via project_board.projects.web.repo" in err
    assert "set via project_board.repo" not in err


@requires_br
def test_the_ready_gate_says_why_an_orphaned_card_fell_back_to_the_flat_repo(board):
    f = board.create_feature(
        "Old card", spec="s", acceptance_criteria="a", files_to_modify=["app.py"], project="default"
    )
    with pytest.raises(BoardError) as exc:
        board.mark_ready(f["id"])
    err = str(exc.value)
    assert "set via project_board.repo" in err
    assert "'default' is not in project_board.projects" in err and "board_update_feature(project=...)" in err


# ── re-homing a card ───────────────────────────────────────────────────────────────
@requires_br
def test_a_backlog_card_is_rehomed_in_place_keeping_its_id(board):
    f = board.create_feature("Old card", spec="s", project="default")
    moved = board.update_feature(f["id"], project="docs")
    assert moved["id"] == f["id"]
    assert moved["project"] == "docs"
    assert [lbl for lbl in moved["labels"] if lbl.startswith("project:")] == ["project:docs"]
    comments = [str(c) for c in board.feature_comments(f["id"])]
    assert any("project re-homed: default → docs" in c for c in comments)


@requires_br
def test_a_rehomed_card_passes_the_ready_gate_in_its_new_repo(board):
    f = board.create_feature(
        "Old card", spec="s", acceptance_criteria="a", files_to_modify=["app.py"], project="default"
    )
    board.update_feature(f["id"], project="web")
    assert board.mark_ready(f["id"])["board_state"] == "ready"


@requires_br
def test_rehoming_to_an_unknown_project_is_refused_naming_the_known_ones(board):
    f = board.create_feature("Old card", spec="s", project="default")
    with pytest.raises(BoardError, match=r"'nope' is not a project on this board \(known: 'docs', 'web'\)"):
        board.update_feature(f["id"], project="nope")
    assert board.get_feature(f["id"])["project"] == "default"  # nothing written


@requires_br
def test_a_ready_card_moves_only_if_its_files_exist_in_the_new_repo(board):
    f = board.create_feature("Web card", spec="s", acceptance_criteria="a", files_to_modify=["app.py"], project="web")
    board.mark_ready(f["id"])
    with pytest.raises(BoardError, match=r"don't exist in 'docs'.*app\.py"):
        board.update_feature(f["id"], project="docs")
    # fixing files_to_modify in the SAME call is the way through
    moved = board.update_feature(f["id"], project="docs", files_to_modify=["guide.md"])
    assert moved["project"] == "docs" and moved["files_to_modify"] == ["guide.md"]


@requires_br
def test_an_in_progress_card_is_refused_with_the_reason(board):
    f = board.create_feature("Web card", spec="s", acceptance_criteria="a", files_to_modify=["app.py"], project="web")
    board.mark_ready(f["id"])
    assert board.claim(f["id"]) is not None
    with pytest.raises(BoardError, match="it is in_progress — only a backlog or ready card can move"):
        board.update_feature(f["id"], project="docs")


@pytest.mark.parametrize(
    "feature,reason",
    [
        ({"board_state": "in_review"}, "it is in_review"),
        ({"board_state": "done"}, "it is done"),
        ({"board_state": "backlog", "pr_url": "https://github.com/o/r/pull/7"}, "already has a PR"),
        ({"board_state": "backlog", "attempts": [1]}, "dispatched before, so a branch may exist"),
        ({"board_state": "ready", "verified_sha": "abc123"}, "dispatched before"),
    ],
)
def test_rehome_refusals_name_what_is_bound_to_the_old_repo(tmp_path, monkeypatch, feature, reason):
    monkeypatch.setattr(store_mod.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    b = BeadsBoard(repo=str(tmp_path), projects=_projects(tmp_path))
    assert reason in b._rehome_refusal({"id": "bd-1", **feature}, "docs")


def test_a_clean_backlog_card_has_no_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    b = BeadsBoard(repo=str(tmp_path), projects=_projects(tmp_path))
    assert b._rehome_refusal({"id": "bd-1", "board_state": "backlog", "attempts": []}, "docs") == ""


# ── orphaned cards: named, with the re-home call ─────────────────────────────────────
def test_orphaned_cards_are_the_live_ones_whose_project_does_not_resolve(tmp_path):
    projects = _projects(tmp_path)
    feats = [
        {"id": "a", "title": "A", "board_state": "backlog", "project": "default"},
        {"id": "b", "title": "B", "board_state": "done", "project": "default"},  # terminal: not flagged
        {"id": "c", "title": "C", "board_state": "ready", "project": "web"},  # resolves
        {"id": "d", "title": "D", "board_state": "backlog", "project": ""},  # unlabeled → default project
    ]
    orphans = orphaned_cards(feats, projects, "web")
    assert [o["id"] for o in orphans] == ["a"]
    assert "board_update_feature(feature_id='a', project='web')" in orphans[0]["hint"]


def test_rehome_hint_lists_the_projects_when_there_is_no_default(tmp_path):
    hint = rehome_hint("a", "default", _projects(tmp_path), "")
    assert "project=<one of 'docs', 'web'>" in hint


@requires_br
def test_board_list_flags_orphaned_rows_and_offers_the_rehome(board, tmp_path, monkeypatch):
    import project_board as pb

    orphan = board.create_feature("Old card", spec="s", project="default")
    fine = board.create_feature("Web card", spec="s", project="web")
    monkeypatch.setattr(store_mod, "get_store", lambda **_kw: board)
    cfg = {
        "repo": str(tmp_path / "legacy"),
        "default_project": "web",
        "projects": {"web": {"repo": str(tmp_path / "web")}, "docs": {"repo": str(tmp_path / "docs")}},
    }
    tools = {t.name: t for t in pb._board_tools(cfg)}
    rows = {r["id"]: r for r in json.loads(tools["board_list"].invoke({}))}
    assert rows[orphan["id"]]["project_unresolved"] is True
    assert f"board_update_feature(feature_id={orphan['id']!r}, project='web')" in rows[orphan["id"]]["project_hint"]
    assert "project_unresolved" not in rows[fine["id"]]

    # …and the tool re-homes it
    reply = tools["board_update_feature"].invoke({"feature_id": orphan["id"], "project": "web"})
    assert not reply.startswith("Error"), reply
    assert board.get_feature(orphan["id"])["project"] == "web"


@requires_br
def test_the_update_tool_returns_the_refusal_as_an_error(board, tmp_path, monkeypatch):
    import project_board as pb

    f = board.create_feature("Web card", spec="s", acceptance_criteria="a", files_to_modify=["app.py"], project="web")
    board.mark_ready(f["id"])
    board.claim(f["id"])
    monkeypatch.setattr(store_mod, "get_store", lambda **_kw: board)
    tools = {t.name: t for t in pb._board_tools({"projects": {"web": {"repo": str(tmp_path / "web")}}})}
    reply = tools["board_update_feature"].invoke({"feature_id": f["id"], "project": "docs"})
    assert reply.startswith("Error:") and "only a backlog or ready card can move" in reply


@requires_br
async def test_the_sweep_publishes_orphaned_cards_for_status(board, tmp_path):
    from project_board.loop import BoardLoop

    orphan = board.create_feature("Old card", spec="s", project="default")
    loop = BoardLoop(
        {
            "coder": "proto",
            "merge_poll": False,
            "default_project": "web",
            "projects": {"web": {"repo": str(tmp_path / "web")}, "docs": {"repo": str(tmp_path / "docs")}},
        }
    )
    await loop._publish_orphaned_cards(board)
    snap = health.orphaned_cards_snapshot()
    assert [o["id"] for o in snap] == [orphan["id"]] and snap[0]["project"] == "default"


def test_status_serves_orphans_and_stale_base_checkouts(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from project_board import api

    health.publish_orphaned_cards([{"id": "ds-2wx", "project": "default", "hint": "re-home it"}])
    health.publish_base_checkouts(
        {
            "web": {"state": "fast_forwarded", "behind": 3, "detail": ""},
            "docs": {"state": "stale", "behind": 2, "detail": "2 commit(s) behind origin/main, not fast-forwarded"},
        }
    )
    monkeypatch.setattr(api, "get_store", lambda **_kw: object())
    app = FastAPI()
    app.include_router(api.build_data_router({}), prefix="/api/plugins/project_board")
    body = TestClient(app).get("/api/plugins/project_board/status").json()
    assert body["orphaned_cards"][0]["id"] == "ds-2wx"
    assert body["stale_base_checkouts"] == {"docs": "2 commit(s) behind origin/main, not fast-forwarded"}
    assert body["base_checkouts"]["web"]["state"] == "fast_forwarded"
    health.publish_orphaned_cards([])
    health.publish_base_checkouts({})


# ── the legacy binding beside projects: (real git remotes) ─────────────────────────
def _git_only(cmd, **kw):
    """The preflight's process runner with REAL git (the remote reads under test) and a
    canned answer for anything else (the suite never shells `br`/`gh` from here)."""
    if cmd and cmd[0] == "git":
        return subprocess.run(cmd, **kw)
    return subprocess.CompletedProcess(cmd, 0, stdout="br 0.0.0-test\n", stderr="")


def _clone_of(path: Path, remote: str) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)
    return path


def test_a_legacy_repo_that_is_another_clone_of_a_project_is_flagged_as_ambiguous(tmp_path):
    remote = "https://github.com/protoLabsAI/protoContent.git"
    legacy = _clone_of(tmp_path / "protoContent-designsystem", remote)
    project = _clone_of(tmp_path / "workspace" / "protoContent", remote)
    other = _clone_of(tmp_path / "workspace" / "ds", "https://github.com/protoLabsAI/design-system.git")
    cfg = {
        "repo": str(legacy),
        "local_gate_cmd": "pnpm test",
        "default_project": "protoContent",
        "projects": {
            "protoContent": {"repo": str(project)},
            "ds": {"repo": str(other), "local_gate_cmd": "npm test"},
        },
    }
    info = setup_check.legacy_binding(cfg, run=_git_only)
    assert info["github"] == "protoLabsAI/protoContent"
    assert info["same_remote"] == ["protoContent"] and info["same_path"] == []
    assert info["gate_fallback_projects"] == ["protoContent"]  # ds sets its own gate
    hint = setup_check.legacy_binding_hint(info)
    assert "ANOTHER clone of protoLabsAI/protoContent" in hint
    assert "project_board.projects.<name>.repo" in hint
    assert "default project's repo (protoContent)" in hint
    assert "local_gate_cmd is still the gate for project(s) whose entry sets none: 'protoContent'" in hint
    assert "Remove the legacy project_board.repo" in hint


def test_a_legacy_repo_that_is_a_project_checkout_is_only_a_duplicate(tmp_path):
    repo = _clone_of(tmp_path / "web", "git@github.com:o/web.git")
    info = setup_check.legacy_binding({"repo": str(repo), "projects": {"web": {"repo": str(repo)}}}, run=_git_only)
    assert info["same_path"] == ["web"] and info["same_remote"] == []
    assert "only duplicates it" in setup_check.legacy_binding_hint(info)


@pytest.mark.parametrize(
    "cfg",
    [
        {"repo": "/flat"},  # no projects map: the flat key IS the binding
        {"repo": ".", "projects": {"web": {"repo": "/w"}}},  # the shipped default, not a choice
        {"repo": "", "projects": {"web": {"repo": "/w"}}},
    ],
)
def test_no_advisory_without_a_legacy_binding_beside_projects(cfg):
    assert setup_check.legacy_binding(cfg, run=_git_only) == {}
    assert setup_check.legacy_binding_hint({}) == ""


def test_setup_status_carries_the_advisory_and_the_reporter_forwards_it(tmp_path):
    remote = "https://github.com/o/r.git"
    legacy = _clone_of(tmp_path / "old", remote)
    project = _clone_of(tmp_path / "new", remote)
    cfg = {"coder": "proto", "repo": str(legacy), "projects": {"r": {"repo": str(project)}}}
    status = setup_check.setup_status(
        cfg, which=lambda _n: "/usr/bin/x", delegates=lambda _n: object(), run=_git_only, loop_snapshot={}
    )
    assert status["legacy_binding"]["same_remote"] == ["r"]
    assert "ANOTHER clone" in status["legacy_binding_hint"]
    assert status["repo"]["ok"] is True  # an advisory, never a failing check

    sent = []

    class _Host:
        def report_setup_gap(self, key, message):
            sent.append((key, message))

    setup_check.GapReporter(_Host()).report(status)
    assert dict(sent)["legacy_binding"] == status["legacy_binding_hint"]


@requires_br
def test_patch_features_rehomes_and_refuses_like_the_tool(board, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from project_board import api

    orphan = board.create_feature("Old card", spec="s", project="default")
    monkeypatch.setattr(api, "get_store", lambda **_kw: board)
    app = FastAPI()
    cfg = {"default_project": "web", "projects": {n: {"repo": e["repo"]} for n, e in board.projects.items()}}
    app.include_router(api.build_data_router(cfg), prefix="/api/plugins/project_board")
    c = TestClient(app)
    ok = c.patch(f"/api/plugins/project_board/features/{orphan['id']}", json={"project": "docs"})
    assert ok.status_code == 200 and ok.json()["project"] == "docs"
    bad = c.patch(f"/api/plugins/project_board/features/{orphan['id']}", json={"project": "nope"})
    assert bad.status_code == 400 and "not a project on this board" in bad.json()["detail"]
    blank = c.patch(f"/api/plugins/project_board/features/{orphan['id']}", json={"project": ""})
    assert blank.status_code == 200 and blank.json()["project"] == "docs"  # "" leaves it alone
