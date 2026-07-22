"""Environment sanitization for spawned subprocesses (#78).

The loop runs *inside* a protoAgent host that identifies and authenticates this
agent through environment variables — the host's identity (``AGENT_NAME``), its
protoAgent wiring (``PROTOAGENT_*``) and its agent-to-agent credentials
(``A2A_*``). Those belong to the HOST agent, not to anything it shells out to.

Every subprocess the loop spawns — the gate preflight, the pre-PR
``local_gate_cmd``, the auto-fix ``format_cmd``, the ``coder.solve()`` seam's
acceptance-test (``verify``) run (#86), and (via the ACP adapter, which inherits
``os.environ``) the coder itself — would otherwise inherit that whole block
verbatim, handing a child process the host's identity and credentials. A
coder that reads ``A2A_*`` can impersonate the host on the bus; one that reads
``AGENT_NAME`` mis-reports who it is. So we strip the host-identity/credential
block from any environment handed to a child.

The strip is a **blacklist** (prefixes + exact names below). A deployment that
genuinely needs a specific variable to reach children keeps it via the
``env_passthrough`` **whitelist** config knob — the whitelist wins, so a listed
name survives even when it also matches the blacklist.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

# Host-identity / credential variables that must never leak into a child process.
# Prefix matches (any var whose name STARTS WITH one of these) plus a set of exact
# names. Kept deliberately small and explicit — this is a security boundary, not a
# heuristic. Extend only for variables that carry the HOST's identity/credentials.
ENV_BLACKLIST_PREFIXES: tuple[str, ...] = ("PROTOAGENT_", "A2A_")
ENV_BLACKLIST_EXACT: frozenset[str] = frozenset({"AGENT_NAME"})


def is_host_identity_var(name: str) -> bool:
    """True if ``name`` is a host-identity/credential variable that must be stripped
    from subprocess environments (before the ``env_passthrough`` whitelist is applied)."""
    return name in ENV_BLACKLIST_EXACT or name.startswith(ENV_BLACKLIST_PREFIXES)


def parse_env_passthrough(cfg: Mapping | None) -> tuple[str, ...]:
    """Read the ``env_passthrough`` whitelist from config.

    Accepts a list/tuple of names, or a single comma-/whitespace-separated string
    (so both ``["A2A_TOKEN", "AGENT_NAME"]`` and ``"A2A_TOKEN, AGENT_NAME"`` work).
    Returns a de-duplicated tuple, order preserved. Missing/blank ⇒ empty."""
    raw = (cfg or {}).get("env_passthrough") or ()
    if isinstance(raw, str):
        parts: Iterable[str] = raw.replace(",", " ").split()
    else:
        parts = raw
    seen: dict[str, None] = {}
    for name in parts:
        name = str(name).strip()
        if name:
            seen.setdefault(name, None)
    return tuple(seen)


def sanitized_env(
    passthrough: Iterable[str] = (),
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a child-process environment from ``environ`` (default: ``os.environ``)
    with the host-identity/credential block stripped.

    A variable is dropped when :func:`is_host_identity_var` matches it, UNLESS its
    name is in ``passthrough`` (the whitelist wins). Returns a fresh dict — the
    source mapping is never mutated — safe to hand to ``subprocess``'s ``env=``."""
    src = os.environ if environ is None else environ
    keep = set(passthrough or ())
    return {k: v for k, v in src.items() if k in keep or not is_host_identity_var(k)}


# NOTE: an in-place ``os.environ`` scrub shipped here in #81 and was REVERTED: it
# mutated the HOST server's own environment at loop start (the host lost its
# PROTOAGENT_HOME/instance identity — a graceful self-restart then re-execed as the
# default instance and died). Never mutate the host env: sanitizing the CODER's
# inherited environment needs an ``env=`` seam through the host ACP adapter instead
# (tracked upstream). ``sanitized_env`` above stays — it covers every subprocess the
# loop spawns directly (gate preflight, local_gate_cmd, format_cmd, and the
# coder.solve seam's acceptance-test verify subprocess — #86) without side effects.
