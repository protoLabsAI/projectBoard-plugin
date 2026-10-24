"""Live routing validation, persistence and audit shared by tools and HTTP (#577)."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from datetime import datetime, timezone

from .projects import (
    IMPLICIT_MARKER,
    TIER_ALIASES,
    canonical_tier,
    default_project,
    normalize_tier_config,
    resolve_projects,
)
from .setup_check import _default_delegates, _rung_names as rung_delegates
from .store import TIER_LADDER, escalation_enabled

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
        # #579 adds the headroom-steering knobs alongside exclusive; #581 adds usage_policy.
        if not isinstance(routing, dict) or set(routing) - {
            "exclusive",
            "headroom",
            "prefer_sibling_at",
            "usage_policy",
        }:
            raise ValueError(
                f"{label}.routing must be a mapping containing only exclusive, headroom, "
                "prefer_sibling_at, usage_policy"
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
        # #581: `usage_policy` is a list of {account, keep_under, prefer, tiers} reorder
        # rules. Each is validated structurally here; `account` and `prefer` join `names` so
        # the resolve pass refuses an unknown delegate/account. An invalid rule raises, and
        # the reload path's try/except then keeps the current policy unchanged.
        policy = routing.get("usage_policy", [])
        if not resolve and policy is None:
            policy = []
        if not isinstance(policy, list):
            raise ValueError(f"{label}.routing.usage_policy must be a list of rules")
        for rule in policy:
            if not isinstance(rule, dict):
                raise ValueError(f"{label}.routing.usage_policy rules must each be a mapping")
            account = rule.get("account")
            prefer = rule.get("prefer")
            if not isinstance(account, str) or not account.strip():
                raise ValueError(f"{label}.routing.usage_policy rule must name an account (a provider/delegate)")
            if not isinstance(prefer, str) or not prefer.strip():
                raise ValueError(f"{label}.routing.usage_policy rule must name a prefer delegate")
            try:
                keep_under = int(rule.get("keep_under"))
            except (TypeError, ValueError):
                raise ValueError(
                    f"{label}.routing.usage_policy keep_under must be an integer percent between 1 and 100"
                )
            if not 1 <= keep_under <= 100:
                raise ValueError(
                    f"{label}.routing.usage_policy keep_under must be an integer percent between 1 and 100"
                )
            tiers = rule.get("tiers", [])
            if tiers is None:
                tiers = []
            if isinstance(tiers, str):
                tiers = [tiers]
            if not isinstance(tiers, (list, tuple)) or any(not isinstance(t, str) or not t.strip() for t in tiers):
                raise ValueError(f"{label}.routing.usage_policy tiers must be a list of tier names")
            for tier_name in tiers:
                if canonical_tier(tier_name.strip()) not in TIER_LADDER:
                    raise ValueError(f"{label}.routing.usage_policy references unknown tier {tier_name!r}")
            names.update([account.strip(), prefer.strip()])
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


def _no_policy():
    """The all-None usage_policy decision a rung carries when no rule steers it (#581 B7b)."""
    return {"rule_index": None, "reason": None, "account": None, "used_pct": None, "prefer": None}


def _policy_rule_view(rule, account_snaps):
    """One usage_policy rule echoed read-only for the view (#581 B7b): its account, target
    ``keep_under`` %, preferred delegate and (canonical) tiers — ``[]`` meaning every tier —
    plus the account's live used-% (``"unknown"`` when the snapshot can't be read, which the
    view renders as "rule inactive (usage unknown)", never an error, D4)."""
    account = str(rule.get("account") or "").strip()
    tiers: list[str] = []
    raw_tiers = rule.get("tiers")
    if isinstance(raw_tiers, str):
        raw_tiers = [raw_tiers]
    for t in raw_tiers or []:
        canon = canonical_tier(str(t).strip())
        if canon and canon not in tiers:
            tiers.append(canon)
    try:
        keep_under = int(rule.get("keep_under"))
    except (TypeError, ValueError):
        keep_under = rule.get("keep_under")
    room = _headroom_display(account_snaps.get(account)) if account else "unknown"
    used = "unknown" if room == "unknown" else 100 - room
    return {
        "account": account,
        "keep_under": keep_under,
        "prefer": str(rule.get("prefer") or "").strip(),
        "tiers": tiers,
        "account_used_pct": used,
    }


def _rung_pick(siblings, usage, *, headroom_on, prefer_at, exclusive, current, tier="", policy=None, snaps=None):
    """Which sibling the next dispatch opens on at this rung, and why (#578; #581 B7b).

    Uses the dispatch cursor snapshot and the loop's sibling preference functions, then layers
    the lowest-precedence ``usage_policy`` reorder on top with the loop's own ``policy_decision``
    (#581) — so ``starts_on``/``reason`` match what dispatch finally does, and the rung carries a
    ``policy`` sub-dict naming the rule that decided (``_no_policy`` when none did, including when
    an account's usage is unknown). This is a preview, not a reservation: concurrent dispatch may
    advance the cursor. ``snaps`` (siblings plus each rule's account) defaults to the sibling-only
    read, keeping the function host-free on a config alone.
    """
    from .loop import policy_decision, prefer_headroom_sibling, prefer_live_sibling, provider_is_down

    n = len(siblings)
    if n == 0:
        return {"starts_on": None, "reason": "no eligible delegate", "policy": _no_policy()}
    if snaps is None:
        snaps = {name: usage(name) for name in siblings}
    if headroom_on:
        idx = prefer_headroom_sibling(siblings, current, set(), snaps, prefer_at=prefer_at) % n
    else:
        idx = prefer_live_sibling(siblings, current, set()) % n
    if idx == current % n:
        reason = "exclusive" if (exclusive and n == 1) else "round-robin"
    elif provider_is_down(siblings[current % n]):
        reason = "down-marked"
    else:
        reason = f"headroom ≥{prefer_at}%"
    # #581 B7b: the usage_policy reorder is applied AFTER the headroom pick (``idx``), with the
    # headroom used-% threshold — None when headroom steering is off — exactly as the drive passes
    # it to apply_usage_policy. Same pure policy_decision the drive calls, so the view cannot drift.
    policy = policy or []
    decision = policy_decision(
        siblings, idx, tier, snaps, policy, spent=set(), prefer_at=prefer_at if headroom_on else None
    )
    out_policy = _no_policy()
    if decision["reason"] is not None:
        out_policy = {
            "rule_index": next((i for i, r in enumerate(policy) if r is decision["rule"]), None),
            "reason": decision["reason"],
            "account": decision["account"],
            "used_pct": decision["used_pct"],
            "prefer": decision["prefer"],
        }
        idx = decision["index"] % n
        reason = decision["reason"]
    return {"starts_on": siblings[idx], "reason": reason, "policy": out_policy}


def effective_routes(cfg, *, usage=None, current=None):
    """The effective routing a console operator sees (#578), per project.

    For each project it returns the exclusive-filtered coder ladder (one entry per
    ``TIER_LADDER`` tier, with the delegates left after the B2 filter and which sibling the
    next dispatch would open on, with the reason, and a ``policy`` sub-dict naming the
    ``usage_policy`` rule that steered that pick, if any — #581 B7b); the project's active
    ``usage_policy`` rules echoed read-only with each account's live used-% (``usage_policy`` /
    ``usage_policy_source``); the five named lifecycle paths — implement, fix (CI/review),
    escalate, review, helpers — with the uncovered ones marked; and, per delegate the ladder
    names, its live down mark (#420) and subscription headroom
    (#579, "unknown" when unread). ``usage(name) -> snapshot|None`` reads a delegate's
    ``graph.sdk.provider_usage`` snapshot; it defaults to all-unknown so the whole function
    is host-free and testable on a config alone. The sibling pick reuses the loop's pure
    functions with one read-only cursor snapshot; concurrent dispatch may change the next pick."""
    from .loop import dispatch_rung, knob_bool, peek_rung_cursor, provider_is_down

    usage = usage or (lambda name: None)
    cfg = normalize_tier_config(cfg)
    current = peek_rung_cursor() if current is None else current
    escalation_on = escalation_enabled(cfg)
    base_coder = str(cfg.get("coder") or "").strip()
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
        # #581 B7b: the project's active usage_policy rules, resolved exactly as the drive's
        # _usage_policy_for (project overrides board, including an explicit empty list; non-dict
        # entries dropped), echoed read-only with each account's live used-% so the view can show a
        # rule as active (account over its target) or inactive ("usage unknown" / under target —
        # never an error, D4). The per-rung decision below reuses the same snapshots.
        raw_policy = proj_routing.get("usage_policy", board_routing.get("usage_policy"))
        policy = [r for r in raw_policy if isinstance(r, dict)] if isinstance(raw_policy, list) else []
        if not is_implicit and "usage_policy" in proj_routing:
            policy_source = "project"
        elif board_routing.get("usage_policy") is not None:
            policy_source = "board"
        else:
            policy_source = "none"
        account_snaps = {a: usage(a) for a in dict.fromkeys(str(r.get("account") or "").strip() for r in policy) if a}
        policy_rules = [_policy_rule_view(r, account_snaps) for r in policy]
        filtered = {tier: [n for n in names if not exclusive or n in exclusive] for tier, names in full.items()}
        ladder = []
        for tier in TIER_LADDER:
            siblings = dispatch_rung(
                filtered, tier, escalation_on=escalation_on, exclusive=exclusive, base_coder=base_coder
            )
            siblings = [n for n in siblings if n and (not exclusive or n in exclusive)]
            snaps = {name: usage(name) for name in siblings}
            snaps.update({a: s for a, s in account_snaps.items() if a not in snaps})
            pick = _rung_pick(
                siblings,
                usage,
                headroom_on=headroom_on,
                prefer_at=prefer_at,
                exclusive=bool(exclusive),
                current=current,
                tier=tier,
                policy=policy,
                snaps=snaps,
            )
            ladder.append({"tier": tier, "delegates": siblings, **pick})
        entry_rung = next((r for r in ladder if r["delegates"]), None)
        # Every delegate the project's ladder names (pre-filter), in ladder order, with its
        # live down mark and headroom — marks/headroom are per delegate, not per rung.
        names_here: list[str] = []
        for tier in TIER_LADDER:
            for d in full[tier]:
                if d not in names_here:
                    names_here.append(d)
        if base_coder and base_coder not in names_here:
            names_here.append(base_coder)
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
                    (
                        f"opens on {entry_rung['starts_on']} at the {entry_rung['tier']} rung"
                        if escalation_on
                        else f"opens on base coder {entry_rung['starts_on']}"
                    )
                    if entry_rung
                    else "no eligible delegate — the card blocks"
                ),
            },
            {
                "path": "fix (CI/review)",
                "covered": True,
                "detail": (
                    "re-dispatches on the card's current rung (same exclusive-filtered delegates)"
                    if escalation_on
                    else "re-dispatches on the base coder, subject to the exclusive filter"
                ),
            },
            {
                "path": "escalate",
                "covered": True,
                "detail": (
                    "climbs the ladder above, after the exclusive filter"
                    if escalation_on
                    else "disabled — dispatch uses the base coder"
                ),
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
            "escalation_on": escalation_on,
            "headroom_on": headroom_on,
            "prefer_sibling_at": prefer_at,
            "usage_policy": policy_rules,
            "usage_policy_source": policy_source,
            "ladder": ladder,
            "paths": paths,
            "delegates": delegate_rows,
        }
    base = cfg.get("coder")
    if isinstance(base, str) and base.strip():
        all_delegates.add(base.strip())
    return {
        "cursor": current,
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
