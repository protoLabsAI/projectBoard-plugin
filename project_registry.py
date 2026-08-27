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

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Fields an operator writes by hand today, and the only ones this tool sets.
_ENTRY_FIELDS = ("repo", "base_branch", "local_gate_cmd", "repo_conventions")


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
    try:
        from graph.sdk import config as host_config

        live = host_config()
        plugin_config = getattr(live, "plugin_config", None)
        section = plugin_config.get("project_board") if isinstance(plugin_config, dict) else None
        # Compatibility with small host stubs and older embeddings that exposed a
        # plugin section directly. The production LangGraphConfig contract is the
        # plugin_config mapping above.
        if section is None:
            section = getattr(live, "project_board", None)
        section = section or {}
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(section, dict):
        return {}
    projects = section.get("projects") or {}
    return dict(projects) if isinstance(projects, dict) else {}


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
        project = (name or "").strip()
        if not project:
            return "Error: name is required — it's the key features carry."
        if not (repo or "").strip():
            return "Error: repo is required — the path to the checkout on disk."

        enabled, root = _host_onboarding()
        if not enabled:
            return (
                "Error: project onboarding is off, so there is no consented space to register "
                "within. An operator enables it under Settings ▸ Project onboarding "
                "(onboarding.enabled) and sets onboarding.root."
            )

        repo_p, err = _resolve_under(root, repo)
        if err:
            return f"Error: {err}."

        existing = _raw_projects()
        entry = {"repo": str(repo_p), "base_branch": (base_branch or "main").strip()}
        if local_gate_cmd.strip():
            entry["local_gate_cmd"] = local_gate_cmd.strip()
        if repo_conventions.strip():
            entry["repo_conventions"] = repo_conventions.strip()

        updating = project in existing
        merged = dict(existing)
        merged[project] = entry

        # A register must never DROP a sibling — the #2556 shape, where a replace-all
        # write quietly removed roots and still answered ok.
        if not set(existing) <= set(merged):
            log.error("[project_board] refusing to apply: merge would drop an existing project")
            return "Error: internal safety check failed — registration would have dropped a project; aborted."

        from graph.plugins.host import HOST

        if HOST.apply_settings is None:
            return "Error: registration is unavailable — the host isn't wired for config apply (no running server)."
        import asyncio

        ok, messages = await asyncio.to_thread(HOST.apply_settings, {"project_board": {"projects": merged}})
        if not ok:
            return f"Error: registering {project} failed: {'; '.join(messages) or 'unknown error'}"

        # apply_settings is synchronous with the graph reload. Never report success
        # merely because the host returned ok: read the live config back and prove
        # both the requested entry and every sibling survived the write.
        persisted = _raw_projects()
        missing_siblings = sorted(set(existing) - set(persisted))
        landed = persisted.get(project)
        mismatched = [key for key, value in entry.items() if not isinstance(landed, dict) or landed.get(key) != value]
        if missing_siblings or mismatched:
            detail = []
            if missing_siblings:
                detail.append("dropped sibling(s): " + ", ".join(missing_siblings))
            if mismatched:
                detail.append("entry did not persist field(s): " + ", ".join(mismatched))
            return (
                f"Error: the host reported that project {project!r} was saved, but live config readback failed "
                f"({'; '.join(detail)}). Registration is not active; no success was assumed."
            )

        verb = "Updated" if updating else "Registered"
        gate = "its own gate" if entry.get("local_gate_cmd") else "the default gate"
        conv = "with conventions" if entry.get("repo_conventions") else "WITHOUT conventions"
        note = (
            ""
            if entry.get("repo_conventions")
            else " — add repo_conventions before dispatching, or the coder will guess this repo's rules"
        )
        return (
            f"{verb} board project '{project}' → {repo_p} (base {entry['base_branch']}, "
            f"{gate}, {conv}). The running board applied it live; no restart is required.{note}"
        )

    return board_register_project
