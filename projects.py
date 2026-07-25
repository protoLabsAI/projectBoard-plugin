"""Resolve `project_board.project` against the host's managed-projects registry.

protoAgent 0.115.0 added a top-level ``projects:`` registry (ADR 0095) — one place
to declare a project, with consumers projecting from it instead of re-declaring it.
This is the board's half: set ``project_board.project: <name>`` and the board takes
that project's ``path`` as its repo and its ``default_branch`` as the base branch,
instead of repeating both here.

**Unresolvable is FATAL, by design.** The board is a writer: it creates worktrees,
branches and PRs. ``repo`` defaults to ``"."``, so silently falling back on a typo'd
or missing project name would start building in whatever the server's cwd happens to
be — wrong-repo branches and PRs, discovered later by a human. So a ``project:`` that
isn't in the registry raises out of ``register()``. The host catches that, records
the message on the plugin entry and skips the plugin (boot is unaffected), which is
exactly the outcome we want: the board doesn't run, and the operator is told why.
A misresolved read is cosmetic; a misresolved WRITE target is not.

**Sentinel caveat, stated plainly.** The manifest ships ``repo: "."`` and
``base_branch: main`` as defaults, and the resolved plugin config always carries
them — so this module cannot tell "the operator chose this" from "nobody set it".
It treats exactly those two default values as unset, meaning the registry supplies
both unless the operator has set something *different* from the default. The one
case this gets wrong: wanting ``base_branch: main`` explicitly while the registry
entry says ``master`` — you'd get ``master``. Fix the registry entry, or leave
``project`` unset and configure ``repo``/``base_branch`` directly.
"""

from __future__ import annotations

# The manifest's own defaults. A resolved config always carries these (the loader
# falls back to `manifest.config`), so they double as "operator didn't choose".
_DEFAULT_REPO = "."
_DEFAULT_BASE_BRANCH = "main"


def registry_projects() -> list[dict]:
    """The host's ADR 0095 registry, or ``[]`` on any host without one.

    Lazy + broadly guarded so the plugin's ``min_protoagent_version`` can stay at
    0.27.0: no host (the host-free suite), a pre-0.115.0 host (no ``projects``
    attribute) and config-not-yet-loaded all yield ``[]``.
    """
    try:
        from graph.sdk import config

        entries = getattr(config(), "projects", None) or []
    except Exception:  # noqa: BLE001 — no host / older host / config unloaded
        return []
    return [e for e in entries if isinstance(e, dict)]


def resolve_project_cfg(cfg: dict) -> dict:
    """Layer the registry under the board's own repo settings.

    No ``project:`` set ⇒ returns ``cfg`` untouched, so every existing config and
    every older host behaves exactly as before. Raises ``ValueError`` when
    ``project:`` names something the registry doesn't have (see module docstring).
    """
    name = str((cfg or {}).get("project") or "").strip()
    if not name:
        return dict(cfg or {})

    entries = registry_projects()
    match = next((e for e in entries if str(e.get("name") or "").strip() == name), None)
    if match is None:
        known = ", ".join(sorted(str(e.get("name") or "?") for e in entries)) or "(registry empty)"
        raise ValueError(
            f"project_board.project={name!r} is not in the host's projects registry. "
            f"Known projects: {known}. The board creates worktrees, branches and PRs, "
            f"so it will not fall back to repo={_DEFAULT_REPO!r} and build somewhere "
            f"unintended — fix the name, add the project to the host's `projects:` "
            f"registry (needs protoAgent 0.115.0+), or clear `project` and set "
            f"`repo`/`base_branch` directly."
        )

    path = str(match.get("path") or "").strip()
    if not path:
        raise ValueError(
            f"project_board.project={name!r} resolves to a registry entry with no `path`. "
            f"The board has nowhere to build — fix the entry or set `repo` directly."
        )

    out = dict(cfg or {})
    if str(out.get("repo") or "").strip() in ("", _DEFAULT_REPO):
        out["repo"] = path
    if str(out.get("base_branch") or "").strip() in ("", _DEFAULT_BASE_BRANCH):
        out["base_branch"] = str(match.get("default_branch") or "").strip() or _DEFAULT_BASE_BRANCH
    return out
