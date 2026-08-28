"""Runtime board-project registration (#167).

The gap: `project_board.projects` had no runtime write path, so an agent could clone a
repo and gain filesystem reach (protoAgent's `onboard_project`) and still not dispatch a
feature to it — every board-managed repo cost an operator a YAML edit and a restart.

These cover the halves that must not rot: the bound (a repo outside the consented space
is refused, by name), the superset invariant (a register never drops a sibling), and
idempotency (re-registering updates rather than duplicating).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import types
from pathlib import Path

import pytest

from project_registry import (
    ProjectRegistryError,
    _raw_projects,
    _resolve_under,
    build_register_tool,
    delete_project,
    project_registry_snapshot,
    upsert_project,
)


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
        cfg.plugin_config["project_board"].update(patch["project_board"])
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


async def test_register_does_not_claim_success_when_persistence_changes_a_sibling(monkeypatch, tmp_path):
    root = tmp_path / "dev"
    repo = _git_repo(root / "beta")
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root=str(root),
        plugin_config={"project_board": {"projects": {"alpha": {"repo": "/a", "coders": {"smart": "proto"}}}}},
    )

    def lossy_apply(patch):
        landed = patch["project_board"]
        cfg.plugin_config["project_board"].update(landed)
        cfg.plugin_config["project_board"]["projects"]["alpha"].pop("coders")
        return True, []

    _wire_host(monkeypatch, cfg, lossy_apply)
    tool = build_register_tool({})
    result = await tool.ainvoke({"name": "beta", "repo": str(repo)})

    assert result.startswith("Error:")
    assert "project entries changed during persistence: alpha" in result


# ── the agent tool surface: no executable-shaped input (hardening) ─────────────
def test_agent_tool_schema_carries_no_gate_command_parameter():
    """The persisted gate is executed at dispatch time, so the agent tool must not
    expose a parameter that carries command text — that write path is operator-only
    (PUT /projects/{name}). The tool's `gate` takes the discovery sentinel instead."""
    tool = build_register_tool({})
    assert tool is not None
    assert "local_gate_cmd" not in tool.args
    assert set(tool.args) == {"name", "repo", "base_branch", "gate", "repo_conventions"}


async def test_register_with_gate_auto_persists_the_discovery_sentinel(monkeypatch, tmp_path):
    """`gate="auto"` is the one gate value the agent may set: the loop resolves it
    against the repo's OWN declared target, so no agent-supplied text is executed."""
    root = tmp_path / "dev"
    repo = _git_repo(root / "beta")
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root=str(root),
        plugin_config={"project_board": {"projects": {}}},
    )

    def apply_settings(patch):
        cfg.plugin_config["project_board"].update(patch["project_board"])
        return True, []

    _wire_host(monkeypatch, cfg, apply_settings)
    tool = build_register_tool({})
    result = await tool.ainvoke({"name": "beta", "repo": str(repo), "gate": "auto"})

    assert not result.startswith("Error:")
    assert cfg.plugin_config["project_board"]["projects"]["beta"]["local_gate_cmd"] == "auto"
    assert "an auto-discovered gate" in result


async def test_register_refuses_any_gate_value_other_than_the_sentinel(monkeypatch, tmp_path):
    """Anything but ""/"auto" is refused BEFORE config apply, naming the bound."""
    root = tmp_path / "dev"
    repo = _git_repo(root / "beta")
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root=str(root),
        plugin_config={"project_board": {"projects": {}}},
    )
    applied = []
    _wire_host(monkeypatch, cfg, lambda patch: (applied.append(patch) or True, []))

    tool = build_register_tool({})
    result = await tool.ainvoke({"name": "beta", "repo": str(repo), "gate": "not-the-sentinel"})

    assert result.startswith("Error:")
    assert 'only the literal "auto"' in result and "operator configuration" in result
    assert applied == []


async def test_agent_reregister_with_blank_gate_preserves_the_operator_set_one(monkeypatch, tmp_path):
    """An agent re-register must not clear (or need to restate) the gate the operator
    configured through the bearer-gated editor."""
    root = tmp_path / "dev"
    repo = _git_repo(root / "alpha")
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root=str(root),
        plugin_config={"project_board": {"projects": {"alpha": {"repo": str(repo), "local_gate_cmd": "make gate"}}}},
    )

    def apply_settings(patch):
        cfg.plugin_config["project_board"].update(patch["project_board"])
        return True, []

    _wire_host(monkeypatch, cfg, apply_settings)
    tool = build_register_tool({})
    result = await tool.ainvoke({"name": "alpha", "repo": str(repo)})

    assert not result.startswith("Error:")
    assert cfg.plugin_config["project_board"]["projects"]["alpha"]["local_gate_cmd"] == "make gate"


