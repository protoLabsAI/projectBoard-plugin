"""Why an auto_merge card's PR is not CLEAN on GitHub (#495).

The auto-merge edge merges only on ``mergeStateStatus == CLEAN``. An ``UNSTABLE`` PR (a
non-required check pending or failing) or a ``BLOCKED`` one (a required check or review
not satisfied) is correctly refused, but the card's ``next_action`` used to say
``auto-merge pending`` for as long as that lasted — hours, on a PR whose ``QA panel``
check sat ``in_progress`` because of unresolved review threads. Nothing on the board said
the PR was the thing stuck.

The merge edge already reads ``mergeStateStatus`` (``worktree.pr_merge_info``) and the
pass already read the head's checks (``worktree.pr_review_state``), so no new GitHub call
is made: the edge records what it saw here, and ``store.annotate_next_action`` projects it
as ``held: PR not clean on GitHub (UNSTABLE) — QA panel: in progress``. Process state,
like the release-freeze holds; a restart re-learns it on the next merge poll.
"""

from __future__ import annotations

import sys
import threading
import time
import types

# The statuses the hold names. BEHIND / DIRTY are the rebase edge's; UNKNOWN is GitHub
# still computing and "" an unreadable PR, both transient.
HELD_STATUSES = ("UNSTABLE", "BLOCKED")

# A check run that has finished in one of these did not pass; one not COMPLETED is pending.
_CHECK_NOT_OK = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
_STATUS_NOT_OK = {"PENDING", "EXPECTED", "FAILURE", "ERROR"}

_SLOT = "project_board.merge_state_hold::" + (__name__.rsplit(".", 1)[0] if "." in __name__ else __name__)
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


def outstanding_checks(view: dict | None) -> list[str]:
    """``["QA panel: in progress", "test: failure"]`` — every check on the head that is
    not passing, from a ``pr_review_state`` payload (its ``statusCheckRollup``). Empty for
    no payload, or when every check passed. Order kept, names deduplicated."""
    out: list[str] = []
    seen: set[str] = set()
    for c in (view or {}).get("statusCheckRollup") or []:
        if not isinstance(c, dict):
            continue
        if str(c.get("__typename") or "") == "StatusContext":
            name = str(c.get("context") or "").strip()
            state = str(c.get("state") or "").upper()
            if state not in _STATUS_NOT_OK:
                continue
        else:
            name = str(c.get("name") or "").strip()
            status = str(c.get("status") or "").upper()
            if status and status != "COMPLETED":
                state = status
            else:
                state = str(c.get("conclusion") or "").upper()
                if state not in _CHECK_NOT_OK:
                    continue
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(f"{name}: {state.replace('_', ' ').lower()}")
    return out


def hold_for(fid: str) -> dict | None:
    """The live hold on ``fid`` (``{status, checks, pr_url, since}``), or None."""
    with _LOCK:
        h = _HOLDS.get(fid)
        return dict(h, checks=list(h["checks"])) if h else None


def set_hold(fid: str, status: str, checks: list[str], pr_url: str) -> bool:
    """Record the hold. True when it is new or names a different blocker than before, so
    the caller logs once per blocker rather than once per poll."""
    with _LOCK:
        prior = _HOLDS.get(fid)
        _HOLDS[fid] = {
            "status": status,
            "checks": list(checks),
            "pr_url": pr_url,
            "since": (prior or {}).get("since") or time.time(),
        }
        return prior is None or prior["status"] != status or prior["checks"] != list(checks)


def clear_hold(fid: str) -> dict | None:
    with _LOCK:
        return _HOLDS.pop(fid, None)
