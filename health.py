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


def publish_preflight(state: dict, dirty: dict) -> None:
    """Called by the loop after each preflight pass with its two live maps:
    ``state`` (project -> True | reason-string | None) and ``dirty``
    (project -> why the checkout wasn't at base)."""
    _health["preflight"] = {
        "held": {n: r for n, r in state.items() if isinstance(r, str)},
        "dirty": dict(dirty),
    }


def preflight_snapshot() -> dict:
    """``{held: {project: reason}, dirty: {project: why}}`` — held projects are the ones
    whose ready work is frozen behind a red gate. Empty before the first preflight pass
    (and on a board whose loop is off), which reads correctly as "nothing held"."""
    snap = _health.get("preflight") or {}
    return {"held": dict(snap.get("held") or {}), "dirty": dict(snap.get("dirty") or {})}
