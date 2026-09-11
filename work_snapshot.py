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
publishes a snapshot and the provider returns that, already in memory.

**How stale it can be, stated exactly (#401).** It used to be refreshed only by the health
sweep, every 300s, and at the START of the sweep, before the sweep's own transitions.
Nothing in it said how old it was. The PM unblocked one card and blocked another with its
own tools, and on its next turn its working state still showed both in their old states.
It believed that over its own tool results and planned against a board that no longer
existed. Now:

- every store write that can change what a card projects as bumps a process-wide board
  revision (``note_board_write``, from ``BeadsBoard._run``). Every write in this process
  goes through there: the agent's tools, the operator's routes, the loop's own edges. So
  does a live config change the hints read (``mark_stale``);
- a snapshot records the revision it was READ at, and while the board has moved past it
  the provider leads with a STALE line naming when it was taken;
- the loop's refresher republishes after a change, at most every ``MIN_INTERVAL_S``, and in
  any case at least every ``MAX_AGE_S``. It runs apart from the claim tick, so a loop paused
  at its setup gate still refreshes, and it backs off while reads fail.

So the snapshot is either marked STALE (a change this process made has not been picked up
yet) or at most ``MAX_AGE_S`` (+ one poll) old. The one thing the revision cannot see is a
writer outside this process, such as an operator's hand-run ``br``. Only that age bound
covers it. A snapshot older than ``STALE_AFTER_S`` (the refresher is failing, or has stopped)
is marked STALE too. With the loop disabled nothing publishes and the section stays empty.
That is honest: a board nothing is driving is not a live commitment.

The state lives in a process-stable ``sys.modules`` slot (the ``store._br_lock`` / #178
pattern). A plugin reload re-imports this module, and module globals would then give the
store (bumping) and the provider (reading) two different counters.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import types
from datetime import datetime, timezone

log = logging.getLogger("protoagent.plugins.project_board")

# Board states worth showing as "open work": everything the board still owes an outcome on.
# Terminal states (done/cancelled) and backlog (not yet promoted through the Ready gate) are
# deliberately excluded — the block is what the agent is ON THE HOOK FOR right now. The one
# backlog exception is a card the board names a step for (#406); see `publish`.
LIVE_STATES = ("ready", "in_progress", "in_review", "blocked")

# Hard cap on what the loop publishes, independent of the host's own per-provider cap. A
# board with 90 live cards must not hand the host a 90-item list to trim every turn.
MAX_ITEMS = 12

# The freshness bounds (#401). A change is picked up at most MIN_INTERVAL_S after the last
# refresh (changes are coalesced: a burst of writes costs one read). A quiet board is
# re-read every MAX_AGE_S, which is what bounds a writer this process can't see. A snapshot
# older than STALE_AFTER_S means the refresher is failing or stopped, and it says so.
MIN_INTERVAL_S = 5.0
MAX_AGE_S = 60.0
STALE_AFTER_S = 2 * MAX_AGE_S

_SLOT_PREFIX = "project_board.work_snapshot::"


def _state():
    """The process-stable holder: ``snapshot`` (cards, taken_at, revision), ``revision``
    and its ``lock``. Installed atomically (``setdefault``, see ``store._br_lock``)."""
    pkg = __name__.rsplit(".", 1)[0] if "." in __name__ else __name__
    name = _SLOT_PREFIX + pkg
    holder = sys.modules.get(name)
    if holder is None:
        holder = types.ModuleType(name)
        holder.__doc__ = "Process-stable holder for project_board's working-state snapshot (#401) — data, not code."
        holder.snapshot = ([], None, 0)
        holder.revision = 0
        holder.lock = threading.Lock()
        holder = sys.modules.setdefault(name, holder)
    return holder


def note_board_write() -> None:
    """Record that the board changed. ``BeadsBoard._run`` calls it after every ``br`` write
    that can move a card's state, title or labels. The call sits in a ``finally``, so it
    also covers a write that raised or timed out and may still have landed."""
    state = _state()
    with state.lock:
        state.revision += 1


def mark_stale() -> None:
    """Record that what the snapshot RENDERS changed without a board write: a live config
    reload of a knob the hints read (auto_merge, review_gate, …)."""
    note_board_write()


def board_revision() -> int:
    """The board revision right now. Read it BEFORE reading the board you publish."""
    return _state().revision


def taken_at() -> float | None:
    """When the current snapshot was read (``time.time()``), or None before the first."""
    return _state().snapshot[1]


def publish(features, *, revision: int | None = None) -> None:
    """Called by the loop with the live board projection. Keeps only the live states (and
    a backlog card the board names a step for, #406), orders them the way the board reasons
    about urgency (blocked first — a card that cannot clear itself is the one the agent most
    needs to see, then in_review, in_progress, ready, and that backlog card last), and trims
    to ``MAX_ITEMS``. Never raises: a bad snapshot must not break the refresher.

    ``revision`` is the ``board_revision()`` read BEFORE ``features`` was. A write that lands
    while the board is being read may or may not be in those rows, so the snapshot counts as
    stale from that write on, and never claims a state it might not include. Omitted means
    the revision now (for a caller that reads with no concurrent writers)."""
    if revision is None:
        revision = board_revision()
    # PER-ITEM, not all-or-nothing. Building the whole list inside one try meant a single
    # malformed card (a None in the list, a non-dict row) aborted the entire update and left
    # the snapshot holding its PREVIOUS value — so the agent kept being shown a stale board
    # indefinitely, with nothing in the working state to say so. One bad card must cost that
    # card, not the whole view.
    # A backlog card is normally not on the hook — except one the board says has a step
    # owed (#406): every dependency it waited on has closed, and nothing promotes it but
    # the agent reading this. It ranks LAST: the list is capped, and a dozen stranded cards
    # must never push out the in-flight work (a PR awaiting merge, a build running) the
    # agent is actually carrying. They fill whatever the live cards leave.
    rank = {state: i for i, state in enumerate(("blocked", "in_review", "in_progress", "ready", "backlog"))}
    live = []
    skipped = 0
    for f in features or []:
        try:
            state = str(f.get("board_state") or "")
            if state in LIVE_STATES or (state == "backlog" and str(f.get("next_action") or "").strip()):
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
    _state().snapshot = (built, time.time(), revision)


def needs_refresh() -> bool:
    """Whether the board changed since the snapshot was taken, or no snapshot was ever taken.
    In memory, no I/O. The refresher adds the age bound (``MAX_AGE_S``) and its coalescing."""
    _cards, taken, revision = _state().snapshot
    return taken is None or revision != board_revision()


def provider() -> list:
    """The registered work provider: an in-memory read, no I/O, no lock.

    The cards, as dicts. A snapshot that is stale is led by ONE plain-string line saying so
    and naming when it was taken: the board changed since it was read, or it is older than
    ``STALE_AFTER_S``. It is the first item so a host that caps a provider's items can never
    trim the warning off. Once the loop has republished, the cards are current and the line
    is gone."""
    cards, taken, revision = _state().snapshot
    if taken is None:
        return list(cards)
    if revision == board_revision() and time.time() - taken <= STALE_AFTER_S:
        return list(cards)
    stamp = datetime.fromtimestamp(taken, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [
        f"STALE — the board may have changed since this list was taken ({stamp}); "
        "re-read with board_list / board_get_feature before acting on it",
        *cards,
    ]


def reset() -> None:
    """Drop the snapshot — used when the loop stops, so a stopped board stops advertising
    work it is no longer driving."""
    _state().snapshot = ([], None, 0)
