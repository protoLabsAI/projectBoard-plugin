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
from pathlib import Path

import pytest

from project_board.projects import (
    default_project,
    registry_projects,
    resolve_project_cfg,
    resolve_projects,
    store_db_path,
)


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


# ── the board's own `projects:` map (#90) — distinct from the host registry above ──


def test_resolve_projects_parses_the_map_with_full_execution_settings():
    cfg = {
        "default_project": "protoagent",
        "projects": {
            "protoagent": {
                "repo": "/dev/protoAgent-team",
                "base_branch": "main",
                "local_gate_cmd": "make gate",
                "coders": {"smart": "sonnet", "reasoning": "opus"},
                "coder_solve_k": 3,
                "gate_files": ["CHANGELOG.md"],
                "repo_conventions": "keep it terse",
                "worktrees_root": ".worktrees",
                "format_cmd": "ruff format .",
                "env_passthrough": ["HOME"],
            },
            "board-plugin": {
                "repo": "/dev/projectBoard-plugin",
                "local_gate_cmd": "ruff check .",
                "coder_solve_k": 5,
            },
        },
    }
    out = resolve_projects(cfg)
    assert set(out) == {"protoagent", "board-plugin"}
    pa = out["protoagent"]
    # every declared execution setting rides through untouched…
    assert pa["repo"] == "/dev/protoAgent-team"
    assert pa["base_branch"] == "main"
    assert pa["local_gate_cmd"] == "make gate"
    assert pa["coders"] == {"smart": "sonnet", "reasoning": "opus"}
    assert pa["coder_solve_k"] == 3  # coder_solve_* carried by prefix, not a fixed key
    assert pa["gate_files"] == ["CHANGELOG.md"]
    assert pa["repo_conventions"] == "keep it terse"
    assert pa["worktrees_root"] == ".worktrees"
    assert pa["format_cmd"] == "ruff format ."
    assert pa["env_passthrough"] == ["HOME"]
    assert pa["name"] == "protoagent"  # each entry carries its own name
    # …and a sparse entry only carries what it declared
    bp = out["board-plugin"]
    assert bp["repo"] == "/dev/projectBoard-plugin"
    assert bp["coder_solve_k"] == 5
    assert "base_branch" not in bp


def test_resolve_projects_requires_repo_in_each_entry():
    """r4: a project entry with no `repo` has nowhere to build → ValueError naming it,
    never a silent bind to the server's cwd."""
    with pytest.raises(ValueError, match="board-plugin.*has no `repo`"):
        resolve_projects({"projects": {"board-plugin": {"local_gate_cmd": "ruff check ."}}})


def test_resolve_projects_expands_tilde_paths(monkeypatch):
    monkeypatch.setenv("HOME", "/home/kj")
    out = resolve_projects({"projects": {"pa": {"repo": "~/dev/pa", "worktrees_root": "~/wt"}}})
    assert out["pa"]["repo"] == "/home/kj/dev/pa"
    assert out["pa"]["worktrees_root"] == "/home/kj/wt"


def test_resolve_projects_does_not_mutate_the_input():
    cfg = {"projects": {"pa": {"repo": "/r"}}}
    resolve_projects(cfg)
    assert cfg == {"projects": {"pa": {"repo": "/r"}}}  # the entry is copied, never annotated in place


# ── back-compat: no `projects:` map ⇒ a single implicit project from the flat keys ──


def test_resolve_projects_synthesizes_an_implicit_project_from_flat_keys():
    """r2/r10: absent `projects:`, one implicit project is synthesized from today's flat
    top-level keys — so a config that predates the map keeps working, no migration."""
    cfg = {
        "repo": "/dev/thing",
        "base_branch": "trunk",
        "local_gate_cmd": "pytest -q",
        "coders": {"smart": "sonnet"},
        "coder_solve_k": 4,
        "gate_files": ["README.md"],
    }
    out = resolve_projects(cfg)
    assert list(out) == ["default"]  # the implicit name
    entry = out["default"]
    assert entry["repo"] == "/dev/thing"
    assert entry["base_branch"] == "trunk"
    assert entry["local_gate_cmd"] == "pytest -q"
    assert entry["coders"] == {"smart": "sonnet"}
    assert entry["coder_solve_k"] == 4
    assert entry["gate_files"] == ["README.md"]
    assert entry["name"] == "default"


def test_resolve_projects_implicit_project_defaults_repo_to_dot():
    """Back-compat: a flat config with no `repo` still yields a project (repo='.'), NOT a
    ValueError — the pre-#90 single-repo default. (The ValueError is only for explicit
    `projects:` entries.)"""
    assert resolve_projects({"base_branch": "main"})["default"]["repo"] == "."


def test_resolve_projects_implicit_project_takes_the_default_project_name():
    out = resolve_projects({"default_project": "solo", "repo": "/r"})
    assert list(out) == ["solo"]
    assert out["solo"]["repo"] == "/r"


# ── default_project: which project a feature falls back to at create time ──


