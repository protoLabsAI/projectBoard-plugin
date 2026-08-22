"""Setup preflight (v0.42.0) — can this board run at all, and if not, say so
where the operator looks.

A fresh "Project Manager" archetype member used to boot GREEN — ``/status`` said
``bound: true``, the host's operator warnings stayed empty — and then tick into a
full traceback every ``loop_interval_s`` (``crash recovery failed`` / ``loop tick
failed``) because the host had no ``br``, the configured coder was a delegate name
that existed only on the maintainer's machine, or ``gh`` was missing. Every one of
those is a knowable fact BEFORE the first tick. This module computes them as a
pure, never-raising status dict, and :class:`GapReporter` forwards the failing ones
to the host's setup-gap seam so they surface as operator warnings in the console.

Four checks, keyed ``br`` / ``gh`` / ``coder`` / ``repo``:

* ``br``   — the beads CLI on PATH (``BR_BIN`` honored, same as store.py). Its
             ``--version`` is sampled once per resolved path (3 s cap) — never a
             board op.
* ``gh``   — the GitHub CLI on PATH (worktree.py's PR edge shells out to it).
* ``coder`` — EVERY configured coder name (``coder``, the ``coders`` tier map, and
             each ``projects:`` entry's ``coders``) resolves to a live ``acp``
             delegate in the host's delegate roster. NO names configured is a
             failure, not a pass — there is deliberately no default coder name.
* ``repo`` — the board is bound to a checkout that exists (or, for the shipped
             ``repo: "."`` default, the cwd already carries a ``.beads/``
             workspace — the one case that default legitimately works).

``ready`` is the AND of all four. ``loop_blockers`` is the subset the puller refuses
to tick without (``br``, ``coder``, ``repo``): each of those turns every tick into a
traceback, while a missing ``gh`` only fails the PR edge of a build — reported, not
paused on.

Everything the checks touch is injectable (``which``, ``delegates``, ``run``) so the
suite exercises every pass/fail branch without a host, a PATH, or a subprocess.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

from . import store as store_mod
from .projects import resolve_projects

log = logging.getLogger("protoagent.plugins.project_board")

# The check keys, in the order they render. Also the keys handed to the host's
# ``report_setup_gap(key, message)`` seam.
SETUP_KEYS: tuple[str, ...] = ("br", "gh", "coder", "repo")
# The checks the puller will not tick without (see module docstring).
LOOP_BLOCKING_KEYS: tuple[str, ...] = ("br", "coder", "repo")

# Operator-facing copy. Each hint is a complete sentence the console can show
# verbatim (as a runtime warning and on the board page's setup card).
BR_HINT = (
    "beads CLI 'br' not found on PATH — install beads-rust (cargo install beads_rust), "
    "not the homebrew `bd`, and restart (or set BR_BIN); the board is paused until then"
)
GH_HINT = (
    "GitHub CLI 'gh' not found on PATH — install it (brew install gh) and run `gh auth login`; "
    "builds can't push branches or open PRs until then"
)
NO_CODER_HINT = (
    "no coder configured — pick a delegate in Settings ▸ Project Board or let the agent "
    "propose_delegate; the board is paused until then"
)
REPO_UNBOUND_HINT = (
    "board not bound to a repo — set project_board.repo to the absolute path of the git checkout "
    "this agent manages (or db_path, or a projects: map) in Settings ▸ Project Board; the board "
    "is paused until then"
)

_BR_VERSION_TIMEOUT_S = 3.0
# ``br --version`` output per resolved binary path — sampled once per process per
# path, so the per-tick / per-page-load re-check stays a ``which`` and nothing more.
_BR_VERSION_CACHE: dict[str, str] = {}


def coder_names(cfg: dict) -> list[str]:
    """Every coder delegate name the board could dispatch to, de-duplicated in
    first-seen order: the flat ``coder``, every value of the ``coders`` tier map, and
    every value of each ``projects:`` entry's own ``coders`` map. Blanks are dropped —
    an unset ``coder`` is ``""``, never a phantom name."""
    cfg = cfg or {}
    out: list[str] = []

    def _add(v) -> None:
        name = str(v or "").strip()
        if name and name not in out:
            out.append(name)

    _add(cfg.get("coder"))
    tiers = cfg.get("coders")
    if isinstance(tiers, dict):
        for v in tiers.values():
            _add(v)
    try:
        projects = resolve_projects(cfg)
    except Exception:  # noqa: BLE001 — a malformed projects map is the repo check's finding
        projects = {}
    for entry in projects.values():
        tiers = (entry or {}).get("coders")
        if isinstance(tiers, dict):
            for v in tiers.values():
                _add(v)
    return out


def _default_delegates(name: str):
    """The live lookup: ``coder_seam.resolve_delegate(name, "acp")`` — lazy, because
    the delegates plugin is a host package this standalone module must not import at
    load time (and may be disabled, in which case every name resolves to None)."""
    from .coder_seam import resolve_delegate

    return resolve_delegate(name, "acp")


def _br_version(path: str, run) -> str:
    """``br --version`` for the binary at ``path`` (first line, stripped), cached per
    path. Any failure — timeout, non-zero exit, OSError — yields ``""``; the version is
    informational, never a gate."""
    if path in _BR_VERSION_CACHE:
        return _BR_VERSION_CACHE[path]
    version = ""
    try:
        proc = run([path, "--version"], capture_output=True, text=True, timeout=_BR_VERSION_TIMEOUT_S)
        if getattr(proc, "returncode", 1) == 0:
            version = str(getattr(proc, "stdout", "") or "").strip().splitlines()[0:1]
            version = version[0].strip() if version else ""
    except Exception:  # noqa: BLE001 — informational only
        version = ""
    _BR_VERSION_CACHE[path] = version
    return version


def _is_bound(cfg: dict) -> bool:
    """The SAME binding rule the ``/status`` route has answered since v0.40.0: an
    explicit ``db_path``, an explicit ``projects:`` map, or a ``repo`` other than the
    shipped ``"."`` default."""
    raw_projects = cfg.get("projects")
    explicit_projects = isinstance(raw_projects, dict) and bool(raw_projects)
    return bool(cfg.get("db_path")) or explicit_projects or str(cfg.get("repo") or ".").strip() not in ("", ".")


def _repo_check(cfg: dict, isdir) -> dict:
    default_repo = os.path.expanduser(str(cfg.get("repo") or ".").strip() or ".")
    try:
        projects = resolve_projects(cfg)
    except Exception as exc:  # noqa: BLE001 — a malformed projects: map IS the finding
        return {"ok": False, "path": default_repo, "hint": f"projects: map is invalid — {exc}"}
    if not _is_bound(cfg):
        # The shipped default only works when the process cwd IS the target repo —
        # honor exactly that case (a `.beads/` already there) and nothing looser.
        if isdir(os.path.join(default_repo, ".beads")):
            return {"ok": True, "path": default_repo, "hint": ""}
        return {"ok": False, "path": default_repo, "hint": REPO_UNBOUND_HINT}
    explicit = isinstance(cfg.get("projects"), dict) and bool(cfg.get("projects"))
    for name, entry in projects.items():
        path = str((entry or {}).get("repo") or "").strip()
        if not path or not isdir(path):
            where = f" (project {name!r})" if explicit else ""
            return {
                "ok": False,
                "path": path or default_repo,
                "hint": f"repo {path or default_repo!r} does not exist{where} — fix project_board.repo "
                "(or the projects: entry) in Settings ▸ Project Board",
            }
    return {"ok": True, "path": default_repo, "hint": ""}


def setup_status(cfg: dict, *, which=None, delegates=None, run=None, isdir=None) -> dict:
    """The board's setup preflight as a plain dict — pure, never raises, never shells
    out to ``br`` for a board op (one cached ``--version`` at most).

    Shape::

        {
          "br":    {"ok", "path", "version", "hint"},
          "gh":    {"ok", "path", "hint"},
          "coder": {"ok", "name", "names", "missing", "hint"},
          "repo":  {"ok", "path", "hint"},
          "loop_enabled": bool,
          "loop_blockers": [key, …],   # the failing checks the puller pauses on
          "ready": bool,               # every check ok
        }

    ``which``/``delegates``/``run``/``isdir`` are injection points for tests
    (defaults: ``shutil.which``, the live delegate roster via
    ``coder_seam.resolve_delegate``, ``subprocess.run``, ``os.path.isdir`` — resolved
    at call time so a monkeypatch on the module globals takes); ``delegates(name)``
    returns a truthy object for a resolvable acp delegate, else None.
    """
    cfg = cfg or {}
    which = which or shutil.which
    delegates = delegates or _default_delegates
    run = run or subprocess.run
    isdir = isdir or os.path.isdir

    def _which(name: str) -> str:
        try:
            return str(which(name) or "")
        except Exception:  # noqa: BLE001 — a broken PATH lookup is "not found"
            return ""

    # Probe the SAME binary name the store shells (`store.BR`, the BR_BIN override),
    # read at call time so the preflight can never disagree with the board op.
    br_bin = str(store_mod.BR or "br")
    br_path = _which(br_bin)
    br = {
        "ok": bool(br_path),
        "path": br_path,
        "version": _br_version(br_path, run) if br_path else "",
        "hint": "" if br_path else (BR_HINT if br_bin == "br" else BR_HINT.replace("'br'", repr(br_bin), 1)),
    }

    gh_path = _which("gh")
    gh = {"ok": bool(gh_path), "path": gh_path, "hint": "" if gh_path else GH_HINT}

    names = coder_names(cfg)
    missing: list[str] = []
    for name in names:
        try:
            found = delegates(name)
        except Exception:  # noqa: BLE001 — delegates plugin disabled / host-free
            found = None
        if found is None:
            missing.append(name)
    if not names:
        coder_hint = NO_CODER_HINT
    elif missing:
        listed = ", ".join(repr(n) for n in missing)
        coder_hint = (
            f"coder delegate {listed} is not declared as an acp delegate — declare it under "
            "Settings ▸ Delegates (plugins.enabled must include delegates) or pick another coder "
            "in Settings ▸ Project Board; the board is paused until then"
        )
    else:
        coder_hint = ""
    coder = {
        "ok": bool(names) and not missing,
        "name": str(cfg.get("coder") or "").strip(),
        "names": names,
        "missing": missing,
        "hint": coder_hint,
    }

    repo = _repo_check(cfg, isdir)

    status = {"br": br, "gh": gh, "coder": coder, "repo": repo}
    status["loop_enabled"] = bool(cfg.get("loop_enabled", False))
    status["loop_blockers"] = [k for k in LOOP_BLOCKING_KEYS if not status[k]["ok"]]
    status["ready"] = all(status[k]["ok"] for k in SETUP_KEYS)
    return status


def loop_blockers(status: dict) -> list[str]:
    """The checks in ``status`` the puller refuses to tick without."""
    return [k for k in LOOP_BLOCKING_KEYS if not (status.get(k) or {}).get("ok", False)]


def blocker_summary(status: dict) -> str:
    """One line for the ``loop paused:`` log — ``key: hint; key: hint``."""
    return "; ".join(f"{k}: {(status.get(k) or {}).get('hint') or 'failed'}" for k in loop_blockers(status))


class GapReporter:
    """Forwards setup gaps to the host's ``report_setup_gap(key, message)`` seam —
    the protoAgent registry hook whose messages surface as operator warnings in the
    console's ``GET /api/runtime/status``. GUARDED: the seam may not exist on the
    host yet (``getattr(registry, "report_setup_gap", None)`` — older hosts, the
    host-free suite), in which case every report is a recorded no-op.

    Edge-triggered: ``report(status)`` calls the seam only for checks whose message
    CHANGED since the last report — a newly failing check sends its hint, a
    recovered one sends ``None`` (the clear), a steady state sends nothing — so a
    30 s tick never spams the host. Never raises."""

    def __init__(self, registry=None):
        fn = getattr(registry, "report_setup_gap", None) if registry is not None else None
        self._fn = fn if callable(fn) else None
        self._reported: dict[str, str | None] = {}

    @property
    def available(self) -> bool:
        """True when the host exposes the seam."""
        return self._fn is not None

    @property
    def reported(self) -> dict[str, str | None]:
        """The last message sent per key (None = cleared / never failed)."""
        return dict(self._reported)

    def report(self, status: dict) -> dict[str, str | None]:
        """Diff ``status`` against the last report and forward the changes. Returns
        ``{key: message_or_None}`` for exactly the keys forwarded this call."""
        changes: dict[str, str | None] = {}
        for key in SETUP_KEYS:
            check = (status or {}).get(key) or {}
            msg = None if check.get("ok", False) else (str(check.get("hint") or "") or f"{key} check failed")
            if self._reported.get(key) == msg:
                continue
            self._reported[key] = msg
            changes[key] = msg
            if self._fn is None:
                continue
            try:
                self._fn(key, msg)
            except Exception:  # noqa: BLE001 — a host-side failure must never break the loop
                log.warning("[project_board] report_setup_gap(%r) failed", key, exc_info=True)
        return changes
