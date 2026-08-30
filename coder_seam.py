"""The P2 board seam (ADR 0064): dispatch a feature's build through the `coder`
plugin's execution-grounded ``solve()`` ladder instead of a single
``delegate_to(acp)`` shot — greedy → best-of-k → tree-search → fusion, gated on
the feature's acceptance tests actually PASSING in a real worktree, never an LLM
judge.

**Composes** `plugins.coder.solve` (a separate, git-URL-installed plugin — imported
lazily/best-effort so this repo carries no hard dependency on it and no import-time
coupling) with THIS repo's own worktree primitives. The coder plugin never sees a
board worktree; it only supplies the deterministic ladder (`solve()`, `Budget`,
`Verdict`). Each candidate the ladder tries gets its OWN throwaway worktree — the
"independent-parallel acp attempts" `coder`'s own generator module already flags as
"the P2 path" (`plugins/coder/generate.py`) — and the winning (test-passing)
candidate is PROMOTED to the feature's canonical worktree/branch so the rest of the
drive (fixups, the pre-PR local gate, `open_pr`, the CI bounce, tier escalation) is
UNCHANGED; every other candidate is reaped.

**Honest degrade** (ADR 0064's no-LLM-judge rule, applied at the board layer): the
dispatch decision (``should_use_solve``) requires ALL THREE — the `coder` plugin
importable (the host has it enabled), the feature's acceptance criteria present
(the Ready gate's oracle), and a configured, runnable acceptance-test command (the
actual executable verifier — `solve()` cannot run prose). Missing any of the three
⇒ the caller falls back to today's single ``delegate_to(acp)`` shot; never a silent
best-of-k/judge substitute.

**Deferred** (see the ADR + the PR that lands this): compiling EARS acceptance
criteria into a generated test file. The simplest-correct path used here instead:
the coder is already prompted (``loop._build_prompt``) to write tests satisfying
the acceptance criteria as part of its definition of done; this module's ``verify``
just RUNS whatever tests exist in a candidate's worktree via the configured command
and gates on its exit code — real execution, no fabricated grounding.

**Rung 4 — fusion (ADR 0064 P3).** Fusion (e.g. ``protolabs/fusion``) is a strong
*generator* but, per the ADR, it **can't tool-call** — unlike the ``acp`` coder
(a real edit/verify session in the worktree), it can only return a plain chat
completion. So its candidate generation is a DIFFERENT shape from the ACP rungs:
``_fusion_prompt`` hands it the task + the CURRENT content of the feature's
declared ``files_to_modify`` (read from the base repo — fusion has no tool access
to look these up itself) and asks for the complete, final content of every file it
creates or changes; ``_parse_fusion_files`` extracts ``{path: content}`` from the
reply; ``_WorktreeSolveAdapter.generate_fusion`` writes those files into a fresh
worktree (the same throwaway-per-candidate discipline as the ACP rungs) and hands
the path to the SAME ``verify()`` — real acceptance tests, same oracle, no separate
judge. Wholesale file replacement (not a unified diff) is deliberate: an LLM
completion reliably reproduces a full file; a hand-rolled patch with drifted
context lines is a common failure mode `git apply` doesn't forgive. Only reached
when a ``fusion_delegate`` is configured (an ``openai``-type Delegate, already
resolved by the caller) — absent that, ``solve()`` gets ``fusion_generate=None``
and stops at tree-search exactly as before (honest degrade, unchanged).

**Fusion + large files — honest-degrade, not silent truncation.** Whole-file
replacement only works when a real completion can (a) see the WHOLE current file
and (b) reproduce the WHOLE new one — and the tighter constraint is usually the
OUTPUT side: a delegate's own ``max_tokens`` (often ~1024 by default, ~4K chars)
can truncate the response well before a merely-medium file's size, and
``OpenAiAdapter.dispatch`` doesn't surface ``finish_reason`` to tell a caller that
happened. So this module never attempts a full-file rewrite it can't stand behind:
``fusion_viable_for_files`` gates on the feature's ACTUAL on-disk file sizes
(per-file and combined) BEFORE fusion is ever dispatched — callers (``loop.py``,
``api.py``) check it and treat "not viable" exactly like "no fusion_delegate
configured" (``fusion_delegate=None`` for that dispatch). As a defensive backstop
(in case a caller skips the gate — direct ``test_rung`` callers, say)
``generate_fusion`` ALSO refuses to write a candidate file back over a
significantly larger original — a shrunk "complete" rewrite is far more likely a
truncated one than an honest tiny file than a real edit, so that file is left
unwritten (verify() then judges the candidate on what's actually there) rather
than risking data loss.

**``test_rung`` (operator-only diagnostic).** Verifying a specific rung — fusion
especially, only otherwise reached after three cheaper rungs fail — shouldn't
require contriving a task hard enough to fail its way there. ``test_rung`` runs
ONE named rung once against a feature's real acceptance tests in a throwaway
worktree that's ALWAYS reaped, win or lose — never promoted, no PR. Exposed via
api.py's ``test-rung`` route with no ``@tool`` wrapper, so it's operator-only,
not something the board's own lead agent can reach for itself."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import re
import concurrent.futures
import sys
import threading
import time
import types
from collections import OrderedDict, deque
from collections.abc import Iterable
from pathlib import Path
from typing import Callable

from . import config, worktree

log = logging.getLogger("protoagent.plugins.project_board")


# ── Live coder monitoring (#84) ────────────────────────────────────────────────
# A per-feature, per-gen in-memory ring buffer fed by the ACP client's
# progress/thought/tool callbacks during a dispatch, and read by the board view's
# monitor drawer over GET …/features/{fid}/progress. Purely in-process and
# best-effort: it NEVER affects a build (a tap that can't wire falls back to an
# untapped dispatch) and is BOUNDED — a rolling thought tail, a capped tool-
# lifecycle history, and a lid on how many features are retained — so a long-lived
# loop can't leak memory.

_THOUGHT_TAIL_MAX = 500  # rolling thought tail, in CHARS — never a per-word accumulation
_ANSWER_TAIL_MAX = 700  # rolling tail of the coder's streamed ANSWER text (its own narration)
_PLAN_ENTRIES_MAX = 40  # latest ACP plan (the coder's live todo list), entry-capped
_TOOL_INPUT_PREVIEW_MAX = 200  # first chars of a tool call's raw input (the command/pattern)
_RECENT_TOOLS_MAX = 200  # rolling tool-lifecycle history per gen (start/end events)
_MAX_FEATURES = 64  # features retained (LRU-evicted) so a long loop stays bounded

# Patchable clock so tests can assert a deterministic elapsed_s.
_monotonic = time.monotonic

# Tool-input keys that carry a file location. The forwarded ACP tool event carries
# the raw input but NOT the structured `locations`, so we mine path-ish keys.
_LOCATION_KEYS = ("path", "file", "file_path", "filePath", "abs_path", "absolute_path", "filename")


def _infer_tool_kind(name: str) -> str:
    """Best-effort tool KIND from its name (the forwarded event has no `kind`)."""
    n = (name or "").lower()
    if any(w in n for w in ("read", "cat", "open", "view")):
        return "read"
    if any(w in n for w in ("edit", "write", "apply", "patch", "create", "update", "replace")):
        return "edit"
    if any(w in n for w in ("bash", "shell", "run", "exec", "terminal")):
        return "execute"
    if any(w in n for w in ("search", "grep", "glob", "find", "list")):
        return "search"
    return ""


def _extract_locations(tool_input) -> list[str]:
    """Best-effort file locations from a tool call's raw input (a JSON string or a
    dict). Mines path-ish keys; never raises on an odd shape (returns [])."""
    data = tool_input
    if isinstance(tool_input, str):
        s = tool_input.strip()
        if not s:
            return []
        try:
            data = json.loads(s)
        except (ValueError, TypeError):
            return []
    if not isinstance(data, dict):
        return []
    locs: list[str] = []
    for k in _LOCATION_KEYS:
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            locs.append(v.strip())
    for k in ("paths", "files", "locations"):
        v = data.get(k)
        if isinstance(v, (list, tuple)):
            locs.extend(str(x) for x in v if isinstance(x, (str, int)))
    seen: set = set()
    out: list[str] = []
    for loc in locs:  # de-dup, preserve order, cap
        if loc not in seen:
            seen.add(loc)
            out.append(loc)
    return out[:8]


class _GenBuffer:
    """One coder generation's live state (one ACP dispatch = one gen)."""

    __slots__ = (
        "gen",
        "tier",
        "started",
        "ended",
        "current_tool",
        "recent_tools",
        "thought_tail",
        "usage",
        "verify",
        "done",
        "answer_tail",
        "plan",
        "stop_reason",
    )

    def __init__(self, gen: int, tier: str = ""):
        self.gen = int(gen)
        self.tier = tier or ""
        self.started = _monotonic()
        self.ended: float | None = None
        self.current_tool: dict | None = None
        self.recent_tools: deque = deque(maxlen=_RECENT_TOOLS_MAX)
        self.thought_tail = ""
        self.answer_tail = ""
        self.plan: list | None = None
        self.usage: dict | None = None
        self.verify: dict | None = None
        self.done = False
        self.stop_reason: str | None = None

    def add_thought(self, delta: str) -> None:
        # Coalesce into a ROLLING tail: append the delta, then keep only the last
        # _THOUGHT_TAIL_MAX chars. Never accumulates per-word chunks as a list.
        if not delta:
            return
        self.thought_tail = (self.thought_tail + delta)[-_THOUGHT_TAIL_MAX:]

    def add_answer(self, delta: str) -> None:
        # Same rolling-tail discipline as thoughts: the coder's streamed answer text is
        # its own narration of what it's doing/finished — the drawer's best plain-words
        # signal — but unbounded accumulation is exactly what this buffer must never do.
        if not delta:
            return
        self.answer_tail = (self.answer_tail + delta)[-_ANSWER_TAIL_MAX:]

    def set_plan(self, entries) -> None:
        # Latest-wins: ACP `plan` updates carry the ENTIRE current plan each time
        # (the coder's live todo list), so replace — never append. Sanitized +
        # entry-capped so a runaway plan can't bloat the buffer.
        if not isinstance(entries, (list, tuple)):
            return
        plan = []
        for e in entries[:_PLAN_ENTRIES_MAX]:
            if isinstance(e, dict):
                plan.append(
                    {
                        "content": str(e.get("content") or "")[:200],
                        "status": str(e.get("status") or ""),
                        "priority": str(e.get("priority") or ""),
                    }
                )
        self.plan = plan

    def add_tool(self, event: dict) -> None:
        phase = str(event.get("phase") or "")
        name = str(event.get("name") or "tool")
        kind = str(event.get("kind") or "") or _infer_tool_kind(name)
        tid = str(event.get("id") or name)
        if phase == "start":
            locs = _extract_locations(event.get("input"))
            # The raw input's head is the "what exactly is it running" line (the
            # command for execute, the pattern for search) — locations alone lose it.
            preview = str(event.get("input") or "")[:_TOOL_INPUT_PREVIEW_MAX]
            self.current_tool = {
                "id": tid,
                "name": name,
                "kind": kind,
                "locations": locs,
                "status": "running",
                "input_preview": preview,
            }
            self.recent_tools.append({"name": name, "kind": kind, "status": "start", "locations": locs})
        elif phase == "end":
            status = str(event.get("status") or "completed")
            if self.current_tool and self.current_tool.get("id") == tid:
                self.current_tool["status"] = status
                self.current_tool["output"] = str(event.get("output") or "")[:400]
                locs = self.current_tool.get("locations") or []
            else:
                locs = []
                self.current_tool = {"id": tid, "name": name, "kind": kind, "locations": locs, "status": status}
            self.recent_tools.append({"name": name, "kind": kind, "status": status, "locations": locs})

    def snapshot(self) -> dict:
        return {
            "gen": self.gen,
            "tier": self.tier,
            "done": self.done,
            # A finished gen's clock FREEZES at progress_end — otherwise the drawer can't
            # tell a completed gen from a running one (panel finding on #89).
            "elapsed_s": round(max(0.0, (self.ended if self.ended is not None else _monotonic()) - self.started), 1),
            "current_tool": dict(self.current_tool) if self.current_tool else None,
            "recent_tools": list(self.recent_tools),
            "thought_tail": self.thought_tail,
            "answer_tail": self.answer_tail,
            "plan": list(self.plan) if self.plan else None,
            "usage": dict(self.usage) if self.usage else None,
            "verify": dict(self.verify) if self.verify else None,
            "stop_reason": self.stop_reason,
        }


