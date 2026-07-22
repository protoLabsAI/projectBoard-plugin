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

from dataclasses import dataclass, field

from project_board import coder_seam, worktree


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


async def testdispatch_coder_tapped_falls_back_and_still_records_the_gen(monkeypatch):
    """No protoAgent host here (the standalone CI case), so the tap can't wire the
    ACP callbacks — it must fall back to worktree.dispatch_coder and STILL register
    the gen (start/tier) so the drawer shows the run even without a live stream."""
    coder_seam._progress.clear()
    seen = {}

    async def _fake(coder, wt, prompt, *, timeout=None):
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

    async def _create(repo, base, cid, root):
        return (f"/wt/feat-{cid}", f"feat/{cid}")

    async def _dispatch(coder, wt, prompt, *, timeout=None):
        return f"reply {wt}"

    async def _remove(repo, wt, branch=""):
        return None

    async def _promote(repo, src_wt, src_branch, fid, root=".worktrees"):
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
