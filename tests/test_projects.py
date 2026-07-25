"""`project_board.project` resolved against the host's ADR 0095 registry.

Host-free: `graph.sdk` doesn't exist in this suite, so the default path exercises
the real "no host" degrade. Where a host IS needed, a fake `graph.sdk` goes into
`sys.modules` (the standalone-plugin pattern).

The fatal-on-unresolvable tests are the point of the module, not an edge case: the
board writes (worktrees, branches, PRs) and `repo` defaults to ".", so a silent
fallback would build in the server's cwd.
"""

from __future__ import annotations

import sys
import types

import pytest

from project_board.projects import registry_projects, resolve_project_cfg


@pytest.fixture
def fake_host(monkeypatch):
    def _install(projects, *, raises: bool = False):
        sdk = types.ModuleType("graph.sdk")

        def config():
            if raises:
                raise RuntimeError("config not loaded")
            return types.SimpleNamespace(projects=projects)

        sdk.config = config
        graph = types.ModuleType("graph")
        graph.sdk = sdk
        monkeypatch.setitem(sys.modules, "graph", graph)
        monkeypatch.setitem(sys.modules, "graph.sdk", sdk)

    return _install


_ENTRY = {"name": "pa", "path": "/work/pa", "github": "o/pa", "default_branch": "master"}


# ── no `project:` — every existing config and every older host is untouched ──


def test_no_project_key_is_a_passthrough():
    cfg = {"repo": ".", "base_branch": "main", "loop_enabled": False}
    assert resolve_project_cfg(cfg) == cfg


def test_blank_project_is_a_passthrough(fake_host):
    fake_host([_ENTRY])
    assert resolve_project_cfg({"project": "   ", "repo": "."})["repo"] == "."


def test_registry_projects_degrades_with_no_host():
    assert registry_projects() == []


# ── resolution ──


def test_project_supplies_repo_and_base_branch(fake_host):
    fake_host([_ENTRY])
    out = resolve_project_cfg({"project": "pa", "repo": ".", "base_branch": "main"})
    assert out["repo"] == "/work/pa"
    assert out["base_branch"] == "master"


def test_explicit_repo_and_branch_win(fake_host):
    """Anything other than the manifest defaults is an operator choice."""
    fake_host([_ENTRY])
    out = resolve_project_cfg({"project": "pa", "repo": "/elsewhere", "base_branch": "dev"})
    assert out["repo"] == "/elsewhere"
    assert out["base_branch"] == "dev"


def test_entry_without_default_branch_keeps_main(fake_host):
    fake_host([{"name": "pa", "path": "/work/pa"}])
    out = resolve_project_cfg({"project": "pa", "repo": ".", "base_branch": "main"})
    assert out["repo"] == "/work/pa" and out["base_branch"] == "main"


def test_resolution_does_not_mutate_the_input(fake_host):
    fake_host([_ENTRY])
    cfg = {"project": "pa", "repo": ".", "base_branch": "main"}
    resolve_project_cfg(cfg)
    assert cfg["repo"] == "." and cfg["base_branch"] == "main"


# ── fatal paths — the safety argument for the whole module ──


def test_unknown_project_is_fatal_and_names_what_it_knows(fake_host):
    fake_host([_ENTRY, {"name": "other", "path": "/work/other"}])
    with pytest.raises(ValueError) as exc:
        resolve_project_cfg({"project": "typo", "repo": "."})
    msg = str(exc.value)
    assert "typo" in msg
    assert "other, pa" in msg  # sorted known names, so the fix is obvious
    assert "worktrees, branches and PRs" in msg  # says WHY it refuses


def test_project_set_but_no_registry_is_fatal():
    """A pre-0.115.0 host can't resolve `project:` — building in cwd is not the
    fallback. registry_projects() degrades to [], resolution does NOT."""
    with pytest.raises(ValueError) as exc:
        resolve_project_cfg({"project": "pa", "repo": "."})
    assert "registry empty" in str(exc.value)


def test_entry_without_a_path_is_fatal(fake_host):
    fake_host([{"name": "pa", "github": "o/pa"}])  # registered for github only
    with pytest.raises(ValueError) as exc:
        resolve_project_cfg({"project": "pa", "repo": "."})
    assert "no `path`" in str(exc.value)


def test_a_raising_host_config_is_fatal_when_project_is_set(fake_host):
    """registry_projects() swallows it to [], and resolution then refuses —
    silently building in cwd because config was mid-reload is the exact bug."""
    fake_host([], raises=True)
    with pytest.raises(ValueError):
        resolve_project_cfg({"project": "pa", "repo": "."})