async def test_editor_update_preserves_siblings_and_file_only_entry_fields(monkeypatch, tmp_path):
    root = tmp_path / "dev"
    repo = _git_repo(root / "alpha")
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root=str(root),
        plugin_config={
            "project_board": {
                "projects": {
                    "alpha": {"repo": str(repo), "coders": {"smart": "proto"}, "local_gate_cmd": "old"},
                    "beta": {"repo": "/b"},
                }
            }
        },
    )

    def apply_settings(patch):
        cfg.plugin_config["project_board"].update(patch["project_board"])
        return True, []

    _wire_host(monkeypatch, cfg, apply_settings)
    result = await upsert_project("alpha", str(repo), local_gate_cmd="", replace_optional=True)

    projects = cfg.plugin_config["project_board"]["projects"]
    assert set(projects) == {"alpha", "beta"}
    assert projects["alpha"]["coders"] == {"smart": "proto"}
    assert "local_gate_cmd" not in projects["alpha"]
    assert result["entry"]["extra_fields"] == ["coders"]
    assert "coders" not in result["entry"]
    assert "proto" not in str(result)


def test_registry_snapshot_names_but_does_not_return_file_only_values(monkeypatch):
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root="/dev",
        plugin_config={
            "project_board": {
                "default_project": "alpha",
                "projects": {"alpha": {"repo": "/dev/a", "coders": {"opus": "secret-agent-name"}}},
            }
        },
    )
    _wire_host(monkeypatch, cfg, lambda _patch: (True, []))

    snapshot = project_registry_snapshot()
    assert snapshot["default_project"] == "alpha"
    assert snapshot["projects"][0]["extra_fields"] == ["coders"]
    assert "coders" not in snapshot["projects"][0]
    assert "secret-agent-name" not in str(snapshot)


def test_registry_snapshot_marks_malformed_file_only_entries_read_only(monkeypatch):
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root="/dev",
        plugin_config={"project_board": {"projects": {"broken": "not-a-mapping"}}},
    )
    _wire_host(monkeypatch, cfg, lambda _patch: (True, []))
    row = project_registry_snapshot()["projects"][0]
    assert row["name"] == "broken" and row["editable"] is False
    assert row["repo"] == "" and row["extra_fields"] == []


def test_registry_snapshot_reports_the_runtime_effective_sole_default(monkeypatch):
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root="/dev",
        plugin_config={"project_board": {"projects": {"alpha": {"repo": "/dev/a"}}}},
    )
    _wire_host(monkeypatch, cfg, lambda _patch: (True, []))

    assert project_registry_snapshot()["default_project"] == "alpha"


async def test_simultaneous_upserts_serialize_the_live_read_merge_write(monkeypatch, tmp_path):
    root = tmp_path / "dev"
    a, b = _git_repo(root / "a"), _git_repo(root / "b")
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root=str(root),
        plugin_config={"project_board": {"projects": {}}},
    )

    def apply_settings(patch):
        cfg.plugin_config["project_board"].update(patch["project_board"])
        return True, []

    _wire_host(monkeypatch, cfg, apply_settings)
    await asyncio.gather(upsert_project("a", str(a)), upsert_project("b", str(b)))
    assert set(cfg.plugin_config["project_board"]["projects"]) == {"a", "b"}


def test_registry_mutation_lock_is_process_stable_across_plugin_reload():
    import project_registry as registry

    assert sys.modules[registry._LOCK_SLOT].lock is registry._MUTATION_LOCK


async def test_adding_a_second_project_preserves_the_implicit_sole_default(monkeypatch, tmp_path):
    root = tmp_path / "dev"
    alpha, beta = _git_repo(root / "alpha"), _git_repo(root / "beta")
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root=str(root),
        plugin_config={"project_board": {"projects": {"alpha": {"repo": str(alpha)}}}},
    )

    def apply_settings(patch):
        cfg.plugin_config["project_board"].update(patch["project_board"])
        return True, []

    _wire_host(monkeypatch, cfg, apply_settings)
    result = await upsert_project("beta", str(beta))

    assert result["default_project"] == "alpha"
    assert cfg.plugin_config["project_board"]["default_project"] == "alpha"


