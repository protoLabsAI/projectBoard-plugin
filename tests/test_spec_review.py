"""Fake SDK/store orchestration coverage; real temporary filesystem boundary coverage."""

import asyncio
import copy
import json

import pytest

from project_board import spec_review as sr

REAL_RUNTIME = sr.runtime


class Store:
    def __init__(self):
        self.card = dict(issue_type="feature", difficulty="medium", title="Proposal", project="demo")
        self.comments = []
        self.drop = False

    def get_feature(self, fid):
        return copy.deepcopy(self.card)

    def comment(self, fid, text):
        if not self.drop:
            self.comments.append(text)

    def feature_comments(self, fid, **kwargs):
        return self.comments


@pytest.fixture
def setup(monkeypatch, tmp_path):
    store = Store()
    calls = []

    # Match the host's required keyword-only description; a permissive fake hid
    # argument-binding failures before either worker could be dispatched.
    async def run(name, prompt, *, description, extra_tools, check_prompt_tools=True):
        assert check_prompt_tools is False
        assert description and name in description
        calls.append((name, json.loads(prompt), extra_tools))
        if name == sr.NAMES[0]:
            return json.dumps(dict(proposal=[], current_behavior=[], invariants=[], missing_decisions=[]))
        return json.dumps(
            dict(
                sources=[{"path": "x.py", "line": 1, "observation": "example"}],
                ambiguities=[],
                simpler_alternatives=[],
                uncertainties=[],
                dispositions=[],
            )
        )

    monkeypatch.setattr(sr, "runtime", lambda: run)
    from project_board.projects import default_project, resolve_projects

    cfg = {"projects": {"demo": {"repo": str(tmp_path)}}}
    store.projects = resolve_projects(cfg)
    store.default_project = default_project(cfg)
    return store, calls


@pytest.mark.parametrize(
    "difficulty,kind", [("small", "feature"), ("unknown", "feature"), ("large", "task"), ("medium", "epic")]
)
def test_refusal(setup, difficulty, kind):
    store, calls = setup
    store.card.update(difficulty=difficulty, issue_type=kind)
    assert asyncio.run(sr.review(store, "bd-1"))["status"] == "refused"
    assert not calls and not store.comments
    store.card = None
    assert asyncio.run(sr.review(store, "bd-1"))["status"] == "refused"


@pytest.mark.parametrize("difficulty", ["medium", "large", "hard"])
def test_sequence_hash_ledger_policy(setup, difficulty):
    store, calls = setup
    store.card["difficulty"] = difficulty
    before = copy.deepcopy(store.card)
    report = asyncio.run(sr.review(store, "bd-1"))
    assert [c[0] for c in calls] == list(sr.NAMES)
    assert calls[0][2] == [] and set(calls[0][1]) == {"snapshot"}
    assert len(calls[1][2]) == 2 and "comprehension" in calls[1][1]
    assert report["persistence"] == "verified" and report["freshness"] == "current"
    assert report["input_hash"] == sr.input_hash(sr.snapshot(before))
    assert sr.input_hash({"a": 1, "b": 2}) == sr.input_hash({"b": 2, "a": 1})
    assert report["isolation"]["verified"] is False
    assert report["metrics"] == {"cost": None, "tokens": None, "coverage": None}
    assert json.loads(store.comments[0])["run_id"] == report["run_id"]
    assert store.card == before


def test_swallowed_write_and_read_failures(setup):
    store, _ = setup
    store.drop = True
    assert asyncio.run(sr.review(store, "bd-1"))["persistence"].startswith("failed")

    def broken(*args, **kwargs):
        raise RuntimeError("read failed")

    store.feature_comments = broken
    assert "read failed" in asyncio.run(sr.review(store, "bd-1"))["persistence"]