# ── Reload-stable buffer slot (#178) ────────────────────────────────────────────
# `_progress` used to be a plain module global — so a plugin hot-reload (graph
# reload, `reload_plugins`) imported a FRESH module instance whose empty dict the
# newly-mounted API routes read, while the still-running dispatch loop (holding the
# OLD instance via its own import-time `from . import coder_seam`) kept streaming
# gens into the previous dict: the drawer answered "No live coder run" for a gen
# that was actively streaming. The buffer now lives on a process-stable holder — a
# synthetic (non-importable) entry in ``sys.modules``, which a plugin reload never
# replaces (it only re-imports the plugin's own package) — so every module instance
# in the process reads and writes the SAME dict. Adoption by SHARING, not copying:
# a copy would orphan the old loop's future writes all over again.
#
# Keyed by the loaded package name (the host mounts each plugin under its own
# synthetic package) so two boards in one host never share a buffer, while a reload
# of THIS plugin (same package name, fresh module object) finds its own slot. The
# prefix keeps the slot name from starting with the plugin package's own name, so a
# host that purges ``sys.modules`` by package prefix on reload can't purge it.
#
# Deliberately NOT filtered by board state on adoption (no "transfer only
# in_progress features"): this module has no store access, and the buffer is
# already bounded (_MAX_FEATURES LRU) and reset per fresh build (progress_new_run),
# so carried-over stale entries age out exactly as they always did.
_PROGRESS_SLOT_PREFIX = "project_board.coder_progress::"


def _progress_slot_name() -> str:
    pkg = __name__.rsplit(".", 1)[0] if "." in __name__ else __name__
    return _PROGRESS_SLOT_PREFIX + pkg


def _attach_progress() -> "tuple[OrderedDict[str, OrderedDict[int, _GenBuffer]], int, int]":
    """Attach to (or create) the process-stable live-monitor buffer. Returns
    ``(buffer, carried, live)``: the shared dict, how many features a PREVIOUS
    module instance left in it (0 on a fresh boot), and how many of those still
    have a running (not-done) gen. The carried gens hold the previous instance's
    ``_GenBuffer`` class — duck-typed everywhere they're read, never isinstance'd."""
    name = _progress_slot_name()
    holder = sys.modules.get(name)
    prev = getattr(holder, "progress", None)
    if isinstance(prev, OrderedDict):
        live = sum(1 for gens in prev.values() if any(not b.done for b in gens.values()))
        return prev, len(prev), live
    holder = types.ModuleType(name)
    holder.__doc__ = (
        "Process-stable holder for project_board's live coder-monitor buffer (#178) — "
        "a data slot that survives plugin reloads, not importable code."
    )
    holder.progress = OrderedDict()
    holder = sys.modules.setdefault(name, holder)  # atomic install — see store._br_lock
    # setdefault may hand back a holder ANOTHER caller installed first (ours is then
    # discarded) — report what that buffer actually carries, not the 0/0 our fresh one
    # would have. The counts feed the reload warning, so they have to be true.
    buf = holder.progress
    live = sum(1 for gens in buf.values() if any(not b.done for b in gens.values()))
    return buf, len(buf), live


# fid -> {gen -> _GenBuffer}. An OrderedDict so whole features LRU-evict cheaply.
# Attached to the process-stable slot above so it survives a plugin reload (#178).
_progress: "OrderedDict[str, OrderedDict[int, _GenBuffer]]"
_progress, _adopted_features, _adopted_live = _attach_progress()
if _adopted_features:
    # The splunk line for #178: a reload adopted the previous instance's buffer.
    log.warning(
        "[project_board] plugin reload: adopted the previous instance's coder-monitor "
        "buffer — %d feature(s) carried over (%d with a live gen)",
        _adopted_features,
        _adopted_live,
    )


def ensure_progress_attached() -> int:
    """``register()``'s mount-time hook (#178): make sure THIS module instance's
    ``_progress`` IS the process-stable shared buffer, and return how many features
    it currently carries. Import-time attachment normally already did this (the
    call is then a no-op); going through it at register() pins adoption to the
    plugin lifecycle — not the first progress poll after a reload — and repairs the
    one divergence left, the slot replaced from outside after this module was
    imported (adopt it: the slot is the single source of truth every instance must
    read AND write; keeping two live dicts is exactly the split this fix removes)."""
    global _progress
    buf, carried, live = _attach_progress()
    if buf is not _progress:
        log.warning(
            "[project_board] coder-monitor buffer slot changed after import — re-adopting "
            "(%d feature(s) carried over, %d with a live gen)",
            carried,
            live,
        )
        _progress = buf
    return len(buf)


def _gens_for(fid: str | None, *, create: bool = False):
    if not fid:
        return None
    gens = _progress.get(fid)
    if gens is None:
        if not create:
            return None
        gens = _progress[fid] = OrderedDict()
        while len(_progress) > _MAX_FEATURES:  # LRU-evict the oldest feature
            _progress.popitem(last=False)
    _progress.move_to_end(fid)
    return gens


def progress_new_run(fid: str | None) -> None:
    """Drop any prior gens for this feature — called at the start of a FRESH build
    so the drawer shows this run, not stale gens from an earlier dispatch."""
    if fid:
        _progress.pop(fid, None)


def progress_begin(fid: str | None, gen: int, tier: str = "") -> None:
    """Register (or reset) a generation's buffer. No-op when ``fid`` is falsy — the
    operator-only test-rung path passes None (it's a diagnostic, not a live run)."""
    gens = _gens_for(fid, create=True)
    if gens is not None:
        gens[int(gen)] = _GenBuffer(gen, tier)


def _buf(fid: str | None, gen: int) -> "_GenBuffer | None":
    gens = _gens_for(fid)
    return gens.get(int(gen)) if gens is not None else None


def progress_thought(fid: str | None, gen: int, delta: str) -> None:
    b = _buf(fid, gen)
    if b is not None:
        b.add_thought(delta)


def progress_tool(fid: str | None, gen: int, event: dict) -> None:
    b = _buf(fid, gen)
    if b is not None:
        b.add_tool(event)


def progress_answer(fid: str | None, gen: int, delta: str) -> None:
    b = _buf(fid, gen)
    if b is not None:
        b.add_answer(delta)


def progress_plan(fid: str | None, gen: int, entries) -> None:
    b = _buf(fid, gen)
    if b is not None and entries is not None:
        b.set_plan(entries)


def progress_usage(fid: str | None, gen: int, usage: dict) -> None:
    b = _buf(fid, gen)
    if b is None or not usage:
        return
    try:
        b.usage = {"used": int(usage.get("used") or 0), "size": int(usage.get("size") or 0)}
    except (TypeError, ValueError, AttributeError):
        pass


def progress_verify(fid: str | None, gen: int, *, test_cmd: str, output: str, passed: bool) -> None:
    b = _buf(fid, gen)
    if b is not None:
        b.verify = {"test_cmd": test_cmd, "passed": bool(passed), "tail": (output or "")[-1500:]}


def progress_stop_reason(fid: str | None, gen: int, reason) -> None:
    """Record the ACP adapter's stop-reason / dead-end signal for a gen (#198) —
    the "why did the coder stop" that the retro and the empty-result classifier
    (loop) read after a dispatch that produced nothing. Falsy/unknown → no-op
    (the field stays None); capped so a pathological reason can't bloat the buffer."""
    b = _buf(fid, gen)
    if b is not None and reason:
        b.stop_reason = str(reason)[:200]


# ── Persist finished gens to the bead (#226) ────────────────────────────────────
# The WRITE side of the coder-monitor history (#226): when a gen finishes, its live
# snapshot is serialized to a `coder-monitor: {…}` JSON bead comment so the drawer
# can replay a run the bounded in-memory buffer has since LRU-evicted (or lost to a
# plugin reload). Wired from register() — which owns store access — via
# set_store_factory; the factory stays None in a standalone test env, where the
# persist is simply skipped. Best-effort end to end: no factory, no store, or any
# failure is swallowed, because a monitoring write must NEVER break a build.

_COMMENT_PREFIX = "coder-monitor: "  # the JSON payload follows; the read side (#226 S2) splits on it
_store_factory: "Callable[[], object] | None" = None


def set_store_factory(fn: "Callable[[], object] | None") -> None:
    """Wire (or clear) the accessor register() hands us to reach the board store, so
    ``progress_end`` can persist a finished gen's snapshot as a bead comment (#226).
    Passing None — or never calling this at all (the standalone/test case) — leaves
    the persist a silent no-op."""
    global _store_factory
    _store_factory = fn


def _persist_gen_snapshot(fid: str | None, gen: int) -> None:
    """Best-effort: write the just-finished gen's ``snapshot()`` as a ``coder-monitor:``
    JSON bead comment (the #226 write side). Skips silently when there's no fid, no
    wired store factory (standalone test env), no store, or the write itself fails —
    a monitoring persist must NEVER break a build.

    ``progress_end`` is a SYNC hook and ``dispatch_coder_tapped``'s finally path calls
    it ON the event-loop thread — where a blocking ``store.comment`` (a `br` subprocess
    plus contention sleeps) stalls every coroutine and trips the #258 "blocking `br
    comments` on the event-loop thread" warning. So when a loop is running in this
    thread, the store write is submitted fire-and-forget to a worker via
    ``loop.run_in_executor`` — ``progress_end`` stays synchronous and returns at once,
    and the debug-level failure logging is preserved on the worker. With no running
    loop (synchronous callers, tests) the write runs INLINE exactly as before, which is
    the correct place to block (#258)."""
    if not fid or _store_factory is None:
        return
    b = _buf(fid, gen)
    if b is None:
        return

    def _write() -> None:
        try:
            store = _store_factory()
            if store is None:
                return
            payload = json.dumps(b.snapshot(), default=str)
            store.comment(fid, _COMMENT_PREFIX + payload)
        except Exception:  # noqa: BLE001 — monitoring must never break a build
            log.debug("[project_board] persisting gen %s snapshot for %s failed", gen, fid, exc_info=True)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        _write()  # no loop (sync callers, tests) — block here, the right place to block
        return
    # A loop is running (progress_end fired from dispatch_coder_tapped's finally): hop
    # the blocking write off the loop thread so it neither stalls the tick nor trips the
    # #258 warning. Fire-and-forget — the future isn't awaited. If the loop is already
    # shutting down and can't accept the job, fall back to an inline write rather than
    # silently drop the snapshot.
    try:
        loop.run_in_executor(None, _write)
    except RuntimeError:  # loop closed/shutting down → block inline instead
        _write()


