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

import os
import re

# The manifest's own defaults. A resolved config always carries these (the loader
# falls back to `manifest.config`), so they double as "operator didn't choose".
_DEFAULT_REPO = "."
_DEFAULT_BASE_BRANCH = "main"

# ── the board's own `projects:` map (#90) ─────────────────────────────────────────
# DISTINCT from `resolve_project_cfg` above, which resolves a single `project:` name
# against the HOST's ADR 0095 registry. `resolve_projects` parses the BOARD's own
# `projects:` config map — one entry per repo a single board instance serves, each
# carrying that repo's full execution settings (gate command, coders, breadth of a
# solve, …). The two compose: `resolve_project_cfg` still resolves the flat repo/
# base_branch, and `resolve_projects` reads the map (or, absent one, synthesizes a
# single implicit project from those same flat keys — so a config that predates the
# map behaves exactly as before, no migration).

# Name of the single project synthesized from the flat keys when no `projects:` map
# is declared (and no `default_project` names it) — the back-compat identity.
IMPLICIT_PROJECT_NAME = "default"
# The key set on the implicit project's entry (see ``_synthesize_implicit_project``).
IMPLICIT_MARKER = "implicit"

# The execution settings a project entry carries. Copied verbatim from the entry
# (or, for the implicit project, lifted from the flat top-level keys); any key that
# is unset is simply absent. `coder_solve_*` is a FAMILY (coder_solve_k,
# coder_solve_test_cmd, coder_solve_budget, …), handled by prefix below, not listed
# here. `repo` is required and normalized (`~` expanded); everything else passes
# through untouched.
_PROJECT_SETTING_KEYS = (
    "repo",
    "base_branch",
    "local_gate_cmd",
    "coders",
    "gate_files",
    "repo_conventions",
    "worktrees_root",
    "format_cmd",
    "setup_cmd",
    "env_passthrough",
    "release_freeze",
    "external_review",
    "breadth_exclude",
    "hot_files",
    "preflight_cmd",
    "require_complete_review",
    "review_summon_handle",
)
# Card-authoring policy (#455, #458) that a top-level value sets for EVERY project, an
# entry's own value overriding it — the `release_freeze` posture. (The execution keys
# above deliberately don't inherit: a gate command or a ladder belongs to one repo.)
_INHERITED_SETTING_KEYS = ("breadth_exclude", "hot_files")
_CODER_SOLVE_PREFIX = "coder_solve_"


def _copy_settings(src: dict) -> dict:
    """Lift the execution settings out of a raw entry/flat-config dict — the fixed
    keys plus every `coder_solve_*` knob."""
    entry = {k: src[k] for k in _PROJECT_SETTING_KEYS if k in src}
    for k, v in src.items():
        if str(k).startswith(_CODER_SOLVE_PREFIX):
            entry[k] = v
    return entry


def _expand_paths(entry: dict) -> dict:
    """Resolve `~` in the path-valued settings (`repo`, `worktrees_root`) so a
    config using `~/dev/...` binds to a real checkout, not a literal-tilde dir."""
    for key in ("repo", "worktrees_root"):
        val = str(entry.get(key) or "").strip()
        if val:
            entry[key] = os.path.expanduser(val)
    return entry


def _resolve_project_entry(name: str, settings) -> dict:
    """Validate + normalize ONE explicit `projects:` entry. `repo` is REQUIRED here
    (unlike the implicit project, which defaults it to `.` for back-compat): an entry
    with no repo has nowhere to build, so it fails loudly with a ValueError naming the
    offending project rather than silently binding to the server's cwd."""
    if not isinstance(settings, dict):
        raise ValueError(f"project {name!r} must be a mapping of settings, got {type(settings).__name__}")
    entry = _copy_settings(settings)
    if not str(entry.get("repo") or "").strip():
        raise ValueError(
            f"project {name!r} has no `repo` — every project entry must declare the repo it "
            "builds in (the board creates worktrees, branches and PRs there); add `repo:`."
        )
    entry = _expand_paths(entry)
    entry["name"] = name
    return entry


