"""Runtime board-project registration (#167).

The gap: `project_board.projects` had no runtime write path, so an agent could clone a
repo and gain filesystem reach (protoAgent's `onboard_project`) and still not dispatch a
feature to it — every board-managed repo cost an operator a YAML edit and a restart.

These cover the halves that must not rot: the bound (a repo outside the consented space
is refused, by name), the superset invariant (a register never drops a sibling), and
idempotency (re-registering updates rather than duplicating).
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

from project_registry import _raw_projects, _resolve_under, build_register_tool


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


# ── the bound ─────────────────────────────────────────────────────────────────
def test_a_repo_under_the_root_resolves(tmp_path):
    root = tmp_path / "dev"
    repo = _git_repo(root / "thing")
    resolved, err = _resolve_under(str(root), str(repo))
    assert err is None
    assert resolved == repo.resolve()


def test_a_repo_outside_the_root_is_refused_naming_the_bound(tmp_path):
    root = tmp_path / "dev"
    root.mkdir()
    outside = _git_repo(tmp_path / "elsewhere" / "thing")
    resolved, err = _resolve_under(str(root), str(outside))
    assert resolved is None
    assert "outside the onboarding root" in err and str(root.resolve()) in err


def test_a_dotdot_escape_cannot_smuggle_a_path_out(tmp_path):
    """Resolve BOTH sides before comparing — a string-prefix check would pass this."""
    root = tmp_path / "dev"
    root.mkdir()
    _git_repo(tmp_path / "elsewhere" / "thing")
    sneaky = str(root / ".." / "elsewhere" / "thing")
    resolved, err = _resolve_under(str(root), sneaky)
    assert resolved is None and "outside the onboarding root" in err


def test_a_path_that_is_not_a_checkout_is_refused(tmp_path):
    root = tmp_path / "dev"
    plain = root / "notarepo"
    plain.mkdir(parents=True)
    resolved, err = _resolve_under(str(root), str(plain))
    assert resolved is None and "isn't a git checkout" in err


def test_a_missing_directory_is_refused_before_anything_else(tmp_path):
    root = tmp_path / "dev"
    root.mkdir()
    resolved, err = _resolve_under(str(root), str(root / "never-cloned"))
    assert resolved is None and "isn't a directory" in err


def test_no_root_configured_is_refused_not_treated_as_unbounded(tmp_path):
    """An empty root must never read as 'anywhere is fine'."""
    resolved, err = _resolve_under("", str(tmp_path))
    assert resolved is None and "no consented space" in err


# ── reading the configured map ────────────────────────────────────────────────
def test_raw_projects_is_empty_with_no_host(monkeypatch):
    """Host-free (tests, CLI): no config seam → empty, never a crash."""
    assert _raw_projects() == {}


def test_raw_projects_reads_the_configured_map(monkeypatch):
    class _Cfg:
        project_board = {"projects": {"alpha": {"repo": "/a"}, "beta": {"repo": "/b"}}}

    fake_sdk = types.ModuleType("graph.sdk")
    fake_sdk.config = lambda: _Cfg()
    monkeypatch.setitem(sys.modules, "graph.sdk", fake_sdk)
    assert set(_raw_projects()) == {"alpha", "beta"}


def test_raw_projects_reads_production_plugin_config_shape(monkeypatch):
    """LangGraphConfig exposes plugin sections inside ``plugin_config`` — not as
    direct attributes. Registration must read the same live shape reload receives."""

    class _Cfg:
        plugin_config = {"project_board": {"projects": {"alpha": {"repo": "/a"}}}}

    fake_sdk = types.ModuleType("graph.sdk")
    fake_sdk.config = lambda: _Cfg()
    monkeypatch.setitem(sys.modules, "graph.sdk", fake_sdk)
    assert _raw_projects() == {"alpha": {"repo": "/a"}}


def test_raw_projects_does_not_synthesize_an_implicit_project(monkeypatch):
    """Reads the CONFIGURED map, never `resolve_projects`' synthesized fallback —
    writing a synthesized entry back would persist a default the operator never wrote,
    turning an additive register into a silent config rewrite."""
    import sys
    import types

    class _Cfg:
        project_board = {"repo": "/flat/repo", "base_branch": "main"}  # flat keys, no map

    fake_sdk = types.ModuleType("graph.sdk")
    fake_sdk.config = lambda: _Cfg()
    monkeypatch.setitem(sys.modules, "graph.sdk", fake_sdk)
    assert _raw_projects() == {}


def test_raw_projects_ignores_a_non_dict_map(monkeypatch):
    """A malformed `projects:` must not crash registration — treat as empty and let the
    merge add the first real entry."""
    import sys
    import types

    class _Cfg:
        project_board = {"projects": ["not", "a", "map"]}

    fake_sdk = types.ModuleType("graph.sdk")
    fake_sdk.config = lambda: _Cfg()
    monkeypatch.setitem(sys.modules, "graph.sdk", fake_sdk)
    assert _raw_projects() == {}


# ── the merge ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "existing,adding,expected",
    [
        ({}, "solo", {"solo"}),
        ({"alpha": {"repo": "/a"}}, "beta", {"alpha", "beta"}),
        ({"alpha": {"repo": "/a"}}, "alpha", {"alpha"}),  # idempotent by name
    ],
)
def test_the_merge_is_a_superset_and_idempotent(existing, adding, expected):
    """The #2556 shape: a replace-all write that silently dropped roots and still
    answered ok. A register adds or updates — it never removes."""
    merged = dict(existing)
    merged[adding] = {"repo": "/new"}
    assert set(existing) <= set(merged)
    assert set(merged) == expected


def _wire_host(monkeypatch, cfg, apply_settings):
    fake_sdk = types.ModuleType("graph.sdk")
    fake_sdk.config = lambda: cfg
    fake_plugins = types.ModuleType("graph.plugins")
    fake_plugins.__path__ = []
    fake_host = types.ModuleType("graph.plugins.host")
    fake_host.HOST = types.SimpleNamespace(apply_settings=apply_settings)
    monkeypatch.setitem(sys.modules, "graph.sdk", fake_sdk)
    monkeypatch.setitem(sys.modules, "graph.plugins", fake_plugins)
    monkeypatch.setitem(sys.modules, "graph.plugins.host", fake_host)


async def test_register_persists_superset_and_reports_live_without_restart(monkeypatch, tmp_path):
    root = tmp_path / "dev"
    repo = _git_repo(root / "beta")
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root=str(root),
        plugin_config={"project_board": {"projects": {"alpha": {"repo": "/a", "base_branch": "main"}}}},
    )
    applied = []

    def apply_settings(patch):
        applied.append(patch)
        cfg.plugin_config["project_board"]["projects"] = patch["project_board"]["projects"]
        return True, []

    _wire_host(monkeypatch, cfg, apply_settings)
    tool = build_register_tool({})
    assert tool is not None
    result = await tool.ainvoke({"name": "beta", "repo": str(repo), "repo_conventions": "Run tests"})

    assert set(applied[0]["project_board"]["projects"]) == {"alpha", "beta"}
    assert cfg.plugin_config["project_board"]["projects"]["beta"]["repo"] == str(repo.resolve())
    assert "applied it live; no restart is required" in result


async def test_register_does_not_claim_success_when_host_ok_did_not_persist(monkeypatch, tmp_path):
    root = tmp_path / "dev"
    repo = _git_repo(root / "beta")
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root=str(root),
        plugin_config={"project_board": {"projects": {"alpha": {"repo": "/a"}}}},
    )
    _wire_host(monkeypatch, cfg, lambda _patch: (True, []))

    tool = build_register_tool({})
    result = await tool.ainvoke({"name": "beta", "repo": str(repo)})

    assert result.startswith("Error:")
    assert "live config readback failed" in result and "no success was assumed" in result
