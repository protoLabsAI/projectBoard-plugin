"""Register a repo as a board project at RUNTIME (#167).

NAMING: this module must NOT be called ``register`` — importing ``.register`` binds the
submodule as an attribute of the package and clobbers the package-level ``register()``
function the host calls, so plugin load dies with ``'module' object is not callable``.
``tests/test_packaging.py::test_register_wires_routers_surface_and_tools`` catches it.


`project_board.projects` was config-file-only: it is a nested map, not a declared
`settings:` field, so the settings API refuses it —

    POST /api/settings {"updates": {"project_board.projects": {...}}}
    → {"ok": false, "messages": ["validation: unknown setting: project_board.projects"]}

— and the console can't render it either. So an agent could clone a repo and register
it for *filesystem* reach (protoAgent's `onboard_project`, #2555) and then stop dead:
onboarding writes only `filesystem.projects`, and without a board entry no feature can
be dispatched there. The agent got a repo it could read and never one it could ship to,
and every board-managed repo cost an operator a YAML edit plus a restart.

This closes that half, mirroring `onboard_project`'s shape deliberately — same host
seam, same superset invariant, same "refuse and name the bound" posture:

- **Consent is the operator's `onboarding` space**, not this tool's own. It refuses
  unless `onboarding.enabled`, and the repo must resolve UNDER `onboarding.root`.
  Registering a board project is strictly narrower than the clone that preceded it: the
  path is already on disk and already inside the declared space.
- **The merge is a superset** — a register can never drop a sibling project. That is the
  protoAgent #2556 hazard (a replace-all route that silently dropped roots and answered
  `{"ok": true}`), and it is worth a belt-and-braces check on the tool side.
- **Idempotent by name**: re-registering updates in place rather than duplicating.

Writes through ``HOST.apply_settings`` (``graph.plugins.host``), never a ``server``
import — the same reason `onboard_project` uses it. Note the seam takes NESTED dicts
(``{"project_board": {"projects": …}}``); the dotted form is an HTTP-route convention
that gets expanded before it reaches here.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

#: Fields an operator writes by hand today, and the only ones this tool sets.
_ENTRY_FIELDS = ("repo", "base_branch", "local_gate_cmd", "repo_conventions")
_PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_MUTATION_LOCK = asyncio.Lock()


class ProjectRegistryError(ValueError):
    """An operator-actionable project registry refusal."""


def _live_section() -> dict[str, Any]:
    """The live ``project_board`` section, read afresh for every operation."""
    try:
        from graph.sdk import config as host_config

        live = host_config()
        plugin_config = getattr(live, "plugin_config", None)
        section = plugin_config.get("project_board") if isinstance(plugin_config, dict) else None
        if section is None:
            section = getattr(live, "project_board", None)
    except Exception:  # noqa: BLE001 — no host (tests, CLI)
        return {}
    return dict(section) if isinstance(section, dict) else {}


def _host_onboarding() -> tuple[bool, str]:
    """``(enabled, root)`` from the host's `onboarding` config (#2555).

    Read live rather than captured at register time: the operator can enable the space
    without restarting the member, and a tool that cached "disabled" at boot would keep
    refusing after they did."""
    try:
        from graph.sdk import config as host_config

        cfg = host_config()
    except Exception:  # noqa: BLE001 — no host (tests, CLI): treat as not consented
        return False, ""
    enabled = bool(getattr(cfg, "onboarding_enabled", False))
    root = str(getattr(cfg, "onboarding_root", "") or "")
    return enabled, root


def _resolve_under(root: str, repo: str) -> tuple[Path | None, str | None]:
    """``(resolved_repo, error)`` — the repo path, proven to sit under ``root``.

    Resolves both sides before comparing so a ``..`` escape or a symlink can't smuggle a
    path outside the consented space past a string prefix check."""
    if not root:
        return None, "onboarding.root isn't set, so there is no consented space to register within"
    try:
        root_p = Path(root).expanduser().resolve()
        repo_p = Path(repo).expanduser().resolve()
    except OSError as exc:
        return None, f"couldn't resolve the path: {exc}"
    if not repo_p.is_dir():
        return None, f"{repo_p} isn't a directory — clone it first"
    if not (repo_p / ".git").exists():
        return None, f"{repo_p} isn't a git checkout (no .git) — the board needs a repo to branch from"
    if root_p != repo_p and root_p not in repo_p.parents:
        return None, f"{repo_p} is outside the onboarding root {root_p} — registration refused"
    return repo_p, None


def _raw_projects() -> dict[str, Any]:
    """The board's `projects:` map as CONFIGURED, not as resolved.

    Deliberately not ``resolve_projects(cfg)``: that synthesizes an implicit project from
    the flat keys when no map is declared, and writing a synthesized entry back would
    persist a default the operator never wrote — turning an additive register into a
    silent config rewrite."""
    section = _live_section()
    projects = section.get("projects") or {}
    return dict(projects) if isinstance(projects, dict) else {}


def project_registry_snapshot() -> dict[str, Any]:
    """Public-safe live registry data for the authenticated console editor.

    Only the fields the editor owns are returned. Unknown/per-project advanced keys
    are named (so the operator knows they exist) but their values remain file-only;
    an editor save preserves them byte-for-byte.
    """
    section = _live_section()
    projects = _raw_projects()
    rows = []
    for name in sorted(projects):
        raw = projects[name] if isinstance(projects[name], dict) else {}
        row = {"name": name, **{key: raw.get(key, "") for key in _ENTRY_FIELDS}}
        row["extra_fields"] = sorted(set(raw) - set(_ENTRY_FIELDS))
        rows.append(row)
    enabled, root = _host_onboarding()
    return {
        "projects": rows,
        "default_project": str(section.get("default_project") or ""),
        "onboarding": {"enabled": enabled, "root": root},
    }


def _validate_name(name: str) -> str:
    project = str(name or "").strip()
    if not project:
        raise ProjectRegistryError("name is required — it is the key features carry")
    if not _PROJECT_NAME.fullmatch(project):
        raise ProjectRegistryError(
            "name must start with a letter or number and contain only letters, numbers, hyphens, or underscores"
        )
    return project


async def _apply_registry(
    projects: dict[str, Any],
    *,
    expected: dict[str, dict[str, Any]] | None = None,
    absent: set[str] | None = None,
    default_project: str | None = None,
) -> None:
    """Apply a complete registry and prove the intended live state landed."""
    from graph.plugins.host import HOST

    if HOST.apply_settings is None:
        raise ProjectRegistryError("project changes are unavailable — the host is not wired for config apply")

    section: dict[str, Any] = {"projects": projects}
    if default_project is not None:
        section["default_project"] = default_project
    ok, messages = await asyncio.to_thread(HOST.apply_settings, {"project_board": section})
    if not ok:
        raise ProjectRegistryError("; ".join(messages) or "the host refused the config update")

    persisted = _raw_projects()
    missing = sorted(set(projects) - set(persisted))
    unexpected = sorted((absent or set()) & set(persisted))
    mismatched = []
    for name, fields in (expected or {}).items():
        landed = persisted.get(name)
        for key, value in fields.items():
            if not isinstance(landed, dict) or landed.get(key) != value:
                mismatched.append(f"{name}.{key}")
    if missing or unexpected or mismatched:
        detail = []
        if missing:
            detail.append("missing project(s): " + ", ".join(missing))
        if unexpected:
            detail.append("deleted project(s) still present: " + ", ".join(unexpected))
        if mismatched:
            detail.append("fields did not persist: " + ", ".join(mismatched))
        raise ProjectRegistryError(
            "the host reported success, but live config readback failed ("
            + "; ".join(detail)
            + "); no success was assumed"
        )
    if default_project is not None and str(_live_section().get("default_project") or "") != default_project:
        raise ProjectRegistryError("the host reported success, but the default project did not persist")


async def upsert_project(
    name: str,
    repo: str,
    *,
    base_branch: str = "main",
    local_gate_cmd: str = "",
    repo_conventions: str = "",
    make_default: bool = False,
    replace_optional: bool = False,
) -> dict[str, Any]:
    """Add/update one project while preserving siblings and unowned entry fields."""
    project = _validate_name(name)
    if not str(repo or "").strip():
        raise ProjectRegistryError("repo is required — it is the checkout the board builds in")
    enabled, root = _host_onboarding()
    if not enabled:
        raise ProjectRegistryError(
            "project onboarding is off — enable Settings ▸ Project onboarding before changing boarded repos"
        )
    repo_p, err = _resolve_under(root, repo)
    if err:
        raise ProjectRegistryError(err)

    async with _MUTATION_LOCK:
        existing = _raw_projects()
        prior = existing.get(project)
        entry = dict(prior) if isinstance(prior, dict) else {}
        entry.update({"repo": str(repo_p), "base_branch": str(base_branch or "main").strip() or "main"})
        optional = {
            "local_gate_cmd": str(local_gate_cmd or "").strip(),
            "repo_conventions": str(repo_conventions or "").strip(),
        }
        for key, value in optional.items():
            if value:
                entry[key] = value
            elif replace_optional:
                entry.pop(key, None)
        merged = dict(existing)
        merged[project] = entry
        if not set(existing) <= set(merged):
            raise ProjectRegistryError("internal safety check failed — the update would drop a sibling project")
        default = project if make_default else None
        await _apply_registry(merged, expected={project: entry}, default_project=default)
        return {
            "project": project,
            "entry": entry,
            "created": project not in existing,
            "default_project": default,
        }


async def delete_project(name: str) -> dict[str, Any]:
    """Delete a project after its caller has proved no active card references it."""
    project = _validate_name(name)
    async with _MUTATION_LOCK:
        existing = _raw_projects()
        if project not in existing:
            raise ProjectRegistryError(f"unknown project {project!r}")
        merged = dict(existing)
        merged.pop(project)
        current_default = str(_live_section().get("default_project") or "")
        default = (next(iter(merged)) if len(merged) == 1 else "") if current_default == project else None
        await _apply_registry(merged, absent={project}, default_project=default)
        return {"project": project, "deleted": True, "default_project": default}


def build_register_tool(cfg: dict):
    """The ``board_register_project`` tool, or ``None`` when langchain isn't importable
    (host-free test runs import this module for its pure helpers)."""
    try:
        from langchain_core.tools import tool
    except Exception:  # noqa: BLE001 — host-free import; the helpers above still test
        return None

    @tool
    async def board_register_project(
        name: str,
        repo: str,
        base_branch: str = "main",
        local_gate_cmd: str = "",
        repo_conventions: str = "",
    ) -> str:
        """Register an already-cloned repo as a board project so features can be dispatched to it.

        Use this after a repo is on disk (onboard_project clones it and grants filesystem
        reach; this adds the board half so a coder can actually open PRs against it).
        Bounded by the operator's onboarding space: the repo must sit under the
        configured onboarding root, and onboarding must be enabled.

        Args:
            name: the project key features will carry (e.g. "pr-reviewer").
            repo: path to the checkout on disk.
            base_branch: branch worktrees are cut from. Defaults to main.
            local_gate_cmd: the pre-PR gate for THIS repo — its own lint/format/test
                command. A repo whose gate differs from the default will merge red
                without it.
            repo_conventions: repo-specific rules injected into every coder dispatch
                (changelog policy, import rules, gate quirks). Omitting this is the
                single most common cause of a coder inventing the wrong convention.

        Returns a line naming what was registered, or an error naming the bound it hit.
        """
        try:
            result = await upsert_project(
                name,
                repo,
                base_branch=base_branch,
                local_gate_cmd=local_gate_cmd,
                repo_conventions=repo_conventions,
            )
        except ProjectRegistryError as exc:
            return f"Error: {exc}."
        project, entry = result["project"], result["entry"]
        verb = "Registered" if result["created"] else "Updated"
        gate = "its own gate" if entry.get("local_gate_cmd") else "the default gate"
        conv = "with conventions" if entry.get("repo_conventions") else "WITHOUT conventions"
        note = (
            ""
            if entry.get("repo_conventions")
            else " — add repo_conventions before dispatching, or the coder will guess this repo's rules"
        )
        return (
            f"{verb} board project '{project}' → {entry['repo']} (base {entry['base_branch']}, "
            f"{gate}, {conv}). The running board applied it live; no restart is required.{note}"
        )

    return board_register_project