def _synthesize_implicit_project(name: str, cfg: dict) -> dict:
    """Synthesize the single implicit project from the flat top-level keys — the
    back-compat path when no `projects:` map is declared. `repo` DEFAULTS to `.` (the
    manifest default, today's single-repo behavior), so this never raises: a config
    that predates the map keeps working with zero migration."""
    entry = _copy_settings(cfg)
    entry["repo"] = str(entry.get("repo") or _DEFAULT_REPO).strip() or _DEFAULT_REPO
    entry = _expand_paths(entry)
    entry["name"] = name
    # Marks the entry as synthesized from the flat keys, so a message about where a repo
    # came from can name `project_board.repo` rather than a `projects:` key that the
    # operator never wrote (#454).
    entry[IMPLICIT_MARKER] = True
    return entry


def repo_config_key(name: str, projects: dict) -> str:
    """The config key a card's repo root is read from (#454): the project's own
    ``project_board.projects.<name>.repo`` when ``name`` is an explicit entry, else the flat
    ``project_board.repo`` (the implicit project, an unlabeled card, or an orphaned label
    that falls back to it)."""
    entry = (projects or {}).get(str(name or "").strip())
    if isinstance(entry, dict) and not entry.get(IMPLICIT_MARKER) and str(entry.get("repo") or "").strip():
        return f"project_board.projects.{name}.repo"
    return "project_board.repo"


def resolve_projects(cfg: dict) -> dict[str, dict]:
    """Parse the board's `projects:` config map → ``{name: settings}`` (#90).

    Each settings dict carries that project's full execution surface — `repo`,
    `base_branch`, `local_gate_cmd`, `coders`, every `coder_solve_*` knob, `gate_files`,
    `repo_conventions`, `worktrees_root`, `format_cmd`, `setup_cmd`, `env_passthrough`,
    `release_freeze`, `preflight_cmd`, `require_complete_review`, `review_summon_handle` — plus its own
    `name`. `repo` is required in every explicit entry and `~` paths are expanded; a
    missing `repo` raises ValueError (the board writes worktrees/branches/PRs, so it must
    never fall back to the server's cwd).

    BACK-COMPAT: when `projects:` is absent, a SINGLE implicit project is synthesized
    from the flat top-level keys (`repo` defaulting to `.`) and returned under its
    default name — so today's flat single-repo config behaves exactly as before, no
    migration. The implicit project's name is `default_project` when set, else
    ``IMPLICIT_PROJECT_NAME``."""
    cfg = dict(cfg or {})
    raw = cfg.get("projects")
    if isinstance(raw, dict) and raw:
        out = {str(name): _resolve_project_entry(str(name), settings) for name, settings in raw.items()}
        for entry in out.values():
            for key in _INHERITED_SETTING_KEYS:
                if key not in entry and key in cfg:
                    entry[key] = cfg[key]
        return out
    name = str(cfg.get("default_project") or "").strip() or IMPLICIT_PROJECT_NAME
    return {name: _synthesize_implicit_project(name, cfg)}


def default_project(cfg: dict) -> str:
    """The name of the default project features fall back to when none is named at
    create time: `default_project` from config when set, else the sole project (the
    single explicit entry, or the synthesized implicit one). Empty only when a
    multi-project map declares no `default_project`."""
    cfg = dict(cfg or {})
    named = str(cfg.get("default_project") or "").strip()
    if named:
        return named
    projects = resolve_projects(cfg)
    return next(iter(projects)) if len(projects) == 1 else ""


def multi_project(cfg: dict) -> bool:
    """True when the board's own ``projects:`` map EXPLICITLY declares more than one
    project — the shape whose cards must share ONE store (#260): pre-D3, per-repo
    ``.beads/`` discovery would have given every project its own workspace and
    fragmented the board (a card created for one project invisible to the loop and
    the others). The implicit single project (no map) is never multi."""
    raw = (cfg or {}).get("projects")
    return isinstance(raw, dict) and len(raw) > 1


