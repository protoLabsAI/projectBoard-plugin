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
import contextlib
import copy
import logging
import os
import re
import signal
import subprocess
import sys
import types
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
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
# One gate smoke per CHECKOUT at a time (#393). The smoke runs outside the registry lock,
# so saves to different projects don't wait on each other's gate. But two gates running in
# one working tree trample each other's caches and build output, as the old global lock
# prevented. Keyed by resolved repo path, in the same process-stable slot as the lock.
_SMOKE_LOCKS: dict[str, asyncio.Lock] = _lock_holder.__dict__.setdefault("smoke_locks", {})
# The latest save per project and how it ended: {id, state, stage, started_at,
# finished_at, detail} (#393). A save that changes the gate answers only once the gate has
# run, which takes minutes for a full suite. An intermediary may give up first: the fleet
# proxy answers 504 after 20s on a plugin API call. So the outcome also lands here, where
# GET /projects serves it and the editor polls for it by the request id it sent.
_SAVES: dict[str, dict[str, Any]] = _lock_holder.__dict__.setdefault("saves", {})
_APPLYING = "applying the change"


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
        # How each project's latest save went. This is what the editor polls when an
        # intermediary gave up on a long gate-changing save before it answered (#393).
        "saves": {name: dict(record) for name, record in _SAVES.items()},
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
            stdin=asyncio.subprocess.DEVNULL,  # #423: never the server's stdin
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
        except asyncio.CancelledError:
            # A cancelled PUT (client gone, shutdown) must not leave the gate running in
            # the operator's base checkout (#423) — the timeout path above always killed
            # the tree; a cancel left it untouched.
            _kill_gate_tree(proc)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.shield(asyncio.wait_for(proc.wait(), timeout=_SMOKE_REAP_TIMEOUT_S))
            raise
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
        # Log the OUTCOME, not just the start (#393). A PUT whose client has timed out gets
        # no access-log line from uvicorn, so this is the only record of what the call did.
        # A green run on a checkout that was NOT at base proves nothing about the base,
        # exactly as the preflight treats it (#300): an operator's local fix can make a
        # broken base pass. It persists either way, since there is no verdict to refuse
        # on, but the log says which it was.
        if dirt:
            log.warning(
                "[project_board] register[%s]: gate passed, but the checkout at %s was NOT at base when it "
                "started (%s) — that is no verdict on the base; persisting (the loop's preflight still gates "
                "dispatch)",
                name,
                repo,
                dirt,
            )
        else:
            log.info("[project_board] register[%s]: gate smoke passed on the clean base", name)
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


def _gate_already_proven(prior: Any, gate_cmd: str, repo: Path) -> bool:
    """Whether ``prior`` already carries ``gate_cmd`` in the same checkout, so a save that
    keeps it has nothing new for the smoke to prove (#393).

    The Projects editor sends EVERY field back on save, so the configured gate always came
    back as "gate text this call carries" and was smoked again. For protoAgent that is ruff +
    lint-imports + the whole pytest suite, synchronously, under the registry lock: a
    conventions-only save took minutes, outlived its client, and a red suite refused an edit
    that had nothing to do with it. A gate is proven against a checkout, so the smoke re-runs
    only when the command or the repo changes.

    NOT the base branch. The smoke runs in the operator's checkout, which a base-branch edit
    does not switch. It would run the old branch's code, read as not-at-base, and yield no
    verdict, only a minutes-long wait. A base change resets the loop's preflight instead, and
    that re-smokes the gate against the new base before any work dispatches. Anything
    unreadable reads as changed (smoke it), never as proven."""
    if not isinstance(prior, dict):
        return False
    prior_repo = str(prior.get("repo") or "").strip()
    if not prior_repo or str(prior.get("local_gate_cmd") or "").strip() != gate_cmd:
        return False
    try:
        return Path(prior_repo).expanduser().resolve() == repo
    except OSError:
        return False


