"""Host-free live routing and operator mutation contracts (#577)."""

import copy
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from _host_apply import apply_updates

from project_board import _board_tools, api, routing, setup_check, worktree
from project_board.loop import BoardLoop
from tests.test_exclusive_routing import LADDER, LadderStore
from tests.test_loop import _drive_with


def test_reload_aliases_and_snapshot():
    loop = BoardLoop({"coders": LADDER})
    loop._resolve_delegate = lambda name, _: name
    captured = loop._coders_for({})
    loop.reload({"coders": {"smart": ["sonnet"], "opus": ["codex"]}, "routing": {"exclusive": ["codex"]}})
    assert captured == LADDER
    assert loop._coders_for({}) == {"standard": [], "advanced": ["codex"]}
    assert loop._exclusive_for({}) == ["codex"]
    assert not setup_check.stale_loop_keys(loop.cfg, setup_check.snapshot_of({}))
    assert not {"coders", "routing"} & set(setup_check.LOOP_RESTART_KEYS)


def _host(section):
    # What the host hands reload(): the whole saved section under `plugin_config`.
    return SimpleNamespace(plugin_config={"project_board": section})


@pytest.mark.parametrize("key", ["routing", "coders"])
def test_host_reload_without_a_policy_key_clears_it(key):
    # Deleting `routing:` (or `coders:`) from the YAML must reach the running loop:
    # neither key has a manifest default, so its absence from the host section is a delete.
    loop = BoardLoop({"coders": copy.deepcopy(LADDER), "routing": {"exclusive": ["codex"]}})
    loop._resolve_delegate = lambda name, _: name
    kept = {k: v for k, v in copy.deepcopy(loop.cfg).items() if k in ("coders", "routing") and k != key}
    result = loop.reload(_host(kept))
    assert key not in loop.cfg
    assert key in result
    if key == "routing":
        assert loop._exclusive_for({}) == []
        assert loop._coders_for({}) == LADDER
    else:
        assert loop.coders == {}
        assert loop.escalation_on is False


@pytest.mark.parametrize("plugin_config", [{}, {"other": {}}, {"project_board": None}, {"project_board": []}])
def test_host_without_board_mapping_is_noop(plugin_config):
    loop = BoardLoop({"coders": copy.deepcopy(LADDER), "routing": {"exclusive": ["codex"]}})
    loop._resolve_delegate = lambda name, _: name
    before = copy.deepcopy(loop.cfg)
    captured = loop._coders_for({})
    assert loop.reload(SimpleNamespace(plugin_config=plugin_config)) == {}
    assert loop.cfg == before
    assert loop._coders_for({}) == captured
    assert loop._exclusive_for({}) == ["codex"]
    assert loop.escalation_on is True


def test_plain_dict_reload_without_a_policy_key_keeps_it():
    # A plain dict is a partial update: a key it doesn't mention is left alone.
    loop = BoardLoop({"coders": copy.deepcopy(LADDER), "routing": {"exclusive": ["codex"]}})
    loop._resolve_delegate = lambda name, _: name
    loop.reload({"max_concurrent": 2})
    assert loop._exclusive_for({}) == ["codex"]
    assert loop.cfg["coders"] == LADDER


@pytest.mark.parametrize(
    "update",
    [
        {"coders": []},
        {"coders": {"typo": "sonnet"}},
        {"coders": {"smart": [3]}},
        {"routing": []},
        {"routing": {"exclusive": [3]}},
    ],
)
def test_bad_reload_is_atomic(update, caplog):
    # A STRUCTURALLY malformed candidate is rejected whole — the piggy-backed projects
    # edit drops with it. A merely-disabled delegate is NOT malformed here: see
    # test_projects_edit_survives_disabled_board_coder.
    loop = BoardLoop({"coders": copy.deepcopy(LADDER)})
    loop._resolve_delegate = lambda name, _: None if name == "missing" else name
    before = copy.deepcopy(loop.cfg)
    loop.reload({**update, "projects": {"new": {"repo": "/new"}}})
    assert loop.cfg == before
    assert loop._coders_for({}) == LADDER
    assert "keeping current map" in caplog.text


def test_projects_edit_survives_disabled_board_coder(caplog):
    # finding 1: the candidate always carries the board's `coders`, so a reload that only
    # touches `projects` must not be dropped because a delegate the board's ladder names
    # has since been disabled (resolves to None). The dispatcher skips it per rung.
    loop = BoardLoop({"coders": {"standard": ["sonnet", "gone"]}, "projects": {"app": {"repo": "/app"}}})
    loop._resolve_delegate = lambda name, _: None if name == "gone" else name
    loop.reload({"projects": {"app": {"repo": "/changed"}, "new": {"repo": "/new"}}})
    assert loop._repo_for({"project": "app"}) == "/changed"
    assert loop._repo_for({"project": "new"}) == "/new"
    assert loop._coders_for({}) == {"standard": ["sonnet", "gone"]}
    assert "keeping current map" not in caplog.text