def progress_end(fid: str | None, gen: int) -> None:
    b = _buf(fid, gen)
    if b is not None and not b.done:  # idempotent — every dispatch exit path may call it
        b.done = True
        b.ended = _monotonic()
        # Snapshot AFTER freezing done+clock so the persisted record reflects the
        # finished state (done=True, frozen elapsed_s) — the read side (#226 S2) reads it back.
        _persist_gen_snapshot(fid, gen)


def progress_snapshot(fid: str) -> dict:
    """The board view's monitor payload for one feature — the per-gen live state,
    gens in order. Empty-but-valid (``{"gens": []}``) when the feature has no
    live/recent run in this process's memory."""
    gens = _progress.get(fid)
    if gens is not None:
        # A feature being actively polled must stay LRU-fresh — otherwise 64 concurrent
        # live features could evict the very one the drawer is watching (panel round 2).
        _progress.move_to_end(fid)
    if not gens:
        return {"gens": []}
    return {"gens": [gens[g].snapshot() for g in sorted(gens)]}


def _import_dispatch_tapped():
    """Best-effort handle on coding_agent's PUBLIC tapped-dispatch seam (C1): the host
    function that drives the pooled ACP client WITH progress callbacks wired in and
    owns the whole worktree-scoped session lifecycle (fresh-both forget, by-kind
    permission policy, cancellation kill, teardown) — so the board taps the live
    stream through ONE public name, reaching zero coding_agent internals. Returns the
    callable, or ``None`` when the host predates the seam (a standalone test env, an
    older host) — the ONE condition ``dispatch_coder_tapped`` degrades on."""
    try:
        from plugins.coding_agent import dispatch_tapped
    except Exception:  # noqa: BLE001 — host predates the public C1 seam → untapped fallback
        return None
    return dispatch_tapped


async def dispatch_coder_tapped(
    coder,
    worktree_path: str,
    prompt: str,
    *,
    fid: str | None,
    gen: int,
    tier: str = "",
    timeout: float | None = None,
    env_passthrough: Iterable[str] = (),
    _dispatch_tapped=None,
) -> str:
    """Dispatch the ACP coder into ``worktree_path`` like ``worktree.dispatch_coder``
    — same fresh-both session discipline, same guaranteed subprocess teardown — but
    with the coder's thought/tool/answer/usage/plan/stop callbacks wired into this
    feature's live-monitoring buffer (#84).

    ``worktree.dispatch_coder`` goes through ``adapter.dispatch``, which does NOT
    forward the ACP callbacks. C1 published ``plugins.coding_agent.dispatch_tapped``
    — a PUBLIC seam that drives the pooled client with those callbacks and owns the
    session lifecycle end to end — so this taps the live stream WITHOUT reaching a
    single coding_agent internal (the private client / permission / spec / kill
    plumbing now lives behind that seam). When the public seam is ABSENT (a host
    predating C1) it degrades to the untapped ``worktree.dispatch_coder`` — the gen
    still records its start/tier/elapsed, just without the live stream. That absence
    is the ONLY fallback trigger: a real dispatch failure BELOW the seam is normalised
    to a ``WorktreeError``/``CoderTimeout`` and propagates, never silently swallowed
    into an untapped retry. A monitoring concern must NEVER break a build.

    ``_dispatch_tapped`` is a test-injection seam (mirrors ``_solve``/
    ``_fusion_dispatch``); production callers never pass it — the real best-effort
    import happens in ``_import_dispatch_tapped``."""
    progress_begin(fid, gen, tier)
    tapped = _dispatch_tapped if _dispatch_tapped is not None else _import_dispatch_tapped()
    if tapped is None:
        # Public C1 seam absent. Do NOT jump to the untapped dispatch — that records a
        # gen with no tools/thoughts/plan, which IS the monitor being broken (observed
        # live the day F7 landed: every host on a released protoAgent lost the drawer).
        # Try the legacy private tap first; only a host where neither is reachable
        # degrades to untapped. Absence-only: NOT a broad except around a tapped path.
        return await _dispatch_coder_tapped_legacy(
            coder,
            worktree_path,
            prompt,
            fid=fid,
            gen=gen,
            timeout=timeout,
            env_passthrough=env_passthrough,
        )

    # Board policy on the scoped copy, applied BEFORE the generic public seam and
    # mirroring worktree.dispatch_coder: the worktree is the workdir, the BOARD owns
    # git (manage_git=False, so the seam's adapter never opens a second PR), and the
    # dispatch env is the sanitized allowlist (#86/#142).
    import dataclasses as _dc

    overrides: dict = {"workdir": worktree_path}
    if any(f.name == "manage_git" for f in _dc.fields(coder)):
        overrides["manage_git"] = False
    if any(f.name == "env" for f in _dc.fields(coder)):
        overrides["env"] = config.sanitized_env(env_passthrough)
    scoped = _dc.replace(coder, **overrides)

    async def _thought_cb(delta):
        progress_thought(fid, gen, delta)

    async def _answer_cb(delta):
        progress_answer(fid, gen, delta)

    async def _tool_cb(event):
        progress_tool(fid, gen, event)

    # Old adapter-path semantics preserved: no configured timeout = UNBOUNDED dispatch,
    # else hard-bound with asyncio.wait_for exactly as worktree.dispatch_coder does —
    # on timeout the seam coro is cancelled (its own finally reaps the subprocess) and
    # we raise CoderTimeout. The seam owns the client's own internal timeout bookkeeping.
    try:
        # C1's ACTUAL signature (plugins/coding_agent, `dispatch_tapped`): three
        # keyword-only STREAM callbacks — `on_tool` / `on_thought` / `on_text` — plus
        # `timeout`. It takes no usage/plan/stop-reason callbacks: those are wire
        # signals the seam snapshots off the client the moment the turn ends and
        # returns ON the result. F7 called it with the private client's kwarg names
        # (`tool_callback=…`) against a host where the seam did not exist yet, so the
        # mismatch could not surface until a release carried it — then EVERY dispatch
        # died on `dispatch_tapped() got an unexpected keyword argument`. The fake in
        # the test below now mirrors this signature exactly (no **kwargs) so a rename
        # on either side fails here instead of on a live board.
        coro = tapped(
            scoped,
            prompt,
            timeout=timeout,
            on_tool=_tool_cb,
            on_thought=_thought_cb,
            on_text=_answer_cb,
        )
        result = await (asyncio.wait_for(coro, timeout) if timeout else coro)
    except asyncio.CancelledError:
        # Turn stopped (operator/watchdog): the public seam already dropped the pooled
        # client + SIGKILLed the tree on its way out — nothing for the board to clean up.
        raise
    except asyncio.TimeoutError:
        raise worktree.CoderTimeout(f"coder timed out after {timeout}s")
    except Exception as exc:  # noqa: BLE001 — normalise every below-seam failure to the
        # adapter path's contract: nothing propagates raw; a dispatch failure surfaces as
        # WorktreeError (this is error normalisation, NOT a fallback — see the seam-absent
        # branch above for the only path that degrades to an untapped dispatch).
        raise worktree.WorktreeError(f"coder dispatch failed: {exc}")
    else:
        # Drain the returned TappedResult onto the gen BEFORE it closes, so the drawer
        # shows the same usage/plan/stop-reason the legacy tap sampled off the client.
        # Outside the `except` on purpose: a shape complaint below is the board's own
        # verdict, not a below-seam failure to re-wrap (it would double the prefix).
        progress_usage(fid, gen, getattr(result, "usage", None) or {})
        progress_plan(fid, gen, getattr(result, "plan", None))
        progress_stop_reason(fid, gen, getattr(result, "stop_reason", None) or getattr(result, "dead_end", None))
        reply = getattr(result, "reply", result)
        if not isinstance(reply, str):
            # Refuse loudly rather than hand a non-string up the board's reply path,
            # where it would end up in a PR body. Accepts BOTH known shapes (a
            # TappedResult, or a bare reply string from a seam that returns one).
            raise worktree.WorktreeError(
                f"coder dispatch failed: dispatch_tapped returned {type(result).__name__}, "
                "expected a TappedResult (or a reply string)"
            )
        return reply
    finally:
        progress_end(fid, gen)


# ── legacy private tap (pre-C1 hosts) ────────────────────────────────────────────
# The C1 seam (`plugins.coding_agent.dispatch_tapped`) is the preferred path and the
# only one that reaches no host internals — but it shipped in protoAgent AFTER the
# releases this plugin supports (`min_protoagent_version: 0.153.2`), so on every
# released host today it is absent. Degrading straight to the UNTAPPED dispatch there
# means the coder monitor records a gen with no tools, no thoughts and no plan — i.e.
# the drawer stops working, which is what happened on a live board the day F7 landed.
# So the ladder is THREE rungs, not two:
#
#   1. `dispatch_tapped`         — public, preferred, zero internals
#   2. this legacy tap           — the pre-F7 implementation, private internals, WARNS once
#   3. `worktree.dispatch_coder` — untapped; only when neither is reachable
#
# Rung 2 exists to be deleted: once a protoAgent release carrying C1 is the floor in
# `min_protoagent_version`, drop this function and its warning with it.
_LEGACY_TAP_WARNED = False


