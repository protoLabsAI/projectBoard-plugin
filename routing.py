"""Live routing validation, persistence and audit shared by tools and HTTP (#577)."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from datetime import datetime, timezone

from .projects import IMPLICIT_MARKER, TIER_ALIASES, default_project, normalize_tier_config, resolve_projects
from .setup_check import _default_delegates, _rung_names as rung_delegates
from .store import TIER_LADDER

log = logging.getLogger("protoagent.plugins.project_board")


def validate_routing(cfg, delegates=None, *, resolve=True, previous=None):
    """Validate structure, preserving legacy load forms when resolve=False.

    Resolve delegates only for operator-supplied fields. The complete stored
    config may name disabled delegates in other fields that still need repair.
    With a previous config, structural checks apply only to changed policy fields
    in each scope. Existing load-compatible values must not block unrelated edits;
    strict operator validation of supplied fields never uses this exemption.
    """
    cfg = normalize_tier_config(copy.deepcopy(cfg))
    projects = resolve_projects(cfg)
    default_project(cfg)
    old = normalize_tier_config(previous) if previous is not None and not resolve else None
    old_projects = resolve_projects(old) if old is not None else {}
    names = set()
    scopes = [
        ("board", cfg, old),
        *(
            (name, entry, old if entry.get(IMPLICIT_MARKER) else old_projects.get(name))
            for name, entry in projects.items()
        ),
    ]
    for label, entry, prior in scopes:
        if prior is not None:
            # Compare normalized fields rather than whole project entries: changing a
            # repo must not revalidate its unchanged legacy ladder.
            entry = {key: value for key, value in entry.items() if key not in prior or value != prior[key]}
        coders = entry.get("coders", {})
        if coders is None and not resolve:
            coders = {}
        if not isinstance(coders, dict):
            raise ValueError(f"{label}.coders must be a mapping")
        for tier, rung in coders.items():
            if tier not in TIER_LADDER:
                raise ValueError(f"unknown coder tier {tier!r}")
            if (
                not isinstance(rung, (str, list) if resolve else (str, list, tuple, type(None)))
                or (
                    isinstance(rung, (list, tuple))
                    and any(not isinstance(n, str) or (resolve and not n.strip()) for n in rung)
                )
                or (resolve and isinstance(rung, str) and not rung.strip())
            ):
                raise ValueError(f"{label}.coders.{tier} must contain delegate names")
            names.update(rung_delegates(rung))
        routing = entry.get("routing", {})
        if routing is None and not resolve:
            routing = {}
        # #579 adds the headroom-steering knobs alongside exclusive; nothing else belongs.
        if not isinstance(routing, dict) or set(routing) - {"exclusive", "headroom", "prefer_sibling_at"}:
            raise ValueError(
                f"{label}.routing must be a mapping containing only exclusive, headroom, prefer_sibling_at"
            )
        exclusive = routing.get("exclusive", [])
        if not resolve and (exclusive is None or isinstance(exclusive, (str, tuple))):
            exclusive = rung_delegates(exclusive)
        if not isinstance(exclusive, list) or any(
            not isinstance(n, str) or (resolve and not n.strip()) for n in exclusive
        ):
            raise ValueError(f"{label}.routing.exclusive must be a list of delegate names")
        names.update(rung_delegates(exclusive))
        # `headroom` (default true) toggles headroom steering; accept a real bool or a
        # YAML/Settings boolean spelling (the loop coerces it with knob_bool).
        if "headroom" in routing:
            hv = routing["headroom"]
            ok = isinstance(hv, bool) or (
                isinstance(hv, str) and hv.strip().lower() in {"1", "true", "yes", "on", "0", "false", "no", "off", ""}
            )
            if not ok:
                raise ValueError(f"{label}.routing.headroom must be a boolean")
        # `prefer_sibling_at` is the used-% threshold (1..100).
        if "prefer_sibling_at" in routing:
            try:
                pct = int(routing["prefer_sibling_at"])
            except (TypeError, ValueError):
                raise ValueError(f"{label}.routing.prefer_sibling_at must be an integer percent between 1 and 100")
            if not 1 <= pct <= 100:
                raise ValueError(f"{label}.routing.prefer_sibling_at must be an integer percent between 1 and 100")
    if resolve:
        resolver = delegates if delegates is not None else (_default_delegates() if names else None)
        for name in sorted(names):
            if resolver(name) is None:
                raise ValueError(f"delegate {name!r} is not configured/enabled")
    return cfg


def effective_routing(cfg):
    """Return inherited ladders and allowlists, including empty tiers."""
    cfg = normalize_tier_config(cfg)
    result = {}
    for name, entry in resolve_projects(cfg).items():
        exclusive = (entry.get("routing") or {}).get("exclusive", (cfg.get("routing") or {}).get("exclusive"))
        coders = entry.get("coders") or cfg.get("coders") or {}
        result[name] = {
            "coders": {tier: rung_delegates(coders.get(tier)) for tier in TIER_LADDER},
            "exclusive": rung_delegates(exclusive),
        }
    return result


def routing_get():
    from .project_registry import _live_section

    return effective_routing(_live_section(required=True))


# Lifecycle paths ``routing.exclusive`` does NOT govern (#578, mirrors
# setup_check.routing_exclusive_hint): the review gate reviewer, review dispatch, and the
# helper delegates all run unrestricted by the strict coder set (B2).
UNCOVERED_HELPER_PATHS = [
    "review dispatch",
    "decompose",
    "coder.solve offload",
    "Max-Mode judge",
    "task-assignee delegates",
]


def _headroom_display(snap):
    """A delegate's free binding-window headroom as a percent, else ``"unknown"`` (#578).

    Unknown is a non-dict snapshot (the host lacks ``graph.sdk.provider_usage`` or the read
    was flaky) or one without a numeric ``headroom_pct`` — never an error, so the view
    reads "unknown" exactly where dispatch treats the sibling as today (ADR 0122 D4)."""
    if not isinstance(snap, dict):
        return "unknown"
    room = snap.get("headroom_pct")
    if isinstance(room, bool) or not isinstance(room, (int, float)):
        return "unknown"
    return room


def _rung_pick(siblings, usage, *, headroom_on, prefer_at, exclusive):
    """Which sibling the next dispatch opens on at this rung, and why (#578).

    Computed with the loop's OWN ``prefer_live_sibling`` / ``prefer_headroom_sibling`` on
    the same inputs, so the view can never disagree with dispatch. ``current`` is 0: a
    fresh card opens on the first sibling unless a down mark (#420) or subscription
    headroom (#579) steers it off. The reason is one of round-robin / down-marked /
    ``headroom ≥N%`` / exclusive."""
    from .loop import prefer_headroom_sibling, prefer_live_sibling, provider_is_down

    n = len(siblings)
    if n == 0:
        return {"starts_on": None, "reason": "no eligible delegate"}
    snaps = {name: usage(name) for name in siblings}
    if headroom_on:
        idx = prefer_headroom_sibling(siblings, 0, set(), snaps, prefer_at=prefer_at) % n
    else:
        idx = prefer_live_sibling(siblings, 0, set()) % n
    if idx == 0:
        reason = "exclusive" if (exclusive and n == 1) else "round-robin"
    elif provider_is_down(siblings[0]):
        reason = "down-marked"
    else:
        reason = f"headroom ≥{prefer_at}%"
    return {"starts_on": siblings[idx], "reason": reason}


def effective_routes(cfg, *, usage=None):
    """The effective routing a console operator sees (#578), per project.

    For each project it returns the exclusive-filtered coder ladder (one entry per
    ``TIER_LADDER`` tier, with the delegates left after the B2 filter and which sibling the
    next dispatch would open on, with the reason); the five named lifecycle paths —
    implement, fix (CI/review), escalate, review, helpers — with the uncovered ones marked;
    and, per delegate the ladder names, its live down mark (#420) and subscription headroom
    (#579, "unknown" when unread). ``usage(name) -> snapshot|None`` reads a delegate's
    ``graph.sdk.provider_usage`` snapshot; it defaults to all-unknown so the whole function
    is host-free and testable on a config alone. The sibling pick reuses the loop's pure
    functions, so the display can't drift from dispatch."""
    from .loop import knob_bool, provider_is_down

    usage = usage or (lambda name: None)
    cfg = normalize_tier_config(cfg)
    projects = resolve_projects(cfg)
    board_routing = cfg.get("routing") or {}
    all_delegates: set[str] = set()
    out = {}
    for name, entry in projects.items():
        is_implicit = bool(entry.get(IMPLICIT_MARKER))
        proj_routing = entry.get("routing") or {}
        exclusive = rung_delegates(proj_routing.get("exclusive", board_routing.get("exclusive")))
        if not is_implicit and "exclusive" in proj_routing:
            source = "project"
        elif board_routing.get("exclusive") is not None:
            source = "board"
        else:
            source = "none"
        coders = entry.get("coders") or cfg.get("coders") or {}
        full = {tier: rung_delegates(coders.get(tier)) for tier in TIER_LADDER}
        # Headroom knobs resolve exactly as the loop's _headroom_on_for /
        # _prefer_sibling_at_for: the project's own value overrides the board default.
        hsrc = proj_routing if "headroom" in proj_routing else board_routing
        headroom_on = knob_bool(hsrc, "headroom", True, strict=False)
        raw_at = proj_routing.get("prefer_sibling_at", board_routing.get("prefer_sibling_at", 90))
        try:
            prefer_at = min(100, max(1, int(raw_at)))
        except (TypeError, ValueError):
            prefer_at = 90
        ladder = []
        for tier in TIER_LADDER:
            siblings = [n for n in full[tier] if n in exclusive] if exclusive else list(full[tier])
            pick = _rung_pick(siblings, usage, headroom_on=headroom_on, prefer_at=prefer_at, exclusive=bool(exclusive))
            ladder.append({"tier": tier, "delegates": siblings, **pick})
        entry_rung = next((r for r in ladder if r["delegates"]), None)
        # Every delegate the project's ladder names (pre-filter), in ladder order, with its
        # live down mark and headroom — marks/headroom are per delegate, not per rung.
        names_here: list[str] = []
        for tier in TIER_LADDER:
            for d in full[tier]:
                if d not in names_here:
                    names_here.append(d)
        all_delegates.update(names_here)
        delegate_rows = [
            {"name": d, "provider_is_down": provider_is_down(d), "headroom": _headroom_display(usage(d))}
            for d in names_here
        ]
        paths = [
            {
                "path": "implement",
                "covered": True,
                "detail": (
                    f"opens on {entry_rung['starts_on']} at the {entry_rung['tier']} rung"
                    if entry_rung
                    else "no eligible delegate — the card blocks"
                ),
            },
            {
                "path": "fix (CI/review)",
                "covered": True,
                "detail": "re-dispatches on the card's current rung (same exclusive-filtered delegates)",
            },
            {
                "path": "escalate",
                "covered": True,
                "detail": "climbs the ladder above, after the exclusive filter",
            },
            {
                "path": "review",
                "covered": False,
                "detail": "review gate reviewer — not covered by routing.exclusive",
            },
            {
                "path": "helpers",
                "covered": False,
                "detail": "not covered by routing.exclusive: " + ", ".join(UNCOVERED_HELPER_PATHS),
            },
        ]
        out[name] = {
            "exclusive": exclusive,
            "exclusive_source": source,
            "headroom_on": headroom_on,
            "prefer_sibling_at": prefer_at,
            "ladder": ladder,
            "paths": paths,
            "delegates": delegate_rows,
        }
    base = cfg.get("coder")
    if isinstance(base, str) and base.strip():
        all_delegates.add(base.strip())
    return {
        "board_exclusive": rung_delegates(board_routing.get("exclusive")),
        "delegates": sorted(all_delegates),
        "projects": out,
    }


def routing_effective():
    """Live effective routing for GET /routing/effective (#578): the running board
    config plus each delegate's live ``graph.sdk.provider_usage`` snapshot (getattr-
    guarded, so an older host degrades every delegate to "unknown", never an error)."""
    from .project_registry import _live_section

    return effective_routes(_live_section(required=True), usage=_provider_usage)


def _provider_usage(name):
    """One delegate's live subscription-usage snapshot via the host SDK (U7, #579), or
    ``None`` when it can't be read — the same getattr-guarded seam the drive loop uses
    (ADR 0122 D8). Never raises: a missing host or a flaky read is "unknown"."""
    try:
        import graph.sdk as graph_sdk
    except Exception:  # noqa: BLE001 — no host package at all (tests, CLI)
        return None
    fn = getattr(graph_sdk, "provider_usage", None)
    if fn is None:
        return None
    try:
        snap = fn(delegate=name)
    except Exception:  # noqa: BLE001 — a flaky usage read is unknown, not a failure
        return None
    return snap if isinstance(snap, dict) else None


def operator_actor(config):
    """Only host-injected RunnableConfig auth may authorize the tool; never tool args."""
    auth = (config or {}).get("configurable", {}).get("auth", {})
    if not isinstance(auth, dict) or auth.get("tier") != "operator":
        raise PermissionError("board_routing_set requires operator-tier invocation context")
    return str(auth.get("subject") or "operator")


async def routing_set(*, actor, coders=None, exclusive=None, project=None):
    from graph.plugins.host import HOST

    from .br_fetch import data_dir
    from .project_registry import ProjectRegistryError, _live_section, _MUTATION_LOCK

    if project is not None and (not isinstance(project, str) or not project.strip()):
        raise ValueError("project must be a non-empty project name")
    if coders is None and exclusive is None:
        raise ValueError("supply coders or exclusive")
    if HOST.apply_settings is None:
        raise ProjectRegistryError("host config apply is unavailable")
    async with _MUTATION_LOCK:
        record = {}

        def compute(live):
            plugin_config = getattr(live, "plugin_config", None)
            section = plugin_config.get("project_board") if isinstance(plugin_config, dict) else None
            if section is None:
                section = getattr(live, "project_board", None)
            if not isinstance(section, dict):
                raise ValueError("live project_board config is not a mapping")
            candidate = copy.deepcopy(section)
            target = candidate
            if project:
                if project not in candidate.get("projects", {}):
                    raise ValueError(f"unknown project {project!r}")
                target = candidate["projects"][project]
            supplied = {}
            if coders is not None:
                supplied["coders"] = coders
                target["coders"] = coders
            if exclusive is not None:
                supplied["routing"] = {"exclusive": exclusive}
                target["routing"] = {**(target.get("routing") or {}), "exclusive": exclusive}
            validate_routing(supplied)
            candidate = validate_routing(candidate, resolve=False, previous=section)
            record.update(
                who=actor,
                when=datetime.now(timezone.utc).isoformat(),
                old=effective_routing(section),
                new=effective_routing(candidate),
            )
            # Same HOST.apply_settings callable/save boundary as project writes. The
            # host merges maps: explicit tombstones remove aliases and replaced rungs.
            patch = {
                key: copy.deepcopy(candidate[key]) for key in ("coders", "routing", "projects") if key in candidate
            }
            # Project entries are assigned whole by the host; only the top-level
            # coders map merges. Tombstones inside project entries would persist.
            if isinstance(patch.get("coders"), dict):
                for key in set(section.get("coders") or {}) - set(patch["coders"]):
                    patch["coders"][key] = None
                for key in TIER_ALIASES.keys() & (section.get("coders") or {}).keys():
                    patch["coders"][key] = None
            return {"project_board": patch}

        ok, messages = await asyncio.to_thread(HOST.apply_settings, compute)
        if not ok:
            raise ProjectRegistryError("; ".join(messages) or "host refused routing update")
        landed = effective_routing(_live_section(required=True))
        if landed != record.get("new"):
            raise ProjectRegistryError("routing readback did not match the saved policy")
        path = data_dir() / "routing-audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
        log.info("[project_board] routing audit: %s", json.dumps(record, sort_keys=True))
        return landed