@pytest.mark.parametrize(
    "start, update, on",
    [
        ({"coder": "sonnet"}, {"coders": LADDER}, True),
        ({"coders": LADDER}, {"coders": {"standard": ["sonnet"]}}, False),
    ],
)
def test_reload_retunes_escalation_gate(start, update, on):
    # finding 2: `escalation_on` is set once at construction, so a live single-coder↔ladder
    # change must recompute it or the next claim's dispatch gate stays stale.
    loop = BoardLoop(copy.deepcopy(start))
    loop._resolve_delegate = lambda name, _: name
    assert loop.escalation_on is (not on)
    loop.reload(copy.deepcopy(update))
    assert loop.escalation_on is on


async def test_inflight_escalation_keeps_captured_policy(monkeypatch):
    dispatched = []
    holder = {}

    def seed(loop):
        holder["loop"] = loop
        loop._resolve_delegate = lambda name, _: name
        loop._dispatch_reached_model = lambda fid: True
        loop.decompose_after_timeouts = 0

    async def dispatch(coder, *args, **kwargs):
        dispatched.append(coder)
        if len(dispatched) == 1:
            holder["loop"].reload({"coders": LADDER, "routing": {"exclusive": ["sonnet"]}})
            raise worktree.CoderTimeout("coder timed out after 1800s")
        return "implemented with tests"

    async def open_pr(*args, **kwargs):
        return "https://github.com/o/r/pull/1"

    await _drive_with(
        monkeypatch,
        cfg={"coder": "sonnet", "coders": LADDER, "routing": {"exclusive": ["codex"]}},
        store=LadderStore(),
        seed=seed,
        dispatch=dispatch,
        open_pr=open_pr,
    )
    assert dispatched == ["codex", "codex"]
    assert holder["loop"]._coders_for({}) == {tier: ["sonnet"] for tier in LADDER}


@pytest.fixture
def host(monkeypatch, tmp_path):
    from project_board import project_registry

    live = SimpleNamespace(
        plugin_config={
            "project_board": {
                "coders": {"smart": "sonnet", "opus": "codex"},
                "projects": {"app": {"repo": "/app"}, "own": {"repo": "/own", "routing": {"exclusive": []}}},
            }
        }
    )
    loop = BoardLoop(copy.deepcopy(live.plugin_config["project_board"]))
    loop._resolve_delegate = lambda name, _: name if name in {"sonnet", "codex"} else None
    calls = []

    def apply(compute):
        patch = compute(live)
        calls.append(patch)
        apply_updates(live.plugin_config, patch)
        loop.reload(live)
        return True, []

    module = ModuleType("graph.plugins.host")
    module.HOST = SimpleNamespace(apply_settings=apply)
    monkeypatch.setitem(sys.modules, "graph.plugins.host", module)
    monkeypatch.setattr(
        project_registry, "_live_section", lambda **kw: copy.deepcopy(live.plugin_config["project_board"])
    )
    monkeypatch.setattr(
        routing, "_default_delegates", lambda: lambda name: name if name in {"sonnet", "codex"} else None
    )
    monkeypatch.setenv("PROJECT_BOARD_DATA_DIR", str(tmp_path))
    return live, loop, calls, tmp_path / "routing-audit.jsonl"


