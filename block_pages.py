"""Host inbox lifecycle for board block pages (#735)."""

from __future__ import annotations

import functools
import logging
import threading
from urllib.parse import quote

from .failures import HOST_DISK_CLASS, is_host_disk

log = logging.getLogger("protoagent.plugins.project_board")


_page_lock = threading.RLock()


def serialized(method):
    @functools.wraps(method)
    def wrapped(*args, **kwargs):
        with _page_lock:
            return method(*args, **kwargs)

    return wrapped


# The whole lead of a host-disk page (#801): what is wrong and what to do, in plain words.
HOST_DISK_LEAD = (
    "This computer is almost out of disk space, so this card can't start. "
    "Free up space and it will start again on its own."
)


def _last_clause(raw: str) -> str:
    """The last line of an error, cut to the text after its final ": " — the part that
    names the cause ("…/bd-1.lock': No space left on device"). Empty when the reason is
    one clause, so there is nothing to tell apart from its start."""
    lines = [" ".join(line.split()) for line in str(raw or "").splitlines()]
    lines = [line.rstrip(".").rstrip() for line in lines if line.strip(". \t")]
    if not lines:
        return ""
    tail = lines[-1].rsplit(": ", 1)[-1].strip()
    return tail if len(lines) > 1 or tail != lines[-1] else ""


def _host_disk_page(fid: str, title: str, link: str) -> str:
    # Budget order: the lead and the link always survive; the card id goes last (the link
    # carries it too, so it is the first to go on a very long id); the title takes
    # whatever room is left, and is dropped when that is too little to read.
    link_md = f"[Open the card]({link})"
    last = f" {fid}" if len(f"{HOST_DISK_LEAD} {link_md} {fid}") <= 200 else ""
    room = 200 - len(f"{HOST_DISK_LEAD}  {link_md}{last}")
    if len(title) > room:
        title = title[: room - 1].rstrip() + "…" if room >= 12 else ""
    return f"{HOST_DISK_LEAD} {title} {link_md}{last}" if title else f"{HOST_DISK_LEAD} {link_md}{last}"


def page_text(fid: str, feature: dict) -> str:
    title = " ".join(str(feature.get("title") or "Untitled card").split())[:55]
    raw_reason = str(feature.get("blocked_reason") or "")
    reason = " ".join(str(feature.get("blocked_reason") or "").split()).rstrip(".")
    link = f"/plugins/project_board/board?card={quote(fid, safe='')}"
    if feature.get("blocked_class") == "needs-review" and reason.startswith(
        "Incomplete review coverage after re-review:"
    ):
        # Recovery evidence must retain the PR and exact head, even on long URLs.
        return f"{fid} is blocked: {reason}. [Open the card]({link})"
    hold = feature.get("blocked_class") == "preflight-hold"
    if feature.get("blocked_class") == HOST_DISK_CLASS or (reason and not hold and is_host_disk(reason)):
        # #801: a full disk is the host's problem, not the card's. Lead with the cause and
        # the fix; never with the card id, the class or the raw git error.
        return _host_disk_page(fid, title, link)
    if not reason:
        action = f"is blocked; [open the card]({link}) to review the reason and choose the next step."
        if hold:
            action = f"is waiting for its project check; [open the card]({link}) to review it."
        room = max(0, 200 - len(f"{fid}:  {action}"))
        return f"{fid}: {title[:room]} {action}"
    # The page states the ask itself (#gg9fa): the reason rides along in plain
    # words so the operator never has to open the card just to learn what is
    # needed. Budget order: the link always survives; the reason is trimmed with
    # an ellipsis; the title is dropped only when trimming would leave almost
    # nothing of the reason.
    # The trim takes the MIDDLE (#801): the last line of an error is usually the one
    # that says what went wrong, so its last clause is kept whole and the ellipsis
    # goes before it. A one-clause reason has no such tail and is trimmed at the end.
    state = "is waiting for its project check" if hold else "is blocked"
    tail = _last_clause(raw_reason)

    def page(t: str) -> tuple[int, bool, str]:
        head = f"{fid}: {t} {state}: " if t else f"{fid} {state}: "
        room = 200 - len(head) - len(f" [Open the card]({link})")
        front = room - len(f"… {tail}.")  # what is left for the start of the reason
        whole = True
        if len(reason) < room:
            said = f"{reason}."
        elif tail and front >= 0:
            said = f"{reason[:front].rstrip()}… {tail}."
        else:
            whole = not tail
            said = (tail or reason)[: max(0, room - 1)].rstrip() + "…"
        return room, whole, f"{head}{said} [Open the card]({link})"

    room, whole, text = page(title)
    if len(reason) >= room and (room < 24 or not whole):
        room, whole, text = page("")
    return text


def inbox_store():
    from inbox import InboxStore
    from . import loop

    path = loop._inbox_db_path()
    if path is None:
        return None
    return InboxStore(str(path), dedup_window_s=7 * 24 * 3600)


def pending(inbox):
    # Include claimed/accepted pages too: hiding them from list() is not retirement.
    return inbox.list(priority_floor="later", include_recovery_claimed=True, limit=2**31 - 1)


