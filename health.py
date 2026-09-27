"""Loop-health facts the API can read without reaching into the running loop (#255).

The gate preflight is the board's most consequential piece of hidden state: one project
whose gate is red fail-closes THAT project's dispatch, and because the held cards drop
out of the ready scan the only thing an operator sees is a board that quietly stops
picking work up — ``claim_decision {"selected": []}``, tick after tick, with the reason
buried in the log. `/status` is where the board page already looks, so the loop publishes
its per-project preflight verdicts here and the route serves them.

Lives in its own module, attached to a process-stable ``sys.modules`` slot exactly like
coder_seam's monitor buffer (#178): the loop and the API routers are separate module
instances after a plugin reload, and a plain module global would leave the reader holding
an empty dict while the writer fills its own copy.
"""

from __future__ import annotations

import sys
import types

# Not prefixed with the plugin package's own name, so a host that purges `sys.modules`
# by package prefix on reload cannot purge the slot (see coder_seam._PROGRESS_SLOT_PREFIX).
_SLOT_PREFIX = "project_board.loop_health::"


def _slot_name() -> str:
    pkg = __name__.rsplit(".", 1)[0] if "." in __name__ else __name__
    return _SLOT_PREFIX + pkg


def _attach() -> dict:
    name = _slot_name()
    holder = sys.modules.get(name)
    prev = getattr(holder, "health", None)
    if isinstance(prev, dict):
        return prev
    holder = types.ModuleType(name)
    holder.__doc__ = "Process-stable holder for project_board's loop-health facts (#255)."
    holder.health = {}
    holder = sys.modules.setdefault(name, holder)  # atomic install — see store._br_lock
    return holder.health


_health: dict = _attach()


def publish_preflight(state: dict, dirty: dict, *, slow: dict | None = None, oracle: dict | None = None) -> None:
    """Called by the loop after each preflight pass with its live maps:
    ``state`` (project -> True | reason-string | None), ``dirty``
    (project -> why the checkout wasn't at base), ``slow`` (project -> {cmd, timeout_s,
    sha}: a preflight command that can't finish inside ``preflight_timeout_s``, #456) and
    ``oracle`` (project -> why its coder.solve() oracle is unwinnable, #459)."""
    _health["preflight"] = {
        "held": {n: r for n, r in state.items() if isinstance(r, str)},
        "dirty": dict(dirty),
        "slow": {n: dict(v) for n, v in (slow or {}).items()},
        "unwinnable_oracle": dict(oracle or {}),
    }


def preflight_snapshot() -> dict:
    """``{held: {project: reason}, dirty: {project: why}, slow: {project: {cmd,
    timeout_s, sha}}, unwinnable_oracle: {project: why}}``. Held projects are the ones
    whose ready work is frozen behind a red gate. Slow ones run a preflight that can't
    finish in its timeout, so their verdict is only "indeterminate" (#456). An unwinnable
    oracle means that project's cards skip coder.solve() (#459). Empty before the first
    preflight pass (and on a board whose loop is off), which reads correctly as "nothing
    held"."""
    snap = _health.get("preflight") or {}
    return {
        "held": dict(snap.get("held") or {}),
        "dirty": dict(snap.get("dirty") or {}),
        "slow": {n: dict(v) for n, v in (snap.get("slow") or {}).items()},
        "unwinnable_oracle": dict(snap.get("unwinnable_oracle") or {}),
    }


def advisory_hint() -> str:
    """One operator line for the setup advisories: the projects whose preflight can't
    finish in time (#456) and whose coder.solve() oracle is unwinnable (#459). "" when
    there are none."""
    snap = preflight_snapshot()
    parts = []
    for name, info in sorted(snap["slow"].items()):
        parts.append(
            f"project {name!r}: the preflight (`{info.get('cmd', '')}`) can't finish inside "
            f"preflight_timeout_s={info.get('timeout_s', 0):.0f}s, so it gives no verdict — set a cheap "
            "`preflight_cmd` (lint + an import check)"
        )
    for name, why in sorted(snap["unwinnable_oracle"].items()):
        parts.append(f"project {name!r}: coder.solve() is off — {why}")
    return "; ".join(parts)


def publish_base_checkouts(results: dict) -> None:
    """Called by the health sweep with ``{project: {repo, base, state, behind, detail,
    checked_at}}`` — how each project's MAIN checkout compares with ``origin/<base>`` after
    the sweep's refresh (#452; see ``worktree.refresh_base_checkout``)."""
    _health["base_checkouts"] = {n: dict(r) for n, r in (results or {}).items()}


def base_checkouts_snapshot() -> dict:
    """``{project: {repo, base, state, behind, detail, checked_at}}`` — empty before the
    first sweep, on a board whose loop is off, or with ``base_refresh: false``."""
    return {n: dict(r) for n, r in (_health.get("base_checkouts") or {}).items()}


def publish_orphaned_cards(cards: list) -> None:
    """Called by the health sweep with the live cards whose project label no longer
    resolves (#454; see ``projects.orphaned_cards``)."""
    _health["orphaned_cards"] = [dict(c) for c in cards or ()]


def orphaned_cards_snapshot() -> list:
    """The orphaned cards the last sweep found — ``[]`` before the first sweep."""
    return [dict(c) for c in _health.get("orphaned_cards") or ()]


def publish_claim_stall(reason: str) -> None:
    """Called by the loop when its claim-stall signal changes (#462): the reason the board
    is idle with ready work and a free slot, or "" once the claim scan runs again."""
    _health["claim_stall"] = str(reason or "")


def claim_stall_hint() -> str:
    """The operator-facing line for a claim stall, "" when there is none (or no loop). It
    rides ``/status`` as a setup/health gap: a board that stops claiming says why."""
    reason = str(_health.get("claim_stall") or "")
    if not reason:
        return ""
    return (
        f"the board loop is not claiming ready work: {reason}. A per-card gate or review may be hung "
        "(see the agent log for that phase); `board_dispatch` runs a claim scan now"
    )