@pytest.mark.parametrize("mutation", ["edit", "delete", "read_error"])
def test_final_freshness(setup, mutation):
    store, _ = setup

    def comment(fid, text):
        store.comments.append(text)
        if mutation == "edit":
            store.card["design"] = "changed"
        elif mutation == "delete":
            store.card = None
        else:

            def fail(fid):
                raise RuntimeError("offline")

            store.get_feature = fail

    store.comment = comment
    report = asyncio.run(sr.review(store, "bd-1"))
    assert report["freshness"] == ("unknown" if mutation == "read_error" else "stale")


@pytest.mark.parametrize("failure", ["malformed", "timeout", "failure"])
def test_stage_gaps(setup, monkeypatch, failure):
    store, calls = setup

    async def run(*args, **kwargs):
        calls.append(args)
        if failure == "timeout":
            raise TimeoutError()
        if failure == "failure":
            raise RuntimeError("provider down")
        return "{}"

    monkeypatch.setattr(sr, "runtime", lambda: run)
    report = asyncio.run(sr.review(store, "bd-1"))
    assert report["stages"]["comprehension"]["status"] == "gap"
    assert report["stages"]["readiness"]["status"] == "not_run"
    assert len(calls) == 1 and report["persistence"] == "verified"


def test_missing_repo(setup):
    store, calls = setup
    store.projects = {}
    report = asyncio.run(sr.review(store, "bd-1"))
    assert len(calls) == 1 and "repository" in report["gap"]


@pytest.mark.parametrize(
    "layout,project",
    [
        ("flat", ""),
        ("flat", "default"),
        ("named_flat", ""),
        ("named_flat", "demo"),
        ("explicit", ""),
        ("explicit", "demo"),
        ("multiple", "other"),
        ("flat", "unknown"),
        ("missing_path", ""),
    ],
)
def test_tool_uses_effective_project_binding(setup, monkeypatch, tmp_path, layout, project):
    """Real tool/config resolution and filesystem; fake SDK and comment/card store."""
    import project_board

    store, calls = setup
    store.card["project"] = project
    other = tmp_path / "other"
    other.mkdir()
    cfg = {"repo": str(tmp_path)}
    expected = tmp_path
    if layout == "named_flat":
        cfg["default_project"] = "demo"
    elif layout in {"explicit", "multiple"}:
        cfg["projects"] = {"demo": {"repo": str(tmp_path)}}
        if layout == "multiple":
            cfg["projects"]["other"] = {"repo": str(other)}
            cfg["default_project"] = "demo"
            expected = other
    elif layout == "missing_path":
        cfg["repo"] = str(tmp_path / "absent")

    def get_store(**kwargs):
        store.projects = kwargs["projects"]
        store.default_project = kwargs["default_project"]
        return store

    monkeypatch.setattr("project_board.store.get_store", get_store)
    review_tool = next(t for t in project_board._board_tools(cfg) if t.name == "board_review_spec")
    report = json.loads(asyncio.run(review_tool.ainvoke({"feature_id": "bd-1"})))
    if project == "unknown" or layout == "missing_path":
        assert len(calls) == 1
        assert "repository" in report["gap"]
        assert report["stages"]["readiness"]["status"] == "not_run"
    else:
        assert [c[0] for c in calls] == list(sr.NAMES)
        assert report["repo"] == str(expected.resolve())
        assert report["stages"]["readiness"]["status"] == "reported"
        (expected / "bound.txt").write_text("selected repository")
        assert calls[1][2][0].invoke({"path": "bound.txt"}) == "selected repository"
    assert report["persistence"] == "verified"


def test_read_search_boundaries(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "x.py").write_text("hello\nworld")
    outside = tmp_path / "secret"
    outside.write_text("secret")
    (root / "escape").symlink_to(outside)
    read, search = sr.repo_tools(root)
    assert read.invoke({"path": "x.py"}) == "hello\nworld"
    assert "x.py:2" in search.invoke({"query": "world"})
    assert "secret" not in search.invoke({"query": "secret"})
    for path in ["../secret", "escape"]:
        with pytest.raises(ValueError):
            read.invoke({"path": path})