async def test_tool_persists_applies_and_audits(host):
    live, loop, calls, audit = host
    tools = {t.name: t for t in _board_tools({})}
    setter = tools["board_routing_set"]
    assert "config" not in setter.tool_call_schema.model_fields
    result = json.loads(
        await setter.ainvoke(
            {"coders": {"smart": ["codex"]}, "exclusive": ["codex"]},
            config={"configurable": {"auth": {"tier": "operator", "subject": "alice"}}},
        )
    )
    assert result["app"]["coders"]["standard"] == ["codex"]
    assert result["app"]["exclusive"] == ["codex"]
    assert result["own"]["exclusive"] == []
    assert loop._coders_for({"project": "app"}) == {"standard": ["codex"]}
    assert live.plugin_config["project_board"]["coders"] == {"standard": ["codex"]}
    assert len(calls) == 1
    records = [json.loads(line) for line in audit.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["who"] == "alice"
    assert records[0]["when"]
    assert records[0]["old"] != records[0]["new"] == result
    assert json.loads(tools["board_routing_get"].invoke({})) == result


@pytest.mark.parametrize("auth", [{}, {"tier": "agent"}, {"tier": "viewer"}])
async def test_tool_refuses_without_operator(host, auth):
    live, loop, calls, audit = host
    before = copy.deepcopy(live.plugin_config)
    setter = next(t for t in _board_tools({}) if t.name == "board_routing_set")
    result = await setter.ainvoke({"exclusive": []}, config={"configurable": {"auth": auth}})
    assert "requires operator" in result
    assert not calls and not audit.exists()
    assert live.plugin_config == before


async def test_http_routes_and_project_override(host):
    live, loop, calls, audit = host
    live.plugin_config["project_board"]["projects"]["app"]["coders"] = {"smart": "sonnet", "opus": "codex"}
    app = FastAPI()

    @app.middleware("http")
    async def host_operator_gate(request: Request, call_next):
        if request.headers.get("authorization") != "Bearer operator":
            return JSONResponse({}, status_code=403)
        request.state.subject = "http-operator"
        return await call_next(request)

    app.include_router(api.build_data_router({}), prefix="/api/plugins/project_board")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path = "/api/plugins/project_board/routing"
        assert (await client.put(path, json={"exclusive": []})).status_code == 403
        assert not calls
        client.headers["authorization"] = "Bearer operator"
        response = await client.put(path, json={"project": "app", "coders": {"opus": "codex"}, "exclusive": ["codex"]})
        assert response.status_code == 200, response.text
        assert (await client.get(path)).json() == response.json()
        assert loop._coders_for({"project": "app"}) == {"advanced": ["codex"]}
        assert live.plugin_config["project_board"]["projects"]["app"]["coders"] == {"advanced": "codex"}
        before = copy.deepcopy(live.plugin_config)
        bad = await client.put(path, json={"coders": {"standard": "missing"}})
        assert bad.status_code == 400 and "missing" in bad.text
        assert live.plugin_config == before
        assert len(calls) == len(audit.read_text().splitlines()) == 1
        assert json.loads(audit.read_text())["who"] == "http-operator"


async def test_malformed_tool_update_has_reason_and_no_side_effects(host):
    live, loop, calls, audit = host
    before = copy.deepcopy(live.plugin_config)
    setter = next(t for t in _board_tools({}) if t.name == "board_routing_set")
    result = await setter.ainvoke(
        {"coders": {"unknown-tier": "codex"}},
        config={"configurable": {"auth": {"tier": "operator", "subject": "alice"}}},
    )
    assert "unknown coder tier" in result
    assert live.plugin_config == before
    assert not calls and not audit.exists()


def test_project_only_reload_rejects_malformed_routing(caplog):
    loop = BoardLoop({"projects": {"app": {"repo": "/app"}}})
    before = copy.deepcopy(loop.cfg)
    loop.reload({"projects": {"app": {"repo": "/changed", "routing": {"exclusive": False}}}})
    assert loop.cfg == before
    assert loop._repo_for({"project": "app"}) == "/app"
    assert "exclusive must be a list" in caplog.text


async def test_host_refusal_neither_applies_nor_audits(host, monkeypatch):
    live, loop, calls, audit = host
    before = copy.deepcopy(live.plugin_config)
    monkeypatch.setattr(
        sys.modules["graph.plugins.host"].HOST, "apply_settings", lambda compute: (False, ["save denied"])
    )
    with pytest.raises(ValueError, match="save denied"):
        await routing.routing_set(actor="alice", exclusive=[])
    assert live.plugin_config == before
    assert not calls and not audit.exists()


@pytest.mark.parametrize(
    "policy",
    [
        {"routing": {"exclusive": "sonnet"}},
        {"routing": {"exclusive": None}},
        {"routing": None},
        {"coders": {"smart": ["", "sonnet", "  "]}},
        {"coders": {"smart": ""}},
        {"coders": None},
        {"coders": []},
        {"coders": False},
        {"coders": {"custom-tier": "sonnet", "smart": "codex"}},
        {"routing": {"exclusive": ["sonnet"], "legacy-option": True}},
    ],
)
@pytest.mark.parametrize("scope", ["board", "project"])
def test_legacy_policy_survives_project_reload(policy, scope, caplog):
    cfg = {"coders": copy.deepcopy(LADDER), "projects": {"app": {"repo": "/app"}}}
    target = cfg if scope == "board" else cfg["projects"]["app"]
    target.update(copy.deepcopy(policy))
    loop = BoardLoop(cfg)
    loop._resolve_delegate = lambda name, _: name
    captured = loop._coders_for({"project": "app"})
    exclusive = loop._exclusive_for({"project": "app"})
    update = copy.deepcopy(cfg)
    update["projects"]["app"]["repo"] = "/changed"
    loop.reload(_host(update))
    assert loop._repo_for({"project": "app"}) == "/changed"
    assert loop._coders_for({"project": "app"}) == captured
    assert loop._exclusive_for({"project": "app"}) == exclusive
    assert routing.effective_routing(update)["app"]["exclusive"] == exclusive
    assert "keeping current map" not in caplog.text


async def test_repair_disabled_delegate_one_field_and_scope_at_a_time(host):
    live, loop, calls, audit = host
    section = live.plugin_config["project_board"]
    for target in (section, section["projects"]["app"], section["projects"]["own"]):
        target.update(coders={"standard": ["gone"]}, routing={"exclusive": ["gone"]})
    loop.reload(live)
    for project in (None, "app", "own"):
        await routing.routing_set(actor="alice", project=project, coders={"smart": ["sonnet"]})
        await routing.routing_set(actor="alice", project=project, exclusive=[])
    assert len(calls) == len(audit.read_text().splitlines()) == 6
    assert all(value["coders"]["standard"] == ["sonnet"] for value in routing.routing_get().values())
    assert loop._coders_for({"project": "app"}) == {"standard": ["sonnet"]}
    before = copy.deepcopy(section)
    with pytest.raises(ValueError, match="gone.*not configured"):
        await routing.routing_set(actor="alice", exclusive=["gone"])
    assert section == before
    assert len(calls) == len(audit.read_text().splitlines()) == 6


async def test_setter_can_replace_null_policy_and_audit_legacy_values(host):
    live, loop, calls, audit = host
    section = live.plugin_config["project_board"]
    section.update(coders=None, routing=None)
    section["projects"]["app"].update(coders={"smart": ["", "sonnet"]}, routing={"exclusive": "sonnet"})
    loop.reload(live)
    result = await routing.routing_set(actor="alice", coders={"smart": "codex"}, exclusive=["codex"])
    assert result["app"]["exclusive"] == ["sonnet"]
    assert result["own"]["coders"]["standard"] == ["codex"]
    assert loop.coders == {"standard": ["codex"]}
    record = json.loads(audit.read_text())
    assert record["old"]["app"]["coders"]["standard"] == ["sonnet"]
    assert record["new"] == result
    assert len(calls) == 1


@pytest.mark.parametrize(
    "policy",
    [
        {"coders": {"custom-tier": "sonnet"}},
        {"coders": []},
        {"routing": {"exclusive": ["codex"], "legacy-option": True}},
    ],
)
@pytest.mark.parametrize("projects", [{}, {"projects": {"app": {"repo": "/app"}, "other": {"repo": "/other"}}}])
def test_default_project_reload_preserves_legacy_policy(policy, projects, caplog):
    loop = BoardLoop({**policy, **projects})
    loop.reload({"default_project": "other"})
    assert loop._default_project == "other"
    assert "keeping current map" not in caplog.text


def test_changed_project_policy_still_validated_with_legacy_board(caplog):
    loop = BoardLoop({"coders": {"custom-tier": "sonnet"}, "projects": {"app": {"repo": "/app"}}})
    before = copy.deepcopy(loop.cfg)
    loop.reload({"projects": {"app": {"repo": "/changed", "coders": {"typo": "sonnet"}}}})
    assert loop.cfg == before
    assert loop._repo_for({"project": "app"}) == "/app"
    assert "unknown coder tier" in caplog.text


async def test_setter_preserves_unmodified_legacy_policy_but_validates_supplied_fields(host):
    live, loop, calls, audit = host
    section = live.plugin_config["project_board"]
    section["coders"] = {"custom-tier": "sonnet"}
    section["projects"]["app"]["routing"] = {"legacy-option": True}
    # Model a board started with legacy policy, not a newly submitted invalid policy.
    loop.__init__(copy.deepcopy(section))
    result = await routing.routing_set(actor="alice", exclusive=["codex"])
    assert loop._exclusive_for({"project": "app"}) == ["codex"]
    assert section["coders"] == {"custom-tier": "sonnet"}
    assert section["projects"]["app"]["routing"] == {"legacy-option": True}
    assert json.loads(audit.read_text())["new"] == result
    with pytest.raises(ValueError, match="unknown coder tier"):
        await routing.routing_set(actor="alice", coders={"custom-tier": "sonnet"})
    assert len(calls) == len(audit.read_text().splitlines()) == 1
