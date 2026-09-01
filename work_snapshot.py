"""The board's contribution to the agent's own ``<working_state>`` block (protoAgent ADR
0079's Observe step, via ``registry.register_work_provider``).

Why this exists: the host injects ``{active goal + plan · open tasks · live watches ·
pending schedules}`` into every turn so the agent OBSERVES its commitments instead of
polling for them — and it read four CORE stores, so the board was invisible to it. An agent
whose entire job is this board could therefore report itself idle while one of its own
cards sat stalled, because the block it treats as "your live commitments" could not see the
board. This closes that.

**The host calls a provider inline on EVERY turn, so it must be cheap and non-blocking.**
Every board read is a ``br`` subprocess (``BoardStore._run`` is THE blocking seam), which is
exactly what a provider may not do. So the provider never touches the store: the loop
publishes a snapshot on its periodic sweep and the provider returns that, already in memory.

Consequences of that choice, both deliberate:

- The snapshot is up to one sweep interval stale (``health_sweep_interval_s``, 300s by
  default). For a "what am I on the hook for" hint that is fine — the board API and the
  board tools remain the authoritative read.
- With the loop disabled nothing publishes, so the section is empty. That is honest: a
  board nothing is driving is not a live commitment.

Module-level rather than loop-instance state so a plugin reload swapping the loop object
does not strand the registered provider (it stays bound to this module's accessor).
"""

from __future__ import annotations

import logging

log = logging.getLogger("protoagent.plugins.project_board")

# Board states worth showing as "open work": everything the board still owes an outcome on.
# Terminal states (done/cancelled) and backlog (not yet promoted through the Ready gate) are
# deliberately excluded — the block is what the agent is ON THE HOOK FOR right now.
LIVE_STATES = ("ready", "in_progress", "in_review", "blocked")

# Hard cap on what the loop publishes, independent of the host's own per-provider cap. A
# board with 90 live cards must not hand the host a 90-item list to trim every turn.
MAX_ITEMS = 12

_SNAPSHOT: list[dict] = []


def publish(features) -> None:
    """Called by the loop's sweep with the live board projection. Keeps only the live
    states, orders them the way the board reasons about urgency (blocked first — a card
    that cannot clear itself is the one the agent most needs to see, then in_review,
    in_progress, ready), and trims to ``MAX_ITEMS``. Never raises: a bad snapshot must not
    break the sweep."""
    global _SNAPSHOT
    # PER-ITEM, not all-or-nothing. Building the whole list inside one try meant a single
    # malformed card (a None in the list, a non-dict row) aborted the entire update and left
    # `_SNAPSHOT` holding its PREVIOUS value — so the agent kept being shown a stale board
    # indefinitely, with nothing in the working state to say so. One bad card must cost that
    # card, not the whole view.
    rank = {state: i for i, state in enumerate(("blocked", "in_review", "in_progress", "ready"))}
    live = []
    skipped = 0
    for f in features or []:
        try:
            if str(f.get("board_state") or "") in LIVE_STATES:
                live.append(f)
        except Exception:  # noqa: BLE001 — not a dict / no .get: drop this row only
            skipped += 1
    try:
        live.sort(key=lambda f: (rank.get(str(f.get("board_state")), 99), str(f.get("id") or "")))
    except Exception:  # noqa: BLE001 — an unsortable row must not cost the snapshot
        log.warning("[project_board] work snapshot sort failed — publishing unsorted", exc_info=True)
    built = []
    for f in live[:MAX_ITEMS]:
        try:
            built.append(
                {
                    "id": str(f.get("id") or ""),
                    "title": str(f.get("title") or "")[:90],
                    "state": str(f.get("board_state") or ""),
                    # `next_action_hint` is the board's own one-line "what unsticks this" —
                    # reuse it rather than inventing a second phrasing for the same thing.
                    "hint": str(f.get("next_action_hint") or "").strip(),
                }
            )
        except Exception:  # noqa: BLE001
            skipped += 1
    if skipped:
        log.warning("[project_board] work snapshot: skipped %d malformed feature row(s)", skipped)
    _SNAPSHOT = built


def provider() -> list[dict]:
    """The registered work provider: an in-memory read, no I/O, no lock."""
    return list(_SNAPSHOT)


def reset() -> None:
    """Drop the snapshot — used when the loop stops, so a stopped board stops advertising
    work it is no longer driving."""
    global _SNAPSHOT
    _SNAPSHOT = []