def store_db_path(cfg: dict) -> str:
    """The beads db every board-store construction rides (D3, #260): the explicit
    ``db_path`` when set (passed through VERBATIM — the operator's hard pin, and the
    same raw value the loop's own store_kw carries, so both land on one cached board),
    else the ONE instance-default store (``store.default_db_path``). Resolved HERE — at
    the config seam — so ``store_kw`` carries the real path instead of deferring to
    ``get_store``'s fallback. An explicitly BLANK ``db_path`` resolves to the instance
    default too (production stores never do per-repo discovery since D3); on a
    multi-project board that inert override is additionally surfaced as a non-blocking
    setup advisory rather than silently ignored."""
    raw = (cfg or {}).get("db_path")
    if str(raw or "").strip():
        return str(raw)
    from .store import default_db_path  # lazy, matching the other cross-module reaches

    return default_db_path()


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


# ── cards whose project no longer resolves (#454) ─────────────────────────────────
# Moving a board from the legacy single-repo binding (flat `repo:`) to a `projects:` map
# leaves every existing card stamped `project:default` (the implicit project's name) — and
# `default` is no longer a key. Nothing said so: the loop quietly builds such a card in the
# DEFAULT project's repo, while the Ready gate checks its paths against the flat `repo`.
# These helpers name them, so status and board_list can flag them and offer the re-home.


def unresolved_project(feature: dict, projects: dict) -> str:
    """The card's ``project`` label when it names no project in ``projects`` (the resolved
    map), else ``""``. An UNLABELED card is not orphaned — it takes the default project."""
    name = str((feature or {}).get("project") or "").strip()
    return name if name and name not in (projects or {}) else ""


def rehome_hint(fid: str, label: str, projects: dict, default: str = "") -> str:
    """Operator/agent copy for re-homing one orphaned card."""
    known = sorted(projects or {})
    target = default if default in (projects or {}) else (known[0] if len(known) == 1 else "")
    listed = ", ".join(repr(n) for n in known) or "(none registered)"
    verb = (
        f"board_update_feature(feature_id={fid!r}, project={target!r})"
        if target
        else f"board_update_feature(feature_id={fid!r}, project=<one of {listed}>)"
    )
    return (
        f"project {label!r} is not in project_board.projects (known: {listed}) — re-home it with {verb} "
        "while it is backlog/ready with no branch or PR"
    )


def orphaned_cards(features, projects: dict, default: str = "") -> list[dict]:
    """The non-terminal cards in ``features`` whose project label doesn't resolve, each
    ``{id, title, state, project, hint}``."""
    out = []
    for f in features or ():
        label = unresolved_project(f, projects)
        state = str(f.get("board_state") or f.get("state") or "")
        if not label or state in ("done", "cancelled"):
            continue
        fid = str(f.get("id") or "")
        out.append(
            {
                "id": fid,
                "title": str(f.get("title") or ""),
                "state": state,
                "project": label,
                "hint": rehome_hint(fid, label, projects, default),
            }
        )
    return out


def orphan_note(label: str, projects: dict) -> str:
    """Why a card labeled ``label`` fell back to the flat repo (it isn't a project)."""
    known = ", ".join(repr(n) for n in sorted(projects or {})) or "(none)"
    return (
        f"this card's project {label!r} is not in project_board.projects (known: {known}) — "
        "re-home it with board_update_feature(project=...)"
    )


_GITHUB_REMOTE = re.compile(
    r"^(?:https?://(?:[^@/]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


def parse_github_remote(url: str) -> str:
    """``owner/repo`` for a GitHub remote URL (https, ssh or scp form), else ``""``."""
    m = _GITHUB_REMOTE.match(str(url or "").strip())
    return f"{m.group('owner')}/{m.group('repo')}" if m else ""