async def test_editor_refuses_to_overwrite_a_malformed_entry(monkeypatch, tmp_path):
    root = tmp_path / "dev"
    repo = _git_repo(root / "broken")
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root=str(root),
        plugin_config={"project_board": {"projects": {"broken": "not-a-mapping"}}},
    )
    applied = []
    _wire_host(monkeypatch, cfg, lambda patch: (applied.append(patch) or True, []))

    with pytest.raises(ProjectRegistryError, match="not a mapping"):
        await upsert_project("broken", str(repo), replace_optional=True)
    assert applied == []


async def test_editor_fails_closed_when_the_live_projects_container_is_malformed(monkeypatch, tmp_path):
    root = tmp_path / "dev"
    repo = _git_repo(root / "alpha")
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root=str(root),
        plugin_config={"project_board": {"projects": ["not", "a", "mapping"]}},
    )
    applied = []
    _wire_host(monkeypatch, cfg, lambda patch: (applied.append(patch) or True, []))

    with pytest.raises(ProjectRegistryError, match="projects is not a mapping"):
        await upsert_project("alpha", str(repo))
    assert applied == []


async def test_queued_upsert_rechecks_live_onboarding_consent_inside_the_lock(monkeypatch, tmp_path):
    import project_registry as registry

    root = tmp_path / "dev"
    repo = _git_repo(root / "alpha")
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root=str(root),
        plugin_config={"project_board": {"projects": {}}},
    )
    applied = []
    _wire_host(monkeypatch, cfg, lambda patch: (applied.append(patch) or True, []))

    class ConsentChangesBeforeEntry:
        async def __aenter__(self):
            cfg.onboarding_enabled = False

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(registry, "_MUTATION_LOCK", ConsentChangesBeforeEntry())

    with pytest.raises(ProjectRegistryError, match="onboarding is off"):
        await upsert_project("alpha", str(repo))
    assert applied == []


async def test_delete_reassigns_a_deleted_default_to_the_only_survivor(monkeypatch):
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root="/dev",
        plugin_config={
            "project_board": {
                "default_project": "alpha",
                "projects": {"alpha": {"repo": "/a"}, "beta": {"repo": "/b"}},
            }
        },
    )

    def apply_settings(patch):
        cfg.plugin_config["project_board"].update(patch["project_board"])
        return True, []

    _wire_host(monkeypatch, cfg, apply_settings)
    await delete_project("alpha")
    assert cfg.plugin_config["project_board"]["projects"] == {"beta": {"repo": "/b"}}
    assert cfg.plugin_config["project_board"]["default_project"] == "beta"


async def test_editor_refuses_to_claim_it_cleared_an_implicit_sole_default(monkeypatch, tmp_path):
    root = tmp_path / "dev"
    repo = _git_repo(root / "alpha")
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root=str(root),
        plugin_config={"project_board": {"default_project": "alpha", "projects": {"alpha": {"repo": str(repo)}}}},
    )

    def apply_settings(patch):
        cfg.plugin_config["project_board"].update(patch["project_board"])
        return True, []

    _wire_host(monkeypatch, cfg, apply_settings)
    with pytest.raises(ProjectRegistryError, match="only project"):
        await upsert_project("alpha", str(repo), clear_default=True)
    assert cfg.plugin_config["project_board"]["default_project"] == "alpha"


async def test_editor_can_clear_an_explicit_default_on_a_multi_project_board(monkeypatch, tmp_path):
    root = tmp_path / "dev"
    alpha, beta = _git_repo(root / "alpha"), _git_repo(root / "beta")
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root=str(root),
        plugin_config={
            "project_board": {
                "default_project": "alpha",
                "projects": {"alpha": {"repo": str(alpha)}, "beta": {"repo": str(beta)}},
            }
        },
    )

    def apply_settings(patch):
        cfg.plugin_config["project_board"].update(patch["project_board"])
        return True, []

    _wire_host(monkeypatch, cfg, apply_settings)
    result = await upsert_project("alpha", str(alpha), clear_default=True)

    assert result["default_project"] == ""
    assert cfg.plugin_config["project_board"]["default_project"] == ""


async def test_invalid_base_branch_is_refused_before_config_apply(monkeypatch, tmp_path):
    root = tmp_path / "dev"
    repo = _git_repo(root / "alpha")
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root=str(root),
        plugin_config={"project_board": {"projects": {}}},
    )
    applied = []
    _wire_host(monkeypatch, cfg, lambda patch: (applied.append(patch) or True, []))
    with pytest.raises(ValueError, match="valid Git branch"):
        await upsert_project("alpha", str(repo), base_branch="bad branch")
    assert applied == []


