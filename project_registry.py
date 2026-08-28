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
import copy
import logging
import os
import re
import signal
import subprocess
import sys
import types
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("protoagent.plugins.project_board")

#: Fields an operator writes by hand today, and the only ones this tool sets.
_ENTRY_FIELDS = ("repo", "base_branch", "local_gate_cmd", "repo_conventions")
_PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_LOCK_SLOT = "project_board.project_registry_lock"
_lock_holder = sys.modules.get(_LOCK_SLOT)
if _lock_holder is None:
    _lock_holder = types.ModuleType(_LOCK_SLOT)
    _lock_holder.lock = asyncio.Lock()
    sys.modules[_LOCK_SLOT] = _lock_holder
_MUTATION_LOCK = _lock_holder.lock


class ProjectRegistryError(ValueError):
    """An operator-actionable project registry refusal."""


class ProjectRegistryConflict(ProjectRegistryError):
    """A registry mutation refused because current board state makes it unsafe."""


def _live_section(*, required: bool = False) -> dict[str, Any]:
    """The live ``project_board`` section, read afresh for every operation.

    Host-free compatibility reads may use the empty fallback. Mutations and their
    readback pass ``required=True``: treating a failed/malformed live read as an
    empty registry would turn an additive update into a destructive replace-all.
    """
    try:
        from graph.sdk import config as host_config

        live = host_config()
        plugin_config = getattr(live, "plugin_config", None)
        section = plugin_config.get("project_board") if isinstance(plugin_config, dict) else None
        if section is None:
            section = getattr(live, "project_board", None)
    except Exception as exc:  # noqa: BLE001 — no host (tests, CLI)
        if required:
            raise ProjectRegistryError(f"could not read live project config: {exc}") from exc
        return {}
    if section is None:
        return {}
    if not isinstance(section, dict):
        if required:
            raise ProjectRegistryError("live project_board config is not a mapping — repair it before editing projects")
        return {}
    return dict(section)


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
    return _projects_from_section(_live_section())


def _projects_from_section(section: dict[str, Any], *, required: bool = False) -> dict[str, Any]:
    """Return the authored project map from one coherent live-section read."""
    projects = section.get("projects")
    if projects is None:
        return {}
    if not isinstance(projects, dict):
        if required:
            raise ProjectRegistryError("project_board.projects is not a mapping — repair it before editing projects")
        return {}
    return copy.deepcopy(projects)


def _effective_default(section: dict[str, Any], projects: dict[str, Any]) -> str:
    """Mirror runtime default resolution for the editor's explicit project map.

    The board treats a sole valid project as the default even when
    ``default_project`` is blank. Reporting only the authored scalar makes the UI
    lie and, worse, lets adding a second project silently erase that routing choice.
    """
    named = str(section.get("default_project") or "").strip()
    if named:
        return named
    if len(projects) == 1:
        name, entry = next(iter(projects.items()))
        if isinstance(entry, dict) and str(entry.get("repo") or "").strip():
            return str(name)
    return ""


def _public_entry(raw: Any) -> dict[str, Any]:
    """Editor-owned values plus names—not values—of preserved file-only fields."""
    valid = isinstance(raw, dict)
    entry = raw if valid else {}
    return {
        **{key: entry.get(key, "") for key in _ENTRY_FIELDS},
        "extra_fields": sorted(set(entry) - set(_ENTRY_FIELDS)),
        "editable": valid,
    }


def project_registry_snapshot() -> dict[str, Any]:
    """Public-safe live registry data for the authenticated console editor.

    Only the fields the editor owns are returned. Unknown/per-project advanced keys
    are named (so the operator knows they exist) but their values remain file-only;
    an editor save preserves them byte-for-byte.
    """
    section = _live_section(required=True)
    projects = _projects_from_section(section, required=True)
    rows = []
    for name in sorted(projects):
        rows.append({"name": name, **_public_entry(projects[name])})
    enabled, root = _host_onboarding()
    return {
        "projects": rows,
        "default_project": _effective_default(section, projects),
        "onboarding": {"enabled": enabled, "root": root},
    }


def _validate_name(name: str) -> str:
    project = str(name or "").strip()
    if not project:
        raise ProjectRegistryError("name is required — it is the key features carry")
    if not _PROJECT_NAME.fullmatch(project):
        raise ProjectRegistryError(
            "name must be at most 128 characters, start with a letter or number, "
            "and contain only letters, numbers, hyphens, or underscores"
        )
    return project


