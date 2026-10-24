"""Host-free usage_policy contracts for the effective-routes view (#581 B7b).

Covers GET /routing/effective once a ``routing.usage_policy`` rule is configured: each project's
active rules are listed with their account's live used-%, and every ladder rung reports which rule
(if any) steers the starting sibling. The per-rung decision is computed with B7a's own
``policy_decision`` for the SAME inputs, so the view can never disagree with dispatch. An unknown
account snapshot reads the rule as inactive (never an error, ADR 0122 D4)."""

import copy

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from project_board import api, routing
from project_board.loop import clear_provider_down, policy_decision


LADDER = {"standard": ["sonnet", "codex"], "reasoning": ["sonnet"], "advanced": ["codex", "sonnet"]}
# sonnet is 95% used (≥ most targets); codex sits at 50%.
SNAPS = {"sonnet": {"usable": True, "headroom_pct": 5}, "codex": {"usable": True, "headroom_pct": 50}}


@pytest.fixture(autouse=True)
def _no_down_marks(monkeypatch):
    from project_board.loop import _common

    # Preview off a known cursor with no leaked down marks so prefer_live_sibling is neutral and
    # the headroom pick lands on `current`, leaving usage_policy as the only thing that can steer.
    monkeypatch.setattr(_common, "_RUNG_CURSOR", 0)
    for name in ("sonnet", "codex"):
        clear_provider_down(name)
    yield
    for name in ("sonnet", "codex"):
        clear_provider_down(name)


def _standard(proj):
    return next(r for r in proj["ladder"] if r["tier"] == "standard")


def test_rule_decides_start_and_matches_policy_decision():
    policy = [{"account": "sonnet", "keep_under": 80, "prefer": "codex", "tiers": ["standard"]}]
    cfg = {"coders": LADDER, "routing": {"headroom": False, "usage_policy": policy}}
    proj = routing.effective_routes(cfg, usage=SNAPS.get, current=0)["projects"]["default"]

    # The rule is listed with its account's live used-% (95% of sonnet's window).
    assert proj["usage_policy_source"] == "board"
    rule = proj["usage_policy"][0]
    assert (rule["account"], rule["keep_under"], rule["prefer"]) == ("sonnet", 80, "codex")
    assert rule["tiers"] == ["standard"]
    assert rule["account_used_pct"] == 95

    # Headroom is off and the cursor is 0, so the pick opens on sonnet (index 0) until the rule
    # steers it. The reported decision must equal policy_decision for the very same inputs.
    siblings = ["sonnet", "codex"]
    decision = policy_decision(siblings, 0, "standard", SNAPS, policy, spent=set(), prefer_at=None)
    rung = _standard(proj)
    assert rung["starts_on"] == siblings[decision["index"]] == "codex"
    assert rung["reason"] == decision["reason"]
    assert decision["reason"] is not None and "usage_policy" in rung["reason"]
    assert rung["policy"]["rule_index"] == 0
    assert rung["policy"]["account"] == decision["account"] == "sonnet"
    assert rung["policy"]["prefer"] == decision["prefer"] == "codex"
    assert rung["policy"]["used_pct"] == decision["used_pct"] == 95


def test_unknown_account_usage_leaves_rule_inactive_never_errors():
    policy = [{"account": "sonnet", "keep_under": 80, "prefer": "codex", "tiers": ["standard"]}]
    cfg = {"coders": LADDER, "routing": {"headroom": False, "usage_policy": policy}}
    # No usage reader at all → every account reads unknown (D4): the rule is listed but inactive,
    # and the pick is left byte-for-byte as the headroom step made it. Never an error.
    proj = routing.effective_routes(cfg, usage=lambda name: None, current=0)["projects"]["default"]
    assert proj["usage_policy"][0]["account_used_pct"] == "unknown"
    rung = _standard(proj)
    assert rung["policy"]["rule_index"] is None
    assert rung["policy"]["reason"] is None
    assert rung["starts_on"] == "sonnet"
    assert rung["reason"] == "round-robin"


def test_rule_under_its_target_is_inactive():
    # sonnet at 50% used is under keep_under 80 → the rule never fires, but it is still listed.
    policy = [{"account": "sonnet", "keep_under": 80, "prefer": "codex", "tiers": ["standard"]}]
    cfg = {"coders": LADDER, "routing": {"headroom": False, "usage_policy": policy}}
    snaps = {"sonnet": {"usable": True, "headroom_pct": 50}, "codex": {"usable": True, "headroom_pct": 50}}
    proj = routing.effective_routes(cfg, usage=snaps.get, current=0)["projects"]["default"]
    assert proj["usage_policy"][0]["account_used_pct"] == 50
    rung = _standard(proj)
    assert rung["policy"]["rule_index"] is None
    assert rung["starts_on"] == "sonnet"