async def _dispatch_coder_tapped_legacy(
    coder,
    worktree_path: str,
    prompt: str,
    *,
    fid: str | None,
    gen: int,
    timeout: float | None = None,
    env_passthrough: Iterable[str] = (),
) -> str:
    """The pre-C1 tap: drive the pooled ``AcpClient`` directly with the progress
    callbacks wired in. Reaches ``coding_agent`` internals (``_client_for``,
    ``_make_permission``, ``_drop_client``, ``adapter._spec``) — which is why C1 exists
    — but it is what keeps the monitor alive on a host that predates the public seam.
    Same contract as the public path: every dispatch failure surfaces as
    ``WorktreeError``/``CoderTimeout``, the gen closes on every exit, and the
    worktree-scoped subprocess is always torn down. A host with neither the seam nor
    these internals falls through to the untapped dispatch inside this function."""
    global _LEGACY_TAP_WARNED
    if not _LEGACY_TAP_WARNED:
        _LEGACY_TAP_WARNED = True
        log.warning(
            "[project_board] this host predates coding_agent.dispatch_tapped (the C1 seam) — "
            "the coder monitor is running on the legacy private tap. It works; upgrade "
            "protoAgent to move onto the public seam."
        )
    try:
        import dataclasses as _dc

        from plugins.coding_agent import _client_for, _drop_client, _make_permission
        from plugins.coding_agent.acp_client import AcpError
        from plugins.delegates.adapters import ADAPTERS, DelegateError

        adapter = ADAPTERS["acp"]
        overrides: dict = {"workdir": worktree_path}
        if any(f.name == "manage_git" for f in _dc.fields(coder)):
            overrides["manage_git"] = False  # the BOARD owns git for scoped dispatches
        if any(f.name == "env" for f in _dc.fields(coder)):
            overrides["env"] = config.sanitized_env(env_passthrough)
        scoped = _dc.replace(coder, **overrides)
        spec = adapter._spec(scoped)
    except Exception:  # noqa: BLE001 — host internals absent/changed → untapped fallback
        try:
            return await worktree.dispatch_coder(
                coder, worktree_path, prompt, timeout=timeout, env_passthrough=env_passthrough
            )
        finally:
            progress_end(fid, gen)  # the gen must close on EVERY exit path (panel: orphaned gens)

    # Fresh-both: forget any persisted session first (see worktree.dispatch_coder).
    try:
        await adapter.forget_session(scoped)
    except Exception:  # noqa: BLE001 — best-effort; a stale session must not block the build
        log.warning("[project_board] forget_session failed for %s", worktree_path, exc_info=True)

    try:
        client = _client_for(spec)
    except Exception as exc:  # noqa: BLE001 — adapter-layer parity: dispatch failures are WorktreeError
        progress_end(fid, gen)
        raise worktree.WorktreeError(f"coder dispatch failed: {exc}")
    try:
        client._permission = _make_permission(spec)  # the adapter's by-kind policy (ADR 0024)
    except Exception:  # noqa: BLE001 — tolerate a host that resolves permissions differently
        pass

    async def _thought_cb(delta):
        progress_thought(fid, gen, delta)

    async def _answer_cb(delta):
        progress_answer(fid, gen, delta)

    async def _tool_cb(event):
        progress_tool(fid, gen, event)
        # usage_update isn't forwarded as a callback — the client folds it into
        # last_usage as it goes, so sample it on each tool event to keep totals live.
        progress_usage(fid, gen, getattr(client, "last_usage", None) or {})
        # Same sampling pattern for the coder's live plan (ACP `plan` updates —
        # its own todo list). Hosts older than the last_plan parse just yield None.
        progress_plan(fid, gen, getattr(client, "last_plan", None))

    # Old adapter-path semantics: no configured timeout = UNBOUNDED dispatch. The host
    # client requires a number (it logs int(timeout)), so "unbounded" rides a 24h
    # sentinel instead of the 600s floor the first tap draft imposed (panel round 2).
    prompt_timeout = timeout or getattr(scoped, "timeout_s", None) or 86400.0
    try:
        coro = client.prompt(
            prompt,
            tool_callback=_tool_cb,
            thought_callback=_thought_cb,
            text_callback=_answer_cb,
            timeout=prompt_timeout,
        )
        reply = await (asyncio.wait_for(coro, timeout) if timeout else coro)
        progress_usage(fid, gen, getattr(client, "last_usage", None) or {})
        return reply
    except asyncio.CancelledError:
        # Turn stopped (operator/watchdog) — drop the pooled client + SIGKILL the tree
        # NOW (mirrors AcpAdapter._prompt) so the subprocess can't keep running detached.
        try:
            _drop_client(spec)
            client.kill_now()
        except Exception:  # noqa: BLE001 — mid-cancellation cleanup is best-effort
            pass
        raise
    except asyncio.TimeoutError:
        raise worktree.CoderTimeout(f"coder timed out after {timeout}s")
    except (AcpError, DelegateError) as exc:
        raise worktree.WorktreeError(f"coder dispatch failed: {exc}")
    except Exception as exc:  # noqa: BLE001 — restore the adapter path's contract: nothing
        # below the seam propagates raw; every dispatch failure surfaces as WorktreeError
        # (panel on #89: the client-direct tap had narrowed AcpError-only).
        raise worktree.WorktreeError(f"coder dispatch failed: {exc}")
    finally:
        # Stash whatever stop-reason / dead-end signal the ACP client reports (#198)
        # — sampled on EVERY exit so an empty reply still records WHY the coder
        # stopped. Best-effort getattr: a host without the attribute yields None.
        progress_stop_reason(
            fid, gen, getattr(client, "last_stop_reason", None) or getattr(client, "last_dead_end", None)
        )
        progress_end(fid, gen)
        try:
            await adapter.teardown(scoped)  # #1 lifecycle rule: reap the worktree-scoped subprocess
        except Exception:  # noqa: BLE001 — never let teardown mask the result/error
            log.warning("[project_board] coder teardown failed for %s", worktree_path, exc_info=True)


async def dispatch_task(delegate, prompt: str, *, timeout: float | None = None) -> str:
    """Dispatch a task-type bead's spec to its assignee sister-agent delegate and
    return the reply — the deliverable a task ships instead of a diff.

    The assignee is a sister agent of EITHER type: an ``acp`` coder delegate (a real
    ACP session) or an ``a2a`` sister agent — the SAME transport review dispatch
    already reaches over ``ADAPTERS["a2a"]``. The adapter is selected by the
    delegate's own ``type`` (#304); a delegate that carries no type defaults to
    ``acp`` (the pre-#304 behaviour). Both dispatch over their native transport and
    the reply is handed straight to ``record_delivery`` by the loop.

    Unlike ``dispatch_coder``/``dispatch_coder_tapped`` there is NO worktree scoping:
    a task produces a doc/decision/artifact, not a code change, so the delegate runs
    in its own context and its reply IS the deliverable. Same teardown discipline
    (reap the delegate on every exit — the ``finally``) and the same error
    normalisation as ``dispatch_coder`` — a ``DelegateError`` surfaces as
    ``WorktreeError`` and a timeout as ``CoderTimeout`` — so the loop's coder-failure
    classifier blocks a task-dispatch failure of either type with the identical code.

    Fire-and-forget only (#304): the reply is awaited inline, bounded by ``timeout``
    (``coder_timeout_s``, 30 minutes by default). There is no correlation-id /
    long-running task state or ingress route — leaving a task unassigned and using
    ``POST /features/{id}/deliver`` remains the separate long-running workaround."""
    from plugins.delegates.adapters import ADAPTERS, DelegateError

    # Select the adapter by the delegate's own type (acp | a2a). ``or ADAPTERS["acp"]``
    # short-circuits when the type resolves an adapter, so a typeless double (or an
    # unknown type) degrades to the acp path without ever touching a missing "acp" slot.
    kind = str(getattr(delegate, "type", "") or "acp")
    adapter = ADAPTERS.get(kind) or ADAPTERS["acp"]
    try:
        coro = adapter.dispatch(delegate, prompt, timeout=timeout)
        return await (asyncio.wait_for(coro, timeout) if timeout else coro)
    except asyncio.TimeoutError:
        raise worktree.CoderTimeout(f"task delegate timed out after {timeout}s")
    except DelegateError as exc:
        raise worktree.WorktreeError(f"coder dispatch failed: {exc}")
    finally:
        try:
            await adapter.teardown(delegate)
        except Exception:  # noqa: BLE001 — never let teardown mask the result/error
            log.warning("[project_board] task delegate teardown failed", exc_info=True)


# ── first-party self-dispatch through the host agent (#311) ──────────────────────
# A task assigned to the board's OWN agent (its configured coder name, or the reserved
# ``self``/``agent`` aliases) is NOT shelled out to a sister-agent delegate — it is
# first-party work the host does itself through ``graph.plugins.host.HOST``'s ``invoke``
# seam. Both the seam AND its optional ``tool_fence`` parameter are feature-detected: a
# host predating either (an older host, or the standalone test env with no ``graph``
# package) degrades to the caller's existing parked behaviour, and a host whose
# ``invoke`` predates ``tool_fence`` is called without it. Self work is intentionally
# UNFENCED — trusted first-party work — so ``tool_fence`` stays ``None`` where supported.


def _import_host(_host=None):
    """Best-effort handle on the host's ``graph.plugins.host.HOST`` object — the seam the
    board drives its OWN agent through for a self-assigned task (#311). Returns it, or
    ``None`` when the host predates the seam (an older host, or the standalone test env
    with no ``graph`` package). ``_host`` is a test-injection seam (mirrors the import
    guards elsewhere in this module); production callers never pass it."""
    if _host is not None:
        return _host
    try:
        from graph.plugins.host import HOST
    except Exception:  # noqa: BLE001 — host-free test env / a host predating the seam
        return None
    return HOST


def resolve_self_invoke(_host=None):
    """Resolve the host's ``invoke`` callable for a self-dispatch, or ``None`` when the
    host exposes no such seam (an older host, or the host-free test env). Feature-detects
    BOTH the host object AND a callable ``invoke`` on it — the loop parks the self task
    when this returns ``None`` (#311, r2), exactly the existing park a human/unassigned
    task gets. ``_host`` is the same test-injection seam ``_import_host`` documents."""
    invoke = getattr(_import_host(_host), "invoke", None)
    return invoke if callable(invoke) else None


# ── the host-invocation slot ──────────────────────────────────────────────────────
# The drain below holds the loop's one-in-flight guard across a TIMEOUT. It cannot hold
# it across an external CANCEL (operator stop / shutdown): CancelledError unwinds this
# coroutine, the drive's done-callback clears `_self_inflight`, and the worker thread
# keeps executing on the host — so a later self task could invoke it concurrently. And
# draining on cancel is not an option: shutdown must not block on a runaway host call.
#
# So the cancel case is covered by a flag the WORKER owns. Acquire and release both live
# inside the thread wrapper, in one frame: if the thread never starts, neither happens,
# so the flag can never leak and strand every future self task. The gap between
# submitting the thread and it entering is covered by `_self_inflight`, which the loop
# sets synchronously before the drive begins. Process-stable slot (#178) so a plugin
# reload mid-invocation cannot forget a live call.
_HOST_SLOT_PREFIX = "project_board.host_invoke::"


def _host_slot():
    name = _HOST_SLOT_PREFIX + (__name__.rsplit(".", 1)[0] if "." in __name__ else __name__)
    holder = sys.modules.get(name)
    if holder is None:
        holder = types.ModuleType(name)
        holder.__doc__ = "Process-stable holder for project_board's host-invoke slot — data, not code."
        holder.busy = 0
        holder.lock = threading.Lock()
        # OUR OWN executor, not the loop's shared default, for one reason: a
        # `concurrent.futures.Future` we submitted ourselves answers the question no
        # `asyncio.to_thread` can — `cancel()` returns True ONLY if the work had not
        # started and now never will. That is the proof needed to release the slot on
        # the cancel path without ever releasing one a live worker still owns.
        # max_workers=1 also makes two simultaneous host invocations structurally
        # impossible rather than merely guarded.
        holder.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="pb-host-invoke")
        holder = sys.modules.setdefault(name, holder)  # atomic install — see store._br_lock
    return holder


def _host_slot_release() -> None:
    holder = _host_slot()
    with holder.lock:
        holder.busy = max(0, holder.busy - 1)


def host_invoke_busy() -> bool:
    """True while a synchronous ``dispatch_self`` worker is STILL EXECUTING on the host —
    including one whose awaiter was cancelled out from under it. The loop consults this
    alongside ``_self_inflight`` so a cancelled self task cannot be followed by a second
    invoke while the first is still running."""
    holder = _host_slot()
    with holder.lock:
        return holder.busy > 0