async def test_delete_runs_live_unused_check_inside_registry_mutation(monkeypatch):
    cfg = types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root="/dev",
        plugin_config={"project_board": {"projects": {"alpha": {"repo": "/a"}}}},
    )
    events = []

    def apply_settings(patch):
        events.append("apply")
        cfg.plugin_config["project_board"].update(patch["project_board"])
        return True, []

    async def assert_unused(name, effective_default):
        events.append(f"check:{name}")
        assert effective_default == "alpha"

    _wire_host(monkeypatch, cfg, apply_settings)
    await delete_project("alpha", assert_unused=assert_unused)
    assert events == ["check:alpha", "apply"]


# ── registration-time gate smoke (#261) ───────────────────────────────────────
# upsert used to validate only the gate command's LENGTH; its first execution was the
# loop's gate preflight, which discovers a broken gate long after the PUT answered ok —
# and answers by silently holding the project's ready work. The operator is present at
# registration, so an incoming explicit gate is smoked once on the clean base BEFORE
# anything persists: a red gate refuses (naming the failure with the output tail), an
# indeterminate verdict (timeout / dirty checkout / force) warns loudly and persists.

_LOG = "protoagent.plugins.project_board"


def _smoke_cfg(root: Path) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        onboarding_enabled=True,
        onboarding_root=str(root),
        plugin_config={"project_board": {"projects": {}}},
    )


def _recording_apply(cfg, applied):
    def apply_settings(patch):
        applied.append(patch)
        cfg.plugin_config["project_board"].update(patch["project_board"])
        return True, []

    return apply_settings


async def test_a_gate_that_fails_on_the_clean_base_refuses_naming_the_output_tail(monkeypatch, tmp_path):
    root = tmp_path / "dev"
    repo = _git_repo(root / "alpha")  # fresh init: clean, so the red verdict convicts the base
    cfg = _smoke_cfg(root)
    applied = []
    _wire_host(monkeypatch, cfg, _recording_apply(cfg, applied))

    with pytest.raises(ProjectRegistryError, match="failed on the clean base") as exc:
        await upsert_project("alpha", str(repo), local_gate_cmd="echo the-suite-is-broken; exit 3")

    assert "the-suite-is-broken" in str(exc.value)  # the output tail names the failure
    assert "exit 3" in str(exc.value)
    assert applied == []  # refused BEFORE persistence — nothing reached the host


async def test_a_passing_gate_persists_as_before(monkeypatch, tmp_path):
    root = tmp_path / "dev"
    repo = _git_repo(root / "alpha")
    cfg = _smoke_cfg(root)
    applied = []
    _wire_host(monkeypatch, cfg, _recording_apply(cfg, applied))

    result = await upsert_project("alpha", str(repo), local_gate_cmd="exit 0")

    assert applied  # persisted exactly as a gate-less register would
    assert cfg.plugin_config["project_board"]["projects"]["alpha"]["local_gate_cmd"] == "exit 0"
    assert result["entry"]["local_gate_cmd"] == "exit 0"


async def test_the_auto_sentinel_is_never_executed_at_registration(monkeypatch, tmp_path):
    """`auto` is the loop's dispatch-time discovery sentinel, not a command — smoking it
    would shell the literal word `auto` and refuse on 'command not found'. Persisting
    cleanly proves it was not run."""
    root = tmp_path / "dev"
    repo = _git_repo(root / "alpha")
    cfg = _smoke_cfg(root)
    applied = []
    _wire_host(monkeypatch, cfg, _recording_apply(cfg, applied))

    await upsert_project("alpha", str(repo), local_gate_cmd="auto")

    assert cfg.plugin_config["project_board"]["projects"]["alpha"]["local_gate_cmd"] == "auto"


async def test_a_reregister_that_preserves_the_prior_gate_does_not_resmoke_it(monkeypatch, tmp_path):
    """Only the gate text THIS call carries is smoked. A re-register with a blank gate
    keeps the operator's configured one untouched — re-running it here would let a red
    suite refuse an unrelated conventions update (the loop's preflight already gates
    dispatch on the preserved command)."""
    root = tmp_path / "dev"
    repo = _git_repo(root / "alpha")
    cfg = _smoke_cfg(root)
    cfg.plugin_config["project_board"]["projects"]["alpha"] = {"repo": str(repo.resolve()), "local_gate_cmd": "exit 7"}
    applied = []
    _wire_host(monkeypatch, cfg, _recording_apply(cfg, applied))

    await upsert_project("alpha", str(repo), repo_conventions="Run tests")

    assert applied  # the update landed even though the preserved gate is red
    assert cfg.plugin_config["project_board"]["projects"]["alpha"]["local_gate_cmd"] == "exit 7"


