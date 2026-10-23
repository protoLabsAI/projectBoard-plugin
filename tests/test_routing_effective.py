"""Host-free effective-routes view contracts (#578).

Covers GET /routing/effective: per-project implement/fix/escalate/review/helpers routes
with the exclusive filter applied and the helper paths marked uncovered; per-delegate down
marks and subscription headroom (unknown == never an error); and the "starts on X because
Y" reason, which must match what the loop's own prefer_live_sibling / prefer_headroom_sibling
return for the same inputs."""

import copy

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from project_board import api, routing
from project_board.loop import (
    clear_provider_down,
    mark_provider_down,
    prefer_headroom_sibling,
    prefer_live_sibling,
)


LADDER = {"standard": ["sonnet", "codex"], "reasoning": ["sonnet"], "advanced": ["codex", "sonnet"]}


@pytest.fixture(autouse=True)
def _no_down_marks():
    # Down marks are process-global (loop._PROVIDER_DOWN); clear the names these tests
    # touch so one case's mark never leaks into another's pick.
    for name in ("sonnet", "codex"):
        clear_provider_down(name)
    yield
    for name in ("sonnet", "codex"):
        clear_provider_down(name)


def _project(result, name="default"):
    return result["projects"][name]


def test_shape_lists_every_named_path_with_coverage():
    result = routing.effective_routes({"coders": LADDER})
    proj = _project(result)
    paths = {p["path"]: p for p in proj["paths"]}
    assert set(paths) == {"implement", "fix (CI/review)", "escalate", "review", "helpers"}
    assert paths["implement"]["covered"] is True
    assert paths["fix (CI/review)"]["covered"] is True
    assert paths["escalate"]["covered"] is True
    # review + helpers are the paths routing.exclusive does NOT govern (B2).
    assert paths["review"]["covered"] is False
    assert paths["helpers"]["covered"] is False
    assert "not covered by routing.exclusive" in paths["helpers"]["detail"]
    for helper in routing.UNCOVERED_HELPER_PATHS:
        assert helper in paths["helpers"]["detail"]
    # The ladder names every tier, in ladder order, with the full (unfiltered) rung.
    assert [rung["tier"] for rung in proj["ladder"]] == ["standard", "reasoning", "advanced"]
    assert proj["ladder"][0]["delegates"] == ["sonnet", "codex"]
    assert proj["exclusive"] == []
    assert proj["exclusive_source"] == "none"


def test_exclusive_filter_applied_and_source_reported():
    result = routing.effective_routes({"coders": LADDER, "routing": {"exclusive": ["codex"]}})
    proj = _project(result)
    assert proj["exclusive"] == ["codex"]
    assert proj["exclusive_source"] == "board"
    assert result["board_exclusive"] == ["codex"]
    # Every rung is filtered to the exclusive set; sonnet-only `reasoning` empties out.
    rungs = {rung["tier"]: rung for rung in proj["ladder"]}
    assert rungs["standard"]["delegates"] == ["codex"]
    assert rungs["reasoning"]["delegates"] == []
    assert rungs["advanced"]["delegates"] == ["codex"]
    # implement opens on the first rung that still has an eligible delegate.
    assert "codex at the standard rung" in next(p for p in proj["paths"] if p["path"] == "implement")["detail"]
    # A single forced delegate reads "exclusive".
    assert rungs["standard"]["reason"] == "exclusive"
    assert rungs["standard"]["starts_on"] == "codex"


def test_project_override_exclusive_source():
    cfg = {
        "coders": LADDER,
        "routing": {"exclusive": ["codex"]},
        "projects": {
            "app": {"repo": "/app", "routing": {"exclusive": ["sonnet"]}},
            "inherit": {"repo": "/inherit"},
        },
    }
    result = routing.effective_routes(cfg)
    assert result["projects"]["app"]["exclusive"] == ["sonnet"]
    assert result["projects"]["app"]["exclusive_source"] == "project"
    assert result["projects"]["inherit"]["exclusive"] == ["codex"]
    assert result["projects"]["inherit"]["exclusive_source"] == "board"
    assert set(result["delegates"]) == {"codex", "sonnet"}


def test_headroom_unknown_is_never_an_error():
    # No usage reader and no host SDK → every delegate's headroom reads "unknown".
    result = routing.effective_routes({"coders": {"standard": ["codex", "sonnet"]}})
    delegates = {d["name"]: d for d in _project(result)["delegates"]}
    assert set(delegates) == {"codex", "sonnet"}
    for row in delegates.values():
        assert row["headroom"] == "unknown"
        assert row["provider_is_down"] is False


def test_down_mark_shown_and_reason_matches_prefer_live_sibling():
    mark_provider_down("sonnet")
    cfg = {"coders": {"standard": ["sonnet", "codex"]}, "routing": {"headroom": False}}
    proj = _project(routing.effective_routes(cfg))
    delegates = {d["name"]: d for d in proj["delegates"]}
    assert delegates["sonnet"]["provider_is_down"] is True
    assert delegates["codex"]["provider_is_down"] is False
    rung = proj["ladder"][0]
    siblings = ["sonnet", "codex"]
    expected = siblings[prefer_live_sibling(siblings, 0, set())]
    assert rung["starts_on"] == expected == "codex"
    assert rung["reason"] == "down-marked"


