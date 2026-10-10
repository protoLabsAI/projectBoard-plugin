"""Auto-merge held because the external panel's pass at the head was INCOMPLETE.

The protoreview panel ("Vera") can finish a round with a finder down: the ``QA panel``
check concludes ``neutral`` ("Incomplete pass — not blocking … Hold:
``hold:incomplete-coverage``") and the review marker carries ``complete=false``. GitHub
treats ``neutral`` as passing, so the PR reads CLEAN and the board merged it — part of the
diff never reviewed (protoAgent#4101, bd-vbyd.13).

A project that sets ``require_complete_review: true`` holds the merge edge on that state
instead. This module is the hold's process state, in the same shape as
``merge_state_hold``: the merge edge records it, ``store.annotate_next_action`` projects
it as ``awaiting complete review (panel pass was incomplete)``, and a restart re-learns it
on the next merge poll. It also remembers which head a re-review was already requested
for, so a 30-second tick never re-posts the summon. The PR comment itself carries a
per-head marker as well, so a restart finds the earlier summon instead of posting again.
"""

from __future__ import annotations

import sys
import threading
import time
import types

NEXT_ACTION = "awaiting complete review (panel pass was incomplete)"
# The hidden marker that names the summon comment for one head. ``worktree.
# post_or_update_pr_comment`` finds an existing comment by it and does nothing when its body
# is unchanged — the durable half of "once per head".
SUMMON_MARKER_PREFIX = "<!-- project-board-complete-review-summon"
DEFAULT_SUMMON_HANDLE = "vera"

_SLOT = "project_board.review_coverage_hold::" + (__name__.rsplit(".", 1)[0] if "." in __name__ else __name__)
_holder = sys.modules.get(_SLOT)
if _holder is None:
    _holder = types.ModuleType(_SLOT)
    _holder.holds = {}
    _holder.lock = threading.Lock()
    _holder = sys.modules.setdefault(_SLOT, _holder)
_HOLDS: dict[str, dict] = _holder.holds
_LOCK: threading.Lock = _holder.lock


def reset_state() -> None:
    with _LOCK:
        _HOLDS.clear()


def summon_marker(head: str) -> str:
    return f"{SUMMON_MARKER_PREFIX} head={head} -->"


def summon_body(handle: str, head: str) -> str:
    """The comment that asks the panel to review the head again: one line, the same text for
    a given head. It must not vary between renders, or the idempotent update would PATCH the
    comment after a restart (and the panel answers ``edited`` comments too)."""
    return (
        f"@{handle} review — the pass at `{head[:12]}` was incomplete (a finder did not run, "
        "`hold:incomplete-coverage`); this project's board merges only after a complete pass."
    )


def hold_for(fid: str) -> dict | None:
    """The live hold on ``fid`` (``{head, signals, pr_url, since, summoned}``), or None."""
    with _LOCK:
        h = _HOLDS.get(fid)
        return dict(h, signals=list(h["signals"])) if h else None


def set_hold(fid: str, head: str, signals: list[str], pr_url: str) -> bool:
    """Record the hold. True when it is new or names a different head than before, so the
    caller logs and comments once per head rather than once per poll. A new head forgets
    the earlier summon."""
    with _LOCK:
        prior = _HOLDS.get(fid)
        same = prior is not None and prior["head"] == head
        _HOLDS[fid] = {
            "head": head,
            "signals": list(signals),
            "pr_url": pr_url,
            "since": prior["since"] if same else time.time(),
            "summoned": prior["summoned"] if same else "",
        }
        return not same


def summoned(fid: str, head: str) -> bool:
    """Whether a re-review was already requested for ``head`` on ``fid``."""
    with _LOCK:
        h = _HOLDS.get(fid)
        return bool(h) and bool(head) and h.get("summoned") == head


def mark_summoned(fid: str, head: str) -> None:
    with _LOCK:
        h = _HOLDS.get(fid)
        if h is not None and h["head"] == head:
            h["summoned"] = head


def clear_hold(fid: str) -> dict | None:
    with _LOCK:
        return _HOLDS.pop(fid, None)
