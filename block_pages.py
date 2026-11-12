"""Host inbox lifecycle for board block pages (#735)."""

from __future__ import annotations

import functools
import logging
import threading
from urllib.parse import quote

log = logging.getLogger("protoagent.plugins.project_board")


_page_lock = threading.RLock()


def serialized(method):
    @functools.wraps(method)
    def wrapped(*args, **kwargs):
        with _page_lock:
            return method(*args, **kwargs)

    return wrapped


def page_text(fid: str, feature: dict) -> str:
    title = " ".join(str(feature.get("title") or "Untitled card").split())[:55]
    link = f"/plugins/project_board/board?card={quote(fid, safe='')}"
    action = f"is blocked; [open the card]({link}) to review the reason and choose the next step."
    if feature.get("blocked_class") == "preflight-hold":
        action = f"is waiting for its project check; [open the card]({link}) to review it."
    room = max(0, 200 - len(f"{fid}:  {action}"))
    return f"{fid}: {title[:room]} {action}"


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
    except ImportError:
        pass  # Host without an inbox.
    except Exception:
        log.warning("[project_board] block page cleanup failed; sweep will retry", exc_info=True)