def test_default_project_prefers_the_configured_name():
    cfg = {"default_project": "pa", "projects": {"pa": {"repo": "/r"}, "x": {"repo": "/y"}}}
    assert default_project(cfg) == "pa"


def test_default_project_falls_back_to_the_sole_project():
    assert default_project({"projects": {"only": {"repo": "/r"}}}) == "only"
    assert default_project({"repo": "/r"}) == "default"  # the implicit single project


def test_default_project_is_empty_for_a_multi_project_map_with_no_default():
    assert default_project({"projects": {"a": {"repo": "/a"}, "b": {"repo": "/b"}}}) == ""


# ── the published instance-store db default (D3, #260) ──
# The manifest ships `db_path: ""` and every store construction resolves that blank
# through `store_db_path` to the ONE instance-default store — never per-repo
# `.beads/` discovery. An explicit path is the documented operator override.


def test_manifest_ships_a_blank_db_path_default():
    """The config golden for D3: the manifest declares `db_path: ""`. There are
    exactly two modes — blank rides the instance store, an explicit path is the
    operator pin; the pre-D3 per-repo discovery mode is gone, so a manifest that
    shipped anything else here would silently re-pin every fresh board."""
    import yaml

    import project_board

    manifest = yaml.safe_load((Path(project_board.__file__).parent / "protoagent.plugin.yaml").read_text())
    assert manifest["config"]["db_path"] == ""


def test_manifest_default_config_resolves_to_the_instance_store(monkeypatch):
    """The manifest's own shipped config block, fed through the D3 seam, lands on the
    instance-default store — pinning the manifest default and the runtime resolution
    together, so neither can drift without this golden moving."""
    import yaml

    import project_board
    from project_board import store as store_mod

    monkeypatch.setattr(store_mod, "default_db_path", lambda: "/inst/project_board/.beads/beads.db")
    manifest = yaml.safe_load((Path(project_board.__file__).parent / "protoagent.plugin.yaml").read_text())
    assert store_db_path(manifest["config"]) == "/inst/project_board/.beads/beads.db"


def test_api_store_kw_resolves_the_db_at_the_config_seam(monkeypatch):
    """api._store_kw (the kwargs BOTH routers construct stores with) resolves the db
    exactly as __init__'s tool store_kw and the coder-monitor persist factory do (D3):
    a blank/absent db_path rides the ONE instance store, an explicit path stays the
    operator pin verbatim — the API can never fall back to per-repo discovery."""
    from project_board import api
    from project_board import store as store_mod

    monkeypatch.setattr(store_mod, "default_db_path", lambda: "/inst/project_board/.beads/beads.db")
    for cfg in (None, {}, {"db_path": ""}, {"db_path": "   "}):
        assert api._store_kw(cfg)["db"] == "/inst/project_board/.beads/beads.db", cfg
    assert api._store_kw({"db_path": "/pin/board.db"})["db"] == "/pin/board.db"


def test_api_two_projects_and_no_db_path_share_the_one_instance_store(monkeypatch):
    """A fresh instance serving TWO projects with no db_path: every store the API
    constructs carries the SAME instance db — one board, every card in one store —
    while each project keeps its own repo for the Ready gate's path checks. The db is
    a real path (not blank), so `store._ensure_workspace` pins every op with `--db`
    and never runs `br init` inside either project repo."""
    from project_board import api
    from project_board import store as store_mod

    monkeypatch.setattr(store_mod, "default_db_path", lambda: "/inst/project_board/.beads/beads.db")
    cfg = {"projects": {"web": {"repo": "/work/web"}, "api": {"repo": "/work/api"}}}
    kw = api._store_kw(cfg)
    assert kw["db"] == "/inst/project_board/.beads/beads.db"
    assert {name: entry["repo"] for name, entry in kw["projects"].items()} == {
        "web": "/work/web",
        "api": "/work/api",
    }


def test_api_blank_db_path_reads_the_same_store_get_store_already_resolved(monkeypatch, tmp_path):
    """The upgrade-safety pin for the seam move (#260): `get_store` resolves a blank
    db to the instance default ITSELF (`db = db or default_db_path()`), so the
    pre-seam API call (`db=cfg.get("db_path") or None`) and the resolved `_store_kw`
    value land on the SAME cache key — the routers read exactly the board they read
    before the seam existed, and no installation's store is re-homed by resolving at
    the config seam instead of inside get_store."""
    from project_board import api
    from project_board import store as store_mod

    inst = str(tmp_path / "inst" / ".beads" / "beads.db")
    monkeypatch.setattr(store_mod, "default_db_path", lambda: inst)
    monkeypatch.setattr(store_mod.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    monkeypatch.setattr(store_mod, "_BOARDS", {})
    cfg = {"repo": str(tmp_path)}  # no db_path — the shipped default
    before = store_mod.get_store(db=cfg.get("db_path") or None, repo=str(tmp_path))  # the pre-seam call
    after = store_mod.get_store(db=api._store_kw(cfg)["db"], repo=str(tmp_path))  # the resolved seam
    assert after is before and before.db == inst
