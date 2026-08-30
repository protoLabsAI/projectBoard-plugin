"""Live coder-monitoring tests (#84) — the per-feature, per-gen in-memory ring
buffer in coder_seam.py that the ACP dispatch tap fills and the board view's
monitor drawer polls over GET …/features/{fid}/progress.

Pure-Python + host-free: the registry, the callback-fed buffer bounds (rolling
thought tail, capped tool history, LRU feature eviction), and the tapped-dispatch
FALLBACK path (host absent → untapped worktree.dispatch_coder, gen still recorded)
are all exercised without the protoAgent host. The `dispatch()` integration reuses
the same solve/Verdict fakes the coder_seam suite uses so no `coder` plugin is
needed either.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field

from project_board import coder_seam, store as store_mod, worktree


# ── the ring buffer: current/last tool, history cap, thought bound, usage, verify ──


def test_snapshot_unknown_feature_is_empty_but_valid():
    coder_seam._progress.clear()
    assert coder_seam.progress_snapshot("nope") == {"gens": []}


def test_progress_functions_noop_on_a_falsy_fid():
    """The operator-only test-rung path passes fid=None — recording must be a no-op,
    never a crash and never a stray entry under an empty key."""
    coder_seam._progress.clear()
    coder_seam.progress_begin(None, 1, "fast")
    coder_seam.progress_tool(None, 1, {"phase": "start", "name": "x"})
    coder_seam.progress_thought(None, 1, "hi")
    coder_seam.progress_usage(None, 1, {"used": 1, "size": 2})
    assert coder_seam.progress_snapshot("") == {"gens": []}


def test_tool_start_then_end_updates_current_tool_and_history():
    coder_seam._progress.clear()
    coder_seam.progress_begin("f", 1, "smart")
    coder_seam.progress_tool("f", 1, {"phase": "start", "id": "t1", "name": "read_file", "input": '{"path": "x.py"}'})
    g = coder_seam.progress_snapshot("f")["gens"][0]
    assert g["tier"] == "smart"
    assert g["current_tool"]["status"] == "running"
    assert g["current_tool"]["name"] == "read_file"
    assert g["current_tool"]["kind"] == "read"  # inferred from the name (event carries no kind)
    assert g["current_tool"]["locations"] == ["x.py"]  # mined from the raw input JSON
    # the matching end transitions the SAME current tool + appends a lifecycle event
    coder_seam.progress_tool("f", 1, {"phase": "end", "id": "t1", "name": "read_file", "status": "completed"})
    g2 = coder_seam.progress_snapshot("f")["gens"][0]
    assert g2["current_tool"]["status"] == "completed"
    assert len(g2["recent_tools"]) == 2  # start + end


def test_recent_tools_history_is_capped():
    coder_seam._progress.clear()
    coder_seam.progress_begin("f", 1)
    for i in range(300):
        coder_seam.progress_tool("f", 1, {"phase": "start", "id": "t%d" % i, "name": "n%d" % i})
    assert len(coder_seam.progress_snapshot("f")["gens"][0]["recent_tools"]) == coder_seam._RECENT_TOOLS_MAX


def test_thought_tail_is_a_rolling_500_char_string_never_per_word():
    """The bound is the whole point (#84): a coalesced rolling tail, NOT a growing
    list of per-word chunks — the last N chars, ending on the most recent thought."""
    coder_seam._progress.clear()
    coder_seam.progress_begin("f", 1)
    for i in range(1000):
        coder_seam.progress_thought("f", 1, "word%d " % i)
    tail = coder_seam.progress_snapshot("f")["gens"][0]["thought_tail"]
    assert isinstance(tail, str)
    assert len(tail) <= coder_seam._THOUGHT_TAIL_MAX == 500
    assert tail.endswith("word999 ")  # the TAIL — most recent
    assert "word0 " not in tail  # the head rolled off


def test_usage_records_used_and_size():
    coder_seam._progress.clear()
    coder_seam.progress_begin("f", 1)
    coder_seam.progress_usage("f", 1, {"used": 5, "size": 50})
    assert coder_seam.progress_snapshot("f")["gens"][0]["usage"] == {"used": 5, "size": 50}


def test_verify_outcome_is_recorded_per_gen():
    coder_seam._progress.clear()
    coder_seam.progress_begin("f", 1)
    coder_seam.progress_verify("f", 1, test_cmd="pytest -q", output="1 passed", passed=True)
    v = coder_seam.progress_snapshot("f")["gens"][0]["verify"]
    assert v == {"test_cmd": "pytest -q", "passed": True, "tail": "1 passed"}


def test_snapshot_orders_gens_ascending():
    coder_seam._progress.clear()
    coder_seam.progress_begin("f", 3)
    coder_seam.progress_begin("f", 1)
    coder_seam.progress_begin("f", 2)
    assert [g["gen"] for g in coder_seam.progress_snapshot("f")["gens"]] == [1, 2, 3]


def test_progress_new_run_clears_prior_gens():
    coder_seam._progress.clear()
    coder_seam.progress_begin("f", 1)
    coder_seam.progress_begin("f", 2)
    assert len(coder_seam.progress_snapshot("f")["gens"]) == 2
    coder_seam.progress_new_run("f")
    assert coder_seam.progress_snapshot("f") == {"gens": []}


def test_registry_evicts_the_oldest_features_beyond_the_cap():
    """A long-lived loop can't leak memory — the registry keeps only the most
    recent _MAX_FEATURES features, LRU-evicting the oldest."""
    coder_seam._progress.clear()
    n = coder_seam._MAX_FEATURES
    for i in range(n + 5):
        coder_seam.progress_begin("f%d" % i, 1)
    assert coder_seam.progress_snapshot("f0") == {"gens": []}  # evicted
    assert coder_seam.progress_snapshot("f%d" % (n + 4))["gens"]  # newest retained


def test_elapsed_s_is_monotonic_and_nonnegative(monkeypatch):
    coder_seam._progress.clear()
    clock = {"t": 100.0}
    monkeypatch.setattr(coder_seam, "_monotonic", lambda: clock["t"])
    coder_seam.progress_begin("f", 1)
    clock["t"] = 104.5
    assert coder_seam.progress_snapshot("f")["gens"][0]["elapsed_s"] == 4.5


# ── the input miners ──────────────────────────────────────────────────────────


def test_extract_locations_mines_path_keys_and_dedups():
    assert coder_seam._extract_locations('{"path": "a.py"}') == ["a.py"]
    assert coder_seam._extract_locations('{"paths": ["a.py", "b.py", "a.py"]}') == ["a.py", "b.py"]
    assert coder_seam._extract_locations("not json") == []
    assert coder_seam._extract_locations("") == []


def test_infer_tool_kind_from_name():
    assert coder_seam._infer_tool_kind("read_file") == "read"
    assert coder_seam._infer_tool_kind("edit_file") == "edit"
    assert coder_seam._infer_tool_kind("bash") == "execute"
    assert coder_seam._infer_tool_kind("grep") == "search"
    assert coder_seam._infer_tool_kind("mystery") == ""


# ── the tap: fallback path (host absent) still records the gen ──────────────────


async def test_dispatch_coder_tapped_falls_back_and_still_records_the_gen(monkeypatch):
    """No protoAgent host here (the standalone CI case), so the tap can't wire the
    ACP callbacks — it must fall back to worktree.dispatch_coder and STILL register
    the gen (start/tier) so the drawer shows the run even without a live stream."""
    coder_seam._progress.clear()
    seen = {}

    async def _fake(coder, wt, prompt, *, timeout=None, env_passthrough=()):
        seen["args"] = (wt, prompt, timeout)
        return "the reply"

    monkeypatch.setattr(worktree, "dispatch_coder", _fake)
    out = await coder_seam.dispatch_coder_tapped(
        object(), "/wt/x", "do it", fid="bd-1", gen=2, tier="smart", timeout=None
    )
    assert out == "the reply"
    assert seen["args"] == ("/wt/x", "do it", None)
    snap = coder_seam.progress_snapshot("bd-1")
    assert [g["gen"] for g in snap["gens"]] == [2]
    assert snap["gens"][0]["tier"] == "smart"


# ── the tap end-to-end through dispatch() (solve/Verdict faked, no coder plugin) ──


@dataclass
class _FakeVerdict:
    passed: bool
    total: int = 0
    failed: int = 0
    failing: list = field(default_factory=list)
    output: str = ""

    def feedback(self) -> str:
        return "" if self.passed else f"{self.failed}/{self.total} failing: {self.output}"


@dataclass
class _FakeResult:
    solution: str | None
    passed: bool | None
    rung: str
    gens_spent: int
    candidates_tried: int
    verdict: _FakeVerdict | None = None
    note: str = ""


class _FakeBudget:
    def __init__(self, total):
        self.total = total


async def test_dispatch_records_per_gen_progress_including_the_verify_outcome(monkeypatch):
    coder_seam._progress.clear()

    async def _create(repo, base, cid, root, **_kw):
        return (f"/wt/feat-{cid}", f"feat/{cid}")

    async def _dispatch(coder, wt, prompt, *, timeout=None, env_passthrough=()):
        return f"reply {wt}"

    async def _remove(repo, wt, branch=""):
        return None

    async def _promote(repo, src_wt, src_branch, fid, root=".worktrees", title=""):
        return (f"/wt/feat-{fid}", f"feat/{fid}")

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)  # tap falls back to this (no host)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    monkeypatch.setattr(worktree, "promote_worktree", _promote)

    async def _proc(*a, **k):
        class _P:
            returncode = 0

            async def communicate(self):
                return (b"1 passed in 0.01s", None)

        return _P()

    monkeypatch.setattr("asyncio.create_subprocess_shell", _proc)  # verify's test subprocess → pass

    async def _fake_solve(task, *, generate, verify, budget, k, tree_depth, fusion_generate=None, fusion_k=2):
        wt = await generate(task, feedback=None)  # gen 1 (records via the tap)
        v = await verify(wt)  # records the per-gen verify outcome
        return _FakeResult(solution=wt, passed=True, rung="greedy", gens_spent=1, candidates_tried=1, verdict=v)

    wt, branch, result = await coder_seam.dispatch(
        task="t",
        coder=object(),
        repo="/repo",
        base="main",
        root=".worktrees",
        fid="bd-9",
        dispatch_timeout=None,
        test_cmd="pytest -q",
        test_timeout=30,
        budget=6,
        k=3,
        tree_depth=2,
        tier="fast",
        _solve=_fake_solve,
        _budget_cls=_FakeBudget,
        _verdict_cls=_FakeVerdict,
    )
    snap = coder_seam.progress_snapshot("bd-9")
    assert [g["gen"] for g in snap["gens"]] == [1]
    g = snap["gens"][0]
    assert g["tier"] == "fast"
    assert g["verify"] is not None
    assert g["verify"]["passed"] is True
    assert g["verify"]["test_cmd"] == "pytest -q"
    assert "1 passed" in g["verify"]["tail"]


def test_progress_end_freezes_elapsed_and_surfaces_done(monkeypatch):
    """Panel on #89: a finished gen must be distinguishable from a running one — done
    surfaces in the snapshot and elapsed_s freezes at progress_end."""
    import project_board.coder_seam as cs

    cs.progress_new_run("bd-t1")
    clock = [100.0]
    monkeypatch.setattr(cs, "_monotonic", lambda: clock[0])
    cs.progress_begin("bd-t1", 1, "smart")
    clock[0] = 105.0
    cs.progress_end("bd-t1", 1)
    clock[0] = 999.0  # long after — a frozen clock must not keep counting
    snap = cs.progress_snapshot("bd-t1")
    g = snap["gens"][0]
    assert g["done"] is True
    assert g["elapsed_s"] == 5.0


def test_progress_end_is_idempotent(monkeypatch):
    """Every dispatch exit path may call progress_end — the first close wins."""
    import project_board.coder_seam as cs

    cs.progress_new_run("bd-t2")
    clock = [10.0]
    monkeypatch.setattr(cs, "_monotonic", lambda: clock[0])
    cs.progress_begin("bd-t2", 1, "smart")
    clock[0] = 12.0
    cs.progress_end("bd-t2", 1)
    clock[0] = 50.0
    cs.progress_end("bd-t2", 1)  # second close: no-op
    assert cs.progress_snapshot("bd-t2")["gens"][0]["elapsed_s"] == 2.0


# ── richer monitor signals: answer tail, live plan, tool-input preview ──────────


def test_answer_tail_is_a_rolling_bounded_string():
    """The coder's streamed ANSWER text (text_callback) rolls exactly like the
    thought tail — bounded chars, never per-chunk accumulation."""
    coder_seam._progress.clear()
    coder_seam.progress_begin("bd-a", 1)
    coder_seam.progress_answer("bd-a", 1, "x" * 800)
    coder_seam.progress_answer("bd-a", 1, "TAIL")
    (g,) = coder_seam.progress_snapshot("bd-a")["gens"]
    assert g["answer_tail"].endswith("TAIL")
    assert len(g["answer_tail"]) == coder_seam._ANSWER_TAIL_MAX


def test_plan_is_latest_wins_sanitized_and_capped():
    """ACP `plan` updates carry the ENTIRE current plan each time — replace, never
    append; entries sanitized to content/status/priority and capped."""
    coder_seam._progress.clear()
    coder_seam.progress_begin("bd-p", 1)
    coder_seam.progress_plan("bd-p", 1, [{"content": "old", "status": "completed"}])
    entries = [{"content": f"step {i}", "status": "pending", "priority": "medium", "junk": object()} for i in range(60)]
    entries[0]["status"] = "in_progress"
    coder_seam.progress_plan("bd-p", 1, entries)
    (g,) = coder_seam.progress_snapshot("bd-p")["gens"]
    assert len(g["plan"]) == coder_seam._PLAN_ENTRIES_MAX
    assert g["plan"][0] == {"content": "step 0", "status": "in_progress", "priority": "medium"}
    assert all(set(e) == {"content", "status", "priority"} for e in g["plan"])
    # a None sample (older host without last_plan) leaves the recorded plan alone
    coder_seam.progress_plan("bd-p", 1, None)
    assert coder_seam.progress_snapshot("bd-p")["gens"][0]["plan"] is not None


def test_tool_start_records_an_input_preview():
    """The raw input's head is the "what exactly is it running" line — kept as a
    bounded preview on current_tool alongside the mined locations."""
    coder_seam._progress.clear()
    coder_seam.progress_begin("bd-i", 1)
    long_cmd = '{"command": "' + "pytest -q " * 40 + '"}'
    coder_seam.progress_tool("bd-i", 1, {"phase": "start", "id": "t1", "name": "bash", "input": long_cmd})
    (g,) = coder_seam.progress_snapshot("bd-i")["gens"]
    cur = g["current_tool"]
    assert cur["input_preview"] == long_cmd[: coder_seam._TOOL_INPUT_PREVIEW_MAX]
    assert len(cur["input_preview"]) == coder_seam._TOOL_INPUT_PREVIEW_MAX


# ── the tap: PUBLIC seam present (C1) streams live signals into the buffer ────────


@dataclass(frozen=True)
class _TappedResult:
    """Stand-in for the host's ``TappedResult`` — what C1's ``dispatch_tapped``
    returns. Mirrors the real shape (the host is not importable here): the reply plus
    the end-of-turn wire signals the seam returns instead of forwarding as callbacks."""

    reply: str
    usage: dict | None = None
    plan: list | None = None
    stop_reason: str | None = None
    dead_end: str | None = None


@dataclass
class _FakeCoder:
    workdir: str = ""
    manage_git: bool = True
    env: dict = field(default_factory=dict)


async def test_dispatch_coder_tapped_streams_the_public_seam_into_the_buffer(monkeypatch):
    """r2: when C1's public ``dispatch_tapped`` seam is present (injected as a fake),
    the tap drives it — not the untapped fallback — and its forwarded thought/tool/
    answer/usage/plan/stop callbacks all land on this gen's live buffer (#84)."""
    coder_seam._progress.clear()

    def _no_fallback(*a, **k):
        raise AssertionError("must not fall back when the public seam is present")

    monkeypatch.setattr(worktree, "dispatch_coder", _no_fallback)

    async def _fake_seam(delegate, prompt, *, on_tool=None, on_thought=None, on_text=None, timeout=None):
        # C1's real signature — three keyword-only stream callbacks + timeout, no
        # **kwargs, and the wire signals (usage/plan/stop_reason) on the RESULT.
        await on_thought("weighing options")
        await on_tool({"phase": "start", "id": "t1", "name": "bash", "input": '{"command": "pytest -q"}'})
        await on_tool({"phase": "end", "id": "t1", "name": "bash", "status": "completed"})
        await on_text("finished.")
        return _TappedResult(
            reply="seam reply",
            usage={"used": 12, "size": 120},
            plan=[{"content": "run the tests", "status": "in_progress", "priority": "high"}],
            stop_reason="end_turn",
        )

    out = await coder_seam.dispatch_coder_tapped(
        _FakeCoder(),
        "/wt/cand",
        "do it",
        fid="bd-seam",
        gen=2,
        tier="smart",
        _dispatch_tapped=_fake_seam,
    )
    assert out == "seam reply"
    (g,) = coder_seam.progress_snapshot("bd-seam")["gens"]
    assert g["gen"] == 2 and g["tier"] == "smart"
    assert "weighing options" in g["thought_tail"]
    assert g["current_tool"]["name"] == "bash" and g["current_tool"]["status"] == "completed"
    assert g["current_tool"]["kind"] == "execute"
    assert len(g["recent_tools"]) == 2  # start + end forwarded through the tap
    assert g["usage"] == {"used": 12, "size": 120}
    assert g["plan"][0] == {"content": "run the tests", "status": "in_progress", "priority": "high"}
    assert g["answer_tail"].endswith("finished.")
    assert g["stop_reason"] == "end_turn"
    assert g["done"] is True


# ── #291: the snapshot persist rides OFF the event loop when a loop is running ────
# progress_end is a SYNC hook called from dispatch_coder_tapped's finally ON the
# event-loop thread; the blocking store.comment write (a `br` subprocess) must hop to
# a worker so it never stalls the tick or trips the #258 event-loop warning. With no
# running loop (sync callers, tests) the same write runs inline exactly as before.


class _ThreadRecordingStore:
    """Records the thread `comment()` runs on (and whether a running loop was present
    in that thread) so a test can prove the write ran ON or OFF the loop thread. Sets
    a threading.Event so the caller can wait for the fire-and-forget worker to land."""

    def __init__(self):
        self.thread_id: int | None = None
        self.on_running_loop: bool | None = None
        self.comments: list[tuple[str, str]] = []
        self.done = threading.Event()

    def comment(self, fid, text):
        self.thread_id = threading.get_ident()
        try:
            asyncio.get_running_loop()
            self.on_running_loop = True
        except RuntimeError:
            self.on_running_loop = False
        self.comments.append((fid, text))
        self.done.set()


async def test_persist_runs_off_the_loop_thread_under_a_running_loop(monkeypatch):
    """r1: with a running event loop (progress_end fired from dispatch_coder_tapped's
    finally), the blocking store.comment runs on a WORKER thread — a different thread
    than the loop's — not inline on the loop thread."""
    cs = coder_seam
    cs.progress_new_run("bd-291a")
    store = _ThreadRecordingStore()
    monkeypatch.setattr(cs, "_store_factory", lambda: store)

    cs.progress_begin("bd-291a", 1, "smart")
    loop_thread = threading.get_ident()
    cs.progress_end("bd-291a", 1)  # sync hook, on the loop thread — must offload

    # Wait for the fire-and-forget worker WITHOUT blocking the loop thread itself.
    landed = await asyncio.get_running_loop().run_in_executor(None, store.done.wait, 5.0)
    assert landed, "the offloaded snapshot write never ran"
    assert len(store.comments) == 1
    assert store.comments[0][0] == "bd-291a"
    assert store.comments[0][1].startswith("coder-monitor: ")
    assert store.thread_id != loop_thread  # ran OFF the loop thread


def test_persist_runs_inline_when_no_loop_is_running(monkeypatch):
    """r2: with NO running loop (a synchronous caller / test), the store.comment write
    runs INLINE on the caller's own thread — same behavior as before #291."""
    cs = coder_seam
    cs.progress_new_run("bd-291b")
    store = _ThreadRecordingStore()
    monkeypatch.setattr(cs, "_store_factory", lambda: store)

    cs.progress_begin("bd-291b", 1, "fast")
    cs.progress_end("bd-291b", 1)  # no loop here → inline, no worker needed

    assert len(store.comments) == 1  # already written, synchronously
    assert store.thread_id == threading.get_ident()  # this very thread
    assert store.on_running_loop is False


async def test_generation_end_does_not_trip_the_258_blocking_warning(monkeypatch):
    """r3: a store whose comment() exercises the REAL #258 event-loop guard
    (``store._warn_blocking_on_event_loop``) must NOT log the blocking warning when the
    write is offloaded — the worker thread has no running loop, so the guard
    short-circuits. This is the #258 warning no longer firing on a generation end."""
    cs = coder_seam
    cs.progress_new_run("bd-291c")

    warnings: list = []
    monkeypatch.setattr(store_mod.log, "warning", lambda *a, **k: warnings.append(a))

    done = threading.Event()

    class _RealGuardStore:
        def comment(self, fid, text):
            # The same guard the real store's `_run` invokes before every `br` shell —
            # it warns ONLY when a running loop is present in this thread (#258).
            store_mod._warn_blocking_on_event_loop("comments")
            done.set()

    monkeypatch.setattr(cs, "_store_factory", lambda: _RealGuardStore())
    cs.progress_begin("bd-291c", 1, "smart")
    cs.progress_end("bd-291c", 1)  # on the loop thread → write hops to a worker

    landed = await asyncio.get_running_loop().run_in_executor(None, done.wait, 5.0)
    assert landed, "the offloaded snapshot write never ran"
    assert warnings == []  # the #258 blocking-`br comments` warning did NOT fire


def test_sync_caller_write_still_trips_the_guard_on_the_loop_thread(monkeypatch):
    """Control for r3: the #258 guard DOES warn when a loop actually runs in the write's
    thread — proving the r3 test's silence comes from the offload, not a dead guard.
    Here the write runs inline INSIDE a running loop (no offload wrapper), so the guard
    fires exactly as the pre-#291 on-loop persist did."""
    warnings: list = []
    monkeypatch.setattr(store_mod.log, "warning", lambda *a, **k: warnings.append(a))

    async def _drive():
        store_mod._warn_blocking_on_event_loop("comments")

    asyncio.run(_drive())
    assert warnings, "the #258 guard must still warn when a blocking call runs on a loop thread"
