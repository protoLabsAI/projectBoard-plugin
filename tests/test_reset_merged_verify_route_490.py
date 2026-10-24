"""HTTP reset uses the same durable reset and live cache invalidation as the tool."""

from copy import deepcopy

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from project_board import api
from project_board.loop import BoardLoop, _common
from project_board.store import BoardTimeout
from test_merge_hold_setter_532 import _bead, _FakeBr

PREFIX = "/api/plugins/project_board"
PATH = "/features/{fid}/reset-merged-verify-budget"
AUTH = {"Authorization": "Bearer operator-token"}


@pytest.fixture
def reset_route(make_board, monkeypatch):
    fake = _FakeBr(
        [
            _bead("bd-1", status="in_progress", labels=["in-review", "budget:merged-verify:6", "budget:ci:2"]),
            _bead("bd-2", labels=["budget:merged-verify:3"]),
        ]
    )
    board = make_board(fake)
    monkeypatch.setattr(api, "get_store", lambda **_kw: board)
    loop = BoardLoop({})
    loop._merged_verify_attempts.update({"bd-1": 6, "bd-2": 3})
    _common._register_loop(loop)

    # The host supplies this gate at the data-router mount; the public router
    # deliberately has no bearer dependency and must never expose this mutation.
    def operator_gate(authorization: str = Header(default="")):
        if authorization != AUTH["Authorization"]:
            raise HTTPException(401, "operator bearer required")

    app = FastAPI()
    app.include_router(api.build_router({}), prefix="/plugins/project_board")
    app.include_router(api.build_data_router({}), prefix=PREFIX, dependencies=[Depends(operator_gate)])
    return TestClient(app), fake, board, loop


@pytest.mark.parametrize("live", [True, False])
def test_reset_clears_only_named_budget_and_reports_live_cache(reset_route, live):
    client, fake, board, loop = reset_route
    if not live:
        _common._loop_slot().loop = None
    response = client.post(PREFIX + PATH.format(fid="bd-1"), headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "id": "bd-1",
        "state": "in_review",
        "merged_verify_budget_reset": True,
        "cache_cleared": live,
    }
    assert board.get_feature("bd-1")["labels"] == ["in-review", "budget:ci:2"]
    assert fake.beads["bd-2"]["labels"] == ["budget:merged-verify:3"]
    assert loop._merged_verify_attempts == {"bd-1": 0 if live else 6, "bd-2": 3}
    assert len(fake.comments_for("bd-1")) == 1
    assert "merged-verify budget reset" in fake.comments_for("bd-1")[0]


@pytest.mark.parametrize("fid", ["missing", "%20%20", ""])
def test_unknown_or_blank_changes_nothing(reset_route, fid):
    client, fake, _, loop = reset_route
    before = deepcopy(fake.beads)
    response = client.post(PREFIX + PATH.format(fid=fid), headers=AUTH)
    assert response.status_code == 404
    assert fake.beads == before
    assert loop._merged_verify_attempts == {"bd-1": 6, "bd-2": 3}
    assert fake.count("update") == fake.count("comments") == 0


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-token"}])
def test_requires_operator_gate_and_is_absent_from_public_router(reset_route, headers):
    client, fake, _, loop = reset_route
    before = deepcopy(fake.beads)
    path = PATH.format(fid="bd-1")
    assert client.post(PREFIX + path, headers=headers).status_code == 401
    assert client.post("/plugins/project_board" + path, headers=AUTH).status_code == 404
    assert fake.beads == before
    assert fake.calls == []
    assert loop._merged_verify_attempts == {"bd-1": 6, "bd-2": 3}


def test_store_timeout_does_not_reset_cache(reset_route, monkeypatch):
    client, fake, board, loop = reset_route

    def timeout(fid):
        raise BoardTimeout("store stalled")

    monkeypatch.setattr(board, "reset_merged_verify_budget", timeout)
    response = client.post(PREFIX + PATH.format(fid="bd-1"), headers=AUTH)
    assert response.status_code == 503
    assert loop._merged_verify_attempts == {"bd-1": 6, "bd-2": 3}
    assert fake.count("update") == fake.count("comments") == 0
