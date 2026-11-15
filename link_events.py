"""Tell the host a card finished: publish ``task.link.closed`` (#765).

Core ties tasks and directives to the work that delivers them through ``links``
(protoAgent ADR 0129 Q1). For a board card (``bd-…``) the host learns the card
finished only from this topic: ``tasks.autoclose`` marks the task's link done
(closing the task once every link is done) and ``graph.directive_autofulfil``
fulfils a directive whose cards finished. Until this module nothing published it,
so a card-linked task never resolved and ADR 0129 D9 counted it as driven forever.

Two halves, like :mod:`block_pages`:

* :func:`after_terminal` wraps the store's terminal edges (merge, manual done,
  task verification, cancel). After a SUCCESSFUL transition whose result is
  ``done`` or ``cancelled`` it publishes ``{"ref": <card id>, "reason": …}``.
  Publishing happens after the store write and is best-effort: a host without a
  bus, or a bus that raises, never fails the transition.
* :func:`sweep` is the backfill and the retry: it reads the host's open tasks,
  and for every ``bd-`` link still unresolved whose card is terminal, publishes
  the same event. That covers cards that went terminal before this shipped, cards
  closed outside these methods (a hand ``br close``), and a publish that was lost
  because the host bus was not wired yet. The core handlers are idempotent, so a
  repeat is harmless.
"""

from __future__ import annotations

import functools
import logging
import re
import time
from typing import Any, Callable

log = logging.getLogger("protoagent.plugins.project_board")

#: The host's generic inbound contract (protoAgent ``tasks.autoclose.TASK_LINK_CLOSED_TOPIC``).
TOPIC = "task.link.closed"

_TERMINAL = ("done", "cancelled")

#: Minimum seconds between backfill passes. The reconcile tick is 30–60 s; a pass
#: reads the task store and runs one ``br show`` per unresolved card link, so it is
#: throttled to the cadence of the host's own link sweeps (5 minutes).
SWEEP_INTERVAL_S = 300.0

_last_sweep = 0.0

_PR_NUMBER = re.compile(r"/pull/(\d+)")


def reason_for(feature: dict) -> str:
    """The close reason the host records on the task (``"bd-x merged (#12)"``)."""
    if feature.get("board_state") == "cancelled":
        return "cancelled"
    m = _PR_NUMBER.search(str(feature.get("pr_url") or ""))
    return f"merged (#{m.group(1)})" if m else "done"


def _default_publish() -> Callable[[str, dict], Any] | None:
    try:
        from graph.plugins.host import HOST
    except ImportError:
        return None
    return HOST.publish


def publish_closed(feature: dict | None, *, publish: Callable[[str, dict], Any] | None = None) -> bool:
    """Publish ``task.link.closed`` for ``feature`` if it is terminal. Returns True when
    an event was handed to the bus. Never raises."""
    if not feature or feature.get("board_state") not in _TERMINAL or not feature.get("id"):
        return False
    try:
        pub = publish or _default_publish()
        if pub is None:
            return False
        pub(TOPIC, {"ref": str(feature["id"]), "reason": reason_for(feature)})
        return True
    except Exception:  # noqa: BLE001 — the transition already committed; the sweep retries
        log.warning("[project_board] could not publish %s for %s", TOPIC, feature.get("id"), exc_info=True)
        return False


def after_terminal(method):
    """Publish after a store method returns a terminal card. A method that raises
    published nothing (its write did not land, or its outcome is unknown)."""

    @functools.wraps(method)
    def wrapped(*args, **kwargs):
        result = method(*args, **kwargs)
        if isinstance(result, dict):
            publish_closed(result)
        return result

    return wrapped


def _open_card_refs(tasks_store: Any) -> list[str]:
    """Unresolved ``bd-`` link refs held by the host's open tasks, de-duplicated."""
    from tasks.store import parse_links

    refs: list[str] = []
    seen: set[str] = set()
    for row in tasks_store.list(include_closed=False):
        for entry in parse_links(row):
            ref = str(entry.get("ref") or "")
            if ref.startswith("bd-") and entry.get("state") != "done" and ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return refs


def _default_tasks_store():
    from tasks.store import TaskStore

    # Same resolution as the host's own store (TASKS_DB_PATH, else the instance's
    # tasks/issues.db), so this reads the tasks the host's handlers will resolve.
    return TaskStore()


def sweep(
    store,
    *,
    tasks_store: Any = None,
    publish: Callable[[str, dict], Any] | None = None,
    now: float | None = None,
    force: bool = False,
) -> int:
    """Backfill pass: publish for each terminal card an open host task still links.
    Returns the number of events published. Throttled to :data:`SWEEP_INTERVAL_S`
    unless ``force``. Never raises: a host without a task store is a no-op, and one
    unreadable card never stops the pass."""
    global _last_sweep
    t = time.monotonic() if now is None else now
    if not force and _last_sweep and t - _last_sweep < SWEEP_INTERVAL_S:
        return 0
    _last_sweep = t
    try:
        ts = tasks_store if tasks_store is not None else _default_tasks_store()
        refs = _open_card_refs(ts)
    except ImportError:
        return 0  # host without a task store
    except Exception:  # noqa: BLE001
        log.warning("[project_board] link backfill: could not read host tasks", exc_info=True)
        return 0
    published = 0
    for ref in refs:
        try:
            feature = store.get_feature(ref)
        except Exception:  # noqa: BLE001 — an unreadable card is not evidence it finished
            log.debug("[project_board] link backfill: cannot read %s", ref, exc_info=True)
            continue
        if publish_closed(feature, publish=publish):
            published += 1
    if published:
        log.info("[project_board] link backfill: published %s for %d terminal card(s)", TOPIC, published)
    return published
