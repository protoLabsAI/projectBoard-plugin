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

Two ADVISORIES beyond the checks (D3, #260), both info-level: reported through the
same gap seam, rendered as callouts on the board page, but never a failing check and
never a pause. First, the stale-override advisory: a multi-entry ``projects:`` map
combined with an EXPLICITLY blank ``db_path`` — the pre-D3 per-repo-discovery
override. Since D3 that blank is INERT: ``projects.store_db_path`` (and ``get_store``
itself) resolve it to the same single instance-default store as an absent key, so the
board runs correctly on ONE shared db and nothing fragments. The finding is the stale
override itself — the operator wrote a knob that no longer does anything — surfaced
under its own ``db`` key (``MULTI_PROJECT_DB_HINT``). Second, the MIGRATION advisory:
with no explicit ``db_path`` the board reads the instance store — but the SAME config
on a pre-D3 board kept its cards inside the configured repo (`br` per-repo
discovery), so a repo that still carries a ``.beads/`` workspace is the upgrade
signature: any cards left there are invisible to this board until the operator pins
``db_path`` back to that file or migrates them. Surfaced under its own ``db_legacy``
key (``legacy_store_hint``), so the switch is never a silently empty board.

Everything the checks touch is injectable (``which``, ``delegates``, ``run``) so the
suite exercises every pass/fail branch without a host, a PATH, or a subprocess.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import types

from . import br_fetch
from . import store as store_mod
from .projects import blank_db_override, multi_project, resolve_projects
from .store import TIER_LADDER, escalation_enabled

log = logging.getLogger("protoagent.plugins.project_board")

# The check keys, in the order they render. Also the keys handed to the host's
# ``report_setup_gap(key, message)`` seam.
SETUP_KEYS: tuple[str, ...] = ("br", "gh", "coder", "repo")
# The checks the puller will not tick without (see module docstring).
LOOP_BLOCKING_KEYS: tuple[str, ...] = ("br", "coder", "repo")
# Three extra host-gap keys beyond the checks: the running loop's config snapshot
# lagging the live config on a restart-only knob (see ``loop_cfg_stale`` below), the
# inert multi-project blank-``db_path`` override (see ``db_override_ignored``), and
# the pre-D3 per-repo workspace migration advisory (see ``legacy_store_repos``) —
# all advisories: reported through the seam, never a failing check, never a pause.
LOOP_STALE_KEY = "loop"
DB_OVERRIDE_KEY = "db"
LEGACY_STORE_KEY = "db_legacy"
REPORT_KEYS: tuple[str, ...] = SETUP_KEYS + (LOOP_STALE_KEY, DB_OVERRIDE_KEY, LEGACY_STORE_KEY)
# The config keys the running loop reads ONCE at construction and cannot pick up on a
# reload (``coder`` is live since v0.42.0 — see loop.LIVE_STR_KNOBS). A reload that
# changes one of these leaves the running loop on the old value until a restart, so
# ``/status`` says so instead of reporting the NEW config as the loop's state.
LOOP_RESTART_KEYS: tuple[str, ...] = ("coders", "repo", "base_branch", "db_path")

# The subprocess runner ``_br_version`` uses — a module attribute (not a bare
# ``subprocess.run`` reference) so the suite's autouse fixture can pin it without
# touching ``subprocess`` itself (the real-br integration tier still shells out).
_subprocess_run = subprocess.run

# Operator-facing copy. Each hint is a complete sentence the console can show
# verbatim (as a runtime warning and on the board page's setup card).
# The plain "no br, nothing fetching" hint — br_fetch.hint_for's idle copy, so the
# preflight and the auto-fetch never disagree on the install wording (v0.43.0).
BR_HINT = br_fetch.hint_for({"state": "idle"})
GH_HINT = (
    "GitHub CLI 'gh' not found on PATH — install it (brew install gh) and run `gh auth login`; "
    "builds can't push branches or open PRs until then"
)
NO_CODER_HINT = (
    "no coder configured — pick a delegate in Settings ▸ Project Board or let the agent "
    "propose_delegate; the board is paused until then (the former implicit default `proto` "
    "no longer applies — set `coder: proto` to keep it)"
)
RESTART_NOTE = "the running loop still has the previous value — restart the agent to apply"
REPO_UNBOUND_HINT = (
    "board not bound to a repo — set project_board.repo to the absolute path of the git checkout "
    "this agent manages (or db_path, or a projects: map) in Settings ▸ Project Board; the board "
    "is paused until then"
)
MULTI_PROJECT_DB_HINT = (
    "projects: declares multiple repos and db_path is explicitly blank — that pre-D3 "
    "per-repo-discovery override is now ignored: a blank db_path resolves to the one instance "
    "store (same as leaving the key out), so every project shares one board and nothing "
    "fragments. The board keeps running; remove the stale db_path override, or set db_path to "
    "a single shared file in Settings ▸ Project Board"
)


def legacy_store_hint(repos: list[str]) -> str:
    """Operator copy for the pre-D3 migration advisory — names every configured repo
    still carrying a ``.beads/`` workspace the board no longer reads. ``""`` when
    there are none (the advisory's quiet state)."""
    if not repos:
        return ""
    listed = ", ".join(repr(r) for r in repos)
    return (
        f"repo {listed} carries a .beads/ workspace the board does not read — with no db_path "
        "configured the board keeps its own instance store (one shared db). A pre-D3 board with "
        "this config kept its cards IN the repo's .beads/; if yours did, those cards are not on "
        "this board: set db_path to that .beads/<file>.db in Settings ▸ Project Board to keep "
        "reading them, or migrate them into the instance store. If the workspace belongs to the "
        "repo's own beads tracking, set an explicit db_path to pin the board and quiet this note"
    )


def legacy_store_repos(cfg: dict, isdir=None) -> list[str]:
    """The configured project repos still carrying a ``.beads/`` workspace while the
    board rides the instance store (no explicit ``db_path``) — the pre-D3 upgrade
    signature: per-repo discovery made each of those the board's store, so cards left
    there are invisible until ``db_path`` pins back to the file or they are migrated.
    Empty when ``db_path`` is set (the operator's pin decides, wherever it points — an
    EXPLICITLY blank value is not a pin, it rides the instance store like an absent
    key), when the ``projects:`` map is malformed (the repo check owns that finding),
    or when no repo carries one. De-duplicated in first-seen order (several projects
    may share a repo)."""
    cfg = cfg or {}
    isdir = isdir or os.path.isdir
    if str(cfg.get("db_path") or "").strip():
        return []
    try:
        resolved = resolve_projects(cfg)
    except Exception:  # noqa: BLE001 — a malformed projects: map is the repo check's finding
        return []
    out: list[str] = []
    for entry in resolved.values():
        path = str((entry or {}).get("repo") or "").strip()
        if path and path not in out and isdir(os.path.join(path, ".beads")):
            out.append(path)
    return out


_BR_VERSION_TIMEOUT_S = 3.0
# ``br --version`` output per resolved binary path — sampled once per process per
# path, so the per-tick / per-page-load re-check stays a ``which`` and nothing more.
_BR_VERSION_CACHE: dict[str, str] = {}


_LOOP_SLOT_PREFIX = "project_board.live_loop::"


def _loop_slot_name() -> str:
    pkg = __name__.rsplit(".", 1)[0] if "." in __name__ else __name__
    return _LOOP_SLOT_PREFIX + pkg


def _loop_slot():
    """The process-stable holder for the RUNNING loop's config snapshot — a
    ``sys.modules`` data slot (the coder_seam #178 pattern) so it survives a plugin
    reload: the reload re-imports this module and rebuilds the routers, but the
    surface (the loop) keeps running on its construction-time config."""
    name = _loop_slot_name()
    holder = sys.modules.get(name)
    if holder is None:
        holder = types.ModuleType(name)
        holder.__doc__ = "Process-stable holder for project_board's running-loop config snapshot — data, not code."
        holder.snapshot = None
        sys.modules[name] = holder
    return holder


def snapshot_of(cfg: dict) -> dict:
    """The restart-only keys of ``cfg`` (plus ``coder``), as the loop reads them."""
    cfg = cfg or {}
    out = {k: cfg.get(k) for k in LOOP_RESTART_KEYS}
    out["coder"] = str(cfg.get("coder") or "").strip()
    return out


def publish_loop_snapshot(cfg: dict | None) -> None:
    """Record the config the RUNNING loop is on (``BoardLoop.start``/``reload``), or
    clear it (``None``, on stop)."""
    _loop_slot().snapshot = snapshot_of(cfg) if cfg is not None else None


def live_loop_snapshot() -> dict | None:
    """The running loop's config snapshot, or None when no loop has started."""
    snap = getattr(_loop_slot(), "snapshot", None)
    return dict(snap) if isinstance(snap, dict) else None


def stale_loop_keys(cfg: dict, snapshot: dict | None) -> list[str]:
    """The restart-only keys on which the running loop's snapshot differs from the
    live ``cfg`` — empty when no loop is running or nothing drifted."""
    if not snapshot:
        return []
    want = snapshot_of(cfg)
    return [k for k in LOOP_RESTART_KEYS if want.get(k) != snapshot.get(k)]


def _normalize_coders(raw) -> dict[str, str]:
    return {str(k): str(v or "").strip() for k, v in raw.items()} if isinstance(raw, dict) else {}


def uncovered_tiers(coders: dict) -> list[str]:
    """The ``TIER_LADDER`` rungs a ``coders`` map leaves unmapped (or blank)."""
    coders = _normalize_coders(coders)
    return [t for t in TIER_LADDER if not coders.get(t)]


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


def _default_delegates():
    """The live lookup — ONE roster read for the whole call (``coder_seam.
    delegate_resolver("acp")``: every name resolved against the same
    ``DelegateRegistry``, not a YAML re-parse per name). Lazy, because the delegates
    plugin is a host package this standalone module must not import at load time
    (and may be disabled, in which case every name resolves to None)."""
    from .coder_seam import delegate_resolver

    return delegate_resolver("acp")


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
    # NB: a multi-entry projects: map with an explicitly blank db_path is NOT a repo
    # failure — since D3 (#260) the blank resolves to the instance store, so the board
    # runs correctly on one shared db. The stale override is the `db` ADVISORY in
    # setup_status, not a pause.
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


def setup_status(cfg: dict, *, which=None, delegates=None, run=None, isdir=None, loop_snapshot=None) -> dict:
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
          "loop_cfg_stale": bool,      # the RUNNING loop is on an older restart-only knob
          "loop_cfg_stale_keys": [k…], # which ones (coders/repo/base_branch/db_path/projects)
          "loop_cfg_stale_hint": str,  # operator copy, "" when not stale
          "db_override_ignored": bool, # multi-entry projects: + explicit db_path: "" (inert, D3 #260)
          "db_override_hint": str,     # operator copy, "" when the advisory is quiet
          "legacy_store_repos": [p…],  # repos still carrying a pre-D3 `.beads/` workspace (D3 #260)
          "legacy_store_hint": str,    # migration copy, "" when the advisory is quiet
          "ready": bool,               # every check ok
        }

    The ``coder`` rule: with ``coder`` set, every configured name must resolve. With
    ``coder`` BLANK the ladder is the only dispatch path, and dispatch resolves
    ``coders.get(tier, coder)`` → ``""`` for any unmapped rung — so blank ``coder``
    passes only when escalation is on (>1 distinct delegate) AND the instance map and
    every ``projects:`` entry's map cover EVERY ``TIER_LADDER`` rung; otherwise a card
    at the unmapped tier blocks with ``coder delegate '' not configured``.

    ``which``/``delegates``/``run``/``isdir``/``loop_snapshot`` are injection points
    for tests (defaults: ``shutil.which``, one ``coder_seam.delegate_resolver("acp")``
    per call, ``_subprocess_run``, ``os.path.isdir``, ``live_loop_snapshot()`` —
    resolved at call time so a monkeypatch on the module globals takes);
    ``delegates(name)`` returns a truthy object for a resolvable acp delegate, else None.
    """
    cfg = cfg or {}
    which = which or shutil.which
    if delegates is None:
        try:
            delegates = _default_delegates()
        except Exception:  # noqa: BLE001 — no roster at all ⇒ nothing resolves
            delegates = lambda _name: None  # noqa: E731
    run = run or _subprocess_run
    isdir = isdir or os.path.isdir
    if loop_snapshot is None:
        loop_snapshot = live_loop_snapshot()

    def _which(name: str) -> str:
        try:
            return str(which(name) or "")
        except Exception:  # noqa: BLE001 — a broken PATH lookup is "not found"
            return ""

    # Probe the SAME binary name the store shells (`store.BR`, the BR_BIN override),
    # read at call time so the preflight can never disagree with the board op.
    br_bin = str(store_mod.BR or "br")
    br_path = _which(br_bin)
    fetch = br_fetch.fetch_state()
    # v0.43.0: every "no br" hint is the auto-fetch's story (hint_for renders idle /
    # fetching / failed / disabled / unsupported — idle is the plain install hint).
    br_hint = "" if br_path else br_fetch.hint_for(fetch, br_bin=br_bin)
    br = {
        "ok": bool(br_path),
        "path": br_path,
        "version": _br_version(br_path, run) if br_path else "",
        "hint": br_hint,
        # How the binary was resolved (env / fetched / path) + the auto-fetch state, so
        # the board page can say "br fetched to …" / "fetching br …".
        "source": "env"
        if str(os.environ.get(br_fetch.ENV_BR_BIN) or "").strip()
        else ("fetched" if fetch["state"] == "done" and fetch.get("path") else "path"),
        "fetch": {
            "state": fetch["state"],
            "version": fetch.get("version", ""),
            "platform": fetch.get("platform", ""),
            "path": fetch.get("path", ""),
            "error": fetch.get("error", ""),
        },
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
    coder_name = str(cfg.get("coder") or "").strip()
    ladder_gap = _ladder_gap(cfg) if not coder_name else ""
    if not names:
        coder_hint = NO_CODER_HINT
    elif ladder_gap:
        coder_hint = ladder_gap
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
        "ok": bool(names) and not missing and not ladder_gap,
        "name": coder_name,
        "names": names,
        "missing": missing,
        "hint": coder_hint,
    }

    repo = _repo_check(cfg, isdir)

    status = {"br": br, "gh": gh, "coder": coder, "repo": repo}
    status["loop_enabled"] = bool(cfg.get("loop_enabled", False))
    status["loop_blockers"] = [k for k in LOOP_BLOCKING_KEYS if not status[k]["ok"]]
    # Restart-only drift (review on #212): the running loop reads `coders`/`repo`/…
    # once; after a reload the routers (and this status) see the NEW config while
    # the loop is still on the old one. Say so — on the failing hints and as its own
    # line — instead of reporting the new config as the loop's state.
    stale_keys = stale_loop_keys(cfg, loop_snapshot)
    status["loop_cfg_stale"] = bool(stale_keys)
    status["loop_cfg_stale_keys"] = stale_keys
    status["loop_cfg_stale_hint"] = (
        f"config changed since the loop started ({', '.join(stale_keys)}) — {RESTART_NOTE}" if stale_keys else ""
    )
    for key in ("coder", "repo"):
        drifted = [k for k in stale_keys if k in _STALE_KEYS_PER_CHECK[key]]
        if drifted and not status[key]["ok"]:
            status[key]["hint"] = f"{status[key]['hint']} ({RESTART_NOTE}: {', '.join(drifted)})"
    # The D3 advisory (#260): a multi-entry projects: map next to an EXPLICITLY blank
    # db_path (the pre-D3 per-repo-discovery escape hatch). The override is inert —
    # store_db_path resolves blank to the same instance default as an absent key, so
    # the board runs correctly on ONE shared store — which is exactly why this is a
    # warning (a knob that silently does nothing), not a failing check or a pause.
    ignored = multi_project(cfg) and blank_db_override(cfg)
    status["db_override_ignored"] = ignored
    status["db_override_hint"] = MULTI_PROJECT_DB_HINT if ignored else ""
    # The D3 MIGRATION advisory (#260): with no explicit db_path the board reads the
    # instance store — but the SAME config on a pre-D3 board kept its cards in the
    # repo's `.beads/` (per-repo discovery), so a repo still carrying one means any
    # cards left there are invisible here until db_path pins back to that file or
    # they are migrated. Info-level like the override above: the board itself is
    # healthy, so it never fails a check or pauses the loop.
    legacy = legacy_store_repos(cfg, isdir=isdir)
    status["legacy_store_repos"] = legacy
    status["legacy_store_hint"] = legacy_store_hint(legacy)
    status["ready"] = all(status[k]["ok"] for k in SETUP_KEYS)
    return status


# Which restart-only knobs feed which check — for the per-check restart note.
_STALE_KEYS_PER_CHECK = {"coder": ("coders", "projects"), "repo": ("repo", "base_branch", "db_path", "projects")}


def _ladder_gap(cfg: dict) -> str:
    """With ``coder`` blank, the reason the ``coders`` ladder can't stand alone — or
    ``""`` when it covers every rung everywhere dispatch can land."""
    cfg = cfg or {}
    coders = _normalize_coders(cfg.get("coders"))
    if not coders:
        return ""  # no ladder either — the no-coder hint covers it
    if not escalation_enabled(cfg):
        return (
            "`coder:` is unset and the coders map has only one distinct delegate, so escalation "
            "is OFF and the loop dispatches `coder:` — set `coder:` (or map >1 distinct delegates "
            "across every tier)"
        )
    gaps: list[str] = []
    missing = uncovered_tiers(coders)
    if missing:
        gaps.append(", ".join(missing))
    try:
        projects = resolve_projects(cfg)
    except Exception:  # noqa: BLE001 — the repo check owns a malformed map
        projects = {}
    explicit = isinstance(cfg.get("projects"), dict) and bool(cfg.get("projects"))
    for name, entry in projects.items():
        pmap = _normalize_coders((entry or {}).get("coders"))
        if not pmap or not explicit:
            continue  # an empty project map falls back to the instance map (checked above)
        pm = uncovered_tiers(pmap)
        if pm:
            gaps.append(f"{', '.join(pm)} (project {name!r})")
    if not gaps:
        return ""
    return (
        f"`coder:` is unset and the coders ladder doesn't cover tier(s) {'; '.join(gaps)} — "
        "a card at an unmapped tier dispatches to '' and blocks; set `coder:` as the fallback "
        "(or map every tier: " + ", ".join(TIER_LADDER) + ")"
    )


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

    Edge-triggered AFTER the first evaluation: ``report(status)`` calls the seam
    for checks whose message CHANGED since the last report — a newly failing check
    sends its hint, a recovered one sends ``None`` (the clear), a steady state sends
    nothing — so a 30 s tick never spams the host. The FIRST evaluation sends every
    key unconditionally (``None`` for a passing check included): a reload builds a
    fresh reporter with no memory of what the previous instance reported, and a
    warning it raised must not outlive the gap (the host's clear is idempotent).
    Never raises."""

    def __init__(self, registry=None):
        fn = getattr(registry, "report_setup_gap", None) if registry is not None else None
        self._fn = fn if callable(fn) else None
        self._reported: dict[str, str | None] = {}
        self._primed = False

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
        first = not self._primed
        self._primed = True
        for key in REPORT_KEYS:
            if key == LOOP_STALE_KEY:
                msg = str((status or {}).get("loop_cfg_stale_hint") or "") or None
            elif key == DB_OVERRIDE_KEY:
                msg = str((status or {}).get("db_override_hint") or "") or None
            elif key == LEGACY_STORE_KEY:
                msg = str((status or {}).get("legacy_store_hint") or "") or None
            else:
                check = (status or {}).get(key) or {}
                msg = None if check.get("ok", False) else (str(check.get("hint") or "") or f"{key} check failed")
            if not first and key in self._reported and self._reported[key] == msg:
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