def card_id(row):
    key = str(row.get("dedup_key") or "")
    if row.get("source") != "project_board" or not key.startswith("blocked:"):
        return None
    # Legacy work-location alerts shared the blocked prefix. They survive completion.
    text = str(row.get("text") or "")
    if text.startswith("Work location ") or (text.startswith("Board card ") and " is held:" in text):
        return None
    return key.split(":")[1]


VERDICT_PREFIX = "panel-verdict:"
# The reason the reconcile's CLOSED edge blocks a card with (loop/reconcile.py).
PR_CLOSED_REASON = "PR closed without merging"


def verdict_key(fid: str) -> str:
    """The per-card dedup key of a "merge is yours" panel-verdict page (#786)."""
    return f"{VERDICT_PREFIX}{fid}"


def verdict_card_id(row):
    """The card a panel-verdict page is about, else None. Matches the per-card key
    ``panel-verdict:<fid>`` and the legacy per-head ``panel-verdict:<fid>:<head>`` (#786)."""
    key = str(row.get("dedup_key") or "")
    if row.get("source") != "project_board" or not key.startswith(VERDICT_PREFIX):
        return None
    return key[len(VERDICT_PREFIX) :].split(":")[0] or None


def retire_verdict_pages(fid, *, inbox=None, rows=None, keep=()) -> bool:
    """Mark the card's still-pending panel-verdict pages delivered — the host lists only
    undelivered rows, so a delivered page leaves Needs-you (#786). ``keep`` names row ids to
    leave pending. True when none (other than ``keep``) is left pending; False when the inbox
    could not be read or written, so the caller can retry or fall back. Never raises."""
    try:
        inbox = inbox or inbox_store()
        if inbox is None:
            return False
        ids = [
            r["id"]
            for r in (pending(inbox) if rows is None else rows)
            if verdict_card_id(r) == fid and r["id"] not in keep
        ]
        if ids:
            inbox.mark_delivered(ids)
            log.info("[project_board] %s: retired %d panel-verdict page(s)", fid, len(ids))
        return True
    except ImportError:
        return False  # Host without an inbox.
    except Exception:
        log.warning("[project_board] could not retire panel-verdict pages for %s; sweep will retry", fid, exc_info=True)
        return False


def verdict_settled(feature) -> bool:
    """Whether the card's PR is known merged or closed, so nothing is left for its
    panel-verdict page to ask. A missing record or failed read is not evidence."""
    if not feature:
        return False
    state = feature.get("board_state")
    if state == "done":
        return True
    return state == "blocked" and str(feature.get("blocked_reason") or "").startswith(PR_CLOSED_REASON)


def retire(feature, *, inbox=None, rows=None):
    if not feature or not feature.get("board_state") or feature.get("board_state") == "blocked":
        return
    try:
        inbox = inbox or inbox_store()
        if inbox is None:
            return
        ids = [r["id"] for r in (pending(inbox) if rows is None else rows) if card_id(r) == feature["id"]]
        if ids:
            inbox.mark_delivered(ids)
    except ImportError:
        pass  # Host without an inbox.
    except Exception:
        log.warning(
            "[project_board] could not retire block pages for %s; sweep will retry", feature.get("id"), exc_info=True
        )


def after_transition(method):
    """Only successful, fully committed transitions retire pages (never partial writes)."""

    @functools.wraps(method)
    def wrapped(*args, **kwargs):
        result = method(*args, **kwargs)
        if result and result.get("id") and result.get("board_state") != "blocked":
            # A re-block may have followed this method's final read. Serialize the
            # fresh read and cleanup with notification insertion, so its new page
            # cannot be retired using the transition's stale return value.
            with _page_lock:
                try:
                    retire(args[0].get_feature(result["id"]))
                except Exception:
                    log.warning("[project_board] cannot confirm page retirement; sweep will retry", exc_info=True)
        return result

    return wrapped


@serialized
def sweep(store):
    """Retry failed cleanup, including cards resolved before this process started."""
    try:
        inbox = inbox_store()
        if inbox is None:
            return
        rows = pending(inbox)
        for fid in {card_id(r) for r in rows} - {None}:
            try:
                feature = store.get_feature(fid)
                # Missing records and failed reads are not evidence of resolution.
                retire(feature, inbox=inbox, rows=rows)
            except Exception:
                log.debug("[project_board] cannot check block page for %s", fid, exc_info=True)
        # #786: panel-verdict pages whose PR merged or closed — a retirement the reconcile
        # edge could not complete, or a page left by a card finished before this existed.
        for fid in {verdict_card_id(r) for r in rows} - {None}:
            try:
                if verdict_settled(store.get_feature(fid)):
                    retire_verdict_pages(fid, inbox=inbox, rows=rows)
            except Exception:
                log.debug("[project_board] cannot check panel-verdict page for %s", fid, exc_info=True)
    except ImportError:
        pass  # Host without an inbox.
    except Exception:
        log.warning("[project_board] block page cleanup failed; sweep will retry", exc_info=True)
