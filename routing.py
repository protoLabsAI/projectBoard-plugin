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
        if not isinstance(routing, dict) or set(routing) - {"exclusive"}:
            raise ValueError(f"{label}.routing must be a mapping containing only exclusive")
        exclusive = routing.get("exclusive", [])
        if not resolve and (exclusive is None or isinstance(exclusive, (str, tuple))):
            exclusive = rung_delegates(exclusive)
        if not isinstance(exclusive, list) or any(
            not isinstance(n, str) or (resolve and not n.strip()) for n in exclusive
        ):
            raise ValueError(f"{label}.routing.exclusive must be a list of delegate names")
        names.update(rung_delegates(exclusive))
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