def test_rule_tier_gate_limits_which_rung_steers():
    # tiers=[advanced] → the rule may reorder the advanced rung only; standard is untouched even
    # though sonnet is over target, matching policy_decision's tier gate.
    policy = [{"account": "sonnet", "keep_under": 80, "prefer": "codex", "tiers": ["advanced"]}]
    cfg = {"coders": LADDER, "routing": {"headroom": False, "usage_policy": policy}}
    proj = routing.effective_routes(cfg, usage=SNAPS.get, current=0)["projects"]["default"]
    assert _standard(proj)["policy"]["rule_index"] is None
    assert _standard(proj)["starts_on"] == "sonnet"
    advanced = next(r for r in proj["ladder"] if r["tier"] == "advanced")  # ["codex", "sonnet"]
    siblings = ["codex", "sonnet"]
    decision = policy_decision(siblings, 0, "advanced", SNAPS, policy, spent=set(), prefer_at=None)
    assert advanced["starts_on"] == siblings[decision["index"]] == "codex"
    assert advanced["policy"]["rule_index"] == 0


def test_policy_honours_live_headroom_threshold_when_steering_on():
    # Headroom steering ON with the default 90% threshold: the rule cannot steer onto a prefer
    # delegate that is itself at/above the threshold (codex at 95%), exactly as dispatch is barred.
    policy = [{"account": "sonnet", "keep_under": 80, "prefer": "codex", "tiers": ["standard"]}]
    cfg = {"coders": LADDER, "routing": {"usage_policy": policy}}  # headroom defaults on
    snaps = {"sonnet": {"usable": True, "headroom_pct": 10}, "codex": {"usable": True, "headroom_pct": 5}}
    proj = routing.effective_routes(cfg, usage=snaps.get, current=0)["projects"]["default"]
    siblings = ["sonnet", "codex"]
    # Mirror the view's own layering: the headroom step first, then policy on top of that index.
    from project_board.loop import prefer_headroom_sibling

    headroom_idx = prefer_headroom_sibling(siblings, 0, set(), snaps, prefer_at=90) % 2
    decision = policy_decision(siblings, headroom_idx, "standard", snaps, policy, spent=set(), prefer_at=90)
    assert decision["reason"] is None  # codex at 95% is over the threshold → policy may not pick it
    rung = _standard(proj)
    assert rung["policy"]["rule_index"] is None
    assert rung["starts_on"] == siblings[headroom_idx]


def test_usage_policy_source_project_overrides_board():
    board_rule = {"account": "sonnet", "keep_under": 80, "prefer": "codex"}
    app_rule = {"account": "codex", "keep_under": 70, "prefer": "sonnet"}
    cfg = {
        "coders": LADDER,
        "routing": {"headroom": False, "usage_policy": [board_rule]},
        "projects": {
            "app": {"repo": "/app", "coders": LADDER, "routing": {"usage_policy": [app_rule]}},
            "inherit": {"repo": "/inherit", "coders": LADDER},
        },
    }
    result = routing.effective_routes(cfg, usage=SNAPS.get, current=0)
    app = result["projects"]["app"]
    inherit = result["projects"]["inherit"]
    assert app["usage_policy_source"] == "project"
    assert app["usage_policy"][0]["account"] == "codex"
    assert inherit["usage_policy_source"] == "board"
    assert inherit["usage_policy"][0]["account"] == "sonnet"


def test_no_usage_policy_configured_reports_none():
    proj = routing.effective_routes({"coders": LADDER}, current=0)["projects"]["default"]
    assert proj["usage_policy"] == []
    assert proj["usage_policy_source"] == "none"
    assert all(r["policy"]["rule_index"] is None for r in proj["ladder"])


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


PATH = "/api/plugins/project_board/routing/effective"


async def test_endpoint_surfaces_rules_and_deciding_rule(monkeypatch):
    section = {
        "coders": {"standard": ["sonnet", "codex"], "advanced": ["codex"]},
        "routing": {
            "headroom": False,
            "usage_policy": [{"account": "sonnet", "keep_under": 80, "prefer": "codex", "tiers": ["standard"]}],
        },
    }
    app = _app(section, provider_usage=lambda delegate: SNAPS.get(delegate), monkeypatch=monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.headers["authorization"] = "Bearer operator"
        response = await client.get(PATH)
        assert response.status_code == 200, response.text
        proj = response.json()["projects"]["default"]
        assert proj["usage_policy_source"] == "board"
        rule = proj["usage_policy"][0]
        assert (rule["account"], rule["prefer"], rule["keep_under"]) == ("sonnet", "codex", 80)
        assert rule["account_used_pct"] == 95
        rung = _standard(proj)
        assert rung["starts_on"] == "codex"
        assert rung["policy"]["rule_index"] == 0
        assert rung["policy"]["account"] == "sonnet"
        assert "usage_policy" in rung["reason"]


async def test_endpoint_unknown_account_usage_marks_rule_inactive(monkeypatch):
    section = {
        "coders": {"standard": ["sonnet", "codex"], "advanced": ["codex"]},
        "routing": {
            "headroom": False,
            "usage_policy": [{"account": "sonnet", "keep_under": 80, "prefer": "codex", "tiers": ["standard"]}],
        },
    }
    # No provider_usage on the host → the account reads unknown; the rule is listed but inactive.
    app = _app(section, monkeypatch=monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.headers["authorization"] = "Bearer operator"
        response = await client.get(PATH)
        assert response.status_code == 200, response.text
        proj = response.json()["projects"]["default"]
        assert proj["usage_policy"][0]["account_used_pct"] == "unknown"
        rung = _standard(proj)
        assert rung["policy"]["rule_index"] is None
        assert rung["starts_on"] == "sonnet"