async def test_force_gate_downgrades_a_red_smoke_to_a_loud_warning(monkeypatch, tmp_path, caplog):
    root = tmp_path / "dev"
    repo = _git_repo(root / "alpha")
    cfg = _smoke_cfg(root)
    applied = []
    _wire_host(monkeypatch, cfg, _recording_apply(cfg, applied))

    with caplog.at_level(logging.WARNING, logger=_LOG):
        await upsert_project("alpha", str(repo), local_gate_cmd="exit 3", force_gate=True)

    assert cfg.plugin_config["project_board"]["projects"]["alpha"]["local_gate_cmd"] == "exit 3"
    assert "persisting under" in caplog.text and "force" in caplog.text  # loud, not silent


async def test_a_red_gate_on_a_dirty_checkout_is_indeterminate_and_persists(monkeypatch, tmp_path, caplog):
    """#255 transplanted to registration: a checkout carrying the operator's uncommitted
    edits makes the gate's verdict about THOSE edits, not the base every worktree
    branches from — so the refusal downgrades to a loud warning and the loop's
    preflight stays the dispatch-time enforcement."""
    root = tmp_path / "dev"
    repo = _git_repo(root / "alpha")
    (repo / "f.txt").write_text("one\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"],
        cwd=repo,
        check=True,
    )
    (repo / "f.txt").write_text("two\n")  # uncommitted tracked change → dirt
    cfg = _smoke_cfg(root)
    applied = []
    _wire_host(monkeypatch, cfg, _recording_apply(cfg, applied))

    with caplog.at_level(logging.WARNING, logger=_LOG):
        await upsert_project("alpha", str(repo), local_gate_cmd="exit 3")

    assert cfg.plugin_config["project_board"]["projects"]["alpha"]["local_gate_cmd"] == "exit 3"
    assert "NOT at base" in caplog.text and "indeterminate" in caplog.text


async def test_a_gate_smoke_timeout_is_indeterminate_and_persists(monkeypatch, tmp_path, caplog):
    """A slow gate must not make registration impossible — mirror the loop's timeout
    posture: no verdict, allow, and say so loudly."""
    import project_registry as registry

    monkeypatch.setattr(registry, "_SMOKE_TIMEOUT_S", 0.2)
    root = tmp_path / "dev"
    repo = _git_repo(root / "alpha")
    cfg = _smoke_cfg(root)
    applied = []
    _wire_host(monkeypatch, cfg, _recording_apply(cfg, applied))

    with caplog.at_level(logging.WARNING, logger=_LOG):
        await upsert_project("alpha", str(repo), local_gate_cmd="sleep 5; exit 1")

    assert cfg.plugin_config["project_board"]["projects"]["alpha"]["local_gate_cmd"] == "sleep 5; exit 1"
    assert "timed out" in caplog.text


async def test_a_smoke_timeout_kills_the_whole_gate_tree_not_just_the_shell(monkeypatch, tmp_path, caplog):
    """The smoked shell's descendants inherit its stdout pipe, so killing only the
    shell on timeout leaves them running with the pipe open — and the PUT blocked
    until they exit on their own. The group kill takes the descendant down with the
    shell: registration answers promptly and leaks no orphaned gate processes."""
    import project_registry as registry

    monkeypatch.setattr(registry, "_SMOKE_TIMEOUT_S", 1.0)
    root = tmp_path / "dev"
    repo = _git_repo(root / "alpha")
    cfg = _smoke_cfg(root)
    applied = []
    _wire_host(monkeypatch, cfg, _recording_apply(cfg, applied))
    pid_file = tmp_path / "descendant.pid"
    # A background subshell — a DESCENDANT of the smoked shell — records its pid,
    # then outlives the smoke timeout while holding the inherited stdout pipe open.
    cmd = f"sh -c 'echo $$ > \"{pid_file}\"; exec sleep 120' & sleep 120"

    with caplog.at_level(logging.WARNING, logger=_LOG):
        await upsert_project("alpha", str(repo), local_gate_cmd=cmd)

    assert "timed out" in caplog.text  # still the indeterminate-verdict posture
    assert cfg.plugin_config["project_board"]["projects"]["alpha"]["local_gate_cmd"] == cmd
    assert pid_file.exists(), "the descendant never started — the smoke did not run the gate"
    pid = int(pid_file.read_text())
    for _ in range(50):  # SIGKILL lands immediately; init just needs a moment to reap
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)  # do not leak a two-minute sleeper into the run
        pytest.fail("the gate's descendant survived the smoke timeout — only the shell was killed")