async def dispatch_self(invoke, prompt: str, session_id: str, *, timeout: float | None = None) -> str:
    """Dispatch a self-assigned task through the host's OWN agent via ``invoke`` and
    return the reply — the first-party sibling of ``dispatch_task``. ``invoke`` is the
    ``HOST.invoke`` callable the caller already resolved with ``resolve_self_invoke``;
    this drives it with a stable per-card ``session_id``.

    The optional ``tool_fence`` parameter is feature-detected with ``inspect.signature``
    rather than ASSUMED on the host's signature (#311): a host whose ``invoke`` accepts it
    is called with ``tool_fence=None`` (self work is intentionally UNFENCED first-party
    work); an older ``invoke`` without the parameter is called without it. A coroutine
    ``invoke`` is awaited directly (and genuinely cancelled on timeout); a synchronous
    ``invoke`` is offloaded to a worker thread (``asyncio.to_thread``) so it never stalls
    the event loop. Bounded by ``timeout`` (``coder_timeout_s``) and its failures normalised
    EXACTLY like ``dispatch_task`` — a timeout to ``CoderTimeout`` and anything else to
    ``WorktreeError`` — so the loop's coder-failure classifier blocks a self-dispatch failure
    via the identical code path (#311, r5).

    A synchronous ``invoke`` runs on a worker thread that CANNOT be cancelled: a plain
    ``wait_for(to_thread(...))`` cancels only the *await* on timeout, leaving the thread
    still executing on the host after this call returns and the loop's done-callback clears
    the one-in-flight guard — a second self task could then invoke the host CONCURRENTLY,
    defeating that guard (the #311 review finding). So on a synchronous timeout the worker is
    DRAINED to true completion before the timeout is surfaced: the caller stays parked on this
    await (guard held, slot held) until the host's own call actually returns, so no concurrent
    invoke can start. The cost — a runaway synchronous host pins the drive until its thread
    finishes — is the honest price of an uncancellable call, and strictly safer than two live
    invokes racing on one host.

    The drain covers a TIMEOUT. An external CANCEL (operator stop / shutdown) it cannot
    cover — CancelledError unwinds this coroutine and the drive's done-callback frees the
    guard while the worker runs on — and draining there would block shutdown on the very
    runaway call the operator is trying to stop. That case is covered instead by the
    busy slot (``host_invoke_busy``), which the loop consults alongside its own guard.

    The slot is raised on THIS thread before the work is submitted — not inside the
    worker — because a cancel landing in the submission window would otherwise find the
    slot still clear while a worker was already queued to run. It is lowered by the
    worker when the call returns, or by the cancel path but ONLY when
    ``Future.cancel()`` proves the work never started and never will. Those are the two
    exhaustive cases, which is why the work is submitted to a private single-worker
    executor rather than through ``asyncio.to_thread``: the shared default executor
    hands back no future that can answer that question."""
    kwargs = {}
    try:
        if "tool_fence" in inspect.signature(invoke).parameters:
            kwargs["tool_fence"] = None
    except (TypeError, ValueError):  # a C/builtin callable with no introspectable signature
        pass
    holder = _host_slot()
    work = None  # the submitted synchronous call, so the cancel handler can interrogate it
    try:
        if inspect.iscoroutinefunction(invoke):
            # A coroutine invoke IS cancellable — wait_for cancels it cleanly on timeout.
            coro = invoke(prompt, session_id, **kwargs)
            reply = await (asyncio.wait_for(coro, timeout) if timeout else coro)
        else:
            # The worker thread is uncancellable — never abandon it past the timeout (see the
            # docstring). ``asyncio.wait`` returns pending tasks WITHOUT cancelling them, so on
            # timeout we drain the still-running thread before raising CoderTimeout.
            def _invoke_releasing_the_slot():
                try:
                    return invoke(prompt, session_id, **kwargs)
                finally:
                    _host_slot_release()

            with holder.lock:  # raised BEFORE submission — a cancel in the submission
                holder.busy += 1  # window must never find the slot clear
            work = holder.executor.submit(_invoke_releasing_the_slot)
            thread = asyncio.ensure_future(asyncio.wrap_future(work))
            if timeout:
                await asyncio.wait({thread}, timeout=timeout)
                if not thread.done():
                    try:
                        await thread  # drain the uncancellable worker — never leak it past the guard
                    except Exception:  # noqa: BLE001 — the drained result/error is moot; the timeout wins
                        pass
                    raise worktree.CoderTimeout(f"host self-invoke timed out after {timeout}s")
                reply = thread.result()
            else:
                reply = await thread
    except asyncio.CancelledError:
        # Operator stop / shutdown. `cancel()` is True only when the work had not started
        # and now never will — then its `finally` will never run and the slot is ours to
        # lower. False means a worker is live and still owns it; releasing here would let
        # the next self task race a call that is still on the host.
        if work is not None and work.cancel():
            _host_slot_release()
        raise
    except asyncio.TimeoutError:
        raise worktree.CoderTimeout(f"host self-invoke timed out after {timeout}s")
    except (worktree.WorktreeError, worktree.CoderTimeout):
        raise  # already normalised — never double-wrap
    except Exception as exc:  # noqa: BLE001 — normalise like dispatch_task: everything → WorktreeError
        raise worktree.WorktreeError(f"coder dispatch failed: {exc}")
    return str(reply or "")


def resolve_delegate(name: str, expect_type: str):
    """Look up a live delegate by name from the delegates registry. Returns the
    Delegate or None (not configured / wrong type / plugin disabled). Shared by
    ``loop.py`` (coder/reviewer/fusion resolution in the real dispatch path) and
    ``api.py`` (the operator-only test-rung route) — one lookup, not two copies."""
    try:
        from plugins.delegates.registry import DelegateRegistry
        from plugins.delegates.store import merged_delegates

        d = DelegateRegistry(merged_delegates()).get(name)
    except Exception:  # noqa: BLE001 — delegates plugin may be disabled
        return None
    if d is None or d.type != expect_type:
        return None
    return d


def delegate_resolver(expect_type: str):
    """ONE roster read → a ``name -> Delegate | None`` lookup. ``resolve_delegate``
    re-parses the delegates YAML (+ secrets overlay) per call, which is fine for the
    dispatch path's single lookup but not for the setup preflight resolving several
    coder names every tick / every ``/status`` poll. A disabled delegates plugin (or
    any roster failure) yields a resolver that answers None for every name."""
    try:
        from plugins.delegates.registry import DelegateRegistry
        from plugins.delegates.store import merged_delegates

        registry = DelegateRegistry(merged_delegates())
    except Exception:  # noqa: BLE001 — delegates plugin may be disabled
        return lambda _name: None

    def _get(name: str):
        try:
            d = registry.get(name)
        except Exception:  # noqa: BLE001
            return None
        if d is None or getattr(d, "type", None) != expect_type:
            return None
        return d

    return _get


# ``### path/to/file.py`` header, then a fenced block (any/no language hint) holding
# that file's COMPLETE new content. Deliberately simple/strict: a fusion completion
# that doesn't follow the format parses to no files, which just fails verify() like
# any other empty candidate — never a silent partial/mangled write.
_FUSION_FILE_RE = re.compile(r"^###\s+(\S.+?)\s*$\n```[^\n]*\n(.*?)```", re.MULTILINE | re.DOTALL)

# Defaults for `fusion_viable_for_files` — deliberately conservative. The binding
# constraint is usually the OUTPUT side (a delegate's own `max_tokens`, often
# ~1024 ⇒ ~4K chars, silently truncating a reply the adapter doesn't even expose
# `finish_reason` for), not this repo's own read logic — these caps exist so
# fusion is refused for a feature's files BEFORE a doomed rewrite is attempted,
# not tuned to "how much can Python read." Configurable per-board — see loop.py's
# `coder_solve_fusion_max_file_chars` / `_max_total_chars`.
FUSION_MAX_FILE_CHARS_DEFAULT = 8_000
FUSION_MAX_TOTAL_CHARS_DEFAULT = 16_000

# Defense-in-depth for `generate_fusion`'s write guard: a returned file under this
# fraction of the ORIGINAL file's size is treated as a likely-truncated rewrite,
# not a legitimately smaller edit, and is refused. Only applies above a minimum
# original size (`_SHRINK_GUARD_MIN_ORIGINAL_CHARS`) — a real small edit to a
# small file (e.g. a 40-char file trimmed to 10) shouldn't trip a "big shrink"
# heuristic meant to catch multi-KB truncation.
_SHRINK_GUARD_RATIO = 0.5
_SHRINK_GUARD_MIN_ORIGINAL_CHARS = 500


def fusion_viable_for_files(
    repo: str,
    files_to_modify: list[str],
    *,
    max_file_chars: int = FUSION_MAX_FILE_CHARS_DEFAULT,
    max_total_chars: int = FUSION_MAX_TOTAL_CHARS_DEFAULT,
) -> tuple[bool, str]:
    """Gate fusion on the feature's files BEFORE ever dispatching to it — whole-file
    replacement only works when a real completion can see the whole current file
    and reproduce the whole new one. Checks actual on-disk size (``os.path.getsize``,
    never reads the file into memory just to measure it). Returns ``(True, "")`` when
    every file is small enough (or doesn't exist yet — nothing to be too large),
    else ``(False, reason)``. Callers treat ``False`` exactly like "no
    fusion_delegate configured": honest degrade, not a silent truncated attempt."""
    import os

    total = 0
    for rel in files_to_modify:
        p = Path(repo) / rel
        try:
            size = os.path.getsize(p)
        except OSError:
            continue  # doesn't exist yet — nothing to be too large
        if size > max_file_chars:
            return False, f"{rel} is {size} chars, over the {max_file_chars}-char per-file cap for a full rewrite"
        total += size
    if total > max_total_chars:
        return False, f"files_to_modify total {total} chars, over the {max_total_chars}-char combined cap"
    return True, ""


def _parse_fusion_files(reply: str) -> dict[str, str]:
    """Extract ``{relative path: full file content}`` from a fusion completion. No
    match ⇒ empty dict — the caller writes nothing, and the untouched worktree just
    fails ``verify()`` like any other candidate that didn't address the task."""
    return {path.strip(): content for path, content in _FUSION_FILE_RE.findall(reply or "")}


def _fusion_prompt(
    task: str,
    *,
    feedback: str | None,
    repo: str,
    files_to_modify: list[str],
    max_file_chars: int = FUSION_MAX_FILE_CHARS_DEFAULT,
) -> str:
    """Build fusion's prompt. Fusion can't read the repo itself (no tool-calling), so
    this hands it the CURRENT content of every file the feature declares — read from
    the base repo, best-effort (a listed-but-not-yet-created file is noted as new).

    Callers are expected to have already checked ``fusion_viable_for_files`` (the
    real gate) so a genuinely oversized file should never reach here — this
    truncation is a DEFENSIVE backstop only (e.g. a direct ``test_rung`` call that
    skipped the gate), and unlike the gate it's never silent: a truncated file is
    marked as such so fusion knows not to claim a full-file replacement for it."""
    file_blocks = []
    for rel in files_to_modify:
        p = Path(repo) / rel
        try:
            raw = p.read_text(errors="replace")
        except OSError:
            file_blocks.append(f"### {rel} (does not exist yet — you are creating it)")
            continue
        if len(raw) > max_file_chars:
            text = raw[:max_file_chars]
            file_blocks.append(
                f"### {rel} (current content — TRUNCATED at {max_file_chars} chars, "
                f"real file is {len(raw)} chars — do NOT return this as a complete "
                "replacement; skip this file instead)\n```\n"
                f"{text}\n```"
            )
        else:
            file_blocks.append(f"### {rel} (current content)\n```\n{raw}\n```")
    files_section = (
        "\n\n".join(file_blocks) if file_blocks else "(no existing files listed — create what the task needs)"
    )
    parts = [
        "Implement the task below. You have NO tool access — you cannot read or run "
        "anything else, so work only from what's given here.",
        "",
        "## Task",
        task.strip(),
        "",
        "## Current file contents",
        files_section,
        "",
        "## Your reply format — REQUIRED, exactly this shape per file",
        "For every file you create or change, return its COMPLETE, FINAL content "
        "(never a partial diff or `...` elisions) as:",
        "### relative/path/to/file.py",
        "```",
        "<the file's entire new content>",
        "```",
        "Only include files you're actually creating or changing. No prose outside the file blocks.",
    ]
    if feedback:
        parts += [
            "",
            "## Your previous attempt FAILED the acceptance tests — fix exactly this",
            feedback.strip(),
        ]
    return "\n".join(parts)


class SolveExhausted(worktree.WorktreeError):
    """``coder.solve()`` spent its whole generation budget against this feature's
    acceptance tests and no candidate passed. A CAPABILITY failure for the CURRENT
    model tier — real diffs existed, they just failed the tests, so this is NOT
    "no diff" — but the loop treats it exactly like ``NoChangesError``/
    ``CoderTimeout``: escalate a configured tier ladder, or block. Never opens a PR
    on an unverified best-partial (ADR 0064's honest-degrade contract)."""