def _explicit_gate(entry: dict[str, Any]) -> str:
    """The gate command ``entry`` will RUN, or ``""``. Blank means inherit, and the ``auto``
    sentinel is resolved by the loop at dispatch time; neither is a command to smoke."""
    gate = str(entry.get("local_gate_cmd") or "").strip()
    return "" if gate in ("", _AGENT_GATE_SENTINEL) else gate


def _consented_repo(repo: str) -> Path:
    """``repo`` resolved and proven to sit inside the operator's onboarding space, read LIVE:
    the operator can switch the space off, or move its root, between two calls."""
    enabled, root = _host_onboarding()
    if not enabled:
        raise ProjectRegistryError(
            "project onboarding is off — enable Settings ▸ Project onboarding before changing boarded repos"
        )
    repo_p, err = _resolve_under(root, repo)
    if err:
        raise ProjectRegistryError(err)
    return repo_p


def _prior_entry(existing: dict[str, Any], project: str) -> Any:
    prior = existing.get(project)
    if project in existing and not isinstance(prior, dict):
        raise ProjectRegistryError(
            f"project {project!r} is not a mapping — repair it in YAML or delete it before replacing it"
        )
    return prior


def _merge_entry(
    prior: Any, repo: Path, branch: str, optional: dict[str, str], replace_optional: bool
) -> dict[str, Any]:
    """The entry this save would persist: ``prior`` (file-only fields preserved) with the
    editor-owned fields applied."""
    entry = dict(prior) if isinstance(prior, dict) else {}
    entry.update({"repo": str(repo), "base_branch": branch})
    for key, value in optional.items():
        if value:
            entry[key] = value
        elif replace_optional:
            entry.pop(key, None)
    return entry


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── the managed-project half (#452) ─────────────────────────────────────────────
# A board project is a repo the board WRITES to; the host's ADR 0095 `projects:` registry
# is what the agent's own filesystem tools (list_projects / read_file / grep) READ through.
# Registering only the board half left the lead agent unable to read the very checkout its
# coders branch from — it went to the GitHub API instead, which reads the default branch,
# not the clone. So a register also upserts a READ-ONLY registry entry, shaped exactly like
# `onboard_project`'s (name / path / github / default_branch / write), through the same
# `HOST.apply_settings` seam, in the SAME patch as the board entry (one reload, one
# outcome).
#
# Ownership is recorded on the BOARD entry (`managed_project: <registry name>`), never on
# the host's entry, so the host schema carries nothing it doesn't know. The board touches a
# registry entry only while it owns it: an entry that was already there (onboard_project,
# the operator) is left alone and never removed, and one the operator has since re-pointed
# is handed back rather than overwritten. Unregistering removes the entry only if the board
# added it and it still points where the board put it.
#
# Deliberately NOT mirrored from onboard_project: flipping `filesystem.enabled` on. An
# operator who switched the filesystem tools off did so on purpose, and a board register is
# no reason to override that; the reply says so instead.
_MANAGED_MARKER = "managed_project"
_MANAGED_FENCE_MARKER = "managed_fence"


def _live_host() -> Any:
    """The live host config object, or ``None`` host-free."""
    try:
        from graph.sdk import config as host_config

        return host_config()
    except Exception:  # noqa: BLE001 — no host (tests, CLI)
        return None


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [copy.deepcopy(e) for e in (value or []) if isinstance(e, dict)] if isinstance(value, list) else []


def _same_path(a: Any, b: Any) -> bool:
    """True when two config paths name the same location (resolved, ``~`` expanded)."""
    if not str(a or "").strip() or not str(b or "").strip():
        return False
    try:
        return Path(str(a)).expanduser().resolve() == Path(str(b)).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return str(a) == str(b)


async def _origin_github(repo: Path) -> str:
    """``owner/repo`` of the checkout's GitHub ``origin``, or ``""`` (see
    ``worktree.origin_github_slug``). Dual import, like ``_base_checkout_dirt``."""
    try:
        try:
            from . import worktree
        except ImportError:
            from project_board import worktree
        return await worktree.origin_github_slug(str(repo))
    except Exception:  # noqa: BLE001 — no origin information → omit `github`
        return ""


def _falsey(value: Any) -> bool:
    """The host's read of ``write`` (ADR 0095 ``_falsey``): a string ``"false"`` is false."""
    if isinstance(value, str):
        return value.strip().lower() in ("", "0", "false", "no", "off")
    return not value


def _no_markers() -> dict[str, Any]:
    return {_MANAGED_MARKER: None, _MANAGED_FENCE_MARKER: None}


def _set_markers(entry: dict[str, Any], markers: dict[str, Any]) -> None:
    for key, value in markers.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value


def _plan_managed_upsert(
    live: Any, board: dict[str, Any], project: str, repo: Path, branch: str, github: str, prior_repo: Any = None
) -> dict[str, Any]:
    """The managed-project half of registering ``project`` → ``repo``, planned from ``live``
    — the config the host hands the patch callable INSIDE its write lock (#452).

    Returns the ``_apply_registry`` plan outcome: ``host_updates`` (the complete top-level
    ``projects`` list, and ``filesystem.projects`` when an explicit fence override is in
    force; ``{}`` when nothing changes), ``projects`` (``board`` with this entry's ownership
    markers set), ``managed`` (``{action, name, detail}``) and the readback's
    ``present`` / ``absent``."""
    board = copy.deepcopy(board)
    entry = board[project]
    out: dict[str, Any] = {"host_updates": {}, "projects": board, "present": None, "absent": None}

    def done(action: str, name: str, detail: str = "", markers: dict[str, Any] | None = None) -> dict[str, Any]:
        _set_markers(entry, markers if markers is not None else _no_markers())
        out["managed"] = {"action": action, "name": name, "detail": detail}
        return out

    registry_raw = getattr(live, "projects", None) if live is not None else None
    if registry_raw is None:
        return done("unsupported", "", "this host has no managed-projects registry (ADR 0095, host 0.115.0+)", {})
    registry = _dict_list(registry_raw)
    fence = _dict_list(getattr(live, "filesystem_projects", None))
    # The ownership recorded by the LAST save: `entry` still carries its markers, and
    # `prior_repo` is the path the board's managed entry was written with.
    owned_name = str(entry.get(_MANAGED_MARKER) or "").strip()
    fence_owned = bool(entry.get(_MANAGED_FENCE_MARKER))
    owned_idx = next(
        (
            i
            for i, e in enumerate(registry)
            if owned_name and str(e.get("name") or "") == owned_name and _same_path(e.get("path"), prior_repo)
        ),
        None,
    )
    desired = {"path": str(repo), "default_branch": branch}
    if github:
        desired["github"] = github

    def _drop_owned_fence(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            f for f in entries if not (str(f.get("name") or "") == owned_name and _same_path(f.get("path"), prior_repo))
        ]

    if owned_idx is not None:
        # The board added this entry and it still points where the board put it: keep it
        # in step with the board entry (a moved repo, a new base branch). The operator's
        # own edits to other keys (e.g. `write`) are preserved.
        current = registry[owned_idx]
        changed = {k: v for k, v in desired.items() if current.get(k) != v}
        markers = {_MANAGED_MARKER: owned_name, _MANAGED_FENCE_MARKER: True if fence_owned else None}
        if not changed:
            return done("unchanged", owned_name, "", markers)
        other = next(
            (e for i, e in enumerate(registry) if i != owned_idx and _same_path(e.get("path"), repo)),
            None,
        ) or next((f for f in fence if _same_path(f.get("path"), repo) and not fence_owned), None)
        if other is not None:
            # Moved onto a checkout that is ALREADY someone else's managed project: that one
            # covers the new path, so the board's own entry (for the old path) goes and the
            # board stops owning anything — never two names for one checkout.
            del registry[owned_idx]
            out["host_updates"]["projects"] = registry
            out["absent"] = (owned_name, str(prior_repo or ""))
            if fence_owned:
                out["host_updates"]["filesystem"] = {"projects": _drop_owned_fence(fence)}
            return done("present", str(other.get("name") or ""))
        current.update(changed)
        out["host_updates"]["projects"] = registry
        out["present"] = (owned_name, str(repo))
        # a moved path drops the old (name, path) pair from the superset check
        if not _same_path(prior_repo, repo):
            out["absent"] = None
            out["moved_from"] = (owned_name, str(prior_repo or ""))
        if fence_owned:
            for f in fence:
                if str(f.get("name") or "") == owned_name and _same_path(f.get("path"), prior_repo):
                    f["path"] = str(repo)
                    if github:
                        f["github"] = github
            out["host_updates"]["filesystem"] = {"projects": fence}
        return done("updated", owned_name, ", ".join(sorted(changed)), markers)

    # Already reachable by path — through the registry (onboard_project, the operator) or an
    # explicit fence entry (#452 review m6). Not the board's to change, or to remove later.
    present = next((e for e in registry if _same_path(e.get("path"), repo)), None) or next(
        (f for f in fence if _same_path(f.get("path"), repo)), None
    )
    if present is not None:
        return done("present", str(present.get("name") or ""))
    clash = next((e for e in registry if str(e.get("name") or "") == project), None)
    if clash is not None:
        return done("skipped", project, f"a managed project named {project!r} already points at {clash.get('path')!r}")
    out["host_updates"]["projects"] = registry + [{"name": project, **desired, "write": False}]
    out["present"] = (project, str(repo))
    markers = {_MANAGED_MARKER: project, _MANAGED_FENCE_MARKER: None}
    if fence:
        # An explicit `filesystem.projects` override shadows the registry in the fence
        # (ADR 0095 D2: explicit wins), so without this mirror the entry would be
        # registered yet unreachable by the fs tools — onboard_project does the same.
        fence_entry = {"name": project, "path": str(repo), "write": False}
        if github:
            fence_entry["github"] = github
        out["host_updates"]["filesystem"] = {"projects": fence + [fence_entry]}
        markers[_MANAGED_FENCE_MARKER] = True
    detail = ""
    if getattr(live, "filesystem_enabled", True) is False:
        detail = (
            "the filesystem tools are switched off on this host, so they can't read it until filesystem.enabled is on"
        )
    return done("added", project, detail, markers)


def _plan_managed_removal(live: Any, board: dict[str, Any], prior: Any) -> dict[str, Any]:
    """The managed-project half of unregistering a board project whose entry was ``prior``,
    planned from ``live`` inside the host's write lock. ``board`` is the SURVIVING board map.

    The entry goes only when the board added it (the marker), it still points at the
    board's repo, it is still read-only (#452 review m3), and no other board project still
    uses that checkout (m4). In that last case ownership MOVES to the sibling instead, so a
    later unregister of the sibling can still clean up."""
    board = copy.deepcopy(board)
    prior = prior if isinstance(prior, dict) else {}
    out: dict[str, Any] = {"host_updates": {}, "projects": board, "present": None, "absent": None}
    owned_name = str(prior.get(_MANAGED_MARKER) or "").strip()

    def done(action: str, detail: str = "") -> dict[str, Any]:
        out["managed"] = {"action": action, "name": owned_name, "detail": detail}
        return out

    if not owned_name:
        return done("none")
    registry_raw = getattr(live, "projects", None) if live is not None else None
    if registry_raw is None:
        return done("none", "no managed-projects registry on this host")
    registry = _dict_list(registry_raw)
    repo = prior.get("repo")
    owned = next(
        (e for e in registry if str(e.get("name") or "") == owned_name and _same_path(e.get("path"), repo)),
        None,
    )
    if owned is None:
        return done("kept", "the managed project was changed or removed by someone else, so it was left alone")
    if not _falsey(owned.get("write")):
        return done("kept", "the managed project was made writable since the board added it, so it was left alone")
    heir = next(
        (n for n, e in sorted(board.items()) if isinstance(e, dict) and _same_path(e.get("repo"), repo)),
        None,
    )
    if heir is not None:
        board[heir][_MANAGED_MARKER] = owned_name
        if prior.get(_MANAGED_FENCE_MARKER):
            board[heir][_MANAGED_FENCE_MARKER] = True
        return done("kept", f"board project {heir!r} still uses that checkout and now owns the managed project")
    out["host_updates"]["projects"] = [e for e in registry if e is not owned]
    out["absent"] = (owned_name, str(repo or ""))
    if prior.get(_MANAGED_FENCE_MARKER):
        fence = _dict_list(getattr(live, "filesystem_projects", None))
        kept = [f for f in fence if not (str(f.get("name") or "") == owned_name and _same_path(f.get("path"), repo))]
        if len(kept) != len(fence):
            out["host_updates"]["filesystem"] = {"projects": kept}
    return done("removed")


def _assert_host_superset(live: Any, host_updates: dict[str, Any], dropped: Any = None) -> None:
    """Refuse a host write that would drop an entry it doesn't mean to (#452 review B1).

    Every ``(name, path)`` in the live ``projects:`` registry and the explicit
    ``filesystem.projects`` fence must survive the write, apart from ``dropped`` — the one
    entry a removal (or a move) takes out. Raised inside the host's patch callable, the
    host answers ``(False, ["config update: …"])`` and nothing is written."""
    allowed = {tuple(dropped)} if dropped else set()
    for label, key, before in (
        ("projects", "projects", getattr(live, "projects", None)),
        ("filesystem.projects", "filesystem", getattr(live, "filesystem_projects", None)),
    ):
        after = host_updates.get(key)
        if key == "filesystem":
            after = (after or {}).get("projects") if isinstance(after, dict) else None
        if after is None:
            continue
        for e in _dict_list(before):
            pair = (str(e.get("name") or ""), str(e.get("path") or ""))
            if any(pair[0] == a[0] and _same_path(pair[1], a[1]) for a in allowed):
                continue
            if not any(_same_path(e.get("path"), x.get("path")) for x in _dict_list(after)):
                raise ProjectRegistryError(
                    f"refusing to write {label}: it would drop {pair[0] or '?'!r} ({pair[1]}) — "
                    "another writer changed it; nothing was saved, save again"
                )


def managed_note(managed: dict[str, Any]) -> str:
    """One sentence for the register reply on what happened to the managed-project half."""
    action, name, detail = managed.get("action"), managed.get("name") or "", managed.get("detail") or ""
    tail = f" ({detail})" if detail else ""
    if action == "added":
        return (
            f" Also registered it as read-only managed project '{name}', so your filesystem tools "
            f"(list_projects, read_file) can read the checkout{tail}."
        )
    if action == "updated":
        return f" Updated the managed project '{name}' the board registered ({detail})."
    if action == "present":
        return f" It is already the managed project '{name}', which was left as it is."
    if action == "skipped":
        return f" NOT registered as a managed project: {detail} — the filesystem tools can't read it by that name."
    if action == "unsupported":
        return f" Not registered as a managed project: {detail}."
    return ""


def _verify_managed(present: tuple[str, str] | None = None, absent: tuple[str, str] | None = None) -> list[str]:
    """Readback of the managed-project half, by ``(name, path)`` — the host may normalise
    the rest of an entry. ``present`` must be live after the apply, ``absent`` must not.
    Returns the mismatches (empty = landed)."""
    if present is None and absent is None:
        return []
    live = _live_host()
    landed = _dict_list(getattr(live, "projects", None) if live is not None else None)

    def _has(name: str, path: str) -> bool:
        return any(str(e.get("name") or "") == name and _same_path(e.get("path"), path) for e in landed)

    problems = []
    if present is not None and not _has(*present):
        problems.append(f"managed project {present[0]!r} is not live")
    if absent is not None and _has(*absent):
        problems.append(f"managed project {absent[0]!r} is still live")
    return problems


async def _apply_registry(
    projects: dict[str, Any],
    *,
    expected: dict[str, dict[str, Any]] | None = None,
    absent: set[str] | None = None,
    default_project: str | None = None,
    plan: Callable[[Any, dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply a complete registry and prove the intended live state landed.

    The patch goes to ``HOST.apply_settings`` as a CALLABLE (host 0.164.0+, #2743): the
    host runs it INSIDE its config write lock against the config every earlier writer has
    committed, so a read-merge-write computed there cannot lose a concurrent writer's
    change. That matters for the managed-project half (#452): the host's top-level
    ``projects:`` list is replaced WHOLESALE, so a list merged from a config read outside
    the lock would silently drop an entry ``onboard_project`` was still writing.

    ``plan(live, projects)`` (#452) computes the managed-project half from that locked
    config and returns ``{host_updates, projects, managed, present, absent}``:
    ``host_updates`` is the top-level ``projects`` list (and ``filesystem.projects`` for an
    explicit fence override) to send in the same patch; ``projects`` the board map with its
    ownership markers applied; ``present`` / ``absent`` the ``(name, path)`` the readback
    must find / not find. Every host list it writes must be a SUPERSET of the live one,
    apart from the one entry it removes (``_assert_host_superset``). Returns what the plan
    decided (``{}`` without one)."""
    from graph.plugins.host import HOST

    if HOST.apply_settings is None:
        raise ProjectRegistryError("project changes are unavailable — the host is not wired for config apply")

    decided: dict[str, Any] = {}

    def compute(live: Any) -> dict[str, Any]:
        outcome = plan(live, copy.deepcopy(projects)) if plan is not None else {}
        board = outcome.get("projects", projects)
        host_updates = copy.deepcopy(outcome.get("host_updates") or {})
        _assert_host_superset(live, host_updates, outcome.get("absent") or outcome.get("moved_from"))
        decided.clear()
        decided.update(outcome, projects=copy.deepcopy(board))
        # A removed project needs an explicit `None` — the host's `apply_updates_to_yaml`
        # MERGES a section-member map (siblings under `projects:` are kept by design, so a
        # concurrent writer's entry is never dropped), and only a `None` value removes a
        # key. Sending the surviving map alone therefore deletes nothing (#408).
        written: dict[str, Any] = copy.deepcopy(board)
        for gone in absent or set():
            written[gone] = None
        section: dict[str, Any] = {"projects": written}
        if default_project is not None:
            section["default_project"] = default_project
        return {**host_updates, "project_board": section}

    ok, messages = await asyncio.to_thread(HOST.apply_settings, compute)
    if not ok:
        raise ProjectRegistryError("; ".join(messages) or "the host refused the config update")
    intended = copy.deepcopy(decided.get("projects", projects))
    if plan is not None:
        expected = {name: intended[name] for name in (expected or {}) if name in intended}

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
    managed_problems = _verify_managed(decided.get("present"), decided.get("absent"))
    if missing or unexpected or changed_entries or mismatched or managed_problems:
        detail = list(managed_problems)
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
    return decided


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
    request_id: str = "",
) -> dict[str, Any]:
    """Add/update one project while preserving siblings and unowned entry fields.

    The gate the entry will RUN is smoke-run once on the repo's base checkout before
    anything persists (#261, ``_smoke_gate_on_clean_base``) when this save changes it: new
    command text, or the same command moved to another repo. A gate the entry already
    carried in that repo is not re-run (#393, ``_gate_already_proven``). ``force_gate``
    downgrades a red smoke to a loud warning.

    The smoke runs OUTSIDE the registry lock (#393), so a save to another project never
    waits minutes behind this one's gate. The lock covers only the read-merge-write, and
    under it the project's entry is re-read. If it changed while the smoke ran, or while
    this save waited, in a way that leaves the gate unproven, the save is refused with
    ``ProjectRegistryConflict`` (409) to be retried, never applied over the change.
    ``request_id`` (the editor's own) keys this save's outcome in ``_SAVES``."""
    project = _validate_name(name)
    repo = _bounded_text(repo, "repo", 4096, required=True)
    if make_default and clear_default:
        raise ProjectRegistryError("default action is ambiguous — set and clear cannot both be requested")
    optional = {
        "local_gate_cmd": _bounded_text(local_gate_cmd, "local gate command", 8192),
        "repo_conventions": _bounded_text(repo_conventions, "repository conventions", 32768),
    }
    branch = await asyncio.to_thread(_validate_base_branch, base_branch)
    status = {
        "id": str(request_id or "")[:64],
        "state": "running",
        "stage": "reading the registry",
        "started_at": _now_iso(),
        "finished_at": None,
        "detail": "",
    }
    _SAVES[project] = status
    try:
        result = await _upsert(
            project,
            repo,
            branch,
            optional,
            status,
            make_default=make_default,
            clear_default=clear_default,
            replace_optional=replace_optional,
            force_gate=force_gate,
        )
    except asyncio.CancelledError:
        detail = (
            "cancelled while the change was being applied — it may have landed; reload the project list"
            if status["stage"] == _APPLYING
            else f"cancelled while {status['stage']} — nothing was saved"
        )
        status.update(state="cancelled", finished_at=_now_iso(), detail=detail)
        log.warning("[project_board] register[%s]: %s (client gone, or shutdown)", project, detail)
        raise
    except ProjectRegistryError as exc:
        # Every refusal is logged here, once, with its full reason. The client may be gone
        # (a timed-out curl, a proxy that gave up), and then this line and `_SAVES` are all
        # that is left of the call.
        status.update(state="refused", finished_at=_now_iso(), detail=str(exc))
        log.warning("[project_board] register[%s]: not saved — %s", project, exc)
        raise
    status.update(state="saved", stage="", finished_at=_now_iso())
    return result


async def _upsert(
    project: str,
    repo: str,
    branch: str,
    optional: dict[str, str],
    status: dict[str, Any],
    *,
    make_default: bool,
    clear_default: bool,
    replace_optional: bool,
    force_gate: bool,
) -> dict[str, Any]:
    """``upsert_project``'s two phases: prove the gate, then the locked read-merge-write."""
    # Phase 1, NO lock: decide whether the gate this save leaves needs proving, and prove it.
    # Consent is checked first, because the smoke executes a command in that checkout.
    repo_p = _consented_repo(repo)
    existing = _projects_from_section(_live_section(required=True), required=True)
    basis = copy.deepcopy(_prior_entry(existing, project))
    gate = _explicit_gate(_merge_entry(basis, repo_p, branch, optional, replace_optional))
    smoked = False
    if gate and not _gate_already_proven(basis, gate, repo_p):
        smoke_lock = _SMOKE_LOCKS.setdefault(str(repo_p), asyncio.Lock())
        if smoke_lock.locked():
            status["stage"] = f"waiting for another gate smoke in {repo_p}"
            log.info("[project_board] register[%s]: %s", project, status["stage"])
        async with smoke_lock:
            status["stage"] = "running the gate on the clean base"
            await _smoke_gate_on_clean_base(project, gate, str(repo_p), branch, force=force_gate)
        smoked = True
    # The managed-project half's `github` (#452): a local `git remote` read, so outside the
    # lock like the smoke.
    github = await _origin_github(repo_p)

    # Phase 2, under the lock: the read-merge-write, on a FRESH read.
    if _MUTATION_LOCK.locked():
        # Say so before waiting (#393): a change queued behind another used to wait in
        # silence, and a retry is exactly the wrong move there.
        status["stage"] = "waiting for another project change"
        log.info("[project_board] register[%s]: waiting for another project change to finish", project)
    async with _MUTATION_LOCK:
        status["stage"] = "saving"
        # Consent and its root are live policy, so they are re-checked here too: a save
        # queued behind another must not keep a permission the operator has since revoked.
        if _consented_repo(repo) != repo_p:
            raise ProjectRegistryConflict(
                f"{repo} resolves to a different checkout than when this save started — save again"
            )
        section = _live_section(required=True)
        existing = _projects_from_section(section, required=True)
        prior = _prior_entry(existing, project)
        if smoked and prior != basis:
            raise ProjectRegistryConflict(
                f"project {project!r} was changed by another save while this one ran its gate — reload the "
                "project list and save again"
            )
        entry = _merge_entry(prior, repo_p, branch, optional, replace_optional)
        gate = _explicit_gate(entry)
        if gate and not smoked and not _gate_already_proven(prior, gate, repo_p):
            raise ProjectRegistryConflict(
                f"project {project!r} was changed by another save while this one waited, and its gate now "
                "needs proving in this checkout — save again"
            )
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
        status["stage"] = _APPLYING
        # The managed-project half (#452) is planned INSIDE the host's write lock, from the
        # config it hands the patch callable — never from a read taken out here.
        prior_repo = prior.get("repo") if isinstance(prior, dict) else None
        decided = await _apply_registry(
            merged,
            expected={project: entry},
            default_project=default,
            plan=lambda live, board: _plan_managed_upsert(live, board, project, repo_p, branch, github, prior_repo),
        )
        entry = decided["projects"][project]
        managed = decided["managed"]
        log.info(
            "[project_board] register[%s]: %s — repo %s, base %s",
            project,
            "created" if project not in existing else "updated",
            entry["repo"],
            branch,
        )
        if managed["action"] in ("added", "updated", "skipped"):
            log.info(
                "[project_board] register[%s]: managed project %s %s%s",
                project,
                managed["name"] or "-",
                managed["action"],
                f" — {managed['detail']}" if managed["detail"] else "",
            )
        return {
            "project": project,
            # Do not leak preserved file-only values through the PUT response after
            # deliberately redacting them from GET /projects.
            "entry": _public_entry(entry),
            "created": project not in existing,
            "default_project": default,
            "managed_project": managed,
        }


async def delete_project(
    name: str,
    *,
    assert_unused: Callable[[str, str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Delete a project after a caller-supplied live safety check, under the lock."""
    project = _validate_name(name)
    if _MUTATION_LOCK.locked():
        log.info("[project_board] delete[%s]: waiting for another project change to finish", project)
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
        prior = existing[project]
        merged = dict(existing)
        merged.pop(project)
        default = (next(iter(merged)) if len(merged) == 1 else "") if current_default == project else current_default
        # The managed-project half (#452): removed only if the board added it, planned
        # inside the host's write lock.
        decided = await _apply_registry(
            merged,
            absent={project},
            default_project=default,
            plan=lambda live, board: _plan_managed_removal(live, board, prior),
        )
        managed = decided["managed"]
        return {
            "project": project,
            "deleted": True,
            "default_project": default,
            "managed_project": managed,
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

        The repo is also registered as a READ-ONLY managed project (the host's
        `projects:` registry, GitHub from its origin remote, default branch = base_branch)
        unless it already is one, so your filesystem tools can read the checkout the
        coders branch from. Deleting the board project later removes that entry only if
        this registration added it.

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
            f"{managed_note(result.get('managed_project') or {})}"
        )

    return board_register_project
