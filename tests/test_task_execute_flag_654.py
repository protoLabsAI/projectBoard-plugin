"""#654: task creation opts into execution routing through persisted labels."""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import project_board as pb
from project_board import api
from project_board.store import BoardError


@pytest.fixture
def board(make_board, monkeypatch):
    card = {"id": "bd-task", "title": "Release check", "board_state": "backlog", "labels": []}

    def run(*args, **kwargs):
        if args[0] == "create":
            card["issue_type"] = args[args.index("--type") + 1]
            return card["id"]
        if args[0] == "update":
            for i, arg in enumerate(args):
                if arg == "--add-label":
                    card["labels"].append(args[i + 1])
            return ""
        raise AssertionError(f"Unexpected br call: {args}")

    store = make_board(run)
    monkeypatch.setattr(store, "get_feature", lambda fid: dict(card))
    monkeypatch.setattr(store, "list_features", lambda **kw: [])
    monkeypatch.setattr(pb, "_ready_dry_run", lambda *args: None)
    monkeypatch.setattr("project_board.store.get_store", lambda **kw: store)
    monkeypatch.setattr(api, "get_store", lambda **kw: store)
    return store


@pytest.mark.parametrize("entry", ["tool", "http"])
@pytest.mark.parametrize("flag", [None, False, True])
def test_create_task_execute_label(board, entry, flag):
    payload = {"title": "Release check"}
    if flag is not None:
        payload["execute"] = flag
    if entry == "tool":
        tool = {t.name: t for t in pb._board_tools({})}["board_create_task"]
        result = json.loads(tool.invoke(payload))
    else:
        app = FastAPI()
        app.include_router(api.build_data_router({}))
        with TestClient(app) as client:
            response = client.post("/features", json={**payload, "issue_type": "task"})
        assert response.status_code == 200
        result = response.json()
    card = board.get_feature(result["id"])
    assert card["issue_type"] == "task"
    expected = (["execute"] if flag else []) + (["project:default"] if entry == "http" else [])
    assert card["labels"] == expected


def test_execute_label_preserves_other_creation_labels(board):
    card = board.create_feature("Release check", issue_type="task", execute=True, project="demo", foundation=True)
    assert set(card["labels"]) == {"execute", "foundation", "project:demo"}


def test_execute_label_failure_retains_created_card_and_warning(board, monkeypatch):
    real_run = board._run

    def fail_update(*args, **kwargs):
        if args[0] == "update":
            raise BoardError("label write failed")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(board, "_run", fail_update)
    tool = {t.name: t for t in pb._board_tools({})}["board_create_task"]
    result = json.loads(tool.invoke({"title": "Release check", "execute": True}))
    assert result["id"] == "bd-task"
    assert result["enrichment_failed"] is True
    assert "execute" in result["missing_fields"]
    assert board.get_feature(result["id"])["labels"] == []


def test_execution_guidance():
    tool = {t.name: t for t in pb._board_tools({})}["board_create_task"]
    description = " ".join(tool.description.split())
    assert "go to a coder: pass `execute=true` and leave `assignee` empty" in description
    assert "never assign one to yourself" in description
    assert "Writing-only tasks (a doc, a decision) may be self-assigned" in description
    guide = (Path(pb.__file__).parent / "skills/board-tool-reference/SKILL.md").read_text()
    assert "### Execution vs writing tasks" in guide
    assert "NEVER assign an execution task to itself" in guide