def _import_solve():
    """Best-effort import of the `coder` plugin's solve library. Returns the module,
    or ``None`` — `coder` is a separate, git-URL-installed plugin (ADR 0064), not a
    dependency of this one, and it ships DISABLED by default, so absent/disabled is
    the expected common case (not an error worth logging)."""
    try:
        return importlib.import_module("plugins.coder.solve")
    except Exception:  # noqa: BLE001 — coder plugin absent/disabled → honest degrade
        return None


def should_use_solve(feature: dict, *, test_cmd: str, _solve_mod=None) -> bool:
    """The P2 dispatch decision (ADR 0064): use `coder.solve()` only when ALL of —
    the `coder` plugin is importable, the feature carries acceptance criteria (the
    Ready gate's oracle), and a runnable acceptance-test command is configured (the
    actual verifier `solve()` gates on — prose acceptance criteria alone isn't
    executable). Missing any ⇒ False, the honest degrade to a single delegate_to(acp)
    shot. ``_solve_mod`` is a test-injection seam; production callers never pass it —
    the real best-effort import happens here."""
    mod = _solve_mod if _solve_mod is not None else _import_solve()
    if mod is None:
        return False
    if not str(feature.get("acceptance_criteria") or "").strip():
        return False
    if not str(test_cmd or "").strip():
        return False
    return True


def _augment_prompt(task: str, feedback: str | None) -> str:
    """Fold the ladder's failing-test feedback into the next candidate's prompt.
    Every candidate gets a FRESH worktree off base (see ``_WorktreeSolveAdapter`` —
    the same "fresh-both" discipline ``worktree.dispatch_coder`` already documents
    for re-dispatches), so the coder is told explicitly there is no prior diff to
    build on in THIS worktree — only the failure to fix."""
    if not feedback:
        return task
    return (
        f"{task}\n\n"
        "## Your previous attempt FAILED the acceptance tests — fix exactly this\n"
        "This is a fresh worktree (no prior diff here); re-implement with the failure "
        f"below in mind:\n{feedback.strip()}\n"
    )


# #146 circuit breaker — a failure SIGNATURE for cross-candidate dedup. The intent
# is to tell "K DIFFERENT failures" (real search, keep going) apart from "K copies of
# the SAME failure" (a spec smell — the assertion is unsatisfiable, not the model's
# fault), so the signature is the pair the spec names: the failing pytest node id
# (``FAILED tests/…::test_name``) plus the ``AssertionError:`` line. The FAILED node
# id is anchored to a line start (pytest's short-summary form); the AssertionError
# line is matched anywhere so BOTH the inline short-summary form (``FAILED … -
# AssertionError: …``) and the ``E   AssertionError: …`` traceback form are caught.
_FAILED_TEST_RE = re.compile(r"^FAILED\s+(\S+::\S+)", re.MULTILINE)
_ASSERTION_LINE_RE = re.compile(r"AssertionError:[^\n]*")


def _failure_signature(output: str) -> str | None:
    """Extract a stable failure signature from a candidate's verify output — the
    failing test's node id and the ``AssertionError:`` line — so identical failures
    across candidates collapse to the same key. Returns ``None`` when NEITHER pattern
    is present (nothing to dedup on — a non-assertion error, an empty output), which
    can never trip the circuit breaker; only a recognizable, repeatable assertion
    signature counts."""
    text = output or ""
    test_m = _FAILED_TEST_RE.search(text)
    assert_m = _ASSERTION_LINE_RE.search(text)
    if test_m is None and assert_m is None:
        return None
    test = test_m.group(1) if test_m else ""
    assertion = assert_m.group(0).strip() if assert_m else ""
    return f"{test} :: {assertion}".strip()