def _bounded_text(value: str, label: str, maximum: int, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ProjectRegistryError(f"{label} is required")
    if len(text) > maximum:
        raise ProjectRegistryError(f"{label} must be at most {maximum} characters")
    if "\0" in text:
        raise ProjectRegistryError(f"{label} cannot contain a NUL byte")
    return text


def _validate_base_branch(branch: str) -> str:
    """Return a Git-valid branch name before persisting a failure for dispatch time."""
    value = _bounded_text(branch or "main", "base branch", 255, required=True)
    try:
        checked = subprocess.run(
            ["git", "check-ref-format", "--branch", value],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectRegistryError(f"could not validate base branch {value!r}: {exc}") from exc
    if checked.returncode != 0:
        raise ProjectRegistryError(f"base branch {value!r} is not a valid Git branch name")
    return value


#: Registration-time gate smoke bounds (#261) — mirroring the loop's shipped defaults
#: (``local_gate_timeout_s`` / ``local_gate_output_chars``); the registry has no loop
#: cfg to read them from.
_SMOKE_TIMEOUT_S = 600.0
_SMOKE_OUTPUT_CHARS = 4000
#: The most of the gate's output kept IN MEMORY while it runs — a rolling tail, so a
#: gate that prints without end (a runaway test log, a progress bar) cannot grow the
#: plugin process with it. Sized for the worst-case UTF-8 width of the decoded tail
#: reported below, so the truncated text is never shorter than ``_SMOKE_OUTPUT_CHARS``.
_SMOKE_OUTPUT_BYTES = _SMOKE_OUTPUT_CHARS * 4
#: Bound on reaping a killed smoke. The group kill below takes the whole gate tree
#: down, but a descendant that re-``setsid``s escapes the group and can keep our
#: stdout pipe open — the PUT must answer anyway, not wait for it.
_SMOKE_REAP_TIMEOUT_S = 5.0


async def _base_checkout_dirt(repo: str, base: str) -> str:
    """``worktree.base_checkout_dirt`` behind the dual import this module needs (it is
    loaded both as a package submodule and as a top-level module in tests). Any failure
    returns '' — dirt may only ever DOWNGRADE a red verdict to indeterminate, so an
    unavailable check keeps the strict refusal rather than inventing a pass."""
    try:
        try:
            from . import worktree
        except ImportError:
            from project_board import worktree
        return await worktree.base_checkout_dirt(repo, base)
    except Exception:  # noqa: BLE001 — no dirt information → keep the strict verdict
        return ""


async def _drain_tail(stream, cap: int) -> bytes:
    """Read ``stream`` to EOF keeping only its last ``cap`` bytes.

    ``proc.communicate()`` buffers EVERYTHING the child writes before the caller can
    truncate it, so a gate with unbounded output was an unbounded allocation inside the
    server process. This reads in chunks and trims to a rolling window (at most
    ``2 * cap`` bytes resident), so memory is bounded by the cap, not by the gate."""
    buf = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(buf[-cap:])
        buf += chunk
        if len(buf) > 2 * cap:
            del buf[:-cap]


async def _run_bounded(proc: asyncio.subprocess.Process, cap: int) -> bytes:
    """Drain the gate's merged stdout into a bounded tail, then reap it."""
    out = await _drain_tail(proc.stdout, cap)
    await proc.wait()
    return out


def _kill_gate_tree(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the smoked gate's whole process group, not just the shell.

    The smoke launches the shell with ``start_new_session=True`` so it leads its own
    group (pgid == its pid). Killing only the shell leaves descendants alive holding
    the inherited stdout pipe — and project registration blocked until they exit on
    their own. The group kill takes the tree down together; the fallback covers a
    group that is already gone (or a platform without ``killpg``)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
        return
    except (AttributeError, OSError):
        pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


async def _smoke_gate_on_clean_base(name: str, cmd: str, repo: str, base: str, *, force: bool = False) -> None:
    """Smoke-run an incoming explicit gate ONCE on the repo's base checkout, before
    anything persists (#261).

    upsert used to validate only the command's LENGTH; the first execution was the
    loop's gate preflight, which discovers a broken gate long after the PUT answered
    ok — and answers by silently holding the project's ready work. The operator is
    present NOW, so a gate that fails on the clean base refuses the registration,
    naming the failure with the output tail. ``force`` downgrades the refusal to a
    loud warning (persist anyway; the loop's preflight still gates dispatch).

    Mirrors the preflight's posture on indeterminate verdicts: a timeout or a signal
    kill is NO verdict (allow — a slow gate must not make registration impossible),
    and a non-zero exit on a checkout that was ALREADY not at base (#255) convicts
    the operator's local edits rather than the base every worktree branches from, so
    it too downgrades to a loud warning. Only a checkout that was clean when the
    gate started, with a red gate, refuses.
    """
    log.info("[project_board] register[%s]: smoking the gate on clean base — %s", name, cmd)
    # Snapshot dirt BEFORE the gate runs. The gate itself may modify tracked files
    # (an in-place formatter, generated code) before exiting non-zero; a post-run
    # check would read that self-inflicted dirt as the operator's local edits and
    # launder a red verdict on the clean base into an indeterminate persist. Only
    # dirt that predates the gate may downgrade its verdict.
    dirt = await _base_checkout_dirt(repo, base)
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=repo,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # Own process group, so a timeout can kill the whole gate tree — not just
            # the shell while its descendants keep our stdout pipe (and the PUT) open.
            start_new_session=True,
        )
        try:
            # Bounded read (not `communicate()`, which buffers the whole stream before any
            # truncation could apply): only the tail stays resident while the gate runs.
            out = await asyncio.wait_for(_run_bounded(proc, _SMOKE_OUTPUT_BYTES), timeout=_SMOKE_TIMEOUT_S)
        except asyncio.TimeoutError:
            _kill_gate_tree(proc)
            try:
                # Reap the killed shell before answering the PUT — bounded, so a
                # descendant that escaped the group kill cannot block registration.
                await asyncio.wait_for(proc.wait(), timeout=_SMOKE_REAP_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.warning(
                    "[project_board] register[%s]: killed gate smoke did not reap within %ss — "
                    "abandoning it rather than blocking registration",
                    name,
                    _SMOKE_REAP_TIMEOUT_S,
                )
            log.warning(
                "[project_board] register[%s]: gate smoke timed out (%ss) — indeterminate, persisting "
                "(the loop's preflight still gates dispatch)",
                name,
                _SMOKE_TIMEOUT_S,
            )
            return
    except (OSError, subprocess.SubprocessError) as exc:
        if force:
            log.warning(
                "[project_board] register[%s]: gate command could not run (%s) — persisting under force; "
                "the loop's preflight will HOLD this project's work until it can",
                name,
                exc,
            )
            return
        raise ProjectRegistryError(f"gate command could not run: {exc}") from exc
    if proc.returncode == 0:
        return
    if proc.returncode is not None and proc.returncode < 0:
        # Killed by a signal (shutdown / external kill / OOM) — the gate never reached
        # a verdict, so it must not produce one. Same posture as the loop's gate runs.
        log.warning(
            "[project_board] register[%s]: gate smoke killed by signal %d — no verdict, persisting "
            "(the loop's preflight still gates dispatch)",
            name,
            -proc.returncode,
        )
        return
    text = (out or b"").decode("utf-8", "replace").strip()
    if len(text) > _SMOKE_OUTPUT_CHARS:
        text = "…(truncated)…\n" + text[-_SMOKE_OUTPUT_CHARS:]
    text = text or f"gate exited {proc.returncode} with no output"
    if force:
        log.warning(
            "[project_board] register[%s]: gate FAILED on the clean base (exit %d) — persisting under "
            "force; the loop's preflight will HOLD this project's work until it passes. Output tail:\n%s",
            name,
            proc.returncode,
            text,
        )
        return
    if dirt:
        log.warning(
            "[project_board] register[%s]: gate FAILED but the checkout at %s was NOT at base when the "
            "gate started (%s) — the gate ran against those local edits, not the base every worktree "
            "branches from, so the verdict is indeterminate; persisting (the loop's preflight still "
            "gates dispatch). Output tail:\n%s",
            name,
            repo,
            dirt,
            text,
        )
        return
    raise ProjectRegistryError(
        f"the gate failed on the clean base checkout (exit {proc.returncode}) — "
        f"fix the gate or the repo before registering it; output tail:\n{text}"
    )


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

    intended = copy.deepcopy(projects)
    section: dict[str, Any] = {"projects": copy.deepcopy(intended)}
    if default_project is not None:
        section["default_project"] = default_project
    ok, messages = await asyncio.to_thread(HOST.apply_settings, {"project_board": section})
    if not ok:
        raise ProjectRegistryError("; ".join(messages) or "the host refused the config update")

    persisted_section = _live_section(required=True)
    persisted = _projects_from_section(persisted_section, required=True)
    missing = sorted(set(intended) - set(persisted))
    unexpected = sorted((absent or set()) & set(persisted))
    changed_entries = sorted(name for name, entry in intended.items() if name in persisted and persisted[name] != entry)
    mismatched = []
    for name, fields in (expected or {}).items():
        landed = persisted.get(name)
        for key, value in fields.items():
            if not isinstance(landed, dict) or landed.get(key) != value:
                mismatched.append(f"{name}.{key}")
    if missing or unexpected or changed_entries or mismatched:
        detail = []
        if missing:
            detail.append("missing project(s): " + ", ".join(missing))
        if unexpected:
            detail.append("deleted project(s) still present: " + ", ".join(unexpected))
        if changed_entries:
            detail.append("project entries changed during persistence: " + ", ".join(changed_entries))
        if mismatched:
            detail.append("fields did not persist: " + ", ".join(mismatched))
        raise ProjectRegistryError(
            "the host reported success, but live config readback failed ("
            + "; ".join(detail)
            + "); no success was assumed"
        )
    if default_project is not None and str(persisted_section.get("default_project") or "") != default_project:
        raise ProjectRegistryError("the host reported success, but the default project did not persist")


async def upsert_project(
    name: str,
    repo: str,
    *,
    base_branch: str = "main",
    local_gate_cmd: str = "",
    repo_conventions: str = "",
    make_default: bool = False,
    clear_default: bool = False,
    replace_optional: bool = False,
    force_gate: bool = False,
) -> dict[str, Any]:
    """Add/update one project while preserving siblings and unowned entry fields.

    An incoming EXPLICIT gate command is smoke-run once on the repo's base checkout
    before anything persists (#261) — see ``_smoke_gate_on_clean_base``. ``force_gate``
    downgrades a red smoke to a loud warning."""
    project = _validate_name(name)
    repo = _bounded_text(repo, "repo", 4096, required=True)
    if make_default and clear_default:
        raise ProjectRegistryError("default action is ambiguous — set and clear cannot both be requested")
    branch = await asyncio.to_thread(_validate_base_branch, base_branch)

    async with _MUTATION_LOCK:
        # Consent and its root are live policy, so validate them after acquiring the
        # mutation lock. A request queued behind another config write must not retain
        # stale permission after onboarding is disabled or its root changes.
        enabled, root = _host_onboarding()
        if not enabled:
            raise ProjectRegistryError(
                "project onboarding is off — enable Settings ▸ Project onboarding before changing boarded repos"
            )
        repo_p, err = _resolve_under(root, repo)
        if err:
            raise ProjectRegistryError(err)
        section = _live_section(required=True)
        existing = _projects_from_section(section, required=True)
        prior = existing.get(project)
        if project in existing and not isinstance(prior, dict):
            raise ProjectRegistryError(
                f"project {project!r} is not a mapping — repair it in YAML or delete it before replacing it"
            )
        entry = dict(prior) if isinstance(prior, dict) else {}
        entry.update({"repo": str(repo_p), "base_branch": branch})
        optional = {
            "local_gate_cmd": _bounded_text(local_gate_cmd, "local gate command", 8192),
            "repo_conventions": _bounded_text(repo_conventions, "repository conventions", 32768),
        }
        for key, value in optional.items():
            if value:
                entry[key] = value
            elif replace_optional:
                entry.pop(key, None)
        # Smoke only the gate text THIS call carries: blank keeps/clears (nothing new
        # to prove), and the "auto" sentinel is resolved by the loop at dispatch time,
        # not a command — shelling the literal word would manufacture a failure. A
        # preserved prior gate was proven when it was set (or is the loop preflight's
        # job for YAML-authored ones); re-running it here would let a red suite refuse
        # an unrelated conventions update.
        gate_cmd = optional["local_gate_cmd"]
        if gate_cmd and gate_cmd != _AGENT_GATE_SENTINEL:
            await _smoke_gate_on_clean_base(project, gate_cmd, str(repo_p), branch, force=force_gate)
        merged = dict(existing)
        merged[project] = entry
        if not set(existing) <= set(merged):
            raise ProjectRegistryError("internal safety check failed — the update would drop a sibling project")
        current_default = _effective_default(section, existing)
        if make_default:
            default = project
        elif clear_default and current_default == project:
            if len(merged) == 1:
                raise ProjectRegistryError(
                    f"cannot clear project {project!r} as the default while it is the only project"
                )
            default = ""
        else:
            # Preserve an implicit sole-project default when this mutation makes the
            # registry multi-project. For the first project, adopt the runtime's
            # automatic sole default and report/persist it truthfully.
            default = current_default or _effective_default({}, merged)
        await _apply_registry(merged, expected={project: entry}, default_project=default)
        return {
            "project": project,
            # Do not leak preserved file-only values through the PUT response after
            # deliberately redacting them from GET /projects.
            "entry": _public_entry(entry),
            "created": project not in existing,
            "default_project": default,
        }


async def delete_project(
    name: str,
    *,
    assert_unused: Callable[[str, str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Delete a project after a caller-supplied live safety check, under the lock."""
    project = _validate_name(name)
    async with _MUTATION_LOCK:
        section = _live_section(required=True)
        existing = _projects_from_section(section, required=True)
        if project not in existing:
            raise ProjectRegistryError(f"unknown project {project!r}")
        current_default = _effective_default(section, existing)
        # The board store and YAML are separate resources, so they cannot share a
        # transaction. Running the fresh board check inside the registry mutation
        # lock at least closes the UI/tool update race before apply_settings.
        if assert_unused is not None:
            await assert_unused(project, current_default)
        merged = dict(existing)
        merged.pop(project)
        default = (next(iter(merged)) if len(merged) == 1 else "") if current_default == project else current_default
        await _apply_registry(merged, absent={project}, default_project=default)
        return {
            "project": project,
            "deleted": True,
            "default_project": default,
        }


#: The only non-blank gate value the agent tool accepts: the discovery sentinel the
#: loop resolves against the repo's OWN declared target. The persisted value is later
#: executed at dispatch time, so agent input must never carry command text — explicit
#: commands are operator configuration (the bearer-gated Projects editor / YAML).
_AGENT_GATE_SENTINEL = "auto"


def _validate_agent_gate(gate: str) -> str:
    """Return ``""`` or the literal ``"auto"`` — the only gate values agent input
    may carry into the registry."""
    value = str(gate or "").strip()
    if value in ("", _AGENT_GATE_SENTINEL):
        return value
    raise ProjectRegistryError(
        'gate accepts only the literal "auto" (discover the gate from the repo\'s own '
        "declared target) — an explicit gate command is operator configuration "
        "(Settings ▸ Projects), not agent input"
    )


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
        gate: str = "",
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
            gate: "" (keep/inherit the configured gate) or the literal "auto" to have
                the loop discover the pre-PR gate from the repo's own declared target
                (a gate/ci/check/verify script or Makefile/justfile target). This tool
                takes no gate command text — explicit commands are operator
                configuration (Settings ▸ Projects), not agent input.
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
                local_gate_cmd=_validate_agent_gate(gate),
                repo_conventions=repo_conventions,
            )
        except ProjectRegistryError as exc:
            return f"Error: {exc}."
        project, entry = result["project"], result["entry"]
        verb = "Registered" if result["created"] else "Updated"
        raw_gate = str(entry.get("local_gate_cmd") or "")
        gate_note = (
            "an auto-discovered gate"
            if raw_gate == _AGENT_GATE_SENTINEL
            else ("its own gate" if raw_gate else "the default gate")
        )
        conv = "with conventions" if entry.get("repo_conventions") else "WITHOUT conventions"
        note = (
            ""
            if entry.get("repo_conventions")
            else " — add repo_conventions before dispatching, or the coder will guess this repo's rules"
        )
        return (
            f"{verb} board project '{project}' → {entry['repo']} (base {entry['base_branch']}, "
            f"{gate_note}, {conv}). The running board applied it live; no restart is required.{note}"
        )

    return board_register_project