def test_round_robin_reason_when_nothing_steers():
    cfg = {"coders": {"standard": ["sonnet", "codex"]}, "routing": {"headroom": False}}
    rung = _project(routing.effective_routes(cfg))["ladder"][0]
    siblings = ["sonnet", "codex"]
    assert rung["starts_on"] == siblings[prefer_live_sibling(siblings, 0, set())] == "sonnet"
    assert rung["reason"] == "round-robin"


def test_headroom_steer_reason_matches_prefer_headroom_sibling():
    snaps = {
        "sonnet": {"usable": True, "headroom_pct": 4},  # 96% used ≥ prefer_at 90
        "codex": {"usable": True, "headroom_pct": 70},
    }
    cfg = {"coders": {"standard": ["sonnet", "codex"]}}  # headroom defaults on
    proj = _project(routing.effective_routes(cfg, usage=lambda name: snaps.get(name)))
    rung = proj["ladder"][0]
    siblings = ["sonnet", "codex"]
    expected = siblings[prefer_headroom_sibling(siblings, 0, set(), snaps, prefer_at=90)]
    assert rung["starts_on"] == expected == "codex"
    assert rung["reason"] == "headroom ≥90%"
    # The headroom percent is surfaced per delegate, not reduced to a boolean.
    delegates = {d["name"]: d for d in proj["delegates"]}
    assert delegates["sonnet"]["headroom"] == 4
    assert delegates["codex"]["headroom"] == 70


def test_prefer_sibling_at_threshold_in_reason():
    # sonnet is 85% used — over the lowered 80% threshold — so dispatch steers off it;
    # at the default 90% threshold it would have stayed. The knob moves the line.
    snaps = {"sonnet": {"usable": True, "headroom_pct": 15}, "codex": {"usable": True, "headroom_pct": 90}}
    cfg = {"coders": {"standard": ["sonnet", "codex"]}, "routing": {"prefer_sibling_at": 80}}
    proj = _project(routing.effective_routes(cfg, usage=lambda name: snaps.get(name)))
    rung = proj["ladder"][0]
    siblings = ["sonnet", "codex"]
    expected = siblings[prefer_headroom_sibling(siblings, 0, set(), snaps, prefer_at=80)]
    assert rung["starts_on"] == expected == "codex"
    assert rung["reason"] == "headroom ≥80%"
    assert proj["prefer_sibling_at"] == 80


# ── endpoint: same auth + reads as the other board routes, host-free ───────────────────


def _app(section, *, provider_usage=None, monkeypatch):
    from project_board import project_registry

    monkeypatch.setattr(project_registry, "_live_section", lambda **kw: copy.deepcopy(section))
    if provider_usage is not None:
        monkeypatch.setattr("graph.sdk.provider_usage", provider_usage, raising=False)
    app = FastAPI()

    @app.middleware("http")
    async def host_operator_gate(request: Request, call_next):
        if request.headers.get("authorization") != "Bearer operator":
            return JSONResponse({}, status_code=403)
        request.state.subject = "http-operator"
        return await call_next(request)

    app.include_router(api.build_data_router({}), prefix="/api/plugins/project_board")
    return app


async def test_endpoint_returns_effective_routes_and_is_gated(monkeypatch):
    section = {"coders": LADDER, "routing": {"exclusive": ["codex"]}}
    app = _app(section, monkeypatch=monkeypatch)
    path = "/api/plugins/project_board/routing/effective"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Same operator gate as the other board reads.
        assert (await client.get(path)).status_code == 403
        client.headers["authorization"] = "Bearer operator"
        response = await client.get(path)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["board_exclusive"] == ["codex"]
        proj = body["projects"]["default"]
        assert proj["exclusive"] == ["codex"]
        # Host without provider_usage → headroom unknown, never an error.
        assert all(row["headroom"] == "unknown" for row in proj["delegates"])
        assert response.headers["cache-control"] == "no-store"


async def test_endpoint_surfaces_live_headroom_when_host_exposes_it(monkeypatch):
    snaps = {"sonnet": {"usable": True, "headroom_pct": 12}, "codex": {"usable": True, "headroom_pct": 88}}
    app = _app(
        {"coders": {"standard": ["sonnet", "codex"]}},
        provider_usage=lambda delegate: snaps.get(delegate),
        monkeypatch=monkeypatch,
    )
    path = "/api/plugins/project_board/routing/effective"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.headers["authorization"] = "Bearer operator"
        proj = (await client.get(path)).json()["projects"]["default"]
        delegates = {d["name"]: d for d in proj["delegates"]}
        assert delegates["sonnet"]["headroom"] == 12
        assert delegates["codex"]["headroom"] == 88
