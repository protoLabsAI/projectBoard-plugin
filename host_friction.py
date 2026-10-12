"""The board loop's failures, recorded in the host agent's friction ledger.

The loop fails outside any agent turn: a coder drive that hangs on a dropped model
connection, an epic base missing locally ("no commits vs base"), a worktree that vanished,
a preflight hold, a phase that crashed. The agent's own ``record_friction`` tool never sees
any of it, so protoEngineer's friction review went quiet through two weeks of exactly that
trouble. The host's ``graph.sdk.record_friction`` seam writes into the same ledger, and this
module is the loop's only door to it.

Three rules:

- **Stable summaries.** The ledger groups by ``(kind, summary)``, so a summary never carries
  an id, a sha, a URL, a path or a count; those go in ``detail``. :func:`stable_phrase` turns
  a free-text reason into such a summary.
- **No flood.** A failure can repeat every tick. Each ``key`` (by default the summary) is
  recorded at most once per :data:`WINDOW_S`, and at most :data:`MAX_PER_WINDOW` records of
  any kind go out per window.
- **Never in the way.** A host without the seam (an older core) or with the friction plugin
  off makes every call a no-op, and nothing here ever raises into the loop.
"""

from __future__ import annotations

import logging
import re
import threading
import time

log = logging.getLogger(__name__)

#: The plugin id stamped on every record (``plugin:project_board`` in the ledger).
SOURCE = "project_board"
#: Every loop failure is harness friction: the board's machinery got in the way.
KIND = "harness"
#: The same key is recorded at most once per window.
WINDOW_S = 3600.0
#: At most this many records of any key go out per window…
MAX_PER_WINDOW = 60
#: …and at most this many under one summary, so one failure that hits many cards (or a
#: key that churns) can never use up the window and silence every other kind.
MAX_PER_SUMMARY = 10

_lock = threading.Lock()
_last: dict[str, float] = {}  # key -> monotonic time it was last recorded
_sent: list[tuple[float, str]] = []  # (monotonic time, summary) of records in the current window


def reset() -> None:
    """Forget every rate-limit stamp (tests, and a fresh loop)."""
    with _lock:
        _last.clear()
        _sent.clear()


def _seam():
    """The host's ``record_friction`` callable, or None (no host, an older core)."""
    try:
        from graph import sdk
    except Exception:  # noqa: BLE001 — host-free (tests, a standalone install)
        return None
    return getattr(sdk, "record_friction", None)


def _admit(key: str, summary: str, now: float) -> bool:
    """Whether ``key`` may be recorded now, stamping it when it may: once per key per
    window, at most ``MAX_PER_SUMMARY`` per summary and ``MAX_PER_WINDOW`` in all."""
    with _lock:
        last = _last.get(key)
        if last is not None and now - last < WINDOW_S:
            return False
        _sent[:] = [(t, s) for t, s in _sent if now - t < WINDOW_S]
        if len(_sent) >= MAX_PER_WINDOW or sum(1 for _t, s in _sent if s == summary) >= MAX_PER_SUMMARY:
            return False
        _last[key] = now
        _sent.append((now, summary))
        return True


def _redact(text: str) -> str:
    """The preflight's secret scrubber (the same one the hold comments use): a detail
    carries gate output and exception text, and the ledger feeds issue drafts."""
    try:
        from .loop.preflight import _redact_gate_output
    except Exception:  # noqa: BLE001 — never lose the record over its scrubber
        return text
    try:
        return _redact_gate_output(text)
    except Exception:  # noqa: BLE001
        return text


def record(summary: str, detail: str = "", *, severity: str = "minor", key: str = "") -> bool:
    """Record one loop failure in the host's friction ledger. Returns True when it was
    handed to the host and accepted, False when it was rate-limited, the host has no seam
    or no friction plugin, or anything went wrong. ``key`` scopes the rate limit (default:
    the summary); pass e.g. ``f"{summary}|{fid}"`` to let each card through once."""
    summary = " ".join(str(summary or "").split())
    if not summary:
        return False
    fn = _seam()
    if fn is None:
        return False
    summary = _redact(summary)
    if not _admit(key or summary, summary, time.monotonic()):
        return False
    try:
        out = fn(KIND, summary, _redact(str(detail or ""))[:600], severity, source=SOURCE)
    except Exception:  # noqa: BLE001 — friction is a side channel; it never breaks the loop
        log.debug("[project_board] friction record failed", exc_info=True)
        return False
    return bool(out)


_URL_RE = re.compile(r"https?://\S+")
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`")
_PATH_RE = re.compile(r"(?:~|\.{0,2})/[\w.@+~/-]+|\b[\w.@+~-]+(?:/[\w.@+~-]+)+")
# A card id: a short lowercase prefix, a dash, a suffix holding a digit (bd-1zp, ds-n8d,
# bd-vbyd.13). Requiring the digit keeps ordinary hyphenated words (merged-state, re-run).
_CARD_ID_RE = re.compile(r"\b[a-z]{1,8}-(?=[a-z0-9]*\d)[a-z0-9]{2,12}(?:\.\d+)*\b")
_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_NUM_RE = re.compile(r"#?\d+(?:\.\d+)*")
_PAREN_RE = re.compile(r"\([^()]*\)")


def stable_phrase(text: str, *, drop: tuple[str, ...] = (), limit: int = 90) -> str:
    """The id-free gist of a free-text failure reason, fit for a ledger summary: its first
    line up to the first `` — `` or ``: `` clause break, with the strings in ``drop`` (the
    card id, the branch), every card id, URLs, quoted text, paths (absolute or relative),
    shas, numbers and parentheticals taken out. Two
    occurrences of one failure on two cards give the same phrase."""
    line = (str(text or "").strip().splitlines() or [""])[0]
    for word in drop:
        if word:
            line = line.replace(str(word), "")
    line = line.strip(" -—:;,.")
    for sep in (" — ", ": ", "; "):
        head = line.split(sep, 1)[0]
        if sep in line and len(head.strip()) >= 6:
            line = head
            break
    for pattern, repl in (
        (_URL_RE, ""),
        (_QUOTED_RE, "…"),
        (_PATH_RE, ""),
        (_PAREN_RE, ""),
        (_CARD_ID_RE, ""),
        (_SHA_RE, ""),
        (_NUM_RE, "N"),
    ):
        line = pattern.sub(repl, line)
    line = " ".join(line.replace("``", "").split()).strip(" -—:;,.")
    if len(line) > limit:
        line = line[: limit - 1].rstrip() + "…"
    return line or "unclassified failure"