def test_registered_workers_and_runtime_capability(monkeypatch):
    import project_board
    from _plugin_testkit import FakeRegistry, install_host_stubs

    install_host_stubs()
    from graph import sdk

    registry = FakeRegistry({"br_autofetch": False}, plugin_id="project_board")
    project_board.register(registry)
    configs = [c for c in registry.subagents if c.name in sr.NAMES]
    assert len(configs) == 2
    assert configs[0].tools == []
    assert configs[1].tools == ["spec_review_read", "spec_review_search"]
    assert all(c.inherit_read_tools is False for c in configs)

    async def unsupported(name, prompt):
        return ""

    monkeypatch.setattr(sdk, "run_subagent", unsupported, raising=False)
    with pytest.raises(ValueError, match="extra_tools"):
        sr.runtime()

    async def supported(name, prompt, *, description, extra_tools, check_prompt_tools=True):
        return ""

    monkeypatch.setattr(sdk, "run_subagent", supported)
    assert sr.runtime() is supported


def test_host_tool_filter_and_prompt_preflight(setup, monkeypatch, tmp_path):
    """Fake host dispatch contract; real bound filesystem tools, no live host/model."""
    from _plugin_testkit import install_host_stubs

    install_host_stubs()
    from graph import sdk
    from project_board.subagents import SPEC_COMPREHENSION_CONFIG, SPEC_READINESS_CONFIG

    store, _ = setup
    store.card["spec"] = "Current behavior uses read_file and search_files."
    (tmp_path / "x.py").write_text("grounded evidence\n")
    configs = {c.name: c for c in (SPEC_COMPREHENSION_CONFIG, SPEC_READINESS_CONFIG)}
    dispatched = []

    async def host_run(name, prompt, *, description, extra_tools, check_prompt_tools=True):
        config = configs[name]
        # Extras enter the candidate map but still pass through the allowlist.
        candidates = {"read_file": object(), "search_files": object(), "write_file": object()}
        candidates.update({t.name: t for t in extra_tools})
        bound = {key: value for key, value in candidates.items() if key in config.tools}
        if check_prompt_tools and any(key in prompt and key not in bound for key in candidates):
            return "Not dispatched"
        assert config.inherit_read_tools is False
        dispatched.append(name)
        if name == sr.NAMES[0]:
            assert not bound and extra_tools == []
            return json.dumps(dict(proposal=["read_file"], current_behavior=[], invariants=[], missing_decisions=[]))
        assert set(bound) == {"spec_review_read", "spec_review_search"}
        assert bound["spec_review_read"].invoke({"path": "x.py"}) == "grounded evidence\n"
        assert "x.py:1" in bound["spec_review_search"].invoke({"query": "grounded"})
        return json.dumps(
            dict(
                sources=[dict(path="x.py", line=1, observation="grounded evidence")],
                ambiguities=[],
                simpler_alternatives=[],
                uncertainties=[],
                dispositions=[],
            )
        )

    # Exercise the real capability checker as well as stage dispatch.
    monkeypatch.setattr(sr, "runtime", REAL_RUNTIME)
    monkeypatch.setattr(sdk, "run_subagent", host_run, raising=False)
    report = asyncio.run(sr.review(store, "bd-1"))
    assert dispatched == list(sr.NAMES)
    for key, config in zip(("comprehension", "readiness"), configs.values()):
        result = report["stages"][key]
        assert result["status"] == "reported"
        assert result["tool_policy"]["tools"] == config.tools
        assert result["tool_policy"]["extra_tools"] == config.tools
    assert report["persistence"] == "verified"


def test_sdk_without_prompt_check_control_is_gap(setup, monkeypatch):
    from graph import sdk

    store, _ = setup

    async def unsupported(name, prompt, *, description, extra_tools):
        pytest.fail("unsupported SDK must not dispatch")

    monkeypatch.setattr(sr, "runtime", REAL_RUNTIME)
    monkeypatch.setattr(sdk, "run_subagent", unsupported, raising=False)
    report = asyncio.run(sr.review(store, "bd-1"))
    assert "check_prompt_tools" in report["gap"]
    assert all(s["status"] == "not_run" for s in report["stages"].values())
    assert report["persistence"] == "verified"