class _WorktreeSolveAdapter:
    """Adapts `coder.solve()`'s ``generate``/``verify`` contract onto board
    worktrees. `solve()` treats a candidate as an opaque string; here that string is
    a candidate's WORKTREE PATH, not code text — the coder edits files, it doesn't
    return a source string. Each ``generate()`` call creates a fresh throwaway
    worktree, dispatches the ACP coder into it, and hands the path back; ``verify()``
    then runs the acceptance-test command in that same worktree and reports real
    pass/fail. Tracks every candidate worktree it creates so the caller can promote
    the winner and reap the losers."""

    # #146: when this many candidates fail on the IDENTICAL assertion signature, the
    # problem is the spec (an unsatisfiable assertion), not model capability — keep
    # searching and we just burn the rest of the budget re-failing the same way. At
    # the threshold, `verify()` raises SolveExhausted early, naming the repeated
    # assertion, and the loop's existing SolveExhausted handler blocks the feature
    # with that quoted — the correct outcome for a spec smell.
    CIRCUIT_BREAKER_THRESHOLD = 3

    def __init__(
        self,
        *,
        repo: str,
        base: str,
        root: str,
        fid: str,
        coder,
        dispatch_timeout: float | None,
        test_cmd: str,
        test_timeout: float,
        verdict_cls,
        fusion_delegate=None,
        files_to_modify: list[str] | None = None,
        fusion_max_file_chars: int = FUSION_MAX_FILE_CHARS_DEFAULT,
        env_passthrough: Iterable[str] = (),
        progress_fid: str | None = None,
        progress_tier: str = "",
        max_concurrent_sessions: int = 0,
        _fusion_dispatch=None,
    ):
        self.repo = repo
        self.base = base
        self.root = root
        self.fid = fid
        self.coder = coder
        # Live-monitoring (#84): the REAL board feature id the drawer polls (which is
        # NOT self.fid for the test-rung diagnostic — that appends ".test") and the
        # current tier, so each candidate gen surfaces under the right feature. None
        # ⇒ don't record (the operator-only test-rung path — no live board run).
        self.progress_fid = progress_fid
        self.progress_tier = progress_tier
        self._gen_by_wt: dict[str, int] = {}  # candidate worktree → its gen (for verify recording)
        self.dispatch_timeout = dispatch_timeout
        self.test_cmd = test_cmd
        self.test_timeout = test_timeout
        self.verdict_cls = verdict_cls  # `plugins.coder.solve.Verdict` — passed in, never imported here
        # The gate's env_passthrough whitelist (#86), threaded from the loop so the
        # acceptance-test (verify) subprocess sees the SAME allowlist environment the
        # gate/preflight/format subprocesses do (F8a/F8b) — the host's
        # PROTOAGENT_*/A2A_*/AGENT_NAME must never leak into a candidate's tests, and
        # nothing outside the baseline reaches them unless a deployment names it here.
        self.env_passthrough = tuple(env_passthrough)
        self.fusion_delegate = fusion_delegate  # a resolved `openai`-type Delegate, or None
        self.files_to_modify = files_to_modify or []
        self.fusion_max_file_chars = fusion_max_file_chars
        # Concurrency cap: `max_concurrent_sessions` limits how many ACP dispatches this
        # solve run holds open simultaneously. 0 (default) = unlimited within the k budget.
        # The best-of-k rung dispatches `k` candidates concurrently via asyncio.gather;
        # peak sessions per drive = coder_solve_k (or fusion_k for the fusion rung).
        # Across multiple concurrent drives (max_concurrent > 1), peak is max_concurrent ×
        # coder_solve_k. Set max_concurrent_sessions=1 to serialize candidates within a
        # drive — useful when the host can support only one ACP process at a time.
        self._session_sem: asyncio.Semaphore | None = (
            asyncio.Semaphore(max(1, max_concurrent_sessions)) if max_concurrent_sessions > 0 else None
        )
        # Test-injection seam (mirrors `_solve`/`_budget_cls`/`_verdict_cls` on
        # `dispatch()`): production never passes this — the real lazy
        # `ADAPTERS["openai"].dispatch` import happens in `generate_fusion` below.
        self._fusion_dispatch = _fusion_dispatch
        self.candidates: list[tuple[str, str]] = []  # (worktree_path, branch)
        # `git worktree add` against the SAME repo must not run concurrently (best-
        # of-k dispatches `generate()` via asyncio.gather) — serialize just that
        # step; the slow part (the coder dispatch) still runs in parallel.
        self._wt_lock = asyncio.Lock()
        self._n = 0
        # worktree_path -> the coder's own final reply (its clean PR summary, per
        # `loop._build_prompt`'s "your FINAL message becomes the PR description"
        # contract) — captured so `dispatch()` can use the WINNING candidate's real
        # summary as the PR body instead of an internal rung/gens diagnostic string.
        # Also what `_verify_goal`'s NO_TEST_NEEDED escape hatch reads. Fusion has no
        # such reply (a plain completion, not a summary) — absent for fusion wins.
        self._replies: dict[str, str] = {}
        # #146: a FAILING sibling candidate's verify output, accumulated across this
        # tier's best-of-k so a later candidate's generate() can learn from what
        # already failed. solve() only threads `feedback` into a rung's SEQUENTIAL
        # retries; PARALLEL best-of-k candidates (dispatched via asyncio.gather) each
        # arrive with feedback=None, so without this every candidate re-attacks the
        # task blind to the exact assertion its siblings already tripped. Each entry
        # is one prior candidate's labeled verify tail; generate() folds the whole
        # accumulated list BELOW the ladder's own feedback via `_augment_prompt`.
        self._completed_failures: list[str] = []
        # #146 circuit breaker: signature (failing test node id + AssertionError line)
        # → how many candidates have failed on exactly it. When any single count hits
        # CIRCUIT_BREAKER_THRESHOLD, `verify()` short-circuits the whole ladder with
        # SolveExhausted rather than spend the rest of the budget re-failing identically.
        self._failure_signatures: dict[str, int] = {}

    async def _new_candidate_worktree(self) -> tuple[str, str]:
        self._n += 1
        cid = f"{self.fid}.g{self._n}"
        async with self._wt_lock:
            wt, branch = await worktree.create_worktree(self.repo, self.base, cid, self.root)
        self.candidates.append((wt, branch))
        return wt, branch

    def _compose_feedback(self, feedback: str | None) -> str | None:
        """Fold this tier's accumulated sibling failures (#146) below the ladder's
        own retry ``feedback``. Kept as ONE feedback string so ``_augment_prompt``
        stays the single place that shapes the failure section of the prompt. The
        two blocks are clearly separated so the coder can tell the ladder's own
        prior-attempt failure apart from other candidates' failures at this tier."""
        if not self._completed_failures:
            return feedback
        prior = (
            "## Prior candidate failures at this tier (sibling best-of-k attempts)\n"
            "Earlier candidates solving THIS SAME task already failed verify with the "
            "errors below — a different attempt from yours, not your own prior try. "
            "Do not repeat their mistakes:\n\n" + "\n\n".join(self._completed_failures)
        )
        own = (feedback or "").strip()
        return f"{own}\n\n{prior}" if own else prior

    async def generate(self, task: str, *, feedback: str | None = None) -> str:
        wt, _branch = await self._new_candidate_worktree()
        self._gen_by_wt[wt] = self._n
        # Tapped dispatch (#84) wires the ACP client's callbacks into this gen's live
        # buffer; it degrades to worktree.dispatch_coder when the tap can't wire.
        # The semaphore (when set) serializes concurrent best-of-k ACP dispatches to
        # honour max_concurrent_sessions; worktree creation above is already complete
        # and unaffected (serialized independently by _wt_lock).
        if self._session_sem is not None:
            async with self._session_sem:
                reply = await dispatch_coder_tapped(
                    self.coder,
                    wt,
                    _augment_prompt(task, self._compose_feedback(feedback)),
                    fid=self.progress_fid,
                    gen=self._n,
                    tier=self.progress_tier,
                    timeout=self.dispatch_timeout,
                    env_passthrough=self.env_passthrough,
                )
        else:
            reply = await dispatch_coder_tapped(
                self.coder,
                wt,
                _augment_prompt(task, self._compose_feedback(feedback)),
                fid=self.progress_fid,
                gen=self._n,
                tier=self.progress_tier,
                timeout=self.dispatch_timeout,
                env_passthrough=self.env_passthrough,
            )
        if (reply or "").strip():
            self._replies[wt] = reply
        return wt

    async def generate_fusion(self, task: str, *, feedback: str | None = None) -> str:
        """Rung 4 (ADR 0064 P3): fusion can't tool-call, so instead of dispatching an
        ACP session into the worktree, get a plain completion and write its files into
        one. Same candidate bookkeeping (``candidates``/``_wt_lock``) as ``generate``,
        so promote/reap treats a fusion winner identically to an ACP one."""
        if self._fusion_dispatch is not None:
            openai_dispatch = self._fusion_dispatch
        else:
            from plugins.delegates.adapters import ADAPTERS

            openai_dispatch = ADAPTERS["openai"].dispatch

        prompt = _fusion_prompt(
            task,
            feedback=feedback,
            repo=self.repo,
            files_to_modify=self.files_to_modify,
            max_file_chars=self.fusion_max_file_chars,
        )
        reply = await openai_dispatch(self.fusion_delegate, prompt, timeout=self.dispatch_timeout)
        files = _parse_fusion_files(reply)
        wt, _branch = await self._new_candidate_worktree()
        self._gen_by_wt[wt] = self._n
        # Fusion can't tool-call (a plain completion, not an ACP session), so there's
        # no live tool/thought stream — still register the gen (#84) with a synthetic
        # marker so the drawer shows a fusion candidate and its verify outcome.
        progress_begin(self.progress_fid, self._n, self.progress_tier)
        progress_tool(
            self.progress_fid, self._n, {"phase": "start", "id": "fusion", "name": "fusion completion", "kind": "edit"}
        )
        # The whole fallible body rides one try/finally so the gen closes on EVERY
        # exit path — the same totality dispatch_coder_tapped guarantees (panel round 2).
        _fusion_ok = False
        try:
            wt_root = Path(wt).resolve()
            written = 0
            for rel, content in files.items():
                # `rel` comes from a model completion — an absolute path or a `../` climb
                # would otherwise write outside the worktree (Path.__truediv__ with an
                # absolute right-hand side even discards the left side entirely). Resolve
                # and require containment; skip (don't crash the candidate) on a miss.
                dest = (wt_root / rel).resolve()
                if wt_root not in dest.parents and dest != wt_root:
                    log.warning(
                        "[project_board] %s fusion tried to write outside its worktree: %r — skipped", self.fid, rel
                    )
                    continue
                # Fusion has no tool access — it can only ever act on the files we showed
                # it. A path outside the feature's declared set means it hallucinated a
                # file (or the parser mis-split the reply); writing it would silently
                # touch unrelated code with no test coverage backing the change.
                if self.files_to_modify and rel not in self.files_to_modify:
                    log.warning(
                        "[project_board] %s fusion tried to write an undeclared path: %r — skipped", self.fid, rel
                    )
                    continue
                # Fusion returns whole-file replacements with no diff to sanity-check.
                # A reply that's drastically smaller than the file it claims to replace
                # is far more likely a truncated completion (see FUSION_MAX_FILE_CHARS_DEFAULT
                # and the delegate's own max_tokens ceiling) than an intentional big
                # deletion — refuse it rather than risk silent data loss.
                if dest.exists():
                    try:
                        original_size = dest.stat().st_size
                    except OSError:
                        original_size = 0
                    if (
                        original_size > _SHRINK_GUARD_MIN_ORIGINAL_CHARS
                        and len(content) < original_size * _SHRINK_GUARD_RATIO
                    ):
                        log.warning(
                            "[project_board] %s fusion's rewrite of %r (%d chars) is suspiciously smaller than "
                            "the original (%d chars) — refusing, likely truncated",
                            self.fid,
                            rel,
                            len(content),
                            original_size,
                        )
                        continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content)
                written += 1
            if not written:
                log.warning(
                    "[project_board] %s fusion reply parsed to 0 writable files — candidate is unchanged base", self.fid
                )
            _fusion_ok = True
            return wt
        finally:
            # The tool card must CLOSE on failure too — otherwise the drawer shows a
            # perpetually-running "fusion completion" on a gen that is already done
            # (panel round 3). Status derives from whether the body completed.
            progress_tool(
                self.progress_fid,
                self._n,
                {
                    "phase": "end",
                    "id": "fusion",
                    "name": "fusion completion",
                    "status": "completed" if _fusion_ok else "failed",
                },
            )
            progress_end(self.progress_fid, self._n)

    async def verify(self, candidate_wt: str):
        verdict = await self._run_acceptance_tests(candidate_wt)
        # Record the per-gen verify outcome (#84) for the live monitor — which test
        # command ran + a tail of its output — keyed by the candidate's worktree →
        # its gen. Best-effort; a no-op off a live board run (progress_fid None).
        gen = self._gen_by_wt.get(candidate_wt)
        if gen is not None and self.progress_fid:
            progress_verify(
                self.progress_fid,
                gen,
                test_cmd=self.test_cmd,
                output=getattr(verdict, "output", "") or "",
                passed=bool(getattr(verdict, "passed", False)),
            )
        # #146: a candidate that FAILED verify feeds its output forward to the sibling
        # best-of-k candidates still to run at this tier (see `_completed_failures`).
        # Independent of progress_fid — this is the solve feedback path, not the live
        # monitor. Tail-capped at 1500 chars, matching progress_verify's own cap.
        if not bool(getattr(verdict, "passed", False)):
            output = getattr(verdict, "output", "") or ""
            tail = output[-1500:]
            label = f"candidate {gen}" if gen is not None else "a prior candidate"
            self._completed_failures.append(f"### {label} failed `{self.test_cmd}`:\n{tail}".rstrip())
            # #146 circuit breaker: when K candidates fail on the IDENTICAL assertion
            # signature, the spec is unsatisfiable — not a model-capability failure — so
            # continuing to search only re-fails the same way and burns the budget. At
            # the threshold, raise SolveExhausted early, naming the repeated assertion:
            # solve() has no try/except around verify(), so this propagates straight out
            # to dispatch()'s handler (which reaps every candidate and re-raises), and
            # the loop treats SolveExhausted as a capability failure and blocks the
            # feature with the message quoted — the right call for a spec smell.
            sig = _failure_signature(output)
            if sig is not None:
                count = self._failure_signatures[sig] = self._failure_signatures.get(sig, 0) + 1
                if count >= self.CIRCUIT_BREAKER_THRESHOLD:
                    raise SolveExhausted(
                        f"circuit breaker tripped: {count} candidates failed on the IDENTICAL "
                        f"assertion — a spec problem, not model capability. Repeated failure: {sig}"
                    )
        return verdict

    async def _run_acceptance_tests(self, candidate_wt: str):
        Verdict = self.verdict_cls
        try:
            proc = await asyncio.create_subprocess_shell(
                self.test_cmd,
                cwd=candidate_wt,
                # #86: with NO env= the child inherits os.environ verbatim (the host's
                # PROTOAGENT_*/A2A_*/AGENT_NAME), which burned 15 solve gens on an
                # unwinnable test. F8b: this child runs a repo-defined command over
                # coder-written code — exactly the posture of the loop's own gate/
                # format/preflight children (F8a) — so it gets the narrow allowlist
                # baseline plus ``env_passthrough``, not os.environ minus the host block.
                env=config.sanitized_env(self.env_passthrough, mode="allowlist"),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            return Verdict(passed=False, total=1, failed=1, output=f"could not launch acceptance tests: {exc}")
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.test_timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            # Unlike the pre-PR local gate (which fails OPEN on a timeout — a broken
            # gate must never block otherwise-good work), THIS is the ladder's own
            # search oracle: a candidate we couldn't confirm passed must never be
            # silently treated as passing, or we'd be faking grounding.
            return Verdict(
                passed=False, total=1, failed=1, output=f"acceptance tests timed out after {self.test_timeout:.0f}s"
            )
        text = (out or b"").decode("utf-8", "replace").strip()
        ok = proc.returncode == 0
        return Verdict(
            passed=ok,
            total=1,
            failed=0 if ok else 1,
            failing=[] if ok else [f"{self.test_cmd!r} (exit {proc.returncode})"],
            output=text[-4000:],
        )


RecordGens = Callable[[int], None]
# The verify-boundary salvage record (#91): called with (branch, sha, worktree) once a
# candidate has PASSED its acceptance tests and been promoted — the loop persists it on
# the bead (store.record_verified_candidate) so a crash between here and open_pr can
# resume the verified build instead of rebuilding fresh.
RecordVerified = Callable[[str, str, str], None]


def _record_gens_best_effort(record_gens: RecordGens, fid: str, n: int) -> None:
    """Call ``record_gens(n)`` and swallow any exception it raises.

    ``store.record_gens_spent`` documents itself as fire-and-forget ("a br hiccup
    here must never fail the build the way a missing PR would") — a transient `br`
    failure (lock contention, a flaky CLI invocation, a race with a concurrent
    label write) must never propagate out of ``dispatch()``. Left unguarded, it
    would surface as an unrelated ``BoardError`` past every capability-failure
    handler in the caller's loop, discarding an already-verified (or already-
    reaped) candidate purely because of a bookkeeping label write."""
    try:
        record_gens(n)
    except Exception:  # noqa: BLE001 — fire-and-forget cost accounting, never fails the build
        log.warning("[project_board] %s record_gens(%d) failed (ignored — fire-and-forget)", fid, n, exc_info=True)


async def dispatch(
    *,
    task: str,
    coder,
    repo: str,
    base: str,
    root: str,
    fid: str,
    dispatch_timeout: float | None,
    test_cmd: str,
    test_timeout: float,
    budget: int,
    k: int,
    tree_depth: int,
    record_gens: RecordGens | None = None,
    fusion_delegate=None,
    fusion_k: int = 2,
    files_to_modify: list[str] | None = None,
    fusion_max_file_chars: int = FUSION_MAX_FILE_CHARS_DEFAULT,
    env_passthrough: Iterable[str] = (),
    tier: str = "",
    record_verified: RecordVerified | None = None,
    commit_message: str = "",
    title: str = "",
    max_concurrent_sessions: int = 0,
    _solve=None,
    _budget_cls=None,
    _verdict_cls=None,
    _fusion_dispatch=None,
) -> tuple[str, str, str]:
    """Run the execution-grounded ladder for one feature build.

    Returns ``(worktree, branch, result_text)`` on a passing candidate — the SAME
    3-tuple shape ``_dispatch_max_mode`` returns, so the caller's downstream drive
    (fixups, local gate, ``open_pr``) is unchanged. ``result_text`` is the WINNING
    candidate's own reply (its clean PR summary) when the ladder reached it via an
    ACP rung; only a fusion win (no natural-language reply, just file content) falls
    back to an internal rung/gens diagnostic string. Raises :class:`SolveExhausted`
    (a capability failure) when the budget is spent with no passing candidate, after
    reaping every candidate worktree it created.

    ``record_gens`` (if given) is called with ``result.gens_spent`` exactly once,
    win or lose — the cost accounting (ADR 0064) must survive a failed search too.
    ``record_verified`` (if given) is called once with the promoted winner's
    ``(branch, sha, worktree)`` at the verify boundary — the crash-salvage record
    (#91), persisted by the loop so a crash between here and ``open_pr`` resumes the
    verified build instead of rebuilding fresh. ``commit_message`` names the commit
    that gives the verified tree its sha (the loop passes the PR title, so the
    shipped commit message is unchanged from what ``open_pr`` would have written).
    ``title`` (#227) is the RAW feature title — it picks the promoted canonical
    branch/dir's human ``-<slug>`` tail (via ``worktree.branch_name``), so the branch the
    loop later recovers/reaps by ``branch_name(fid, title)`` matches the one promoted
    here. Absent (candidate worktrees never slug), the canonical stays the bare
    ``feat-<id>``.
    ``_solve``/``_budget_cls``/``_verdict_cls`` are test-injection seams for
    ``solve()``/``Budget``/``Verdict``; production callers never pass them (the real
    import happens here, deferred so this module carries no hard dependency on the
    `coder` plugin).

    ``fusion_delegate`` (a resolved ``openai``-type Delegate, or ``None``) gates rung
    4 (ADR 0064 P3) — the caller resolves it (mirroring how ``coder`` itself is
    resolved), so this module never does delegate lookup. ``None`` (unconfigured) ⇒
    ``solve()`` gets ``fusion_generate=None`` and stops at tree-search, unchanged from
    before this rung existed. ``files_to_modify`` feeds fusion's prompt (it can't read
    the repo itself, unlike the ACP rungs) — the same list the feature's Ready gate
    already required.

    ``env_passthrough`` (#86) is the loop's env whitelist, threaded through to the
    adapter so the acceptance-test (verify) subprocess strips the same host
    identity/credential block (``PROTOAGENT_*``/``A2A_*``/``AGENT_NAME``) the gate and
    preflight already strip — with no ``env=`` the verify child would inherit the host's
    whole environment and could pass/fail on the HOST's identity, not the candidate's.

    **Concurrency note.** ``max_concurrent`` is feature-level: up to that many drives run
    simultaneously, each invoking ``dispatch()`` once. Within a single drive the
    best-of-k rung of ``solve()`` dispatches ``k`` ACP sessions concurrently
    (``asyncio.gather``), so peak ACP processes = ``max_concurrent × coder_solve_k``.
    ``max_concurrent_sessions`` (default 0 = unlimited within the k budget) caps the
    concurrent ACP dispatches within THIS call — set to 1 to run k candidates
    serially when the host cannot sustain that many parallel processes.

    **``solve()`` itself can raise.** The ladder (`coder`'s own ``solve.py``) has no
    try/except around ``generate``/``verify`` — it assumes a candidate attempt never
    errors, only that it might fail its tests. A REAL dispatch can still raise
    (``CoderTimeout`` on one best-of-k candidate, a worktree op erroring) and that
    propagates straight out of ``solve()`` uncaught. Every worktree ``generate()``
    already created for THIS run is tracked in ``adapter.candidates`` (appended right
    after ``create_worktree`` returns, before the dispatch that might fail) but would
    otherwise leak forever: it's untracked in the loop's ``_inflight`` map until this
    function returns, and invisible to the health sweep (a `.gN` candidate id isn't a
    real board feature, so the sweep's own ``get_feature`` lookup raises and the sweep
    skips it rather than reaping). So any exception here reaps every candidate seen so
    far, surfaces the attempted cost, and re-raises the ORIGINAL exception unchanged —
    the loop's existing capability-failure handling (retry/escalate/block) still
    applies to whatever it actually was."""
    if _solve is not None:
        solve, Budget, Verdict = _solve, _budget_cls, _verdict_cls
    else:
        from plugins.coder.solve import Budget, Verdict, solve

    adapter = _WorktreeSolveAdapter(
        repo=repo,
        base=base,
        root=root,
        fid=fid,
        coder=coder,
        dispatch_timeout=dispatch_timeout,
        test_cmd=test_cmd,
        test_timeout=test_timeout,
        verdict_cls=Verdict,
        fusion_delegate=fusion_delegate,
        files_to_modify=files_to_modify,
        fusion_max_file_chars=fusion_max_file_chars,
        env_passthrough=env_passthrough,
        progress_fid=fid,
        progress_tier=tier,
        max_concurrent_sessions=max_concurrent_sessions,
        _fusion_dispatch=_fusion_dispatch,
    )
    try:
        result = await solve(
            task,
            generate=adapter.generate,
            verify=adapter.verify,
            budget=Budget(budget),
            k=k,
            tree_depth=tree_depth,
            fusion_generate=adapter.generate_fusion if fusion_delegate is not None else None,
            fusion_k=fusion_k,
        )
    except Exception as exc:
        for wt, branch in adapter.candidates:
            await worktree.remove_worktree(repo, wt, branch)
        if record_gens is not None and adapter._n:
            # `solve()` never got to return a `gens_spent` count — the attempted
            # generation count is the honest stand-in (a failed dispatch still spent
            # the gen; ADR 0064's cost accounting doesn't get to look the other way).
            # Best-effort per store.record_gens_spent's own contract ("a br hiccup
            # here must never fail the build"): the worktrees above are ALREADY
            # reaped and the original exception below is what the loop must see —
            # a transient `br` failure recording the spend must never mask it.
            _record_gens_best_effort(record_gens, fid, adapter._n)
        log.warning(
            "[project_board] %s coder.solve raised mid-ladder (%d candidate(s) reaped): %s",
            fid,
            len(adapter.candidates),
            exc,
        )
        raise
    if record_gens is not None:
        # Same fire-and-forget contract as above: a bookkeeping failure here must
        # never discard a candidate that ALREADY exists on disk (test-verified or
        # not) — the promote/reap logic below still has to run either way.
        _record_gens_best_effort(record_gens, fid, result.gens_spent)

    if not result.passed or not result.solution:
        for wt, branch in adapter.candidates:
            await worktree.remove_worktree(repo, wt, branch)
        detail = result.verdict.feedback() if result.verdict else ""
        log.info(
            "[project_board] %s coder.solve exhausted (rung=%s, gens=%d/%d) — no candidate passed",
            fid,
            result.rung,
            result.gens_spent,
            budget,
        )
        raise SolveExhausted(
            f"coder.solve exhausted after {result.gens_spent} generation(s) (rung={result.rung}): "
            f"{detail or result.note}"
        )

    win_wt = result.solution
    win_branch = next(b for wt, b in adapter.candidates if wt == win_wt)
    canon_wt, canon_branch = await worktree.promote_worktree(repo, win_wt, win_branch, fid, root, title=title)
    # The verify boundary's crash-salvage record (#91): the candidate PASSED its
    # acceptance tests but open_pr is still ahead (fixups + the pre-PR gate can take
    # minutes) — a crash in that window used to throw the whole verified build away.
    # Commit the verified tree so its content has a real sha, then persist
    # {branch, sha, worktree} via `record_verified` (the loop writes it on the bead)
    # so recovery can resume at promote→fixups→gate→open_pr instead of re-solving.
    # Best-effort: a bookkeeping failure must never fail a build that already passed.
    if record_verified is not None:
        try:
            await worktree.commit_worktree(canon_wt, commit_message or f"feat: {fid} (verified candidate)")
            rc, head, _err = await worktree._git(canon_wt, "rev-parse", "HEAD")
            sha = (head or "").strip()
            if rc == 0 and sha:
                record_verified(canon_branch, sha, canon_wt)
        except Exception:  # noqa: BLE001 — fire-and-forget salvage bookkeeping
            log.warning("[project_board] %s could not record the verified candidate (ignored)", fid, exc_info=True)
    for wt, branch in adapter.candidates:
        if wt != win_wt:
            await worktree.remove_worktree(repo, wt, branch)
    log.info(
        "[project_board] %s coder.solve verified by acceptance tests (rung=%s, gens=%d/%d)",
        fid,
        result.rung,
        result.gens_spent,
        budget,
    )
    # The winning candidate's own reply (its clean PR summary, per the "your FINAL
    # message becomes the PR description" contract every coder dispatch is given) is
    # the real result — `loop.py` uses this verbatim as the PR body, and `_verify_goal`
    # reads it for the NO_TEST_NEEDED escape hatch. Only fusion (a plain completion,
    # no such reply) or an unexpectedly-empty one falls back to the diagnostic string.
    result_text = (
        adapter._replies.get(win_wt) or f"[coder.solve rung={result.rung} gens={result.gens_spent}] {result.note}"
    )
    return canon_wt, canon_branch, result_text


async def test_rung(
    *,
    rung: str,
    task: str,
    coder,
    repo: str,
    base: str,
    root: str,
    fid: str,
    dispatch_timeout: float | None,
    test_cmd: str,
    test_timeout: float,
    budget: int = 10,
    k: int = 3,
    tree_depth: int = 2,
    fusion_delegate=None,
    fusion_k: int = 2,
    files_to_modify: list[str] | None = None,
    fusion_max_file_chars: int = FUSION_MAX_FILE_CHARS_DEFAULT,
    env_passthrough: Iterable[str] = (),
    _solve=None,
    _budget_cls=None,
    _verdict_cls=None,
    _fusion_dispatch=None,
) -> dict:
    """Operator-only diagnostic (ADR 0064): run exactly ONE named rung of
    ``coder.solve()`` against a feature's REAL acceptance tests, in a throwaway
    worktree that is ALWAYS reaped afterward — never promoted, no PR opened, no
    board state touched. For verifying a rung actually works (especially fusion,
    only otherwise reached after three cheaper rungs fail) without contriving a
    task hard enough to fail its way there.

    Deliberately separate from ``dispatch()``: that function's contract (promote
    the winner, raise ``SolveExhausted`` on exhaustion) is shaped for the board's
    real per-feature build — mixing test semantics into it would risk the real
    dispatch path. This is exposed to operators only via api.py's ``test-rung``
    route, which carries NO ``@tool`` wrapper — the board's own lead agent has no
    way to call this itself (see api.py's docstring for the same boundary the
    plugin already draws around ``/features/{id}/cancel`` etc.)."""
    if _solve is not None:
        solve, Budget, Verdict = _solve, _budget_cls, _verdict_cls
    else:
        from plugins.coder.solve import Budget, Verdict, solve

    adapter = _WorktreeSolveAdapter(
        repo=repo,
        base=base,
        root=root,
        fid=f"{fid}.test",
        coder=coder,
        dispatch_timeout=dispatch_timeout,
        test_cmd=test_cmd,
        test_timeout=test_timeout,
        verdict_cls=Verdict,
        fusion_delegate=fusion_delegate,
        files_to_modify=files_to_modify,
        fusion_max_file_chars=fusion_max_file_chars,
        env_passthrough=env_passthrough,
        _fusion_dispatch=_fusion_dispatch,
    )
    try:
        result = await solve(
            task,
            generate=adapter.generate,
            verify=adapter.verify,
            budget=Budget(budget),
            k=k,
            tree_depth=tree_depth,
            fusion_generate=adapter.generate_fusion if fusion_delegate is not None else None,
            fusion_k=fusion_k,
            force_rung=rung,
        )
    finally:
        # ALWAYS reap — pass or fail, this is a diagnostic run, never a real build.
        for wt, branch in adapter.candidates:
            await worktree.remove_worktree(repo, wt, branch)
    return {
        "rung": result.rung,
        "passed": result.passed,
        "gens_spent": result.gens_spent,
        "candidates_tried": result.candidates_tried,
        "note": result.note,
        "verdict_output": result.verdict.output if result.verdict else "",
    }
